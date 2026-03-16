"""
Data ingestion and technical indicator computation.
Fetches and caches bar data for multiple timeframes.
Computes RSI, SMA, ATR, VWAP, and volume averages using pandas + numpy.
"""

import logging
from typing import Optional, Dict, List
from datetime import datetime

import numpy as np
import pandas as pd

from core.alpaca_client import get_bars, get_multi_symbol_bars
from config import ATR_PERIOD, RSI_PERIOD, SWING_SMA_FAST, SWING_SMA_SLOW, SWING_SMA_ADAPTIVE_FAST

logger = logging.getLogger(__name__)

# In-memory cache: { "AAPL_5Min": (timestamp, DataFrame), ... }
_bar_cache: Dict[str, tuple] = {}
CACHE_TTL_SECONDS = 300  # 5 minutes


# ─── Manual Indicator Functions ───

def _calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Calculate Relative Strength Index manually."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi


def _calc_atr(high: pd.Series, low: pd.Series, close: pd.Series,
              period: int = 14) -> pd.Series:
    """Calculate Average True Range manually."""
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = true_range.rolling(window=period, min_periods=period).mean()
    return atr


def _calc_sma(series: pd.Series, period: int) -> pd.Series:
    """Calculate Simple Moving Average."""
    return series.rolling(window=period, min_periods=period).mean()


# ─── Caching ───

def _cache_key(symbol: str, timeframe: str) -> str:
    """Generate cache key for a symbol + timeframe combination."""
    return f"{symbol}_{timeframe}"


def _is_cache_valid(key: str) -> bool:
    """Check if cached data is still fresh."""
    if key not in _bar_cache:
        return False
    cached_time, _ = _bar_cache[key]
    return (datetime.now() - cached_time).total_seconds() < CACHE_TTL_SECONDS


def clear_cache():
    """Clear all cached bar data. Called at the start of each scan cycle."""
    _bar_cache.clear()
    logger.debug("Bar data cache cleared.")


def fetch_bars(symbol: str, timeframe: str = "5Min", limit: int = 100,
               use_cache: bool = True) -> Optional[pd.DataFrame]:
    """
    Fetch bar data with caching to avoid redundant API calls.

    Args:
        symbol: Stock ticker
        timeframe: '1Min', '5Min', '1Hour', '1Day'
        limit: Number of bars
        use_cache: Whether to use cached data

    Returns:
        DataFrame with OHLCV columns, or None
    """
    key = _cache_key(symbol, timeframe)

    if use_cache and _is_cache_valid(key):
        logger.debug(f"Cache hit for {key}")
        return _bar_cache[key][1].copy()

    df = get_bars(symbol, timeframe=timeframe, limit=limit)
    if df is None or df.empty:
        logger.warning(f"No data returned for {symbol} ({timeframe})")
        return None

    # Cache the raw data
    _bar_cache[key] = (datetime.now(), df.copy())
    return df


def compute_indicators(df: pd.DataFrame, include_sma: bool = False,
                       timeframe: str = "") -> pd.DataFrame:
    """
    Compute technical indicators on a DataFrame with OHLCV columns.

    Args:
        df: DataFrame with open, high, low, close, volume columns
        include_sma: If True, compute SMA 50 and SMA 200 (for swing trades)
        timeframe: Optional timeframe hint; daily data auto-enables SMAs

    Returns:
        DataFrame with additional indicator columns
    """
    # Auto-enable SMAs for daily timeframes
    if timeframe in ("1Day", "daily"):
        include_sma = True
    if df is None or df.empty:
        return df

    df = df.copy()

    # RSI
    df["rsi"] = _calc_rsi(df["close"], period=RSI_PERIOD)

    # ATR
    df["atr"] = _calc_atr(df["high"], df["low"], df["close"], period=ATR_PERIOD)

    # Volume moving average (20-period)
    df["volume_avg_20"] = df["volume"].rolling(window=20).mean()

    # VWAP (cumulative for intraday)
    if "vwap" not in df.columns:
        typical_price = (df["high"] + df["low"] + df["close"]) / 3
        df["vwap"] = (typical_price * df["volume"]).cumsum() / df["volume"].cumsum()

    # SMAs for swing trade analysis
    if include_sma:
        df[f"sma_{SWING_SMA_ADAPTIVE_FAST}"] = _calc_sma(df["close"], period=SWING_SMA_ADAPTIVE_FAST)
        df[f"sma_{SWING_SMA_FAST}"] = _calc_sma(df["close"], period=SWING_SMA_FAST)
        df[f"sma_{SWING_SMA_SLOW}"] = _calc_sma(df["close"], period=SWING_SMA_SLOW)

    return df


def get_intraday_data(symbol: str, limit: int = 100) -> Optional[pd.DataFrame]:
    """Fetch 5-minute bars with intraday indicators (RSI, ATR, VWAP, volume avg)."""
    df = fetch_bars(symbol, timeframe="5Min", limit=limit)
    if df is None:
        return None
    return compute_indicators(df, include_sma=False)


def get_daily_data(symbol: str, limit: int = 250) -> Optional[pd.DataFrame]:
    """Fetch daily bars with all indicators including SMAs."""
    df = fetch_bars(symbol, timeframe="1Day", limit=limit)
    if df is None:
        return None
    return compute_indicators(df, include_sma=True)


def get_intraday_data_batch(symbols: List[str], limit: int = 100,
                            batch_size: int = 100) -> Dict[str, pd.DataFrame]:
    """Fetch 5-minute bars for many symbols at once, with indicators computed per symbol."""
    raw = get_multi_symbol_bars(symbols, timeframe="5Min", limit=limit, batch_size=batch_size)
    result = {}
    for sym, df in raw.items():
        try:
            enriched = compute_indicators(df, include_sma=False)
            if enriched is not None:
                result[sym] = enriched
        except Exception as e:
            logger.warning(f"Failed to compute intraday indicators for {sym}: {e}")
    return result


def get_daily_data_batch(symbols: List[str], limit: int = 60,
                         batch_size: int = 100) -> Dict[str, pd.DataFrame]:
    """Fetch daily bars for many symbols at once, with indicators computed per symbol."""
    raw = get_multi_symbol_bars(symbols, timeframe="1Day", limit=limit, batch_size=batch_size)
    result = {}
    for sym, df in raw.items():
        try:
            enriched = compute_indicators(df, include_sma=True)
            if enriched is not None:
                result[sym] = enriched
        except Exception as e:
            logger.warning(f"Failed to compute daily indicators for {sym}: {e}")
    return result


def get_sma_slope(df: pd.DataFrame, sma_column: str, periods: int = 5) -> Optional[float]:
    """
    Calculate the slope of an SMA over the last N bars.
    Positive slope = bullish trend, Negative = bearish.

    Returns:
        Slope value or None if insufficient data
    """
    if df is None or sma_column not in df.columns:
        return None

    sma_values = df[sma_column].dropna().tail(periods)
    if len(sma_values) < periods:
        return None

    # Simple slope: (last - first) / periods
    return (sma_values.iloc[-1] - sma_values.iloc[0]) / periods

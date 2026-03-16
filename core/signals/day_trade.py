"""
Day Trade Signal Engine.
Strategy: Volume-weighted momentum breakouts with multi-layer filtering.

Entry conditions (ALL must be true):
  1. Price breaks above VWAP with volume > 1.5x 20-period avg
  2. RSI(14) between DAY_RSI_ENTRY_THRESHOLD (60) and DAY_RSI_ENTRY_CEILING (75)
  3. Volume is not a blow-off top (< DAY_VOLUME_SPIKE_CAP x avg)
  4. Intraday ATR meets minimum threshold (stock has real volatility)
  5. Breakout confirmation: previous bar also closed above VWAP (not just this bar)
  6. Daily trend alignment:
     - Daily SMA50 slope is not negative
     - Daily RSI > DAY_DAILY_RSI_FLOOR (45)
     - Price is above daily SMA50 (if available)
  7. Price deviation check: current price is within 10% of last daily close
  8. Daily data quality: sufficient bars and non-NaN indicators
"""

import logging
import math
from typing import Optional, Dict, Any, Callable

import pandas as pd

from core.data_ingestion import get_intraday_data, get_daily_data, get_sma_slope
from db.database import log_heartbeat
from config import (
    DAY_RSI_ENTRY_THRESHOLD, DAY_RSI_ENTRY_CEILING,
    DAY_VOLUME_MULTIPLIER, DAY_VOLUME_SPIKE_CAP,
    DAY_USE_DAILY_ATR, DAY_MIN_INTRADAY_ATR,
    DAY_MAX_PRICE_DEVIATION_PCT, DAY_REQUIRE_CONFIRMATION,
    DAY_DAILY_RSI_FLOOR, DAY_REQUIRE_ABOVE_SMA50,
    DAY_MIN_DAILY_BARS,
    SWING_SMA_FAST,
)

logger = logging.getLogger(__name__)


def _log_signal_event(symbol: str, signal_type: str, reason: str,
                      indicators: dict = None):
    """Log a scanner signal event to the heartbeat with structured data."""
    detail = {"symbol": symbol, "signal": signal_type, "reason": reason}
    if indicators:
        detail["indicators"] = indicators
    event = "signal" if signal_type == "buy" else "rejection"
    level = "info" if signal_type == "buy" else "info"
    log_heartbeat(
        f"[{symbol}] {signal_type.upper()}: {reason}",
        level=level,
        event_type=event,
        detail=detail,
    )


def generate_signal(symbol: str, intraday_df: Optional[pd.DataFrame] = None,
                    daily_df: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
    """
    Generate a day trade signal for a symbol.

    Args:
        symbol: Stock ticker
        intraday_df: Pre-fetched 5-min DataFrame (optional, will fetch if None)
        daily_df: Pre-fetched daily DataFrame (optional, will fetch if None)

    Returns:
        Dict with keys: signal ('buy', 'sell', 'hold'), reason, symbol, data
    """
    result = {
        "symbol": symbol,
        "signal": "hold",
        "reason": "",
        "entry_price": None,
        "atr": None,
        # Enriched data for trade logging (full decision context)
        "_intraday_df": None,
        "_daily_df": None,
        "_indicator_values": {},
    }

    # ── Fetch intraday data ──────────────────────────────────────────
    if intraday_df is None:
        intraday_df = get_intraday_data(symbol, limit=100)
    if intraday_df is None or len(intraday_df) < 20:
        result["reason"] = "Insufficient intraday data"
        return result

    # Get the latest bar and previous bar
    latest = intraday_df.iloc[-1]
    prev = intraday_df.iloc[-2] if len(intraday_df) > 1 else latest

    # Check required columns exist
    required_cols = ["close", "vwap", "volume", "rsi", "atr", "volume_avg_20"]
    for col in required_cols:
        if col not in intraday_df.columns or pd.isna(latest.get(col)):
            result["reason"] = f"Missing indicator: {col}"
            return result

    current_price = float(latest["close"])
    vwap = float(latest["vwap"])
    volume = float(latest["volume"])
    rsi = float(latest["rsi"])
    intraday_atr = float(latest["atr"])
    volume_avg = float(latest["volume_avg_20"])

    result["entry_price"] = current_price
    result["_intraday_df"] = intraday_df
    result["_daily_df"] = daily_df
    result["_indicator_values"] = {
        "current_price": current_price,
        "vwap": vwap,
        "volume": volume,
        "rsi": rsi,
        "atr": intraday_atr,
        "volume_avg_20": volume_avg,
    }

    # ── Fetch and validate daily data ────────────────────────────────
    if daily_df is None:
        daily_df = get_daily_data(symbol, limit=60)
        result["_daily_df"] = daily_df

    # Daily data quality gate: require sufficient bars and non-NaN indicators
    daily_atr = None
    daily_rsi = None
    daily_close = None
    daily_sma50 = None

    if daily_df is not None and len(daily_df) >= DAY_MIN_DAILY_BARS:
        daily_latest = daily_df.iloc[-1]
        daily_close = float(daily_latest["close"]) if not pd.isna(daily_latest.get("close")) else None
        daily_atr = float(daily_latest["atr"]) if not pd.isna(daily_latest.get("atr")) else None
        daily_rsi = float(daily_latest["rsi"]) if not pd.isna(daily_latest.get("rsi")) else None
        sma_col = f"sma_{SWING_SMA_FAST}"
        if sma_col in daily_df.columns:
            daily_sma50 = float(daily_latest[sma_col]) if not pd.isna(daily_latest.get(sma_col)) else None
    else:
        result["signal"] = "hold"
        result["reason"] = (
            f"Insufficient daily data: need {DAY_MIN_DAILY_BARS} bars, "
            f"have {len(daily_df) if daily_df is not None else 0}. "
            f"Cannot validate trend or calculate proper ATR."
        )
        logger.info(f"[{symbol}] {result['reason']}")
        return result

    # Require daily ATR to be available (critical for proper stop/profit sizing)
    if daily_atr is None or daily_atr <= 0:
        result["signal"] = "hold"
        result["reason"] = (
            f"Daily ATR unavailable or zero — cannot calculate proper stop/profit levels. "
            f"Skipping to avoid absurdly tight stops."
        )
        logger.info(f"[{symbol}] {result['reason']}")
        return result

    # Use daily ATR for stop/profit calculations (not the tiny intraday ATR)
    if DAY_USE_DAILY_ATR:
        result["atr"] = daily_atr
    else:
        result["atr"] = intraday_atr

    result["_indicator_values"]["daily_atr"] = daily_atr
    result["_indicator_values"]["daily_rsi"] = daily_rsi
    result["_indicator_values"]["daily_close"] = daily_close
    result["_indicator_values"]["daily_sma50"] = daily_sma50

    # ── BUY Signal: Core conditions ──────────────────────────────────
    price_above_vwap = current_price > vwap
    volume_surge = volume > (volume_avg * DAY_VOLUME_MULTIPLIER) if volume_avg > 0 else False
    rsi_bullish = rsi > DAY_RSI_ENTRY_THRESHOLD

    if not (price_above_vwap and volume_surge and rsi_bullish):
        # --- Check SELL signal (for open positions) ---
        price_below_vwap = current_price < vwap
        rsi_overbought = rsi > 70

        if price_below_vwap and rsi_overbought:
            result["signal"] = "sell"
            result["reason"] = f"Price below VWAP and RSI overbought ({rsi:.1f})"
            return result

        # --- HOLD: Core conditions not met ---
        reasons = []
        if not price_above_vwap:
            reasons.append(f"price ${current_price:.2f} <= VWAP ${vwap:.2f}")
        if not volume_surge:
            reasons.append(f"volume {volume:.0f} <= {DAY_VOLUME_MULTIPLIER}x avg")
        if not rsi_bullish:
            reasons.append(f"RSI {rsi:.1f} <= {DAY_RSI_ENTRY_THRESHOLD}")
        result["reason"] = "No signal — " + ", ".join(reasons)
        return result

    # ── BUY Signal: Quality filters (any failure = hold) ─────────────

    # Helper: build indicator snapshot for rejection logging
    def _rejection_indicators(**extra):
        base = {"price": current_price, "vwap": vwap, "rsi": round(rsi, 1),
                "volume": volume, "volume_avg": volume_avg}
        base.update(extra)
        return base

    # Filter 1: RSI ceiling — reject overbought exhaustion
    if rsi > DAY_RSI_ENTRY_CEILING:
        result["signal"] = "hold"
        result["reason"] = (
            f"RSI ceiling rejection — RSI {rsi:.1f} > {DAY_RSI_ENTRY_CEILING} (overbought exhaustion). "
            f"Buying at extreme RSI typically means entering at the top of the move."
        )
        logger.info(f"[{symbol}] {result['reason']}")
        _log_signal_event(symbol, "rejected", result["reason"],
                          _rejection_indicators(filter="rsi_ceiling"))
        return result

    # Filter 2: Volume spike cap — reject blow-off tops
    volume_ratio = volume / volume_avg if volume_avg > 0 else 0
    if volume_ratio > DAY_VOLUME_SPIKE_CAP:
        result["signal"] = "hold"
        result["reason"] = (
            f"Volume spike cap rejection — volume ratio {volume_ratio:.1f}x > "
            f"{DAY_VOLUME_SPIKE_CAP}x (likely blow-off top, move already happened)."
        )
        logger.info(f"[{symbol}] {result['reason']}")
        _log_signal_event(symbol, "rejected", result["reason"],
                          _rejection_indicators(filter="volume_spike", volume_ratio=round(volume_ratio, 1)))
        return result

    # Filter 3: Minimum intraday ATR — skip dead/illiquid stocks
    if intraday_atr < DAY_MIN_INTRADAY_ATR:
        result["signal"] = "hold"
        result["reason"] = (
            f"Minimum ATR rejection — intraday ATR ${intraday_atr:.4f} < "
            f"${DAY_MIN_INTRADAY_ATR} (stock barely moves, too illiquid)."
        )
        logger.info(f"[{symbol}] {result['reason']}")
        _log_signal_event(symbol, "rejected", result["reason"],
                          _rejection_indicators(filter="min_atr", intraday_atr=round(intraday_atr, 4)))
        return result

    # Filter 4: Price deviation check — current price vs last daily close
    if daily_close and daily_close > 0:
        deviation = abs(current_price - daily_close) / daily_close
        if deviation > DAY_MAX_PRICE_DEVIATION_PCT:
            result["signal"] = "hold"
            result["reason"] = (
                f"Price deviation rejection — current ${current_price:.2f} is "
                f"{deviation*100:.1f}% from daily close ${daily_close:.2f} "
                f"(>{DAY_MAX_PRICE_DEVIATION_PCT*100:.0f}% limit). "
                f"Extreme gap/spike likely to revert."
            )
            logger.info(f"[{symbol}] {result['reason']}")
            _log_signal_event(symbol, "rejected", result["reason"],
                              _rejection_indicators(filter="price_deviation", deviation_pct=round(deviation * 100, 1)))
            return result

    # Filter 5: Breakout confirmation — previous bar must also be above VWAP
    if DAY_REQUIRE_CONFIRMATION and len(intraday_df) > 1:
        prev_close = float(prev["close"])
        prev_vwap = float(prev.get("vwap", vwap))
        if prev_close <= prev_vwap:
            result["signal"] = "hold"
            result["reason"] = (
                f"Breakout confirmation rejection — previous bar close ${prev_close:.2f} "
                f"<= VWAP ${prev_vwap:.2f}. Waiting for confirmation "
                f"(need 2 consecutive bars above VWAP, not just the breakout bar)."
            )
            logger.info(f"[{symbol}] {result['reason']}")
            _log_signal_event(symbol, "rejected", result["reason"],
                              _rejection_indicators(filter="breakout_confirm"))
            return result

    # Filter 6: Daily trend — SMA50 slope
    sma_col = f"sma_{SWING_SMA_FAST}"
    if daily_df is not None and sma_col in daily_df.columns:
        slope = get_sma_slope(daily_df, sma_col, periods=5)
        result["_indicator_values"]["daily_sma50_slope"] = slope
        if slope is not None and slope < 0:
            result["signal"] = "hold"
            result["reason"] = (
                f"Trend filter rejection — Daily 50 SMA slope is bearish ({slope:.4f}). "
                f"VWAP breakout + volume + RSI conditions met but daily trend is down."
            )
            logger.info(f"[{symbol}] {result['reason']}")
            _log_signal_event(symbol, "rejected", result["reason"],
                              _rejection_indicators(filter="trend_sma50", sma50_slope=round(slope, 4)))
            return result

    # Filter 7: Daily RSI floor — don't buy when daily timeframe is bearish
    if daily_rsi is not None and daily_rsi < DAY_DAILY_RSI_FLOOR:
        result["signal"] = "hold"
        result["reason"] = (
            f"Daily RSI rejection — daily RSI {daily_rsi:.1f} < {DAY_DAILY_RSI_FLOOR} "
            f"(daily timeframe is bearish, intraday breakout is likely a trap)."
        )
        logger.info(f"[{symbol}] {result['reason']}")
        _log_signal_event(symbol, "rejected", result["reason"],
                          _rejection_indicators(filter="daily_rsi", daily_rsi=round(daily_rsi, 1)))
        return result

    # Filter 8: Price above SMA50 — confluence with daily trend
    if DAY_REQUIRE_ABOVE_SMA50 and daily_sma50 is not None:
        if current_price < daily_sma50:
            result["signal"] = "hold"
            result["reason"] = (
                f"Below SMA50 rejection — price ${current_price:.2f} < daily SMA50 "
                f"${daily_sma50:.2f}. Intraday breakout against daily resistance."
            )
            logger.info(f"[{symbol}] {result['reason']}")
            _log_signal_event(symbol, "rejected", result["reason"],
                              _rejection_indicators(filter="below_sma50", daily_sma50=daily_sma50))
            return result

    # ── All filters passed — GENERATE BUY SIGNAL ─────────────────────
    result["signal"] = "buy"
    result["reason"] = (
        f"VWAP breakout: price ${current_price:.2f} > VWAP ${vwap:.2f}, "
        f"volume {volume:.0f} ({volume_ratio:.1f}x avg), "
        f"RSI {rsi:.1f} [{DAY_RSI_ENTRY_THRESHOLD}-{DAY_RSI_ENTRY_CEILING}], "
        f"daily ATR ${daily_atr:.2f}, daily RSI {f'{daily_rsi:.1f}' if daily_rsi else 'N/A'}"
    )
    logger.info(f"[{symbol}] BUY signal: {result['reason']}")
    _log_signal_event(symbol, "buy", result["reason"], {
        "price": current_price, "vwap": vwap,
        "volume": volume, "volume_ratio": round(volume_ratio, 1),
        "rsi": round(rsi, 1), "daily_atr": round(daily_atr, 2),
        "daily_rsi": round(daily_rsi, 1) if daily_rsi else None,
    })
    return result


def generate_signals_batch(symbols: list,
                           intraday_cache: Optional[Dict[str, pd.DataFrame]] = None,
                           daily_cache: Optional[Dict[str, pd.DataFrame]] = None,
                           on_buy=None) -> list:
    """
    Generate day trade signals for a list of symbols.

    Args:
        symbols: List of stock tickers
        intraday_cache: Pre-fetched intraday DataFrames keyed by symbol
        daily_cache: Pre-fetched daily DataFrames keyed by symbol
        on_buy: Optional callback fired immediately when a buy signal is
                detected: ``on_buy(signal_dict) -> None``.  This allows the
                scheduler to execute orders as soon as they are found instead
                of waiting for the entire scan to finish.

    Returns:
        List of signal dicts
    """
    signals = []
    for symbol in symbols:
        intraday_df = intraday_cache.get(symbol) if intraday_cache else None
        daily_df = daily_cache.get(symbol) if daily_cache else None
        sig = generate_signal(symbol, intraday_df=intraday_df, daily_df=daily_df)
        signals.append(sig)

        # Fire the callback immediately so the order is placed while the
        # scan continues processing remaining symbols.
        if on_buy and sig["signal"] == "buy":
            try:
                on_buy(sig)
            except Exception as e:
                logger.error(f"[{symbol}] on_buy callback failed: {e}")

    return signals

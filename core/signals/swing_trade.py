"""
Swing Trade Signal Engine (AGGRESSIVE VARIANT).
Strategy: Trend following with 4 entry types + adaptive MA + ratchet trailing stop.

Entry types:
  1. Golden Cross — SMA fast crosses above SMA slow (classic trend reversal)
  2. Mean Reversion — RSI < SWING_RSI_OVERSOLD while price > SMA slow (buy the dip)
  3. Pullback to SMA50 — Price dips to within 1-2% of SMA50 in confirmed uptrend
  4. Sustained Uptrend — SMA fast > SMA slow for 5+ days AND price near SMA fast

Adaptive MA:
  When SMA200 is unavailable (insufficient data), falls back to SMA20/SMA50 pair.
  This ensures newer stocks (e.g. recent IPOs/spinoffs) still get swing entries.

Exit signals:
  - Death Cross (SMA fast crosses below SMA slow)
  - Trailing stop managed by risk_manager.py (with ratchet at +20%)
"""

import logging
from typing import Optional, Dict, Any

import pandas as pd

from core.data_ingestion import get_daily_data
from config import (
    SWING_SMA_FAST, SWING_SMA_SLOW,
    SWING_SMA_ADAPTIVE_FAST, SWING_SMA_ADAPTIVE_SLOW,
    SWING_RSI_OVERSOLD,
    SWING_USE_PULLBACK_ENTRY, SWING_USE_SUSTAINED_UPTREND,
    SWING_USE_ADAPTIVE_MA,
)

logger = logging.getLogger(__name__)

# Minimum bars needed — reduced from SMA_SLOW+5 when adaptive MA is available
_MIN_BARS_STRICT = SWING_SMA_SLOW + 5   # 205 bars for SMA200
_MIN_BARS_ADAPTIVE = SWING_SMA_ADAPTIVE_SLOW + 5  # 55 bars for SMA50 fallback


def _get_ma_columns(daily_df: pd.DataFrame) -> tuple:
    """
    Determine which MA columns to use. If SMA200 is available, use SMA50/SMA200.
    If not and adaptive MA is enabled, fall back to SMA20/SMA50.

    Returns:
        (fast_col, slow_col, is_adaptive) or (None, None, False) if unavailable
    """
    sma_fast_col = f"sma_{SWING_SMA_FAST}"
    sma_slow_col = f"sma_{SWING_SMA_SLOW}"
    sma_adap_fast_col = f"sma_{SWING_SMA_ADAPTIVE_FAST}"
    sma_adap_slow_col = f"sma_{SWING_SMA_ADAPTIVE_SLOW}"

    latest = daily_df.iloc[-1]

    # Try primary MAs first (SMA50/SMA200)
    if sma_slow_col in daily_df.columns and not pd.isna(latest.get(sma_slow_col)):
        if sma_fast_col in daily_df.columns and not pd.isna(latest.get(sma_fast_col)):
            return sma_fast_col, sma_slow_col, False

    # Fall back to adaptive MAs (SMA20/SMA50) if enabled
    if SWING_USE_ADAPTIVE_MA:
        if sma_adap_slow_col in daily_df.columns and not pd.isna(latest.get(sma_adap_slow_col)):
            if sma_adap_fast_col in daily_df.columns and not pd.isna(latest.get(sma_adap_fast_col)):
                return sma_adap_fast_col, sma_adap_slow_col, True

    return None, None, False


def generate_signal(symbol: str, daily_df: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
    """
    Generate a swing trade signal for a symbol.

    Args:
        symbol: Stock ticker
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
        "entry_type": None,  # NEW: tracks which entry type triggered
        # Enriched data for trade logging (full decision context)
        "_daily_df": None,
        "_indicator_values": {},
    }

    # Fetch daily data if not provided
    if daily_df is None:
        daily_df = get_daily_data(symbol, limit=250)

    # Determine minimum bars based on whether adaptive MA is available
    min_bars = _MIN_BARS_ADAPTIVE if SWING_USE_ADAPTIVE_MA else _MIN_BARS_STRICT
    if daily_df is None or len(daily_df) < min_bars:
        result["reason"] = f"Insufficient daily data (need {min_bars} bars, have {len(daily_df) if daily_df is not None else 0})"
        return result

    # Determine which MAs to use
    fast_col, slow_col, is_adaptive = _get_ma_columns(daily_df)
    if fast_col is None:
        result["reason"] = "No valid MA pair available (SMA200 and adaptive both unavailable)"
        return result

    # Check required indicators exist
    for col in [fast_col, slow_col, "rsi", "atr"]:
        if col not in daily_df.columns:
            result["reason"] = f"Missing indicator: {col}"
            return result

    latest = daily_df.iloc[-1]
    prev = daily_df.iloc[-2]

    current_price = float(latest["close"])
    sma_fast = float(latest[fast_col])
    sma_slow = float(latest[slow_col])
    prev_sma_fast = float(prev[fast_col])
    prev_sma_slow = float(prev[slow_col])
    rsi = float(latest["rsi"]) if not pd.isna(latest["rsi"]) else 50
    atr = float(latest["atr"]) if not pd.isna(latest["atr"]) else 0

    result["entry_price"] = current_price
    result["atr"] = atr
    result["_daily_df"] = daily_df
    result["_indicator_values"] = {
        "current_price": current_price,
        "sma_fast": sma_fast,
        "sma_slow": sma_slow,
        "prev_sma_fast": prev_sma_fast,
        "prev_sma_slow": prev_sma_slow,
        "rsi": rsi,
        "atr": atr,
        "sma_fast_col": fast_col,
        "sma_slow_col": slow_col,
        "is_adaptive_ma": is_adaptive,
    }

    # ═══════════════════════════════════════════════════════════════
    # EXIT SIGNALS (checked first — exits take priority)
    # ═══════════════════════════════════════════════════════════════

    # Death Cross SELL Signal
    death_cross = (sma_fast < sma_slow) and (prev_sma_fast >= prev_sma_slow)
    if death_cross:
        ma_label = "adaptive " if is_adaptive else ""
        result["signal"] = "sell"
        result["reason"] = (
            f"Death Cross ({ma_label}{fast_col}/{slow_col}): "
            f"{fast_col} ({sma_fast:.2f}) crossed below "
            f"{slow_col} ({sma_slow:.2f})"
        )
        logger.info(f"[{symbol}] SELL signal (Death Cross): {result['reason']}")
        return result

    # ═══════════════════════════════════════════════════════════════
    # ENTRY SIGNALS (priority order: Golden Cross > Mean Reversion >
    #                Pullback to SMA50 > Sustained Uptrend)
    # ═══════════════════════════════════════════════════════════════

    entry_reason = None
    entry_type = None

    # --- 1. Golden Cross BUY Signal ---
    golden_cross = (sma_fast > sma_slow) and (prev_sma_fast <= prev_sma_slow)
    if golden_cross:
        ma_label = "adaptive " if is_adaptive else ""
        entry_type = "golden_cross"
        entry_reason = (
            f"Golden Cross ({ma_label}{fast_col}/{slow_col}): "
            f"{fast_col} ({sma_fast:.2f}) crossed above "
            f"{slow_col} ({sma_slow:.2f})"
        )

    # --- 2. Mean Reversion BUY Signal ---
    # RSI < SWING_RSI_OVERSOLD AND price still in uptrend (above slow SMA)
    if not entry_reason:
        in_uptrend = current_price > sma_slow
        rsi_oversold = rsi < SWING_RSI_OVERSOLD

        if rsi_oversold and in_uptrend:
            entry_type = "mean_reversion"
            entry_reason = (
                f"Mean reversion: RSI {rsi:.1f} < {SWING_RSI_OVERSOLD} while price "
                f"${current_price:.2f} > {slow_col} ${sma_slow:.2f} (uptrend intact)"
            )

    # --- 3. Pullback to SMA50 in Confirmed Uptrend (NEW) ---
    if not entry_reason and SWING_USE_PULLBACK_ENTRY:
        sma50_col = f"sma_{SWING_SMA_FAST}"
        sma200_col = f"sma_{SWING_SMA_SLOW}"

        if sma50_col in daily_df.columns and sma200_col in daily_df.columns:
            sma50_val = latest.get(sma50_col)
            sma200_val = latest.get(sma200_col)

            if not pd.isna(sma50_val) and not pd.isna(sma200_val):
                sma50_val = float(sma50_val)
                sma200_val = float(sma200_val)

                # Confirmed uptrend: SMA50 > SMA200
                if sma50_val > sma200_val:
                    # Price pulled back to within 1% below to 2% above SMA50
                    if current_price >= sma50_val * 0.99 and current_price <= sma50_val * 1.02 and rsi < 50:
                        entry_type = "pullback_sma50"
                        entry_reason = (
                            f"Pullback to SMA50: price ${current_price:.2f} near "
                            f"SMA50 ${sma50_val:.2f} (within 1-2%), RSI {rsi:.1f}, "
                            f"confirmed uptrend (SMA50 ${sma50_val:.2f} > SMA200 ${sma200_val:.2f})"
                        )

    # --- 4. Sustained Uptrend Re-entry (NEW) ---
    if not entry_reason and SWING_USE_SUSTAINED_UPTREND:
        # Check if fast SMA has been above slow SMA for 5+ consecutive days
        if len(daily_df) >= 6:
            sustained = True
            for k in range(1, 6):
                prev_row = daily_df.iloc[-(k + 1)]
                pf = prev_row.get(fast_col)
                ps = prev_row.get(slow_col)
                if pd.isna(pf) or pd.isna(ps) or float(pf) <= float(ps):
                    sustained = False
                    break

            if sustained and sma_fast > sma_slow:
                # Only enter on a dip: RSI < 55 and price near fast SMA (within 3%)
                if rsi < 55 and current_price <= sma_fast * 1.03:
                    entry_type = "sustained_uptrend"
                    entry_reason = (
                        f"Sustained uptrend re-entry: {fast_col} > {slow_col} for 5+ days, "
                        f"price ${current_price:.2f} near {fast_col} ${sma_fast:.2f} (within 3%), "
                        f"RSI {rsi:.1f} < 55 (mild dip in strong trend)"
                    )

    # ═══════════════════════════════════════════════════════════════
    # EMIT SIGNAL
    # ═══════════════════════════════════════════════════════════════

    if entry_reason:
        result["signal"] = "buy"
        result["reason"] = entry_reason
        result["entry_type"] = entry_type
        logger.info(f"[{symbol}] BUY signal ({entry_type}): {entry_reason}")
        return result

    # --- HOLD ---
    trend = "bullish" if sma_fast > sma_slow else "bearish"
    ma_label = "adaptive " if is_adaptive else ""
    result["reason"] = (
        f"No signal — {ma_label}trend is {trend} "
        f"({fast_col}={sma_fast:.2f}, {slow_col}={sma_slow:.2f}), "
        f"RSI={rsi:.1f}"
    )
    return result


def generate_signals_batch(symbols: list,
                           daily_cache: Optional[Dict[str, pd.DataFrame]] = None) -> list:
    """
    Generate swing trade signals for a list of symbols.

    Args:
        symbols: List of stock tickers
        daily_cache: Pre-fetched daily DataFrames keyed by symbol

    Returns:
        List of signal dicts
    """
    signals = []
    for symbol in symbols:
        daily_df = daily_cache.get(symbol) if daily_cache else None
        sig = generate_signal(symbol, daily_df=daily_df)
        signals.append(sig)
    return signals

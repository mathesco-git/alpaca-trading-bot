"""
Trade Decision Logger.

Writes one JSON file per trade to trade_logs/<date>/<trade_id>_<symbol>.json.
Each file captures the FULL decision context at entry and is updated at exit,
creating a complete audit trail for post-trade analysis and algorithm improvement.

Log structure:
    - meta: trade ID, timestamps, strategy, symbol
    - entry_snapshot: all indicators, raw bars, sentiment, signal reasoning
    - risk_snapshot: position sizing inputs/outputs, stop/TP calculations
    - portfolio_snapshot: equity, buying power, open positions, exposure
    - config_snapshot: all active config parameters relevant to this strategy
    - market_context: watchlist size, scan source info
    - exit_snapshot: (appended on close) exit indicators, reason, P&L, duration
"""

import json
import logging
import os
from datetime import datetime, date
from pathlib import Path
from typing import Optional, Dict, Any, List

import pandas as pd

import config

logger = logging.getLogger(__name__)

# Base directory for trade logs (configurable via config.TRADE_LOG_DIR)
def _get_log_dir() -> Path:
    base = getattr(config, "TRADE_LOG_DIR", "trade_logs")
    return Path(base)


def _get_today_dir() -> Path:
    """Get or create today's log directory: trade_logs/YYYY-MM-DD/"""
    today_dir = _get_log_dir() / date.today().isoformat()
    today_dir.mkdir(parents=True, exist_ok=True)
    return today_dir


def _make_serializable(obj: Any) -> Any:
    """Recursively convert objects to JSON-serializable types."""
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    if isinstance(obj, pd.Series):
        return {str(k): _make_serializable(v) for k, v in obj.to_dict().items()}
    if isinstance(obj, pd.DataFrame):
        return _dataframe_to_records(obj)
    if isinstance(obj, dict):
        return {str(k): _make_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_make_serializable(item) for item in obj]
    if hasattr(obj, '__float__'):
        try:
            import math
            val = float(obj)
            if math.isnan(val) or math.isinf(val):
                return None
            return round(val, 6)
        except (ValueError, TypeError):
            return str(obj)
    return str(obj)


def _dataframe_to_records(df: pd.DataFrame, last_n: int = 20) -> List[Dict]:
    """Convert the last N rows of a DataFrame to a list of dicts."""
    if df is None or df.empty:
        return []
    subset = df.tail(last_n).copy()
    # Convert index to column if it's a DatetimeIndex
    if isinstance(subset.index, pd.DatetimeIndex):
        subset = subset.reset_index()
        if "timestamp" not in subset.columns and "index" in subset.columns:
            subset = subset.rename(columns={"index": "timestamp"})
    records = subset.to_dict(orient="records")
    return [_make_serializable(r) for r in records]


def _extract_indicator_snapshot(df: pd.DataFrame) -> Dict[str, Any]:
    """
    Extract the latest indicator values from a DataFrame.
    Returns a flat dict of all computed indicators at the most recent bar.
    """
    if df is None or df.empty:
        return {}

    latest = df.iloc[-1]
    snapshot = {}

    indicator_cols = [
        "open", "high", "low", "close", "volume",
        "vwap", "rsi", "atr", "volume_avg_20",
        "sma_50", "sma_200",
    ]

    for col in indicator_cols:
        if col in df.columns:
            val = latest.get(col)
            snapshot[col] = _make_serializable(val)

    # Add some derived context
    if "close" in df.columns and len(df) >= 2:
        prev_close = float(df.iloc[-2]["close"])
        curr_close = float(latest["close"])
        snapshot["bar_change_pct"] = round((curr_close - prev_close) / prev_close * 100, 4) if prev_close else None

    if "volume" in df.columns and "volume_avg_20" in df.columns:
        vol = latest.get("volume")
        avg = latest.get("volume_avg_20")
        if vol and avg and avg > 0:
            snapshot["volume_ratio"] = round(float(vol) / float(avg), 4)

    return snapshot


def _get_config_snapshot(strategy_type: str) -> Dict[str, Any]:
    """Capture all config parameters relevant to the trade's strategy."""
    common = {
        "ENABLE_TRADING": config.ENABLE_TRADING,
        "MAX_RISK_PER_TRADE": config.MAX_RISK_PER_TRADE,
        "MAX_POSITION_VALUE_PCT": config.MAX_POSITION_VALUE_PCT,
        "ATR_PERIOD": config.ATR_PERIOD,
        "RSI_PERIOD": config.RSI_PERIOD,
        "ENABLE_SENTIMENT": config.ENABLE_SENTIMENT,
        "SENTIMENT_WEIGHT": config.SENTIMENT_WEIGHT,
        "SENTIMENT_BULLISH_THRESHOLD": config.SENTIMENT_BULLISH_THRESHOLD,
        "SENTIMENT_BEARISH_THRESHOLD": config.SENTIMENT_BEARISH_THRESHOLD,
    }

    if strategy_type == "day":
        common.update({
            "DAY_TRADE_ALLOCATION": config.DAY_TRADE_ALLOCATION,
            "DAY_MAX_POSITIONS": config.DAY_MAX_POSITIONS,
            "DAY_STOP_MULTIPLIER": config.DAY_STOP_MULTIPLIER,
            "DAY_PROFIT_MULTIPLIER": config.DAY_PROFIT_MULTIPLIER,
            "DAY_RSI_ENTRY_THRESHOLD": config.DAY_RSI_ENTRY_THRESHOLD,
            "DAY_VOLUME_MULTIPLIER": config.DAY_VOLUME_MULTIPLIER,
            "DAY_SCAN_INTERVAL_MINUTES": config.DAY_SCAN_INTERVAL_MINUTES,
        })
    else:
        common.update({
            "SWING_TRADE_ALLOCATION": config.SWING_TRADE_ALLOCATION,
            "SWING_MAX_POSITIONS": config.SWING_MAX_POSITIONS,
            "SWING_STOP_MULTIPLIER": config.SWING_STOP_MULTIPLIER,
            "SWING_POSITION_SIZE_REDUCTION": config.SWING_POSITION_SIZE_REDUCTION,
            "SWING_SMA_FAST": config.SWING_SMA_FAST,
            "SWING_SMA_SLOW": config.SWING_SMA_SLOW,
            "SWING_RSI_OVERSOLD": config.SWING_RSI_OVERSOLD,
        })

    return common


def log_trade_entry(
    trade_id: int,
    symbol: str,
    strategy_type: str,
    signal: Dict[str, Any],
    shares: int,
    entry_price: float,
    stop_loss: float,
    take_profit: Optional[float],
    atr: float,
    portfolio_equity: float,
    buying_power: float,
    intraday_df: Optional[pd.DataFrame] = None,
    daily_df: Optional[pd.DataFrame] = None,
    sentiment_data: Optional[Dict[str, Any]] = None,
    market_context: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """
    Log a complete trade entry snapshot to a JSON file.

    Args:
        trade_id: Database trade ID
        symbol: Stock ticker
        strategy_type: 'day' or 'swing'
        signal: Full signal dict from signal engine
        shares: Number of shares traded
        entry_price: Entry price
        stop_loss: Stop-loss price
        take_profit: Take-profit price (None for swing)
        atr: ATR value used for sizing
        portfolio_equity: Portfolio equity at time of trade
        buying_power: Buying power at time of trade
        intraday_df: 5-min bar DataFrame (if available)
        daily_df: Daily bar DataFrame (if available)
        sentiment_data: Sentiment analysis result (if available)
        market_context: Additional context (watchlist size, scan cycle, etc.)

    Returns:
        File path of the log file, or None on error
    """
    try:
        timestamp = datetime.utcnow()
        filename = f"{trade_id}_{symbol}_{strategy_type}.json"
        filepath = _get_today_dir() / filename

        # --- Build the full log entry ---
        log_entry = {
            "meta": {
                "trade_id": trade_id,
                "symbol": symbol,
                "strategy_type": strategy_type,
                "side": "buy",
                "entry_timestamp_utc": timestamp.isoformat(),
                "log_version": "1.0",
            },

            "signal": {
                "action": signal.get("signal"),
                "reason": signal.get("reason"),
                "entry_price": signal.get("entry_price"),
                "atr": signal.get("atr"),
                "sentiment_score": signal.get("sentiment_score"),
                "sentiment_label": signal.get("sentiment_label"),
                "sentiment_articles": signal.get("sentiment_articles"),
                "raw_signal": _make_serializable(signal),
            },

            "entry_snapshot": {
                "intraday_indicators": _extract_indicator_snapshot(intraday_df) if intraday_df is not None else None,
                "daily_indicators": _extract_indicator_snapshot(daily_df) if daily_df is not None else None,
                "intraday_bars_last_20": _dataframe_to_records(intraday_df, last_n=20) if intraday_df is not None else None,
                "daily_bars_last_10": _dataframe_to_records(daily_df, last_n=10) if daily_df is not None else None,
            },

            "risk_snapshot": {
                "shares": shares,
                "entry_price": entry_price,
                "position_value": round(entry_price * shares, 2),
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "atr": atr,
                "risk_per_share": round(entry_price - stop_loss, 2) if stop_loss else None,
                "total_risk_dollars": round((entry_price - stop_loss) * shares, 2) if stop_loss else None,
                "risk_pct_of_equity": round((entry_price - stop_loss) * shares / portfolio_equity * 100, 4) if stop_loss and portfolio_equity > 0 else None,
                "reward_risk_ratio": round((take_profit - entry_price) / (entry_price - stop_loss), 2) if take_profit and stop_loss and (entry_price - stop_loss) > 0 else None,
                "position_pct_of_equity": round(entry_price * shares / portfolio_equity * 100, 4) if portfolio_equity > 0 else None,
            },

            "portfolio_snapshot": {
                "equity": portfolio_equity,
                "buying_power": buying_power,
                "position_value": round(entry_price * shares, 2),
            },

            "sentiment_snapshot": _make_serializable(sentiment_data) if sentiment_data else None,

            "config_snapshot": _get_config_snapshot(strategy_type),

            "market_context": _make_serializable(market_context) if market_context else None,

            "exit_snapshot": None,  # Filled in on exit
        }

        # Write JSON
        with open(filepath, "w") as f:
            json.dump(log_entry, f, indent=2, default=str)

        logger.info(f"[TradeLog] Entry logged: {filepath}")
        return str(filepath)

    except Exception as e:
        logger.error(f"[TradeLog] Failed to log entry for trade {trade_id} ({symbol}): {e}")
        return None


def log_trade_exit(
    trade_id: int,
    symbol: str,
    strategy_type: str,
    exit_price: float,
    exit_reason: str,
    exit_status: str,
    pnl: float,
    entry_price: float,
    shares: int,
    entry_time: Optional[datetime] = None,
    intraday_df: Optional[pd.DataFrame] = None,
    daily_df: Optional[pd.DataFrame] = None,
) -> bool:
    """
    Append exit data to the existing trade log JSON file.

    Searches for the entry log file and adds the exit_snapshot section.
    If the entry log is not found (e.g., from a previous day), creates
    a standalone exit log.

    Returns:
        True if exit was logged successfully
    """
    try:
        timestamp = datetime.utcnow()

        # Build exit snapshot
        exit_snapshot = {
            "exit_timestamp_utc": timestamp.isoformat(),
            "exit_price": exit_price,
            "exit_reason": exit_reason,
            "exit_status": exit_status,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / (entry_price * shares) * 100, 4) if entry_price and shares else None,
            "shares": shares,
            "entry_price": entry_price,
            "hold_duration_seconds": (timestamp - entry_time).total_seconds() if entry_time else None,
            "hold_duration_human": str(timestamp - entry_time) if entry_time else None,
            "exit_intraday_indicators": _extract_indicator_snapshot(intraday_df) if intraday_df is not None else None,
            "exit_daily_indicators": _extract_indicator_snapshot(daily_df) if daily_df is not None else None,
        }

        # Try to find and update the entry log file
        filepath = _find_trade_log(trade_id, symbol, strategy_type)

        if filepath and os.path.exists(filepath):
            # Update existing log
            with open(filepath, "r") as f:
                log_entry = json.load(f)

            log_entry["exit_snapshot"] = exit_snapshot

            with open(filepath, "w") as f:
                json.dump(log_entry, f, indent=2, default=str)

            logger.info(f"[TradeLog] Exit logged (updated): {filepath}")
        else:
            # Create standalone exit log (trade may have been opened on a different day)
            filename = f"{trade_id}_{symbol}_{strategy_type}_exit.json"
            filepath = _get_today_dir() / filename

            standalone = {
                "meta": {
                    "trade_id": trade_id,
                    "symbol": symbol,
                    "strategy_type": strategy_type,
                    "side": "buy",
                    "note": "Standalone exit log — entry log not found (may be from a previous day)",
                    "log_version": "1.0",
                },
                "exit_snapshot": exit_snapshot,
            }

            with open(filepath, "w") as f:
                json.dump(standalone, f, indent=2, default=str)

            logger.info(f"[TradeLog] Exit logged (standalone): {filepath}")

        return True

    except Exception as e:
        logger.error(f"[TradeLog] Failed to log exit for trade {trade_id} ({symbol}): {e}")
        return False


def _find_trade_log(trade_id: int, symbol: str, strategy_type: str) -> Optional[str]:
    """
    Search for the entry log file for a given trade.
    Checks today's directory first, then recent days (up to 30 days back).
    """
    filename = f"{trade_id}_{symbol}_{strategy_type}.json"
    log_dir = _get_log_dir()

    if not log_dir.exists():
        return None

    # Check today first
    today_path = _get_today_dir() / filename
    if today_path.exists():
        return str(today_path)

    # Check recent date directories (for swing trades that span multiple days)
    try:
        date_dirs = sorted(
            [d for d in log_dir.iterdir() if d.is_dir()],
            key=lambda d: d.name,
            reverse=True
        )
        for d in date_dirs[:30]:  # Look back up to 30 days
            candidate = d / filename
            if candidate.exists():
                return str(candidate)
    except Exception:
        pass

    return None


def log_rejected_trade(
    symbol: str,
    strategy_type: str,
    signal: Dict[str, Any],
    rejection_reason: str,
    portfolio_equity: float = 0,
    buying_power: float = 0,
    intraday_df: Optional[pd.DataFrame] = None,
    daily_df: Optional[pd.DataFrame] = None,
    sentiment_data: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """
    Log a trade that was REJECTED by risk management or other filters.
    These are valuable for understanding why the bot passed on opportunities
    (or correctly avoided bad trades).

    Controlled by config.TRADE_LOG_REJECTED (default True).

    Returns:
        File path of the log file, or None on error
    """
    if not getattr(config, "TRADE_LOG_REJECTED", True):
        return None

    try:
        timestamp = datetime.utcnow()
        ts_str = timestamp.strftime("%H%M%S")
        filename = f"REJECTED_{symbol}_{strategy_type}_{ts_str}.json"
        filepath = _get_today_dir() / filename

        log_entry = {
            "meta": {
                "symbol": symbol,
                "strategy_type": strategy_type,
                "type": "rejected",
                "timestamp_utc": timestamp.isoformat(),
                "rejection_reason": rejection_reason,
                "log_version": "1.0",
            },
            "signal": _make_serializable(signal),
            "indicators": {
                "intraday": _extract_indicator_snapshot(intraday_df) if intraday_df is not None else None,
                "daily": _extract_indicator_snapshot(daily_df) if daily_df is not None else None,
            },
            "portfolio": {
                "equity": portfolio_equity,
                "buying_power": buying_power,
            },
            "sentiment": _make_serializable(sentiment_data) if sentiment_data else None,
        }

        with open(filepath, "w") as f:
            json.dump(log_entry, f, indent=2, default=str)

        logger.debug(f"[TradeLog] Rejected trade logged: {filepath}")
        return str(filepath)

    except Exception as e:
        logger.error(f"[TradeLog] Failed to log rejected trade for {symbol}: {e}")
        return None

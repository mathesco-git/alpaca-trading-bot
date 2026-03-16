"""
Phase 2: Performance Analytics Module.

Calculates key portfolio metrics from closed trade history:
    - Sharpe Ratio (annualized)
    - Max Drawdown ($ and %)
    - Win Rate (overall, by strategy)
    - Profit Factor
    - Average Win / Average Loss
    - Expectancy (per trade)
    - Total P&L
    - Best / Worst trade
    - Streak tracking (consecutive wins/losses)
    - Daily returns for equity curve analytics
"""

import logging
import math
from datetime import datetime, date, timedelta
from typing import Dict, Any, List, Optional

from db.database import get_db
from db.models import Trade, EquityHistory
import config

logger = logging.getLogger(__name__)


def get_closed_trades(
    strategy: Optional[str] = None,
    lookback_days: Optional[int] = None
) -> List[Dict[str, Any]]:
    """Fetch closed trades from DB, optionally filtered."""
    with get_db() as session:
        query = session.query(Trade).filter(Trade.status.in_(["closed", "stopped_out", "eod_liquidated"]))
        if strategy:
            query = query.filter(Trade.strategy_type == strategy)
        if lookback_days:
            cutoff = datetime.utcnow() - timedelta(days=lookback_days)
            query = query.filter(Trade.exit_time >= cutoff)
        trades = query.order_by(Trade.exit_time.asc()).all()
        return [t.to_dict() for t in trades]


def calculate_analytics(
    strategy: Optional[str] = None,
    lookback_days: Optional[int] = None
) -> Dict[str, Any]:
    """
    Calculate comprehensive performance analytics.

    Returns:
        Dict with all performance metrics, or minimal dict if insufficient data.
    """
    if lookback_days is None:
        lookback_days = config.ANALYTICS_LOOKBACK_DAYS

    trades = get_closed_trades(strategy=strategy, lookback_days=lookback_days)

    result = {
        "total_trades": len(trades),
        "strategy_filter": strategy or "all",
        "lookback_days": lookback_days,
        "sufficient_data": len(trades) >= config.MIN_TRADES_FOR_ANALYTICS,
    }

    if len(trades) < config.MIN_TRADES_FOR_ANALYTICS:
        result.update({
            "message": f"Need at least {config.MIN_TRADES_FOR_ANALYTICS} closed trades for analytics",
            "sharpe_ratio": None, "max_drawdown_pct": None, "win_rate": None,
            "profit_factor": None, "total_pnl": None, "avg_win": None,
            "avg_loss": None, "expectancy": None, "best_trade": None,
            "worst_trade": None, "max_consecutive_wins": None,
            "max_consecutive_losses": None, "by_strategy": {},
        })
        return result

    pnls = [t["pnl"] or 0 for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    scratches = [p for p in pnls if p == 0]

    total_pnl = sum(pnls)
    win_count = len(wins)
    loss_count = len(losses)
    win_rate = win_count / len(pnls) if pnls else 0

    avg_win = sum(wins) / win_count if wins else 0
    avg_loss = sum(losses) / loss_count if losses else 0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf') if gross_profit > 0 else 0

    # Expectancy = (Win% * Avg Win) + (Loss% * Avg Loss)
    loss_rate = loss_count / len(pnls) if pnls else 0
    expectancy = (win_rate * avg_win) + (loss_rate * avg_loss)

    # Best / Worst trade
    best_trade = max(trades, key=lambda t: (t["pnl"] or 0))
    worst_trade = min(trades, key=lambda t: (t["pnl"] or 0))

    # Consecutive streaks
    max_wins, max_losses, cur_wins, cur_losses = 0, 0, 0, 0
    for p in pnls:
        if p > 0:
            cur_wins += 1
            cur_losses = 0
            max_wins = max(max_wins, cur_wins)
        elif p < 0:
            cur_losses += 1
            cur_wins = 0
            max_losses = max(max_losses, cur_losses)
        else:
            cur_wins = 0
            cur_losses = 0

    # Max Drawdown from cumulative P&L
    cumulative = []
    running = 0
    for p in pnls:
        running += p
        cumulative.append(running)

    peak = cumulative[0] if cumulative else 0
    max_dd = 0
    max_dd_pct = 0
    for val in cumulative:
        if val > peak:
            peak = val
        dd = peak - val
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = (dd / peak * 100) if peak > 0 else 0

    # Sharpe Ratio (annualized)
    sharpe = _calculate_sharpe(pnls)

    # Holding periods
    holding_hours = []
    for t in trades:
        if t["entry_time"] and t["exit_time"]:
            entry = datetime.fromisoformat(t["entry_time"])
            exit_ = datetime.fromisoformat(t["exit_time"])
            hours = (exit_ - entry).total_seconds() / 3600
            holding_hours.append(hours)
    avg_holding = sum(holding_hours) / len(holding_hours) if holding_hours else 0

    # By strategy breakdown
    by_strategy = {}
    for strat in ["day", "swing"]:
        strat_trades = [t for t in trades if t["strategy_type"] == strat]
        strat_pnls = [t["pnl"] or 0 for t in strat_trades]
        strat_wins = [p for p in strat_pnls if p > 0]
        strat_losses = [p for p in strat_pnls if p < 0]
        by_strategy[strat] = {
            "total_trades": len(strat_trades),
            "total_pnl": round(sum(strat_pnls), 2),
            "win_rate": round(len(strat_wins) / len(strat_pnls) * 100, 1) if strat_pnls else 0,
            "avg_pnl": round(sum(strat_pnls) / len(strat_pnls), 2) if strat_pnls else 0,
        }

    # By status breakdown
    status_counts = {}
    for t in trades:
        s = t["status"]
        status_counts[s] = status_counts.get(s, 0) + 1

    result.update({
        "total_pnl": round(total_pnl, 2),
        "win_count": win_count,
        "loss_count": loss_count,
        "scratch_count": len(scratches),
        "win_rate": round(win_rate * 100, 1),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": round(profit_factor, 2) if profit_factor != float('inf') else "∞",
        "expectancy": round(expectancy, 2),
        "sharpe_ratio": round(sharpe, 2) if sharpe is not None else None,
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd_pct, 1),
        "best_trade": {
            "symbol": best_trade["symbol"],
            "pnl": best_trade["pnl"],
            "strategy": best_trade["strategy_type"],
            "date": best_trade["exit_time"],
        },
        "worst_trade": {
            "symbol": worst_trade["symbol"],
            "pnl": worst_trade["pnl"],
            "strategy": worst_trade["strategy_type"],
            "date": worst_trade["exit_time"],
        },
        "max_consecutive_wins": max_wins,
        "max_consecutive_losses": max_losses,
        "avg_holding_hours": round(avg_holding, 1),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "by_strategy": by_strategy,
        "status_counts": status_counts,
    })

    return result


def _calculate_sharpe(pnls: List[float]) -> Optional[float]:
    """
    Calculate annualized Sharpe Ratio from trade P&Ls.
    Assumes one trade ≈ one observation; annualizes assuming ~252 trading days.
    """
    if len(pnls) < 2:
        return None

    mean_return = sum(pnls) / len(pnls)
    variance = sum((p - mean_return) ** 2 for p in pnls) / (len(pnls) - 1)
    std_dev = math.sqrt(variance) if variance > 0 else 0

    if std_dev == 0:
        return None

    # Daily risk-free rate (approximate)
    daily_rf = config.RISK_FREE_RATE / 252

    sharpe = (mean_return - daily_rf) / std_dev
    # Annualize: multiply by sqrt(trades per year estimate)
    trades_per_year = min(len(pnls) * (252 / max(1, config.ANALYTICS_LOOKBACK_DAYS)), 252 * 5)
    annualized = sharpe * math.sqrt(trades_per_year)

    return annualized


def get_equity_curve_data(lookback_days: Optional[int] = None) -> List[Dict[str, Any]]:
    """Get equity history data for charting."""
    with get_db() as session:
        query = session.query(EquityHistory).order_by(EquityHistory.date.asc())
        if lookback_days:
            cutoff = date.today() - timedelta(days=lookback_days)
            query = query.filter(EquityHistory.date >= cutoff)
        return [e.to_dict() for e in query.all()]


def get_monthly_summary() -> List[Dict[str, Any]]:
    """Get P&L aggregated by month for each strategy."""
    trades = get_closed_trades()
    monthly = {}

    for t in trades:
        if not t["exit_time"]:
            continue
        dt = datetime.fromisoformat(t["exit_time"])
        key = dt.strftime("%Y-%m")
        if key not in monthly:
            monthly[key] = {"month": key, "day_pnl": 0, "swing_pnl": 0, "trades": 0}
        monthly[key]["trades"] += 1
        pnl = t["pnl"] or 0
        if t["strategy_type"] == "day":
            monthly[key]["day_pnl"] += pnl
        else:
            monthly[key]["swing_pnl"] += pnl

    # Sort by month and round
    result = sorted(monthly.values(), key=lambda x: x["month"])
    for m in result:
        m["day_pnl"] = round(m["day_pnl"], 2)
        m["swing_pnl"] = round(m["swing_pnl"], 2)
        m["total_pnl"] = round(m["day_pnl"] + m["swing_pnl"], 2)

    return result

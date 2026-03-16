"""
Unified Risk Manager (AGGRESSIVE VARIANT).

Handles:
- ATR-based position sizing (4% risk, 20% max position)
- Portfolio allocation enforcement (60/80 day/swing split)
- Max concurrent position checks
- Stop-loss and take-profit calculations
- Pre-trade validation
- Trailing stop with ratchet mechanism (+20% gain -> tighten from 3x to 2x ATR)

Important margin notes:
    - Day Trading uses Day Trading Buying Power (4x equity in US, requires $25k+ for PDT)
    - Swing Trading uses Reg T margin (2x equity)
    - The risk manager tracks these separately. Day trade orders draw from day trade
      allocation, swing trade orders from swing allocation. One must never consume the other.
"""

import logging
import math
from typing import Optional, Dict, Tuple

from db.database import get_db
from db.models import Trade
from config import (
    MAX_RISK_PER_TRADE, MAX_POSITION_VALUE_PCT, ATR_PERIOD,
    DAY_TRADE_ALLOCATION, SWING_TRADE_ALLOCATION,
    DAY_MAX_POSITIONS, SWING_MAX_POSITIONS,
    DAY_STOP_MULTIPLIER, DAY_PROFIT_MULTIPLIER,
    SWING_STOP_MULTIPLIER, SWING_POSITION_SIZE_REDUCTION,
    SWING_RATCHET_ENABLED, SWING_RATCHET_THRESHOLD,
    SWING_RATCHET_STOP_MULTIPLIER,
)

logger = logging.getLogger(__name__)


def calculate_position_size(portfolio_equity: float, atr: float,
                            strategy_type: str, entry_price: float = 0.0,
                            allocation_pct: Optional[float] = None) -> int:
    """
    Calculate number of shares to trade based on ATR risk sizing,
    capped by maximum position value (% of equity).

    Formula: shares = risk_amount / (ATR * stop_multiplier)
    Where: risk_amount = portfolio_equity * MAX_RISK_PER_TRADE * allocation_pct

    The result is then capped so total position cost never exceeds
    MAX_POSITION_VALUE_PCT of portfolio equity.
    """
    if atr <= 0 or portfolio_equity <= 0:
        logger.warning(f"Invalid inputs: equity={portfolio_equity}, atr={atr}")
        return 0

    if allocation_pct is None:
        allocation_pct = DAY_TRADE_ALLOCATION if strategy_type == "day" else SWING_TRADE_ALLOCATION

    stop_multiplier = DAY_STOP_MULTIPLIER if strategy_type == "day" else SWING_STOP_MULTIPLIER
    risk_amount = portfolio_equity * MAX_RISK_PER_TRADE * allocation_pct

    shares = risk_amount / (atr * stop_multiplier)

    # Swing size reduction (now 0% in aggressive config -- no reduction)
    if strategy_type == "swing":
        shares *= (1 - SWING_POSITION_SIZE_REDUCTION)

    shares = max(1, math.floor(shares))

    # Cap position value at MAX_POSITION_VALUE_PCT of equity
    if entry_price > 0:
        max_position_value = portfolio_equity * MAX_POSITION_VALUE_PCT
        max_shares_by_value = math.floor(max_position_value / entry_price)
        if max_shares_by_value < 1:
            max_shares_by_value = 1
        if shares > max_shares_by_value:
            logger.info(
                f"Position capped: {shares} -> {max_shares_by_value} shares "
                f"(max position value ${max_position_value:.2f} at ${entry_price:.2f}/share)"
            )
            shares = max_shares_by_value

    logger.debug(
        f"Position size: {shares} shares (equity=${portfolio_equity:.2f}, "
        f"ATR=${atr:.2f}, strategy={strategy_type}, risk=${risk_amount:.2f})"
    )
    return shares


def calculate_stop_loss(entry_price: float, atr: float, strategy_type: str,
                        side: str = "buy") -> float:
    """Calculate stop-loss price based on ATR multiplier."""
    multiplier = DAY_STOP_MULTIPLIER if strategy_type == "day" else SWING_STOP_MULTIPLIER
    if side.lower() == "buy":
        return round(entry_price - (atr * multiplier), 2)
    else:
        return round(entry_price + (atr * multiplier), 2)


def calculate_take_profit(entry_price: float, atr: float, strategy_type: str,
                          side: str = "buy") -> Optional[float]:
    """Calculate take-profit price. Day trades have fixed TP, swing trades don't."""
    if strategy_type == "swing":
        return None  # Swing trades: let winners run

    multiplier = DAY_PROFIT_MULTIPLIER
    if side.lower() == "buy":
        return round(entry_price + (atr * multiplier), 2)
    else:
        return round(entry_price - (atr * multiplier), 2)


def get_open_position_count(strategy_type: str) -> int:
    """Count open positions for a given strategy type."""
    with get_db() as session:
        count = session.query(Trade).filter(
            Trade.strategy_type == strategy_type,
            Trade.status == "open"
        ).count()
    return count


def get_strategy_exposure(strategy_type: str) -> float:
    """Calculate total dollar exposure for a strategy's open positions."""
    with get_db() as session:
        trades = session.query(Trade).filter(
            Trade.strategy_type == strategy_type,
            Trade.status == "open"
        ).all()
        exposure = sum(t.entry_price * t.quantity for t in trades)
    return exposure


def pre_trade_check(symbol: str, strategy_type: str, shares: int,
                    entry_price: float, portfolio_equity: float,
                    buying_power: float) -> Tuple[bool, str]:
    """
    Validate a trade before execution.

    Checks:
        1. Strategy hasn't exceeded max positions
        2. Strategy hasn't exceeded buying power allocation
        3. Single trade risk doesn't exceed max position value
    """
    # Check 1: Max positions
    max_positions = DAY_MAX_POSITIONS if strategy_type == "day" else SWING_MAX_POSITIONS
    current_positions = get_open_position_count(strategy_type)

    if current_positions >= max_positions:
        reason = (
            f"Max {strategy_type} positions reached: {current_positions}/{max_positions}. "
            f"Cannot open new {symbol} position."
        )
        logger.warning(f"[Risk] {reason}")
        return False, reason

    # Check 2: Buying power allocation
    allocation_pct = DAY_TRADE_ALLOCATION if strategy_type == "day" else SWING_TRADE_ALLOCATION
    max_allocation = buying_power * allocation_pct
    current_exposure = get_strategy_exposure(strategy_type)
    order_cost = entry_price * shares

    if current_exposure + order_cost > max_allocation:
        reason = (
            f"Allocation exceeded for {strategy_type}: current ${current_exposure:.2f} + "
            f"order ${order_cost:.2f} > max ${max_allocation:.2f} "
            f"({allocation_pct*100:.0f}% of ${buying_power:.2f})"
        )
        logger.warning(f"[Risk] {reason}")
        return False, reason

    # Check 3: Single trade cost vs max position value
    trade_risk = entry_price * shares
    max_position_value = portfolio_equity * MAX_POSITION_VALUE_PCT
    if trade_risk > max_position_value:
        reason = (
            f"Single trade cost ${trade_risk:.2f} is too large relative to "
            f"portfolio equity ${portfolio_equity:.2f}"
        )
        logger.warning(f"[Risk] {reason}")
        return False, reason

    logger.info(
        f"[Risk] Pre-trade check PASSED for {symbol} ({strategy_type}): "
        f"{shares} shares @ ${entry_price:.2f}, positions: {current_positions}/{max_positions}"
    )
    return True, "Approved"


def update_trailing_stop(trade: Trade, current_price: float, atr: float) -> Optional[float]:
    """
    Update trailing stop for swing trades with ratchet mechanism.
    Only moves stop up (for long positions), never down.

    Ratchet: Once the trade is up +20% from entry, the trailing stop tightens
    from 3x ATR to 2x ATR to protect accumulated gains.

    Returns:
        New stop-loss price if updated, None if no change
    """
    if trade.side != "buy":
        return None

    # Determine which stop multiplier to use
    stop_mult = SWING_STOP_MULTIPLIER  # Default: 3x ATR

    if SWING_RATCHET_ENABLED and trade.entry_price > 0:
        gain_pct = (current_price - trade.entry_price) / trade.entry_price
        if gain_pct >= SWING_RATCHET_THRESHOLD:
            stop_mult = SWING_RATCHET_STOP_MULTIPLIER  # Tighter: 2x ATR
            logger.debug(
                f"[{trade.symbol}] Ratchet active: gain {gain_pct*100:.1f}% >= "
                f"{SWING_RATCHET_THRESHOLD*100:.0f}%, using {stop_mult}x ATR stop"
            )

    new_stop = round(current_price - (atr * stop_mult), 2)

    if new_stop > trade.stop_loss:
        logger.info(
            f"[{trade.symbol}] Trailing stop updated: ${trade.stop_loss:.2f} -> ${new_stop:.2f} "
            f"(mult={stop_mult}x ATR)"
        )
        return new_stop

    return None

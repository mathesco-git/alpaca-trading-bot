"""
Order execution module.
Submits, cancels, and closes orders via Alpaca.
Respects the ENABLE_TRADING flag — when False, logs intent but skips actual orders.

IMPORTANT: Trades are only recorded to the database AFTER Alpaca confirms the
order has been filled. This prevents ghost positions where the DB thinks we hold
shares that were never actually purchased.
"""

import logging
import time
from datetime import datetime
from typing import Optional, Dict, Any

import config
from core import alpaca_client
from core.risk_manager import (
    calculate_position_size, calculate_stop_loss, calculate_take_profit,
    pre_trade_check
)
from core.trade_logger import log_trade_entry, log_trade_exit, log_rejected_trade
from db.database import get_db, log_heartbeat
from db.models import Trade

logger = logging.getLogger(__name__)

# How long to wait for an order to fill before giving up
ORDER_FILL_TIMEOUT_SECONDS = 30
ORDER_FILL_POLL_INTERVAL = 1.0  # seconds between status checks


def _wait_for_fill(order_id: str, symbol: str, timeout: float = ORDER_FILL_TIMEOUT_SECONDS) -> Optional[Dict[str, Any]]:
    """
    Poll Alpaca until the order is filled, rejected, or times out.

    Returns:
        Order details dict if filled (with filled_avg_price, filled_qty).
        None if the order was not filled (cancelled, rejected, or timed out).
    """
    start = time.time()
    while (time.time() - start) < timeout:
        details = alpaca_client.get_order_details(order_id)
        if not details:
            time.sleep(ORDER_FILL_POLL_INTERVAL)
            continue

        status = details["status"]

        if status == "filled":
            return details

        if status in ("canceled", "cancelled", "expired", "rejected", "suspended"):
            logger.warning(
                f"[{symbol}] Order {order_id} ended with status '{status}' — not filled."
            )
            return None

        # Still pending — keep polling
        time.sleep(ORDER_FILL_POLL_INTERVAL)

    # Timed out — cancel the order to avoid a stale pending order
    logger.warning(
        f"[{symbol}] Order {order_id} did not fill within {timeout}s — cancelling."
    )
    alpaca_client.cancel_order(order_id)
    return None


def execute_entry(symbol: str, strategy_type: str, signal: Dict[str, Any],
                  portfolio_equity: float, buying_power: float,
                  sentiment_data: Optional[Dict] = None,
                  market_context: Optional[Dict] = None) -> Optional[int]:
    """
    Execute a trade entry based on a signal.

    Flow:
      1. Calculate position size and validate risk
      2. Submit market order to Alpaca
      3. Wait for fill confirmation (poll order status)
      4. ONLY THEN record the trade in the database with actual fill price

    This prevents ghost positions where the DB thinks we hold shares
    that were never actually purchased.

    Args:
        symbol: Stock ticker
        strategy_type: 'day' or 'swing'
        signal: Signal dict from the signal engine
        portfolio_equity: Current portfolio equity
        buying_power: Current buying power
        sentiment_data: Optional sentiment analysis data for logging
        market_context: Optional market context for logging

    Returns:
        Trade ID from database on success, None on failure
    """
    entry_price = signal.get("entry_price")
    atr = signal.get("atr")

    if not entry_price or not atr or atr <= 0:
        logger.warning(f"[{symbol}] Invalid signal data: price={entry_price}, atr={atr}")
        return None

    # Calculate position size (with position value cap)
    shares = calculate_position_size(portfolio_equity, atr, strategy_type, entry_price=entry_price)
    if shares <= 0:
        logger.warning(f"[{symbol}] Position size is 0 — skipping")
        log_rejected_trade(
            symbol, strategy_type, signal, "Position size is 0",
            portfolio_equity, buying_power,
            intraday_df=signal.get("_intraday_df"),
            daily_df=signal.get("_daily_df"),
            sentiment_data=sentiment_data,
        )
        return None

    # Pre-trade risk checks
    approved, reason = pre_trade_check(
        symbol, strategy_type, shares, entry_price, portfolio_equity, buying_power
    )
    if not approved:
        log_heartbeat(
            f"Order rejected for {symbol}: {reason}", level="warning",
            event_type="rejection",
            detail={"symbol": symbol, "strategy": strategy_type,
                    "shares": shares, "price": entry_price, "reason": reason},
        )
        log_rejected_trade(
            symbol, strategy_type, signal, reason,
            portfolio_equity, buying_power,
            intraday_df=signal.get("_intraday_df"),
            daily_df=signal.get("_daily_df"),
            sentiment_data=sentiment_data,
        )
        return None

    # Check trading mode
    if not config.ENABLE_TRADING:
        stop_loss = calculate_stop_loss(entry_price, atr, strategy_type)
        take_profit = calculate_take_profit(entry_price, atr, strategy_type)
        logger.info(
            f"MONITOR MODE — order not placed: BUY {shares} {symbol} @ ~${entry_price:.2f} "
            f"({strategy_type}), SL=${stop_loss:.2f}, TP={take_profit}"
        )
        log_heartbeat(
            f"MONITOR MODE — would buy {shares} {symbol} @ ~${entry_price:.2f} ({strategy_type})",
            level="info",
            event_type="order",
            detail={"symbol": symbol, "strategy": strategy_type, "shares": shares,
                    "price": entry_price, "stop_loss": stop_loss,
                    "take_profit": take_profit, "status": "monitor_mode"},
        )
        return None

    # ── Step 1: Submit order to Alpaca ────────────────────────────────
    order_id = alpaca_client.submit_market_order(symbol, shares, "buy")
    if not order_id:
        log_heartbeat(f"Failed to submit buy order for {symbol}", level="error")
        return None

    log_heartbeat(
        f"Order submitted for {symbol}: BUY {shares} shares @ ~${entry_price:.2f} — waiting for fill...",
        level="info",
        event_type="order",
        detail={"symbol": symbol, "strategy": strategy_type, "shares": shares,
                "price": entry_price, "order_id": order_id, "status": "submitted"},
    )

    # ── Step 2: Wait for fill confirmation ────────────────────────────
    fill_details = _wait_for_fill(order_id, symbol)

    if not fill_details:
        log_heartbeat(
            f"Order for {symbol} was NOT filled — trade will NOT be recorded.",
            level="warning",
            event_type="order",
            detail={"symbol": symbol, "strategy": strategy_type,
                    "order_id": order_id, "status": "not_filled"},
        )
        log_rejected_trade(
            symbol, strategy_type, signal,
            f"Order {order_id} not filled (cancelled/rejected/timeout)",
            portfolio_equity, buying_power,
            intraday_df=signal.get("_intraday_df"),
            daily_df=signal.get("_daily_df"),
            sentiment_data=sentiment_data,
        )
        return None

    # ── Step 3: Use actual fill data ──────────────────────────────────
    actual_fill_price = fill_details["filled_avg_price"] or entry_price
    actual_qty = fill_details["filled_qty"] or shares

    # Recalculate stops using the ACTUAL fill price (not the signal price)
    stop_loss = calculate_stop_loss(actual_fill_price, atr, strategy_type)
    take_profit = calculate_take_profit(actual_fill_price, atr, strategy_type)

    # ── Step 4: Record confirmed trade in database ────────────────────
    with get_db() as session:
        trade = Trade(
            symbol=symbol,
            strategy_type=strategy_type,
            side="buy",
            quantity=actual_qty,
            entry_price=actual_fill_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            status="open",
            alpaca_order_id=order_id,
            entry_time=datetime.utcnow(),
            notes=signal.get("reason", ""),
        )
        session.add(trade)
        session.flush()
        trade_id = trade.id

    log_heartbeat(
        f"FILLED: BUY {actual_qty} {symbol} @ ${actual_fill_price:.2f} ({strategy_type}) "
        f"SL=${stop_loss:.2f} TP={take_profit} — Order: {order_id}",
        level="info",
        event_type="order",
        detail={"symbol": symbol, "strategy": strategy_type,
                "shares": actual_qty, "fill_price": actual_fill_price,
                "signal_price": entry_price,
                "slippage": round(actual_fill_price - entry_price, 4),
                "stop_loss": stop_loss, "take_profit": take_profit,
                "order_id": order_id, "trade_id": trade_id,
                "status": "filled"},
    )

    # Log full trade decision context to JSON
    log_trade_entry(
        trade_id=trade_id,
        symbol=symbol,
        strategy_type=strategy_type,
        signal=signal,
        shares=actual_qty,
        entry_price=actual_fill_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        atr=atr,
        portfolio_equity=portfolio_equity,
        buying_power=buying_power,
        intraday_df=signal.get("_intraday_df"),
        daily_df=signal.get("_daily_df"),
        sentiment_data=sentiment_data,
        market_context=market_context,
    )

    return trade_id


def execute_exit(trade_id: int, exit_price: float, reason: str = "signal",
                 status: str = "closed",
                 intraday_df=None, daily_df=None) -> bool:
    """
    Execute a trade exit.

    Args:
        trade_id: Database trade ID
        exit_price: Price at which to exit
        reason: Exit reason for logging
        status: New status ('closed', 'stopped_out', 'eod_liquidated')
        intraday_df: Optional intraday DataFrame for exit logging
        daily_df: Optional daily DataFrame for exit logging

    Returns:
        True on success, False on failure
    """
    with get_db() as session:
        trade = session.query(Trade).filter(Trade.id == trade_id).first()
        if not trade:
            logger.error(f"Trade {trade_id} not found")
            return False

        if trade.status != "open":
            logger.warning(f"Trade {trade_id} is already {trade.status}")
            return False

        symbol = trade.symbol
        trade_strategy = trade.strategy_type
        trade_entry_price = trade.entry_price
        trade_quantity = trade.quantity
        trade_entry_time = trade.entry_time

        if not config.ENABLE_TRADING:
            logger.info(
                f"MONITOR MODE — exit not placed: SELL {trade.quantity} {symbol} "
                f"@ ~${exit_price:.2f} ({reason})"
            )
            return False

        # Verify we actually hold this position on Alpaca before selling.
        # Without this check, we'd try to sell shares we don't own (e.g. if the
        # entry order never filled, or the position was already closed), which
        # Alpaca rejects with "asset cannot be sold short".
        positions = alpaca_client.get_positions()
        held_qty = 0
        for pos in positions:
            if pos["symbol"] == symbol:
                held_qty = pos["qty"]
                break

        if held_qty <= 0:
            logger.warning(
                f"[{symbol}] No Alpaca position found — cannot sell. "
                f"Entry order may not have filled. Marking trade as closed with P&L=0."
            )
            log_heartbeat(
                f"No position for {symbol} on Alpaca — marking trade closed (entry likely unfilled)",
                level="warning"
            )
            # Mark trade as closed in DB without submitting a sell order
            trade.exit_price = trade.entry_price  # No actual fill, so P&L = 0
            trade.pnl = 0.0
            trade.status = "closed"
            trade.exit_time = datetime.utcnow()
            trade.notes = (trade.notes or "") + f" | Exit: {reason} (no Alpaca position — entry unfilled)"

            # Still log exit for audit trail
            log_trade_exit(
                trade_id=trade_id,
                symbol=symbol,
                strategy_type=trade_strategy,
                exit_price=trade.entry_price,
                exit_reason=f"{reason} (no Alpaca position)",
                exit_status="closed",
                pnl=0.0,
                entry_price=trade_entry_price,
                shares=trade_quantity,
                entry_time=trade_entry_time,
                intraday_df=intraday_df,
                daily_df=daily_df,
            )
            return True

        # Cap sell quantity to what we actually hold (safety)
        sell_qty = min(trade_quantity, held_qty)
        if sell_qty < trade_quantity:
            logger.warning(
                f"[{symbol}] Position mismatch: DB says {trade_quantity} shares, "
                f"Alpaca has {held_qty}. Selling {sell_qty}."
            )

        # Submit sell order
        order_id = alpaca_client.submit_market_order(symbol, sell_qty, "sell")
        if not order_id:
            log_heartbeat(f"Failed to submit sell order for {symbol}", level="error")
            return False

        # Calculate P&L
        side_multiplier = 1 if trade.side == "buy" else -1
        pnl = (exit_price - trade.entry_price) * trade.quantity * side_multiplier

        # Update trade record
        trade.exit_price = exit_price
        trade.pnl = round(pnl, 2)
        trade.status = status
        trade.exit_time = datetime.utcnow()
        trade.notes = (trade.notes or "") + f" | Exit: {reason}"

    log_heartbeat(
        f"SELL {trade_quantity} {symbol} @ ~${exit_price:.2f} ({status}) "
        f"P&L: ${pnl:.2f} — {reason}",
        level="info",
        event_type="order",
        detail={"symbol": symbol, "strategy": trade_strategy, "side": "sell",
                "shares": trade_quantity, "exit_price": exit_price,
                "entry_price": trade_entry_price, "pnl": round(pnl, 2),
                "status": status, "reason": reason},
    )

    # Log exit to trade decision log
    log_trade_exit(
        trade_id=trade_id,
        symbol=symbol,
        strategy_type=trade_strategy,
        exit_price=exit_price,
        exit_reason=reason,
        exit_status=status,
        pnl=pnl,
        entry_price=trade_entry_price,
        shares=trade_quantity,
        entry_time=trade_entry_time,
        intraday_df=intraday_df,
        daily_df=daily_df,
    )

    return True


def close_trade_by_id(trade_id: int) -> Dict[str, Any]:
    """
    Close a single trade by its database ID. Used by dashboard manual close.
    Handles three scenarios:
      1. Order still pending/queued (market closed) → cancel the order
      2. Position filled → submit a sell order
      3. No price data → try closing via Alpaca position API

    Returns:
        Dict with success status and message
    """
    with get_db() as session:
        trade = session.query(Trade).filter(Trade.id == trade_id).first()
        if not trade:
            return {"success": False, "message": f"Trade {trade_id} not found"}
        if trade.status != "open":
            return {"success": False, "message": f"Trade {trade_id} is already {trade.status}"}
        symbol = trade.symbol
        alpaca_order_id = trade.alpaca_order_id
        entry_price = trade.entry_price

    # --- Scenario 1: Check if the Alpaca order is still pending/queued ---
    if alpaca_order_id:
        order_status = alpaca_client.get_order_status(alpaca_order_id)
        if order_status and order_status in ("new", "accepted", "pending_new", "partially_filled"):
            # Order hasn't fully filled yet — cancel it instead of trying to sell
            cancelled = alpaca_client.cancel_order(alpaca_order_id)
            if cancelled:
                with get_db() as session:
                    trade = session.query(Trade).filter(Trade.id == trade_id).first()
                    trade.status = "closed"
                    trade.exit_time = datetime.utcnow()
                    trade.exit_price = entry_price  # No fill, so P&L = 0
                    trade.pnl = 0.0
                    trade.notes = (trade.notes or "") + f" | Cancelled pending order ({order_status})"
                log_heartbeat(
                    f"Cancelled pending order for {symbol} (was {order_status})",
                    level="info"
                )
                return {"success": True, "message": f"Cancelled pending order for {symbol}"}
            else:
                # Cancel failed — maybe it filled in the meantime, fall through to try sell
                logger.warning(f"Could not cancel order {alpaca_order_id} for {symbol}, will try sell")

    # --- Scenario 2: Order filled, we have a position — get price and sell ---
    price = alpaca_client.get_latest_price(symbol)
    if not price:
        # Try daily bars as fallback for price
        df = alpaca_client.get_bars(symbol, "1Day", limit=2)
        if df is not None and not df.empty:
            price = float(df.iloc[-1]["close"])

    if not price:
        # --- Scenario 3: No price data — try closing via Alpaca position API ---
        if config.ENABLE_TRADING:
            closed = alpaca_client.close_position(symbol)
            if closed:
                with get_db() as session:
                    trade = session.query(Trade).filter(Trade.id == trade_id).first()
                    trade.status = "closed"
                    trade.exit_time = datetime.utcnow()
                    trade.notes = (trade.notes or "") + " | Manual close (no price data)"
                return {"success": True, "message": f"Closed {symbol} via Alpaca"}
        return {"success": False, "message": f"Could not get price for {symbol}"}

    success = execute_exit(trade_id, price, reason="manual_close", status="closed")
    return {
        "success": success,
        "message": f"Closed {symbol}" if success else f"Failed to close {symbol}"
    }


def close_all_by_strategy(strategy_type: str) -> Dict[str, Any]:
    """
    Close all open positions for a given strategy type.

    Returns:
        Dict with closed_count and any errors
    """
    closed_count = 0
    errors = []

    with get_db() as session:
        open_trades = session.query(Trade).filter(
            Trade.strategy_type == strategy_type,
            Trade.status == "open"
        ).all()
        trade_ids = [(t.id, t.symbol) for t in open_trades]

    for trade_id, symbol in trade_ids:
        result = close_trade_by_id(trade_id)
        if result["success"]:
            closed_count += 1
        else:
            errors.append(f"{symbol}: {result['message']}")

    status = "eod_liquidated" if strategy_type == "day" else "closed"
    log_heartbeat(
        f"Closed {closed_count}/{len(trade_ids)} {strategy_type} positions. "
        f"Errors: {len(errors)}",
        level="info" if not errors else "warning"
    )

    return {"closed_count": closed_count, "errors": errors}

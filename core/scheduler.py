"""
All 7 scheduled job functions for the trading bot.

Jobs:
    1. pre_market_setup   — 9:00 AM ET, refresh daily bars + swing signals
    2. day_trade_scan     — Every 15 min, 9:35 AM - 3:45 PM ET (configurable via DAY_SCAN_INTERVAL_MINUTES)
    3. swing_trade_scan   — 9:35 AM + 1:00 PM ET
    4. stop_loss_monitor  — Every 1 min, 9:30 AM - 4:00 PM ET
    5. eod_liquidation    — 3:50 PM ET, close all day trades
    6. post_market_report — 4:05 PM ET, daily P&L summary
    7. health_check       — Every 30 seconds (always)

All trade jobs check market_clock.is_open before executing.
Exceptions are caught, logged, and retried once.
"""

import logging
import traceback
import time
from datetime import datetime, date
from typing import Optional

from core import alpaca_client
from core.data_ingestion import (
    get_daily_data, get_intraday_data, clear_cache, fetch_bars, compute_indicators,
    get_intraday_data_batch, get_daily_data_batch
)
from core.signals.day_trade import generate_signals_batch as day_signals_batch
from core.signals.swing_trade import generate_signals_batch as swing_signals_batch
from core.order_executor import execute_entry, execute_exit, close_all_by_strategy, close_trade_by_id
from core.risk_manager import update_trailing_stop
from core.sentiment import adjust_signal_with_sentiment, get_sentiment
from core.alerts import (
    alert_trade_entry, alert_stop_loss, alert_daily_report, alert_error
)
from db.database import get_db, log_heartbeat
from db.models import Trade, EquityHistory
import config

# Track scan cycle count for market context logging
_day_scan_cycle = 0
_swing_scan_cycle = 0

logger = logging.getLogger(__name__)

# Track consecutive health check failures
_health_check_failures = 0
_last_health_check_success = None


def _check_market_open() -> bool:
    """Check if the market is currently open. Returns False if we can't determine."""
    try:
        clock = alpaca_client.get_market_clock()
        if clock and clock.get("is_open"):
            return True
        logger.debug("Market is closed — skipping trade job.")
        return False
    except Exception as e:
        logger.error(f"Failed to check market clock: {e}")
        return False


def _run_with_retry(func_name: str, func, *args, **kwargs):
    """Run a function with one retry on failure."""
    try:
        return func(*args, **kwargs)
    except Exception as e:
        logger.error(f"[{func_name}] First attempt failed: {e}\n{traceback.format_exc()}")
        log_heartbeat(f"{func_name} failed (attempt 1): {str(e)[:200]}", level="error")

        # Retry once after 10 seconds
        time.sleep(10)
        try:
            return func(*args, **kwargs)
        except Exception as e2:
            logger.error(f"[{func_name}] Retry failed: {e2}\n{traceback.format_exc()}")
            log_heartbeat(f"{func_name} retry failed: {str(e2)[:200]}", level="error")
            return None


def _build_day_trade_universe():
    """
    Dynamically build the day trade watchlist from multiple sources:
      1. All Alpaca tradeable assets (filtered by price/volume, ranked by avg volume)
      2. Top 100 movers (biggest % change) from Alpaca screener API
      3. Top 100 most active (by volume) from Alpaca screener API

    The final universe is the union of all three sources (de-duplicated),
    with fallback symbols always included if they pass filters.

    Updates config.DAY_TRADE_WATCHLIST in place.
    """
    log_heartbeat("Building dynamic day trade universe...", level="info")

    try:
        # ── Source 1: Tradeable assets ranked by avg volume ──────────────
        assets = alpaca_client.get_tradeable_assets(
            exchange_filter=config.DAY_UNIVERSE_EXCHANGES
        )
        if not assets:
            log_heartbeat("Dynamic universe: no assets returned, keeping fallback watchlist", level="warning")
            return

        symbols = [a["symbol"] for a in assets]
        logger.info(f"Dynamic universe: {len(symbols)} tradeable assets from Alpaca")

        # Fetch daily bars in batches of 2000 to avoid slow/unreliable API calls
        BAR_BATCH_SIZE = 2000
        bar_data = {}
        for batch_start in range(0, len(symbols), BAR_BATCH_SIZE):
            batch = symbols[batch_start:batch_start + BAR_BATCH_SIZE]
            batch_num = batch_start // BAR_BATCH_SIZE + 1
            total_batches = (len(symbols) + BAR_BATCH_SIZE - 1) // BAR_BATCH_SIZE
            logger.info(f"Fetching daily bars batch {batch_num}/{total_batches} ({len(batch)} symbols)")
            batch_data = alpaca_client.get_multi_symbol_daily_bars(batch, limit=20)
            bar_data.update(batch_data)

        # Filter by price and volume thresholds
        qualified = []
        for symbol, data in bar_data.items():
            latest_close = data["close"]
            avg_volume = data["avg_volume"]

            if latest_close < config.DAY_UNIVERSE_MIN_PRICE:
                continue
            if latest_close > config.DAY_UNIVERSE_MAX_PRICE:
                continue
            if avg_volume < config.DAY_UNIVERSE_MIN_AVG_VOLUME:
                continue

            qualified.append({
                "symbol": symbol,
                "price": latest_close,
                "avg_volume": avg_volume,
            })

        if not qualified:
            log_heartbeat("Dynamic universe: no symbols passed filters, keeping fallback", level="warning")
            return

        # Rank by average volume (most liquid first) and take top N
        qualified.sort(key=lambda x: x["avg_volume"], reverse=True)
        top_n = qualified[:config.DAY_UNIVERSE_MAX_SYMBOLS]
        volume_symbols = [q["symbol"] for q in top_n]

        # ── Source 2: Top movers (biggest % change) ─────────────────────
        movers_symbols = []
        movers_count = 0
        if config.DAY_UNIVERSE_INCLUDE_MOVERS:
            try:
                movers = alpaca_client.get_top_movers(
                    top=config.DAY_UNIVERSE_TOP_MOVERS
                )
                for m in movers:
                    sym = m["symbol"]
                    price = m.get("price", 0)
                    # Apply price filters (skip filter if API didn't return price)
                    if price > 0:
                        if price < config.DAY_UNIVERSE_MIN_PRICE:
                            continue
                        if price > config.DAY_UNIVERSE_MAX_PRICE:
                            continue
                    movers_symbols.append(sym)
                movers_count = len(movers_symbols)
                logger.info(f"Top movers: {movers_count} symbols passed price filters "
                            f"(from {len(movers)} fetched)")
            except Exception as e:
                logger.warning(f"Failed to fetch top movers (non-fatal): {e}")

        # ── Source 3: Most active (by volume) ───────────────────────────
        actives_symbols = []
        actives_count = 0
        if config.DAY_UNIVERSE_INCLUDE_MOST_ACTIVE:
            try:
                actives = alpaca_client.get_most_actives(
                    top=config.DAY_UNIVERSE_TOP_MOST_ACTIVE
                )
                for a in actives:
                    sym = a["symbol"]
                    price = a.get("price", 0)
                    # Apply price filters (skip filter if API didn't return price)
                    if price > 0:
                        if price < config.DAY_UNIVERSE_MIN_PRICE:
                            continue
                        if price > config.DAY_UNIVERSE_MAX_PRICE:
                            continue
                    actives_symbols.append(sym)
                actives_count = len(actives_symbols)
                logger.info(f"Most active: {actives_count} symbols passed price filters "
                            f"(from {len(actives)} fetched)")
            except Exception as e:
                logger.warning(f"Failed to fetch most actives (non-fatal): {e}")

        # ── Merge all sources (de-duplicate, preserve priority order) ───
        seen = set()
        new_watchlist = []

        # Priority 1: volume-ranked symbols from bar data
        for sym in volume_symbols:
            if sym not in seen:
                seen.add(sym)
                new_watchlist.append(sym)

        # Priority 2: top movers
        for sym in movers_symbols:
            if sym not in seen:
                seen.add(sym)
                new_watchlist.append(sym)

        # Priority 3: most active
        for sym in actives_symbols:
            if sym not in seen:
                seen.add(sym)
                new_watchlist.append(sym)

        # Always include fallback symbols if they passed filters
        qualified_symbols = {q["symbol"] for q in qualified}
        all_screener_symbols = set(movers_symbols + actives_symbols)
        for fb in config.DAY_TRADE_WATCHLIST_FALLBACK:
            if fb not in seen and (fb in qualified_symbols or fb in all_screener_symbols):
                new_watchlist.append(fb)

        # Update the live config
        config.DAY_TRADE_WATCHLIST.clear()
        config.DAY_TRADE_WATCHLIST.extend(new_watchlist)

        top_5 = ", ".join(new_watchlist[:5])
        log_heartbeat(
            f"Dynamic universe built: {len(new_watchlist)} symbols "
            f"(volume: {len(volume_symbols)}, movers: {movers_count}, "
            f"actives: {actives_count}, "
            f"from {len(qualified)} qualified out of {len(symbols)} total). "
            f"Top by volume: {top_5}...",
            level="info"
        )

    except Exception as e:
        logger.error(f"Failed to build dynamic universe: {e}")
        log_heartbeat(f"Dynamic universe failed: {str(e)[:200]}, using fallback", level="error")


def pre_market_setup():
    """
    9:00 AM ET (weekdays): Build dynamic day trade universe,
    refresh daily bars, recalculate swing signals.
    """
    if not _check_market_open() and not _is_pre_market():
        log_heartbeat("Pre-market setup: Market closed — skipping", level="info")
        return

    log_heartbeat("Pre-market setup starting...", level="info")

    def _do_setup():
        clear_cache()

        # Build dynamic day trade universe from all Alpaca assets
        _build_day_trade_universe()

        # Fetch daily data for all symbols (day + swing combined)
        all_symbols = list(set(config.DAY_TRADE_WATCHLIST + config.SWING_TRADE_WATCHLIST))
        daily_cache = {}

        for symbol in all_symbols:
            df = get_daily_data(symbol, limit=250)
            if df is not None:
                daily_cache[symbol] = df
                logger.info(f"Loaded {len(df)} daily bars for {symbol}")

        # Generate swing signals for the day
        swing_signals = swing_signals_batch(config.SWING_TRADE_WATCHLIST, daily_cache)
        buy_signals = [s for s in swing_signals if s["signal"] == "buy"]

        log_heartbeat(
            f"Pre-market setup complete: {len(config.DAY_TRADE_WATCHLIST)} day trade symbols, "
            f"{len(daily_cache)} symbols loaded, "
            f"{len(buy_signals)} swing buy signals identified",
            level="info"
        )

    _run_with_retry("pre_market_setup", _do_setup)


def _is_pre_market() -> bool:
    """Check if we're in pre-market hours (allow setup to run)."""
    try:
        from datetime import timezone, timedelta
        et = timezone(timedelta(hours=-5))
        now = datetime.now(et)
        return 8 <= now.hour <= 9
    except Exception:
        return True  # Default to allowing


def day_trade_scan():
    """
    Every 15 min (configurable), 9:35 AM - 3:45 PM ET: Fetch 5-min bars,
    run day trade signal engine, execute entries/exits.
    """
    if not _check_market_open():
        return

    log_heartbeat("Day trade scan starting...", level="info")

    def _do_scan():
        global _day_scan_cycle
        _day_scan_cycle += 1
        clear_cache()

        # Fetch intraday and daily data in batches for the full watchlist
        watchlist = config.DAY_TRADE_WATCHLIST
        logger.info(f"Fetching data for {len(watchlist)} symbols in batches...")
        intraday_cache = get_intraday_data_batch(watchlist, limit=100, batch_size=100)
        daily_cache = get_daily_data_batch(watchlist, limit=60, batch_size=100)

        # Generate signals
        signals = day_signals_batch(config.DAY_TRADE_WATCHLIST, intraday_cache, daily_cache)

        # Apply sentiment filter (Phase 2)
        if config.ENABLE_SENTIMENT:
            signals = [adjust_signal_with_sentiment(s) for s in signals]

        # Get account info for position sizing
        account = alpaca_client.get_account()
        if not account:
            log_heartbeat("Day scan: Could not get account info", level="error")
            return

        equity = account["equity"]
        buying_power = account["buying_power"]

        # Build market context for trade logging
        market_context = {
            "scan_type": "day_trade",
            "scan_cycle": _day_scan_cycle,
            "watchlist_size": len(config.DAY_TRADE_WATCHLIST),
            "symbols_with_data": len(intraday_cache),
            "total_signals_generated": len(signals),
            "buy_signals_count": len([s for s in signals if s["signal"] == "buy"]),
            "sell_signals_count": len([s for s in signals if s["signal"] == "sell"]),
            "hold_signals_count": len([s for s in signals if s["signal"] == "hold"]),
            "scan_timestamp_utc": datetime.utcnow().isoformat(),
        }

        # Execute buy signals
        buys = [s for s in signals if s["signal"] == "buy"]
        for signal in buys:
            # Gather sentiment data for logging
            sentiment_data = None
            if config.ENABLE_SENTIMENT:
                sentiment_data = get_sentiment(signal["symbol"])

            trade_id = execute_entry(
                signal["symbol"], "day", signal, equity, buying_power,
                sentiment_data=sentiment_data,
                market_context=market_context,
            )
            if trade_id:
                alert_trade_entry(
                    signal["symbol"], "day", signal.get("qty", 0),
                    signal.get("entry_price", 0), signal.get("stop_loss", 0),
                )

        log_heartbeat(
            f"Day scan complete: {len(signals)} symbols scanned, "
            f"{len(buys)} buy signals",
            level="info"
        )

    _run_with_retry("day_trade_scan", _do_scan)


def swing_trade_scan():
    """
    9:35 AM + 1:00 PM ET: Analyze daily candles for swing watchlist,
    check entry/exit conditions, update trailing stops.
    """
    if not _check_market_open():
        log_heartbeat("Swing scan: Market closed — skipping", level="info")
        return

    log_heartbeat("Swing trade scan starting...", level="info")

    def _do_scan():
        global _swing_scan_cycle
        _swing_scan_cycle += 1
        daily_cache = {}
        for symbol in config.SWING_TRADE_WATCHLIST:
            df = get_daily_data(symbol, limit=250)
            if df is not None:
                daily_cache[symbol] = df

        signals = swing_signals_batch(config.SWING_TRADE_WATCHLIST, daily_cache)

        # Apply sentiment filter (Phase 2)
        if config.ENABLE_SENTIMENT:
            signals = [adjust_signal_with_sentiment(s) for s in signals]

        account = alpaca_client.get_account()
        if not account:
            log_heartbeat("Swing scan: Could not get account info", level="error")
            return

        equity = account["equity"]
        buying_power = account["buying_power"]

        # Build market context for trade logging
        market_context = {
            "scan_type": "swing_trade",
            "scan_cycle": _swing_scan_cycle,
            "watchlist_size": len(config.SWING_TRADE_WATCHLIST),
            "symbols_with_data": len(daily_cache),
            "total_signals_generated": len(signals),
            "buy_signals_count": len([s for s in signals if s["signal"] == "buy"]),
            "sell_signals_count": len([s for s in signals if s["signal"] == "sell"]),
            "scan_timestamp_utc": datetime.utcnow().isoformat(),
        }

        # Execute buy signals
        buys = [s for s in signals if s["signal"] == "buy"]
        for signal in buys:
            # Gather sentiment data for logging
            sentiment_data = None
            if config.ENABLE_SENTIMENT:
                sentiment_data = get_sentiment(signal["symbol"])

            trade_id = execute_entry(
                signal["symbol"], "swing", signal, equity, buying_power,
                sentiment_data=sentiment_data,
                market_context=market_context,
            )
            if trade_id:
                alert_trade_entry(
                    signal["symbol"], "swing", signal.get("qty", 0),
                    signal.get("entry_price", 0), signal.get("stop_loss", 0),
                )

        # Update trailing stops for open swing positions
        with get_db() as session:
            open_swings = session.query(Trade).filter(
                Trade.strategy_type == "swing",
                Trade.status == "open"
            ).all()

            for trade in open_swings:
                if trade.symbol in daily_cache:
                    df = daily_cache[trade.symbol]
                    if "atr" in df.columns and not df.empty:
                        atr = float(df.iloc[-1]["atr"])
                        current_price = float(df.iloc[-1]["close"])
                        new_stop = update_trailing_stop(trade, current_price, atr)
                        if new_stop:
                            trade.stop_loss = new_stop

        # Check for sell signals on open positions
        sells = [s for s in signals if s["signal"] == "sell"]
        for signal in sells:
            with get_db() as session:
                open_trade = session.query(Trade).filter(
                    Trade.symbol == signal["symbol"],
                    Trade.strategy_type == "swing",
                    Trade.status == "open"
                ).first()
                if open_trade:
                    execute_exit(
                        open_trade.id,
                        signal["entry_price"],
                        reason=signal["reason"],
                        status="closed",
                        daily_df=daily_cache.get(signal["symbol"]),
                    )

        log_heartbeat(
            f"Swing scan complete: {len(signals)} symbols, "
            f"{len(buys)} buys, {len(sells)} sells, "
            f"{len(open_swings) if 'open_swings' in dir() else 0} trailing stops checked",
            level="info"
        )

    _run_with_retry("swing_trade_scan", _do_scan)


def stop_loss_monitor():
    """
    Every 1 min, 9:30 AM - 4:00 PM ET: Check all open positions
    against stop-loss and take-profit levels.
    """
    if not _check_market_open():
        return

    def _do_monitor():
        with get_db() as session:
            open_trades = session.query(Trade).filter(Trade.status == "open").all()
            trade_data = [
                (t.id, t.symbol, t.stop_loss, t.take_profit, t.side, t.strategy_type)
                for t in open_trades
            ]

        if not trade_data:
            return

        for trade_id, symbol, stop_loss, take_profit, side, strategy_type in trade_data:
            price = alpaca_client.get_latest_price(symbol)
            if not price:
                continue

            # Fetch current indicator data for exit logging context
            exit_intraday_df = None
            exit_daily_df = None
            try:
                if strategy_type == "day":
                    exit_intraday_df = get_intraday_data(symbol, limit=20)
                exit_daily_df = get_daily_data(symbol, limit=10)
            except Exception:
                pass  # Non-critical: logging without indicators is still valuable

            # Check stop-loss
            if side == "buy" and price <= stop_loss:
                log_heartbeat(
                    f"STOP-LOSS triggered: {symbol} @ ${price:.2f} <= ${stop_loss:.2f} ({strategy_type})",
                    level="warning"
                )
                execute_exit(
                    trade_id, price,
                    reason=f"Stop-loss @ ${stop_loss:.2f}",
                    status="stopped_out",
                    intraday_df=exit_intraday_df,
                    daily_df=exit_daily_df,
                )
                alert_stop_loss(symbol, strategy_type, price, stop_loss)
                continue

            # Check take-profit (day trades only)
            if take_profit and side == "buy" and price >= take_profit:
                log_heartbeat(
                    f"TAKE-PROFIT triggered: {symbol} @ ${price:.2f} >= ${take_profit:.2f} ({strategy_type})",
                    level="info"
                )
                execute_exit(
                    trade_id, price,
                    reason=f"Take-profit @ ${take_profit:.2f}",
                    status="closed",
                    intraday_df=exit_intraday_df,
                    daily_df=exit_daily_df,
                )

    _run_with_retry("stop_loss_monitor", _do_monitor)


def eod_liquidation():
    """
    3:50 PM ET: Close day trade positions that have exceeded DAY_MAX_HOLD_DAYS.

    AGGRESSIVE VARIANT: Instead of liquidating ALL day trades at EOD, we now
    allow holding for up to DAY_MAX_HOLD_DAYS (default 3 days). This gives
    the take-profit and stop-loss levels time to trigger on trend-following
    momentum, rather than force-closing profitable setups at 3:50 PM.

    Positions that have NOT exceeded the hold limit stay open overnight.
    """
    log_heartbeat("EOD check starting — closing expired day trade positions", level="info")

    def _do_liquidation():
        closed_count = 0
        kept_count = 0
        errors = []

        with get_db() as session:
            open_day_trades = session.query(Trade).filter(
                Trade.strategy_type == "day",
                Trade.status == "open"
            ).all()
            trade_data = [
                (t.id, t.symbol, t.entry_time) for t in open_day_trades
            ]

        for trade_id, symbol, entry_time in trade_data:
            # Calculate how many trading days this position has been held
            if entry_time:
                hold_days = (datetime.utcnow() - entry_time).days
            else:
                hold_days = config.DAY_MAX_HOLD_DAYS  # Force close if no entry time

            if hold_days >= config.DAY_MAX_HOLD_DAYS:
                # Exceeded hold limit — close the position
                result = close_trade_by_id(trade_id)
                if result["success"]:
                    closed_count += 1
                else:
                    errors.append(f"{symbol}: {result['message']}")

                # Mark as eod_liquidated if still open
                with get_db() as session:
                    trade = session.query(Trade).filter(Trade.id == trade_id).first()
                    if trade and trade.status == "open":
                        trade.status = "eod_liquidated"
                        trade.exit_time = datetime.utcnow()
                        trade.notes = (trade.notes or "") + f" | EOD liquidation after {hold_days}d hold"
                        closed_count += 1
            else:
                kept_count += 1

        log_heartbeat(
            f"EOD check complete: {closed_count} expired day trades closed, "
            f"{kept_count} kept open (under {config.DAY_MAX_HOLD_DAYS}d limit). "
            f"Errors: {len(errors)}",
            level="info" if not errors else "warning"
        )

    _run_with_retry("eod_liquidation", _do_liquidation)


def post_market_report():
    """
    4:05 PM ET: Calculate daily P&L, record equity snapshot.
    """
    log_heartbeat("Post-market report generating...", level="info")

    def _do_report():
        account = alpaca_client.get_account()
        if not account:
            log_heartbeat("Post-market report: Could not get account", level="error")
            return

        today = date.today()

        # Calculate P&L by strategy
        with get_db() as session:
            day_trades = session.query(Trade).filter(
                Trade.strategy_type == "day",
                Trade.exit_time != None,
            ).all()
            day_pnl = sum(t.pnl or 0 for t in day_trades
                          if t.exit_time and t.exit_time.date() == today)

            swing_trades = session.query(Trade).filter(
                Trade.strategy_type == "swing",
                Trade.exit_time != None,
            ).all()
            swing_pnl = sum(t.pnl or 0 for t in swing_trades
                            if t.exit_time and t.exit_time.date() == today)

            open_day = session.query(Trade).filter(
                Trade.strategy_type == "day", Trade.status == "open"
            ).count()
            open_swing = session.query(Trade).filter(
                Trade.strategy_type == "swing", Trade.status == "open"
            ).count()

            # Upsert equity history
            existing = session.query(EquityHistory).filter(
                EquityHistory.date == today
            ).first()

            if existing:
                existing.equity = account["equity"]
                existing.cash = account["cash"]
                existing.day_pnl = day_pnl
                existing.swing_pnl = swing_pnl
                existing.open_day_positions = open_day
                existing.open_swing_positions = open_swing
            else:
                snapshot = EquityHistory(
                    date=today,
                    equity=account["equity"],
                    cash=account["cash"],
                    day_pnl=day_pnl,
                    swing_pnl=swing_pnl,
                    open_day_positions=open_day,
                    open_swing_positions=open_swing,
                )
                session.add(snapshot)

        log_heartbeat(
            f"Daily report: Equity=${account['equity']:.2f}, "
            f"Day P&L=${day_pnl:.2f}, Swing P&L=${swing_pnl:.2f}, "
            f"Open: {open_day} day / {open_swing} swing",
            level="info"
        )
        alert_daily_report(account['equity'], day_pnl, swing_pnl, open_day, open_swing)

    _run_with_retry("post_market_report", _do_report)


def health_check():
    """
    Every 30 seconds: Ping Alpaca API, log heartbeat, update dashboard status.
    Runs always, even outside market hours.
    """
    global _health_check_failures, _last_health_check_success

    try:
        account = alpaca_client.get_account()
        if account:
            _health_check_failures = 0
            _last_health_check_success = datetime.utcnow()

            clock = alpaca_client.get_market_clock()
            market_status = "OPEN" if (clock and clock.get("is_open")) else "CLOSED"

            log_heartbeat(
                f"Health check OK — Equity: ${account['equity']:.2f}, "
                f"Market: {market_status}",
                level="info"
            )
        else:
            _health_check_failures += 1
            log_heartbeat(
                f"Health check FAILED — could not reach Alpaca API "
                f"(consecutive failures: {_health_check_failures})",
                level="error"
            )

            if _health_check_failures >= 5:
                log_heartbeat(
                    "CRITICAL: Alpaca API unreachable for 5+ consecutive checks!",
                    level="error"
                )
                alert_error(
                    "API Connection Critical",
                    f"Alpaca API unreachable for {_health_check_failures} consecutive checks"
                )
    except Exception as e:
        _health_check_failures += 1
        log_heartbeat(f"Health check exception: {str(e)[:200]}", level="error")
        logger.error(f"Health check failed: {e}\n{traceback.format_exc()}")


def get_bot_health_status() -> dict:
    """Get current bot health status for the dashboard."""
    return {
        "consecutive_failures": _health_check_failures,
        "last_success": _last_health_check_success.isoformat() if _last_health_check_success else None,
        "status": "error" if _health_check_failures >= 5 else (
            "warning" if _health_check_failures > 0 else "ok"
        ),
    }

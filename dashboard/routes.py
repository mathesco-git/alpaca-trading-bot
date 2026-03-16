"""
FastAPI routes for the trading bot dashboard.
Serves the HTML dashboard and all JSON API endpoints.
"""

import logging
import asyncio
from datetime import datetime, date
from typing import Optional

from fastapi import APIRouter, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

import pandas as pd
from core import alpaca_client
from core.order_executor import close_trade_by_id, close_all_by_strategy
from core.scheduler import get_bot_health_status
from db.database import get_db, log_heartbeat
from db.models import Trade, HeartbeatLog, EquityHistory
import config

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="dashboard/templates")

# Reference to the scheduler (set from main.py)
_scheduler = None
_scheduler_paused = False
_bot_start_time = None


def set_scheduler(scheduler):
    """Set the scheduler reference for pause/resume controls."""
    global _scheduler, _bot_start_time
    _scheduler = scheduler
    _bot_start_time = datetime.utcnow()


# ─────────────────────────────────────────────
# HTML Dashboard
# ─────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Render the main dashboard HTML page."""
    return templates.TemplateResponse("index.html", {
        "request": request,
        "poll_interval": config.DASHBOARD_POLL_INTERVAL_MS,
        "chart_refresh": config.CHART_REFRESH_INTERVAL_MS,
    })


# ─────────────────────────────────────────────
# Account & Status Endpoints
# ─────────────────────────────────────────────

@router.get("/api/account")
async def api_account():
    """Get account overview data."""
    def _fetch():
        account = alpaca_client.get_account()
        if not account:
            return None

        today = date.today()
        with get_db() as session:
            day_trades_today = session.query(Trade).filter(
                Trade.strategy_type == "day",
                Trade.exit_time != None,
            ).all()
            day_pnl = sum(t.pnl or 0 for t in day_trades_today
                           if t.exit_time and t.exit_time.date() == today)

            swing_trades_today = session.query(Trade).filter(
                Trade.strategy_type == "swing",
                Trade.exit_time != None,
            ).all()
            swing_pnl = sum(t.pnl or 0 for t in swing_trades_today
                             if t.exit_time and t.exit_time.date() == today)

        total_pnl = day_pnl + swing_pnl
        equity = account["equity"]
        last_equity = account.get("last_equity", equity)
        pnl_pct = ((equity - last_equity) / last_equity * 100) if last_equity else 0

        with get_db() as session:
            day_exposure = sum(
                t.entry_price * t.quantity
                for t in session.query(Trade).filter(
                    Trade.strategy_type == "day", Trade.status == "open"
                ).all()
            )
            swing_exposure = sum(
                t.entry_price * t.quantity
                for t in session.query(Trade).filter(
                    Trade.strategy_type == "swing", Trade.status == "open"
                ).all()
            )

        bp = account["buying_power"]
        return {
            "equity": equity,
            "cash": account["cash"],
            "buying_power": bp,
            "day_trade_bp": account.get("daytrading_buying_power", bp * config.DAY_TRADE_ALLOCATION),
            "regt_bp": account.get("regt_buying_power", bp * config.SWING_TRADE_ALLOCATION),
            "day_pnl": round(total_pnl, 2),
            "day_pnl_pct": round(pnl_pct, 2),
            "day_allocation_used": round(day_exposure, 2),
            "day_allocation_available": round(bp * config.DAY_TRADE_ALLOCATION - day_exposure, 2),
            "swing_allocation_used": round(swing_exposure, 2),
            "swing_allocation_available": round(bp * config.SWING_TRADE_ALLOCATION - swing_exposure, 2),
        }

    result = await asyncio.to_thread(_fetch)
    if result is None:
        return JSONResponse({"error": "Could not connect to Alpaca API"}, status_code=503)
    return result


@router.get("/api/bot/status")
async def api_bot_status():
    """Get bot running status."""
    global _scheduler_paused

    def _fetch_status():
        health = get_bot_health_status()
        clock = alpaca_client.get_market_clock()

        jobs_count = 0
        if _scheduler:
            try:
                jobs_count = len(_scheduler.get_jobs())
            except Exception:
                pass

        return {
            "running": _scheduler is not None and _scheduler.running and not _scheduler_paused,
            "trading_enabled": config.ENABLE_TRADING,
            "last_heartbeat": health.get("last_success"),
            "jobs_count": jobs_count,
            "market_open": clock.get("is_open", False) if clock else False,
            "health_status": health.get("status", "unknown"),
            "paused": _scheduler_paused,
            "next_open": clock.get("next_open") if clock else None,
            "next_close": clock.get("next_close") if clock else None,
        }

    return await asyncio.to_thread(_fetch_status)


@router.get("/api/bot/details")
async def api_bot_details():
    """Get detailed bot info: uptime, scheduled jobs, next fire times."""
    uptime_seconds = 0
    if _bot_start_time:
        uptime_seconds = int((datetime.utcnow() - _bot_start_time).total_seconds())

    jobs_info = []
    if _scheduler:
        try:
            for job in _scheduler.get_jobs():
                next_run = job.next_run_time
                jobs_info.append({
                    "id": job.id,
                    "next_run": next_run.isoformat() if next_run else None,
                    "next_run_human": next_run.strftime("%I:%M %p ET") if next_run else "N/A",
                })
        except Exception:
            pass

    # Sort by next_run
    jobs_info.sort(key=lambda j: j["next_run"] or "9999")

    return {
        "uptime_seconds": uptime_seconds,
        "start_time": _bot_start_time.isoformat() if _bot_start_time else None,
        "jobs": jobs_info,
        "paused": _scheduler_paused,
    }


# ─────────────────────────────────────────────
# Watchlist Endpoint
# ─────────────────────────────────────────────

@router.get("/api/watchlist")
async def api_watchlist():
    """Get the current active day trade and swing trade watchlists."""
    return JSONResponse({
        "day_trade": config.DAY_TRADE_WATCHLIST,
        "swing_trade": config.SWING_TRADE_WATCHLIST,
        "day_trade_count": len(config.DAY_TRADE_WATCHLIST),
        "swing_trade_count": len(config.SWING_TRADE_WATCHLIST),
    })


# ─────────────────────────────────────────────
# Positions Endpoints
# ─────────────────────────────────────────────

@router.get("/api/positions")
async def api_positions(strategy: Optional[str] = Query(None)):
    """Get open positions with optional strategy filter."""
    def _fetch_positions():
        with get_db() as session:
            query = session.query(Trade).filter(Trade.status == "open")
            if strategy:
                query = query.filter(Trade.strategy_type == strategy)
            trades = query.all()

            # Enrich with current prices from Alpaca
            alpaca_positions = {p["symbol"]: p for p in alpaca_client.get_positions()}

            result = []
            for t in trades:
                data = t.to_dict()
                ap = alpaca_positions.get(t.symbol, {})
                data["current_price"] = ap.get("current_price", t.entry_price)
                data["unrealized_pl"] = ap.get("unrealized_pl", 0)
                data["unrealized_plpc"] = ap.get("unrealized_plpc", 0)
                result.append(data)

        return result

    return await asyncio.to_thread(_fetch_positions)


# ─────────────────────────────────────────────
# Trade History Endpoints
# ─────────────────────────────────────────────

@router.get("/api/trades")
async def api_trades(
    strategy: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Get trade history from database."""
    with get_db() as session:
        query = session.query(Trade).order_by(Trade.entry_time.desc())
        if strategy:
            query = query.filter(Trade.strategy_type == strategy)
        trades = query.offset(offset).limit(limit).all()
        return [t.to_dict() for t in trades]


# ─────────────────────────────────────────────
# Heartbeat Log
# ─────────────────────────────────────────────

@router.get("/api/heartbeat")
async def api_heartbeat():
    """Get latest heartbeat log entries."""
    with get_db() as session:
        entries = session.query(HeartbeatLog).order_by(
            HeartbeatLog.timestamp.desc()
        ).limit(config.HEARTBEAT_LOG_MAX_DISPLAY).all()
        return [e.to_dict() for e in entries]


# ─────────────────────────────────────────────
# Chart Data Endpoints
# ─────────────────────────────────────────────

@router.get("/api/equity-history")
async def api_equity_history():
    """Get equity history for the equity curve chart."""
    with get_db() as session:
        snapshots = session.query(EquityHistory).order_by(
            EquityHistory.date.asc()
        ).all()
        return [s.to_dict() for s in snapshots]


@router.get("/api/pnl-daily")
async def api_pnl_daily():
    """Get daily P&L broken down by strategy for the bar chart."""
    with get_db() as session:
        snapshots = session.query(EquityHistory).order_by(
            EquityHistory.date.asc()
        ).all()
        return [
            {
                "date": s.date.isoformat(),
                "day_pnl": s.day_pnl or 0,
                "swing_pnl": s.swing_pnl or 0,
            }
            for s in snapshots
        ]


# ─────────────────────────────────────────────
# Manual Control Endpoints
# ─────────────────────────────────────────────

@router.post("/api/close/{trade_id}")
async def api_close_trade(trade_id: int):
    """Close a single trade by its database ID."""
    return await asyncio.to_thread(close_trade_by_id, trade_id)


@router.post("/api/close-all/{strategy_type}")
async def api_close_all(strategy_type: str):
    """Close all open positions of a given strategy type."""
    if strategy_type not in ("day", "swing"):
        return JSONResponse(
            {"error": "strategy_type must be 'day' or 'swing'"},
            status_code=400
        )
    result = await asyncio.to_thread(close_all_by_strategy, strategy_type)
    log_heartbeat(
        f"Manual close-all {strategy_type}: {result['closed_count']} closed",
        level="warning"
    )
    return result


@router.post("/api/bot/pause")
async def api_pause_bot():
    """Pause the scheduler (stop all jobs)."""
    global _scheduler_paused
    if _scheduler:
        try:
            _scheduler.pause()
            _scheduler_paused = True
            log_heartbeat("Bot PAUSED by user", level="warning")
            return {"success": True, "message": "Scheduler paused"}
        except Exception as e:
            return JSONResponse(
                {"success": False, "message": str(e)},
                status_code=500
            )
    return JSONResponse(
        {"success": False, "message": "Scheduler not available"},
        status_code=503
    )


@router.post("/api/bot/resume")
async def api_resume_bot():
    """Resume the scheduler."""
    global _scheduler_paused
    if _scheduler:
        try:
            _scheduler.resume()
            _scheduler_paused = False
            log_heartbeat("Bot RESUMED by user", level="info")
            return {"success": True, "message": "Scheduler resumed"}
        except Exception as e:
            return JSONResponse(
                {"success": False, "message": str(e)},
                status_code=500
            )
    return JSONResponse(
        {"success": False, "message": "Scheduler not available"},
        status_code=503
    )


@router.post("/api/bot/toggle-trading")
async def api_toggle_trading():
    """Toggle the ENABLE_TRADING flag."""
    config.ENABLE_TRADING = not config.ENABLE_TRADING
    mode = "ENABLED" if config.ENABLE_TRADING else "DISABLED (monitor only)"
    log_heartbeat(f"Trading {mode} by user", level="warning")
    return {"trading_enabled": config.ENABLE_TRADING}


@router.post("/api/trigger/build-universe")
async def api_trigger_build_universe():
    """Manually trigger the day trade universe builder."""
    from core.scheduler import _build_day_trade_universe
    from core.alpaca_client import _invalidate

    def _run():
        try:
            _invalidate("tradeable_assets")  # Clear stale cache before rebuild
            _invalidate("top_movers")         # Clear screener caches too
            _invalidate("most_actives")
            _build_day_trade_universe()
            count = len(config.DAY_TRADE_WATCHLIST)
            log_heartbeat(f"Manual universe build: {count} symbols loaded", level="info")
            return {"success": True, "message": f"Universe rebuilt — {count} symbols", "symbol_count": count}
        except Exception as e:
            logger.exception("Manual universe build failed")
            return {"success": False, "message": str(e)}

    result = await asyncio.to_thread(_run)
    if not result["success"]:
        return JSONResponse(result, status_code=500)
    return result


@router.post("/api/trigger/day-scan")
async def api_trigger_day_scan():
    """Manually trigger a day trade scan cycle."""
    from core.scheduler import day_trade_scan

    def _run():
        try:
            day_trade_scan()
            log_heartbeat("Manual day trade scan completed", level="info")
            return {"success": True, "message": "Day trade scan completed"}
        except Exception as e:
            logger.exception("Manual day trade scan failed")
            return {"success": False, "message": str(e)}

    result = await asyncio.to_thread(_run)
    if not result["success"]:
        return JSONResponse(result, status_code=500)
    return result


@router.post("/api/test-trade")
async def api_test_trade(request: Request):
    """
    Place a test paper trade: buy 1 share of a symbol.
    Useful for verifying the pipeline works end-to-end.
    Accepts JSON body: {"symbol": "AAPL", "strategy_type": "day"}
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    symbol = body.get("symbol", "AAPL").upper()
    strategy_type = body.get("strategy_type", "day")

    if strategy_type not in ("day", "swing"):
        return JSONResponse({"success": False, "message": "strategy_type must be 'day' or 'swing'"}, status_code=400)

    def _do_test_trade():
        # Get current price (uses cache + daily bars first for speed)
        price = alpaca_client.get_latest_price(symbol)
        if not price:
            return {"success": False, "message": f"Could not get price for {symbol}"}

        qty = 1

        if not config.ENABLE_TRADING:
            log_heartbeat(
                f"TEST TRADE (monitor mode): Would buy {qty} {symbol} @ ~${price:.2f} ({strategy_type})",
                level="info"
            )
            return {"success": True, "message": f"Monitor mode — test trade logged for {symbol} @ ~${price:.2f}", "simulated": True}

        # Submit real paper order
        order_id = alpaca_client.submit_market_order(symbol, qty, "buy")
        if not order_id:
            return {"success": False, "message": f"Failed to submit order for {symbol}"}

        # Record in DB
        stop_loss = round(price * 0.98, 2)
        take_profit = round(price * 1.04, 2)

        with get_db() as session:
            trade = Trade(
                symbol=symbol,
                strategy_type=strategy_type,
                side="buy",
                quantity=qty,
                entry_price=price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                status="open",
                alpaca_order_id=order_id,
                entry_time=datetime.utcnow(),
                notes=f"Test trade via dashboard",
            )
            session.add(trade)
            session.flush()
            trade_id = trade.id

        log_heartbeat(
            f"TEST TRADE: BUY {qty} {symbol} @ ~${price:.2f} ({strategy_type}) "
            f"SL=${stop_loss:.2f} TP=${take_profit:.2f} — Order: {order_id}",
            level="info"
        )
        return {
            "success": True,
            "message": f"Bought {qty} {symbol} @ ~${price:.2f}",
            "trade_id": trade_id,
            "order_id": order_id,
            "simulated": False,
        }

    result = await asyncio.to_thread(_do_test_trade)
    if not result.get("success") and "Could not" in result.get("message", ""):
        return JSONResponse(result, status_code=503)
    return result


# ═══════════════════════════════════════════════
# PHASE 2: Analytics, Sentiment, Charting
# ═══════════════════════════════════════════════

@router.get("/api/analytics")
async def api_analytics(
    strategy: Optional[str] = Query(None),
    lookback: int = Query(config.ANALYTICS_LOOKBACK_DAYS, ge=1, le=365),
):
    """Get performance analytics (Sharpe, drawdown, win rate, etc.)."""
    from core.analytics import calculate_analytics
    return await asyncio.to_thread(calculate_analytics, strategy=strategy, lookback_days=lookback)


@router.get("/api/analytics/monthly")
async def api_analytics_monthly():
    """Get monthly P&L summary."""
    from core.analytics import get_monthly_summary
    return await asyncio.to_thread(get_monthly_summary)


@router.get("/api/sentiment/{symbol}")
async def api_sentiment(symbol: str):
    """Get sentiment analysis for a symbol."""
    from core.sentiment import get_sentiment
    return await asyncio.to_thread(get_sentiment, symbol.upper())


@router.get("/api/sentiment")
async def api_sentiment_batch():
    """Get sentiment for all watchlist symbols."""
    from core.sentiment import get_batch_sentiment
    all_symbols = list(set(config.DAY_TRADE_WATCHLIST + config.SWING_TRADE_WATCHLIST))
    return await asyncio.to_thread(get_batch_sentiment, all_symbols)


@router.get("/api/chart/candlestick/{symbol}")
async def api_candlestick(
    symbol: str,
    timeframe: str = Query("5Min"),
    limit: int = Query(config.CANDLESTICK_DEFAULT_BARS, ge=10, le=500),
):
    """Get OHLCV candlestick data with indicator overlays for a symbol."""
    from core.data_ingestion import fetch_bars, compute_indicators

    def _build_candles():
        df = fetch_bars(symbol.upper(), timeframe=timeframe, limit=limit)
        if df is None or df.empty:
            return None

        df2 = compute_indicators(df, timeframe=timeframe)

        candles = []
        for _, row in df2.iterrows():
            candle = {
                "timestamp": str(row.get("timestamp", "")),
                "open": round(float(row["open"]), 2),
                "high": round(float(row["high"]), 2),
                "low": round(float(row["low"]), 2),
                "close": round(float(row["close"]), 2),
                "volume": int(row.get("volume", 0)),
            }
            for ind in ["vwap", "rsi", "atr", "sma_50", "sma_200", "volume_avg_20"]:
                if ind in row and not pd.isna(row[ind]):
                    candle[ind] = round(float(row[ind]), 4)
            candles.append(candle)
        return candles

    candles = await asyncio.to_thread(_build_candles)
    if candles is None:
        return JSONResponse({"error": f"No data for {symbol}"}, status_code=404)

    return {
        "symbol": symbol.upper(),
        "timeframe": timeframe,
        "count": len(candles),
        "candles": candles,
    }


@router.get("/api/alerts/config")
async def api_alerts_config():
    """Get current alert configuration."""
    return {
        "email_enabled": config.ENABLE_EMAIL_ALERTS,
        "webhook_enabled": config.ENABLE_WEBHOOK_ALERTS,
        "smtp_configured": bool(config.SMTP_USER and config.ALERT_EMAIL_TO),
        "webhook_configured": bool(config.WEBHOOK_URL),
        "alert_on_trade": config.ALERT_ON_TRADE,
        "alert_on_stop_loss": config.ALERT_ON_STOP_LOSS,
        "alert_on_error": config.ALERT_ON_ERROR,
        "alert_on_daily_report": config.ALERT_ON_DAILY_REPORT,
    }


@router.post("/api/alerts/test")
async def api_alerts_test():
    """Send a test alert to verify alert configuration."""
    from core.alerts import send_alert
    await asyncio.to_thread(
        send_alert,
        title="Test Alert",
        message="This is a test alert from your Trading Bot dashboard.",
        level="info",
    )
    return {"success": True, "message": "Test alert dispatched"}

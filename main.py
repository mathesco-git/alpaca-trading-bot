"""
Trading Bot — Single entry point.
Starts FastAPI + APScheduler + Uvicorn in one process.

Usage:
    python main.py

Dashboard:
    http://localhost:8000
"""

import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.events import EVENT_JOB_ERROR
from fastapi import FastAPI
import uvicorn

from config import (
    DASHBOARD_HOST, DASHBOARD_PORT, HEALTH_CHECK_INTERVAL_SECONDS,
    NGROK_ENABLED, NGROK_AUTHTOKEN, DAY_SCAN_INTERVAL_MINUTES,
)
from core.scheduler import (
    pre_market_setup, day_trade_scan, swing_trade_scan,
    stop_loss_monitor, eod_liquidation, post_market_report, health_check,
)
from dashboard.routes import router as dashboard_router, set_scheduler
from db.database import init_db, log_heartbeat
from utils.logger import setup_logging

# Initialize logging first
setup_logging()
logger = logging.getLogger(__name__)

# Create FastAPI app
app = FastAPI(title="Trading Bot", docs_url="/docs", redoc_url=None)
app.include_router(dashboard_router)

# Create scheduler (BackgroundScheduler runs jobs in threads — no event loop required)
scheduler = BackgroundScheduler(timezone="US/Eastern")


def job_error_listener(event):
    """Log job failures without crashing the bot."""
    logger.error(f"Job {event.job_id} failed: {event.exception}", exc_info=True)
    try:
        log_heartbeat(
            f"Job '{event.job_id}' error: {str(event.exception)[:200]}",
            level="error"
        )
    except Exception:
        pass


@app.on_event("startup")
async def startup():
    """Initialize database, configure scheduler, and start all jobs."""
    init_db()
    log_heartbeat("Bot starting up...", level="info")

    # Register error listener
    scheduler.add_listener(job_error_listener, EVENT_JOB_ERROR)

    # Add all 7 scheduled jobs
    scheduler.add_job(
        pre_market_setup, 'cron',
        hour=9, minute=0, day_of_week='mon-fri',
        id='pre_market_setup', replace_existing=True,
        misfire_grace_time=300
    )
    scheduler.add_job(
        day_trade_scan, 'cron',
        minute=f'*/{DAY_SCAN_INTERVAL_MINUTES}', hour='9-15', day_of_week='mon-fri',
        id='day_trade_scan', replace_existing=True,
        misfire_grace_time=120
    )
    scheduler.add_job(
        swing_trade_scan, 'cron',
        hour='9,13', minute=35, day_of_week='mon-fri',
        id='swing_trade_scan', replace_existing=True,
        misfire_grace_time=300
    )
    scheduler.add_job(
        stop_loss_monitor, 'cron',
        minute='*', hour='9-15', day_of_week='mon-fri',
        id='stop_loss_monitor', replace_existing=True,
        misfire_grace_time=30
    )
    scheduler.add_job(
        eod_liquidation, 'cron',
        hour=15, minute=50, day_of_week='mon-fri',
        id='eod_liquidation', replace_existing=True,
        misfire_grace_time=60
    )
    scheduler.add_job(
        post_market_report, 'cron',
        hour=16, minute=5, day_of_week='mon-fri',
        id='post_market_report', replace_existing=True,
        misfire_grace_time=300
    )
    scheduler.add_job(
        health_check, 'interval',
        seconds=HEALTH_CHECK_INTERVAL_SECONDS,
        id='health_check', replace_existing=True
    )

    scheduler.start()
    set_scheduler(scheduler)

    job_count = len(scheduler.get_jobs())
    logger.info(f"Scheduler started with {job_count} jobs")
    log_heartbeat(f"Bot online — scheduler running with {job_count} jobs", level="info")


@app.on_event("shutdown")
async def shutdown():
    """Gracefully shut down the scheduler."""
    logger.info("Shutting down scheduler...")
    scheduler.shutdown(wait=False)
    log_heartbeat("Bot shutting down", level="warning")


if __name__ == "__main__":
    # Start ngrok tunnel if enabled
    if NGROK_ENABLED:
        try:
            import ngrok
            if NGROK_AUTHTOKEN:
                ngrok.set_auth_token(NGROK_AUTHTOKEN)
            listener = ngrok.forward(DASHBOARD_PORT, authtoken_from_env=True)
            public_url = listener.url()
            logger.info(f"")
            logger.info(f"  ╔══════════════════════════════════════════╗")
            logger.info(f"  ║  NGROK PUBLIC URL:                      ║")
            logger.info(f"  ║  {public_url:<40} ║")
            logger.info(f"  ╚══════════════════════════════════════════╝")
            logger.info(f"")
            log_heartbeat(f"Ngrok tunnel active: {public_url}", level="info")
        except Exception as e:
            logger.error(f"Failed to start ngrok: {e}")
            logger.info("Continuing without ngrok — dashboard only available locally")

    uvicorn.run(
        "main:app",
        host=DASHBOARD_HOST,
        port=DASHBOARD_PORT,
        reload=False,
        log_level="info",
    )

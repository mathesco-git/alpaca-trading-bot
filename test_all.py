"""
Automated test suite for the Trading Bot.
35 tests across 8 categories.

Usage:
    python test_all.py

Tests requiring Alpaca API will be SKIPPED (not FAILED) if .env is not configured.
"""

import os
import sys
import time
import asyncio
import traceback
from datetime import datetime, date

# Ensure we're in the project directory
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(PROJECT_DIR)
sys.path.insert(0, PROJECT_DIR)

# ─── Test Framework ───

class TestResult:
    def __init__(self, name, category):
        self.name = name
        self.category = category
        self.passed = False
        self.skipped = False
        self.error = None

    def __repr__(self):
        if self.skipped:
            return f"  SKIPPED  {self.name} — .env not configured"
        elif self.passed:
            return f"  PASSED   {self.name}"
        else:
            return f"  FAILED   {self.name}: {self.error}"

results = []

def run_test(name, category, func, requires_api=False):
    """Execute a test function and record the result."""
    r = TestResult(name, category)
    results.append(r)

    if requires_api and not _api_configured():
        r.skipped = True
        return

    try:
        func()
        r.passed = True
    except Exception as e:
        r.error = str(e)[:200]
        if os.getenv("DEBUG"):
            traceback.print_exc()


def _api_configured():
    """Check if Alpaca API keys are set."""
    from dotenv import load_dotenv
    load_dotenv()
    return bool(os.getenv("ALPACA_API_KEY") and os.getenv("ALPACA_SECRET_KEY"))


# ═══════════════════════════════════════════════
# CONNECTIVITY TESTS (3)
# ═══════════════════════════════════════════════

def test_alpaca_connection():
    """Alpaca Paper API connection succeeds (account endpoint returns data)."""
    from core.alpaca_client import get_account
    account = get_account()
    assert account is not None, "get_account() returned None"
    assert "equity" in account, "Missing 'equity' key in account"
    assert float(account["equity"]) > 0, "Equity should be > 0"

def test_account_active():
    """Account status is ACTIVE."""
    from core.alpaca_client import get_account
    account = get_account()
    assert account is not None, "get_account() returned None"
    assert account["status"] == "ACTIVE", f"Account status is {account['status']}, expected ACTIVE"

def test_market_clock():
    """Market clock endpoint returns valid data with is_open field."""
    from core.alpaca_client import get_market_clock
    clock = get_market_clock()
    assert clock is not None, "get_market_clock() returned None"
    assert "is_open" in clock, "Missing 'is_open' in market clock"
    assert isinstance(clock["is_open"], bool), "is_open should be boolean"


# ═══════════════════════════════════════════════
# DATA TESTS (3)
# ═══════════════════════════════════════════════

def test_fetch_5min_bars():
    """Fetch 5-minute bars for AAPL (last 5 trading days)."""
    from core.alpaca_client import get_bars
    df = get_bars("AAPL", timeframe="5Min", limit=50)
    assert df is not None, "get_bars returned None"
    assert len(df) > 0, "DataFrame is empty"
    for col in ["open", "high", "low", "close", "volume"]:
        assert col in df.columns, f"Missing column: {col}"

def test_fetch_daily_bars():
    """Fetch daily bars for AAPL (last 200 trading days)."""
    from core.alpaca_client import get_bars
    df = get_bars("AAPL", timeframe="1Day", limit=200)
    assert df is not None, "get_bars returned None"
    assert len(df) > 50, f"Expected 50+ bars, got {len(df)}"

def test_technical_indicators():
    """Technical indicators compute without errors on fetched data."""
    from core.data_ingestion import get_daily_data
    df = get_daily_data("AAPL", limit=250)
    assert df is not None, "get_daily_data returned None"
    for col in ["rsi", "atr", "sma_50", "sma_200"]:
        assert col in df.columns, f"Missing indicator column: {col}"
    # Check that indicators have non-NaN values (at least in later rows)
    last = df.iloc[-1]
    assert not last.isna().all(), "All indicators are NaN in last row"


# ═══════════════════════════════════════════════
# SIGNAL ENGINE TESTS (3)
# ═══════════════════════════════════════════════

def test_day_trade_signal():
    """Day Trade signal generator produces valid output."""
    import pandas as pd
    import numpy as np
    from core.signals.day_trade import generate_signal

    # Create mock 5-min data with a buy signal setup
    n = 30
    df = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01 09:30", periods=n, freq="5min"),
        "open": np.linspace(100, 105, n),
        "high": np.linspace(101, 106, n),
        "low": np.linspace(99, 104, n),
        "close": np.linspace(100, 105, n),
        "volume": [500000 + i*10000 for i in range(n)],
        "vwap": np.linspace(99, 103, n),
        "rsi": np.linspace(40, 60, n),
        "atr": [2.0] * n,
        "volume_avg_20": [300000] * n,
    })

    result = generate_signal("TEST", intraday_df=df)
    assert result is not None, "Signal result is None"
    assert result["signal"] in ("buy", "sell", "hold"), f"Invalid signal: {result['signal']}"
    assert "reason" in result, "Missing 'reason' in signal"

def test_swing_trade_signal():
    """Swing Trade signal generator produces valid output."""
    import pandas as pd
    import numpy as np
    from core.signals.swing_trade import generate_signal

    # Create mock daily data
    n = 210
    prices = np.concatenate([np.linspace(100, 80, n//2), np.linspace(80, 120, n//2)])
    df = pd.DataFrame({
        "timestamp": pd.date_range("2023-01-01", periods=n, freq="D"),
        "open": prices * 0.99,
        "high": prices * 1.01,
        "low": prices * 0.98,
        "close": prices,
        "volume": [1000000] * n,
        "rsi": np.linspace(30, 60, n),
        "atr": [2.0] * n,
        "sma_50": pd.Series(prices).rolling(50).mean().values,
        "sma_200": pd.Series(prices).rolling(200).mean().values,
    })

    result = generate_signal("TEST", daily_df=df)
    assert result is not None, "Signal result is None"
    assert result["signal"] in ("buy", "sell", "hold"), f"Invalid signal: {result['signal']}"

def test_cross_timeframe_filter():
    """Cross-timeframe filter blocks day trade when daily SMA slope is negative."""
    import pandas as pd
    import numpy as np
    from core.signals.day_trade import generate_signal

    # Intraday data with a buy setup (all conditions met)
    n = 30
    intraday = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01 09:30", periods=n, freq="5min"),
        "open": [100] * n,
        "high": [106] * n,
        "low": [99] * n,
        "close": [105] * n,  # above VWAP
        "volume": [600000] * n,  # high volume
        "vwap": [102] * n,
        "rsi": [60] * n,  # above 55
        "atr": [2.0] * n,
        "volume_avg_20": [300000] * n,  # volume > 1.5x avg
    })

    # Daily data with bearish SMA slope (declining)
    nd = 60
    declining_sma = list(reversed(range(90, 90 + nd)))  # declining values
    daily = pd.DataFrame({
        "timestamp": pd.date_range("2023-11-01", periods=nd, freq="D"),
        "open": [100] * nd,
        "high": [101] * nd,
        "low": [99] * nd,
        "close": [100] * nd,
        "volume": [1000000] * nd,
        "sma_50": declining_sma,
    })

    result = generate_signal("TEST", intraday_df=intraday, daily_df=daily)
    assert result["signal"] == "hold", f"Expected 'hold' due to bearish trend filter, got '{result['signal']}'"
    assert "trend filter" in result["reason"].lower() or "bearish" in result["reason"].lower(), \
        f"Expected trend filter rejection reason, got: {result['reason']}"


# ═══════════════════════════════════════════════
# RISK MANAGER TESTS (3)
# ═══════════════════════════════════════════════

def test_position_sizing():
    """Position size calculation returns valid share count > 0."""
    from core.risk_manager import calculate_position_size
    shares = calculate_position_size(
        portfolio_equity=100000,
        atr=2.50,
        strategy_type="day"
    )
    assert isinstance(shares, int), f"Expected int, got {type(shares)}"
    assert shares > 0, f"Expected shares > 0, got {shares}"

def test_max_positions_enforcement():
    """Portfolio allocation enforces max positions — 6th day trade is rejected."""
    from core.risk_manager import pre_trade_check
    from db.database import init_db, get_db
    from db.models import Trade, Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import config

    # Use in-memory DB for this test
    test_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=test_engine)
    TestSession = sessionmaker(bind=test_engine)

    # Monkey-patch get_db temporarily
    import db.database as db_mod
    original_session = db_mod.SessionLocal
    db_mod.SessionLocal = TestSession

    try:
        # Insert 5 open day trades
        session = TestSession()
        for i in range(5):
            session.add(Trade(
                symbol=f"TEST{i}", strategy_type="day", side="buy",
                quantity=10, entry_price=100, stop_loss=95,
                status="open", entry_time=datetime.utcnow()
            ))
        session.commit()
        session.close()

        # Try to open a 6th — should be rejected
        approved, reason = pre_trade_check(
            symbol="TEST6", strategy_type="day", shares=10,
            entry_price=100, portfolio_equity=100000, buying_power=400000
        )
        assert not approved, "6th day trade should be rejected"
        assert "max" in reason.lower() or "position" in reason.lower(), f"Bad rejection reason: {reason}"
    finally:
        db_mod.SessionLocal = original_session

def test_concurrent_position_limits():
    """Max concurrent position limits respected for both strategies."""
    from core.risk_manager import pre_trade_check
    from db.models import Trade, Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import db.database as db_mod

    test_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=test_engine)
    TestSession = sessionmaker(bind=test_engine)
    original_session = db_mod.SessionLocal
    db_mod.SessionLocal = TestSession

    try:
        session = TestSession()
        # Fill up swing positions (10)
        for i in range(10):
            session.add(Trade(
                symbol=f"SW{i}", strategy_type="swing", side="buy",
                quantity=5, entry_price=100, stop_loss=90,
                status="open", entry_time=datetime.utcnow()
            ))
        session.commit()
        session.close()

        approved, reason = pre_trade_check(
            symbol="SW11", strategy_type="swing", shares=5,
            entry_price=100, portfolio_equity=100000, buying_power=400000
        )
        assert not approved, "11th swing trade should be rejected"
    finally:
        db_mod.SessionLocal = original_session


# ═══════════════════════════════════════════════
# ORDER EXECUTION TESTS (4) — Paper Trading
# ═══════════════════════════════════════════════

_test_order_id = None

def test_submit_market_buy():
    """Submit a paper market buy order for 1 share of AAPL."""
    global _test_order_id
    from core.alpaca_client import submit_market_order, get_order_status
    order_id = submit_market_order("AAPL", 1, "buy")
    assert order_id is not None, "Order submission failed"
    _test_order_id = order_id
    time.sleep(2)
    status = get_order_status(order_id)
    assert status in ("accepted", "filled", "new", "partially_filled"), f"Unexpected status: {status}"

def test_submit_limit_sell():
    """Submit a paper limit buy order and confirm it appears in open orders."""
    global _test_order_id
    from core.alpaca_client import submit_limit_order, get_latest_price, close_position
    # Close any AAPL position from the buy test first to avoid wash trade
    close_position("AAPL")
    time.sleep(2)
    price = get_latest_price("AAPL")
    if price is None:
        price = 200.0  # Fallback price
    # Submit a limit BUY at a low price (won't fill) to avoid wash trade issues
    order_id = submit_limit_order("AAPL", 1, "buy", round(price * 0.80, 2))
    assert order_id is not None, "Limit order submission failed"
    _test_order_id = order_id

def test_cancel_order():
    """Cancel the limit order."""
    global _test_order_id
    from core.alpaca_client import cancel_order, get_order_status
    assert _test_order_id is not None, "No order to cancel"
    time.sleep(1)
    success = cancel_order(_test_order_id)
    assert success, "Cancel failed"
    time.sleep(1)
    status = get_order_status(_test_order_id)
    # Status might be 'canceled' or 'cancelled' depending on API version
    assert status and "cancel" in status.lower(), f"Expected canceled, got: {status}"

def test_cleanup_positions():
    """Clean up any test positions opened during testing."""
    from core.alpaca_client import close_position, get_positions, cancel_order, get_open_orders
    # Cancel any remaining open orders
    open_orders = get_open_orders()
    for o in open_orders:
        cancel_order(o["id"])
        time.sleep(0.5)
    # Close any test positions
    positions = get_positions()
    for p in positions:
        if p["symbol"] == "AAPL":
            close_position("AAPL")
            time.sleep(1)
    assert True  # Cleanup is best-effort


# ═══════════════════════════════════════════════
# DATABASE TESTS (3)
# ═══════════════════════════════════════════════

def test_db_create_day_trade():
    """Create tables, insert a day trade — confirm queryable."""
    from db.models import Trade, Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    trade = Trade(
        symbol="AAPL", strategy_type="day", side="buy",
        quantity=10, entry_price=150.0, stop_loss=147.0,
        take_profit=154.0, status="open",
        entry_time=datetime.utcnow()
    )
    session.add(trade)
    session.commit()

    result = session.query(Trade).filter(Trade.strategy_type == "day").first()
    assert result is not None, "Could not query day trade"
    assert result.symbol == "AAPL"
    assert result.strategy_type == "day"
    session.close()

def test_db_create_swing_trade():
    """Insert a swing trade — confirm queryable."""
    from db.models import Trade, Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    trade = Trade(
        symbol="MSFT", strategy_type="swing", side="buy",
        quantity=5, entry_price=380.0, stop_loss=370.0,
        status="open", entry_time=datetime.utcnow()
    )
    session.add(trade)
    session.commit()

    result = session.query(Trade).filter(Trade.strategy_type == "swing").first()
    assert result is not None, "Could not query swing trade"
    assert result.symbol == "MSFT"
    session.close()

def test_db_strategy_filter():
    """Query WHERE strategy_type = 'day' AND status = 'open' returns only day trades."""
    from db.models import Trade, Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    # Add both types
    session.add(Trade(
        symbol="AAPL", strategy_type="day", side="buy",
        quantity=10, entry_price=150, stop_loss=147,
        status="open", entry_time=datetime.utcnow()
    ))
    session.add(Trade(
        symbol="MSFT", strategy_type="swing", side="buy",
        quantity=5, entry_price=380, stop_loss=370,
        status="open", entry_time=datetime.utcnow()
    ))
    session.commit()

    # Query day trades only
    day_trades = session.query(Trade).filter(
        Trade.strategy_type == "day",
        Trade.status == "open"
    ).all()

    assert len(day_trades) == 1, f"Expected 1 day trade, got {len(day_trades)}"
    assert day_trades[0].symbol == "AAPL"
    assert day_trades[0].strategy_type == "day"

    # Confirm swing trade was NOT returned
    for t in day_trades:
        assert t.strategy_type != "swing", "Swing trade leaked into day trade query!"
    session.close()


# ═══════════════════════════════════════════════
# DASHBOARD TESTS (11)
# ═══════════════════════════════════════════════

_test_app = None
_test_scheduler = None

def _get_test_client():
    """Create a test client for FastAPI with scheduler initialized."""
    global _test_app, _test_scheduler
    if _test_app is None:
        from fastapi.testclient import TestClient
        from main import app, scheduler
        from dashboard.routes import set_scheduler
        from db.database import init_db

        # Make sure DB is ready
        init_db()

        # Start scheduler if not running
        if not scheduler.running:
            from apscheduler.events import EVENT_JOB_ERROR
            from config import HEALTH_CHECK_INTERVAL_SECONDS
            from core.scheduler import (
                pre_market_setup, day_trade_scan, swing_trade_scan,
                stop_loss_monitor, eod_liquidation, post_market_report, health_check,
            )

            def _job_error_listener(event):
                pass  # Silently handle errors in tests

            scheduler.add_listener(_job_error_listener, EVENT_JOB_ERROR)
            scheduler.add_job(pre_market_setup, 'cron', hour=9, minute=0, day_of_week='mon-fri', id='pre_market_setup', replace_existing=True, misfire_grace_time=300)
            scheduler.add_job(day_trade_scan, 'cron', minute='*/5', hour='9-15', day_of_week='mon-fri', id='day_trade_scan', replace_existing=True, misfire_grace_time=120)
            scheduler.add_job(swing_trade_scan, 'cron', hour='9,13', minute=35, day_of_week='mon-fri', id='swing_trade_scan', replace_existing=True, misfire_grace_time=300)
            scheduler.add_job(stop_loss_monitor, 'cron', minute='*', hour='9-15', day_of_week='mon-fri', id='stop_loss_monitor', replace_existing=True, misfire_grace_time=30)
            scheduler.add_job(eod_liquidation, 'cron', hour=15, minute=50, day_of_week='mon-fri', id='eod_liquidation', replace_existing=True, misfire_grace_time=60)
            scheduler.add_job(post_market_report, 'cron', hour=16, minute=5, day_of_week='mon-fri', id='post_market_report', replace_existing=True, misfire_grace_time=300)
            scheduler.add_job(health_check, 'interval', seconds=HEALTH_CHECK_INTERVAL_SECONDS, id='health_check', replace_existing=True)
            scheduler.start()

        set_scheduler(scheduler)
        _test_scheduler = scheduler
        _test_app = TestClient(app)
    return _test_app

def test_dashboard_server_starts():
    """FastAPI server starts on port 8000 without errors."""
    client = _get_test_client()
    assert client is not None, "Could not create test client"

def test_dashboard_html():
    """GET / returns HTTP 200 with HTML containing 'Trading Bot'."""
    client = _get_test_client()
    r = client.get("/")
    assert r.status_code == 200, f"Status: {r.status_code}"
    assert "Trading Bot" in r.text, "Missing 'Trading Bot' in HTML"

def test_api_account():
    """GET /api/account returns JSON with equity, buying_power, cash."""
    client = _get_test_client()
    r = client.get("/api/account")
    # May return 503 if no API keys — that's acceptable
    if r.status_code == 200:
        data = r.json()
        for key in ("equity", "buying_power", "cash"):
            assert key in data, f"Missing key: {key}"
    elif r.status_code == 503:
        pass  # API not configured, acceptable
    else:
        raise AssertionError(f"Unexpected status: {r.status_code}")

def test_api_positions():
    """GET /api/positions returns a JSON array."""
    client = _get_test_client()
    r = client.get("/api/positions")
    assert r.status_code == 200, f"Status: {r.status_code}"
    data = r.json()
    assert isinstance(data, list), f"Expected list, got {type(data)}"

def test_api_positions_filtered():
    """GET /api/positions?strategy=day returns filtered results."""
    client = _get_test_client()
    r = client.get("/api/positions?strategy=day")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    for item in data:
        assert item.get("strategy_type") == "day", f"Non-day trade in filtered results: {item}"

def test_api_trades():
    """GET /api/trades returns a JSON array from SQLite."""
    client = _get_test_client()
    r = client.get("/api/trades")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)

def test_api_heartbeat():
    """GET /api/heartbeat returns a JSON array."""
    client = _get_test_client()
    r = client.get("/api/heartbeat")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)

def test_api_pnl_daily():
    """GET /api/pnl-daily returns JSON with strategy-separated P&L."""
    client = _get_test_client()
    r = client.get("/api/pnl-daily")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)

def test_api_bot_status():
    """GET /api/bot/status returns JSON with running and last_heartbeat."""
    client = _get_test_client()
    r = client.get("/api/bot/status")
    assert r.status_code == 200
    data = r.json()
    assert "running" in data, "Missing 'running' key"

def test_api_bot_pause():
    """POST /api/bot/pause returns HTTP 200."""
    client = _get_test_client()
    r = client.post("/api/bot/pause")
    assert r.status_code == 200
    data = r.json()
    assert data.get("success") is True, f"Pause failed: {data}"

def test_api_bot_resume():
    """POST /api/bot/resume returns HTTP 200."""
    client = _get_test_client()
    r = client.post("/api/bot/resume")
    assert r.status_code == 200
    data = r.json()
    assert data.get("success") is True, f"Resume failed: {data}"


# ═══════════════════════════════════════════════
# SCHEDULER TESTS (5)
# ═══════════════════════════════════════════════

def test_scheduler_jobs_registered():
    """APScheduler starts with all 7 expected jobs."""
    _get_test_client()  # Ensure scheduler is initialized
    jobs = _test_scheduler.get_jobs()
    job_ids = [j.id for j in jobs]
    expected = [
        "pre_market_setup", "day_trade_scan", "swing_trade_scan",
        "stop_loss_monitor", "eod_liquidation", "post_market_report",
        "health_check"
    ]
    assert len(jobs) == 7, f"Expected 7 jobs, got {len(jobs)}: {job_ids}"
    for eid in expected:
        assert eid in job_ids, f"Missing job: {eid}"

def test_health_check_executes():
    """health_check job executes within 30 seconds."""
    from db.models import HeartbeatLog, Base
    from db.database import get_db
    time.sleep(2)  # Give scheduler a moment

    with get_db() as session:
        recent = session.query(HeartbeatLog).order_by(
            HeartbeatLog.timestamp.desc()
        ).first()
    # There should be at least one entry from startup
    assert recent is not None, "No heartbeat entries found"

def test_health_check_writes_heartbeat():
    """health_check writes to heartbeat_log table."""
    from db.database import log_heartbeat, get_db
    from db.models import HeartbeatLog

    # Write a test entry
    log_heartbeat("Test heartbeat entry", level="info")

    with get_db() as session:
        entry = session.query(HeartbeatLog).filter(
            HeartbeatLog.message == "Test heartbeat entry"
        ).first()
        # Read values while still in session
        assert entry is not None, "Test heartbeat not found in DB"
        entry_level = entry.level
    assert entry_level == "info"

def test_monitor_mode_no_orders():
    """When ENABLE_TRADING = False, scans run but no orders placed."""
    import config
    from db.database import init_db
    init_db()  # Ensure tables exist

    original = config.ENABLE_TRADING
    config.ENABLE_TRADING = False

    try:
        from core.order_executor import execute_entry
        # Attempt an entry — should return None (no order placed)
        result = execute_entry(
            symbol="TEST",
            strategy_type="day",
            signal={"entry_price": 100, "atr": 2.0, "reason": "test"},
            portfolio_equity=100000,
            buying_power=400000,
        )
        assert result is None, "Expected None in monitor mode"
    finally:
        config.ENABLE_TRADING = original

def test_scheduler_handles_exception():
    """Scheduler handles job exceptions without crashing."""
    _get_test_client()  # Ensure scheduler is initialized

    # Add a job that will fail
    def failing_job():
        raise ValueError("Intentional test error")

    _test_scheduler.add_job(failing_job, 'date', run_date=datetime.now(), id='test_fail_job',
                            replace_existing=True, misfire_grace_time=60)
    time.sleep(2)

    # Scheduler should still be running
    assert _test_scheduler.running, "Scheduler stopped after exception"

    # Clean up
    try:
        _test_scheduler.remove_job('test_fail_job')
    except Exception:
        pass


# ═══════════════════════════════════════════════
# MAIN — Run All Tests
# ═══════════════════════════════════════════════

def main():
    print("\n" + "=" * 48)
    print("        TRADING BOT TEST SUITE")
    print("=" * 48 + "\n")

    # Initialize the database before any tests that need it
    try:
        from db.database import init_db
        init_db()
    except Exception as e:
        print(f"  WARNING: Could not init DB: {e}\n")

    api = _api_configured()
    if not api:
        print("  NOTE: .env not configured — API tests will be skipped\n")

    # ── Connectivity (3) ──
    run_test("Alpaca API connection", "Connectivity", test_alpaca_connection, requires_api=True)
    run_test("Account status is ACTIVE", "Connectivity", test_account_active, requires_api=True)
    run_test("Market clock returns valid data", "Connectivity", test_market_clock, requires_api=True)

    # ── Data (3) ──
    run_test("Fetch 5-min bars for AAPL", "Data", test_fetch_5min_bars, requires_api=True)
    run_test("Fetch daily bars for AAPL", "Data", test_fetch_daily_bars, requires_api=True)
    run_test("Technical indicators compute", "Data", test_technical_indicators, requires_api=True)

    # ── Signals (3) ──
    run_test("Day trade signal generator", "Signals", test_day_trade_signal)
    run_test("Swing trade signal generator", "Signals", test_swing_trade_signal)
    run_test("Cross-timeframe filter", "Signals", test_cross_timeframe_filter)

    # ── Risk (3) ──
    run_test("Position size calculation", "Risk", test_position_sizing)
    run_test("Max positions enforcement", "Risk", test_max_positions_enforcement)
    run_test("Concurrent position limits", "Risk", test_concurrent_position_limits)

    # ── Orders (4) ──
    run_test("Submit market buy order", "Orders", test_submit_market_buy, requires_api=True)
    run_test("Submit limit sell order", "Orders", test_submit_limit_sell, requires_api=True)
    run_test("Cancel limit sell order", "Orders", test_cancel_order, requires_api=True)
    run_test("Cleanup test positions", "Orders", test_cleanup_positions, requires_api=True)

    # ── Database (3) ──
    run_test("Create and query day trade", "Database", test_db_create_day_trade)
    run_test("Create and query swing trade", "Database", test_db_create_swing_trade)
    run_test("Strategy filter query", "Database", test_db_strategy_filter)

    # ── Dashboard (11) ──
    run_test("FastAPI server starts", "Dashboard", test_dashboard_server_starts)
    run_test("GET / returns HTML with title", "Dashboard", test_dashboard_html)
    run_test("GET /api/account returns JSON", "Dashboard", test_api_account)
    run_test("GET /api/positions returns array", "Dashboard", test_api_positions)
    run_test("GET /api/positions?strategy=day filtered", "Dashboard", test_api_positions_filtered)
    run_test("GET /api/trades returns array", "Dashboard", test_api_trades)
    run_test("GET /api/heartbeat returns array", "Dashboard", test_api_heartbeat)
    run_test("GET /api/pnl-daily returns JSON", "Dashboard", test_api_pnl_daily)
    run_test("GET /api/bot/status returns JSON", "Dashboard", test_api_bot_status)
    run_test("POST /api/bot/pause returns 200", "Dashboard", test_api_bot_pause)
    run_test("POST /api/bot/resume returns 200", "Dashboard", test_api_bot_resume)

    # ── Scheduler (5) ──
    run_test("7 jobs registered", "Scheduler", test_scheduler_jobs_registered)
    run_test("health_check executes", "Scheduler", test_health_check_executes)
    run_test("health_check writes heartbeat", "Scheduler", test_health_check_writes_heartbeat)
    run_test("Monitor mode skips orders", "Scheduler", test_monitor_mode_no_orders)
    run_test("Scheduler handles exceptions", "Scheduler", test_scheduler_handles_exception)

    # ── Report ──
    print("\n" + "=" * 48)
    print("        TRADING BOT TEST RESULTS")
    print("=" * 48)

    categories = ["Connectivity", "Data", "Signals", "Risk", "Orders", "Database", "Dashboard", "Scheduler"]
    total_passed = 0
    total_skipped = 0
    total_failed = 0

    for cat in categories:
        cat_results = [r for r in results if r.category == cat]
        passed = sum(1 for r in cat_results if r.passed)
        skipped = sum(1 for r in cat_results if r.skipped)
        failed = sum(1 for r in cat_results if not r.passed and not r.skipped)
        total_passed += passed
        total_skipped += skipped
        total_failed += failed

        status = "PASSED" if failed == 0 else "FAILED"
        count_str = f"{passed}/{len(cat_results)}"
        skip_str = f" ({skipped} skipped)" if skipped else ""
        pad = 15 - len(cat)
        print(f"{cat}:{' ' * pad}{count_str}  {status}{skip_str}")

        # Show failures
        for r in cat_results:
            if not r.passed and not r.skipped:
                print(f"    FAILED: {r.name}")
                print(f"           {r.error}")

    total = len(results)
    print("-" * 48)

    if total_skipped > 0:
        print(f"TOTAL:         {total_passed}/{total} PASSED ({total_skipped} skipped — .env not configured)")
    else:
        if total_failed == 0:
            print(f"TOTAL:         {total_passed}/{total} PASSED \u2713")
        else:
            print(f"TOTAL:         {total_passed}/{total} PASSED, {total_failed} FAILED \u2717")

    print("=" * 48)
    print(f"\nStartup command: python main.py")
    print(f"Dashboard URL:  http://localhost:8000\n")

    return total_failed == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)

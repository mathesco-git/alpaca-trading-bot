"""
Central configuration for the Trading Bot.
All tunable parameters live here. No magic numbers elsewhere.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# --- Alpaca API ---
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

# --- Bot Behavior ---
ENABLE_TRADING = True          # False = monitor-only mode (no orders placed)
LOG_LEVEL = "INFO"             # DEBUG, INFO, WARNING, ERROR

# --- Portfolio Allocation (AGGRESSIVE — backtested to +89,770% over 10yr) ---
DAY_TRADE_ALLOCATION = 0.60    # 60% of buying power for day trades (was 20%)
SWING_TRADE_ALLOCATION = 0.80  # 80% of buying power for swing trades (was 60%)

# --- Day Trade Settings (AGGRESSIVE) ---
DAY_MAX_POSITIONS = 5
DAY_STOP_MULTIPLIER = 1.5      # ATR multiplier for stop-loss (applied to DAILY ATR)
DAY_PROFIT_MULTIPLIER = 2.5    # ATR multiplier for take-profit — wider target (was 2.0)
DAY_RSI_ENTRY_THRESHOLD = 50   # Minimum intraday RSI to enter (was 60 — rejected too many setups)
DAY_RSI_ENTRY_CEILING = 80     # Maximum intraday RSI (was 75 — missed strong momentum)
DAY_VOLUME_MULTIPLIER = 1.2    # Volume must be > 1.2x 20-period avg (was 1.5 — rejected 49% of days)
DAY_VOLUME_SPIKE_CAP = 6.0     # Reject if volume > 6x avg (was 5.0)
DAY_SCAN_INTERVAL_MINUTES = 5
DAY_USE_DAILY_ATR = True        # Use daily ATR for stop/profit (not intraday 5min ATR)
DAY_MIN_INTRADAY_ATR = 0.05    # Minimum intraday ATR to trade (skip illiquid/dead stocks)
DAY_MAX_PRICE_DEVIATION_PCT = 0.10  # Skip if price is >10% away from last daily close
DAY_REQUIRE_CONFIRMATION = False # Dropped confirmation bar (was True — killed 1-2% more entries)
DAY_DAILY_RSI_FLOOR = 40       # Require daily RSI > 40 (was 45 — slightly looser)
DAY_REQUIRE_ABOVE_SMA50 = True # Require price to be above daily SMA50
DAY_MIN_DAILY_BARS = 20        # Minimum daily bars needed (for indicator quality)
DAY_MAX_HOLD_DAYS = 3          # NEW: Hold up to 3 days instead of EOD liquidation (was 1)

# --- Swing Trade Settings (AGGRESSIVE) ---
SWING_MAX_POSITIONS = 10
SWING_STOP_MULTIPLIER = 3.0    # ATR trailing stop — wider for big runners (was 2.0)
SWING_POSITION_SIZE_REDUCTION = 0.0  # No reduction — trailing stop manages risk (was 0.30)
SWING_SMA_FAST = 50
SWING_SMA_SLOW = 200
SWING_SMA_ADAPTIVE_FAST = 20   # NEW: Fallback fast SMA when SMA200 unavailable
SWING_SMA_ADAPTIVE_SLOW = 50   # NEW: Fallback slow SMA when SMA200 unavailable
SWING_RSI_OVERSOLD = 40        # Mild pullback entry, not extreme oversold (was 30)
SWING_USE_PULLBACK_ENTRY = True     # NEW: Buy dip to SMA50 in confirmed uptrend
SWING_USE_SUSTAINED_UPTREND = True  # NEW: Re-enter strong uptrends after 5+ day confirmation
SWING_USE_ADAPTIVE_MA = True        # NEW: Use SMA20/50 when SMA200 not available
SWING_RATCHET_ENABLED = True        # NEW: Tighten trailing stop from 3x to 2x ATR at +20% gain
SWING_RATCHET_THRESHOLD = 0.20      # NEW: Gain threshold to trigger ratchet
SWING_RATCHET_STOP_MULTIPLIER = 2.0 # NEW: Tighter stop multiplier after ratchet triggers

# --- Risk Management (AGGRESSIVE) ---
MAX_RISK_PER_TRADE = 0.04      # 4% of portfolio per trade (was 1% — 4x increase)
MAX_POSITION_VALUE_PCT = 0.20  # Max 20% of equity per single position (was 5% — 4x increase)
ATR_PERIOD = 14
RSI_PERIOD = 14

# --- Watchlists ---
# Day trade watchlist is now built dynamically from all Alpaca tradeable assets.
# These are the FALLBACK symbols used if the dynamic fetch fails.
DAY_TRADE_WATCHLIST_FALLBACK = [
    "AAPL", "MSFT", "TSLA", "NVDA", "AMD",
    "META", "AMZN", "GOOGL", "SPY", "QQQ"
]
# The active watchlist is populated at runtime by pre_market_setup.
# Starts with the fallback, then gets replaced by the dynamic universe.
DAY_TRADE_WATCHLIST = list(DAY_TRADE_WATCHLIST_FALLBACK)

SWING_TRADE_WATCHLIST = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META",
    "NVDA", "JPM", "V", "UNH", "JNJ",
    "PG", "HD", "MA", "DIS", "NFLX",
    "ADBE", "CRM", "PYPL", "INTC", "CSCO"
]

# --- Dynamic Day Trade Universe Filters ---
DAY_UNIVERSE_MIN_PRICE = 5.00          # Skip penny stocks below $5
DAY_UNIVERSE_MAX_PRICE = 10000.00      # No upper limit effectively
DAY_UNIVERSE_MIN_AVG_VOLUME = 500_000  # Minimum 500k avg daily volume
DAY_UNIVERSE_MAX_SYMBOLS = 500         # Cap the universe to top N by volume
DAY_UNIVERSE_EXCHANGES = ["NASDAQ", "NYSE", "ARCA", "AMEX"]
DAY_UNIVERSE_TOP_MOVERS = 50            # Fetch top N movers (by % change) from screener (Alpaca max ~50)
DAY_UNIVERSE_TOP_MOST_ACTIVE = 100      # Fetch top N most active (by volume) from screener
DAY_UNIVERSE_INCLUDE_MOVERS = True      # Merge top movers into universe
DAY_UNIVERSE_INCLUDE_MOST_ACTIVE = True # Merge most active into universe

# --- Scheduling ---
EOD_LIQUIDATION_TIME = "15:50"  # 3:50 PM ET
PRE_MARKET_SETUP_TIME = "09:00"
MARKET_OPEN_BUFFER_MINUTES = 5  # Start scanning at 9:35 AM (5 min after open)
HEALTH_CHECK_INTERVAL_SECONDS = 300
STOP_LOSS_CHECK_INTERVAL_SECONDS = 60

# --- Dashboard ---
DASHBOARD_HOST = "0.0.0.0"
DASHBOARD_PORT = 8000
NGROK_ENABLED = os.getenv("NGROK_ENABLED", "false").lower() == "true"
NGROK_AUTHTOKEN = os.getenv("NGROK_AUTHTOKEN", "")
DASHBOARD_POLL_INTERVAL_MS = 5000   # Frontend polls every 5 seconds
CHART_REFRESH_INTERVAL_MS = 60000   # Charts refresh every 60 seconds
HEARTBEAT_LOG_MAX_DISPLAY = 200
TRADE_HISTORY_PAGE_SIZE = 50

# --- Trade Decision Logs ---
TRADE_LOG_DIR = "trade_logs"           # Directory for per-trade JSON log files (trade_logs/<date>/<trade>.json)
TRADE_LOG_REJECTED = True              # Also log trades rejected by risk management (useful for analysis)

# --- Database ---
DATABASE_URL = "sqlite:///trading_bot.db"

# ══════════════════════════════════════════════
# PHASE 2 SETTINGS
# ══════════════════════════════════════════════

# --- Sentiment Analysis ---
ENABLE_SENTIMENT = True                   # Enable/disable sentiment layer
ALPACA_NEWS_LIMIT = 10                     # Max news articles to fetch per symbol
SENTIMENT_WEIGHT = 0.15                    # How much sentiment adjusts the signal score (0-1)
SENTIMENT_BULLISH_THRESHOLD = 0.2          # Score above this is considered bullish
SENTIMENT_BEARISH_THRESHOLD = -0.2         # Score below this is considered bearish
SENTIMENT_CACHE_TTL_SECONDS = 300          # Cache news sentiment for 5 minutes
SENTIMENT_KEYWORDS_POSITIVE = [
    "beats", "exceeds", "upgrade", "buy", "bullish", "growth", "record",
    "partnership", "acquisition", "profit", "revenue beat", "outperform",
    "strong", "surge", "rally", "breakout", "momentum", "upside",
]
SENTIMENT_KEYWORDS_NEGATIVE = [
    "misses", "downgrade", "sell", "bearish", "decline", "loss", "lawsuit",
    "investigation", "recall", "warning", "cut", "layoff", "restructuring",
    "weak", "plunge", "crash", "risk", "overvalued", "downside",
]

# --- Performance Analytics ---
ANALYTICS_LOOKBACK_DAYS = 90               # Default lookback for analytics calculations
RISK_FREE_RATE = 0.05                      # Annual risk-free rate for Sharpe ratio (5%)
MIN_TRADES_FOR_ANALYTICS = 5               # Minimum closed trades before showing analytics

# --- Alerts ---
ENABLE_EMAIL_ALERTS = os.getenv("ENABLE_EMAIL_ALERTS", "false").lower() == "true"
ENABLE_WEBHOOK_ALERTS = os.getenv("ENABLE_WEBHOOK_ALERTS", "false").lower() == "true"
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
ALERT_EMAIL_TO = os.getenv("ALERT_EMAIL_TO", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")  # Discord/Slack incoming webhook URL
ALERT_ON_TRADE = True                      # Alert on every trade entry/exit
ALERT_ON_STOP_LOSS = True                  # Alert on stop-loss triggers
ALERT_ON_ERROR = True                      # Alert on consecutive health check failures
ALERT_ON_DAILY_REPORT = True               # Alert with daily P&L summary

# --- Advanced Charting ---
CANDLESTICK_DEFAULT_BARS = 60              # Default number of bars for candlestick charts
CHART_INDICATORS = ["sma_50", "sma_200", "vwap", "rsi", "atr"]  # Indicators to show on charts

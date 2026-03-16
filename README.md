# MTH Trader — Automated US Equities Trading Bot

A production-grade automated trading bot for US equities using Alpaca's Paper Trading API. It runs two concurrent strategies (Day Trading and Swing Trading) with a real-time web dashboard, sentiment analysis, performance analytics, and multi-channel alert notifications.

Built with Python 3.14, FastAPI, SQLAlchemy, and APScheduler.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Project Structure](#project-structure)
3. [Getting Started](#getting-started)
4. [Configuration Reference](#configuration-reference)
5. [Trading Strategies](#trading-strategies)
6. [Risk Management](#risk-management)
7. [Scheduled Jobs](#scheduled-jobs)
8. [Dashboard & API](#dashboard--api)
9. [Phase 2 Features](#phase-2-features)
10. [Database Schema](#database-schema)
11. [Performance Optimizations](#performance-optimizations)
12. [Extending the Bot](#extending-the-bot)

---

## Architecture Overview

The bot runs as a single Python process containing three subsystems:

1. **FastAPI web server** (Uvicorn) serves the dashboard UI and JSON API endpoints on port 8000.
2. **APScheduler** (BackgroundScheduler, US/Eastern timezone) runs 7 scheduled jobs in background threads for scanning, monitoring, and reporting.
3. **SQLite database** (via SQLAlchemy ORM) stores all trades, heartbeat logs, and daily equity snapshots.

### Data Flow

```
Alpaca Market Data API
        |
        v
alpaca_client.py (singleton clients, response caching, rate-limit retry)
        |
        v
data_ingestion.py (fetch bars, compute RSI/ATR/VWAP/SMA indicators, 5-min cache)
        |
        v
signals/day_trade.py  &  signals/swing_trade.py  (buy/sell/hold decisions)
        |
        v
sentiment.py (Phase 2 — optional filter: block buys on bearish news)
        |
        v
risk_manager.py (position sizing, pre-trade validation, allocation checks)
        |
        v
order_executor.py (submit orders to Alpaca, record trades in SQLite)
        |
        v
scheduler.py (orchestrates all jobs throughout the trading day)
        |
        +---> stop_loss_monitor (every 1 min — checks stop-loss & take-profit)
        +---> eod_liquidation (3:50 PM — force-closes all day trades)
        +---> post_market_report (4:05 PM — daily P&L snapshot)
        +---> health_check (every 30 sec — API connectivity monitoring)
        +---> alerts.py (email via SMTP, Discord/Slack webhooks)
        |
        v
dashboard/routes.py (21 JSON API endpoints, all async with thread offloading)
        |
        v
dashboard/templates/index.html (Tailwind CSS dark theme, Chart.js, live polling)
```

---

## Project Structure

```
trading-bot/
├── main.py                          # Entry point: FastAPI + APScheduler + Uvicorn
├── config.py                        # Central configuration (all tunable parameters)
├── requirements.txt                 # Python dependencies
├── trading_bot.db                   # SQLite database (auto-created on first run)
├── trading_bot.log                  # Rotating log file (10 MB max, 5 backups)
│
├── core/                            # Core trading logic
│   ├── alpaca_client.py             # Alpaca API wrapper (singleton + cache)
│   ├── order_executor.py            # Trade entry/exit execution
│   ├── scheduler.py                 # 7 scheduled job functions
│   ├── risk_manager.py              # Position sizing & pre-trade validation
│   ├── data_ingestion.py            # Bar data fetching & technical indicators
│   ├── sentiment.py                 # Phase 2: News sentiment analysis
│   ├── analytics.py                 # Phase 2: Performance metrics calculation
│   ├── alerts.py                    # Phase 2: Email & webhook notifications
│   └── signals/
│       ├── day_trade.py             # VWAP breakout + volume + RSI strategy
│       └── swing_trade.py           # Golden Cross + RSI mean reversion strategy
│
├── db/                              # Database layer
│   ├── database.py                  # SQLAlchemy engine, session factory, helpers
│   └── models.py                    # ORM models: Trade, HeartbeatLog, EquityHistory
│
├── dashboard/                       # Web dashboard
│   ├── routes.py                    # FastAPI router with 21 API endpoints
│   └── templates/
│       └── index.html               # Single-page dashboard (Jinja2 + Tailwind + Chart.js)
│
├── utils/
│   └── logger.py                    # Logging config (console + rotating file)
│
└── test_all.py                      # Test suite (35 tests)
```

---

## Getting Started

### Prerequisites

- Python 3.10+ (developed on 3.14)
- Alpaca Paper Trading account (free at https://alpaca.markets)

### Installation

```bash
cd trading-bot
pip install -r requirements.txt
```

### Environment Variables

Create a `.env` file in the `trading-bot/` directory:

```env
# Required — Alpaca Paper Trading API
ALPACA_API_KEY=your_api_key_here
ALPACA_SECRET_KEY=your_secret_key_here
ALPACA_BASE_URL=https://paper-api.alpaca.markets

# Optional — Discord/Slack Webhook Alerts
ENABLE_WEBHOOK_ALERTS=true
WEBHOOK_URL=https://discord.com/api/webhooks/your/webhook/url

# Optional — Email Alerts
ENABLE_EMAIL_ALERTS=false
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your_email@gmail.com
SMTP_PASSWORD=your_app_password
ALERT_EMAIL_TO=recipient@example.com

# Optional — Public URL via ngrok
NGROK_ENABLED=false
NGROK_AUTHTOKEN=your_ngrok_authtoken
```

### Running

```bash
python main.py
```

Dashboard available at `http://localhost:8000`. If ngrok is enabled, a public URL is printed in the console.

### Running Tests

```bash
python test_all.py
```

35 tests covering all core modules (alpaca_client, data_ingestion, risk_manager, order_executor, signals, scheduler, dashboard routes, database).

---

## Configuration Reference

All tunable parameters live in `config.py`. No magic numbers elsewhere in the codebase.

### Portfolio Allocation

| Parameter | Default | Description |
|-----------|---------|-------------|
| `DAY_TRADE_ALLOCATION` | 0.40 | 40% of buying power reserved for day trades |
| `SWING_TRADE_ALLOCATION` | 0.60 | 60% of buying power reserved for swing trades |
| `MAX_RISK_PER_TRADE` | 0.02 | Maximum 2% of portfolio equity risked per trade |

### Day Trade Settings

| Parameter | Default | Description |
|-----------|---------|-------------|
| `DAY_MAX_POSITIONS` | 5 | Maximum concurrent day trade positions |
| `DAY_STOP_MULTIPLIER` | 1.5 | ATR multiplier for stop-loss distance |
| `DAY_PROFIT_MULTIPLIER` | 2.0 | ATR multiplier for take-profit target |
| `DAY_RSI_ENTRY_THRESHOLD` | 55 | Minimum RSI for buy signal |
| `DAY_VOLUME_MULTIPLIER` | 1.5 | Volume must exceed 1.5x the 20-period average |
| `DAY_SCAN_INTERVAL_MINUTES` | 5 | Scan frequency during market hours |

### Swing Trade Settings

| Parameter | Default | Description |
|-----------|---------|-------------|
| `SWING_MAX_POSITIONS` | 10 | Maximum concurrent swing positions |
| `SWING_STOP_MULTIPLIER` | 2.0 | ATR multiplier for trailing stop |
| `SWING_POSITION_SIZE_REDUCTION` | 0.30 | 30% smaller positions than day trades (gap risk) |
| `SWING_SMA_FAST` | 50 | Fast SMA period for Golden/Death Cross |
| `SWING_SMA_SLOW` | 200 | Slow SMA period for Golden/Death Cross |
| `SWING_RSI_OVERSOLD` | 30 | RSI threshold for mean reversion entry |

### Watchlists

**Day Trade** (10 symbols): AAPL, MSFT, TSLA, NVDA, AMD, META, AMZN, GOOGL, SPY, QQQ

**Swing Trade** (20 symbols): AAPL, MSFT, GOOGL, AMZN, META, NVDA, JPM, V, UNH, JNJ, PG, HD, MA, DIS, NFLX, ADBE, CRM, PYPL, INTC, CSCO

### Scheduling

| Parameter | Default | Description |
|-----------|---------|-------------|
| `EOD_LIQUIDATION_TIME` | 15:50 ET | Force-close all day trades |
| `PRE_MARKET_SETUP_TIME` | 09:00 ET | Pre-market data refresh |
| `MARKET_OPEN_BUFFER_MINUTES` | 5 | Wait 5 min after open before scanning |
| `HEALTH_CHECK_INTERVAL_SECONDS` | 30 | API connectivity ping interval |
| `STOP_LOSS_CHECK_INTERVAL_SECONDS` | 60 | Stop-loss monitoring interval |

### Dashboard

| Parameter | Default | Description |
|-----------|---------|-------------|
| `DASHBOARD_HOST` | 0.0.0.0 | Listen on all interfaces |
| `DASHBOARD_PORT` | 8000 | HTTP port |
| `DASHBOARD_POLL_INTERVAL_MS` | 5000 | Frontend polls every 5 seconds |
| `CHART_REFRESH_INTERVAL_MS` | 60000 | Charts refresh every 60 seconds |

### Phase 2 — Sentiment

| Parameter | Default | Description |
|-----------|---------|-------------|
| `ENABLE_SENTIMENT` | True | Enable/disable sentiment layer |
| `ALPACA_NEWS_LIMIT` | 10 | Max news articles per symbol |
| `SENTIMENT_WEIGHT` | 0.15 | Signal adjustment weight (0–1) |
| `SENTIMENT_BULLISH_THRESHOLD` | 0.2 | Score above this = bullish |
| `SENTIMENT_BEARISH_THRESHOLD` | -0.2 | Score below this = bearish |
| `SENTIMENT_CACHE_TTL_SECONDS` | 300 | Cache per-symbol sentiment for 5 min |

### Phase 2 — Analytics

| Parameter | Default | Description |
|-----------|---------|-------------|
| `ANALYTICS_LOOKBACK_DAYS` | 90 | Default lookback for metric calculation |
| `RISK_FREE_RATE` | 0.05 | Annual risk-free rate for Sharpe ratio |
| `MIN_TRADES_FOR_ANALYTICS` | 5 | Minimum closed trades before showing metrics |

### Phase 2 — Alerts

| Parameter | Default | Description |
|-----------|---------|-------------|
| `ENABLE_EMAIL_ALERTS` | from .env | Enable email alerts via SMTP |
| `ENABLE_WEBHOOK_ALERTS` | from .env | Enable webhook alerts (Discord/Slack) |
| `WEBHOOK_URL` | from .env | Incoming webhook URL |
| `ALERT_ON_TRADE` | True | Alert on trade entries and exits |
| `ALERT_ON_STOP_LOSS` | True | Alert on stop-loss triggers |
| `ALERT_ON_ERROR` | True | Alert on consecutive health check failures |
| `ALERT_ON_DAILY_REPORT` | True | Alert with daily P&L summary |

---

## Trading Strategies

### Day Trade Strategy — VWAP Breakout (`core/signals/day_trade.py`)

Identifies short-term momentum setups on 5-minute candles.

**Entry conditions** (all must be true):
1. **VWAP Breakout**: Price breaks above VWAP (volume-weighted average price)
2. **Volume Surge**: Volume exceeds 1.5x the 20-period average
3. **RSI Momentum**: RSI(14) is above 55
4. **Trend Filter**: Daily 50 SMA slope must be positive over the last 5 bars — rejects counter-trend trades where the daily trend is bearish

**Exit conditions**:
- Price drops below VWAP AND RSI is above 70 (reversal signal)
- Stop-loss: 1.5x ATR below entry price
- Take-profit: 2.0x ATR above entry price
- EOD forced liquidation at 3:50 PM ET (all day trades closed regardless)

**Data requirements**: 100 bars of 5-minute data + 60 bars of daily data per symbol.

### Swing Trade Strategy — Golden Cross + Mean Reversion (`core/signals/swing_trade.py`)

Identifies multi-day trend setups using daily candles.

**Entry signals** (either one triggers a buy):
1. **Golden Cross**: 50 SMA crosses above 200 SMA (trend reversal to bullish)
2. **Mean Reversion**: RSI(14) drops below 30 while price remains above the 200 SMA (oversold pullback in an intact uptrend)

**Exit signal**:
- **Death Cross**: 50 SMA crosses below 200 SMA (trend reversal to bearish)
- Trailing stop: 2.0x ATR below current price (only moves upward, never down)

**Data requirements**: 250 bars of daily data per symbol (enough for 200 SMA calculation + buffer).

**Key differences from day trades**: Swing positions have no take-profit target (let winners run), use trailing stops that ratchet upward only, get 30% smaller position sizes for overnight gap risk, and are never affected by the EOD liquidation job.

---

## Risk Management

The risk manager (`core/risk_manager.py`) enforces four layers of protection:

### 1. ATR-Based Position Sizing

Formula: `shares = (equity * 2% * allocation%) / (ATR * stop_multiplier)`

- Day trades: 40% allocation, 1.5x ATR stop multiplier
- Swing trades: 60% allocation, 2x ATR stop multiplier, then an additional 30% position reduction for overnight gap risk
- Minimum 1 share per trade

### 2. Pre-Trade Validation

Every trade passes three checks before order submission:

1. **Max positions check**: Day trades max 5, swing trades max 10 concurrent positions
2. **Allocation check**: Order cost cannot exceed remaining buying power allocation for that strategy type
3. **Single trade risk check**: No single trade can be unreasonably large relative to portfolio equity

### 3. Stop-Loss & Take-Profit

- Day trades: Fixed stop-loss (1.5x ATR below entry) and fixed take-profit (2.0x ATR above entry)
- Swing trades: Trailing stop-loss (2.0x ATR below current price), no take-profit
- Trailing stops only move UP for long positions — they are never lowered
- Stop-loss monitor runs every 60 seconds during market hours

### 4. EOD Forced Liquidation

At 3:50 PM ET, all day trade positions are force-closed. The `strategy_type` field on every trade record is the single source of truth that ensures swing positions are never touched. Any remaining open day trades after liquidation are marked `eod_liquidated`.

### Monitor-Only Mode

Setting `ENABLE_TRADING = False` in config.py puts the bot in monitor-only mode. Signals are generated and logged, but no actual orders are submitted to Alpaca. Useful for validating strategy logic before going live on paper trading.

---

## Scheduled Jobs

Seven jobs are registered with APScheduler, all running in the US/Eastern timezone:

| # | Job | Schedule | Description |
|---|-----|----------|-------------|
| 1 | `pre_market_setup` | 9:00 AM, weekdays | Clear data caches. Fetch 250 daily bars for all watchlist symbols. Generate initial swing trade signals. |
| 2 | `day_trade_scan` | Every 5 min, 9:35 AM–3:45 PM | Fetch 100 5-min bars + 60 daily bars for day trade watchlist. Generate signals, apply sentiment filter, execute entries via order executor. |
| 3 | `swing_trade_scan` | 9:35 AM + 1:00 PM | Fetch 250 daily bars for swing watchlist. Generate signals, apply sentiment filter, execute entries. Update trailing stops on all open swing positions. Process sell signals for death cross exits. |
| 4 | `stop_loss_monitor` | Every 1 min, 9:30 AM–4:00 PM | Get latest price for every open position. Trigger stop-loss or take-profit exits when levels are breached. Send alerts on stop-loss hits. |
| 5 | `eod_liquidation` | 3:50 PM | Force-close ALL day trade positions. Mark any stragglers as `eod_liquidated`. Never touches swing trades. |
| 6 | `post_market_report` | 4:05 PM | Calculate daily P&L by strategy. Upsert equity snapshot to `equity_history` table. Send daily report alert via configured channels. |
| 7 | `health_check` | Every 30 seconds (always) | Ping Alpaca API (get_account). Track consecutive failures. Send critical error alert at 5+ consecutive failures. Log market open/closed status. |

### Execution Timeline (US/Eastern)

```
08:00-09:00   Pre-market: health_check running every 30s
09:00         pre_market_setup: load daily bars for all symbols, prepare swing signals
09:30         NYSE market opens
09:35         First day_trade_scan + first swing_trade_scan
09:36         First stop_loss_monitor check
09:40         Second day_trade_scan (repeats every 5 min)
...
13:00         Second swing_trade_scan (update trailing stops, check entries)
...
15:45         Last day_trade_scan of the day
15:50         eod_liquidation: force-close all day trades
16:00         NYSE market closes
16:05         post_market_report: equity snapshot + daily report alert
16:06+        Only health_check continues running (every 30s)
```

### Retry Logic

Each job runs inside `_run_with_retry()`: if the first attempt fails, the error is logged, it waits 10 seconds, and retries once. APScheduler's error listener catches any remaining unhandled exceptions and logs them without crashing the bot process.

---

## Dashboard & API

### Web UI (`dashboard/templates/index.html`)

Single-page dashboard built with Jinja2 templates, Tailwind CSS (CDN, dark theme), and Chart.js (CDN).

**Dashboard sections**:
- **Top bar**: Account overview (equity, cash, buying power), allocation bars showing day/swing usage, daily P&L with percentage
- **Bot status**: Running/paused indicator with pulsing heartbeat animation (green = healthy, yellow = degraded, red = error), market open/closed with countdown to next open, uptime display
- **Bot activity panel**: Process uptime, scheduler status, upcoming jobs grid with next fire times for each job
- **Active positions**: Live positions table showing symbol, strategy, quantity, entry price, current price, unrealized P&L, and a close button per position
- **Trade history**: Paginated table of all historical trades, filterable by strategy type
- **Charts**: Equity curve (line chart over time), daily P&L by strategy (stacked bar chart)
- **Heartbeat log**: Scrolling activity feed of all bot operations, color-coded by severity
- **Manual controls**: Pause/resume scheduler, toggle trading on/off, close all positions by strategy type
- **Test trade form**: Buy 1 share of any symbol as day or swing trade for pipeline verification
- **Phase 2 — Performance analytics**: Sharpe ratio, win rate, max drawdown, profit factor, expectancy, with 30d/90d/1Y lookback toggles
- **Phase 2 — Candlestick chart**: Interactive OHLCV chart with symbol selector, timeframe selector (1Min/5Min/1Hour/1Day), and RSI/ATR/VWAP readouts
- **Phase 2 — Market sentiment**: Color-coded grid showing bullish/neutral/bearish labels for all watchlist symbols with score bars
- **Phase 2 — Alerts panel**: Status badges showing which channels are enabled (email/webhook), Discord/Slack connectivity status, and a Test Alert button

**Polling intervals**:
- Account, status, positions, heartbeat: every 5 seconds (configurable via `DASHBOARD_POLL_INTERVAL_MS`)
- Bot details (uptime, jobs): every 10 seconds
- Analytics: every 120 seconds
- Candlestick chart: every 60 seconds
- Sentiment: every 300 seconds
- Alert config: once on page load

### API Endpoints (21 total)

All endpoints are async. Heavy operations (Alpaca API calls, database queries with enrichment) are wrapped in `asyncio.to_thread()` to prevent blocking the event loop.

**HTML**

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Render dashboard HTML page |

**Account & Status**

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/account` | Equity, cash, buying power, day/swing allocation tracking, daily P&L |
| GET | `/api/bot/status` | Running state, trading enabled, market open/closed, health status, next open/close times |
| GET | `/api/bot/details` | Uptime in seconds, start time, list of scheduled jobs with next fire times |

**Positions & Trade History**

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/positions?strategy=` | Open positions enriched with current prices and unrealized P&L from Alpaca |
| GET | `/api/trades?strategy=&limit=&offset=` | Trade history with pagination (default 50, max 500) |
| GET | `/api/heartbeat` | Latest bot activity log entries (max 200) |

**Charts**

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/equity-history` | Equity curve data points (date, equity, cash) |
| GET | `/api/pnl-daily` | Daily P&L broken down by day_pnl and swing_pnl |
| GET | `/api/chart/candlestick/{symbol}?timeframe=&limit=` | OHLCV candle data with RSI, ATR, VWAP, SMA overlays |

**Manual Controls**

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/close/{trade_id}` | Close a single position — handles pending orders (cancels) and filled positions (sells) |
| POST | `/api/close-all/{strategy_type}` | Close all open positions for "day" or "swing" |
| POST | `/api/bot/pause` | Pause the APScheduler (all jobs stop firing) |
| POST | `/api/bot/resume` | Resume the APScheduler |
| POST | `/api/bot/toggle-trading` | Toggle `ENABLE_TRADING` flag at runtime (no restart needed) |
| POST | `/api/test-trade` | Place a test paper trade — JSON body: `{"symbol": "AAPL", "strategy_type": "day"}` |

**Phase 2 — Analytics & Sentiment**

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/analytics?strategy=&lookback=` | Performance metrics: Sharpe, drawdown, win rate, profit factor, etc. |
| GET | `/api/analytics/monthly` | Monthly P&L summary aggregated by calendar month |
| GET | `/api/sentiment/{symbol}` | Sentiment score, label, and scored headlines for one symbol |
| GET | `/api/sentiment` | Batch sentiment for all watchlist symbols (combined set) |

**Phase 2 — Alerts**

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/alerts/config` | Current alert channel configuration and enabled status |
| POST | `/api/alerts/test` | Send a test alert to all enabled channels |

---

## Phase 2 Features

### Sentiment Analysis (`core/sentiment.py`)

Filters buy signals using news headline analysis from the Alpaca News API.

**How it works**:
1. Fetches up to 10 recent news articles per symbol via the Alpaca News SDK (falls back to REST API at `data.alpaca.markets/v1beta1/news` if the SDK import fails)
2. Scores each headline using keyword matching against configurable positive and negative word lists (defined in `config.py` as `SENTIMENT_KEYWORDS_POSITIVE` and `SENTIMENT_KEYWORDS_NEGATIVE`)
3. Calculates a normalized average score from -1.0 (fully bearish) to +1.0 (fully bullish)
4. Labels the result: bullish (score >= 0.2), bearish (score <= -0.2), neutral (in between)
5. Results are cached for 5 minutes per symbol in memory to avoid excessive API calls

**Signal adjustment rules** (applied in `adjust_signal_with_sentiment()`):
- Buy signal + bearish sentiment = signal downgraded to "hold" (trade blocked, reason annotated)
- Buy signal + bullish sentiment = signal confirmed (reason annotated with sentiment score)
- Sell and hold signals pass through unmodified

**Positive keywords**: beats, exceeds, upgrade, buy, bullish, growth, record, partnership, acquisition, profit, revenue beat, outperform, strong, surge, rally, breakout, momentum, upside

**Negative keywords**: misses, downgrade, sell, bearish, decline, loss, lawsuit, investigation, recall, warning, cut, layoff, restructuring, weak, plunge, crash, risk, overvalued, downside

### Performance Analytics (`core/analytics.py`)

Calculates comprehensive portfolio metrics from closed trade history.

**Metrics calculated**:
- **Win rate**: Percentage of trades with positive P&L
- **Profit factor**: Gross profit / gross loss (values > 1.0 indicate profitability)
- **Expectancy**: Average expected P&L per trade = (win% * avg win) + (loss% * avg loss)
- **Sharpe ratio**: Annualized risk-adjusted return using 252 trading days and 5% risk-free rate
- **Max drawdown**: Largest peak-to-trough decline in cumulative P&L (both $ amount and %)
- **Best/worst trade**: Individual trades with highest and lowest P&L
- **Consecutive streaks**: Longest winning streak and longest losing streak
- **Average holding hours**: Mean trade duration from entry to exit
- **Strategy breakdown**: Trade count, total P&L, win rate, and average P&L split by day vs swing
- **Status breakdown**: Count of trades grouped by exit reason (closed, stopped_out, eod_liquidated)
- **Monthly summary**: P&L aggregated by calendar month for each strategy, accessible via `/api/analytics/monthly`

Requires at least 5 closed trades before analytics are displayed (configurable via `MIN_TRADES_FOR_ANALYTICS`).

### Alert System (`core/alerts.py`)

Fire-and-forget notifications via two channels. All alert dispatching is wrapped in error suppression so a failed notification never crashes the trading bot.

**Delivery channels**:
1. **Email (SMTP)**: HTML-formatted messages with Catppuccin dark theme styling. Supports configurable SMTP server (Gmail, Outlook, etc.). Sends both plain text and HTML versions.
2. **Webhooks**: Discord (rich embeds with color coding — green for info, yellow for warning, red for error) and Slack (formatted text with emoji markers). Auto-detects Discord vs Slack based on the webhook URL pattern.

**Alert types**:
- `alert_trade_entry()`: Triggered on buy order execution — includes symbol, quantity, price, stop-loss, take-profit, strategy
- `alert_trade_exit()`: Triggered on sell order execution — includes P&L and exit reason
- `alert_stop_loss()`: Triggered when a stop-loss price is breached — includes hit price and stop level
- `alert_daily_report()`: Triggered at 4:05 PM ET — includes equity, day/swing P&L, open position counts
- `alert_error()`: Triggered after 5+ consecutive health check failures — includes failure count

**Configuration**: Set `ENABLE_WEBHOOK_ALERTS=true` and `WEBHOOK_URL=https://discord.com/api/webhooks/...` in `.env`. Each alert type can be individually toggled via `ALERT_ON_*` flags in config.py.

### Advanced Charting

The `/api/chart/candlestick/{symbol}` endpoint returns OHLCV candle data enriched with indicator overlays:
- VWAP (volume-weighted average price)
- RSI (14-period relative strength index)
- ATR (14-period average true range)
- SMA 50 and SMA 200 (simple moving averages, enabled for daily timeframe)
- Volume with 20-period average

Supports timeframes: 1Min, 5Min, 1Hour, 1Day. Configurable bar count (10–500, default 60).

### Public URL Access (ngrok)

The bot can expose the dashboard via a public HTTPS URL for mobile access using the `ngrok` Python library (not the CLI).

**Setup**:
1. `pip install ngrok`
2. Sign up at ngrok.com and get your authtoken
3. Add to `.env`: `NGROK_ENABLED=true` and `NGROK_AUTHTOKEN=your_token`
4. On startup, the public URL is printed in the console log with a prominent box banner

---

## Database Schema

SQLite database (`trading_bot.db`) with three tables, managed via SQLAlchemy ORM. Auto-created on first startup via `init_db()`. Thread-safe configuration with `check_same_thread=False`.

### `trades` Table

| Column | Type | Description |
|--------|------|-------------|
| `id` | Integer PK | Auto-incrementing trade ID |
| `symbol` | String (indexed) | Ticker symbol (e.g., AAPL) |
| `strategy_type` | String (indexed) | "day" or "swing" — determines EOD liquidation behavior |
| `side` | String | "buy" or "sell" |
| `quantity` | Integer | Number of shares |
| `entry_price` | Float | Price at entry |
| `exit_price` | Float (nullable) | Price at exit (null while open) |
| `stop_loss` | Float | Current stop-loss price level |
| `take_profit` | Float (nullable) | Take-profit target (null for swing trades) |
| `status` | String | "open", "closed", "stopped_out", "eod_liquidated" |
| `pnl` | Float (nullable) | Realized profit/loss in dollars (null while open) |
| `alpaca_order_id` | String (nullable) | Alpaca order UUID for status tracking and cancellation |
| `entry_time` | DateTime | UTC timestamp of trade entry |
| `exit_time` | DateTime (nullable) | UTC timestamp of trade exit (null while open) |
| `notes` | String (nullable) | Signal reason, exit reason, sentiment annotations |

Composite index on `(strategy_type, status)` for fast EOD liquidation queries.

### `heartbeat_log` Table

| Column | Type | Description |
|--------|------|-------------|
| `id` | Integer PK | Auto-incrementing log ID |
| `timestamp` | DateTime (indexed) | UTC timestamp of the log entry |
| `message` | String | Human-readable log message |
| `level` | String | "info", "warning", "error" |

### `equity_history` Table

| Column | Type | Description |
|--------|------|-------------|
| `id` | Integer PK | Auto-incrementing snapshot ID |
| `date` | Date (unique, indexed) | Calendar date (one row per trading day) |
| `equity` | Float | Total account equity at end of day |
| `cash` | Float | Available cash at end of day |
| `day_pnl` | Float | Realized day trade P&L for this date |
| `swing_pnl` | Float | Realized swing trade P&L for this date |
| `open_day_positions` | Integer | Count of open day positions at snapshot time |
| `open_swing_positions` | Integer | Count of open swing positions at snapshot time |

---

## Performance Optimizations

### Singleton Alpaca Clients (`core/alpaca_client.py`)

Instead of creating new `TradingClient` and `StockHistoricalDataClient` instances on every API call (which was the original bottleneck causing 30-second dashboard loads), the module uses thread-safe singletons with double-checked locking:

- One `TradingClient` and one `StockHistoricalDataClient` per process lifetime
- Initialization guarded by `threading.Lock()` for thread safety
- Eliminates connection setup overhead on every poll cycle

### Response Caching with TTL

Frequently accessed API responses are cached in memory with configurable time-to-live:

| Cache Key | TTL | Purpose |
|-----------|-----|---------|
| `account` | 10s | Account equity, cash, buying power |
| `clock` | 30s | Market open/close status and times |
| `positions` | 10s | All open Alpaca positions |
| `latest_price:{symbol}` | 15s | Per-symbol latest trade price |

**Cache invalidation**: Mutating operations (`submit_market_order`, `submit_limit_order`, `cancel_order`, `close_position`) automatically invalidate the `account` and `positions` caches so the next dashboard poll fetches fresh data.

### Non-Blocking API Endpoints

All dashboard endpoints that call the Alpaca API or perform enriched database queries are wrapped in `asyncio.to_thread()`. This offloads synchronous blocking calls to a thread pool, allowing Uvicorn's single-threaded event loop to serve multiple concurrent dashboard requests without stalling.

**Wrapped endpoints**: `/api/account`, `/api/bot/status`, `/api/positions`, `/api/close/{id}`, `/api/close-all/{strategy}`, `/api/test-trade`, `/api/analytics`, `/api/analytics/monthly`, `/api/sentiment`, `/api/sentiment/{symbol}`, `/api/chart/candlestick/{symbol}`, `/api/alerts/test`.

### Fast Price Lookups

`get_latest_price()` tries daily bars first (single API call, fast response even outside market hours) before falling back to 1-minute bars. This avoids the common failure mode where intraday data is unavailable during non-market hours.

### Data Ingestion Caching

`data_ingestion.py` maintains an in-memory cache for fetched bar data with a 5-minute TTL per symbol/timeframe combination. This prevents redundant API calls when multiple scheduler jobs request the same data within a short window (e.g., day_trade_scan and stop_loss_monitor both needing AAPL data).

### Rate Limit Handling

All Alpaca API calls go through `_retry_on_rate_limit()` which implements exponential backoff (1s, 2s, 4s delays) on HTTP 429 responses, up to 3 retries before raising.

---

## Extending the Bot

### Adding a New Strategy

1. Create a new signal file in `core/signals/` (e.g., `breakout_trade.py`) with functions:
   - `generate_signal(symbol, ...)` returning a dict with keys: `symbol`, `signal` ("buy"/"sell"/"hold"), `reason`, `entry_price`, `atr`
   - `generate_signals_batch(symbols, ...)` for batch processing
2. Add a new watchlist and strategy-specific parameters in `config.py`
3. Register a new scheduled job in `main.py` (follow the pattern of existing jobs)
4. The existing `order_executor.py`, `risk_manager.py`, and `alerts.py` all work with any `strategy_type` string — no changes needed

### Adding a New Alert Channel

1. Add a `_send_*_safe()` + `_send_*()` function pair in `core/alerts.py` (follow the email/webhook pattern)
2. Add configuration variables in `config.py` and `.env`
3. Add a dispatch call in the `send_alert()` function

### Adding a New Dashboard Section

1. Add a new API endpoint in `dashboard/routes.py` — wrap heavy operations in `asyncio.to_thread()`
2. Add the HTML section in `dashboard/templates/index.html`
3. Add a JavaScript fetch function and register it in the polling interval setup at the bottom of the template

### Adding New Technical Indicators

1. Add the calculation function in `core/data_ingestion.py` (follow the pattern of `_calc_rsi()` or `_calc_atr()`)
2. Call it from `compute_indicators()` to add the new column to the DataFrame
3. Reference the new column in your signal engine's entry/exit conditions

---

## Dependencies

```
alpaca-py>=0.21.0       # Alpaca Trading & Data API client (SDK)
pandas>=2.0.0           # DataFrame operations for bar data & indicators
numpy>=1.24.0           # Numerical computing (pandas dependency)
fastapi>=0.110.0        # Async web framework for dashboard API
uvicorn>=0.29.0         # ASGI server for running FastAPI
sqlalchemy>=2.0.0       # ORM for SQLite database
python-dotenv>=1.0.0    # Load .env configuration files
apscheduler>=3.10.0     # Background job scheduler (cron + interval)
jinja2>=3.1.0           # HTML template engine for dashboard
httpx>=0.27.0           # HTTP client for webhook delivery & news API fallback
aiohttp>=3.9.0          # Async HTTP (used by some Alpaca SDK internals)
```

**Optional**: `ngrok` (pip package) for public URL tunneling to access dashboard remotely.

---

## Key Design Decisions

1. **Strategy type tagging**: The `strategy_type` field on every trade record is the single source of truth that separates day and swing positions throughout the entire system. This prevents EOD liquidation from accidentally closing swing trades.

2. **Fire-and-forget alerts**: All alert dispatching suppresses errors internally. A failed Discord webhook or SMTP timeout should never crash the trading bot or interrupt order execution.

3. **Monitor-only mode**: `ENABLE_TRADING = False` lets you observe all signal generation, risk checks, and logging without placing actual paper orders. Every path that would submit an order checks this flag first.

4. **Singleton clients + response caching**: The biggest performance optimization. The original implementation created new Alpaca HTTP client instances on every API call (dozens per dashboard poll cycle), causing 30+ second load times. Singleton clients with TTL-based caching reduced this to 1–2 seconds.

5. **Thread offloading**: `asyncio.to_thread()` on all heavy dashboard endpoints ensures the single-threaded event loop remains responsive. Without this, a slow Alpaca API call would block all other dashboard requests.

6. **Context manager for DB**: `get_db()` guarantees auto-commit on success and auto-rollback on exception, preventing orphaned database sessions or partial writes.

7. **Retry with backoff**: All Alpaca API calls go through exponential backoff retry logic for rate limits (HTTP 429), making the bot resilient to temporary API throttling.

8. **Closing pending orders**: The manual close button (`close_trade_by_id`) checks if the Alpaca order is still in a pending state (new, accepted, pending_new) and cancels it instead of trying to submit a sell order against a non-existent position. This handles the common case of closing test trades placed outside market hours.

---

## Disclaimer

This bot uses paper trading only. It is not financial advice. Use at your own risk.

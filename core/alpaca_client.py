"""
Alpaca Paper Trading API wrapper.
Handles connection, account info, market clock, bar data, and order management.
All API calls are wrapped in try/except with retry logic for rate limits.

Performance optimizations:
    - Singleton client instances (reused across all calls)
    - Response caching with TTL for account, clock, positions
    - get_latest_price uses cached daily bars as fast fallback
"""

import logging
import time
import threading
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

import pandas as pd

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest, LimitOrderRequest, StopOrderRequest,
    GetOrdersRequest
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL, ALPACA_DATA_FEED

logger = logging.getLogger(__name__)

# Maximum retries for rate-limited requests
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1  # seconds

# ─── Singleton Clients ───────────────────────────
_trading_client: Optional[TradingClient] = None
_data_client: Optional[StockHistoricalDataClient] = None
_client_lock = threading.Lock()

# ─── Response Cache ──────────────────────────────
_cache: Dict[str, tuple] = {}  # key -> (timestamp, data)
_cache_lock = threading.Lock()

CACHE_TTL = {
    "account": 10,             # 10 seconds — account data refreshes every poll
    "clock": 30,               # 30 seconds — market clock rarely changes
    "positions": 10,           # 10 seconds — positions refresh every poll
    "latest_price": 5,         # 5 seconds — price data (must be fresh for stop-loss checks)
    "tradeable_assets": 3600,  # 1 hour — asset list changes rarely
    "top_movers": 300,         # 5 minutes — screener data
    "most_actives": 300,       # 5 minutes — screener data
}


def _get_cached(key: str) -> Optional[Any]:
    """Get cached data if still valid."""
    with _cache_lock:
        if key in _cache:
            ts, data = _cache[key]
            # Determine TTL from key prefix
            prefix = key.split(":")[0]
            ttl = CACHE_TTL.get(prefix, 10)
            if (time.time() - ts) < ttl:
                return data
    return None


def _set_cache(key: str, data: Any):
    """Store data in cache."""
    with _cache_lock:
        _cache[key] = (time.time(), data)


def _is_configured() -> bool:
    """Check if Alpaca API keys are configured."""
    return bool(ALPACA_API_KEY and ALPACA_SECRET_KEY)


def _get_trading_client() -> Optional[TradingClient]:
    """Get or create the singleton TradingClient."""
    global _trading_client
    if not _is_configured():
        logger.warning("Alpaca API keys not configured.")
        return None
    if _trading_client is None:
        with _client_lock:
            if _trading_client is None:
                paper = "paper" in ALPACA_BASE_URL
                _trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=paper)
                logger.debug("TradingClient singleton created")
    return _trading_client


def _get_data_client() -> Optional[StockHistoricalDataClient]:
    """Get or create the singleton StockHistoricalDataClient."""
    global _data_client
    if not _is_configured():
        logger.warning("Alpaca API keys not configured.")
        return None
    if _data_client is None:
        with _client_lock:
            if _data_client is None:
                _data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
                logger.debug("StockHistoricalDataClient singleton created")
    return _data_client


def _retry_on_rate_limit(func, *args, **kwargs):
    """Execute a function with exponential backoff on rate limits (HTTP 429)."""
    for attempt in range(MAX_RETRIES):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            error_str = str(e)
            if "429" in error_str or "rate limit" in error_str.lower():
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(f"Rate limited. Retrying in {delay}s (attempt {attempt + 1}/{MAX_RETRIES})")
                time.sleep(delay)
            else:
                raise
    raise Exception(f"Max retries ({MAX_RETRIES}) exceeded for rate-limited request.")


def get_account() -> Optional[Dict[str, Any]]:
    """Fetch account information from Alpaca. Cached for 10s."""
    cached = _get_cached("account")
    if cached is not None:
        return cached

    client = _get_trading_client()
    if not client:
        return None
    try:
        account = _retry_on_rate_limit(client.get_account)
        result = {
            "equity": float(account.equity),
            "cash": float(account.cash),
            "buying_power": float(account.buying_power),
            "daytrading_buying_power": float(account.daytrading_buying_power),
            "regt_buying_power": float(account.regt_buying_power),
            "status": account.status.value if hasattr(account.status, 'value') else str(account.status),
            "currency": account.currency,
            "last_equity": float(account.last_equity),
        }
        _set_cache("account", result)
        return result
    except Exception as e:
        logger.error(f"Failed to get account: {e}")
        return None


def get_market_clock() -> Optional[Dict[str, Any]]:
    """Fetch market clock from Alpaca. Cached for 30s."""
    cached = _get_cached("clock")
    if cached is not None:
        return cached

    client = _get_trading_client()
    if not client:
        return None
    try:
        clock = _retry_on_rate_limit(client.get_clock)
        result = {
            "is_open": clock.is_open,
            "next_open": clock.next_open.isoformat() if clock.next_open else None,
            "next_close": clock.next_close.isoformat() if clock.next_close else None,
            "timestamp": clock.timestamp.isoformat() if clock.timestamp else None,
        }
        _set_cache("clock", result)
        return result
    except Exception as e:
        logger.error(f"Failed to get market clock: {e}")
        return None


def get_bars(symbol: str, timeframe: str = "5Min", limit: int = 100) -> Optional[pd.DataFrame]:
    """
    Fetch historical bars for a symbol.

    Args:
        symbol: Stock ticker symbol
        timeframe: '1Min', '5Min', '1Hour', '1Day'
        limit: Number of bars to fetch

    Returns:
        DataFrame with OHLCV columns or None on failure
    """
    client = _get_data_client()
    if not client:
        return None

    tf_map = {
        "1Min": TimeFrame.Minute,
        "5Min": TimeFrame(5, TimeFrameUnit.Minute),
        "1Hour": TimeFrame.Hour,
        "1Day": TimeFrame.Day,
    }

    tf = tf_map.get(timeframe, TimeFrame(5, TimeFrameUnit.Minute))

    # Calculate start date based on timeframe and limit
    if timeframe == "1Day":
        start = datetime.now() - timedelta(days=int(limit * 1.5) + 10)
    elif timeframe == "1Hour":
        start = datetime.now() - timedelta(hours=limit * 2)
    else:
        start = datetime.now() - timedelta(days=max(limit // 78 + 2, 7))

    try:
        # Build request with explicit data feed to avoid IEX vs SIP mismatches
        request_kwargs = dict(
            symbol_or_symbols=symbol,
            timeframe=tf,
            start=start,
            limit=limit,
        )
        # Add feed parameter if configured (requires alpaca-py >= 0.8)
        try:
            from alpaca.data.enums import DataFeed
            feed_map = {"sip": DataFeed.SIP, "iex": DataFeed.IEX}
            if ALPACA_DATA_FEED.lower() in feed_map:
                request_kwargs["feed"] = feed_map[ALPACA_DATA_FEED.lower()]
        except (ImportError, AttributeError):
            # Older alpaca-py version — feed param not available
            logger.debug("DataFeed enum not available — using default feed")

        request = StockBarsRequest(**request_kwargs)
        bars = _retry_on_rate_limit(client.get_stock_bars, request)

        # Handle different alpaca-py result formats
        if not bars:
            logger.warning(f"No bar data returned for {symbol}")
            return None

        # Try to get DataFrame — format varies by alpaca-py version
        df = None
        try:
            if hasattr(bars, 'df'):
                df = bars.df
            elif symbol in bars:
                bar_data = bars[symbol]
                if hasattr(bar_data, 'df'):
                    df = bar_data.df
                elif isinstance(bar_data, list):
                    records = []
                    for b in bar_data:
                        rec = {
                            "timestamp": b.timestamp,
                            "open": float(b.open),
                            "high": float(b.high),
                            "low": float(b.low),
                            "close": float(b.close),
                            "volume": float(b.volume),
                            "vwap": float(b.vwap) if hasattr(b, 'vwap') and b.vwap else None,
                        }
                        records.append(rec)
                    df = pd.DataFrame(records)
            elif hasattr(bars, 'data') and symbol in bars.data:
                bar_data = bars.data[symbol]
                if isinstance(bar_data, list):
                    records = []
                    for b in bar_data:
                        rec = {
                            "timestamp": b.timestamp,
                            "open": float(b.open),
                            "high": float(b.high),
                            "low": float(b.low),
                            "close": float(b.close),
                            "volume": float(b.volume),
                            "vwap": float(b.vwap) if hasattr(b, 'vwap') and b.vwap else None,
                        }
                        records.append(rec)
                    df = pd.DataFrame(records)
        except Exception as ex:
            logger.error(f"Error parsing bar data for {symbol}: {ex}")
            return None

        if df is None or df.empty:
            logger.warning(f"No bar data returned for {symbol}")
            return None

        # Handle MultiIndex from .df property
        if isinstance(df.index, pd.MultiIndex):
            df = df.droplevel(0)

        df = df.reset_index()

        # Normalize column names to lowercase
        df.columns = [c.lower() for c in df.columns]

        return df.tail(limit)

    except Exception as e:
        logger.error(f"Failed to fetch bars for {symbol} ({timeframe}): {e}")
        return None


def get_positions() -> List[Dict[str, Any]]:
    """Fetch all open positions from Alpaca. Cached for 10s."""
    cached = _get_cached("positions")
    if cached is not None:
        return cached

    client = _get_trading_client()
    if not client:
        return []
    try:
        positions = _retry_on_rate_limit(client.get_all_positions)
        result = [
            {
                "symbol": p.symbol,
                "qty": int(p.qty),
                "side": p.side.value if hasattr(p.side, 'value') else str(p.side),
                "avg_entry_price": float(p.avg_entry_price),
                "current_price": float(p.current_price),
                "unrealized_pl": float(p.unrealized_pl),
                "unrealized_plpc": float(p.unrealized_plpc),
                "market_value": float(p.market_value),
            }
            for p in positions
        ]
        _set_cache("positions", result)
        return result
    except Exception as e:
        logger.error(f"Failed to get positions: {e}")
        return []


def submit_market_order(symbol: str, qty: int, side: str, time_in_force: str = "day") -> Optional[str]:
    """
    Submit a market order.

    Returns:
        Alpaca order ID on success, None on failure
    """
    client = _get_trading_client()
    if not client:
        return None
    try:
        tif = TimeInForce.DAY if time_in_force == "day" else TimeInForce.GTC
        order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        request = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=order_side,
            time_in_force=tif,
        )
        order = _retry_on_rate_limit(client.submit_order, request)
        order_id = str(order.id)
        logger.info(f"Market order submitted: {side} {qty} {symbol} — Order ID: {order_id}")
        # Invalidate caches that are affected by order
        _invalidate("account")
        _invalidate("positions")
        return order_id
    except Exception as e:
        logger.error(f"Failed to submit market order for {symbol}: {e}")
        return None


def submit_limit_order(symbol: str, qty: int, side: str, limit_price: float,
                       time_in_force: str = "day") -> Optional[str]:
    """
    Submit a limit order.

    Returns:
        Alpaca order ID on success, None on failure
    """
    client = _get_trading_client()
    if not client:
        return None
    try:
        tif = TimeInForce.DAY if time_in_force == "day" else TimeInForce.GTC
        order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        request = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=order_side,
            time_in_force=tif,
            limit_price=round(limit_price, 2),
        )
        order = _retry_on_rate_limit(client.submit_order, request)
        order_id = str(order.id)
        logger.info(f"Limit order submitted: {side} {qty} {symbol} @ ${limit_price:.2f} — Order ID: {order_id}")
        _invalidate("account")
        _invalidate("positions")
        return order_id
    except Exception as e:
        logger.error(f"Failed to submit limit order for {symbol}: {e}")
        return None


def cancel_order(order_id: str) -> bool:
    """Cancel an order by its Alpaca order ID."""
    client = _get_trading_client()
    if not client:
        return False
    try:
        _retry_on_rate_limit(client.cancel_order_by_id, order_id)
        logger.info(f"Order canceled: {order_id}")
        _invalidate("account")
        _invalidate("positions")
        return True
    except Exception as e:
        logger.error(f"Failed to cancel order {order_id}: {e}")
        return False


def close_position(symbol: str) -> bool:
    """Close an entire position for a symbol."""
    client = _get_trading_client()
    if not client:
        return False
    try:
        _retry_on_rate_limit(client.close_position, symbol)
        logger.info(f"Position closed: {symbol}")
        _invalidate("account")
        _invalidate("positions")
        return True
    except Exception as e:
        logger.error(f"Failed to close position {symbol}: {e}")
        return False


def get_order_status(order_id: str) -> Optional[str]:
    """Get the status of an order by ID."""
    client = _get_trading_client()
    if not client:
        return None
    try:
        order = _retry_on_rate_limit(client.get_order_by_id, order_id)
        status = order.status.value if hasattr(order.status, 'value') else str(order.status)
        return status
    except Exception as e:
        logger.error(f"Failed to get order status for {order_id}: {e}")
        return None


def get_order_details(order_id: str) -> Optional[Dict[str, Any]]:
    """
    Get full order details including fill price and filled quantity.
    Used to confirm order execution before recording trades.

    Returns:
        Dict with status, filled_qty, filled_avg_price, symbol, side, etc.
        None on failure.
    """
    client = _get_trading_client()
    if not client:
        return None
    try:
        order = _retry_on_rate_limit(client.get_order_by_id, order_id)
        return {
            "id": str(order.id),
            "symbol": order.symbol,
            "side": order.side.value if hasattr(order.side, 'value') else str(order.side),
            "status": order.status.value if hasattr(order.status, 'value') else str(order.status),
            "qty": int(order.qty) if order.qty else 0,
            "filled_qty": int(order.filled_qty) if order.filled_qty else 0,
            "filled_avg_price": float(order.filled_avg_price) if order.filled_avg_price else None,
            "submitted_at": order.submitted_at.isoformat() if order.submitted_at else None,
            "filled_at": order.filled_at.isoformat() if order.filled_at else None,
        }
    except Exception as e:
        logger.error(f"Failed to get order details for {order_id}: {e}")
        return None


def get_open_orders() -> List[Dict[str, Any]]:
    """Fetch all open orders from Alpaca."""
    client = _get_trading_client()
    if not client:
        return []
    try:
        request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        orders = _retry_on_rate_limit(client.get_orders, request)
        return [
            {
                "id": str(o.id),
                "symbol": o.symbol,
                "side": o.side.value if hasattr(o.side, 'value') else str(o.side),
                "qty": str(o.qty),
                "type": o.type.value if hasattr(o.type, 'value') else str(o.type),
                "status": o.status.value if hasattr(o.status, 'value') else str(o.status),
                "limit_price": str(o.limit_price) if o.limit_price else None,
            }
            for o in orders
        ]
    except Exception as e:
        logger.error(f"Failed to get open orders: {e}")
        return []


def get_latest_price(symbol: str) -> Optional[float]:
    """
    Get the latest trade price for a symbol.

    IMPORTANT: During market hours, we MUST use intraday bars (1Min) first.
    Daily bars only contain the *previous day's* close, which is stale and will
    cause stop-loss/take-profit triggers on completely wrong prices.

    All data is sourced from the configured ALPACA_DATA_FEED (SIP by default),
    ensuring consistency across signals, stop-loss checks, and order execution.

    Priority order:
      1. Alpaca latest trade API (real-time on paid plan, may be delayed on free)
      2. 1-minute bars (near real-time)
      3. 5-minute bars (fallback)
      4. Daily bars (last resort — only valid outside market hours)
    """
    cache_key = f"latest_price:{symbol}"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    # Priority 1: Try Alpaca latest trade API
    try:
        client = _get_data_client()
        if client:
            from alpaca.data.requests import StockLatestTradeRequest
            request_kwargs = {"symbol_or_symbols": symbol}
            try:
                from alpaca.data.enums import DataFeed
                feed_map = {"sip": DataFeed.SIP, "iex": DataFeed.IEX}
                if ALPACA_DATA_FEED.lower() in feed_map:
                    request_kwargs["feed"] = feed_map[ALPACA_DATA_FEED.lower()]
            except (ImportError, AttributeError):
                pass
            request = StockLatestTradeRequest(**request_kwargs)
            trades = _retry_on_rate_limit(client.get_stock_latest_trade, request)
            if trades:
                # Handle both dict and direct response formats
                trade = trades.get(symbol, trades) if isinstance(trades, dict) else trades
                if hasattr(trade, 'price') and trade.price:
                    price = float(trade.price)
                    _set_cache(cache_key, price)
                    return price
    except Exception as e:
        logger.debug(f"Latest trade API failed for {symbol}: {e}")

    # Priority 2: 1-minute bars
    df = get_bars(symbol, timeframe="1Min", limit=5)
    if df is not None and not df.empty:
        price = float(df.iloc[-1]["close"])
        _set_cache(cache_key, price)
        return price

    # Priority 3: 5-minute bars
    df = get_bars(symbol, timeframe="5Min", limit=5)
    if df is not None and not df.empty:
        price = float(df.iloc[-1]["close"])
        _set_cache(cache_key, price)
        return price

    # Priority 4: Daily bars (last resort — stale during market hours)
    df = get_bars(symbol, timeframe="1Day", limit=2)
    if df is not None and not df.empty:
        price = float(df.iloc[-1]["close"])
        logger.warning(
            f"Using daily bar close for {symbol} (${price:.2f}) — "
            f"intraday data unavailable. Price may be stale!"
        )
        _set_cache(cache_key, price)
        return price

    return None


def get_multi_symbol_bars(symbols: List[str], timeframe: str = "5Min",
                          limit: int = 100, batch_size: int = 100) -> Dict[str, pd.DataFrame]:
    """
    Fetch bar data for many symbols at once using multi-symbol requests.
    Returns dict of symbol -> DataFrame with OHLCV columns.

    Uses batches to avoid slow/unreliable API calls.
    """
    client = _get_data_client()
    if not client:
        return {}

    tf_map = {
        "1Min": TimeFrame.Minute,
        "5Min": TimeFrame(5, TimeFrameUnit.Minute),
        "1Hour": TimeFrame.Hour,
        "1Day": TimeFrame.Day,
    }
    tf = tf_map.get(timeframe, TimeFrame(5, TimeFrameUnit.Minute))

    # Calculate start date based on timeframe and limit
    if timeframe == "1Day":
        start = datetime.now() - timedelta(days=int(limit * 1.5) + 10)
    elif timeframe == "1Hour":
        start = datetime.now() - timedelta(hours=limit * 2)
    else:
        start = datetime.now() - timedelta(days=max(limit // 78 + 2, 7))

    result = {}
    total_batches = (len(symbols) + batch_size - 1) // batch_size
    failed_batches = 0

    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        batch_num = i // batch_size + 1
        try:
            request_kwargs = dict(
                symbol_or_symbols=batch,
                timeframe=tf,
                start=start,
                limit=limit * len(batch),  # limit is per-request total, so scale by batch size
            )
            try:
                from alpaca.data.enums import DataFeed
                feed_map = {"sip": DataFeed.SIP, "iex": DataFeed.IEX}
                if ALPACA_DATA_FEED.lower() in feed_map:
                    request_kwargs["feed"] = feed_map[ALPACA_DATA_FEED.lower()]
            except (ImportError, AttributeError):
                pass
            request = StockBarsRequest(**request_kwargs)
            bars = _retry_on_rate_limit(client.get_stock_bars, request)
            if not bars:
                failed_batches += 1
                continue

            # Parse into per-symbol DataFrames
            # Try .df first (returns MultiIndex DataFrame)
            df = None
            try:
                if hasattr(bars, 'df') and not bars.df.empty:
                    df = bars.df
            except Exception:
                pass

            if df is not None and isinstance(df.index, pd.MultiIndex):
                for sym in df.index.get_level_values(0).unique():
                    sym_str = str(sym)
                    sym_df = df.loc[sym].copy().reset_index()
                    sym_df.columns = [c.lower() for c in sym_df.columns]
                    if len(sym_df) >= 1:
                        result[sym_str] = sym_df.tail(limit)
            else:
                # Fallback: try .data dict
                bar_dict = getattr(bars, 'data', None) or {}
                for sym, bar_list in bar_dict.items():
                    sym_str = str(sym)
                    if isinstance(bar_list, list) and len(bar_list) >= 1:
                        records = []
                        for b in bar_list:
                            records.append({
                                "timestamp": b.timestamp,
                                "open": float(b.open),
                                "high": float(b.high),
                                "low": float(b.low),
                                "close": float(b.close),
                                "volume": float(b.volume),
                                "vwap": float(b.vwap) if hasattr(b, 'vwap') and b.vwap else None,
                            })
                        sym_df = pd.DataFrame(records)
                        result[sym_str] = sym_df.tail(limit)

        except Exception as e:
            failed_batches += 1
            logger.warning(f"Multi-bar batch {batch_num}/{total_batches} failed: {e}")
            continue

        # Brief pause between batches for rate limiting
        if i + batch_size < len(symbols):
            import time as _time
            _time.sleep(0.3)

    logger.info(f"Multi-symbol bars ({timeframe}): got data for {len(result)}/{len(symbols)} symbols "
                f"({failed_batches}/{total_batches} batches failed)")
    return result


def get_multi_symbol_daily_bars(symbols: List[str], limit: int = 20) -> Dict[str, Dict[str, float]]:
    """
    Fetch daily bars for many symbols at once using multi-symbol requests.
    Returns dict of symbol -> {"close": latest_close, "avg_volume": avg_volume}
    for symbols that have enough data.

    Uses batches of up to 1000 symbols per request (Alpaca API limit).
    """
    client = _get_data_client()
    if not client:
        return {}

    # ~35 calendar days covers ~20 trading days of daily bars
    start = datetime.now() - timedelta(days=35)
    result = {}
    batch_size = 100  # Small batches for API reliability

    total_batches = (len(symbols) + batch_size - 1) // batch_size
    failed_batches = 0

    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        batch_num = i // batch_size + 1
        try:
            # Note: do NOT pass limit here — it caps total bars across ALL symbols
            # in the batch, not per-symbol. Use the date range to control history depth.
            request_kwargs = dict(
                symbol_or_symbols=batch,
                timeframe=TimeFrame.Day,
                start=start,
            )
            try:
                from alpaca.data.enums import DataFeed
                feed_map = {"sip": DataFeed.SIP, "iex": DataFeed.IEX}
                if ALPACA_DATA_FEED.lower() in feed_map:
                    request_kwargs["feed"] = feed_map[ALPACA_DATA_FEED.lower()]
            except (ImportError, AttributeError):
                pass
            request = StockBarsRequest(**request_kwargs)
            bars = _retry_on_rate_limit(client.get_stock_bars, request)
            if not bars:
                failed_batches += 1
                continue

            # Parse multi-symbol response — try .data dict first, then .df
            parsed = 0
            bar_dict = getattr(bars, 'data', None) or {}

            if bar_dict:
                for sym, bar_list in bar_dict.items():
                    sym_str = str(sym)
                    if isinstance(bar_list, list) and len(bar_list) >= 5:
                        closes = [float(b.close) for b in bar_list]
                        volumes = [float(b.volume) for b in bar_list]
                        result[sym_str] = {
                            "close": closes[-1],
                            "avg_volume": sum(volumes) / len(volumes),
                        }
                        parsed += 1
                    elif hasattr(bar_list, 'df'):
                        sym_df = bar_list.df
                        if len(sym_df) >= 5:
                            result[sym_str] = {
                                "close": float(sym_df.iloc[-1]["close"]),
                                "avg_volume": float(sym_df["volume"].mean()),
                            }
                            parsed += 1
            elif hasattr(bars, 'df'):
                df = bars.df
                if not df.empty and isinstance(df.index, pd.MultiIndex):
                    for sym in df.index.get_level_values(0).unique():
                        sym_df = df.loc[sym]
                        if len(sym_df) >= 5:
                            result[sym] = {
                                "close": float(sym_df.iloc[-1]["close"]),
                                "avg_volume": float(sym_df["volume"].mean()),
                            }
                            parsed += 1

            if parsed == 0 and len(batch) > 0:
                logger.debug(f"Batch {batch_num}: 0 symbols parsed from {len(batch)} requested")

        except Exception as e:
            failed_batches += 1
            logger.warning(f"Batch {batch_num}/{total_batches} bar fetch failed: {e}")
            continue

        # Brief pause between batches for rate limiting
        if i + batch_size < len(symbols):
            import time as _time
            _time.sleep(0.3)

    logger.info(f"Multi-symbol bars: got data for {len(result)}/{len(symbols)} symbols "
                f"({failed_batches}/{total_batches} batches failed)")
    return result


def get_tradeable_assets(min_price: float = 5.0, max_price: float = 10000.0,
                         exchange_filter: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """
    Fetch all tradeable US equity assets from Alpaca.
    Cached for 1 hour (refreshed during pre-market setup).

    Args:
        min_price: Minimum last known price (filters penny stocks)
        max_price: Maximum price
        exchange_filter: List of exchanges to include (e.g., ['NASDAQ', 'NYSE', 'ARCA']).
                        If None, defaults to major US exchanges.

    Returns:
        List of asset dicts with symbol, name, exchange, etc.
    """
    cache_key = "tradeable_assets"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    client = _get_trading_client()
    if not client:
        return []

    if exchange_filter is None:
        exchange_filter = ["NASDAQ", "NYSE", "ARCA", "AMEX", "BATS"]

    try:
        from alpaca.trading.requests import GetAssetsRequest
        from alpaca.trading.enums import AssetClass, AssetStatus

        request = GetAssetsRequest(
            asset_class=AssetClass.US_EQUITY,
            status=AssetStatus.ACTIVE,
        )
        assets = _retry_on_rate_limit(client.get_all_assets, request)

        result = []
        # Log a sample to debug exchange format
        if assets:
            sample = assets[0]
            logger.debug(f"Sample asset: symbol={sample.symbol}, exchange={sample.exchange!r}, "
                         f"type={type(sample.exchange).__name__}, str={str(sample.exchange)}")

        for a in assets:
            # Filter: must be tradeable, not OTC, fractionable or standard
            raw_exchange = a.exchange
            # Handle both enum (.value) and plain string formats
            exchange = raw_exchange.value if hasattr(raw_exchange, 'value') else str(raw_exchange or "")
            exchange = exchange.upper()
            if not a.tradable:
                continue
            if exchange not in exchange_filter:
                continue
            if not a.shortable and not a.easy_to_borrow:
                # Skip very illiquid assets that can't even be shorted
                pass  # Still include — we only buy

            result.append({
                "symbol": a.symbol,
                "name": a.name or "",
                "exchange": exchange,
                "fractionable": getattr(a, 'fractionable', False),
            })

        logger.info(f"Fetched {len(result)} tradeable assets from Alpaca "
                     f"(filtered from {len(assets)} total)")

        # Cache for 1 hour
        with _cache_lock:
            _cache[cache_key] = (time.time(), result)

        return result
    except Exception as e:
        logger.error(f"Failed to fetch tradeable assets: {e}")
        return []


def get_top_movers(top: int = 100) -> List[Dict[str, Any]]:
    """
    Fetch top stock movers (biggest % change) from the Alpaca screener API.
    Returns list of dicts with symbol, percent_change, price, volume, etc.
    Cached for 5 minutes.
    """
    cache_key = f"top_movers:{top}"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    if not _is_configured():
        return []

    try:
        import requests as req

        url = "https://data.alpaca.markets/v1beta1/screener/stocks/movers"
        headers = {
            "APCA-API-KEY-ID": ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        }
        # Alpaca movers API has an undocumented max for 'top' param.
        # Cap at 50 to avoid 400 Bad Request errors.
        safe_top = min(top, 50)
        params = {"top": safe_top}

        resp = req.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        result = []
        # Response contains "gainers" and "losers" lists
        for mover in data.get("gainers", []):
            result.append({
                "symbol": mover.get("symbol", ""),
                "percent_change": float(mover.get("percent_change", 0)),
                "price": float(mover.get("price", 0)),
                "change": float(mover.get("change", 0)),
                "volume": int(mover.get("volume", 0)),
                "type": "gainer",
            })
        for mover in data.get("losers", []):
            result.append({
                "symbol": mover.get("symbol", ""),
                "percent_change": float(mover.get("percent_change", 0)),
                "price": float(mover.get("price", 0)),
                "change": float(mover.get("change", 0)),
                "volume": int(mover.get("volume", 0)),
                "type": "loser",
            })

        # Sort by absolute percent change (biggest movers first)
        result.sort(key=lambda x: abs(x["percent_change"]), reverse=True)

        logger.info(f"Fetched {len(result)} top movers from Alpaca screener")
        _set_cache(cache_key, result)
        return result

    except Exception as e:
        logger.error(f"Failed to fetch top movers: {e}")
        return []


def get_most_actives(top: int = 100) -> List[Dict[str, Any]]:
    """
    Fetch most active stocks (by volume) from the Alpaca screener API.
    Returns list of dicts with symbol, volume, trade_count, etc.
    Cached for 5 minutes.
    """
    cache_key = f"most_actives:{top}"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    if not _is_configured():
        return []

    try:
        import requests as req

        url = "https://data.alpaca.markets/v1beta1/screener/stocks/most-actives"
        headers = {
            "APCA-API-KEY-ID": ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        }
        params = {"by": "volume", "top": top}

        resp = req.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        result = []
        for stock in data.get("most_actives", []):
            result.append({
                "symbol": stock.get("symbol", ""),
                "volume": int(stock.get("volume", 0)),
                "trade_count": int(stock.get("trade_count", 0)),
                "price": float(stock.get("price", 0)),
            })

        logger.info(f"Fetched {len(result)} most active stocks from Alpaca screener")
        _set_cache(cache_key, result)
        return result

    except Exception as e:
        logger.error(f"Failed to fetch most actives: {e}")
        return []


def _invalidate(prefix: str):
    """Invalidate all cache entries starting with prefix."""
    with _cache_lock:
        keys_to_remove = [k for k in _cache if k.startswith(prefix)]
        for k in keys_to_remove:
            del _cache[k]

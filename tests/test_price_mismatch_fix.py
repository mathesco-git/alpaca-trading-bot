"""
Tests for the stop-loss monitor and order execution logic.

Verifies that:
1. stop_loss_monitor triggers correctly when price is below stop-loss
2. stop_loss_monitor does NOT trigger when price is above stop-loss
3. execute_entry uses actual Alpaca fill price for stop/profit calculations
4. SIP feed parameter is set in config

These tests mock all external dependencies so they can run without
alpaca, sqlalchemy, etc. installed.
"""

import sys
import os
import types
import unittest
from unittest.mock import patch, MagicMock

# Add project root to path
PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, PROJECT_ROOT)


def _mock_module(name, attrs=None):
    mod = types.ModuleType(name)
    if attrs:
        for k, v in attrs.items():
            setattr(mod, k, v)
    return mod


# SQLAlchemy: use a MagicMock that auto-creates any attribute
class _AutoMock(MagicMock):
    """MagicMock that works as a module — any attribute access returns a MagicMock."""
    def __getattr__(self, name):
        if name.startswith('_'):
            return super().__getattr__(name)
        return MagicMock()

# Create a mock Base class that can be subclassed
_mock_base = type("Base", (), {"__tablename__": None, "metadata": MagicMock()})

_sa_mock = _AutoMock()
_sa_mock.Column = MagicMock(return_value=None)
_sa_mock.Index = MagicMock(return_value=None)
sys.modules["sqlalchemy"] = _sa_mock

_sa_orm = _AutoMock()
_sa_orm.declarative_base = MagicMock(return_value=_mock_base)
sys.modules["sqlalchemy.orm"] = _sa_orm

for submod in ["sqlalchemy.ext", "sqlalchemy.ext.declarative",
               "sqlalchemy.pool", "sqlalchemy.exc"]:
    sys.modules[submod] = _AutoMock()

# Alpaca mocks
for mod_name in [
    "alpaca", "alpaca.trading", "alpaca.trading.client", "alpaca.trading.requests",
    "alpaca.trading.enums", "alpaca.data", "alpaca.data.historical",
    "alpaca.data.requests", "alpaca.data.timeframe",
]:
    sys.modules[mod_name] = _mock_module(mod_name)

sys.modules["alpaca.trading.client"].TradingClient = MagicMock
sys.modules["alpaca.trading.requests"].MarketOrderRequest = MagicMock
sys.modules["alpaca.trading.requests"].LimitOrderRequest = MagicMock
sys.modules["alpaca.trading.requests"].StopOrderRequest = MagicMock
sys.modules["alpaca.trading.requests"].GetOrdersRequest = MagicMock
sys.modules["alpaca.trading.requests"].GetAssetsRequest = MagicMock
sys.modules["alpaca.trading.requests"].StockLatestTradeRequest = MagicMock
sys.modules["alpaca.trading.enums"].OrderSide = MagicMock()
sys.modules["alpaca.trading.enums"].TimeInForce = MagicMock()
sys.modules["alpaca.trading.enums"].OrderStatus = MagicMock()
sys.modules["alpaca.trading.enums"].QueryOrderStatus = MagicMock()
sys.modules["alpaca.trading.enums"].AssetClass = MagicMock()
sys.modules["alpaca.trading.enums"].AssetStatus = MagicMock()
sys.modules["alpaca.data.historical"].StockHistoricalDataClient = MagicMock
sys.modules["alpaca.data.requests"].StockBarsRequest = MagicMock
sys.modules["alpaca.data.timeframe"].TimeFrame = MagicMock()
sys.modules["alpaca.data.timeframe"].TimeFrameUnit = MagicMock()

# Other optional deps
for mod_name in ["apscheduler", "apscheduler.schedulers",
                 "apscheduler.schedulers.background", "apscheduler.triggers",
                 "apscheduler.triggers.cron", "fastapi", "fastapi.responses",
                 "fastapi.templating", "jinja2"]:
    if mod_name not in sys.modules:
        sys.modules[mod_name] = _mock_module(mod_name)

import pandas as pd


# ═══════════════════════════════════════════════════════
# Test 1: SIP feed configuration
# ═══════════════════════════════════════════════════════

class TestSIPFeedConfig(unittest.TestCase):
    """Verify SIP feed is configured."""

    def test_data_feed_config_exists(self):
        import config
        self.assertTrue(hasattr(config, 'ALPACA_DATA_FEED'))

    def test_scan_interval_is_15(self):
        import config
        self.assertEqual(config.DAY_SCAN_INTERVAL_MINUTES, 15)


# ═══════════════════════════════════════════════════════
# Test 2: stop_loss_monitor behavior
# ═══════════════════════════════════════════════════════

class TestStopLossMonitor(unittest.TestCase):
    """Test that stop_loss_monitor triggers correctly based on price vs stop-loss."""

    def _run_scenario(self, latest_price, intraday_closes, stop_loss=118.13, take_profit=150.85):
        """Helper to run stop_loss_monitor with mocked data."""
        from core import scheduler

        with patch.object(scheduler, '_check_market_open', return_value=True), \
             patch.object(scheduler, 'get_db') as mock_db, \
             patch.object(scheduler, 'alpaca_client') as mock_alpaca, \
             patch.object(scheduler, 'get_intraday_data') as mock_intraday, \
             patch.object(scheduler, 'get_daily_data', return_value=None), \
             patch.object(scheduler, 'execute_exit') as mock_exit, \
             patch.object(scheduler, 'log_heartbeat'):

            mock_trade = MagicMock()
            mock_trade.id = 1
            mock_trade.symbol = "NBIS"
            mock_trade.stop_loss = stop_loss
            mock_trade.take_profit = take_profit
            mock_trade.side = "buy"
            mock_trade.strategy_type = "day"

            mock_session = MagicMock()
            mock_session.query.return_value.filter.return_value.all.return_value = [mock_trade]
            mock_db.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)

            mock_alpaca.get_latest_price.return_value = latest_price

            if intraday_closes is not None:
                n = len(intraday_closes)
                df = pd.DataFrame({
                    "close": intraday_closes,
                    "open": intraday_closes,
                    "high": [p + 0.5 for p in intraday_closes],
                    "low": [p - 0.5 for p in intraday_closes],
                    "volume": [5000] * n,
                    "vwap": intraday_closes,
                    "rsi": [50.0] * n,
                    "atr": [1.0] * n,
                    "volume_avg_20": [4000] * n,
                })
                mock_intraday.return_value = df
            else:
                mock_intraday.return_value = None

            scheduler.stop_loss_monitor()
            return mock_exit

    def test_triggers_when_price_below_stop(self):
        """Price $115 < stop $118.13 → should trigger stop-loss."""
        mock_exit = self._run_scenario(115.00, [116.0, 115.5, 115.0])
        mock_exit.assert_called_once()
        self.assertEqual(mock_exit.call_args[0][1], 115.00)

    def test_no_trigger_when_price_above_stop(self):
        """Price $131.50 > stop $118.13 → no trigger."""
        mock_exit = self._run_scenario(131.50, [130.0, 131.0, 131.50])
        mock_exit.assert_not_called()

    def test_works_without_intraday_data(self):
        """No intraday data available. Price $115 < stop → should still trigger."""
        mock_exit = self._run_scenario(115.00, None)
        mock_exit.assert_called_once()

    def test_triggers_at_exact_stop_price(self):
        """Price exactly at stop-loss → should trigger (<=)."""
        mock_exit = self._run_scenario(118.13, [119.0, 118.5, 118.13])
        mock_exit.assert_called_once()

    def test_no_trigger_just_above_stop(self):
        """Price $118.14 just above stop $118.13 → no trigger."""
        mock_exit = self._run_scenario(118.14, [119.0, 118.5, 118.14])
        mock_exit.assert_not_called()


# ═══════════════════════════════════════════════════════
# Test 3: execute_entry uses actual fill price
# ═══════════════════════════════════════════════════════

class TestExecuteEntryFillPrice(unittest.TestCase):
    """Test that execute_entry uses the actual Alpaca fill price, not the signal price."""

    def _run_entry(self, signal_price, fill_price):
        """Helper to run execute_entry."""
        from core import order_executor
        from db import models

        class MockTrade:
            def __init__(self, **kwargs):
                for k, v in kwargs.items():
                    setattr(self, k, v)
                self.id = 1

        with patch.object(order_executor, 'config') as mock_config, \
             patch.object(order_executor, 'alpaca_client') as mock_alpaca, \
             patch.object(order_executor, 'calculate_position_size', return_value=195), \
             patch.object(order_executor, 'pre_trade_check', return_value=(True, "")), \
             patch.object(order_executor, 'calculate_stop_loss', return_value=118.13) as mock_sl, \
             patch.object(order_executor, 'calculate_take_profit', return_value=150.85) as mock_tp, \
             patch.object(order_executor, 'log_trade_entry'), \
             patch.object(order_executor, 'log_rejected_trade'), \
             patch.object(order_executor, 'get_db') as mock_db, \
             patch.object(order_executor, 'log_heartbeat'), \
             patch.object(order_executor, 'logger'), \
             patch.object(models, 'Trade', MockTrade), \
             patch('core.order_executor.Trade', MockTrade):

            mock_config.ENABLE_TRADING = True
            mock_alpaca.submit_market_order.return_value = "order-123"
            mock_alpaca.get_order_details.return_value = {
                "status": "filled",
                "filled_avg_price": fill_price,
                "filled_qty": 195,
            }

            mock_session = MagicMock()
            mock_db.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_db.return_value.__exit__ = MagicMock(return_value=False)

            signal = {"entry_price": signal_price, "atr": 8.18, "reason": "test"}
            result = order_executor.execute_entry("NBIS", "day", signal, 100000, 400000)

            return result, mock_sl, mock_tp

    def test_uses_fill_price_for_stop_loss(self):
        """Stop-loss should be calculated from actual fill price, not signal price."""
        result, mock_sl, mock_tp = self._run_entry(
            signal_price=95.75, fill_price=130.40
        )
        # calculate_stop_loss was called with actual fill price (130.40), not signal (95.75)
        mock_sl.assert_called_once()
        self.assertEqual(mock_sl.call_args[0][0], 130.40)

    def test_trade_proceeds_normally(self):
        """Trade should complete successfully and return a trade ID."""
        result, _, _ = self._run_entry(signal_price=130.40, fill_price=130.50)
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)

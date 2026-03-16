"""
SQLAlchemy ORM models for the trading bot database.

Tables:
    - trades: All executed trades with strategy tagging
    - heartbeat_log: Bot health and activity log entries
    - equity_history: Daily equity snapshots for charting
"""

from datetime import datetime, date
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Date, Text,
    Index, create_engine
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Trade(Base):
    """
    Records every trade the bot enters.
    strategy_type ('day' or 'swing') is the single source of truth
    that prevents EOD liquidation from closing swing trades.
    """
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String, nullable=False, index=True)
    strategy_type = Column(String, nullable=False, index=True)  # "day" or "swing"
    side = Column(String, nullable=False)  # "buy" or "sell"
    quantity = Column(Integer, nullable=False)
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=True)
    stop_loss = Column(Float, nullable=False)
    take_profit = Column(Float, nullable=True)
    status = Column(String, nullable=False, default="open")  # open, closed, stopped_out, eod_liquidated
    pnl = Column(Float, nullable=True)
    alpaca_order_id = Column(String, nullable=True)
    entry_time = Column(DateTime, nullable=False, default=datetime.utcnow)
    exit_time = Column(DateTime, nullable=True)
    notes = Column(String, nullable=True)

    # Composite index for EOD liquidation query
    __table_args__ = (
        Index("ix_trades_strategy_status", "strategy_type", "status"),
    )

    def to_dict(self):
        """Convert trade to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "symbol": self.symbol,
            "strategy_type": self.strategy_type,
            "side": self.side,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "status": self.status,
            "pnl": self.pnl,
            "alpaca_order_id": self.alpaca_order_id,
            "entry_time": self.entry_time.isoformat() if self.entry_time else None,
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
            "notes": self.notes,
        }


class HeartbeatLog(Base):
    """Bot health and activity log entries displayed on the dashboard."""
    __tablename__ = "heartbeat_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    message = Column(String, nullable=False)
    level = Column(String, nullable=False, default="info")  # info, warning, error
    # Structured fields for scanner signals and order events
    event_type = Column(String, nullable=True, index=True)  # signal, order, rejection, scan, health, ...
    detail = Column(Text, nullable=True)  # JSON-encoded structured data

    def to_dict(self):
        """Convert log entry to dictionary for JSON serialization."""
        import json
        parsed_detail = None
        if self.detail:
            try:
                parsed_detail = json.loads(self.detail)
            except (json.JSONDecodeError, TypeError):
                parsed_detail = self.detail
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "message": self.message,
            "level": self.level,
            "event_type": self.event_type,
            "detail": parsed_detail,
        }


class EquityHistory(Base):
    """Daily equity snapshots for the equity curve chart."""
    __tablename__ = "equity_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, nullable=False, unique=True, index=True)
    equity = Column(Float, nullable=False)
    cash = Column(Float, nullable=False)
    day_pnl = Column(Float, default=0)
    swing_pnl = Column(Float, default=0)
    open_day_positions = Column(Integer, default=0)
    open_swing_positions = Column(Integer, default=0)

    def to_dict(self):
        """Convert equity snapshot to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "date": self.date.isoformat() if self.date else None,
            "equity": self.equity,
            "cash": self.cash,
            "day_pnl": self.day_pnl,
            "swing_pnl": self.swing_pnl,
            "open_day_positions": self.open_day_positions,
            "open_swing_positions": self.open_swing_positions,
        }

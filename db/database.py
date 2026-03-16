"""
Database engine, session factory, and initialization.
Uses SQLite via SQLAlchemy for trade logs, heartbeat, and equity history.
"""

import logging
from datetime import datetime
from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from db.models import Base
from config import DATABASE_URL

logger = logging.getLogger(__name__)

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},  # Required for SQLite + threads
    echo=False,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db():
    """Create all database tables if they don't exist."""
    Base.metadata.create_all(bind=engine)

    # Lightweight column migration for SQLite (no Alembic).
    # Adds new nullable columns to existing tables if they're missing.
    _migrate_add_columns()

    logger.info("Database initialized — all tables created.")


def _migrate_add_columns():
    """Add any missing nullable columns to existing tables.

    SQLAlchemy's create_all only creates new tables — it won't add columns
    to tables that already exist.  This helper inspects the live schema and
    issues ALTER TABLE statements for any columns that are defined in the
    ORM models but missing from the database.
    """
    from sqlalchemy import inspect as sa_inspect, text
    inspector = sa_inspect(engine)
    migrations = [
        # (table_name, column_name, column_type_sql)
        ("heartbeat_log", "event_type", "VARCHAR"),
        ("heartbeat_log", "detail", "TEXT"),
    ]
    with engine.connect() as conn:
        for table, col, col_type in migrations:
            if table not in inspector.get_table_names():
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            if col not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))
                logger.info(f"Migration: added column {table}.{col} ({col_type})")
        conn.commit()


def get_session() -> Session:
    """Get a new database session. Caller must close it."""
    return SessionLocal()


@contextmanager
def get_db():
    """Context manager for database sessions with auto-commit and rollback."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def log_heartbeat(message: str, level: str = "info",
                   event_type: str = None, detail: dict = None):
    """Write an entry to the heartbeat_log table.

    Args:
        message: Human-readable log message.
        level: "info", "warning", or "error".
        event_type: Optional event category for structured filtering
                    (e.g. "signal", "order", "rejection", "scan", "health").
        detail: Optional dict of structured data — stored as JSON.
    """
    from db.models import HeartbeatLog
    import json
    try:
        detail_json = json.dumps(detail) if detail else None
        with get_db() as session:
            entry = HeartbeatLog(
                timestamp=datetime.utcnow(),
                message=message,
                level=level,
                event_type=event_type,
                detail=detail_json,
            )
            session.add(entry)
        logger.log(
            getattr(logging, level.upper(), logging.INFO),
            f"[Heartbeat] {message}"
        )
    except Exception as e:
        logger.error(f"Failed to write heartbeat: {e}")

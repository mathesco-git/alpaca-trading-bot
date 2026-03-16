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
    logger.info("Database initialized — all tables created.")


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


def log_heartbeat(message: str, level: str = "info"):
    """Write an entry to the heartbeat_log table."""
    from db.models import HeartbeatLog
    try:
        with get_db() as session:
            entry = HeartbeatLog(
                timestamp=datetime.utcnow(),
                message=message,
                level=level,
            )
            session.add(entry)
        logger.log(
            getattr(logging, level.upper(), logging.INFO),
            f"[Heartbeat] {message}"
        )
    except Exception as e:
        logger.error(f"Failed to write heartbeat: {e}")

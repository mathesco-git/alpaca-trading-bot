"""
Centralized logging configuration.
Console output + rotating file handler for trading_bot.log.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from config import LOG_LEVEL


def setup_logging():
    """Configure the root logger with console and rotating file handlers."""
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    # Clear existing handlers to avoid duplicates on re-init
    root_logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # Rotating file handler — max 10MB, keep 5 backups
    try:
        file_handler = RotatingFileHandler(
            "trading_bot.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
    except Exception:
        pass

    # Suppress noisy third-party loggers
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    return root_logger

"""
Phase 2: Alert System.

Sends notifications via:
    1. Email (SMTP) — for trade executions, stop-loss hits, daily reports
    2. Webhook (Discord/Slack) — same events via incoming webhook URL

All alerts are fire-and-forget with error suppression so they never
crash the bot. Controlled by config flags ENABLE_EMAIL_ALERTS and
ENABLE_WEBHOOK_ALERTS.
"""

import logging
import smtplib
import json
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from typing import Optional, Dict, Any

import config

logger = logging.getLogger(__name__)


def send_alert(
    title: str,
    message: str,
    level: str = "info",
    data: Optional[Dict[str, Any]] = None,
):
    """
    Dispatch an alert to all enabled channels.

    Args:
        title: Short alert title (e.g. "Trade Executed", "Stop-Loss Hit")
        message: Detailed message body
        level: "info", "warning", "error"
        data: Optional structured data for rich formatting
    """
    if config.ENABLE_EMAIL_ALERTS:
        _send_email_safe(title, message, level, data)
    if config.ENABLE_WEBHOOK_ALERTS:
        _send_webhook_safe(title, message, level, data)


def alert_trade_entry(symbol: str, strategy: str, qty: int, price: float,
                      stop_loss: float, take_profit: Optional[float] = None):
    """Alert when a trade is entered."""
    if not config.ALERT_ON_TRADE:
        return
    tp_str = f"${take_profit:.2f}" if take_profit else "N/A"
    send_alert(
        title=f"BUY {qty} {symbol} ({strategy})",
        message=(
            f"Entry: ${price:.2f}\n"
            f"Stop-Loss: ${stop_loss:.2f}\n"
            f"Take-Profit: {tp_str}\n"
            f"Strategy: {strategy.upper()}"
        ),
        level="info",
        data={"symbol": symbol, "side": "buy", "qty": qty, "price": price,
              "stop_loss": stop_loss, "take_profit": take_profit, "strategy": strategy},
    )


def alert_trade_exit(symbol: str, strategy: str, qty: int, exit_price: float,
                     pnl: float, reason: str):
    """Alert when a trade is exited."""
    if not config.ALERT_ON_TRADE:
        return
    pnl_emoji = "+" if pnl >= 0 else ""
    send_alert(
        title=f"SELL {qty} {symbol} ({strategy}) — {pnl_emoji}${pnl:.2f}",
        message=(
            f"Exit: ${exit_price:.2f}\n"
            f"P&L: {pnl_emoji}${pnl:.2f}\n"
            f"Reason: {reason}\n"
            f"Strategy: {strategy.upper()}"
        ),
        level="info" if pnl >= 0 else "warning",
        data={"symbol": symbol, "side": "sell", "qty": qty, "price": exit_price,
              "pnl": pnl, "reason": reason, "strategy": strategy},
    )


def alert_stop_loss(symbol: str, strategy: str, price: float, stop_price: float):
    """Alert when a stop-loss is triggered."""
    if not config.ALERT_ON_STOP_LOSS:
        return
    send_alert(
        title=f"STOP-LOSS: {symbol} ({strategy})",
        message=(
            f"Price: ${price:.2f} hit stop @ ${stop_price:.2f}\n"
            f"Strategy: {strategy.upper()}"
        ),
        level="warning",
        data={"symbol": symbol, "price": price, "stop_price": stop_price, "strategy": strategy},
    )


def alert_daily_report(equity: float, day_pnl: float, swing_pnl: float,
                       open_day: int, open_swing: int):
    """Alert with daily summary report."""
    if not config.ALERT_ON_DAILY_REPORT:
        return
    total = day_pnl + swing_pnl
    pnl_sign = "+" if total >= 0 else ""
    send_alert(
        title=f"Daily Report — {pnl_sign}${total:.2f}",
        message=(
            f"Equity: ${equity:.2f}\n"
            f"Day P&L: ${day_pnl:.2f}\n"
            f"Swing P&L: ${swing_pnl:.2f}\n"
            f"Open positions: {open_day} day / {open_swing} swing"
        ),
        level="info",
        data={"equity": equity, "day_pnl": day_pnl, "swing_pnl": swing_pnl,
              "open_day": open_day, "open_swing": open_swing},
    )


def alert_error(title: str, error_message: str):
    """Alert on critical errors (health check failures, API issues)."""
    if not config.ALERT_ON_ERROR:
        return
    send_alert(title=title, message=error_message, level="error")


# ─── Email Delivery ──────────────────────────────

def _send_email_safe(title: str, message: str, level: str,
                     data: Optional[Dict[str, Any]] = None):
    """Send email alert with error suppression."""
    try:
        _send_email(title, message, level, data)
    except Exception as e:
        logger.warning(f"Email alert failed: {e}")


def _send_email(title: str, message: str, level: str,
                data: Optional[Dict[str, Any]] = None):
    """Send an email alert via SMTP."""
    if not config.SMTP_USER or not config.ALERT_EMAIL_TO:
        logger.debug("Email alerts configured but SMTP_USER or ALERT_EMAIL_TO not set")
        return

    level_icons = {"info": "ℹ️", "warning": "⚠️", "error": "🚨"}
    icon = level_icons.get(level, "📊")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"{icon} Trading Bot: {title}"
    msg["From"] = config.SMTP_USER
    msg["To"] = config.ALERT_EMAIL_TO

    # Plain text
    text_body = f"{title}\n{'='*40}\n\n{message}\n\nTime: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
    msg.attach(MIMEText(text_body, "plain"))

    # HTML
    level_colors = {"info": "#a6e3a1", "warning": "#f9e2af", "error": "#f38ba8"}
    color = level_colors.get(level, "#cdd6f4")

    html_body = f"""
    <div style="font-family:system-ui;background:#1e1e2e;color:#cdd6f4;padding:20px;border-radius:12px;">
        <div style="border-left:4px solid {color};padding-left:12px;margin-bottom:16px;">
            <h2 style="margin:0;color:{color};">{icon} {title}</h2>
        </div>
        <pre style="background:#11111b;padding:12px;border-radius:8px;color:#cdd6f4;white-space:pre-wrap;">{message}</pre>
        <p style="color:#6c7086;font-size:12px;margin-top:16px;">
            {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC — Trading Bot Alert
        </p>
    </div>
    """
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as server:
        server.starttls()
        server.login(config.SMTP_USER, config.SMTP_PASSWORD)
        server.send_message(msg)

    logger.debug(f"Email alert sent: {title}")


# ─── Webhook Delivery ──────────────────────────────

def _send_webhook_safe(title: str, message: str, level: str,
                       data: Optional[Dict[str, Any]] = None):
    """Send webhook alert with error suppression."""
    try:
        _send_webhook(title, message, level, data)
    except Exception as e:
        logger.warning(f"Webhook alert failed: {e}")


def _send_webhook(title: str, message: str, level: str,
                  data: Optional[Dict[str, Any]] = None):
    """Send a webhook alert to Discord or Slack."""
    if not config.WEBHOOK_URL:
        logger.debug("Webhook alerts enabled but WEBHOOK_URL not set")
        return

    import httpx

    url = config.WEBHOOK_URL

    # Detect Discord vs Slack by URL pattern
    if "discord" in url.lower():
        payload = _format_discord(title, message, level, data)
    else:
        payload = _format_slack(title, message, level, data)

    resp = httpx.post(url, json=payload, timeout=10)
    resp.raise_for_status()
    logger.debug(f"Webhook alert sent: {title}")


def _format_discord(title: str, message: str, level: str,
                    data: Optional[Dict[str, Any]] = None) -> Dict:
    """Format alert as Discord webhook embed."""
    level_colors = {"info": 0xa6e3a1, "warning": 0xf9e2af, "error": 0xf38ba8}
    return {
        "embeds": [{
            "title": title,
            "description": f"```\n{message}\n```",
            "color": level_colors.get(level, 0xcdd6f4),
            "timestamp": datetime.utcnow().isoformat(),
            "footer": {"text": "Trading Bot Alert"},
        }]
    }


def _format_slack(title: str, message: str, level: str,
                  data: Optional[Dict[str, Any]] = None) -> Dict:
    """Format alert as Slack incoming webhook payload."""
    level_emojis = {"info": ":information_source:", "warning": ":warning:", "error": ":rotating_light:"}
    emoji = level_emojis.get(level, ":chart_with_upwards_trend:")
    return {
        "text": f"{emoji} *{title}*\n```{message}```",
    }

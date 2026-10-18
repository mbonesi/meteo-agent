"""Minimal client for the Telegram Bot API.

Only sendMessage and getUpdates: no need for a library for two HTTP endpoints.
"""

from __future__ import annotations

import logging
import re

import requests

from .config import TelegramConfig

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
MAX_LEN = 4096  # Telegram's per-message limit
SEND_ATTEMPTS = 2

# The bot token travels in the URL path (/bot<id>:<secret>/method), and both
# urllib3's retry warnings and requests' exception messages quote that path on
# connection failures (DNS down, connection refused, TLS errors). Anything that
# reaches the journal must go through redact() first.
_TOKEN_IN_PATH = re.compile(r"/bot\d+:[^/\s'\"]+")


def redact(text: str) -> str:
    """Masks any bot token in a URL path: /bot123:ABC/sendMessage -> /bot<redacted>/sendMessage."""
    return _TOKEN_IN_PATH.sub("/bot<redacted>", text)


class TokenRedactingFilter(logging.Filter):
    """Handler filter that masks bot tokens in every record, including tracebacks.

    It sits on the handler, not on a logger, so it also covers records that
    third-party libraries (urllib3) emit and propagate to the root logger.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = redact(message)
        if redacted != message:
            record.msg, record.args = redacted, None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


def install_log_redaction() -> None:
    """Adds TokenRedactingFilter to every root handler (call after logging is configured)."""
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, TokenRedactingFilter) for f in handler.filters):
            handler.addFilter(TokenRedactingFilter())


def send(session: requests.Session, cfg: TelegramConfig, text: str) -> bool:
    """Sends a message. Returns True if delivered.

    Never raises: a lost notification must not stop the agent, it should just
    end up in the log (and in the notification table, with delivered = 0).
    """
    if len(text) > MAX_LEN:
        text = text[: MAX_LEN - 20] + "\n<i>[troncato]</i>"
    response = None
    for attempt in range(1, SEND_ATTEMPTS + 1):
        try:
            response = session.post(
                f"{API_BASE}/bot{cfg.token}/sendMessage",
                json={
                    "chat_id": cfg.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=cfg.timeout_s,
            )
            break
        except requests.ConnectionError as exc:
            # Hours pass between two notifications: by then Telegram has
            # already closed the keep-alive connection sitting in the pool, and
            # the first POST dies with RemoteDisconnected. The dead connection
            # is discarded, so the second attempt opens a fresh one. http.py's
            # automatic retry doesn't cover POSTs, and that's fine: here we
            # only retry connection errors, not ReadTimeouts, where the message
            # may already have arrived and a second send would be a duplicate.
            if attempt < SEND_ATTEMPTS:
                log.warning("Telegram: connection lost (%s), retrying", redact(str(exc)))
                continue
            log.error("Telegram: send failed: %s", redact(str(exc)))
            return False
        except requests.RequestException as exc:
            log.error("Telegram: send failed: %s", redact(str(exc)))
            return False

    if response.status_code != 200:
        # description holds the real reason (wrong chat_id, bot blocked, malformed HTML...)
        log.error(
            "Telegram: HTTP %d: %s",
            response.status_code,
            _description(response),
        )
        return False
    return True


def get_me(session: requests.Session, token: str, timeout: float = 20.0) -> dict | None:
    try:
        response = session.get(f"{API_BASE}/bot{token}/getMe", timeout=timeout)
        if response.status_code != 200:
            log.error("Telegram getMe: HTTP %d: %s", response.status_code, _description(response))
            return None
        return response.json().get("result")
    except requests.RequestException as exc:
        log.error("Telegram getMe: %s", redact(str(exc)))
        return None


def get_updates(session: requests.Session, token: str, timeout: float = 20.0) -> list[dict]:
    """Recent messages received by the bot: used to discover your chat_id."""
    try:
        response = session.get(f"{API_BASE}/bot{token}/getUpdates", timeout=timeout)
        if response.status_code != 200:
            log.error(
                "Telegram getUpdates: HTTP %d: %s", response.status_code, _description(response)
            )
            return []
        return response.json().get("result", [])
    except requests.RequestException as exc:
        log.error("Telegram getUpdates: %s", redact(str(exc)))
        return []


def _description(response: requests.Response) -> str:
    try:
        return str(response.json().get("description", response.text[:200]))
    except ValueError:
        return response.text[:200]

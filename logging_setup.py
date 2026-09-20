"""Logging setup: console + rotating file, with secret redaction."""
from __future__ import annotations

import logging
import re
import sys
import time as time_module
from collections.abc import Iterable
from logging.handlers import RotatingFileHandler

from config import BASE_DIR, settings

_REDACTED = "[REDACTED]"
_SECRET_PATTERNS = (
    re.compile(r"AIza[\w-]{20,}"),
    re.compile(r"sk-[\w-]{10,}"),
    re.compile(r"gsk_\w{10,}"),
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}"),  # Telegram bot token shape
)
logger = logging.getLogger("logging_setup")
_LOG_DIR = BASE_DIR / "logs"
LOG_FILE = _LOG_DIR / "bot.log"
_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_NOISY_LOGGERS = ("aiosqlite", "httpx", "httpcore", "PIL", "asyncio")

_configured = False


class RedactionFilter(logging.Filter):
    """Masks configured secrets and well-known key patterns in messages and tracebacks."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = sorted({s for s in secrets if s and len(s) >= 8}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, _REDACTED)
        for pattern in _SECRET_PATTERNS:
            text = pattern.sub(_REDACTED, text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        record.msg = self.redact(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self.redact(record.stack_info)
        return True


def _collect_secrets() -> list[str]:
    secrets: list[str] = [settings.bot_token]
    for group in settings.gemini_groups:
        secrets.extend(group)
    for provider in settings.fallbacks:
        secrets.extend(provider.keys)
    return secrets


def setup_logging() -> None:
    """Configure root logging once: console + logs/bot.log (5 MB x 5)."""
    global _configured
    if _configured:
        return
    _LOG_DIR.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, settings.log_level, logging.INFO)
    formatter = logging.Formatter(_LOG_FORMAT)
    redactor = RedactionFilter(_collect_secrets())

    console = logging.StreamHandler(sys.stderr)
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    for handler in (console, file_handler):
        handler.setFormatter(formatter)
        handler.addFilter(redactor)
        handler.setLevel(level)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(console)
    root.addHandler(file_handler)

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.captureWarnings(True)
    _configured = True


_LOG_MAX_AGE_DAYS = 3


def cleanup_old_logs(max_age_days: int = _LOG_MAX_AGE_DAYS) -> int:
    """Delete files in logs/ whose last-modified time is older than `max_age_days`.

    Pure filesystem operation (no network involved), so it works the same whether the
    internet is up or down. Call this both on startup (to catch up on days the bot was
    off) and on a recurring schedule. Never raises: every failure is logged and skipped
    so one bad file can't stop the rest of the cleanup or crash the caller.
    Returns the number of files actually removed.
    """
    removed = 0
    try:
        if not _LOG_DIR.is_dir():
            return 0
        cutoff = time_module.time() - max_age_days * 86400
        for entry in _LOG_DIR.iterdir():
            try:
                if not entry.is_file():
                    continue
                if entry.stat().st_mtime < cutoff:
                    entry.unlink()
                    removed += 1
                    logger.info("Deleted old log file: %s", entry.name)
            except OSError as exc:
                logger.warning("Could not remove log file %s: %s", entry.name, exc)
        if removed:
            logger.info("Log cleanup: removed %d file(s) older than %d day(s)", removed, max_age_days)
    except Exception:
        logger.exception("Unexpected error during log cleanup")
    return removed

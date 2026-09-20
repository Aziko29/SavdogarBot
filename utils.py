"""Shared helpers: time, background tasks, key masking, admin alerts."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Coroutine
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter

from config import settings
from db.admins import admin_ids

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger("utils")

_TZ = ZoneInfo(settings.tz)
_bg_tasks: set[asyncio.Task[Any]] = set()
_alert_last_sent: dict[str, float] = {}
_MAX_ALERT_LEN = 4000


def utcnow() -> datetime:
    """Current time, timezone-aware UTC."""
    return datetime.now(timezone.utc)


def local_now() -> datetime:
    """Current time in settings.tz."""
    return datetime.now(_TZ)


def _on_task_done(task: asyncio.Task[Any]) -> None:
    _bg_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Background task %s failed: %r", task.get_name(), exc, exc_info=exc)


def fire_and_forget(coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
    """Schedule a coroutine, keeping a strong reference and logging its failure."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        raise
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_on_task_done)
    return task


def mask_key(key: str) -> str:
    """Safe label for logs, e.g. '…ab12'."""
    if len(key) < 12:
        return "…"
    return f"…{key[-4:]}"


def key_id(key: str) -> str:
    """Stable DB identifier of an API key (sha256 hex, first 16 chars)."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


async def alert_admins(bot: Bot, text: str, key: str, throttle_sec: int = 900) -> None:
    """Send a plain-text alert to every admin, at most once per `throttle_sec` per `key`."""
    now = time.monotonic()
    last = _alert_last_sent.get(key)
    if last is not None and now - last < throttle_sec:
        return
    _alert_last_sent[key] = now

    message = text[:_MAX_ALERT_LEN]
    for admin_id in admin_ids():
        try:
            await bot.send_message(admin_id, message, parse_mode=None)
        except TelegramForbiddenError:
            logger.warning("Alert not delivered: admin %s has not started or blocked the bot", admin_id)
        except TelegramRetryAfter as exc:
            logger.warning("Alert to admin %s rate-limited (retry after %ss)", admin_id, exc.retry_after)
        except TelegramAPIError as exc:
            logger.error("Alert to admin %s failed: %s", admin_id, exc)
        except Exception:
            logger.exception("Unexpected error while alerting admin %s", admin_id)

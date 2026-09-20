"""Autopost scheduling: night window, weighted product choice, and the recurring jobs."""
from __future__ import annotations

import logging
import random
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from chat_guard import sweep_pending_chats
from config import settings
from db.posts import recent_posted_product_ids
from db.products import list_postable, recompute_categories
from db.settings import get_settings
from log_sender import send_logs_job
from logging_setup import cleanup_old_logs
from poster import publish_product
from utils import local_now, utcnow

if TYPE_CHECKING:
    from aiogram import Bot

    from db.models import Product

logger = logging.getLogger("scheduler")

_POST_JOB_ID = "post_next"
_DOWNGRADE_JOB_ID = "run_downgrade"
_LOG_CLEANUP_JOB_ID = "cleanup_logs"
_LOG_SEND_JOB_ID = "send_logs"
_LOG_SEND_FIRST_DELAY_SEC = 60
_PLACEHOLDER_INTERVAL_MIN = 60
_DOWNGRADE_HOUR = 3
_DOWNGRADE_MINUTE = 0
_LOG_CLEANUP_HOURS = 24


def _parse_hhmm(value: str) -> time:
    """Parse a validated 'HH:MM' string (from db.settings) into a time object."""
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def is_night(t: time, start: time, end: time) -> bool:
    """True when `t` falls in the [start, end) window, handling windows that cross midnight."""
    return (start <= t < end) if start <= end else (t >= start or t < end)


def _eligible_for_repost(product: Product, now: datetime, gap: timedelta, recent_ids: set[int]) -> bool:
    """A product may be reposted unless it was posted very recently or is in the no-repeat window."""
    if product.id in recent_ids:
        return False
    if product.last_posted_at is not None and now - product.last_posted_at < gap:
        return False
    return True


async def post_next(bot: Bot, force: bool = False) -> str:
    """Pick and publish the next product; returns a short human-readable result."""
    bot_settings = await get_settings()

    if not force:
        if not bot_settings.autopost_enabled:
            return "Avtomatik post o'chirilgan."
        night_start = _parse_hhmm(bot_settings.night_start)
        night_end = _parse_hhmm(bot_settings.night_end)
        if is_night(local_now().time(), night_start, night_end):
            return "Tungi vaqt — post qilinmadi."

    candidates = await list_postable()
    if not candidates:
        return "Post qilish uchun tayyor mahsulot topilmadi."

    now = utcnow()
    gap = timedelta(hours=settings.min_repost_gap_hours)
    recent_ids = set(await recent_posted_product_ids(settings.no_repeat_last_n))

    filtered = [p for p in candidates if _eligible_for_repost(p, now, gap, recent_ids)]
    pool = filtered if filtered else candidates

    weight_map = {
        "new": bot_settings.new_multi,
        "mid": bot_settings.mid_multi,
        "old": bot_settings.old_multi,
    }
    population = [p for p in pool if weight_map.get(p.category, 0) > 0]
    if not population:
        return "Mos mahsulot topilmadi (barcha toifalar og'irligi 0 bo'lishi mumkin)."
    weights = [weight_map[p.category] for p in population]

    chosen = random.choices(population, weights=weights, k=1)[0]
    try:
        await publish_product(bot, chosen)
    except Exception as exc:
        logger.exception("Failed to publish product %s", chosen.id)
        return f"Xato: \"{chosen.name}\" (id={chosen.id}) joylanmadi — {exc}"
    return f"Post qilindi: \"{chosen.name}\" (id={chosen.id})."


async def run_downgrade() -> None:
    """Recompute new/mid/old categories from the current keep-counts in settings."""
    bot_settings = await get_settings()
    await recompute_categories(bot_settings.new_keep, bot_settings.mid_keep)


async def run_log_cleanup() -> None:
    """Delete log files older than 3 days. Pure disk I/O — works with or without internet.

    Never raises: cleanup_old_logs already catches and logs every error internally, so a
    bad file or a permissions issue can't crash this job or the scheduler.
    """
    cleanup_old_logs()


_CHAT_GUARD_JOB_ID = "chat_guard"
_CHAT_GUARD_INTERVAL_MIN = 10


def create_scheduler(bot: Bot) -> AsyncIOScheduler:
    """Build the scheduler with its jobs; call `apply_interval` before `.start()`."""
    scheduler = AsyncIOScheduler(timezone=settings.tz)
    scheduler.add_job(
        post_next,
        "interval",
        minutes=_PLACEHOLDER_INTERVAL_MIN,
        args=(bot,),
        id=_POST_JOB_ID,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=_PLACEHOLDER_INTERVAL_MIN * 30,
    )
    scheduler.add_job(
        run_downgrade,
        "cron",
        hour=_DOWNGRADE_HOUR,
        minute=_DOWNGRADE_MINUTE,
        id=_DOWNGRADE_JOB_ID,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        run_log_cleanup,
        "interval",
        hours=_LOG_CLEANUP_HOURS,
        id=_LOG_CLEANUP_JOB_ID,
        max_instances=1,
        coalesce=True,
        # No grace-time limit: if the bot (or the machine's internet) was down when this
        # was due, it still fires as soon as the process is back up — pure file cleanup
        # doesn't need the network anyway.
        misfire_grace_time=None,
    )
    scheduler.add_job(
        sweep_pending_chats,
        "interval",
        minutes=_CHAT_GUARD_INTERVAL_MIN,
        args=(bot,),
        id=_CHAT_GUARD_JOB_ID,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
        next_run_time=local_now() + timedelta(seconds=30),
    )
    if settings.owner_id and settings.log_send_hours > 0:
        # First delivery shortly after start (so a crash/restart log reaches the owner), then every N hours.
        scheduler.add_job(
            send_logs_job,
            "interval",
            hours=settings.log_send_hours,
            args=(bot,),
            id=_LOG_SEND_JOB_ID,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
            next_run_time=local_now() + timedelta(seconds=_LOG_SEND_FIRST_DELAY_SEC),
        )
    return scheduler


async def apply_interval(scheduler: AsyncIOScheduler) -> None:
    """Reschedule the autopost job's interval and misfire grace time from DB settings."""
    bot_settings = await get_settings()
    minutes = max(1, bot_settings.interval_mins)
    scheduler.reschedule_job(_POST_JOB_ID, trigger="interval", minutes=minutes)
    scheduler.modify_job(_POST_JOB_ID, misfire_grace_time=minutes * 30)
    logger.info("Autopost interval set to %d minute(s)", minutes)

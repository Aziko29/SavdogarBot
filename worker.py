"""AI processing queue: a bounded FIFO of product ids plus the worker loop that drains it."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from ai.copywriter import rewrite_post
from ai.errors import AllProvidersFailed, BlockedContentError
from config import settings
from db.products import claim_for_ai, recoverable_ids, set_ai_result, set_ai_status
from scheduler import run_downgrade
from utils import utcnow

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger("worker")

_MAX_ATTEMPTS = 5
_RETRY_DELAY_SEC = 60.0

PRODUCT_QUEUE: asyncio.Queue[int] = asyncio.Queue(maxsize=settings.queue_maxsize)


def enqueue(pid: int) -> None:
    """Queue a product id for AI processing; a full queue is logged, never raised."""
    try:
        PRODUCT_QUEUE.put_nowait(pid)
    except asyncio.QueueFull:
        logger.warning("Product queue is full; product %s will be picked up by the recovery loop", pid)


async def recover_pending() -> int:
    """Re-enqueue every product due for AI processing; returns how many were enqueued."""
    ids = await recoverable_ids()
    for pid in ids:
        enqueue(pid)
    return len(ids)


async def recovery_loop(interval_sec: float = 60.0) -> None:
    """Periodically re-enqueue due/stuck products; the safety net for a full queue or a crash."""
    while True:
        try:
            await asyncio.sleep(interval_sec)
            count = await recover_pending()
            if count:
                logger.info("Recovery loop re-enqueued %d product(s)", count)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Recovery loop iteration failed")


async def _process_one(bot: Bot, pid: int) -> None:
    """Claim one product, rewrite its copy and store the result, handling every failure mode."""
    product = await claim_for_ai(pid)
    if product is None:
        logger.debug("Product %s is no longer pending; skipping", pid)
        return

    try:
        result = await rewrite_post(bot, product)
    except BlockedContentError as exc:
        logger.warning("Product %s blocked by content policy: %s", pid, exc)
        await set_ai_status(pid, "blocked")
        return
    except AllProvidersFailed as exc:
        if product.attempts >= _MAX_ATTEMPTS:
            logger.error("Product %s failed after %d attempt(s): %s", pid, product.attempts, exc)
            await set_ai_status(pid, "failed")
            return
        logger.warning(
            "All AI providers failed for product %s (attempt %d/%d): %s",
            pid, product.attempts, _MAX_ATTEMPTS, exc,
        )
        # Do NOT sleep here: this coroutine IS the worker, so sleeping would block it from
        # picking up the next queued product for _RETRY_DELAY_SEC. recovery_loop (same
        # ~60s cadence) already re-enqueues every 'pending' product whose next_try_at is
        # due, so simply recording the retry time is enough.
        await set_ai_status(pid, "pending", next_try_at=utcnow() + timedelta(seconds=_RETRY_DELAY_SEC))
        return
    except Exception:
        logger.exception("Unexpected error rewriting product %s", pid)
        await set_ai_status(pid, "failed")
        return

    await set_ai_result(pid, result.fields, result.caption_html, result.needs_review)
    try:
        await run_downgrade()
    except Exception:
        logger.exception("run_downgrade failed after processing product %s", pid)


async def worker_loop(bot: Bot, name: str) -> None:
    """Consume PRODUCT_QUEUE forever, processing one product id at a time. Never dies."""
    logger.info("Worker %s started", name)
    while True:
        pid = await PRODUCT_QUEUE.get()
        try:
            await _process_one(bot, pid)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Worker %s: unhandled error processing product %s", name, pid)
        finally:
            PRODUCT_QUEUE.task_done()

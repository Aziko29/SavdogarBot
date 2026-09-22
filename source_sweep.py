"""Periodically checks the source channel/group for posts that were deleted there and drops
the matching products from the bot's memory (status='removed'), taking down any live posts too.

The Bot API has no "does this message still exist" call, so existence is probed indirectly:
forwarding the source message to the head admin's DM either succeeds (message still there —
the forwarded copy is deleted right away) or fails with a "not found" error (message is gone).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy.exc import SQLAlchemyError

from config import settings
from db.posts import live_posts, mark_post_dead
from db.products import list_sources_to_check, set_status

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger("source_sweep")

# Substrings of the Bot API error text that mean "this message no longer exists" rather than
# some transient or permission problem. Matched case-insensitively.
_NOT_FOUND_MARKERS = (
    "message to forward not found",
    "message to copy not found",
    "message not found",
    "message_id_invalid",
)


async def _still_exists(bot: Bot, source_chat_id: int, source_msg_id: int) -> bool | None:
    """Probe one source message.

    Returns True if it is still there, False if it was deleted, or None when the probe itself
    failed for some other reason (network error, missing permission, etc.) — in that case the
    product is left alone and gets rechecked on the next sweep.
    """
    try:
        copy = await bot.forward_message(
            chat_id=settings.head_admin_id,
            from_chat_id=source_chat_id,
            message_id=source_msg_id,
            disable_notification=True,
        )
    except TelegramBadRequest as exc:
        text = str(exc).lower()
        if any(marker in text for marker in _NOT_FOUND_MARKERS):
            return False
        logger.warning("Could not check source message %s/%s: %s", source_chat_id, source_msg_id, exc)
        return None
    except TelegramForbiddenError:
        logger.warning("Bot has no access to source chat %s anymore; skipping its checks this round", source_chat_id)
        return None
    # TelegramRetryAfter deliberately propagates: the caller backs off instead of misreading
    # a flood wait as "deleted".

    try:
        await bot.delete_message(settings.head_admin_id, copy.message_id)
    except TelegramAPIError:
        pass  # harmless leftover in the head admin's DM; not worth failing the check over
    return True


async def _drop_live_posts(bot: Bot, product_id: int) -> None:
    """Delete every still-live channel/group post of a dropped product."""
    for post in await live_posts(product_id):
        try:
            await bot.delete_message(post.chat_id, post.message_id)
        except TelegramBadRequest:
            logger.debug("Live post %s of dropped product %s already gone", post.message_id, product_id)
        except Exception:
            logger.exception("Failed to delete live post %s of dropped product %s", post.message_id, product_id)
        await mark_post_dead(post.id)


async def sweep_deleted_source_posts(bot: Bot) -> int:
    """Drop every product whose source post no longer exists; returns how many were dropped.

    Never raises: a database or Telegram failure is logged and the next run tries again.
    """
    try:
        rows = await list_sources_to_check()
    except SQLAlchemyError:
        logger.exception("Could not read the products due for a source check")
        return 0

    dropped = 0
    for product_id, source_chat_id, source_msg_id in rows:
        try:
            exists = await _still_exists(bot, source_chat_id, source_msg_id)
        except TelegramRetryAfter as exc:
            logger.warning("Source sweep is rate-limited (%ss); the rest waits for the next sweep", exc.retry_after)
            break
        if exists is not False:
            continue  # still there, or the check itself failed: leave it for the next sweep

        try:
            await set_status(product_id, "removed")
        except SQLAlchemyError:
            logger.exception("Could not mark product %s removed", product_id)
            continue
        await _drop_live_posts(bot, product_id)
        dropped += 1
        logger.info(
            "Product %s dropped: its source post %s/%s no longer exists",
            product_id, source_chat_id, source_msg_id,
        )
    return dropped

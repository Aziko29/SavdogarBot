"""Leaves channels/groups that the head admin never registered.

Anyone can add the bot to any chat. Such a chat is tracked (db.chats.track_pending) with a deadline;
if it is still not registered when the deadline passes, the bot leaves it.
"""
from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy.exc import SQLAlchemyError

from config import settings
from db.chats import PendingChat, clear_pending, due_pending_chats, list_chats
from utils import utcnow

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger("chat_guard")


async def _clear(chat_id: int) -> None:
    try:
        await clear_pending(chat_id)
    except SQLAlchemyError:
        logger.exception("Could not stop tracking chat %s", chat_id)


async def _tell_head_admin(bot: Bot, pending: PendingChat) -> None:
    text = (
        f"\U0001f6aa Begona chatdan chiqdim: <b>{html.escape(pending.title or str(pending.chat_id))}</b> "
        f"(<code>{pending.chat_id}</code>).\n"
        f"{settings.foreign_chat_grace_hours:g} soat ichida ro'yxatga qo'shilmadi."
    )
    try:
        await bot.send_message(settings.head_admin_id, text, parse_mode="HTML")
    except TelegramAPIError as exc:
        logger.warning("Could not tell the head admin about leaving chat %s: %s", pending.chat_id, exc)


async def sweep_pending_chats(bot: Bot) -> int:
    """Leave every unregistered chat whose deadline has passed; returns how many chats were left.

    Never raises: a failing chat is retried on the next sweep (network errors) or dropped from the
    tracking table (the bot is already out of it).
    """
    try:
        due = await due_pending_chats(utcnow())
        registered = {e.chat_id for e in await list_chats()} if due else set()
    except SQLAlchemyError:
        logger.exception("Could not read the chats due for leaving")
        return 0

    left = 0
    for pending in due:
        if pending.chat_id in registered:  # registered meanwhile: never leave a registered chat
            await _clear(pending.chat_id)
            continue
        try:
            await bot.leave_chat(pending.chat_id)
        except TelegramRetryAfter as exc:
            logger.warning("leave_chat is rate-limited (%ss); the rest waits for the next sweep", exc.retry_after)
            break
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            logger.info("The bot is already out of chat %s: %s", pending.chat_id, exc)
        except TelegramAPIError as exc:
            logger.warning("Could not leave chat %s (will retry): %s", pending.chat_id, exc)
            continue
        else:
            left += 1
            logger.info("Left unregistered chat %s (%s)", pending.chat_id, pending.title)
            await _tell_head_admin(bot, pending)
        await _clear(pending.chat_id)
    return left

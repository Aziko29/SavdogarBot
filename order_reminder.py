"""Reminds the admins about customer orders that nobody has accepted or rejected yet."""
from __future__ import annotations

import asyncio
import html
import logging
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import ReplyParameters
from sqlalchemy.exc import SQLAlchemyError

from config import settings
from db.admins import admin_ids
from db.orders import get_order, list_due_for_reminder, mark_reminded
from db.products import get_product
from utils import utcnow

if TYPE_CHECKING:
    from aiogram import Bot

    from db.models import Order

logger = logging.getLogger("order_reminder")

_BATCH_SIZE = 20


def _reminder_text(order: Order, product_name: str) -> str:
    """Render one reminder: how long the order has waited and which reminder of the series this is."""
    waited_min = max(1, int((utcnow() - order.created_at).total_seconds() // 60))
    number = order.remind_count + 1
    customer = f'<a href="tg://user?id={order.user_id}">{html.escape(order.user_fullname or str(order.user_id))}</a>'
    lines = [
        f"\u23f0 <b>Buyurtma #{order.order_id} javob kutmoqda</b> ({waited_min} daqiqa)",
        f"Mahsulot: <b>{html.escape(product_name)}</b> \u00d7 {order.quantity}",
        f"Mijoz: {customer}",
        "",
        "Qabul qilish yoki rad etish: buyurtma kartasidagi tugmalar yoki /admin \u2192 \U0001f9fe Buyurtmalar.",
        f"Eslatma {number}/{settings.order_remind_max}",
    ]
    return "\n".join(lines)


async def _send_reminder(bot: Bot, admin_id: int, text: str, card_id: int | None) -> bool:
    """Send one reminder to one admin (as a reply to the order card when it is known); True if delivered."""
    reply = ReplyParameters(message_id=card_id, allow_sending_without_reply=True) if card_id else None
    for _attempt in range(2):
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML", reply_parameters=reply)
            return True
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
        except TelegramForbiddenError:
            logger.warning("Admin %s has not started the bot; order reminder not delivered", admin_id)
            return False
        except TelegramAPIError:
            logger.exception("Could not send an order reminder to admin %s", admin_id)
            return False
    return False


async def remind_pending_orders(bot: Bot) -> int:
    """Remind every admin about pending orders that waited too long; returns how many orders were reminded.

    Never raises: a database or Telegram failure is logged and the next run tries again.
    """
    if settings.order_remind_max < 1:
        return 0
    try:
        due = await list_due_for_reminder(
            settings.order_remind_after_min,
            settings.order_remind_every_min,
            settings.order_remind_max,
            _BATCH_SIZE,
        )
    except SQLAlchemyError:
        logger.exception("Could not read the orders due for a reminder")
        return 0

    admins = list(admin_ids())
    reminded = 0
    for order in due:
        try:
            fresh = await get_order(order.order_id)
            if fresh is None or fresh.status != "pending":
                continue  # decided (or withdrawn) while we were busy: no reminder needed
            product = await get_product(order.product_id)
        except SQLAlchemyError:
            logger.exception("Could not prepare the reminder for order #%s", order.order_id)
            continue

        text = _reminder_text(fresh, product.name if product is not None else f"id={order.product_id}")
        delivered = 0
        for admin_id in admins:
            if await _send_reminder(bot, admin_id, text, fresh.admin_msg_ids.get(admin_id)):
                delivered += 1
        try:
            # Counted even when nobody could be reached, so a dead admin list cannot cause an endless loop.
            await mark_reminded(order.order_id)
        except SQLAlchemyError:
            logger.exception("Could not record the reminder for order #%s", order.order_id)
        if delivered:
            reminded += 1
    return reminded

"""Asks the admins what to do with a customer inquiry that went quiet.

Once an admin has accepted a customer's inquiry, the customer's messages reach him alone. If nobody
writes for `INQUIRY_IDLE_HOURS` (1 hour by default), the chat is NOT closed: every admin gets a card
with "Davom ettirish" (continue) and "Suhbatni yakunlash" (end chat) buttons instead. The first admin
to tap "continue" takes the chat over and is shown the customer, the product (with its id) and the
whole conversation so far; "end chat" closes it. The card is sent once per quiet spell.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError

from config import settings
from db.inquiries import list_idle_claimed_inquiries, mark_idle_notice_sent
from db.products import get_product
from handlers.client import notify_admins_inquiry_idle

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger("inquiry_idle")

_BATCH_SIZE = 20


async def sweep_idle_inquiries(bot: Bot) -> int:
    """Send the "continue or end?" card for every claimed inquiry that has been quiet too long.

    Returns how many inquiries were announced. Never raises: a database or Telegram failure is
    logged and the next run tries again.
    """
    hours = settings.inquiry_idle_hours
    try:
        idle = await list_idle_claimed_inquiries(hours)
    except SQLAlchemyError:
        logger.exception("Could not read the idle inquiries")
        return 0

    announced = 0
    for inquiry in idle[:_BATCH_SIZE]:
        try:
            product = await get_product(inquiry.product_id) if inquiry.product_id is not None else None
            # Atomic: fails if a message arrived since the read above (the chat is not idle after all)
            # or another sweep already sent this notice, so no admin is ever pinged twice for one spell.
            if not await mark_idle_notice_sent(inquiry.user_id, older_than_hours=hours):
                continue
        except SQLAlchemyError:
            logger.exception("Could not prepare the idle notice for user %s's inquiry", inquiry.user_id)
            continue
        try:
            if await notify_admins_inquiry_idle(bot, inquiry, product):
                announced += 1
        except Exception:
            logger.exception("Could not announce the idle inquiry of user %s", inquiry.user_id)
    return announced

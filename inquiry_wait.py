"""Tells a customer that nobody has picked up his question yet.

While an inquiry is still unclaimed `INQUIRY_WAIT_MIN` minutes (7 by default) after the customer's
first message, he gets one short note: "the admins are busy" by day, "we answer in the morning"
inside the night window set in /admin. It is sent once per opened inquiry and never to a chat an
admin has already accepted.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from sqlalchemy.exc import SQLAlchemyError

from config import settings
from db.inquiries import list_unanswered_inquiries, mark_wait_notice_sent
from db.settings import get_settings
from utils import is_night, local_now, parse_hhmm

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger("inquiry_wait")

_BATCH_SIZE = 20
BUSY_TEXT = "\u23f3 Hozir adminlar band. Savolingiz qabul qilindi, tez orada javob beramiz."
NIGHT_TEXT = "\U0001f319 Hozir tungi vaqt. Savolingizga ertalab javob beramiz."


async def _night_now() -> bool:
    """True when the current local time is inside the night window configured in /admin."""
    bot_settings = await get_settings()
    return is_night(local_now().time(), parse_hhmm(bot_settings.night_start), parse_hhmm(bot_settings.night_end))


async def sweep_unanswered_inquiries(bot: Bot) -> int:
    """Send the waiting note to every customer whose unclaimed inquiry has waited long enough.

    Returns how many customers were told. Never raises: a database or Telegram failure is logged
    and the next run tries again.
    """
    if settings.inquiry_wait_min <= 0:
        return 0
    try:
        waiting = await list_unanswered_inquiries(settings.inquiry_wait_min)
        if not waiting:
            return 0
        text = NIGHT_TEXT if await _night_now() else BUSY_TEXT
    except SQLAlchemyError:
        logger.exception("Could not read the unanswered inquiries")
        return 0

    told = 0
    for inquiry in waiting[:_BATCH_SIZE]:
        try:
            # Atomic: fails if an admin accepted the chat since the read above, or another sweep got here first.
            if not await mark_wait_notice_sent(inquiry.user_id):
                continue
        except SQLAlchemyError:
            logger.exception("Could not prepare the waiting note for user %s", inquiry.user_id)
            continue
        try:
            await bot.send_message(inquiry.user_id, text)
            told += 1
        except TelegramForbiddenError:
            logger.info("Inquiry of user %s: he has blocked the bot; waiting note not delivered", inquiry.user_id)
        except TelegramAPIError:
            logger.exception("Could not send the waiting note to user %s", inquiry.user_id)
    return told

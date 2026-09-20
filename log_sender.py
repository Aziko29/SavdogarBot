"""Periodically send the new log lines to the server owner (OWNER_ID) as a .log document.

Only lines written since the last successful delivery are sent, so nothing is sent twice and
nothing is lost between two runs. The logs are already redacted by logging_setup.RedactionFilter.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import BufferedInputFile

from config import BASE_DIR, settings
from log_tail import limit_payload, load_state, read_new_lines, save_state
from logging_setup import LOG_FILE
from utils import local_now

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger("log_sender")

_STATE_FILE = BASE_DIR / "data" / "log_send_state.json"


async def send_logs_to_owner(bot: Bot) -> bool:
    """Send the not-yet-delivered log lines to OWNER_ID; True when a file was delivered."""
    owner_id = settings.owner_id
    if not owner_id:
        return False

    offset, inode = await asyncio.to_thread(load_state, _STATE_FILE)
    payload, new_offset, new_inode = await asyncio.to_thread(read_new_lines, LOG_FILE, offset, inode)
    if not payload.strip():
        if (new_offset, new_inode) != (offset, inode):
            await asyncio.to_thread(save_state, _STATE_FILE, new_offset, new_inode)
        return False

    me = await bot.me()
    label = me.username or str(me.id)
    stamp = local_now().strftime("%Y-%m-%d_%H-%M")
    document = BufferedInputFile(limit_payload(payload), filename=f"{label}-{stamp}.log")
    try:
        await bot.send_document(owner_id, document, caption=f"\U0001f4c4 @{label} logi ({stamp})", parse_mode=None)
    except TelegramForbiddenError:
        logger.warning("Logs not delivered: the owner (%s) has not started the bot or blocked it", owner_id)
        return False
    except TelegramAPIError as exc:
        logger.warning("Logs not delivered to the owner: %s", exc)
        return False

    # Save the position only after a successful delivery, so a failed send is retried next time.
    await asyncio.to_thread(save_state, _STATE_FILE, new_offset, new_inode)
    logger.debug("Sent %d byte(s) of logs to the owner", len(payload))
    return True


async def send_logs_job(bot: Bot) -> None:
    """Scheduler entry point; never raises so a failure cannot disturb the scheduler."""
    try:
        await send_logs_to_owner(bot)
    except Exception:
        logger.exception("Unexpected error while sending logs to the owner")

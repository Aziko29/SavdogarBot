"""The command menu behind Telegram's "/" button (and the blue "Menu" button next to the input field).

Two menus are published with `setMyCommands`:
- customers see the commands of the order flow (every private chat by default);
- admins get their own list through a per-chat scope, so `/admin` is one tap away for them and they are
  not offered customer commands that mean nothing to them.

Everything here is best effort: a menu that cannot be published must never stop the bot, so every
Telegram error is logged and swallowed. The command *handlers* live in `handlers/client.py` / `handlers/admin.py`.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat

from db.admins import admin_ids

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger("bot_commands")

# Telegram wants lower-case latin letters, digits and underscores; the descriptions are what the customer reads.
CUSTOMER_COMMANDS: tuple[BotCommand, ...] = (
    BotCommand(command="katalog", description="📂 Katalog"),
    BotCommand(command="buyurtmalarim", description="🧾 Buyurtmalarim"),
    BotCommand(command="malumotlarim", description="📋 Saqlangan ma'lumotlarim"),
    BotCommand(command="cancel", description="❌ Bekor qilish"),
    BotCommand(command="yordam", description="ℹ️ Yordam"),
)

ADMIN_COMMANDS: tuple[BotCommand, ...] = (
    BotCommand(command="admin", description="⚙️ Admin paneli"),
    BotCommand(command="cancel", description="❌ Amalni bekor qilish"),
    BotCommand(command="yordam", description="ℹ️ Yordam"),
)

# Admins whose personal menu was published during this run; makes `ensure_admin_commands` cheap to call often.
_admin_menu_done: set[int] = set()


async def _publish_customer_menu(bot: Bot) -> bool:
    try:
        await bot.set_my_commands(list(CUSTOMER_COMMANDS), scope=BotCommandScopeAllPrivateChats())
    except TelegramAPIError as exc:
        logger.warning("Could not publish the customer command menu: %s", exc)
        return False
    return True


async def ensure_admin_commands(bot: Bot, user_id: int) -> bool:
    """Give one admin the admin menu in his private chat; True when it is in place.

    Fails quietly (returns False) when the admin has never pressed Start: Telegram has no chat to attach the
    menu to yet. The next call (his `/start`, or the next bot start) retries.
    """
    if user_id in _admin_menu_done:
        return True
    try:
        await bot.set_my_commands(list(ADMIN_COMMANDS), scope=BotCommandScopeChat(chat_id=user_id))
    except TelegramAPIError as exc:
        logger.debug("Admin menu for %s not published yet: %s", user_id, exc)
        return False
    _admin_menu_done.add(user_id)
    return True


async def drop_admin_commands(bot: Bot, user_id: int) -> None:
    """Take the admin menu away from a removed admin; his chat falls back to the customer menu."""
    _admin_menu_done.discard(user_id)
    try:
        await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=user_id))
    except TelegramAPIError as exc:
        logger.debug("Could not remove the admin menu of %s: %s", user_id, exc)


async def setup_bot_commands(bot: Bot) -> None:
    """Publish the customer menu and the admin menu of every current admin (called once at startup)."""
    customer_ok = await _publish_customer_menu(bot)
    ready = 0
    ids = admin_ids()
    for user_id in ids:
        if await ensure_admin_commands(bot, user_id):
            ready += 1
    logger.info(
        "Command menu: customers %s, admins %d/%d",
        "published" if customer_ok else "NOT published",
        ready,
        len(ids),
    )

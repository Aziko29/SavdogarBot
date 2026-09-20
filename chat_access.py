"""Checks what the bot may do in a chat before the head admin registers it as source or target."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (
    ChatMemberAdministrator,
    ChatMemberMember,
    ChatMemberOwner,
    ChatMemberRestricted,
)

from db.chats import ROLE_SOURCE, ROLE_TARGET
from db.models import CHAT_TYPES

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import ChatMember

logger = logging.getLogger("chat_access")


@dataclass(frozen=True, slots=True)
class AccessResult:
    """Outcome of an access check; `problem` is a ready-to-show Uzbek message when `ok` is False."""

    ok: bool
    chat_id: int
    chat_type: str
    title: str
    username: str | None
    problem: str | None = None


def _fail(chat_ref: int | str, problem: str, chat_type: str = "", title: str = "", username: str | None = None) -> AccessResult:
    chat_id = chat_ref if isinstance(chat_ref, int) else 0
    return AccessResult(False, chat_id, chat_type, title, username, problem)


def _rights_problem(member: ChatMember, chat_type: str, role: str) -> str | None:
    """Return why the bot cannot work in this chat under `role`, or None when everything is fine."""
    if isinstance(member, ChatMemberOwner):
        return None
    if isinstance(member, ChatMemberAdministrator):
        if role == ROLE_TARGET and chat_type == "channel":
            rights = (
                ("post yuborish", member.can_post_messages),
                ("xabarni tahrirlash", member.can_edit_messages),
                ("xabarni o'chirish", member.can_delete_messages),
            )
            missing = [label for label, granted in rights if granted is not True]
            if missing:
                return "Botga kanalda quyidagi huquqlar yetishmaydi: " + ", ".join(missing) + "."
        return None
    if role == ROLE_SOURCE:
        return "Bot bu kanalda admin emas. Uni kanal admini qilib qo'shing, aks holda postlar yetib kelmaydi."
    if isinstance(member, ChatMemberMember):
        if chat_type == "channel":
            return "Bot bu kanalda admin emas. Uni post yozish, tahrirlash va o'chirish huquqli admin qiling."
        return None
    if isinstance(member, ChatMemberRestricted):
        if member.can_send_messages is not True or member.can_send_photos is not True:
            return "Botga guruhda xabar va rasm yuborish cheklangan. Cheklovni olib tashlang."
        return None
    return f"Bot bu chatda qatnashmayapti (holat: {member.status})."


async def check_bot_access(bot: Bot, chat_ref: int | str, role: str) -> AccessResult:
    """Look the chat up and verify the bot's membership and rights for `role` (never raises)."""
    try:
        chat = await bot.get_chat(chat_ref)
    except (TelegramBadRequest, TelegramForbiddenError):
        return _fail(chat_ref, "Chat topilmadi yoki bot unga kira olmaydi. Botni avval kanal/guruhga qo'shing.")
    except TelegramAPIError as exc:
        logger.warning("get_chat(%s) failed: %s", chat_ref, exc)
        return _fail(chat_ref, f"Telegram javob bermadi: {exc}")

    chat_type = str(getattr(chat.type, "value", chat.type))
    title = chat.title or ""
    if chat_type not in CHAT_TYPES:
        return _fail(chat.id, "Faqat kanal yoki guruh qo'shish mumkin.", chat_type, title, chat.username)
    if role == ROLE_SOURCE and chat_type != "channel":
        return _fail(chat.id, "Manba faqat kanal bo'lishi mumkin (guruh emas).", chat_type, title, chat.username)

    try:
        me = await bot.me()
        member = await bot.get_chat_member(chat.id, me.id)
    except TelegramAPIError as exc:
        logger.warning("get_chat_member(%s) failed: %s", chat.id, exc)
        return _fail(chat.id, "Botning bu chatdagi huquqlarini o'qib bo'lmadi.", chat_type, title, chat.username)

    problem = _rights_problem(member, chat_type, role)
    return AccessResult(problem is None, chat.id, chat_type, title, chat.username, problem)

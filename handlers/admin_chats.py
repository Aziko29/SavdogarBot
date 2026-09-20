"""Source and posting chats (head admin only).

The head admin adds the bot to a channel/group; the bot then writes to him and he picks the role:
source channel (posts are read from it) or posting place (products are published to it).
A chat can also be added by hand: forward a channel post, or send the chat ID / @username.
"""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.exc import SQLAlchemyError

from chat_access import check_bot_access
from config import settings
from db.admins import is_head_admin
from db.chats import (
    ROLE_SOURCE,
    ROLE_TARGET,
    ChatEntry,
    PendingChat,
    add_chat,
    get_chat_entry,
    list_chats,
    list_pending_chats,
    remove_chat,
    remove_chats_for,
    track_pending,
)
from db.models import CHAT_TYPES
from handlers.admin import CHATS_CALLBACK, MenuCB, _db_guard, _edit
from handlers.filters import HeadAdminFilter
from utils import utcnow

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger("handlers.admin_chats")

router = Router(name="admin_chats")
router.message.filter(HeadAdminFilter(), F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(HeadAdminFilter())
# NOTE: the my_chat_member observer is deliberately NOT filtered here; it checks the actor itself.

_ROLE_LABEL = {ROLE_SOURCE: "\U0001f4e5 Manba", ROLE_TARGET: "\U0001f4e4 Post joyi"}
_TYPE_ICON = {"channel": "\U0001f4e2", "group": "\U0001f465", "supergroup": "\U0001f465"}

_ID_RE = re.compile(r"^-?\d{6,20}$")
_USERNAME_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{3,31})$")
_LINK_RE = re.compile(r"^(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{3,31})/?$", re.IGNORECASE)

_ASK_TEXTS = {
    ROLE_SOURCE: (
        "\U0001f4e5 <b>Manba kanal qo'shish</b>\n\n"
        "Postlar olinadigan kanalni ko'rsating (bot u kanalda admin bo'lishi kerak):\n"
        "\u2022 kanaldan biror postni shu yerga forward qiling, yoki\n"
        "\u2022 kanal ID sini yuboring (<code>-1001234567890</code>), yoki\n"
        "\u2022 ochiq kanal bo'lsa @username yuboring.\n\n"
        "Osonroq yo'l: botni kanalga admin qilib qo'shing, men o'zim sizga yozaman.\n"
        "Bekor qilish: /cancel"
    ),
    ROLE_TARGET: (
        "\U0001f4e4 <b>Post joyi qo'shish</b>\n\n"
        "Tayyor postlar joylanadigan kanal yoki guruhni ko'rsating:\n"
        "\u2022 kanaldan postni forward qiling, yoki\n"
        "\u2022 chat ID sini yuboring (<code>-1001234567890</code>), yoki\n"
        "\u2022 ochiq chat bo'lsa @username yuboring.\n\n"
        "Kanalda botga post yozish, tahrirlash va o'chirish huquqlarini bering; "
        "guruhda botni a'zo qilib qo'shish yetarli.\n"
        "Osonroq yo'l: botni kanal/guruhga qo'shing, men o'zim sizga yozaman.\n"
        "Bekor qilish: /cancel"
    ),
}


class ChatMgr(StatesGroup):
    """FSM for the 'add chat by hand' input; the role is kept in the FSM data."""

    waiting_chat = State()


class ChatActCB(CallbackData, prefix="chxa"):
    """Chats-section action (the list itself is opened by CHATS_CALLBACK)."""

    action: str  # add | pick | ask_del | del | cancel | dismiss
    role: str = ""
    chat_id: int = 0
    row_id: int = 0


def _deadline() -> datetime:
    """When the bot leaves a chat that is still unregistered (aware UTC)."""
    return utcnow() + timedelta(hours=settings.foreign_chat_grace_hours)


def _grace_hours_text() -> str:
    return f"{settings.foreign_chat_grace_hours:g}"


def _pending_line(index: int, pending: PendingChat) -> str:
    icon = _TYPE_ICON.get(pending.chat_type, "\u2022")
    minutes = max(0, int((pending.expires_at - utcnow()).total_seconds() // 60))
    left = f"{minutes // 60} soat {minutes % 60} daq"
    name = html.escape(pending.title or "\u2014")
    return f"{index}. {icon} {name} \u00b7 <code>{pending.chat_id}</code> \u00b7 {left} qoldi"


def _btn(text: str, callback_data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=callback_data)


def _chat_line(index: int, entry: ChatEntry) -> str:
    icon = _TYPE_ICON.get(entry.chat_type, "\u2022")
    name = html.escape(entry.title or "\u2014")
    username = f" (@{html.escape(entry.username)})" if entry.username else ""
    return f"{index}. {icon} {name}{username} \u00b7 <code>{entry.chat_id}</code>"


async def _render_list() -> tuple[str, InlineKeyboardMarkup]:
    """Build the chats screen: sources, posting places, add and remove buttons."""
    entries = await list_chats()
    pending = await list_pending_chats()
    sources = [e for e in entries if e.role == ROLE_SOURCE]
    targets = [e for e in entries if e.role == ROLE_TARGET]
    lines = ["\U0001f4e1 <b>Kanal va guruhlar</b>", "", "\U0001f4e5 <b>Manba</b> (postlar shu kanaldan olinadi):"]
    lines.extend(_chat_line(i, e) for i, e in enumerate(sources, start=1))
    if not sources:
        lines.append("\u2014 belgilanmagan")
    lines += ["", "\U0001f4e4 <b>Post joylanadigan joylar</b> (kanal yoki guruh):"]
    lines.extend(_chat_line(i, e) for i, e in enumerate(targets, start=1))
    if not targets:
        lines.append("\u2014 belgilanmagan")
    if pending:
        lines += ["", "\u23f3 <b>Kutilmoqda</b> (vaqt tugasa, bot chatdan chiqib ketadi):"]
        lines.extend(_pending_line(i, p) for i, p in enumerate(pending, start=1))
    lines += ["", "\U0001f4a1 Botni kanal/guruhga qo'shsangiz, men sizga yozaman va vazifani tanlaysiz."]

    rows: list[list[InlineKeyboardButton]] = [
        [
            _btn("\u2795 Manba kanal", ChatActCB(action="add", role=ROLE_SOURCE).pack()),
            _btn("\u2795 Post joyi", ChatActCB(action="add", role=ROLE_TARGET).pack()),
        ]
    ]
    for p in pending:
        name = (p.title or str(p.chat_id))[:14]
        pick_row: list[InlineKeyboardButton] = []
        if p.chat_type == "channel":
            pick_row.append(_btn(f"\U0001f4e5 {name}", ChatActCB(action="pick", role=ROLE_SOURCE, chat_id=p.chat_id).pack()))
        pick_row.append(_btn(f"\U0001f4e4 {name}", ChatActCB(action="pick", role=ROLE_TARGET, chat_id=p.chat_id).pack()))
        rows.append(pick_row)
    delete_buttons = [
        _btn(f"\U0001f5d1 {_TYPE_ICON.get(e.chat_type, '')} {(e.title or str(e.chat_id))[:18]}", ChatActCB(action="ask_del", row_id=e.id).pack())
        for e in entries
    ]
    rows.extend(delete_buttons[i : i + 2] for i in range(0, len(delete_buttons), 2))
    rows.append([_btn("\u2b05\ufe0f Orqaga", MenuCB(action="root").pack())])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _extract_chat_ref(message: Message) -> tuple[int | str | None, str | None]:
    """Return (chat reference, None) or (None, error text) from a forwarded post, an ID or a @username."""
    origin = message.forward_origin
    if origin is not None:
        chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
        if chat is None:
            return None, "Bu xabardan kanal/guruhni aniqlab bo'lmadi. Chat ID sini (-100\u2026) yoki @username ni yuboring."
        return chat.id, None
    text = (message.text or "").strip()
    if _ID_RE.match(text):
        value = int(text)
        if value > 0:
            return None, "Kanal/guruh ID si manfiy bo'ladi (masalan: -1001234567890)."
        return value, None
    match = _LINK_RE.match(text) or _USERNAME_RE.match(text)
    if match:
        return f"@{match.group(1)}", None
    return None, "Tushunmadim. Postni forward qiling, chat ID (-100\u2026) yoki @username yuboring. Bekor qilish: /cancel"


async def _register(bot: Bot, ref: int | str, role: str, actor_id: int) -> tuple[bool, str]:
    """Check the bot's rights and register the chat; returns (ok, HTML text to show)."""
    result = await check_bot_access(bot, ref, role)
    if not result.ok:
        return False, f"\u274c {html.escape(result.problem or 'Xato')}"
    try:
        added = await add_chat(
            chat_id=result.chat_id,
            role=role,
            chat_type=result.chat_type,
            title=result.title,
            username=result.username,
            added_by=actor_id,
        )
    except ValueError as exc:
        return False, f"\u274c {html.escape(str(exc))}"
    verb = "qo'shildi" if added else "yangilandi"
    return True, f"\u2705 <b>{html.escape(result.title or str(result.chat_id))}</b> \u2014 {_ROLE_LABEL[role]} {verb}."


async def _notify_head(bot: Bot, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Message the head admin; he may not have started the bot yet."""
    try:
        await bot.send_message(settings.head_admin_id, text, reply_markup=markup, parse_mode="HTML")
    except TelegramAPIError as exc:
        logger.warning("Could not message the head admin: %s", exc)


# ---------------------------------------------------------------- bot added to / removed from a chat


async def _offer_roles(bot: Bot, chat: Chat) -> None:
    chat_type = str(getattr(chat.type, "value", chat.type))
    if chat_type not in CHAT_TYPES:
        return
    buttons = []
    if chat_type == "channel":
        buttons.append(_btn(_ROLE_LABEL[ROLE_SOURCE], ChatActCB(action="pick", role=ROLE_SOURCE, chat_id=chat.id).pack()))
    buttons.append(_btn(_ROLE_LABEL[ROLE_TARGET], ChatActCB(action="pick", role=ROLE_TARGET, chat_id=chat.id).pack()))
    markup = InlineKeyboardMarkup(
        inline_keyboard=[buttons, [_btn("\U0001f6ab Hech biri", ChatActCB(action="dismiss").pack())]]
    )
    text = (
        f"\u2795 Bot qo'shildi: {_TYPE_ICON[chat_type]} <b>{html.escape(chat.title or str(chat.id))}</b>\n"
        f"<code>{chat.id}</code>\n\n"
        "Uni qaysi vazifaga belgilaymiz?\n"
        "\U0001f4e5 Manba \u2014 postlar shu kanaldan olinadi\n"
        "\U0001f4e4 Post joyi \u2014 tayyor postlar shu yerga joylanadi\n\n"
        f"\u23f3 {_grace_hours_text()} soat ichida tanlanmasa, bot bu chatdan chiqib ketadi."
    )
    await _notify_head(bot, text, markup)


@router.my_chat_member()
async def on_bot_membership(event: ChatMemberUpdated, bot: Bot) -> None:
    """React to the bot being added to or removed from a channel/group."""
    chat = event.chat
    if chat.type == ChatType.PRIVATE:
        return
    status = event.new_chat_member.status
    try:
        if status in ("left", "kicked"):
            removed = await remove_chats_for(chat.id)
            if removed:
                await _notify_head(
                    bot,
                    f"\u26a0\ufe0f Bot <b>{html.escape(chat.title or str(chat.id))}</b> dan chiqarildi; "
                    "bu chat ro'yxatdan olib tashlandi.",
                )
            return
        if status not in ("administrator", "creator", "member", "restricted"):
            return
        chat_type = str(getattr(chat.type, "value", chat.type))
        if chat_type not in CHAT_TYPES:
            return
        if any(e.chat_id == chat.id for e in await list_chats()):
            return
        actor = event.from_user
        # Every unregistered chat gets a leave-countdown (the ORIGINAL deadline is kept on repeats).
        await track_pending(chat.id, chat_type, chat.title or "", actor.id if actor else None, _deadline())
        if actor is None or not is_head_admin(actor.id):
            logger.info(
                "Bot was added to chat %s by non-head user %s; it leaves in %s h unless the head admin registers it",
                chat.id, actor.id if actor else None, _grace_hours_text(),
            )
            return
        await _offer_roles(bot, chat)
    except SQLAlchemyError:
        logger.exception("Database error while handling a membership change in chat %s", chat.id)


# ---------------------------------------------------------------- list / pick / dismiss


@router.callback_query(F.data == CHATS_CALLBACK)
@_db_guard
async def on_list(callback: CallbackQuery, state: FSMContext) -> None:
    """Open the chats screen."""
    await state.clear()
    text, markup = await _render_list()
    await _edit(callback, text, markup)
    await callback.answer()


@router.callback_query(ChatActCB.filter(F.action == "cancel"))
@_db_guard
async def on_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Cancel adding or removing and return to the list."""
    await state.clear()
    text, markup = await _render_list()
    await _edit(callback, text, markup)
    await callback.answer("Bekor qilindi.")


@router.callback_query(ChatActCB.filter(F.action == "dismiss"))
async def on_dismiss(callback: CallbackQuery) -> None:
    """Close the 'bot was added' prompt without registering anything."""
    msg = callback.message
    if isinstance(msg, Message):
        try:
            await msg.delete()
        except TelegramAPIError:
            await _edit(callback, "Bekor qilindi.", InlineKeyboardMarkup(inline_keyboard=[]))
    await callback.answer("Bekor qilindi.")


@router.callback_query(ChatActCB.filter(F.action == "pick"))
@_db_guard
async def on_pick(callback: CallbackQuery, callback_data: ChatActCB, bot: Bot, state: FSMContext) -> None:
    """The head admin chose a role for a chat the bot was just added to."""
    await state.clear()
    ok, text = await _register(bot, callback_data.chat_id, callback_data.role, callback.from_user.id)
    if ok:
        list_text, markup = await _render_list()
        await _edit(callback, f"{text}\n\n{list_text}", markup)
        await callback.answer("Saqlandi \u2705")
        return
    retry = InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("\U0001f504 Qayta tekshirish", ChatActCB(action="pick", role=callback_data.role, chat_id=callback_data.chat_id).pack())],
            [_btn("\u2b05\ufe0f Kanal va guruhlar", CHATS_CALLBACK)],
        ]
    )
    await _edit(callback, f"{text}\n\nTuzatib, \"Qayta tekshirish\" tugmasini bosing.", retry)
    await callback.answer()


# ---------------------------------------------------------------- add by hand


@router.callback_query(ChatActCB.filter(F.action == "add"))
async def on_add(callback: CallbackQuery, callback_data: ChatActCB, state: FSMContext) -> None:
    """Ask for the chat to add under the chosen role."""
    role = callback_data.role if callback_data.role in _ASK_TEXTS else ROLE_TARGET
    await state.set_state(ChatMgr.waiting_chat)
    await state.update_data(role=role)
    cancel = InlineKeyboardMarkup(inline_keyboard=[[_btn("\u274c Bekor qilish", ChatActCB(action="cancel").pack())]])
    await _edit(callback, _ASK_TEXTS[role], cancel)
    await callback.answer()


@router.message(Command("cancel"), StateFilter(ChatMgr))
@_db_guard
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """Cancel the add-chat input."""
    await state.clear()
    text, markup = await _render_list()
    await message.answer("Bekor qilindi.")
    await message.answer(text, reply_markup=markup, parse_mode="HTML")


@router.message(ChatMgr.waiting_chat, F.text | F.forward_origin)
@_db_guard
async def on_chat_input(message: Message, state: FSMContext, bot: Bot) -> None:
    """Receive a forwarded post, an ID or a @username, verify the bot's rights and register the chat."""
    ref, error = _extract_chat_ref(message)
    if ref is None:
        await message.answer(error or "Noto'g'ri kiritildi.")
        return
    data = await state.get_data()
    role = data.get("role", ROLE_TARGET)
    ok, text = await _register(bot, ref, role, message.from_user.id if message.from_user else settings.head_admin_id)
    if not ok:
        await message.answer(f"{text}\n\nTuzatib qayta yuboring yoki /cancel.", parse_mode="HTML")
        return
    await state.clear()
    list_text, markup = await _render_list()
    await message.answer(text, parse_mode="HTML")
    await message.answer(list_text, reply_markup=markup, parse_mode="HTML")


@router.message(ChatMgr.waiting_chat)
async def on_chat_not_text(message: Message) -> None:
    """Anything that is neither text nor a forward while waiting for a chat."""
    await message.answer("Postni forward qiling yoki chat ID / @username ni matn qilib yuboring. Bekor qilish: /cancel")


# ---------------------------------------------------------------- remove


@router.callback_query(ChatActCB.filter(F.action == "ask_del"))
@_db_guard
async def on_ask_delete(callback: CallbackQuery, callback_data: ChatActCB) -> None:
    """Ask for confirmation before unregistering a chat."""
    entry = await get_chat_entry(callback_data.row_id)
    if entry is None:
        await callback.answer("Allaqachon o'chirilgan.", show_alert=True)
        return
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _btn("\u2705 Ha, olib tashlash", ChatActCB(action="del", row_id=entry.id).pack()),
                _btn("\u274c Bekor qilish", ChatActCB(action="cancel").pack()),
            ]
        ]
    )
    extra = "Yangi postlar endi bu yerga joylanmaydi." if entry.role == ROLE_TARGET else "Bu kanaldan yangi postlar olinmaydi."
    extra += f"\nBot {_grace_hours_text()} soatdan keyin bu chatdan chiqib ketadi (tugmalar bilan qayta qo'shmasangiz)."
    text = (
        f"\U0001f5d1 <b>{html.escape(entry.title or str(entry.chat_id))}</b> ({_ROLE_LABEL[entry.role]}) "
        f"ro'yxatdan olib tashlansinmi?\n{extra}\nAvval joylangan postlar o'chib ketmaydi."
    )
    await _edit(callback, text, markup)
    await callback.answer()


@router.callback_query(ChatActCB.filter(F.action == "del"))
@_db_guard
async def on_delete(callback: CallbackQuery, callback_data: ChatActCB, state: FSMContext) -> None:
    """Unregister the chat and show the refreshed list."""
    await state.clear()
    removed = await remove_chat(callback_data.row_id)
    if removed is not None and removed.chat_type in CHAT_TYPES:
        # The chat is unregistered now: the bot leaves it unless it is registered again in time.
        await track_pending(removed.chat_id, removed.chat_type, removed.title, callback.from_user.id, _deadline())
    text, markup = await _render_list()
    await _edit(callback, text, markup)
    await callback.answer("Olib tashlandi \u2705" if removed else "Allaqachon o'chirilgan.")

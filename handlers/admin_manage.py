"""Admin management (head admin only): list, add and remove regular admins from inside the bot."""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import Command, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import settings
from db.admins import add_admin, is_head_admin, list_admins, remove_admin
from handlers.admin import ADMINS_CALLBACK, MenuCB, _db_guard, _edit
from handlers.filters import HeadAdminFilter

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger("handlers.admin_manage")

router = Router(name="admin_manage")
router.message.filter(HeadAdminFilter(), F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(HeadAdminFilter())

_ID_RE = re.compile(r"^\d{5,15}$")
_ASK_ID_TEXT = (
    "\u2795 <b>Admin qo'shish</b>\n\n"
    "Yangi adminning Telegram ID raqamini yuboring (masalan: <code>123456789</code>) "
    "yoki uning biror xabarini shu yerga forward qiling.\n\n"
    "Bekor qilish: /cancel"
)
_NEW_ADMIN_TEXT = "\u2705 Sizga botda admin huquqi berildi. Admin panelni ochish uchun /admin yuboring."


class AdminMgr(StatesGroup):
    """FSM for the 'add admin' input."""

    waiting_id = State()


class AdminActCB(CallbackData, prefix="adxa"):
    """Admin-management action (the list itself is opened by ADMINS_CALLBACK)."""

    action: str  # add | ask_del | del | cancel
    user_id: int = 0


def _btn(text: str, callback_data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=callback_data)


def _back_row() -> list[InlineKeyboardButton]:
    return [_btn("\u2b05\ufe0f Orqaga", MenuCB(action="root").pack())]


async def _render_list() -> tuple[str, InlineKeyboardMarkup]:
    """Build the admins screen: head admin, regular admins, add and remove buttons."""
    entries = await list_admins()
    lines = [
        "\U0001f465 <b>Adminlar</b>",
        "",
        f"\U0001f451 Bosh admin: <code>{settings.head_admin_id}</code>",
        "",
    ]
    if entries:
        lines.append(f"Adminlar ({len(entries)}):")
        lines.extend(f"{i}. <code>{e.user_id}</code>" for i, e in enumerate(entries, start=1))
    else:
        lines.append("Boshqa adminlar hali yo'q.")

    rows: list[list[InlineKeyboardButton]] = [[_btn("\u2795 Admin qo'shish", AdminActCB(action="add").pack())]]
    delete_buttons = [
        _btn(f"\U0001f5d1 {e.user_id}", AdminActCB(action="ask_del", user_id=e.user_id).pack()) for e in entries
    ]
    rows.extend(delete_buttons[i : i + 2] for i in range(0, len(delete_buttons), 2))
    rows.append(_back_row())
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _extract_user_id(message: Message) -> tuple[int | None, str | None]:
    """Return (user_id, None) or (None, error text) from a typed ID or a forwarded message."""
    origin = message.forward_origin
    if origin is not None:
        sender = getattr(origin, "sender_user", None)
        if sender is None:
            return None, "Bu xabardan foydalanuvchi ID sini olib bo'lmadi (profil yashirin yoki xabar kanaldan). ID raqamini yozib yuboring."
        if sender.is_bot:
            return None, "Botni admin qilib bo'lmaydi."
        return sender.id, None
    text = (message.text or "").strip()
    if not _ID_RE.match(text):
        return None, "Noto'g'ri ID. Faqat raqamlardan iborat Telegram ID yuboring yoki xabarni forward qiling. Bekor qilish: /cancel"
    return int(text), None


# ---------------------------------------------------------------- list


@router.callback_query(F.data == ADMINS_CALLBACK)
@_db_guard
async def on_list(callback: CallbackQuery, state: FSMContext) -> None:
    """Open the admins screen."""
    await state.clear()
    text, markup = await _render_list()
    await _edit(callback, text, markup)
    await callback.answer()


@router.callback_query(AdminActCB.filter(F.action == "cancel"))
@_db_guard
async def on_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Cancel adding or removing and return to the list."""
    await state.clear()
    text, markup = await _render_list()
    await _edit(callback, text, markup)
    await callback.answer("Bekor qilindi.")


# ---------------------------------------------------------------- add


@router.callback_query(AdminActCB.filter(F.action == "add"))
async def on_add(callback: CallbackQuery, state: FSMContext) -> None:
    """Ask for the new admin's ID."""
    await state.set_state(AdminMgr.waiting_id)
    cancel = InlineKeyboardMarkup(
        inline_keyboard=[[_btn("\u274c Bekor qilish", AdminActCB(action="cancel").pack())]]
    )
    await _edit(callback, _ASK_ID_TEXT, cancel)
    await callback.answer()


@router.message(Command("cancel"), StateFilter(AdminMgr))
@_db_guard
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """Cancel the add-admin input."""
    await state.clear()
    text, markup = await _render_list()
    await message.answer("Bekor qilindi.")
    await message.answer(text, reply_markup=markup, parse_mode="HTML")


@router.message(AdminMgr.waiting_id, F.text | F.forward_origin)
@_db_guard
async def on_id_input(message: Message, state: FSMContext, bot: Bot) -> None:
    """Receive the new admin's ID (typed or from a forwarded message) and store it."""
    new_id, error = _extract_user_id(message)
    if new_id is None:
        await message.answer(error or "Noto'g'ri ID.")
        return
    if is_head_admin(new_id):
        await message.answer("Bu ID bosh adminga (sizga) tegishli, u allaqachon admin.")
        return

    added = await add_admin(new_id, added_by=settings.head_admin_id)
    await state.clear()

    if not added:
        notice = f"\u2139\ufe0f <code>{new_id}</code> allaqachon admin."
    else:
        notice = f"\u2705 <code>{new_id}</code> admin qilib qo'shildi."
        try:
            await bot.send_message(new_id, _NEW_ADMIN_TEXT)
        except TelegramForbiddenError:
            notice += (
                "\n\u26a0\ufe0f U botni hali ishga tushirmagan. Botga kirib /start bosmasa, "
                "buyurtma xabarlari unga yetib bormaydi."
            )
        except TelegramAPIError as exc:
            logger.warning("Could not notify the new admin %s: %s", new_id, exc)
            notice += "\n\u26a0\ufe0f Unga xabar yuborib bo'lmadi (ID to'g'ri ekanini tekshiring)."
    await message.answer(notice, parse_mode="HTML")
    text, markup = await _render_list()
    await message.answer(text, reply_markup=markup, parse_mode="HTML")


@router.message(AdminMgr.waiting_id)
async def on_id_not_text(message: Message) -> None:
    """Anything that is neither text nor a forward while waiting for an ID."""
    await message.answer("ID raqamini matn ko'rinishida yuboring yoki xabarni forward qiling. Bekor qilish: /cancel")


# ---------------------------------------------------------------- remove


@router.callback_query(AdminActCB.filter(F.action == "ask_del"))
async def on_ask_delete(callback: CallbackQuery, callback_data: AdminActCB) -> None:
    """Ask for confirmation before removing an admin."""
    target = callback_data.user_id
    if is_head_admin(target):
        await callback.answer("Bosh adminni o'chirib bo'lmaydi.", show_alert=True)
        return
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _btn("\u2705 Ha, o'chirish", AdminActCB(action="del", user_id=target).pack()),
                _btn("\u274c Bekor qilish", AdminActCB(action="cancel").pack()),
            ]
        ]
    )
    text = f"\U0001f5d1 <code>{target}</code> adminni o'chirasizmi?\nU admin panelga kira olmaydi va buyurtmalarni olmaydi."
    await _edit(callback, text, markup)
    await callback.answer()


@router.callback_query(AdminActCB.filter(F.action == "del"))
@_db_guard
async def on_delete(callback: CallbackQuery, callback_data: AdminActCB, state: FSMContext) -> None:
    """Remove the admin and show the refreshed list."""
    await state.clear()
    try:
        removed = await remove_admin(callback_data.user_id)
    except ValueError:
        await callback.answer("Bosh adminni o'chirib bo'lmaydi.", show_alert=True)
        return
    text, markup = await _render_list()
    await _edit(callback, text, markup)
    await callback.answer("O'chirildi \u2705" if removed else "Allaqachon o'chirilgan.")

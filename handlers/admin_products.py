"""Admin product browser (list, card, actions, typed edits) and the pending-order list."""
from __future__ import annotations

import asyncio
import html
import json
import logging
from typing import TYPE_CHECKING, Any

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from caption import build_caption, parse_ai_json, product_caption_fields, strip_html
from db.models import CATEGORIES
from db.orders import list_pending, set_admin_msgs
from db.posts import live_posts
from db.products import (
    get_product,
    list_products,
    reset_for_reprocess,
    set_category,
    set_status,
    update_fields,
)
from handlers.admin import ORDERS_CALLBACK, PRODUCTS_CALLBACK, MenuCB, _db_guard, _edit, _fmt_dt
from handlers.client import OrderDecisionCB, _build_admin_text
from handlers.filters import AdminFilter
from poster import PublishError, mark_available, mark_sold, publish_product
from repolish import RepolishOutcome, repolish_after_edit
from utils import fire_and_forget
from worker import enqueue

if TYPE_CHECKING:
    from aiogram import Bot

    from db.models import Order, Product

logger = logging.getLogger("handlers.admin_products")

router = Router(name="admin_products")
router.message.filter(AdminFilter(), F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(AdminFilter())

_PAGE_SIZE = 8
_MAX_ORDERS = 20
_SOURCE_PREVIEW_CHARS = 300
_CAPTION_PREVIEW_CHARS = 800

_FILTERS: dict[str, str] = {
    "active": "Faol",
    "sold": "Sotilgan",
    "review": "Tekshiruv",
    "failed": "Xato",
    "all": "Hammasi",
}
_STATUS_ICONS: dict[str, str] = {"active": "\U0001f7e2", "sold": "\U0001f534", "removed": "\u26ab"}
_STATUS_LABELS: dict[str, str] = {
    "active": "faol",
    "sold": "sotilgan",
    "removed": "olib tashlangan",
}
_CATEGORY_LABELS: dict[str, str] = {
    "new": "\U0001f195 Yangi",
    "mid": "\U0001f552 O'rta",
    "old": "\U0001f4e6 Eski",
}
_AI_LABELS: dict[str, str] = {
    "pending": "navbatda",
    "processing": "ishlanmoqda",
    "done": "tayyor",
    "failed": "xato",
    "blocked": "bloklangan",
}
# field -> (label, max length)
_EDIT_FIELDS: dict[str, tuple[str, int]] = {
    "name": ("Nom", 120),
    "price": ("Narx", 60),
    "size": ("O'lcham", 60),
    "fabric": ("Mato", 80),
    "stock": ("Mavjud soni", 60),
}

_BAD_ID_TEXT = "Noto'g'ri identifikator."
_NOT_FOUND_TEXT = "Mahsulot topilmadi."
_BAD_VALUE_TEXT = "Noto'g'ri qiymat."
_STALE_TEXT = "Xabar eskirgan, /admin yuboring."
_CANCELLED_TEXT = "Bekor qilindi."

_PUBLISH_LOCK = asyncio.Lock()


class EditProduct(StatesGroup):
    """FSM for typing a new value of one product field."""

    value = State()


class ListCB(CallbackData, prefix="prl"):
    """Open one page of the product list."""

    flt: str = "active"
    page: int = 0


class ActCB(CallbackData, prefix="pra"):
    """Product card action; flt/page remember which list page to return to."""

    act: str
    pid: int
    arg: str = ""
    flt: str = "active"
    page: int = 0


# ---------------------------------------------------------------- helpers


def _btn(text: str, callback_data: str) -> InlineKeyboardButton:
    """Build one inline button."""
    return InlineKeyboardButton(text=text, callback_data=callback_data)


def _act(action: str, pid: int, flt: str, page: int, arg: str = "") -> str:
    """Pack an ActCB callback string."""
    return ActCB(act=action, pid=pid, arg=arg, flt=flt, page=page).pack()


def _flt(value: str) -> str:
    """Normalise a list filter coming from callback data."""
    return value if value in _FILTERS else "active"


def _menu_row() -> list[InlineKeyboardButton]:
    """Back-to-admin-menu button row."""
    return [_btn("\u2b05\ufe0f Orqaga", MenuCB(action="root").pack())]


def _load_ai(raw: str | None) -> dict[str, Any]:
    """Parse the stored ai_json into a dict; bad data yields {}."""
    return parse_ai_json(raw)


async def _load(callback: CallbackQuery, pid: int) -> Product | None:
    """Validate a product id from callback data and load it, alerting the admin on failure."""
    if pid < 1:
        await callback.answer(_BAD_ID_TEXT, show_alert=True)
        return None
    product = await get_product(pid)
    if product is None:
        await callback.answer(_NOT_FOUND_TEXT, show_alert=True)
        return None
    return product


# ---------------------------------------------------------------- list view


def _item_label(p: Product) -> str:
    """One-line button label for a product in the list."""
    marks = _STATUS_ICONS.get(p.status, "\u2022")
    if p.needs_review:
        marks += "\u26a0\ufe0f"
    if p.ai_status in ("failed", "blocked"):
        marks += "\u2757"
    elif p.ai_status in ("pending", "processing"):
        marks += "\u23f3"
    name = p.name.strip() or "(nomsiz)"
    if len(name) > 24:
        name = name[:23] + "\u2026"
    price = p.price.strip()[:14]
    return f"{marks} #{p.id} {name}" + (f" \u00b7 {price}" if price else "")


async def _list_view(flt: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    """Render one page of the product list with filter, item and navigation buttons."""
    flt = _flt(flt)
    page = max(0, page)
    items, total = await list_products(flt, page * _PAGE_SIZE, _PAGE_SIZE)
    pages = max(1, -(-total // _PAGE_SIZE))
    if page >= pages:
        page = pages - 1
        items, total = await list_products(flt, page * _PAGE_SIZE, _PAGE_SIZE)

    filter_buttons = [
        _btn(f"\u2022 {label}" if key == flt else label, ListCB(flt=key, page=0).pack())
        for key, label in _FILTERS.items()
    ]
    rows: list[list[InlineKeyboardButton]] = [filter_buttons[:3], filter_buttons[3:]]
    rows.extend([_btn(_item_label(p), _act("open", p.id, flt, page))] for p in items)
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(_btn("\u2b05\ufe0f", ListCB(flt=flt, page=page - 1).pack()))
    if page < pages - 1:
        nav.append(_btn("\u27a1\ufe0f", ListCB(flt=flt, page=page + 1).pack()))
    if nav:
        rows.append(nav)
    rows.append(_menu_row())

    text = (
        f"\U0001f4e6 <b>Mahsulotlar</b> \u2014 {_FILTERS[flt]}\n"
        f"Jami: {total} \u00b7 sahifa {page + 1}/{pages}"
    )
    if not items:
        text += "\n\nBu yerda hozircha hech narsa yo'q."
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_list(callback: CallbackQuery, flt: str, page: int) -> None:
    """Render the list into the callback's message."""
    text, markup = await _list_view(flt, page)
    await _edit(callback, text, markup)


# ---------------------------------------------------------------- card view


def _card_text(p: Product, live_count: int, note: str | None = None) -> str:
    """Render the product card as HTML."""
    esc = html.escape
    lines: list[str] = []
    if note:
        lines += [esc(note), ""]
    lines += [
        f"{_STATUS_ICONS.get(p.status, '\u2022')} <b>#{p.id} {esc(p.name.strip() or '(nomsiz)')}</b>",
        "",
        f"\U0001f4b0 Narxi: {esc(p.price.strip() or '\u2014')}",
        f"\U0001f4cf O'lchami: {esc(p.size.strip() or '\u2014')}",
        f"\U0001f9f5 Mato: {esc(p.fabric.strip() or '\u2014')}",
        f"\U0001f4e6 Mavjud: {esc(p.stock.strip() or '\u2014')}",
        "",
        f"Holat: <b>{_STATUS_LABELS.get(p.status, p.status)}</b>",
        f"Toifa: <b>{_CATEGORY_LABELS.get(p.category, p.category)}</b>{' \U0001f512' if p.category_locked else ''}",
        f"AI: <b>{_AI_LABELS.get(p.ai_status, p.ai_status)}</b> (urinish: {p.attempts})",
        f"Postlar: {p.post_count} \u00b7 kanalda jonli: {live_count}",
    ]
    if p.last_posted_at is not None:
        lines.append(f"Oxirgi post: {_fmt_dt(p.last_posted_at)}")
    if p.needs_review:
        lines.append("\u26a0\ufe0f <b>Tekshirish kerak</b> (narx/o'lcham/son manba matnida topilmadi)")
    source = p.original_text.strip()
    if source:
        preview = source[:_SOURCE_PREVIEW_CHARS] + ("\u2026" if len(source) > _SOURCE_PREVIEW_CHARS else "")
        lines += ["", f"<i>Manba:</i> {esc(preview)}"]
    return "\n".join(lines)


def _card_markup(p: Product, flt: str, page: int) -> InlineKeyboardMarkup:
    """Build the action keyboard of a product card."""
    pid = p.id
    rows: list[list[InlineKeyboardButton]] = []

    first: list[InlineKeyboardButton] = []
    if p.status == "active" and p.ai_status == "done":
        first.append(_btn("\U0001f680 Post qilish", _act("post", pid, flt, page)))
    if p.status == "active":
        first.append(_btn("\u274c Tugadi", _act("sold", pid, flt, page)))
    else:
        first.append(_btn("\u2705 Qayta sotuvda", _act("avail", pid, flt, page)))
    rows.append(first)

    rows.append(
        [
            _btn(f"\u2705 {label}" if p.category == key else label, _act("cat", pid, flt, page, key))
            for key, label in _CATEGORY_LABELS.items()
        ]
    )
    if p.category_locked:
        rows.append([_btn("\U0001f513 Toifa qulfini ochish", _act("unlock", pid, flt, page))])

    edit_buttons = [
        _btn(f"\u270f\ufe0f {label}", _act("edit", pid, flt, page, key))
        for key, (label, _limit) in _EDIT_FIELDS.items()
    ]
    rows += [edit_buttons[:3], edit_buttons[3:]]

    ai_row = [_btn("\U0001f916 AI qayta", _act("reai", pid, flt, page))]
    if p.status != "removed":
        ai_row.append(_btn("\U0001f5d1 Olib tashlash", _act("rm", pid, flt, page)))
    rows.append(ai_row)

    extra = [_btn("\U0001f5bc Ko'rish", _act("photo", pid, flt, page))]
    if p.needs_review:
        extra.append(_btn("\u2714\ufe0f Tekshirildi", _act("reviewed", pid, flt, page)))
    rows.append(extra)

    rows.append([_btn("\u2b05\ufe0f Ro'yxat", ListCB(flt=flt, page=page).pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_card(
    callback: CallbackQuery, pid: int, flt: str, page: int, note: str | None = None
) -> None:
    """Render a product card into the callback's message (falls back to the list if it vanished)."""
    flt = _flt(flt)
    product = await get_product(pid)
    if product is None:
        await _show_list(callback, flt, page)
        return
    live = await live_posts(pid)
    await _edit(callback, _card_text(product, len(live), note), _card_markup(product, flt, page))


def _result_note(prefix: str, result: dict[str, int]) -> str:
    """Summarise a mark_sold/mark_available result."""
    note = f"{prefix}: {result.get('edited', 0)} ta post yangilandi"
    failed = result.get("failed", 0)
    return note + (f", {failed} ta xato" if failed else "")


# ---------------------------------------------------------------- list handlers


@router.callback_query(F.data == PRODUCTS_CALLBACK)
@_db_guard
async def on_products_entry(callback: CallbackQuery, state: FSMContext) -> None:
    """Entry from the admin menu: first page of active products."""
    await state.clear()
    await _show_list(callback, "active", 0)
    await callback.answer()


@router.callback_query(ListCB.filter())
@_db_guard
async def on_list(callback: CallbackQuery, callback_data: ListCB, state: FSMContext) -> None:
    """Switch filter or page."""
    await state.clear()
    await _show_list(callback, callback_data.flt, callback_data.page)
    await callback.answer()


# ---------------------------------------------------------------- card handlers


@router.callback_query(ActCB.filter(F.act == "open"))
@_db_guard
async def on_open(callback: CallbackQuery, callback_data: ActCB, state: FSMContext) -> None:
    """Open a product card."""
    await state.clear()
    if await _load(callback, callback_data.pid) is None:
        return
    await _show_card(callback, callback_data.pid, callback_data.flt, callback_data.page)
    await callback.answer()


@router.callback_query(ActCB.filter(F.act == "post"))
@_db_guard
async def on_post(callback: CallbackQuery, callback_data: ActCB, bot: Bot) -> None:
    """Publish this product to the target channel right now."""
    product = await _load(callback, callback_data.pid)
    if product is None:
        return
    if product.status != "active" or product.ai_status != "done":
        await callback.answer("Faqat faol va AI tayyor mahsulotni post qilish mumkin.", show_alert=True)
        return
    if _PUBLISH_LOCK.locked():
        await callback.answer("Boshqa post jarayonda.", show_alert=True)
        return
    async with _PUBLISH_LOCK:
        await callback.answer("Post qilinmoqda\u2026")
        try:
            await publish_product(bot, product)
            note = "\u2705 Post qilindi."
        except PublishError as exc:
            note = f"\u26d4 {exc}"
        except TelegramForbiddenError:
            note = "\u26d4 Bot kanalga yozolmayapti (huquq yo'q)."
        except TelegramAPIError as exc:
            logger.exception("Manual publish of product %s failed", product.id)
            note = f"Xato: {exc}"
    await _show_card(callback, callback_data.pid, callback_data.flt, callback_data.page, note)


@router.callback_query(ActCB.filter(F.act.in_({"sold", "avail"})))
@_db_guard
async def on_sold_toggle(callback: CallbackQuery, callback_data: ActCB, bot: Bot) -> None:
    """Mark the product sold or put it back on sale, updating its live posts."""
    if await _load(callback, callback_data.pid) is None:
        return
    await callback.answer("Bajarilmoqda\u2026")
    sold = callback_data.act == "sold"
    try:
        result = await (mark_sold if sold else mark_available)(bot, callback_data.pid)
        note = _result_note("\u274c Tugadi" if sold else "\u2705 Qayta sotuvda", result)
    except ValueError:
        note = _NOT_FOUND_TEXT
    except TelegramAPIError as exc:
        logger.exception("Status change of product %s failed", callback_data.pid)
        note = f"Xato: {exc}"
    await _show_card(callback, callback_data.pid, callback_data.flt, callback_data.page, note)


@router.callback_query(ActCB.filter(F.act == "cat"))
@_db_guard
async def on_category(callback: CallbackQuery, callback_data: ActCB) -> None:
    """Set the category and lock it against automatic recomputation."""
    if callback_data.arg not in CATEGORIES:
        await callback.answer(_BAD_VALUE_TEXT, show_alert=True)
        return
    if await _load(callback, callback_data.pid) is None:
        return
    await set_category(callback_data.pid, callback_data.arg, lock=True)
    await _show_card(callback, callback_data.pid, callback_data.flt, callback_data.page)
    await callback.answer("Toifa o'zgartirildi (qulflandi).")


@router.callback_query(ActCB.filter(F.act == "unlock"))
@_db_guard
async def on_unlock(callback: CallbackQuery, callback_data: ActCB) -> None:
    """Let the nightly recomputation manage this product's category again."""
    product = await _load(callback, callback_data.pid)
    if product is None:
        return
    await set_category(product.id, product.category, lock=False)
    await _show_card(callback, callback_data.pid, callback_data.flt, callback_data.page)
    await callback.answer("Qulf ochildi.")


@router.callback_query(ActCB.filter(F.act == "rm"))
@_db_guard
async def on_remove_ask(callback: CallbackQuery, callback_data: ActCB) -> None:
    """Ask for confirmation before marking a product removed."""
    product = await _load(callback, callback_data.pid)
    if product is None:
        return
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _btn("\u2705 Ha, olib tashlash", _act("rm_ok", product.id, callback_data.flt, callback_data.page)),
                _btn("\u2b05\ufe0f Yo'q", _act("open", product.id, callback_data.flt, callback_data.page)),
            ]
        ]
    )
    await _edit(
        callback,
        f"\U0001f5d1 <b>#{product.id} {html.escape(product.name.strip() or '(nomsiz)')}</b> "
        "olib tashlansinmi?\n\nMahsulot avtopostdan chiqariladi (kanaldagi postlar o'chirilmaydi).",
        markup,
    )
    await callback.answer()


@router.callback_query(ActCB.filter(F.act == "rm_ok"))
@_db_guard
async def on_remove_confirmed(callback: CallbackQuery, callback_data: ActCB) -> None:
    """Mark the product removed."""
    if await _load(callback, callback_data.pid) is None:
        return
    await set_status(callback_data.pid, "removed")
    await _show_card(
        callback, callback_data.pid, callback_data.flt, callback_data.page, "\U0001f5d1 Olib tashlandi."
    )
    await callback.answer()


@router.callback_query(ActCB.filter(F.act == "reai"))
@_db_guard
async def on_reprocess(callback: CallbackQuery, callback_data: ActCB) -> None:
    """Send the product through the AI pipeline again."""
    product = await _load(callback, callback_data.pid)
    if product is None:
        return
    if product.ai_status == "processing":
        await callback.answer("Hozir AI ishlamoqda, biroz kuting.", show_alert=True)
        return
    await reset_for_reprocess(product.id)
    enqueue(product.id)
    await _show_card(
        callback, callback_data.pid, callback_data.flt, callback_data.page, "\U0001f916 AI qayta ishga tushirildi."
    )
    await callback.answer()


@router.callback_query(ActCB.filter(F.act == "reviewed"))
@_db_guard
async def on_reviewed(callback: CallbackQuery, callback_data: ActCB) -> None:
    """Clear the needs-review flag."""
    if await _load(callback, callback_data.pid) is None:
        return
    await update_fields(callback_data.pid, needs_review=False)
    await _show_card(callback, callback_data.pid, callback_data.flt, callback_data.page)
    await callback.answer("Belgilandi.")


@router.callback_query(ActCB.filter(F.act == "photo"))
@_db_guard
async def on_photo(callback: CallbackQuery, callback_data: ActCB, bot: Bot) -> None:
    """Send the product photo with its caption to the admin."""
    product = await _load(callback, callback_data.pid)
    if product is None:
        return
    chat_id = callback.from_user.id
    caption = product.caption_html or None
    try:
        try:
            await bot.send_photo(chat_id, product.tg_file_id, caption=caption, parse_mode="HTML")
        except TelegramBadRequest as exc:
            if "can't parse entities" not in str(exc).lower() or caption is None:
                raise
            await bot.send_photo(chat_id, product.tg_file_id, caption=strip_html(caption), parse_mode=None)
    except TelegramAPIError:
        logger.exception("Could not send the photo of product %s", product.id)
        await callback.answer("Rasmni yuborib bo'lmadi.", show_alert=True)
        return
    await callback.answer()


# ---------------------------------------------------------------- typed edit


@router.callback_query(ActCB.filter(F.act == "edit"))
@_db_guard
async def on_edit_prompt(callback: CallbackQuery, callback_data: ActCB, state: FSMContext) -> None:
    """Ask the admin to type a new value for one field."""
    field = callback_data.arg
    if field not in _EDIT_FIELDS:
        await callback.answer(_BAD_VALUE_TEXT, show_alert=True)
        return
    product = await _load(callback, callback_data.pid)
    if product is None:
        return
    msg = callback.message
    if not isinstance(msg, Message):
        await callback.answer(_STALE_TEXT, show_alert=True)
        return
    if product.ai_status != "done":
        await callback.answer("Avval AI ishlovi tugashi kerak.", show_alert=True)
        return

    label, limit = _EDIT_FIELDS[field]
    flt = _flt(callback_data.flt)
    await state.set_state(EditProduct.value)
    await state.update_data(pid=product.id, field=field, flt=flt, page=max(0, callback_data.page))
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[_btn("\u274c Bekor qilish", _act("edit_cancel", product.id, flt, callback_data.page))]]
    )
    current = str(getattr(product, field)).strip() or "\u2014"
    try:
        await msg.answer(
            f"\u270f\ufe0f <b>{label}</b> uchun yangi qiymatni yuboring (\u2264 {limit} belgi).\n"
            f"Hozirgi: <code>{html.escape(current)}</code>",
            reply_markup=markup,
            parse_mode="HTML",
        )
    except TelegramAPIError:
        logger.exception("Could not send the edit prompt")
        await state.clear()
    await callback.answer()


@router.callback_query(ActCB.filter(F.act == "edit_cancel"))
@_db_guard
async def on_edit_cancel(callback: CallbackQuery, callback_data: ActCB, state: FSMContext) -> None:
    """Abandon the typed edit and go back to the card."""
    await state.clear()
    await _show_card(callback, callback_data.pid, callback_data.flt, callback_data.page)
    await callback.answer(_CANCELLED_TEXT)


@router.message(Command("cancel"), StateFilter(EditProduct.value))
async def cmd_cancel_edit(message: Message, state: FSMContext) -> None:
    """Abandon the typed edit via /cancel."""
    await state.clear()
    await message.answer(_CANCELLED_TEXT)


_REPOLISH_REASONS: dict[str, str] = {
    "blocked": "kontent AI tomonidan bloklandi",
    "unavailable": "AI hozir mavjud emas",
    "invalid": "AI javobi tekshiruvdan o'tmadi",
    "error": "kutilmagan xato",
    "missing": "mahsulot topilmadi",
}


def _repolish_report(label: str, outcome: RepolishOutcome) -> str:
    """Final admin message after the AI re-polish of an edited product."""
    if outcome.stale:
        return (
            f"\u2705 <b>{label}</b> saqlandi.\n\n"
            "\u2139\ufe0f Shu orada yangi tahrir kiritildi \u2014 post oxirgi tahrirga ko'ra yangilanadi."
        )
    if outcome.polished:
        lines = [f"\u2705 <b>{label}</b> saqlandi va post AI yordamida qayta yozildi."]
    else:
        why = _REPOLISH_REASONS.get(outcome.reason, outcome.reason)
        lines = [
            f"\u2705 <b>{label}</b> saqlandi.",
            f"\u26a0\ufe0f AI taglavhani qayta yozolmadi ({why}); oddiy taglavha ishlatildi.",
        ]
    if outcome.caption_html:
        preview = html.escape(strip_html(outcome.caption_html)[:_CAPTION_PREVIEW_CHARS])
        lines += ["", "<b>Yangi taglavha:</b>", preview]
    if outcome.live_edited or outcome.live_failed:
        live = f"\U0001f4e1 Kanaldagi postlar: {outcome.live_edited} ta yangilandi"
        if outcome.live_failed:
            live += f", {outcome.live_failed} ta xato"
        lines += ["", live]
    return "\n".join(lines)


async def _repolish_and_report(
    bot: Bot, status: Message, pid: int, field: str, old_value: str, label: str, flt: str, page: int
) -> None:
    """Background job: AI re-polish, refresh the live posts, then update the admin's status message."""
    outcome = await repolish_after_edit(bot, pid, field, old_value)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[_btn("\U0001f4e6 Kartaga qaytish", _act("open", pid, flt, page))]]
    )
    try:
        await status.edit_text(_repolish_report(label, outcome), reply_markup=markup, parse_mode="HTML")
    except TelegramAPIError:
        logger.exception("Could not update the re-polish status message of product %s", pid)


@router.message(EditProduct.value, F.text)
@_db_guard
async def on_edit_value(message: Message, state: FSMContext, bot: Bot) -> None:
    """Save the typed value, rebuild the caption, then let the AI polish the post and refresh the channel."""
    data = await state.get_data()
    pid = data.get("pid")
    field = data.get("field")
    if not isinstance(pid, int) or field not in _EDIT_FIELDS:
        await state.clear()
        await message.answer(_STALE_TEXT)
        return
    flt = _flt(str(data.get("flt", "active")))
    page = int(data.get("page", 0))
    label, limit = _EDIT_FIELDS[field]

    value = " ".join((message.text or "").split())
    if not value:
        await message.answer("Bo'sh qiymat bo'lmaydi. Qaytadan yuboring yoki /cancel.")
        return
    if len(value) > limit:
        await message.answer(f"Juda uzun (maksimum {limit} belgi). Qaytadan yuboring yoki /cancel.")
        return

    product = await get_product(pid)
    if product is None or product.ai_status != "done":
        await state.clear()
        await message.answer(_NOT_FOUND_TEXT if product is None else "AI ishlovi hali tugamagan.")
        return

    old_value = str(getattr(product, field))
    fields = product_caption_fields(product)
    fields[field] = value
    caption = build_caption(fields, pid)
    ai_data = _load_ai(product.ai_json)
    ai_data.update(fields)
    # The admin's value is stored first, so nothing is lost if the AI step below fails.
    await update_fields(
        pid,
        **{field: value},
        caption_html=caption,
        ai_json=json.dumps(ai_data, ensure_ascii=False),
    )
    await state.clear()

    live = await live_posts(pid) if product.status in ("active", "sold") else []
    text = f"\u2705 <b>{label}</b> saqlandi.\n\U0001f916 AI postni chiroyli qilib qayta yozmoqda\u2026"
    if live:
        text += f"\nKanaldagi {len(live)} ta post avtomatik yangilanadi."
    status = await message.answer(text, parse_mode="HTML")
    fire_and_forget(_repolish_and_report(bot, status, pid, field, old_value, label, flt, page))


@router.message(StateFilter(EditProduct.value))
async def on_edit_not_text(message: Message) -> None:
    """Remind the admin that the value must be text."""
    await message.answer("Iltimos, matn yuboring yoki /cancel.")


@router.callback_query(ActCB.filter(F.act == "live_yes"))
@_db_guard
async def on_live_update(callback: CallbackQuery, callback_data: ActCB, bot: Bot) -> None:
    """Re-edit the live channel posts with the rebuilt caption."""
    product = await _load(callback, callback_data.pid)
    if product is None:
        return
    if product.status not in ("active", "sold"):
        await callback.answer("Bu mahsulot uchun postlarni yangilab bo'lmaydi.", show_alert=True)
        return
    await callback.answer("Yangilanmoqda\u2026")
    try:
        if product.status == "sold":
            result = await mark_sold(bot, product.id)
        else:
            result = await mark_available(bot, product.id)
        note = _result_note("\U0001f504 Yangilandi", result)
    except ValueError:
        note = _NOT_FOUND_TEXT
    except TelegramAPIError as exc:
        logger.exception("Live post refresh of product %s failed", product.id)
        note = f"Xato: {exc}"
    await _show_card(callback, callback_data.pid, callback_data.flt, callback_data.page, note)


@router.callback_query(ActCB.filter(F.act == "live_no"))
@_db_guard
async def on_live_skip(callback: CallbackQuery, callback_data: ActCB) -> None:
    """Skip the live-post refresh and show the card."""
    if await _load(callback, callback_data.pid) is None:
        return
    await _show_card(callback, callback_data.pid, callback_data.flt, callback_data.page)
    await callback.answer()


# ---------------------------------------------------------------- pending orders


async def _send_order_card(bot: Bot, admin_id: int, order: Order) -> None:
    """Send one pending order with accept/reject buttons and rebind it as this admin's live card."""
    product = await get_product(order.product_id)
    if product is None:
        logger.warning("Order %s references missing product %s", order.order_id, order.product_id)
        return
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _btn(
                    "\u2705 Qabul qildim",
                    OrderDecisionCB(action="accept", order_id=order.order_id).pack(),
                ),
                _btn(
                    "\u274c Qolmagan",
                    OrderDecisionCB(action="reject", order_id=order.order_id).pack(),
                ),
            ]
        ]
    )
    text = _build_admin_text(order, product)
    sent: Message | None = None
    for _attempt in range(2):
        try:
            sent = await bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=markup)
            break
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
        except TelegramAPIError:
            logger.exception("Could not send order #%s card to admin %s", order.order_id, admin_id)
            return
    if sent is None:
        return

    old_id = order.admin_msg_ids.get(admin_id)
    await set_admin_msgs(order.order_id, {**order.admin_msg_ids, admin_id: sent.message_id})
    if old_id is not None and old_id != sent.message_id:
        try:
            await bot.delete_message(admin_id, old_id)
        except TelegramAPIError:
            logger.debug("Old order #%s card %s could not be deleted", order.order_id, old_id)


@router.callback_query(F.data == ORDERS_CALLBACK)
@_db_guard
async def on_orders(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """List pending orders as cards with accept/reject buttons."""
    await state.clear()
    orders = await list_pending(_MAX_ORDERS)
    back = InlineKeyboardMarkup(inline_keyboard=[_menu_row()])
    if not orders:
        await _edit(callback, "\U0001f9fe Kutilayotgan buyurtma yo'q.", back)
        await callback.answer()
        return
    header = f"\U0001f9fe <b>Kutilayotgan buyurtmalar:</b> {len(orders)} ta"
    if len(orders) >= _MAX_ORDERS:
        header += f" (eng eski {_MAX_ORDERS} tasi)"
    await _edit(callback, header, back)
    await callback.answer()
    admin_id = callback.from_user.id
    for order in orders:
        await _send_order_card(bot, admin_id, order)

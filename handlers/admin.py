"""Admin panel: root menu, settings editor, instant posting and status view."""
from __future__ import annotations

import asyncio
import functools
import html
import logging
import os
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from datetime import time as dtime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from apscheduler.jobstores.base import JobLookupError
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from ai.router import router_status
from config import settings
from db.admins import is_head_admin
from db.engine import engine
from db.schema import orders_t, post_log_t
from db.settings import MULTI_MAX, MULTI_MIN, get_settings, update_settings
from handlers.filters import AdminFilter
from scheduler import apply_interval, is_night, post_next
from utils import local_now
from worker import PRODUCT_QUEUE

if TYPE_CHECKING:
    from aiogram import Bot
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from db.models import BotSettings

logger = logging.getLogger("handlers.admin")

router = Router(name="admin")
router.message.filter(AdminFilter(), F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(AdminFilter())

# Callback routes owned by handlers/admin_products.py (step S12).
PRODUCTS_CALLBACK = "prd:list"
ORDERS_CALLBACK = "ord:list"
# Owned by handlers/admin_manage.py; the button is shown to the head admin only.
ADMINS_CALLBACK = "adx:list"
CHATS_CALLBACK = "chx:list"

_POST_JOB_ID = "post_next"  # must match the autopost job id in scheduler.py
_INTERVAL_PRESETS: tuple[int, ...] = (15, 30, 60, 120)
_FIELD_STEPS: dict[str, int] = {
    "new_multi": 1,
    "mid_multi": 1,
    "old_multi": 1,
    "new_keep": 5,
    "mid_keep": 5,
}
_MULTI_KEYS = frozenset({"new_multi", "mid_multi", "old_multi"})
_FIELD_LABELS: dict[str, str] = {
    "new_multi": "\U0001f195 Yangi \u00d7",
    "mid_multi": "\U0001f552 O'rta \u00d7",
    "old_multi": "\U0001f4e6 Eski \u00d7",
    "new_keep": "\U0001f195 Yangi soni: ",
    "mid_keep": "\U0001f552 O'rta soni: ",
}
_POLICY_LABELS: dict[str, str] = {
    "delete_previous": "oldingisini o'chirish",
    "keep": "saqlab qolish",
}
_INT_RE = re.compile(r"^\d{1,4}$")
_MAX_KEY_LINES = 12
_TZ = ZoneInfo(settings.tz)
_POST_LOCK = asyncio.Lock()

_ROOT_TEXT = "\U0001f6e0 <b>Admin panel</b>\n\nKerakli bo'limni tanlang."
_DB_ERROR_TEXT = "Ma'lumotlar bazasi xatosi. Iltimos, keyinroq urinib ko'ring."
_STALE_TEXT = "Xabar eskirgan, /admin yuboring."
_BAD_VALUE_TEXT = "Noto'g'ri qiymat."
_SAVED_TEXT = "Saqlandi \u2705"
_CANCELLED_TEXT = "Bekor qilindi."


class AdminInput(StatesGroup):
    """FSM for typed settings input."""

    interval = State()
    night_start = State()
    night_end = State()


class MenuCB(CallbackData, prefix="adm"):
    """Admin root-menu navigation."""

    action: str  # root | settings | status | postnow | postnow_go


class SetCB(CallbackData, prefix="aset"):
    """Settings-panel action."""

    op: str  # adj | interval | interval_custom | night_start | night_end | auto | policy | cancel | noop
    field: str = ""
    sign: int = 0
    value: int = 0


_PROMPTS: dict[str, tuple[State, str]] = {
    "interval_custom": (
        AdminInput.interval,
        "\u23f1 Yangi intervalni daqiqalarda yuboring (1\u20131440), masalan: <code>45</code>",
    ),
    "night_start": (
        AdminInput.night_start,
        "\U0001f319 Tun boshlanish vaqtini <code>HH:MM</code> formatida yuboring, masalan: <code>23:00</code>",
    ),
    "night_end": (
        AdminInput.night_end,
        "\U0001f305 Tun tugash vaqtini <code>HH:MM</code> formatida yuboring, masalan: <code>08:00</code>",
    ),
}


def _db_guard(fn: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
    """Log a SQLAlchemy error raised by a handler and tell the admin instead of dropping the update."""

    @functools.wraps(fn)
    async def wrapper(event: Message | CallbackQuery, *args: Any, **kwargs: Any) -> None:
        try:
            await fn(event, *args, **kwargs)
        except SQLAlchemyError:
            logger.exception("Database error in %s", fn.__name__)
            try:
                if isinstance(event, CallbackQuery):
                    await event.answer(_DB_ERROR_TEXT, show_alert=True)
                else:
                    await event.answer(_DB_ERROR_TEXT)
            except TelegramAPIError:
                logger.warning("Could not deliver the database error notice", exc_info=True)

    return wrapper


def _btn(text: str, callback_data: str) -> InlineKeyboardButton:
    """Build one inline button."""
    return InlineKeyboardButton(text=text, callback_data=callback_data)


def _root_markup(user_id: int | None = None) -> InlineKeyboardMarkup:
    """Keyboard of the root admin menu, grouped 2-per-row; the admin-management row is head-admin only."""
    rows = [
        [
            _btn("\U0001f4e6 Mahsulotlar", PRODUCTS_CALLBACK),
            _btn("\U0001f9fe Buyurtmalar", ORDERS_CALLBACK),
        ],
        [
            _btn("\u2699\ufe0f Sozlamalar", MenuCB(action="settings").pack()),
            _btn("\U0001f4ca Holat", MenuCB(action="status").pack()),
        ],
        [_btn("\U0001f680 Hoziroq post qilish", MenuCB(action="postnow").pack())],
    ]
    if is_head_admin(user_id):
        rows.append(
            [
                _btn("\U0001f465 Adminlar", ADMINS_CALLBACK),
                _btn("\U0001f4e1 Kanal va guruhlar", CHATS_CALLBACK),
            ]
        )
    rows.append([_btn("\U0001f504 Yangilash", MenuCB(action="root").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _back_markup(*extra: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    """Keyboard with optional extra rows above a back-to-menu button."""
    return InlineKeyboardMarkup(
        inline_keyboard=[*extra, [_btn("\u2b05\ufe0f Orqaga", MenuCB(action="root").pack())]]
    )


def _settings_text(s: BotSettings) -> str:
    """Render the current settings as an HTML message."""
    autopost = "yoqilgan \u2705" if s.autopost_enabled else "o'chirilgan \u26d4"
    policy = _POLICY_LABELS.get(s.repost_policy, s.repost_policy)
    return (
        "\u2699\ufe0f <b>Sozlamalar</b>\n\n"
        f"<b>Post tanlash og'irligi ({MULTI_MIN}\u00d7\u2013{MULTI_MAX}\u00d7):</b>\n"
        f"\U0001f195 Yangi: <b>{s.new_multi}</b> \u00b7 \U0001f552 O'rta: <b>{s.mid_multi}</b> "
        f"\u00b7 \U0001f4e6 Eski: <b>{s.old_multi}</b>\n"
        f"\U0001f522 Toifada qoldirish: yangi <b>{s.new_keep}</b> ta \u00b7 o'rta <b>{s.mid_keep}</b> ta\n\n"
        f"\u23f1 Interval: <b>{s.interval_mins}</b> daqiqa\n"
        f"\U0001f319 Tun: <b>{html.escape(s.night_start)}</b> \u2192 <b>{html.escape(s.night_end)}</b>\n"
        f"\U0001f916 Avtopost: <b>{autopost}</b>\n"
        f"\u267b\ufe0f Qayta post: <b>{html.escape(policy)}</b>"
    )


def _settings_markup(s: BotSettings) -> InlineKeyboardMarkup:
    """Keyboard of the settings panel."""
    rows: list[list[InlineKeyboardButton]] = []
    for field, label in _FIELD_LABELS.items():
        rows.append(
            [
                _btn("\u2796", SetCB(op="adj", field=field, sign=-1).pack()),
                _btn(f"{label}{getattr(s, field)}", SetCB(op="noop").pack()),
                _btn("\u2795", SetCB(op="adj", field=field, sign=1).pack()),
            ]
        )
    rows.append(
        [
            _btn(
                f"{'\u2705 ' if s.interval_mins == m else ''}{m} daq",
                SetCB(op="interval", value=m).pack(),
            )
            for m in _INTERVAL_PRESETS
        ]
    )
    rows.append([_btn("\u270f\ufe0f Boshqa interval", SetCB(op="interval_custom").pack())])
    rows.append(
        [
            _btn(f"\U0001f319 Boshlanish: {s.night_start}", SetCB(op="night_start").pack()),
            _btn(f"\U0001f305 Tugash: {s.night_end}", SetCB(op="night_end").pack()),
        ]
    )
    autopost = "\U0001f916 Avtopost: YOQILGAN \u2705" if s.autopost_enabled else "\U0001f916 Avtopost: O'CHIRILGAN \u26d4"
    rows.append([_btn(autopost, SetCB(op="auto").pack())])
    policy = _POLICY_LABELS.get(s.repost_policy, s.repost_policy)
    rows.append([_btn(f"\u267b\ufe0f Qayta post: {policy}", SetCB(op="policy").pack())])
    return _back_markup(*rows)


def _cancel_markup() -> InlineKeyboardMarkup:
    """Single cancel button shown under typed-input prompts."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[_btn("\u274c Bekor qilish", SetCB(op="cancel").pack())]]
    )


def _to_time(value: str) -> dtime:
    """Parse a validated 'HH:MM' string."""
    hours, minutes = value.split(":")
    return dtime(int(hours), int(minutes))


def _fmt_dt(value: datetime) -> str:
    """Format an aware datetime in the configured timezone."""
    return value.astimezone(_TZ).strftime("%d.%m %H:%M")


def _fmt_bytes(size: int) -> str:
    """Human-readable file size."""
    value = float(size)
    for unit in ("B", "KB", "MB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


async def _edit(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    """Edit the callback's message in place; fall back to a new message if editing fails."""
    msg = callback.message
    if not isinstance(msg, Message):
        return
    try:
        await msg.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
        logger.warning("Could not edit admin message, sending a new one: %s", exc)
        try:
            await msg.answer(text, reply_markup=markup, parse_mode="HTML")
        except TelegramAPIError:
            logger.exception("Could not send admin message")
    except TelegramAPIError:
        logger.exception("Telegram error while editing admin message")


async def _show_settings(callback: CallbackQuery) -> None:
    """Render the settings panel into the callback's message."""
    current = await get_settings()
    await _edit(callback, _settings_text(current), _settings_markup(current))


async def _send_settings(message: Message) -> None:
    """Send the settings panel as a new message."""
    current = await get_settings()
    await message.answer(_settings_text(current), reply_markup=_settings_markup(current), parse_mode="HTML")


async def _reschedule(scheduler: AsyncIOScheduler) -> str | None:
    """Apply the stored interval to the scheduler; returns an error text on failure."""
    try:
        await apply_interval(scheduler)
    except (JobLookupError, ValueError):
        logger.exception("Could not reschedule the autopost job")
        return "Interval saqlandi, lekin rejalashtiruvchi yangilanmadi."
    return None


# ---------------------------------------------------------------- root menu


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    """Open the admin root menu."""
    await state.clear()
    user = message.from_user
    await message.answer(_ROOT_TEXT, reply_markup=_root_markup(user.id if user else None), parse_mode="HTML")


@router.callback_query(MenuCB.filter(F.action == "root"))
async def on_root(callback: CallbackQuery, state: FSMContext) -> None:
    """Return to the root menu."""
    await state.clear()
    await _edit(callback, _ROOT_TEXT, _root_markup(callback.from_user.id))
    await callback.answer()


# ---------------------------------------------------------------- settings


@router.callback_query(MenuCB.filter(F.action == "settings"))
@_db_guard
async def on_settings(callback: CallbackQuery, state: FSMContext) -> None:
    """Open the settings panel."""
    await state.clear()
    await _show_settings(callback)
    await callback.answer()


@router.callback_query(SetCB.filter(F.op == "noop"))
async def on_noop(callback: CallbackQuery) -> None:
    """Acknowledge taps on label-only buttons."""
    await callback.answer()


@router.callback_query(SetCB.filter(F.op == "adj"))
@_db_guard
async def on_adjust(callback: CallbackQuery, callback_data: SetCB) -> None:
    """Step a multiplier or keep-count up or down."""
    step = _FIELD_STEPS.get(callback_data.field)
    if step is None or callback_data.sign not in (-1, 1):
        await callback.answer(_BAD_VALUE_TEXT, show_alert=True)
        return
    current = await get_settings()
    current_value = int(getattr(current, callback_data.field))
    new_value = current_value + callback_data.sign * step
    if callback_data.field in _MULTI_KEYS:
        # A weight stays within MULTI_MIN..MULTI_MAX; a stored value outside it is pulled back in.
        new_value = max(MULTI_MIN, min(MULTI_MAX, new_value))
        if new_value == current_value:
            await callback.answer(f"Chegara: {MULTI_MIN}\u00d7 \u2013 {MULTI_MAX}\u00d7")
            return
    try:
        await update_settings(**{callback_data.field: new_value})
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await _show_settings(callback)
    await callback.answer()


@router.callback_query(SetCB.filter(F.op == "interval"))
@_db_guard
async def on_interval_preset(callback: CallbackQuery, callback_data: SetCB, scheduler: AsyncIOScheduler) -> None:
    """Apply one of the interval presets."""
    if callback_data.value not in _INTERVAL_PRESETS:
        await callback.answer(_BAD_VALUE_TEXT, show_alert=True)
        return
    try:
        await update_settings(interval_mins=callback_data.value)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    warning = await _reschedule(scheduler)
    await callback.answer(warning or _SAVED_TEXT, show_alert=warning is not None)
    await _show_settings(callback)


@router.callback_query(SetCB.filter(F.op.in_(set(_PROMPTS))))
async def on_prompt(callback: CallbackQuery, callback_data: SetCB, state: FSMContext) -> None:
    """Ask the admin to type a custom interval or a night-window time."""
    msg = callback.message
    if not isinstance(msg, Message):
        await callback.answer(_STALE_TEXT, show_alert=True)
        return
    target_state, prompt = _PROMPTS[callback_data.op]
    await state.set_state(target_state)
    try:
        await msg.answer(prompt, reply_markup=_cancel_markup(), parse_mode="HTML")
    except TelegramAPIError:
        logger.exception("Could not send the input prompt")
        await state.clear()
    await callback.answer()


@router.callback_query(SetCB.filter(F.op.in_({"auto", "policy"})))
@_db_guard
async def on_toggle(callback: CallbackQuery, callback_data: SetCB) -> None:
    """Toggle autopost or the repost policy."""
    current = await get_settings()
    if callback_data.op == "auto":
        change: dict[str, Any] = {"autopost_enabled": not current.autopost_enabled}
    else:
        change = {"repost_policy": "keep" if current.repost_policy == "delete_previous" else "delete_previous"}
    try:
        await update_settings(**change)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await _show_settings(callback)
    await callback.answer()


@router.callback_query(SetCB.filter(F.op == "cancel"))
@_db_guard
async def on_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Abandon a typed-input prompt and return to the settings panel."""
    await state.clear()
    await _show_settings(callback)
    await callback.answer(_CANCELLED_TEXT)


@router.message(Command("cancel"), StateFilter(AdminInput))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """Abandon a typed-input prompt via /cancel."""
    await state.clear()
    await message.answer(_CANCELLED_TEXT)


@router.message(AdminInput.interval, F.text)
@_db_guard
async def on_interval_input(message: Message, state: FSMContext, scheduler: AsyncIOScheduler) -> None:
    """Handle a typed custom interval."""
    text = (message.text or "").strip()
    if not _INT_RE.match(text):
        await message.answer("Butun son yuboring (1\u20131440) yoki /cancel.")
        return
    try:
        await update_settings(interval_mins=int(text))
    except ValueError as exc:
        await message.answer(f"{html.escape(str(exc))}\n\nQaytadan yuboring yoki /cancel.", parse_mode="HTML")
        return
    await state.clear()
    warning = await _reschedule(scheduler)
    if warning:
        await message.answer(warning)
    await _send_settings(message)


@router.message(StateFilter(AdminInput.night_start, AdminInput.night_end), F.text)
@_db_guard
async def on_night_input(message: Message, state: FSMContext) -> None:
    """Handle a typed night-window start or end time."""
    field = "night_start" if await state.get_state() == AdminInput.night_start.state else "night_end"
    try:
        await update_settings(**{field: (message.text or "").strip()})
    except ValueError as exc:
        await message.answer(
            f"{html.escape(str(exc))}\n\nQaytadan yuboring (HH:MM) yoki /cancel.", parse_mode="HTML"
        )
        return
    await state.clear()
    await _send_settings(message)


@router.message(StateFilter(AdminInput))
async def on_input_not_text(message: Message) -> None:
    """Remind the admin that typed input must be text."""
    await message.answer("Iltimos, matn yuboring yoki /cancel.")


# ---------------------------------------------------------------- post now


async def _run_post(callback: CallbackQuery, bot: Bot) -> None:
    """Publish the next product right now, ignoring autopost and the night window."""
    if _POST_LOCK.locked():
        await callback.answer("Post allaqachon jarayonda.", show_alert=True)
        return
    async with _POST_LOCK:
        await callback.answer("Post qilinmoqda\u2026")
        try:
            result = await post_next(bot, force=True)
        except Exception:  # noqa: BLE001 - last line of defence so the admin always sees an outcome
            logger.exception("Manual post_next failed")
            result = "Kutilmagan xato, loglarni tekshiring."
    await _edit(callback, f"\U0001f680 {html.escape(result)}", _back_markup())


@router.callback_query(MenuCB.filter(F.action == "postnow"))
@_db_guard
async def on_post_now(callback: CallbackQuery, bot: Bot) -> None:
    """Post immediately; at night, ask for confirmation first."""
    current = await get_settings()
    night = is_night(local_now().time(), _to_time(current.night_start), _to_time(current.night_end))
    if not night:
        await _run_post(callback, bot)
        return
    confirm = _back_markup(
        [_btn("\u2705 Ha, post qilish", MenuCB(action="postnow_go").pack())]
    )
    await _edit(
        callback,
        f"\U0001f319 Hozir tungi vaqt ({html.escape(current.night_start)}\u2013{html.escape(current.night_end)}). "
        "Baribir post qilinsinmi?",
        confirm,
    )
    await callback.answer()


@router.callback_query(MenuCB.filter(F.action == "postnow_go"))
async def on_post_now_confirmed(callback: CallbackQuery, bot: Bot) -> None:
    """Post immediately after the admin confirmed the night-time override."""
    await _run_post(callback, bot)


# ---------------------------------------------------------------- status


async def _today_counts() -> tuple[int, int, int] | None:
    """Return (posts today, orders today, pending orders), or None on a database error."""
    start = local_now().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    try:
        async with engine.connect() as conn:
            posts = (
                await conn.execute(select(func.count()).select_from(post_log_t).where(post_log_t.c.posted_at >= start))
            ).scalar_one()
            orders = (
                await conn.execute(select(func.count()).select_from(orders_t).where(orders_t.c.created_at >= start))
            ).scalar_one()
            pending = (
                await conn.execute(select(func.count()).select_from(orders_t).where(orders_t.c.status == "pending"))
            ).scalar_one()
    except SQLAlchemyError:
        logger.exception("Could not count today's posts and orders")
        return None
    return int(posts), int(orders), int(pending)


async def _db_size() -> str:
    """Size of the SQLite file (plus its WAL), or an em dash when unavailable."""
    path = engine.url.database
    if not path or path == ":memory:" or path.startswith("file:"):
        return "\u2014"

    def _measure() -> int:
        total = 0
        for suffix in ("", "-wal"):
            try:
                total += os.stat(path + suffix).st_size
            except FileNotFoundError:
                continue
        return total

    try:
        return _fmt_bytes(await asyncio.to_thread(_measure))
    except OSError:
        logger.warning("Could not stat the database file", exc_info=True)
        return "\u2014"


def _router_lines() -> list[str]:
    """Summarise the AI router state (labels only, never raw keys)."""
    status = router_status()
    if not status.get("initialized"):
        return ["\U0001f916 <b>AI router</b>", "Ishga tushirilmagan."]
    keys: list[dict[str, Any]] = status.get("keys", [])
    invalid = sum(1 for k in keys if k.get("invalid"))
    exhausted = sum(1 for k in keys if not k.get("invalid") and k.get("exhausted"))
    usage = sum(int(k.get("usage_today", 0)) for k in keys)
    lines = [
        "\U0001f916 <b>AI router</b>",
        f"Kalitlar: {len(keys) - invalid - exhausted} faol \u00b7 {exhausted} limitda \u00b7 {invalid} yaroqsiz "
        f"(jami {len(keys)})",
        f"Bugungi so'rovlar: {usage}",
        f"Yaroqsiz juftliklar: {status.get('bad_pairs', 0)} \u00b7 vaqtincha to'xtatilgan: "
        f"{status.get('cooling_pairs', 0)}",
    ]
    for kind, value in (status.get("sticky") or {}).items():
        lines.append(f"\U0001f4cc {html.escape(str(kind))}: {html.escape(str(value)) if value else '\u2014'}")
    for key in keys[:_MAX_KEY_LINES]:
        if key.get("invalid"):
            mark = "\u26d4"
        elif key.get("exhausted"):
            mark = "\u23f3"
            until = key.get("exhausted_until")
            if until:
                try:
                    mark += f" {_fmt_dt(datetime.fromisoformat(until))}"
                except ValueError:
                    logger.warning("Unparseable exhausted_until: %r", until)
        else:
            mark = "\u2705"
        lines.append(
            f"\u2022 {html.escape(str(key.get('label', '?')))} [{html.escape(str(key.get('provider', '?')))} "
            f"g{key.get('group', '?')}] {mark} \u00b7 {key.get('usage_today', 0)}"
        )
    if len(keys) > _MAX_KEY_LINES:
        lines.append(f"\u2026va yana {len(keys) - _MAX_KEY_LINES} ta kalit")
    return lines


async def _status_text(scheduler: AsyncIOScheduler) -> str:
    """Assemble the full status message."""
    current = await get_settings()
    counts = await _today_counts()
    size = await _db_size()

    job = scheduler.get_job(_POST_JOB_ID)
    next_run = getattr(job, "next_run_time", None) if job is not None else None
    next_text = _fmt_dt(next_run) if next_run is not None else "\u2014"
    if not current.autopost_enabled:
        next_text += " (avtopost o'chirilgan)"

    if counts is None:
        today_lines = ["Postlar (bugun): \u2014", "Buyurtmalar (bugun): \u2014"]
    else:
        posts, orders, pending = counts
        today_lines = [f"Postlar (bugun): {posts}", f"Buyurtmalar (bugun): {orders} \u00b7 kutilmoqda: {pending}"]

    lines = [
        "\U0001f4ca <b>Holat</b>",
        "",
        f"\U0001f4e5 Navbat: {PRODUCT_QUEUE.qsize()}/{PRODUCT_QUEUE.maxsize}",
        *today_lines,
        f"\u23ed Keyingi avtopost: {html.escape(next_text)} ({html.escape(settings.tz)})",
        f"\U0001f4be Baza hajmi: {size}",
        "",
        *_router_lines(),
    ]
    return "\n".join(lines)


@router.callback_query(MenuCB.filter(F.action == "status"))
@_db_guard
async def on_status(callback: CallbackQuery, scheduler: AsyncIOScheduler) -> None:
    """Show queue, AI router, today's activity, next run and DB size."""
    text = await _status_text(scheduler)
    refresh = [_btn("\U0001f504 Yangilash", MenuCB(action="status").pack())]
    await _edit(callback, text, _back_markup(refresh))
    await callback.answer()

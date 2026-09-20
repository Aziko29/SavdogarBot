"""Client-facing flow: deep-link product entry, order comment capture, admin decisions."""
from __future__ import annotations

import html
import logging
import re
import time
from collections import defaultdict, deque
from typing import TYPE_CHECKING, Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from db.admins import admin_ids, is_admin
from db.orders import (
    close_chat_if_open,
    create_order,
    decide_order,
    find_order_id_by_relay,
    get_open_order_for_user,
    get_order,
    recent_pending_exists,
    record_relay_message,
    set_admin_msgs,
    set_chat_open,
)
from db.products import get_product
from handlers.filters import AdminFilter, NotAdminFilter
from poster import mark_sold

if TYPE_CHECKING:
    from aiogram import Bot

    from db.models import Order, Product

logger = logging.getLogger("handlers.client")

router = Router(name="client")
router.message.filter(F.chat.type == "private")

_DEEP_LINK_RE = re.compile(r"^prod_(\d{1,12})$")
_STATE_TTL_SEC = 1800.0
_START_WINDOW_SEC = 60.0
_START_MAX_PER_WINDOW = 5
_VOICE_PLACEHOLDER = "(ovozli xabar)"
_PHOTO_PLACEHOLDER = "(rasm)"
# Telegram messages are capped at 4096 chars; the admin card also carries the product name,
# price and customer link, so the comment itself must stay well under that. HTML-escaping can
# expand raw text up to 5x (each '&' -> '&amp;'), so the raw cap has to leave generous headroom.
_MAX_COMMENT_CHARS = 500
_COMMENT_TRUNCATED_SUFFIX = "\u2026 (qisqartirildi)"
# Rendering safety net inside _build_admin_text (see there); generous enough to never trigger
# for a comment that already went through _clip_comment, but caps runaway data regardless.
_MAX_ESCAPED_COMMENT_CHARS = 3000

_NOT_AVAILABLE_TEXT = "Kechirasiz, ushbu mahsulot mavjud emas / tugagan."
_ASK_COMMENT_TEXT = (
    "Savolingiz yoki izohingizni yozing — matn, ovozli xabar yoki rasm ko'rinishida "
    "yuborishingiz mumkin. Bekor qilish uchun /cancel yozing."
)
_CANCELLED_TEXT = "Bekor qilindi."
_EXPIRED_TEXT = "Vaqt tugadi, iltimos mahsulot havolasini qaytadan bosing."
_DUPLICATE_TEXT = "Siz bu mahsulot uchun allaqachon so'rov yuborgansiz, iltimos javobni kuting."
_ORDER_SENT_TEXT = "So'rovingiz qabul qilindi, tez orada javob beramiz."
_ACCEPTED_USER_TEXT = (
    "Buyurtmangiz qabul qilindi \u2705\nSavollaringizni shu yerga yozishingiz mumkin — "
    "sotuvchi jonli javob beradi."
)
_REJECTED_USER_TEXT = "Kechirasiz, ushbu mahsulot tugagan."
_ALREADY_DECIDED_TEXT = "Allaqachon hal qilingan."
_MARK_SOLD_BUTTON_TEXT = "\u274c Mahsulotni \u00abTugadi\u00bb qilish"
_CLOSE_CHAT_BUTTON_TEXT = "\U0001f512 Suhbatni yakunlash"
_CHAT_CLOSED_ADMIN_TEXT = "Suhbat yakunlandi."
_CHAT_CLOSED_USER_TEXT = "Suhbat sotuvchi tomonidan yakunlandi. Yangi savolingiz bo'lsa, mahsulot havolasi orqali murojaat qiling."
_CHAT_ALREADY_CLOSED_TEXT = "Bu suhbat allaqachon yakunlangan."
_RELAY_HEADER = "\U0001f4ac <b>Buyurtma #{order_id}</b> \u2014 {name} dan xabar:"
_RELAY_SENT_TO_ADMIN_FAIL = "Mijozga yuborib bo'lmadi (u botni bloklagan bo'lishi mumkin)."
_RELAY_DELIVERED_TEXT = "\u2705"
_PLAIN_START_TEXT = (
    "Assalomu alaykum! Mahsulot haqida savol berish yoki buyurtma qoldirish uchun "
    "kanaldagi mahsulot ostidagi havolani bosing."
)


class OrderFlow(StatesGroup):
    """FSM for capturing a customer's question/comment about one product."""

    waiting_comment = State()


class Checkout(StatesGroup):
    """HIDDEN FEATURE: a later delivery-address step. Not yet wired into the order flow."""

    waiting_address = State()


class OrderDecisionCB(CallbackData, prefix="odec"):
    """Admin tapped accept/reject on an order notification."""

    action: str  # "accept" | "reject"
    order_id: int


class MarkSoldCB(CallbackData, prefix="osold"):
    """Admin tapped the follow-up 'mark product sold' button after rejecting an order."""

    product_id: int


class ChatCloseCB(CallbackData, prefix="chatcl"):
    """Admin tapped 'end chat' on an accepted order's live-chat relay."""

    order_id: int


# Sliding-window /start rate limit: at most _START_MAX_PER_WINDOW per _START_WINDOW_SEC, per user.
_start_hits: dict[int, deque[float]] = defaultdict(deque)


def _allow_start(user_id: int) -> bool:
    """True if this user is still under the /start rate limit; records the hit as a side effect."""
    now = time.monotonic()
    hits = _start_hits[user_id]
    while hits and now - hits[0] > _START_WINDOW_SEC:
        hits.popleft()
    if len(hits) >= _START_MAX_PER_WINDOW:
        return False
    hits.append(now)
    return True


def _build_admin_text(order: Order, product: Product, decision_line: str | None = None) -> str:
    """Render the order-notification text shown to admins, optionally with a decision line appended."""
    customer_link = f'<a href="tg://user?id={order.user_id}">{html.escape(order.user_fullname)}</a>'
    lines = [
        f"\U0001f6cd <b>Yangi so'rov #{order.order_id}</b>",
        "",
        f"Mahsulot: <b>{html.escape(product.name)}</b> (id={product.id})",
        f"Narxi: {html.escape(product.price)}",
        "",
        f"Mijoz: {customer_link}",
    ]
    if order.username:
        lines.append(f"@{order.username}")
    # Second line of defense: _clip_comment already caps new comments at intake, but this keeps
    # the card safe even for orders created before that cap existed, or by any future caller.
    escaped_comment = html.escape(order.comment)
    if len(escaped_comment) > _MAX_ESCAPED_COMMENT_CHARS:
        escaped_comment = escaped_comment[:_MAX_ESCAPED_COMMENT_CHARS].rstrip() + "\u2026"
    lines.append(f"Izoh: {escaped_comment}")
    if decision_line:
        lines.append("")
        lines.append(decision_line)
    return "\n".join(lines)


async def _notify_admins(bot: Bot, order_id: int) -> None:
    """Send the order card to every admin and remember which message went to whom."""
    order = await get_order(order_id)
    if order is None:
        logger.warning("Order %s vanished before admin notification", order_id)
        return
    product = await get_product(order.product_id)
    if product is None:
        logger.warning("Order %s references missing product %s", order_id, order.product_id)
        return

    text = _build_admin_text(order, product)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="\u2705 Qabul qildim",
                callback_data=OrderDecisionCB(action="accept", order_id=order_id).pack(),
            ),
            InlineKeyboardButton(
                text="\u274c Qolmagan",
                callback_data=OrderDecisionCB(action="reject", order_id=order_id).pack(),
            ),
        ]]
    )

    admin_msgs: dict[int, int] = {}
    for admin_id in admin_ids():
        try:
            sent = await bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=markup)
            admin_msgs[admin_id] = sent.message_id
        except TelegramForbiddenError:
            logger.warning("Admin %s has not started the bot; order #%s notice not delivered", admin_id, order_id)
        except Exception:
            logger.exception("Failed to notify admin %s about order #%s", admin_id, order_id)
    if admin_msgs:
        await set_admin_msgs(order_id, admin_msgs)


async def _forward_media_to_admins(bot: Bot, message: Message) -> None:
    """Copy the customer's original voice/photo message to every admin (no order-card text)."""
    for admin_id in admin_ids():
        try:
            await bot.copy_message(chat_id=admin_id, from_chat_id=message.chat.id, message_id=message.message_id)
        except TelegramForbiddenError:
            logger.warning("Admin %s has not started the bot; media copy not delivered", admin_id)
        except Exception:
            logger.exception("Failed to copy media to admin %s", admin_id)


@router.message(CommandStart(deep_link=True))
async def cmd_start_deeplink(message: Message, command: CommandObject, state: FSMContext) -> None:
    """Entry point from a product's 'buy / ask' button: `/start prod_<id>`."""
    user = message.from_user
    if user is None or not _allow_start(user.id):
        return

    match = _DEEP_LINK_RE.match(command.args or "")
    if not match:
        return

    product_id = int(match.group(1))
    product = await get_product(product_id)
    if product is None or product.status in ("sold", "removed"):
        await message.answer(_NOT_AVAILABLE_TEXT)
        return

    await state.set_state(OrderFlow.waiting_comment)
    await state.update_data(product_id=product_id, expires_at=time.time() + _STATE_TTL_SEC)
    await message.answer_photo(product.tg_file_id, caption=product.caption_html, parse_mode="HTML")
    await message.answer(_ASK_COMMENT_TEXT)


@router.message(CommandStart(deep_link=False))
async def cmd_start_plain(message: Message) -> None:
    """`/start` with no (or an unrecognized) deep-link payload: greet, no FSM entered."""
    if message.from_user is not None and not _allow_start(message.from_user.id):
        return
    await message.answer(_PLAIN_START_TEXT)


@router.message(Command("cancel"), StateFilter(OrderFlow.waiting_comment, Checkout.waiting_address))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """Abandon whichever step of the order flow the user is currently in."""
    await state.clear()
    await message.answer(_CANCELLED_TEXT)


async def _load_fresh_state(message: Message, state: FSMContext) -> dict[str, Any] | None:
    """Return the FSM data if the 30-minute window hasn't expired; otherwise clear it and tell the user."""
    data = await state.get_data()
    expires_at = data.get("expires_at")
    if not data.get("product_id") or expires_at is None or time.time() > float(expires_at):
        await state.clear()
        await message.answer(_EXPIRED_TEXT)
        return None
    return data


def _clip_comment(text: str, limit: int = _MAX_COMMENT_CHARS) -> str:
    """Cap a customer-supplied comment so the admin card can never exceed Telegram's message limit.

    Without this, a customer could send an arbitrarily long (or HTML-metacharacter-heavy, since
    '<'/'>'/'&' expand up to 5x once escaped) comment that pushes the rendered admin notification
    past 4096 chars; Telegram would then reject bot.send_message for every admin and the order
    would silently sit in the database with nobody notified.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + _COMMENT_TRUNCATED_SUFFIX


async def _finalize_order(message: Message, state: FSMContext, bot: Bot, comment_text: str, *, has_media: bool) -> None:
    """Shared tail for text/voice/photo comments: validate, create the order, notify admins."""
    comment_text = _clip_comment(comment_text)
    data = await _load_fresh_state(message, state)
    if data is None:
        return
    product_id = int(data["product_id"])

    product = await get_product(product_id)
    if product is None or product.status in ("sold", "removed"):
        await state.clear()
        await message.answer(_NOT_AVAILABLE_TEXT)
        return

    user = message.from_user
    if user is None:
        await state.clear()
        return
    if await recent_pending_exists(user.id, product_id):
        await state.clear()
        await message.answer(_DUPLICATE_TEXT)
        return

    order_id = await create_order(
        product_id=product_id,
        user_id=user.id,
        username=user.username,
        fullname=user.full_name,
        comment=comment_text,
    )
    await state.clear()
    await message.answer(_ORDER_SENT_TEXT)

    # HIDDEN FEATURE: re-enable to ask for delivery address
    # await state.set_state(Checkout.waiting_address)
    # await message.answer("Iltimos, yetkazib berish manzilini yuboring")

    await _notify_admins(bot, order_id)
    if has_media:
        await _forward_media_to_admins(bot, message)


@router.message(OrderFlow.waiting_comment, F.text)
async def on_comment_text(message: Message, state: FSMContext, bot: Bot) -> None:
    """The customer's comment arrived as plain text."""
    await _finalize_order(message, state, bot, message.text or "", has_media=False)


@router.message(OrderFlow.waiting_comment, F.voice)
async def on_comment_voice(message: Message, state: FSMContext, bot: Bot) -> None:
    """The customer's comment arrived as a voice message; the audio itself is copied to admins."""
    await _finalize_order(message, state, bot, _VOICE_PLACEHOLDER, has_media=True)


@router.message(OrderFlow.waiting_comment, F.photo)
async def on_comment_photo(message: Message, state: FSMContext, bot: Bot) -> None:
    """The customer's comment arrived as a photo; the photo itself is copied to admins."""
    await _finalize_order(message, state, bot, message.caption or _PHOTO_PLACEHOLDER, has_media=True)


@router.message(Checkout.waiting_address, F.text)
async def on_address(message: Message, state: FSMContext) -> None:
    """HIDDEN FEATURE: collect a delivery address. Unreachable while the transition above stays commented out."""
    await state.update_data(address=message.text)
    await state.clear()
    await message.answer("Manzil qabul qilindi, rahmat!")


@router.callback_query(OrderDecisionCB.filter(), AdminFilter())
async def on_order_decision(callback: CallbackQuery, callback_data: OrderDecisionCB, bot: Bot) -> None:
    """An admin tapped accept/reject on an order notification."""
    order_id = callback_data.order_id
    new_status = "accepted" if callback_data.action == "accept" else "rejected"

    changed = await decide_order(order_id, new_status)
    if not changed:
        await _safe_answer(callback, _ALREADY_DECIDED_TEXT, show_alert=True)
        return

    order = await get_order(order_id)
    if order is None:
        await _safe_answer(callback)
        return
    product = await get_product(order.product_id)

    if new_status == "accepted":
        decision_line = "\u2705 <b>Qabul qilindi</b>"
        user_text = _ACCEPTED_USER_TEXT
        await set_chat_open(order_id, True)
        markup: InlineKeyboardMarkup | None = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(
                    text=_CLOSE_CHAT_BUTTON_TEXT,
                    callback_data=ChatCloseCB(order_id=order_id).pack(),
                )
            ]]
        )
    else:
        decision_line = "\u274c <b>Rad etildi</b>"
        user_text = _REJECTED_USER_TEXT
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(
                    text=_MARK_SOLD_BUTTON_TEXT,
                    callback_data=MarkSoldCB(product_id=order.product_id).pack(),
                )
            ]]
        )

    try:
        await bot.send_message(order.user_id, user_text)
    except TelegramForbiddenError:
        logger.warning("Order #%s: user %s has blocked the bot", order_id, order.user_id)
    except Exception:
        logger.exception("Order #%s: failed to notify user %s", order_id, order.user_id)

    if product is not None:
        new_text = _build_admin_text(order, product, decision_line)
        for admin_id, msg_id in order.admin_msg_ids.items():
            try:
                await bot.edit_message_text(
                    new_text, chat_id=admin_id, message_id=msg_id, parse_mode="HTML", reply_markup=markup
                )
            except TelegramBadRequest as exc:
                if "message is not modified" not in str(exc).lower():
                    logger.warning("Order #%s: failed to edit admin %s copy: %s", order_id, admin_id, exc)
            except Exception:
                logger.exception("Order #%s: failed to edit admin %s copy", order_id, admin_id)
            if new_status == "accepted":
                try:
                    await record_relay_message(order_id, admin_id, msg_id)
                except Exception:
                    logger.exception("Order #%s: failed to register chat relay link for admin %s", order_id, admin_id)

    await _safe_answer(callback)


async def _safe_answer(callback: CallbackQuery, text: str | None = None, show_alert: bool = False) -> None:
    """callback.answer() fails with 'query is too old' if a slow network hiccup delays us past
    Telegram's ~30-60s callback-query window (e.g. the polling timeout/reconnect seen after a
    TelegramNetworkError). The tap was still handled — closing this out shouldn't blow up the
    update as an unhandled error, so swallow just that one expected failure mode.
    """
    try:
        await callback.answer(text, show_alert=show_alert)
    except TelegramBadRequest as exc:
        if "query is too old" in str(exc).lower() or "query id is invalid" in str(exc).lower():
            logger.info("Callback answer skipped (stale query): %s", exc)
        else:
            raise


@router.callback_query(ChatCloseCB.filter(), AdminFilter())
async def on_chat_close(callback: CallbackQuery, callback_data: ChatCloseCB, bot: Bot) -> None:
    """An admin tapped 'end chat'; closes the relay and lets the customer know.

    close_chat_if_open() is atomic, so if two admins tap this within the same instant only one
    of them gets True back — the other sees "already closed" instead of the customer getting
    the close notice twice.
    """
    order = await get_order(callback_data.order_id)
    if order is None:
        await _safe_answer(callback, _CHAT_ALREADY_CLOSED_TEXT, show_alert=True)
        return
    if not await close_chat_if_open(callback_data.order_id):
        await _safe_answer(callback, _CHAT_ALREADY_CLOSED_TEXT, show_alert=True)
        return

    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass

    try:
        await bot.send_message(order.user_id, _CHAT_CLOSED_USER_TEXT)
    except TelegramForbiddenError:
        logger.info("Order #%s: user %s has blocked the bot; close notice not delivered", order.order_id, order.user_id)
    except Exception:
        logger.exception("Order #%s: failed to notify user of chat close", order.order_id)

    await _safe_answer(callback, _CHAT_CLOSED_ADMIN_TEXT)


@router.message(StateFilter(None), F.reply_to_message, AdminFilter(), F.chat.type == "private")
async def on_admin_chat_reply(message: Message, bot: Bot) -> None:
    """Admin replied (Telegram's native reply) to a relayed chat message; forward it to the customer.

    Works for ANY admin replying to ANY message that was part of that order's chat — the
    original order card or a later customer message — so if one admin is unavailable
    (busy, on another chat, etc.), any other admin can pick up the conversation just by
    replying to what they see, with no extra commands to learn.
    """
    reply_to = message.reply_to_message
    if reply_to is None or message.from_user is None:
        return
    order_id = await find_order_id_by_relay(message.from_user.id, reply_to.message_id)
    if order_id is None:
        return  # Not a chat-relay message (e.g. a normal admin-panel reply) — ignore.

    order = await get_order(order_id)
    if order is None or not order.chat_open:
        await message.reply(_CHAT_ALREADY_CLOSED_TEXT)
        return

    try:
        await bot.copy_message(
            chat_id=order.user_id, from_chat_id=message.chat.id, message_id=message.message_id
        )
    except TelegramForbiddenError:
        await message.reply(_RELAY_SENT_TO_ADMIN_FAIL)
        return
    except Exception:
        logger.exception("Order #%s: failed to relay admin reply to customer", order_id)
        await message.reply(_RELAY_SENT_TO_ADMIN_FAIL)
        return

    try:
        await message.reply(_RELAY_DELIVERED_TEXT)
    except Exception:
        pass  # A missing confirmation shouldn't matter; the reply already reached the customer.


# NotAdminFilter is essential: this catch-all matches every private message, and the client router
# runs before the admin routers, so without it an admin's "/admin" would be swallowed right here.
@router.message(
    StateFilter(None),
    F.chat.type == "private",
    NotAdminFilter(),
    F.content_type.in_({"text", "photo", "voice", "video", "document", "video_note", "audio", "sticker"}),
)
async def on_customer_chat_message(message: Message, bot: Bot) -> None:
    """A message from a user with an open order chat; relay it to every admin.

    Broadcasting to *all* admins (not just the one who accepted) means the conversation
    survives any single admin being away, in spam, or offline — whoever is free can reply.
    """
    user = message.from_user
    if user is None or is_admin(user.id):
        return
    order = await get_open_order_for_user(user.id)
    if order is None:
        return  # No open chat for this user — leave the message alone (e.g. spam/small talk).

    header = _RELAY_HEADER.format(order_id=order.order_id, name=html.escape(user.full_name))
    # Every relayed customer message carries its own "end chat" button — sent only into the
    # admin's private chat, so the customer never sees it — so whichever admin picks up the
    # conversation can close it right from the newest message, without scrolling back to the
    # original order card.
    close_markup = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text=_CLOSE_CHAT_BUTTON_TEXT,
                callback_data=ChatCloseCB(order_id=order.order_id).pack(),
            )
        ]]
    )
    for admin_id in admin_ids():
        try:
            await bot.send_message(admin_id, header, parse_mode="HTML")
            sent = await bot.copy_message(
                chat_id=admin_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
                reply_markup=close_markup,
            )
            await record_relay_message(order.order_id, admin_id, sent.message_id)
        except TelegramForbiddenError:
            logger.warning("Order #%s: admin %s has not started the bot; chat message not delivered", order.order_id, admin_id)
        except Exception:
            logger.exception("Order #%s: failed to relay customer message to admin %s", order.order_id, admin_id)


@router.callback_query(MarkSoldCB.filter(), AdminFilter())
async def on_mark_sold(callback: CallbackQuery, callback_data: MarkSoldCB, bot: Bot) -> None:
    """An admin tapped the follow-up button to mark the rejected order's product as sold."""
    try:
        result = await mark_sold(bot, callback_data.product_id)
    except ValueError:
        await callback.answer("Mahsulot topilmadi.", show_alert=True)
        return
    except Exception:
        logger.exception("Failed to mark product %s sold from the order flow", callback_data.product_id)
        await callback.answer("Xatolik yuz berdi.", show_alert=True)
        return

    if callback.message is not None:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
    await callback.answer(f"Belgilandi: {result['edited']} ta post yangilandi.")

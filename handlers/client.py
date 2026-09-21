"""Client-facing flow: deep-link product entry, the order form, admin decisions, order lifecycle and live chat."""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from collections import defaultdict, deque
from typing import TYPE_CHECKING, Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    ReplyParameters,
)

from sqlalchemy.exc import SQLAlchemyError

from bot_commands import ensure_admin_commands
from config import settings
from db.admins import admin_ids, is_admin
from db.orders import (
    cancel_pending_by_user,
    close_chat_if_open,
    create_order,
    decide_order,
    find_order_id_by_relay,
    finish_order,
    get_open_order_for_user,
    get_order,
    list_user_orders,
    recent_pending_exists,
    record_relay_message,
    set_admin_msgs,
    set_chat_open,
)
from db.inquiries import (
    close_inquiry_if_open,
    customer_has_written,
    find_inquiry_user_by_relay,
    get_inquiry_history,
    get_open_inquiry,
    open_inquiry,
    pop_inquiry_notice_cards,
    record_inquiry_history,
    record_inquiry_relay,
    replace_inquiry_notice_cards,
    touch_inquiry,
    try_claim_inquiry,
    try_continue_inquiry,
)
from db.customers import delete_saved_details, get_saved_details, save_details
from db.models import CATEGORIES
from db.products import count_active_by_category, get_product, list_catalog_products
from handlers.filters import AdminFilter, NotAdminFilter
from poster import mark_sold

if TYPE_CHECKING:
    from aiogram import Bot

    from db.inquiries import Inquiry
    from db.models import Order, Product

logger = logging.getLogger("handlers.client")

router = Router(name="client")
router.message.filter(F.chat.type == "private")

_DEEP_LINK_RE = re.compile(r"^prod_(\d{1,12})$")
_PHONE_CHARS_RE = re.compile(r"^[\d\s()+\-.]+$")
_STATE_TTL_SEC = 1800.0  # the order form expires after this much inactivity
_START_WINDOW_SEC = 60.0
_START_MAX_PER_WINDOW = 5
_VOICE_PLACEHOLDER = "(ovozli xabar)"
_PHOTO_PLACEHOLDER = "(rasm)"
_MAX_QUANTITY = 99
_QUANTITY_BUTTONS = (1, 2, 3, 4, 5)
_MIN_ADDRESS_CHARS = 5
_MAX_ADDRESS_CHARS = 300
_MY_ORDERS_LIMIT = 10
# Telegram messages are capped at 4096 chars; the admin card also carries the product name,
# price and customer link, so the comment itself must stay well under that. HTML-escaping can
# expand raw text up to 5x (each '&' -> '&amp;'), so the raw cap has to leave generous headroom.
_MAX_COMMENT_CHARS = 500
_COMMENT_TRUNCATED_SUFFIX = "\u2026 (qisqartirildi)"
# Rendering safety net inside _build_admin_text (see there); generous enough to never trigger
# for a comment that already went through _clip_comment, but caps runaway data regardless.
_MAX_ESCAPED_COMMENT_CHARS = 3000

_NOT_AVAILABLE_TEXT = "Kechirasiz, ushbu mahsulot mavjud emas / tugagan."
_ASK_QUANTITY_TEXT = (
    "Nechta dona buyurtma qilasiz? Tugmani bosing yoki raqam yozing. "
    "Bekor qilish uchun /cancel yozing."
)
_BAD_QUANTITY_TEXT = f"Iltimos, 1 dan {_MAX_QUANTITY} gacha butun son yozing."
_PHONE_BUTTON_TEXT = "\U0001f4f1 Raqamimni yuborish"
_ASK_PHONE_TEXT = (
    "Aloqa uchun telefon raqamingizni yuboring: pastdagi tugmani bosing yoki "
    "raqamni yozing (masalan, +998901234567)."
)
_BAD_PHONE_TEXT = "Raqam noto'g'ri ko'rinadi. Iltimos, +998901234567 ko'rinishida yozing yoki tugmani bosing."
_NOT_OWN_CONTACT_TEXT = "Iltimos, o'zingizning raqamingizni yuboring."
_LOCATION_BUTTON_TEXT = "\U0001f4cd Joylashuvni yuborish"
_PICKUP_BUTTON_TEXT = "\U0001f3ea Olib ketaman"
_PICKUP_ADDRESS = "Olib ketadi (yetkazib berish kerak emas)"
_ASK_ADDRESS_TEXT = (
    "Yetkazib berish manzilini yozing yoki joylashuvingizni yuboring. "
    "O'zingiz olib ketsangiz, «Olib ketaman» tugmasini bosing."
)
_BAD_ADDRESS_TEXT = "Manzil juda qisqa. Iltimos, to'liqroq yozing yoki joylashuvingizni yuboring."
_SKIP_BUTTON_TEXT = "\u27a1\ufe0f O'tkazib yuborish"
_ASK_NOTE_TEXT = (
    "Izoh yoki savolingiz bo'lsa yozing (matn, ovozli xabar yoki rasm). "
    "Bo'lmasa, «O'tkazib yuborish» tugmasini bosing."
)
_DETAILS_SAVED_TEXT = "Ma'lumotlar qabul qilindi \u2705"
_CONFIRM_BUTTON_TEXT = "\u2705 Tasdiqlash"
_CANCEL_BUTTON_TEXT = "\u274c Bekor qilish"
_USE_THE_BUTTONS_TEXT = "Iltimos, yuqoridagi tugmalardan birini bosing yoki bekor qilish uchun /cancel yozing."
_USE_THE_FORM_TEXT = "Iltimos, so'ralgan ma'lumotni yuboring yoki bekor qilish uchun /cancel yozing."
_CANCELLED_TEXT = "Bekor qilindi."
_EXPIRED_TEXT = "Vaqt tugadi, iltimos mahsulot havolasini qaytadan bosing."
_DUPLICATE_TEXT = "Siz bu mahsulot uchun allaqachon buyurtma bergansiz, iltimos javobni kuting."
_ORDER_SENT_TEXT = (
    "Buyurtmangiz #{order_id} yuborildi \u2705 Sotuvchi tez orada javob beradi.\n"
    "Holatini /buyurtmalarim orqali ko'rishingiz mumkin."
)
_ACCEPTED_USER_TEXT = (
    "Buyurtmangiz #{order_id} qabul qilindi \u2705\nSavollaringizni shu yerga yozishingiz mumkin \u2014 "
    "sotuvchi jonli javob beradi."
)
_REJECTED_USER_TEXT = "Kechirasiz, ushbu mahsulot tugagan (buyurtma #{order_id})."
_COMPLETED_USER_TEXT = "Buyurtmangiz #{order_id} bajarildi \U0001f3c1 Xaridingiz uchun rahmat!"
_CANCELLED_USER_TEXT = (
    "Kechirasiz, buyurtmangiz #{order_id} sotuvchi tomonidan bekor qilindi. "
    "Savolingiz bo'lsa, mahsulot havolasi orqali qayta murojaat qiling."
)
_ALREADY_DECIDED_TEXT = "Allaqachon hal qilingan."
_ALREADY_FINISHED_TEXT = "Bu buyurtma allaqachon yakunlangan."
_MARK_SOLD_BUTTON_TEXT = "\U0001f4e6 Mahsulotni \u00abTugadi\u00bb qilish"
_DONE_BUTTON_TEXT = "\u2705 Bajarildi"
_CANCEL_ORDER_BUTTON_TEXT = "\U0001f6ab Bekor qilish"
_CLOSE_CHAT_BUTTON_TEXT = "\U0001f512 Suhbatni yakunlash"
_CHAT_CLOSED_ADMIN_TEXT = "Suhbat yakunlandi."
_CHAT_CLOSED_USER_TEXT = "Suhbat sotuvchi tomonidan yakunlandi. Yangi savolingiz bo'lsa, mahsulot havolasi orqali murojaat qiling."
_CHAT_ALREADY_CLOSED_TEXT = "Bu suhbat allaqachon yakunlangan."
_RELAY_HEADER = "\U0001f4ac <b>Buyurtma #{order_id}</b> \u2014 {name} dan xabar:"
_RELAY_SENT_TO_ADMIN_FAIL = "Mijozga yuborib bo'lmadi (u botni bloklagan bo'lishi mumkin)."
_RELAY_DELIVERED_TEXT = "\u2705"
_NO_ORDERS_TEXT = "Sizda hali buyurtma yo'q. Buyurtma berish uchun kanaldagi mahsulot ostidagi havolani bosing."
_CANNOT_CANCEL_TEXT = "Bu buyurtmani endi bekor qilib bo'lmaydi: sotuvchi uni allaqachon ko'rib chiqqan."
_CANCELLED_BY_USER_TEXT = "Buyurtma bekor qilindi."
_USER_CANCELLED_ADMIN_TEXT = "\U0001f6ab Mijoz buyurtma #{order_id} ni bekor qildi."
_STALE_BUTTON_TEXT = "Bu so'rov allaqachon yakunlangan yoki vaqti o'tgan."
_ORDER_BUTTON_TEXT = "\U0001f6d2 Buyurtma qilish"
_ASK_BUTTON_TEXT = "\U0001f4ac Admin bilan bog'lanish"
_CATALOG_TITLE_TEXT = "\U0001f4c2 <b>Katalog</b>\n\nKategoriyani tanlang:"
_CATALOG_EMPTY_TEXT = "Bu kategoriyada hozircha mahsulot yo'q."
_CATALOG_MENU_BUTTON_TEXT = "\U0001f4c2 Kategoriyalar"
_CATALOG_PREV_BUTTON_TEXT = "\u2b05\ufe0f Oldingi"
_CATALOG_NEXT_BUTTON_TEXT = "Keyingi \u27a1\ufe0f"
_CATALOG_POSITION_LINE = "\n\n\U0001f4c4 {index}/{total}"
_CATEGORY_LABELS: dict[str, str] = {"new": "\U0001f195 Yangi", "mid": "\U0001f538 O'rta", "old": "\u231b Eski"}
_INQUIRY_OPENED_TEXT = (
    "\U0001f4ac Savolingizni yozing (matn, ovozli xabar yoki rasm) \u2014 sotuvchiga yetkazaman va u shu yerda javob beradi.\n"
    "Suhbatni tugatish uchun /cancel yozing."
)
_INQUIRY_HAS_ORDER_CHAT_TEXT = (
    "Buyurtmangiz #{order_id} bo'yicha suhbat ochiq \u2014 savolingizni shu yerga yozing, sotuvchi javob beradi."
)
_INQUIRY_CLOSED_USER_TEXT = (
    "Suhbat sotuvchi tomonidan yakunlandi. Yangi savolingiz bo'lsa, mahsulot havolasini qayta bosing."
)
_INQUIRY_ENDED_BY_USER_TEXT = "Suhbat yakunlandi."
_NOTHING_TO_CANCEL_TEXT = "Bekor qilinadigan narsa yo'q."
_INQUIRY_CLOSED_BY_USER_ADMIN_TEXT = "\U0001f512 Mijoz suhbatni yakunladi."
_INQUIRY_HEADER = "\U0001f4ac <b>Savol</b> \u2014 {name} dan xabar:"
_INQUIRY_CARD_HINT = "Javob berish uchun shu xabarga (yoki mijozning keyingi xabariga) reply qiling."
_CLAIM_BUTTON_TEXT = "\u2705 Qabul qilish"
_INQUIRY_CLAIMED_TEXT = "\u2705 Siz qabul qildingiz. Endi mijozning barcha xabarlari sizga keladi."
_INQUIRY_TAKEN_ALERT_TEXT = "Bu suhbatni {name} allaqachon qabul qildi."
_INQUIRY_TAKEN_REPLY_TEXT = "Bu suhbatni allaqachon {name} qabul qilgan \u2014 javobingiz yuborilmadi."
_INQUIRY_TAKEN_CARD_TEXT = "\U0001f512 Bu suhbatni {name} qabul qildi."
_INQUIRY_PRODUCT_LINE = "Mahsulot: <b>{name}</b> (id={id})\nNarxi: {price}"
_INQUIRY_HISTORY_HEADER = "\U0001f4dc <b>Suhbat tarixi:</b>"
_INQUIRY_NO_HISTORY_TEXT = "(hozircha xabar almashinuvi yo'q)"
_INQUIRY_CONTINUE_BUTTON_TEXT = "\u25b6\ufe0f Davom ettirish"
_INQUIRY_IDLE_TEXT = (
    "\u23f0 <b>Suhbat harakatsiz</b> \u2014 {name} bilan suhbatda ~{hours:g} soatdan beri xabar yo'q."
)
_INQUIRY_IDLE_OWNER_LINE = "Hozir suhbatni olib borayotgan admin: {owner}"
_INQUIRY_IDLE_QUESTION = "Davom ettirasizmi yoki tugatasizmi?"
_INQUIRY_IDLE_TAKEN_CARD_TEXT = "\U0001f512 Suhbatni {name} davom ettirdi."
_INQUIRY_ACTIVE_AGAIN_TEXT = "\u2705 Suhbat yana davom etmoqda \u2014 bu so'rov endi kerak emas."
_INQUIRY_CONTINUED_TEXT = "\u25b6\ufe0f Suhbat sizga o'tdi."
_INQUIRY_CONTINUE_TAKEN_ALERT_TEXT = "Bu suhbat allaqachon davom etmoqda ({name})."
_INQUIRY_CONTINUE_INTRO = "\u25b6\ufe0f <b>Suhbat sizga o'tdi</b>"
_INQUIRY_CONTINUE_READY_TEXT = (
    "\u2705 Endi mijozning yangi xabarlari faqat sizga keladi.\n"
    "Javob berish uchun yuqoridagi xabarlardan biriga (yoki mijozning keyingi xabariga) reply qiling."
)
_INQUIRY_HISTORY_TRUNCATED_TEXT = "(faqat oxirgi {shown} ta xabar ko'rsatildi, jami {total} ta)"
_INQUIRY_HISTORY_MAX = 50  # replayed messages per hand-over; older ones are summarised, not sent
_INQUIRY_SEEN_USER_TEXT = "\u2705 Admin savolingizni ko'rdi, tez orada javob yozadi."
_INQUIRY_JOINED_USER_TEXT = "\u2705 Admin suhbatga qo'shildi. Savolingizni yozing."
_TG_RETRY_CAP_SEC = 30.0  # longest flood-control wait we sit through inside a handler
_SAVED_DETAILS_TEXT = (
    "\U0001f4cb <b>Saqlangan ma'lumotlaringiz</b>\n\n"
    "Telefon: {phone}\nManzil: {address}\n\n"
    "Shu ma'lumotlar bilan davom etamizmi?"
)
_USE_SAVED_BUTTON_TEXT = "\u2705 Ha, shular"
_EDIT_SAVED_BUTTON_TEXT = "\u270f\ufe0f O'zgartirish"
_DETAILS_REMEMBERED_TEXT = (
    "\U0001f4be Telefon va manzilingiz keyingi buyurtma uchun saqlandi (faqat o'zingiz ko'rasiz).\n"
    "Ko'rish yoki o'chirish: /malumotlarim"
)
_MY_DATA_TEXT = "\U0001f4cb <b>Saqlangan ma'lumotlaringiz</b>\n\nTelefon: {phone}\nManzil: {address}"
_NO_SAVED_DATA_TEXT = "Sizning saqlangan ma'lumotingiz yo'q. Ular birinchi buyurtmangizdan keyin saqlanadi."
_DELETE_SAVED_BUTTON_TEXT = "\U0001f5d1 Ma'lumotlarimni o'chirish"
_SAVED_DELETED_TEXT = "\U0001f5d1 Saqlangan ma'lumotlaringiz o'chirildi."
_PLAIN_START_TEXT = (
    "Assalomu alaykum! Buyurtma berish yoki mahsulot haqida savol berish uchun "
    "kanaldagi mahsulot ostidagi havolani bosing.\n"
    "Mahsulotlarni ko'rish: /katalog\n"
    "Buyurtmalaringiz holati: /buyurtmalarim\n"
    "Saqlangan telefon va manzilingiz: /malumotlarim\n"
    "Barcha buyruqlar: chap pastdagi «Menu» tugmasi yoki /yordam"
)
_HELP_TEXT = (
    "\u2139\ufe0f <b>Yordam</b>\n\n"
    "\U0001f4c2 <b>Katalog:</b> /katalog orqali mahsulotlarni kategoriya bo'yicha ko'ring, "
    "\u00abOldingi\u00bb / \u00abKeyingi\u00bb tugmalari bilan varaqlang.\n"
    "\U0001f6d2 <b>Buyurtma berish:</b> mahsulot ostidagi \u00abBuyurtma qilish\u00bb tugmasini tanlang. "
    "Bot sizdan ma'lumotlarni birma-bir so'raydi.\n"
    "\U0001f4ac <b>Savol berish:</b> shu yerdagi \u00abAdmin bilan bog'lanish\u00bb tugmasini tanlang "
    "\u2014 sotuvchi shu yerda javob beradi.\n\n"
    "<b>Buyruqlar:</b>\n"
    "/katalog \u2014 mahsulotlarni kategoriya bo'yicha ko'rish\n"
    "/buyurtmalarim \u2014 buyurtmalaringiz holati (ko'rib chiqilmaganini bekor qilish mumkin)\n"
    "/malumotlarim \u2014 saqlangan telefon va manzilingiz (o'chirish mumkin)\n"
    "/cancel \u2014 buyurtma formasini yoki admin bilan suhbatni bekor qilish\n"
    "/yordam \u2014 shu xabar"
)
_ADMIN_HELP_TEXT = (
    "\u2139\ufe0f <b>Yordam (admin)</b>\n\n"
    "/admin \u2014 admin paneli: buyurtmalar, mahsulotlar, suhbatlar\n"
    "/cancel \u2014 boshlangan kiritishni (masalan, qiymat o'zgartirishni) bekor qilish\n"
    "/yordam \u2014 shu xabar\n\n"
    "Mijoz savoliga javob berish uchun uning xabariga reply qiling."
)


class OrderFlow(StatesGroup):
    """FSM of the order form: quantity -> (saved details?) -> phone -> address -> note -> confirmation."""

    waiting_quantity = State()
    waiting_saved = State()  # only when saved details exist: use them or retype
    waiting_phone = State()
    waiting_address = State()
    waiting_note = State()
    confirming = State()


class QuantityCB(CallbackData, prefix="oqty"):
    """Customer tapped a quantity button in the order form."""

    value: int


class SavedDetailsCB(CallbackData, prefix="osav"):
    """Customer's answer to "use your saved phone and address?" in the order form."""

    action: str  # "use" | "edit"


class MyDataCB(CallbackData, prefix="mydat"):
    """Customer tapped 'delete' on his saved-details screen (/malumotlarim)."""

    action: str  # "delete"


class ConfirmCB(CallbackData, prefix="ocnf"):
    """Customer confirmed or cancelled the order summary."""

    action: str  # "yes" | "no"


class ProductActionCB(CallbackData, prefix="pact"):
    """Customer's choice under a product opened from the channel: order it or ask the admin."""

    action: str  # "order" | "ask"
    product_id: int


class CatalogCB(CallbackData, prefix="katalog"):
    """Customer browsing /katalog: open the category menu, pick a category, or step next/prev."""

    action: str  # "menu" | "cat" | "nav"
    category: str = ""
    index: int = 0


class InquiryCloseCB(CallbackData, prefix="inqcl"):
    """Admin tapped 'end chat' on a customer inquiry (a chat that is not tied to an order)."""

    user_id: int


class InquiryClaimCB(CallbackData, prefix="inqac"):
    """Admin tapped 'Qabul qilish' first: the inquiry is now his alone."""

    user_id: int


class InquiryContinueCB(CallbackData, prefix="inqct"):
    """Admin tapped 'continue' on the 1-hour idle notice, re-claiming a quiet inquiry."""

    user_id: int


class OrderDecisionCB(CallbackData, prefix="odec"):
    """Admin tapped accept/reject on an order notification."""

    action: str  # "accept" | "reject"
    order_id: int


class OrderFinishCB(CallbackData, prefix="ofin"):
    """Admin closed an accepted order as done or cancelled."""

    action: str  # "done" | "cancel"
    order_id: int


class MarkSoldCB(CallbackData, prefix="osold"):
    """Admin tapped the 'mark product sold' button on an order card."""

    product_id: int


class ChatCloseCB(CallbackData, prefix="chatcl"):
    """Admin tapped 'end chat' on an accepted order's live-chat relay."""

    order_id: int


class UserCancelCB(CallbackData, prefix="ucan"):
    """Customer withdrew his own pending order from the /buyurtmalarim list."""

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


def _order_state_line(order: Order) -> str | None:
    """The decision/outcome line shown at the bottom of an admin card; None while the order is pending."""
    if order.status == "accepted":
        if order.outcome == "completed":
            return "\U0001f3c1 <b>Bajarildi</b>"
        if order.outcome == "cancelled":
            return "\U0001f6ab <b>Bekor qilindi</b>"
        return "\u2705 <b>Qabul qilindi</b>"
    if order.status == "rejected":
        if order.outcome == "cancelled":
            return "\U0001f6ab <b>Mijoz bekor qildi</b>"
        return "\u274c <b>Rad etildi</b>"
    return None


def _build_admin_text(order: Order, product: Product, decision_line: str | None = None) -> str:
    """Render the order card shown to admins; the state line is derived from the order unless overridden."""
    customer_link = f'<a href="tg://user?id={order.user_id}">{html.escape(order.user_fullname)}</a>'
    lines = [
        f"\U0001f6cd <b>Yangi buyurtma #{order.order_id}</b>",
        "",
        f"Mahsulot: <b>{html.escape(product.name)}</b> (id={product.id})",
        f"Narxi: {html.escape(product.price)}",
        f"Soni: <b>{order.quantity}</b>",
        "",
        f"Mijoz: {customer_link}",
    ]
    if order.username:
        lines.append(f"@{order.username}")
    if order.phone:
        lines.append(f"Telefon: <code>{html.escape(order.phone)}</code>")
    if order.address:
        lines.append(f"Manzil: {html.escape(order.address)}")
    # Second line of defense: _clip_comment already caps new comments at intake, but this keeps
    # the card safe even for orders created before that cap existed, or by any future caller.
    escaped_comment = html.escape(order.comment)
    if len(escaped_comment) > _MAX_ESCAPED_COMMENT_CHARS:
        escaped_comment = escaped_comment[:_MAX_ESCAPED_COMMENT_CHARS].rstrip() + "\u2026"
    if escaped_comment:
        lines.append(f"Izoh: {escaped_comment}")
    state_line = decision_line if decision_line is not None else _order_state_line(order)
    if state_line:
        lines.append("")
        lines.append(state_line)
    return "\n".join(lines)


def order_markup(order: Order, product: Product | None = None) -> InlineKeyboardMarkup | None:
    """Buttons an admin card should carry for the order's current state (None = no buttons)."""
    rows: list[list[InlineKeyboardButton]] = []
    if order.status == "pending":
        rows.append([
            InlineKeyboardButton(
                text="\u2705 Qabul qildim",
                callback_data=OrderDecisionCB(action="accept", order_id=order.order_id).pack(),
            ),
            InlineKeyboardButton(
                text="\u274c Qolmagan",
                callback_data=OrderDecisionCB(action="reject", order_id=order.order_id).pack(),
            ),
        ])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    sellable = product is None or product.status == "active"
    if order.status == "accepted" and not order.outcome:
        rows.append([
            InlineKeyboardButton(
                text=_DONE_BUTTON_TEXT,
                callback_data=OrderFinishCB(action="done", order_id=order.order_id).pack(),
            ),
            InlineKeyboardButton(
                text=_CANCEL_ORDER_BUTTON_TEXT,
                callback_data=OrderFinishCB(action="cancel", order_id=order.order_id).pack(),
            ),
        ])
        if order.chat_open:
            rows.append([
                InlineKeyboardButton(
                    text=_CLOSE_CHAT_BUTTON_TEXT,
                    callback_data=ChatCloseCB(order_id=order.order_id).pack(),
                )
            ])
    if sellable and not order.outcome and order.status in ("accepted", "rejected"):
        rows.append([
            InlineKeyboardButton(
                text=_MARK_SOLD_BUTTON_TEXT,
                callback_data=MarkSoldCB(product_id=order.product_id).pack(),
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def _edit_admin_cards(
    bot: Bot, order: Order, product: Product | None, markup: InlineKeyboardMarkup | None
) -> None:
    """Rewrite every admin's copy of the order card from the order's current state, with `markup`."""
    if product is None:
        return
    text = _build_admin_text(order, product)
    for admin_id, msg_id in order.admin_msg_ids.items():
        try:
            await bot.edit_message_text(
                text, chat_id=admin_id, message_id=msg_id, parse_mode="HTML", reply_markup=markup
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                logger.warning("Order #%s: failed to edit admin %s copy: %s", order.order_id, admin_id, exc)
        except Exception:
            logger.exception("Order #%s: failed to edit admin %s copy", order.order_id, admin_id)


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
    markup = order_markup(order, product)

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


async def _forward_media_to_admins(bot: Bot, from_chat_id: int, message_id: int) -> None:
    """Copy the customer's original voice/photo message to every admin (no order-card text)."""
    for admin_id in admin_ids():
        try:
            await bot.copy_message(chat_id=admin_id, from_chat_id=from_chat_id, message_id=message_id)
        except TelegramForbiddenError:
            logger.warning("Admin %s has not started the bot; media copy not delivered", admin_id)
        except Exception:
            logger.exception("Failed to copy media to admin %s", admin_id)


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


async def _delete_quiet(message: Message) -> None:
    """Delete a message, ignoring errors (already gone, too old, no rights, etc.)."""
    try:
        await message.delete()
    except TelegramBadRequest:
        pass
    except Exception:
        logger.exception("Failed to delete a catalog message")





def _product_actions_markup(product_id: int) -> InlineKeyboardMarkup:
    """The two choices under a product: order it, or contact the admin."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_ORDER_BUTTON_TEXT,
                    callback_data=ProductActionCB(action="order", product_id=product_id).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text=_ASK_BUTTON_TEXT,
                    callback_data=ProductActionCB(action="ask", product_id=product_id).pack(),
                )
            ],
        ]
    )


def _catalog_menu_markup(counts: dict[str, int]) -> InlineKeyboardMarkup:
    """One button per category, each carrying its current active-product count."""
    rows = [
        [
            InlineKeyboardButton(
                text=f"{_CATEGORY_LABELS[category]} ({counts.get(category, 0)})",
                callback_data=CatalogCB(action="cat", category=category).pack(),
            )
        ]
        for category in CATEGORIES
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _catalog_product_markup(product_id: int, category: str, index: int, total: int) -> InlineKeyboardMarkup:
    """Prev/next (whichever apply) on top, then order/ask, then back to the category menu."""
    nav_row: list[InlineKeyboardButton] = []
    if index > 0:
        nav_row.append(
            InlineKeyboardButton(
                text=_CATALOG_PREV_BUTTON_TEXT,
                callback_data=CatalogCB(action="nav", category=category, index=index - 1).pack(),
            )
        )
    if index < total - 1:
        nav_row.append(
            InlineKeyboardButton(
                text=_CATALOG_NEXT_BUTTON_TEXT,
                callback_data=CatalogCB(action="nav", category=category, index=index + 1).pack(),
            )
        )
    rows: list[list[InlineKeyboardButton]] = [nav_row] if nav_row else []
    rows.append([
        InlineKeyboardButton(
            text=_ORDER_BUTTON_TEXT, callback_data=ProductActionCB(action="order", product_id=product_id).pack()
        )
    ])
    rows.append([
        InlineKeyboardButton(
            text=_ASK_BUTTON_TEXT, callback_data=ProductActionCB(action="ask", product_id=product_id).pack()
        )
    ])
    rows.append([InlineKeyboardButton(text=_CATALOG_MENU_BUTTON_TEXT, callback_data=CatalogCB(action="menu").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("katalog"))
async def cmd_katalog(message: Message, state: FSMContext) -> None:
    """`/katalog`: browse active products by category, one at a time, with next/prev buttons."""
    await state.clear()  # leaves any half-filled order form behind; catalog browsing takes over
    counts = await count_active_by_category()
    await message.answer(_CATALOG_TITLE_TEXT, parse_mode="HTML", reply_markup=_catalog_menu_markup(counts))


@router.callback_query(CatalogCB.filter(F.action == "menu"))
async def on_catalog_menu(callback: CallbackQuery) -> None:
    """Customer tapped 'Kategoriyalar': back to the category menu."""
    message = callback.message
    if not isinstance(message, Message):
        await _safe_answer(callback)
        return
    counts = await count_active_by_category()
    await _safe_answer(callback)
    await message.answer(_CATALOG_TITLE_TEXT, parse_mode="HTML", reply_markup=_catalog_menu_markup(counts))
    await _delete_quiet(message)


@router.callback_query(CatalogCB.filter(F.action.in_({"cat", "nav"})))
async def on_catalog_product(callback: CallbackQuery, callback_data: CatalogCB) -> None:
    """Customer picked a category, or tapped 'Oldingi'/'Keyingi': show the product at that index."""
    message = callback.message
    if not isinstance(message, Message):
        await _safe_answer(callback)
        return
    category = callback_data.category
    if category not in CATEGORIES:
        await _safe_answer(callback)
        return

    index = max(callback_data.index, 0)
    items, total = await list_catalog_products(category, index, 1)
    if not items:
        await _safe_answer(callback, _CATALOG_EMPTY_TEXT, show_alert=True)
        return

    product = items[0]
    caption = product.caption_html + _CATALOG_POSITION_LINE.format(index=index + 1, total=total)
    await _safe_answer(callback)
    await message.answer_photo(
        product.tg_file_id,
        caption=caption,
        parse_mode="HTML",
        reply_markup=_catalog_product_markup(product.id, category, index, total),
    )
    await _delete_quiet(message)


@router.message(CommandStart(deep_link=True))
async def cmd_start_deeplink(message: Message, command: CommandObject, state: FSMContext) -> None:
    """Entry point from a product's button: `/start prod_<id>` shows the product with two choices.

    The customer picks "order" (the order form starts) or "contact the admin" (an inquiry chat opens).
    """
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

    await state.clear()  # a fresh product link abandons any half-filled order form
    await message.answer_photo(
        product.tg_file_id,
        caption=product.caption_html,
        parse_mode="HTML",
        reply_markup=_product_actions_markup(product_id),
    )


@router.callback_query(ProductActionCB.filter(F.action == "order"))
async def on_product_order(
    callback: CallbackQuery, callback_data: ProductActionCB, state: FSMContext
) -> None:
    """The customer chose "order": start the order form (quantity first)."""
    message = callback.message
    if not isinstance(message, Message):
        await _safe_answer(callback)
        return
    product = await get_product(callback_data.product_id)
    if product is None or product.status in ("sold", "removed"):
        await _safe_answer(callback, _NOT_AVAILABLE_TEXT, show_alert=True)
        return

    await _safe_answer(callback)
    await state.set_state(OrderFlow.waiting_quantity)
    await state.set_data({"product_id": product.id, "expires_at": time.time() + _STATE_TTL_SEC})
    await message.answer(_ASK_QUANTITY_TEXT, reply_markup=_quantity_markup())


def _inquiry_product_lines(product: Product | None) -> list[str]:
    """Product info line(s) for an inquiry card/history, with its id (empty list if no product)."""
    if product is None:
        return []
    return [_INQUIRY_PRODUCT_LINE.format(name=html.escape(product.name), id=product.id, price=html.escape(product.price))]


def _inquiry_claim_markup(user_id: int) -> InlineKeyboardMarkup:
    """Buttons on the initial, unclaimed inquiry card: accept it, or end it outright."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=_CLAIM_BUTTON_TEXT, callback_data=InquiryClaimCB(user_id=user_id).pack())],
            [InlineKeyboardButton(text=_CLOSE_CHAT_BUTTON_TEXT, callback_data=InquiryCloseCB(user_id=user_id).pack())],
        ]
    )


def _inquiry_owned_markup(user_id: int) -> InlineKeyboardMarkup:
    """Buttons once an admin owns the chat: just the ability to end it."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text=_CLOSE_CHAT_BUTTON_TEXT, callback_data=InquiryCloseCB(user_id=user_id).pack())
        ]]
    )


async def _disable_other_notice_cards(bot: Bot, cards: list[tuple[int, int]], winner_admin_id: int, taken_text: str) -> None:
    """Replace every OTHER admin's now-stale accept/continue card with a plain "taken" notice."""
    for admin_id, message_id in cards:
        if admin_id == winner_admin_id:
            continue
        try:
            await bot.edit_message_text(taken_text, chat_id=admin_id, message_id=message_id, reply_markup=None)
        except TelegramBadRequest:
            pass  # already edited/deleted by the admin, or the text didn't change; harmless
        except Exception:
            logger.exception("Failed to update inquiry card for admin %s", admin_id)


async def _tg_call(method: Any, *args: Any, **kwargs: Any) -> Any:
    """Run one Telegram call, sitting out a single (bounded) flood-control answer before giving up."""
    try:
        return await method(*args, **kwargs)
    except TelegramRetryAfter as exc:
        await asyncio.sleep(min(float(exc.retry_after), _TG_RETRY_CAP_SEC))
        return await method(*args, **kwargs)


async def _send_inquiry_context(
    bot: Bot, admin_id: int, user_id: int, product: Product | None, customer_name: str = ""
) -> None:
    """Hand a newly (re)claiming admin everything he needs: the customer, the product with its id, the talk so far.

    Every message sent here is registered as belonging to the inquiry, so replying to any of them
    reaches the customer; the last one also carries the "end chat" button.
    """
    customer = html.escape(customer_name) if customer_name else str(user_id)
    intro = [_INQUIRY_CONTINUE_INTRO, "", f'Mijoz: <a href="tg://user?id={user_id}">{customer}</a>']
    intro.extend(_inquiry_product_lines(product))
    try:
        sent = await _tg_call(bot.send_message, admin_id, "\n".join(intro), parse_mode="HTML")
        await record_inquiry_relay(user_id, admin_id, sent.message_id)

        entries = await get_inquiry_history(user_id)
        await _tg_call(bot.send_message, admin_id, _INQUIRY_HISTORY_HEADER, parse_mode="HTML")
        if not entries:
            await _tg_call(bot.send_message, admin_id, _INQUIRY_NO_HISTORY_TEXT)
        if len(entries) > _INQUIRY_HISTORY_MAX:
            note = _INQUIRY_HISTORY_TRUNCATED_TEXT.format(shown=_INQUIRY_HISTORY_MAX, total=len(entries))
            await _tg_call(bot.send_message, admin_id, note)
            entries = entries[-_INQUIRY_HISTORY_MAX:]

        last_sender: str | None = None
        for entry in entries:
            try:
                if entry.sender != last_sender:  # one label per run of messages keeps the replay short
                    label = "\U0001f9d1 Mijoz:" if entry.sender == "customer" else "\U0001f6e1 Admin:"
                    await _tg_call(bot.send_message, admin_id, label)
                    last_sender = entry.sender
                copied = await _tg_call(
                    bot.copy_message, chat_id=admin_id, from_chat_id=entry.chat_id, message_id=entry.message_id
                )
                await record_inquiry_relay(user_id, admin_id, copied.message_id)
            except TelegramForbiddenError:
                raise
            except TelegramBadRequest:
                logger.info("Inquiry of user %s: history message %s is gone; skipped", user_id, entry.message_id)
            except Exception:
                logger.exception("Inquiry of user %s: could not replay one history message to admin %s", user_id, admin_id)

        ready = await _tg_call(
            bot.send_message, admin_id, _INQUIRY_CONTINUE_READY_TEXT, reply_markup=_inquiry_owned_markup(user_id)
        )
        await record_inquiry_relay(user_id, admin_id, ready.message_id)
    except TelegramForbiddenError:
        logger.warning("Admin %s has not started the bot; inquiry history not delivered", admin_id)
    except Exception:
        logger.exception("Failed to send the inquiry context to admin %s for user %s", admin_id, user_id)


async def _notify_admins_inquiry(bot: Bot, user: Any, product: Product | None) -> None:
    """Tell every admin that a customer wants to talk; whoever accepts first owns the chat."""
    customer_link = f'<a href="tg://user?id={user.id}">{html.escape(user.full_name)}</a>'
    lines = ["\U0001f4ac <b>Yangi savol</b>", "", f"Mijoz: {customer_link}"]
    if user.username:
        lines.append(f"@{user.username}")
    lines.extend(_inquiry_product_lines(product))
    lines.extend(["", _INQUIRY_CARD_HINT])
    text = "\n".join(lines)
    markup = _inquiry_claim_markup(user.id)
    cards: list[tuple[int, int]] = []
    for admin_id in admin_ids():
        try:
            sent = await bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=markup)
            await record_inquiry_relay(user.id, admin_id, sent.message_id)
            cards.append((admin_id, sent.message_id))
        except TelegramForbiddenError:
            logger.warning("Admin %s has not started the bot; inquiry notice not delivered", admin_id)
        except Exception:
            logger.exception("Failed to notify admin %s about an inquiry of user %s", admin_id, user.id)
    await replace_inquiry_notice_cards(user.id, cards)


def _inquiry_idle_markup(user_id: int) -> InlineKeyboardMarkup:
    """Buttons on the 1-hour idle notice: keep the chat going (as the tapping admin) or end it."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text=_INQUIRY_CONTINUE_BUTTON_TEXT, callback_data=InquiryContinueCB(user_id=user_id).pack()),
            InlineKeyboardButton(text=_CLOSE_CHAT_BUTTON_TEXT, callback_data=InquiryCloseCB(user_id=user_id).pack()),
        ]]
    )


async def notify_admins_inquiry_idle(bot: Bot, inquiry: Inquiry, product: Product | None) -> int:
    """Ask EVERY admin whether a quiet, claimed inquiry should go on or end; returns how many got the card.

    The cards are tracked, so the first admin to decide (or the chat waking up by itself) can
    retire all the others. Called by the `inquiry_idle` sweep.
    """
    owner = "\u2014"
    if inquiry.claimed_by is not None:
        owner = await _admin_label(bot, inquiry.claimed_by)
    customer = f'<a href="tg://user?id={inquiry.user_id}">{html.escape(inquiry.user_fullname or str(inquiry.user_id))}</a>'
    lines = [_INQUIRY_IDLE_TEXT.format(name=customer, hours=settings.inquiry_idle_hours)]
    lines.extend(_inquiry_product_lines(product))
    lines.extend(["", _INQUIRY_IDLE_OWNER_LINE.format(owner=html.escape(owner)), _INQUIRY_IDLE_QUESTION])
    text = "\n".join(lines)
    markup = _inquiry_idle_markup(inquiry.user_id)

    cards: list[tuple[int, int]] = []
    for admin_id in admin_ids():
        try:
            sent = await _tg_call(bot.send_message, admin_id, text, parse_mode="HTML", reply_markup=markup)
            cards.append((admin_id, sent.message_id))
        except TelegramForbiddenError:
            logger.warning("Admin %s has not started the bot; inquiry idle notice not delivered", admin_id)
        except Exception:
            logger.exception("Failed to send the idle notice of user %s's inquiry to admin %s", inquiry.user_id, admin_id)
    await replace_inquiry_notice_cards(inquiry.user_id, cards)
    return len(cards)


async def _dismiss_idle_cards(bot: Bot, user_id: int) -> None:
    """The chat is active again: retire any outstanding "continue or end?" cards (no-op when there are none)."""
    cards = await pop_inquiry_notice_cards(user_id)
    if cards:
        await _disable_other_notice_cards(bot, cards, 0, _INQUIRY_ACTIVE_AGAIN_TEXT)  # 0: nobody is exempt


@router.callback_query(ProductActionCB.filter(F.action == "ask"))
async def on_product_ask(
    callback: CallbackQuery, callback_data: ProductActionCB, state: FSMContext, bot: Bot
) -> None:
    """The customer chose "contact the admin": open an inquiry chat and tell the admins."""
    message = callback.message
    user = callback.from_user
    if not isinstance(message, Message):
        await _safe_answer(callback)
        return
    product = await get_product(callback_data.product_id)
    if product is None:
        await _safe_answer(callback, _NOT_AVAILABLE_TEXT, show_alert=True)
        return

    await _safe_answer(callback)
    await state.clear()  # leaving an unfinished order form, if any

    open_order = await get_open_order_for_user(user.id)
    if open_order is not None:
        # His messages already reach the admins through the order chat; a second chat would only confuse.
        await message.answer(_INQUIRY_HAS_ORDER_CHAT_TEXT.format(order_id=open_order.order_id), reply_markup=ReplyKeyboardRemove())
        return

    await open_inquiry(user.id, product.id, user.full_name)
    await message.answer(_INQUIRY_OPENED_TEXT, reply_markup=ReplyKeyboardRemove())
    await _notify_admins_inquiry(bot, user, product)


@router.message(CommandStart(deep_link=False))
async def cmd_start_plain(message: Message, bot: Bot) -> None:
    """`/start` with no (or an unrecognized) deep-link payload: greet, no FSM entered."""
    user = message.from_user
    if user is not None and not _allow_start(user.id):
        return
    if user is not None and is_admin(user.id):
        # An admin who had never pressed Start had no chat to attach his menu to; now he has one.
        await ensure_admin_commands(bot, user.id)
    await message.answer(_PLAIN_START_TEXT)


@router.message(Command("yordam", "help"))
async def cmd_help(message: Message) -> None:
    """`/yordam`: what the bot does and every command; works in any state and never touches the form."""
    user = message.from_user
    text = _ADMIN_HELP_TEXT if user is not None and is_admin(user.id) else _HELP_TEXT
    await message.answer(text, parse_mode="HTML")


def _user_status_text(order: Order) -> str:
    """The customer-facing status of one order."""
    if order.status == "pending":
        return "\u23f3 ko'rib chiqilmoqda"
    if order.status == "accepted":
        if order.outcome == "completed":
            return "\U0001f3c1 bajarildi"
        if order.outcome == "cancelled":
            return "\U0001f6ab sotuvchi bekor qildi"
        return "\u2705 qabul qilindi"
    if order.outcome == "cancelled":
        return "\U0001f6ab siz bekor qildingiz"
    return "\u274c mahsulot qolmagan"


async def _my_orders_view(user_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    """Text and cancel buttons of the customer's latest orders."""
    orders = await list_user_orders(user_id, _MY_ORDERS_LIMIT)
    if not orders:
        return _NO_ORDERS_TEXT, None
    lines = ["\U0001f9fe <b>Buyurtmalaringiz</b>", ""]
    rows: list[list[InlineKeyboardButton]] = []
    for order in orders:
        product = await get_product(order.product_id)
        name = html.escape(product.name) if product is not None else f"id={order.product_id}"
        lines.append(f"#{order.order_id} \u00b7 {name} \u00d7 {order.quantity} \u2014 {_user_status_text(order)}")
        if order.status == "pending":
            rows.append([
                InlineKeyboardButton(
                    text=f"\U0001f6ab #{order.order_id} ni bekor qilish",
                    callback_data=UserCancelCB(order_id=order.order_id).pack(),
                )
            ])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


@router.message(Command("buyurtmalarim"))
async def cmd_my_orders(message: Message) -> None:
    """`/buyurtmalarim`: the customer's latest orders with their status."""
    if message.from_user is None:
        return
    text, markup = await _my_orders_view(message.from_user.id)
    await message.answer(text, parse_mode="HTML", reply_markup=markup)


@router.message(Command("malumotlarim"))
async def cmd_my_data(message: Message) -> None:
    """Show the customer the phone/address we saved for him, with a button to delete them."""
    user = message.from_user
    if user is None:
        return
    try:
        saved = await get_saved_details(user.id)
    except SQLAlchemyError:
        await message.answer("Ma'lumotlar bazasi xatosi. Iltimos, keyinroq urinib ko'ring.")
        return
    if saved is None or not (saved.phone or saved.address):
        await message.answer(_NO_SAVED_DATA_TEXT)
        return
    text = _MY_DATA_TEXT.format(
        phone=html.escape(saved.phone) or "\u2014", address=html.escape(saved.address) or "\u2014"
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=_DELETE_SAVED_BUTTON_TEXT, callback_data=MyDataCB(action="delete").pack())]]
    )
    await message.answer(text, parse_mode="HTML", reply_markup=markup)


@router.callback_query(MyDataCB.filter(F.action == "delete"))
async def on_my_data_delete(callback: CallbackQuery) -> None:
    """The customer asked us to forget his saved phone/address."""
    try:
        await delete_saved_details(callback.from_user.id)
    except SQLAlchemyError:
        await _safe_answer(callback, "Ma'lumotlar bazasi xatosi. Iltimos, keyinroq urinib ko'ring.", show_alert=True)
        return
    await _safe_answer(callback, _SAVED_DELETED_TEXT)
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(_SAVED_DELETED_TEXT)
        except TelegramBadRequest:
            pass


@router.callback_query(UserCancelCB.filter())
async def on_user_cancel(callback: CallbackQuery, callback_data: UserCancelCB, bot: Bot) -> None:
    """The customer withdrew a pending order; admins' cards are updated and told."""
    user = callback.from_user
    order_id = callback_data.order_id
    if await cancel_pending_by_user(order_id, user.id):
        await _safe_answer(callback, _CANCELLED_BY_USER_TEXT)
        order = await get_order(order_id)
        if order is not None:
            product = await get_product(order.product_id)
            await _edit_admin_cards(bot, order, product, None)
            for admin_id, card_id in order.admin_msg_ids.items():
                try:
                    await bot.send_message(
                        admin_id,
                        _USER_CANCELLED_ADMIN_TEXT.format(order_id=order_id),
                        reply_parameters=ReplyParameters(message_id=card_id, allow_sending_without_reply=True),
                    )
                except Exception:
                    logger.exception("Order #%s: failed to tell admin %s about the cancellation", order_id, admin_id)
    else:
        await _safe_answer(callback, _CANNOT_CANCEL_TEXT, show_alert=True)

    if isinstance(callback.message, Message):
        text, markup = await _my_orders_view(user.id)
        try:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                logger.warning("Could not refresh the order list of user %s: %s", user.id, exc)


@router.message(Command("cancel"), StateFilter(*OrderFlow.__all_states__))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """Abandon whichever step of the order form the user is currently in."""
    await state.clear()
    await message.answer(_CANCELLED_TEXT, reply_markup=ReplyKeyboardRemove())


@router.message(Command("cancel"), StateFilter(None), NotAdminFilter())
async def cmd_cancel_inquiry(message: Message, bot: Bot) -> None:
    """`/cancel` outside the order form: the customer ends his inquiry chat with the admins."""
    user = message.from_user
    if user is None:
        return
    if not await close_inquiry_if_open(user.id):
        await message.answer(_NOTHING_TO_CANCEL_TEXT)
        return
    await message.answer(_INQUIRY_ENDED_BY_USER_TEXT, reply_markup=ReplyKeyboardRemove())
    for admin_id in admin_ids():
        try:
            await bot.send_message(admin_id, f"{_INQUIRY_CLOSED_BY_USER_ADMIN_TEXT} ({html.escape(user.full_name)})", parse_mode="HTML")
        except Exception:
            logger.debug("Could not tell admin %s that user %s ended the inquiry", admin_id, user.id)


# ------------------------------------------------------------------ the order form


def _quantity_markup() -> InlineKeyboardMarkup:
    """Quick quantity buttons 1..5 (any other number can be typed)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text=str(n), callback_data=QuantityCB(value=n).pack())
            for n in _QUANTITY_BUTTONS
        ]]
    )


def _phone_markup() -> ReplyKeyboardMarkup:
    """Reply keyboard with the 'share my contact' button."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=_PHONE_BUTTON_TEXT, request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _address_markup() -> ReplyKeyboardMarkup:
    """Reply keyboard: share location, or pick the shop up in person."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=_LOCATION_BUTTON_TEXT, request_location=True)],
            [KeyboardButton(text=_PICKUP_BUTTON_TEXT)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _note_markup() -> ReplyKeyboardMarkup:
    """Reply keyboard with the 'skip' button of the optional note step."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=_SKIP_BUTTON_TEXT)]], resize_keyboard=True, one_time_keyboard=True
    )


def _confirm_markup() -> InlineKeyboardMarkup:
    """Confirm / cancel buttons under the order summary."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text=_CONFIRM_BUTTON_TEXT, callback_data=ConfirmCB(action="yes").pack()),
            InlineKeyboardButton(text=_CANCEL_BUTTON_TEXT, callback_data=ConfirmCB(action="no").pack()),
        ]]
    )


async def _fresh_data(state: FSMContext) -> dict[str, Any] | None:
    """Return the form data while the inactivity window is open; otherwise clear the state and return None."""
    data = await state.get_data()
    expires_at = data.get("expires_at")
    if not data.get("product_id") or expires_at is None or time.time() > float(expires_at):
        await state.clear()
        return None
    return data


async def _touch(state: FSMContext, **fields: Any) -> None:
    """Store form fields and restart the inactivity window."""
    await state.update_data(expires_at=time.time() + _STATE_TTL_SEC, **fields)


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


def _normalize_phone(raw: str) -> str | None:
    """Turn a typed or shared phone number into '+<digits>'; None if it does not look like a phone number."""
    if not _PHONE_CHARS_RE.match(raw):
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 9:  # local Uzbek format without the country code, e.g. 90 123 45 67
        digits = "998" + digits
    if not 10 <= len(digits) <= 15:
        return None
    return "+" + digits


def _saved_markup() -> InlineKeyboardMarkup:
    """'Use my saved details' / 'change them' buttons."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text=_USE_SAVED_BUTTON_TEXT, callback_data=SavedDetailsCB(action="use").pack()),
            InlineKeyboardButton(text=_EDIT_SAVED_BUTTON_TEXT, callback_data=SavedDetailsCB(action="edit").pack()),
        ]]
    )


async def _accept_quantity(message: Message, state: FSMContext, quantity: int) -> None:
    """Store the quantity, then offer the saved phone/address (repeat customer) or ask for the phone number.

    `message` may be the bot's own prompt (when a quantity button was tapped), so the customer is
    identified by the chat id, which in this private-chat-only router is his user id.
    """
    await _touch(state, quantity=quantity)
    try:
        saved = await get_saved_details(message.chat.id)
    except SQLAlchemyError:
        saved = None  # the form must keep working without the shortcut
    if saved is not None and saved.usable:
        await _touch(state, saved_phone=saved.phone, saved_address=saved.address)
        await state.set_state(OrderFlow.waiting_saved)
        text = _SAVED_DETAILS_TEXT.format(phone=html.escape(saved.phone), address=html.escape(saved.address))
        await message.answer(text, parse_mode="HTML", reply_markup=_saved_markup())
        return
    await state.set_state(OrderFlow.waiting_phone)
    await message.answer(_ASK_PHONE_TEXT, reply_markup=_phone_markup())


async def _accept_phone(message: Message, state: FSMContext, phone: str) -> None:
    """Store the phone number and ask for the delivery address."""
    await _touch(state, phone=phone)
    await state.set_state(OrderFlow.waiting_address)
    await message.answer(_ASK_ADDRESS_TEXT, reply_markup=_address_markup())


async def _accept_address(message: Message, state: FSMContext, address: str) -> None:
    """Store the address and ask for the optional note."""
    await _touch(state, address=address)
    await state.set_state(OrderFlow.waiting_note)
    await message.answer(_ASK_NOTE_TEXT, reply_markup=_note_markup())


def _build_summary_text(product: Product, data: dict[str, Any]) -> str:
    """The order summary the customer confirms."""
    lines = [
        "\U0001f9fe <b>Buyurtmangizni tekshiring</b>",
        "",
        f"Mahsulot: <b>{html.escape(product.name)}</b>",
        f"Narxi: {html.escape(product.price)}",
        f"Soni: {int(data['quantity'])}",
        f"Telefon: {html.escape(str(data['phone']))}",
        f"Manzil: {html.escape(str(data['address']))}",
    ]
    note = str(data.get("note") or "")
    if note:
        lines.append(f"Izoh: {html.escape(note)}")
    lines.append("")
    lines.append("Hammasi to'g'rimi?")
    return "\n".join(lines)


async def _show_summary(message: Message, state: FSMContext) -> None:
    """Show the collected details and ask for the final confirmation."""
    data = await _fresh_data(state)
    if data is None:
        await message.answer(_EXPIRED_TEXT, reply_markup=ReplyKeyboardRemove())
        return
    product = await get_product(int(data["product_id"]))
    if product is None or product.status in ("sold", "removed"):
        await state.clear()
        await message.answer(_NOT_AVAILABLE_TEXT, reply_markup=ReplyKeyboardRemove())
        return
    await state.set_state(OrderFlow.confirming)
    await message.answer(_DETAILS_SAVED_TEXT, reply_markup=ReplyKeyboardRemove())
    await message.answer(_build_summary_text(product, data), parse_mode="HTML", reply_markup=_confirm_markup())


@router.callback_query(OrderFlow.waiting_quantity, QuantityCB.filter())
async def on_quantity_button(callback: CallbackQuery, callback_data: QuantityCB, state: FSMContext) -> None:
    """The customer tapped a quantity button."""
    message = callback.message
    if not isinstance(message, Message) or not 1 <= callback_data.value <= _MAX_QUANTITY:
        await _safe_answer(callback)
        return
    if await _fresh_data(state) is None:
        await _safe_answer(callback, _EXPIRED_TEXT, show_alert=True)
        return
    await _safe_answer(callback)
    try:
        await message.edit_text(f"Soni: <b>{callback_data.value}</b> dona", parse_mode="HTML")
    except TelegramBadRequest:
        pass  # the prompt is already gone or unchanged; nothing to clean up
    await _accept_quantity(message, state, callback_data.value)


@router.message(OrderFlow.waiting_quantity, F.text)
async def on_quantity_text(message: Message, state: FSMContext) -> None:
    """The customer typed the quantity."""
    if await _fresh_data(state) is None:
        await message.answer(_EXPIRED_TEXT)
        return
    raw = (message.text or "").strip()
    if len(raw) > 3 or not raw.isdecimal() or not 1 <= int(raw) <= _MAX_QUANTITY:
        await message.answer(_BAD_QUANTITY_TEXT)
        return
    await _accept_quantity(message, state, int(raw))


@router.callback_query(OrderFlow.waiting_saved, SavedDetailsCB.filter())
async def on_saved_details(callback: CallbackQuery, callback_data: SavedDetailsCB, state: FSMContext) -> None:
    """The customer answered "use the saved phone and address?": skip those two steps, or retype them."""
    message = callback.message
    if not isinstance(message, Message):
        await _safe_answer(callback)
        return
    data = await _fresh_data(state)
    if data is None:
        await _safe_answer(callback, _EXPIRED_TEXT, show_alert=True)
        return
    await _safe_answer(callback)
    try:
        await message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass  # the buttons are already gone

    phone, address = str(data.get("saved_phone") or ""), str(data.get("saved_address") or "")
    if callback_data.action == "use" and phone and address:
        await _touch(state, phone=phone, address=address)
        await state.set_state(OrderFlow.waiting_note)
        await message.answer(_ASK_NOTE_TEXT, reply_markup=_note_markup())
        return
    await state.set_state(OrderFlow.waiting_phone)
    await message.answer(_ASK_PHONE_TEXT, reply_markup=_phone_markup())


@router.message(OrderFlow.waiting_saved)
async def on_saved_details_text(message: Message) -> None:
    """Typing instead of tapping at the saved-details question: point back at the buttons."""
    await message.answer(_USE_THE_BUTTONS_TEXT)


@router.message(OrderFlow.waiting_phone, F.contact)
async def on_phone_contact(message: Message, state: FSMContext) -> None:
    """The customer shared his Telegram contact."""
    if await _fresh_data(state) is None:
        await message.answer(_EXPIRED_TEXT, reply_markup=ReplyKeyboardRemove())
        return
    contact, user = message.contact, message.from_user
    if contact is None or user is None:
        return
    if contact.user_id is not None and contact.user_id != user.id:
        await message.answer(_NOT_OWN_CONTACT_TEXT, reply_markup=_phone_markup())
        return
    phone = _normalize_phone(contact.phone_number)
    if phone is None:
        await message.answer(_BAD_PHONE_TEXT, reply_markup=_phone_markup())
        return
    await _accept_phone(message, state, phone)


@router.message(OrderFlow.waiting_phone, F.text)
async def on_phone_text(message: Message, state: FSMContext) -> None:
    """The customer typed his phone number."""
    if await _fresh_data(state) is None:
        await message.answer(_EXPIRED_TEXT, reply_markup=ReplyKeyboardRemove())
        return
    phone = _normalize_phone((message.text or "").strip())
    if phone is None:
        await message.answer(_BAD_PHONE_TEXT, reply_markup=_phone_markup())
        return
    await _accept_phone(message, state, phone)


@router.message(OrderFlow.waiting_address, F.location)
async def on_address_location(message: Message, state: FSMContext) -> None:
    """The customer shared his location; stored as a map link."""
    if await _fresh_data(state) is None:
        await message.answer(_EXPIRED_TEXT, reply_markup=ReplyKeyboardRemove())
        return
    if message.location is None:
        return
    link = f"https://maps.google.com/?q={message.location.latitude},{message.location.longitude}"
    await _accept_address(message, state, f"\U0001f4cd {link}")


@router.message(OrderFlow.waiting_address, F.text)
async def on_address_text(message: Message, state: FSMContext) -> None:
    """The customer typed the delivery address, or chose to pick the order up himself."""
    if await _fresh_data(state) is None:
        await message.answer(_EXPIRED_TEXT, reply_markup=ReplyKeyboardRemove())
        return
    text = (message.text or "").strip()
    if text == _PICKUP_BUTTON_TEXT:
        await _accept_address(message, state, _PICKUP_ADDRESS)
        return
    if len(text) < _MIN_ADDRESS_CHARS:
        await message.answer(_BAD_ADDRESS_TEXT, reply_markup=_address_markup())
        return
    await _accept_address(message, state, text[:_MAX_ADDRESS_CHARS])


@router.message(OrderFlow.waiting_note, F.text)
async def on_note_text(message: Message, state: FSMContext) -> None:
    """The optional note arrived as text (or the customer skipped the step)."""
    if await _fresh_data(state) is None:
        await message.answer(_EXPIRED_TEXT, reply_markup=ReplyKeyboardRemove())
        return
    text = (message.text or "").strip()
    note = "" if text == _SKIP_BUTTON_TEXT else _clip_comment(text)
    await _touch(state, note=note, media_msg_id=None)
    await _show_summary(message, state)


@router.message(OrderFlow.waiting_note, F.voice)
async def on_note_voice(message: Message, state: FSMContext) -> None:
    """The optional note arrived as a voice message; the audio itself is copied to the admins later."""
    if await _fresh_data(state) is None:
        await message.answer(_EXPIRED_TEXT, reply_markup=ReplyKeyboardRemove())
        return
    await _touch(state, note=_VOICE_PLACEHOLDER, media_msg_id=message.message_id)
    await _show_summary(message, state)


@router.message(OrderFlow.waiting_note, F.photo)
async def on_note_photo(message: Message, state: FSMContext) -> None:
    """The optional note arrived as a photo; the photo itself is copied to the admins later."""
    if await _fresh_data(state) is None:
        await message.answer(_EXPIRED_TEXT, reply_markup=ReplyKeyboardRemove())
        return
    await _touch(state, note=_clip_comment(message.caption or "") or _PHOTO_PLACEHOLDER, media_msg_id=message.message_id)
    await _show_summary(message, state)


async def _remember_details(message: Message, user_id: int, phone: str, address: str) -> None:
    """Keep the phone/address for the customer's next order; tell him once when something was stored.

    Never raises: the order is already placed and the admins notified, so a storage problem only
    means the customer retypes his details next time.
    """
    try:
        # A "pick it up myself" order must not replace a saved delivery address.
        changed = await save_details(user_id, phone, None if address == _PICKUP_ADDRESS else address)
        if changed:
            await message.answer(_DETAILS_REMEMBERED_TEXT)
    except Exception:
        logger.exception("Could not remember the order details of user %s", user_id)


# One confirmation at a time per customer: two quick taps must not create two orders.
_confirming: set[int] = set()


@router.callback_query(OrderFlow.confirming, ConfirmCB.filter())
async def on_confirm(callback: CallbackQuery, callback_data: ConfirmCB, state: FSMContext, bot: Bot) -> None:
    """The customer confirmed (create the order, notify admins) or cancelled the summary."""
    message = callback.message
    user = callback.from_user
    if not isinstance(message, Message):
        await _safe_answer(callback)
        return
    if user.id in _confirming:
        await _safe_answer(callback)
        return

    _confirming.add(user.id)
    try:
        await _safe_answer(callback)
        try:
            await message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass  # the buttons are already gone
        if callback_data.action != "yes":
            await state.clear()
            await message.answer(_CANCELLED_TEXT)
            return

        data = await _fresh_data(state)
        if data is None:
            await message.answer(_EXPIRED_TEXT)
            return
        product_id = int(data["product_id"])
        product = await get_product(product_id)
        if product is None or product.status in ("sold", "removed"):
            await state.clear()
            await message.answer(_NOT_AVAILABLE_TEXT)
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
            comment=str(data.get("note") or ""),
            quantity=int(data["quantity"]),
            phone=str(data["phone"]),
            address=str(data["address"]),
        )
        media_msg_id = data.get("media_msg_id")
        await state.clear()
        await message.answer(_ORDER_SENT_TEXT.format(order_id=order_id))

        await _notify_admins(bot, order_id)
        if media_msg_id:
            await _forward_media_to_admins(bot, message.chat.id, int(media_msg_id))
        await _remember_details(message, user.id, str(data["phone"]), str(data["address"]))
    finally:
        _confirming.discard(user.id)


@router.callback_query(
    F.data.startswith((QuantityCB.__prefix__ + ":", ConfirmCB.__prefix__ + ":", SavedDetailsCB.__prefix__ + ":"))
)
async def on_stale_form_button(callback: CallbackQuery) -> None:
    """A form button pressed after its form ended (finished, cancelled or expired)."""
    await _safe_answer(callback, _STALE_BUTTON_TEXT, show_alert=True)


@router.message(StateFilter(*OrderFlow.__all_states__))
async def on_unexpected_form_input(message: Message) -> None:
    """Anything the current form step does not accept (sticker, wrong content type, ...)."""
    await message.answer(_USE_THE_FORM_TEXT)


# ------------------------------------------------------------------ admin decisions


@router.callback_query(OrderDecisionCB.filter(), AdminFilter())
async def on_order_decision(callback: CallbackQuery, callback_data: OrderDecisionCB, bot: Bot) -> None:
    """An admin tapped accept/reject on an order notification."""
    order_id = callback_data.order_id
    new_status = "accepted" if callback_data.action == "accept" else "rejected"

    changed = await decide_order(order_id, new_status)
    if not changed:
        await _safe_answer(callback, _ALREADY_DECIDED_TEXT, show_alert=True)
        return

    if new_status == "accepted":
        await set_chat_open(order_id, True)
    order = await get_order(order_id)
    if order is None:
        await _safe_answer(callback)
        return
    product = await get_product(order.product_id)

    user_text = (_ACCEPTED_USER_TEXT if new_status == "accepted" else _REJECTED_USER_TEXT).format(order_id=order_id)
    try:
        await bot.send_message(order.user_id, user_text)
    except TelegramForbiddenError:
        logger.warning("Order #%s: user %s has blocked the bot", order_id, order.user_id)
    except Exception:
        logger.exception("Order #%s: failed to notify user %s", order_id, order.user_id)

    await _edit_admin_cards(bot, order, product, order_markup(order, product))
    if new_status == "accepted":
        for admin_id, msg_id in order.admin_msg_ids.items():
            try:
                await record_relay_message(order_id, admin_id, msg_id)
            except Exception:
                logger.exception("Order #%s: failed to register chat relay link for admin %s", order_id, admin_id)

    await _safe_answer(callback)


@router.callback_query(OrderFinishCB.filter(), AdminFilter())
async def on_order_finish(callback: CallbackQuery, callback_data: OrderFinishCB, bot: Bot) -> None:
    """An admin closed an accepted order as done (delivered/sold) or cancelled."""
    if callback_data.action not in ("done", "cancel"):
        await _safe_answer(callback)
        return
    order_id = callback_data.order_id
    outcome = "completed" if callback_data.action == "done" else "cancelled"

    if not await finish_order(order_id, outcome):
        await _safe_answer(callback, _ALREADY_FINISHED_TEXT, show_alert=True)
        return
    order = await get_order(order_id)
    if order is None:
        await _safe_answer(callback)
        return
    product = await get_product(order.product_id)
    await _edit_admin_cards(bot, order, product, None)

    user_text = (_COMPLETED_USER_TEXT if outcome == "completed" else _CANCELLED_USER_TEXT).format(order_id=order_id)
    try:
        await bot.send_message(order.user_id, user_text)
    except TelegramForbiddenError:
        logger.info("Order #%s: user %s has blocked the bot; the final notice was not delivered", order_id, order.user_id)
    except Exception:
        logger.exception("Order #%s: failed to notify user %s about the outcome", order_id, order.user_id)

    await _safe_answer(callback)


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
        # On the order card the "done / cancel" buttons must survive closing the chat; a relayed
        # customer message only ever carried the "end chat" button, so it ends up without any.
        markup: InlineKeyboardMarkup | None = None
        fresh = await get_order(callback_data.order_id)
        if fresh is not None and fresh.admin_msg_ids.get(callback.from_user.id) == callback.message.message_id:
            markup = order_markup(fresh, await get_product(fresh.product_id))
        try:
            await callback.message.edit_reply_markup(reply_markup=markup)
        except TelegramBadRequest:
            pass

    try:
        await bot.send_message(order.user_id, _CHAT_CLOSED_USER_TEXT)
    except TelegramForbiddenError:
        logger.info("Order #%s: user %s has blocked the bot; close notice not delivered", order.order_id, order.user_id)
    except Exception:
        logger.exception("Order #%s: failed to notify user of chat close", order.order_id)

    await _safe_answer(callback, _CHAT_CLOSED_ADMIN_TEXT)


async def _admin_label(bot: Bot, admin_id: int) -> str:
    """Best-effort plain-text name of an admin, for "already taken by ..." messages; falls back to the id.

    Plain text on purpose (alerts, replies and edited cards carry no parse mode); callers that put
    it into an HTML message must `html.escape` it themselves.
    """
    try:
        chat = await bot.get_chat(admin_id)
        name = " ".join(part for part in (chat.first_name, chat.last_name) if part) or chat.username
        return name or str(admin_id)
    except Exception:
        return str(admin_id)


async def _reply_to_inquiry(message: Message, bot: Bot, replied_to_id: int) -> None:
    """An admin replied to an inquiry message: pass the answer on to that customer.

    Anything that is not a relayed inquiry message (a normal admin-panel reply) is ignored.
    Replying also counts as accepting the chat when nobody has yet (see `try_claim_inquiry`);
    if someone else already claimed it, the reply is refused instead of being sent.
    """
    admin = message.from_user
    if admin is None:
        return
    user_id = await find_inquiry_user_by_relay(admin.id, replied_to_id)
    if user_id is None:
        return
    claim = await try_claim_inquiry(user_id, admin.id)
    if not claim.is_open:
        await message.reply(_CHAT_ALREADY_CLOSED_TEXT)
        return
    if not claim.success:
        other = await _admin_label(bot, claim.claimed_by) if claim.claimed_by is not None else "boshqa admin"
        await message.reply(_INQUIRY_TAKEN_REPLY_TEXT.format(name=other))
        return
    if claim.newly_claimed:
        cards = await pop_inquiry_notice_cards(user_id)
        await _disable_other_notice_cards(
            bot, cards, admin.id, _INQUIRY_TAKEN_CARD_TEXT.format(name=admin.full_name)
        )
    else:
        await _dismiss_idle_cards(bot, user_id)  # the owner is answering after all: no need to ask around
    # touch_inquiry() also restarts the inactivity window, so the customer can answer the reply.
    await touch_inquiry(user_id)
    try:
        await bot.copy_message(chat_id=user_id, from_chat_id=message.chat.id, message_id=message.message_id)
    except TelegramForbiddenError:
        await message.reply(_RELAY_SENT_TO_ADMIN_FAIL)
        return
    except Exception:
        logger.exception("Inquiry of user %s: failed to relay the admin reply", user_id)
        await message.reply(_RELAY_SENT_TO_ADMIN_FAIL)
        return
    await record_inquiry_history(user_id, message.chat.id, message.message_id, "admin")
    try:
        await message.reply(_RELAY_DELIVERED_TEXT)
    except Exception:
        pass  # a missing confirmation shouldn't matter; the reply already reached the customer


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
        await _reply_to_inquiry(message, bot, reply_to.message_id)  # ignores non-relay replies itself
        return

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


async def _tell_customer_claimed(bot: Bot, user_id: int) -> None:
    """Let the customer know an admin took his inquiry (best effort; a failure here never affects the admin).

    If he has not written anything yet the note asks for his question instead of claiming it was seen.
    """
    try:
        written = await customer_has_written(user_id)
    except SQLAlchemyError:
        written = False
    text = _INQUIRY_SEEN_USER_TEXT if written else _INQUIRY_JOINED_USER_TEXT
    try:
        await _tg_call(bot.send_message, user_id, text)
    except TelegramForbiddenError:
        logger.info("Inquiry of user %s: he has blocked the bot; acceptance note not delivered", user_id)
    except Exception:
        logger.exception("Inquiry of user %s: failed to send the acceptance note", user_id)


@router.callback_query(InquiryClaimCB.filter(), AdminFilter())
async def on_inquiry_claim(callback: CallbackQuery, callback_data: InquiryClaimCB, bot: Bot) -> None:
    """Admin tapped 'Qabul qilish' on the initial card: he alone now owns this inquiry."""
    admin = callback.from_user
    claim = await try_claim_inquiry(callback_data.user_id, admin.id)
    if not claim.is_open:
        await _safe_answer(callback, _CHAT_ALREADY_CLOSED_TEXT, show_alert=True)
        return
    if not claim.success:
        other = await _admin_label(bot, claim.claimed_by) if claim.claimed_by is not None else "boshqa admin"
        await _safe_answer(callback, _INQUIRY_TAKEN_ALERT_TEXT.format(name=other), show_alert=True)
        return

    await _safe_answer(callback, _INQUIRY_CLAIMED_TEXT)
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_reply_markup(reply_markup=_inquiry_owned_markup(callback_data.user_id))
        except TelegramBadRequest:
            pass
    if claim.newly_claimed:
        cards = await pop_inquiry_notice_cards(callback_data.user_id)
        await _disable_other_notice_cards(
            bot, cards, admin.id, _INQUIRY_TAKEN_CARD_TEXT.format(name=admin.full_name)
        )
        # Accepting by button says nothing to the customer by itself (a reply does, so that path stays silent).
        await _tell_customer_claimed(bot, callback_data.user_id)


@router.callback_query(InquiryContinueCB.filter(), AdminFilter())
async def on_inquiry_continue(callback: CallbackQuery, callback_data: InquiryContinueCB, bot: Bot) -> None:
    """Admin tapped 'Davom ettirish' on the 1-hour idle notice: the chat is his now, with everything shown.

    The first tap wins (atomic, see `try_continue_inquiry`); the other admins' cards are retired.
    He then gets the customer, the product with its id and the whole transcript so far, since he may
    not have followed the conversation from the start.
    """
    admin = callback.from_user
    user_id = callback_data.user_id
    result = await try_continue_inquiry(user_id, admin.id)
    if not result.success:
        if isinstance(callback.message, Message):
            try:
                await callback.message.edit_reply_markup(reply_markup=None)  # this card is stale either way
            except TelegramBadRequest:
                pass
        if not result.is_open:
            await _safe_answer(callback, _CHAT_ALREADY_CLOSED_TEXT, show_alert=True)
            return
        other = await _admin_label(bot, result.claimed_by) if result.claimed_by is not None else "boshqa admin"
        await _safe_answer(callback, _INQUIRY_CONTINUE_TAKEN_ALERT_TEXT.format(name=other), show_alert=True)
        return

    await _safe_answer(callback, _INQUIRY_CONTINUED_TEXT)
    cards = await pop_inquiry_notice_cards(user_id)
    await _disable_other_notice_cards(bot, cards, admin.id, _INQUIRY_IDLE_TAKEN_CARD_TEXT.format(name=admin.full_name))
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_reply_markup(reply_markup=_inquiry_owned_markup(user_id))
        except TelegramBadRequest:
            pass
        await record_inquiry_relay(user_id, admin.id, callback.message.message_id)  # replying to the card works too

    inquiry = await get_open_inquiry(user_id)
    if inquiry is None:  # closed or expired within the last instants; nothing to hand over
        return
    product = await get_product(inquiry.product_id) if inquiry.product_id is not None else None
    await _send_inquiry_context(bot, admin.id, user_id, product, inquiry.user_fullname)


async def _relay_inquiry_message(message: Message, bot: Bot) -> None:
    """Relay a customer's message: to the admin who owns the chat, or to every admin until someone claims it."""
    user = message.from_user
    if user is None:
        return
    inquiry = await get_open_inquiry(user.id)
    if inquiry is None:
        return
    await touch_inquiry(user.id)
    if inquiry.claimed_by is not None:
        await _dismiss_idle_cards(bot, user.id)  # he wrote again: any "continue or end?" card is obsolete
    await record_inquiry_history(user.id, message.chat.id, message.message_id, "customer")

    header = _INQUIRY_HEADER.format(name=html.escape(user.full_name))
    close_markup = _inquiry_owned_markup(user.id)
    # Once claimed, only that admin gets the rest of the conversation; before that, everyone does,
    # so the request doesn't stall if nobody has tapped "Qabul qilish" yet.
    targets = [inquiry.claimed_by] if inquiry.claimed_by is not None else list(admin_ids())
    for admin_id in targets:
        try:
            await bot.send_message(admin_id, header, parse_mode="HTML")
            sent = await bot.copy_message(
                chat_id=admin_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
                reply_markup=close_markup,
            )
            await record_inquiry_relay(user.id, admin_id, sent.message_id)
        except TelegramForbiddenError:
            logger.warning("Inquiry of user %s: admin %s has not started the bot", user.id, admin_id)
        except Exception:
            logger.exception("Inquiry of user %s: failed to relay a message to admin %s", user.id, admin_id)


@router.callback_query(InquiryCloseCB.filter(), AdminFilter())
async def on_inquiry_close(callback: CallbackQuery, callback_data: InquiryCloseCB, bot: Bot) -> None:
    """An admin tapped 'end chat' on an inquiry; closes it, clears any pending card, and tells the customer."""
    cards = await pop_inquiry_notice_cards(callback_data.user_id)
    if not await close_inquiry_if_open(callback_data.user_id):
        await _safe_answer(callback, _CHAT_ALREADY_CLOSED_TEXT, show_alert=True)
        return
    closer_id = callback.from_user.id if callback.from_user is not None else -1
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
    if cards:
        await _disable_other_notice_cards(bot, cards, closer_id, _CHAT_CLOSED_ADMIN_TEXT)
    try:
        await bot.send_message(callback_data.user_id, _INQUIRY_CLOSED_USER_TEXT)
    except TelegramForbiddenError:
        logger.info("Inquiry of user %s: he has blocked the bot; close notice not delivered", callback_data.user_id)
    except Exception:
        logger.exception("Inquiry of user %s: failed to send the close notice", callback_data.user_id)
    await _safe_answer(callback, _CHAT_CLOSED_ADMIN_TEXT)


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
        await _relay_inquiry_message(message, bot)
        return  # No open order chat: it was either an inquiry message or small talk to leave alone.

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


def _without_mark_sold(markup: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup | None:
    """The same keyboard minus the 'mark product sold' button (None when nothing is left)."""
    if markup is None:
        return None
    rows = [
        [button for button in row if not (button.callback_data or "").startswith(MarkSoldCB.__prefix__ + ":")]
        for row in markup.inline_keyboard
    ]
    rows = [row for row in rows if row]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


@router.callback_query(MarkSoldCB.filter(), AdminFilter())
async def on_mark_sold(callback: CallbackQuery, callback_data: MarkSoldCB, bot: Bot) -> None:
    """An admin tapped the 'mark product sold' button on an order card."""
    try:
        result = await mark_sold(bot, callback_data.product_id)
    except ValueError:
        await callback.answer("Mahsulot topilmadi.", show_alert=True)
        return
    except Exception:
        logger.exception("Failed to mark product %s sold from the order flow", callback_data.product_id)
        await callback.answer("Xatolik yuz berdi.", show_alert=True)
        return

    if isinstance(callback.message, Message):
        try:
            # Only this button goes away: an accepted order's "done / cancel" buttons must stay usable.
            await callback.message.edit_reply_markup(reply_markup=_without_mark_sold(callback.message.reply_markup))
        except TelegramBadRequest:
            pass
    await callback.answer(f"Belgilandi: {result['edited']} ta post yangilandi.")

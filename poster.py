"""Publishing products to the registered target channels/groups and reflecting sold/available state."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from caption import strip_html, with_sold_banner
from db.chats import target_chats
from db.posts import add_post_log, live_posts, mark_post_dead
from db.products import get_product, mark_posted, set_status
from db.settings import get_settings
from post_id import ensure_caption_id
from utils import alert_admins

if TYPE_CHECKING:
    from aiogram import Bot

    from db.models import Product

logger = logging.getLogger("poster")

_BUY_BUTTON_TEXT = "\U0001f6d2 Sotib olish / Savol berish"
_ENTITIES_ERROR = "can't parse entities"
_NOT_MODIFIED_ERROR = "message is not modified"
_EDIT_NOT_FOUND_ERROR = "message to edit not found"

# Serializes publish_product across the scheduler job and any admin-triggered "post now",
# so two callers can never post the same product (or two products) to the channel at once.
_publish_lock = asyncio.Lock()


def _buy_markup(bot_username: str, product_id: int) -> InlineKeyboardMarkup:
    """Build the single 'buy / ask' button, deep-linking into the bot's /start."""
    url = f"https://t.me/{bot_username}?start=prod_{product_id}"
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=_BUY_BUTTON_TEXT, url=url)]])


class PublishError(RuntimeError):
    """Raised when a product could not be published to any target chat."""


class NoTargetError(PublishError):
    """Raised when the head admin has not registered a posting channel/group yet."""


async def _send_with_fallback(
    bot: Bot, chat_id: int, product: Product, markup: InlineKeyboardMarkup
) -> int:
    """Send the product photo to one chat, retrying once on flood control or bad caption entities."""
    try:
        message = await bot.send_photo(
            chat_id,
            product.tg_file_id,
            caption=product.caption_html,
            parse_mode="HTML",
            reply_markup=markup,
        )
        return message.message_id
    except TelegramRetryAfter as exc:
        logger.warning(
            "Flood control publishing product %s to %s; sleeping %ss", product.id, chat_id, exc.retry_after
        )
        await asyncio.sleep(exc.retry_after)
        message = await bot.send_photo(
            chat_id,
            product.tg_file_id,
            caption=product.caption_html,
            parse_mode="HTML",
            reply_markup=markup,
        )
        return message.message_id
    except TelegramBadRequest as exc:
        if _ENTITIES_ERROR not in str(exc).lower():
            raise
        logger.warning(
            "Caption entities rejected for product %s in %s; retrying as plain text", product.id, chat_id
        )
        message = await bot.send_photo(
            chat_id,
            product.tg_file_id,
            caption=strip_html(product.caption_html),
            parse_mode=None,
            reply_markup=markup,
        )
        return message.message_id


async def publish_product(bot: Bot, product: Product) -> int:
    """Post a product's photo+caption to EVERY registered target chat; returns the first new message_id.

    A target that fails does not stop the others; PublishError is raised only when none succeeded
    (NoTargetError when no target is registered at all).
    """
    targets = target_chats()
    if not targets:
        raise NoTargetError(
            "Post joylanadigan kanal/guruh belgilanmagan. /admin \u2192 Kanal va guruhlar bo'limida qo'shing."
        )
    async with _publish_lock:
        # The channel post must carry the same ID the database row has (also for captions
        # stored before the ID line existed).
        product = await ensure_caption_id(product)
        me = await bot.me()
        markup = _buy_markup(me.username or "", product.id)

        sent: list[tuple[int, int]] = []  # (chat_id, message_id)
        errors: list[str] = []
        for target in targets:
            label = target.title or str(target.chat_id)
            try:
                message_id = await _send_with_fallback(bot, target.chat_id, product, markup)
            except TelegramForbiddenError:
                logger.error("Bot lacks permission to post in %s (%s)", label, target.chat_id)
                await alert_admins(
                    bot,
                    f"\u26a0\ufe0f Bot \u00ab{label}\u00bb ga yozolmayapti (huquq yo'q yoki chetlashtirilgan).",
                    key=f"target_forbidden_{target.chat_id}",
                )
                errors.append(f"{label}: huquq yo'q")
                continue
            except TelegramAPIError as exc:
                logger.exception("Publishing product %s to %s (%s) failed", product.id, label, target.chat_id)
                errors.append(f"{label}: {exc}")
                continue
            post_log_id = await add_post_log(product.id, target.chat_id, message_id)
            sent.append((target.chat_id, message_id))
            logger.info(
                "Published product %s to %s (%s) as message %s (post_log %s)",
                product.id, label, target.chat_id, message_id, post_log_id,
            )

        if not sent:
            raise PublishError("Hech bir joyga yuborib bo'lmadi \u2014 " + "; ".join(errors))
        await mark_posted(product.id)

        bot_settings = await get_settings()
        if bot_settings.repost_policy == "delete_previous":
            fresh = set(sent)
            for old in await live_posts(product.id):
                if (old.chat_id, old.message_id) in fresh:
                    continue
                try:
                    await bot.delete_message(old.chat_id, old.message_id)
                except TelegramBadRequest:
                    logger.debug("Old post %s of product %s already gone", old.message_id, product.id)
                except Exception:
                    logger.exception("Failed to delete old post %s of product %s", old.message_id, product.id)
                await mark_post_dead(old.id)

        return sent[0][1]


async def _edit_live_posts(
    bot: Bot,
    product_id: int,
    caption: str,
    markup: InlineKeyboardMarkup | None,
    action: str,
) -> dict[str, int]:
    """Edit every live post of a product to `caption`/`markup`; returns edit/fail counts."""
    edited = 0
    failed = 0
    for post in await live_posts(product_id):
        try:
            await bot.edit_message_caption(
                chat_id=post.chat_id,
                message_id=post.message_id,
                caption=caption,
                parse_mode="HTML",
                reply_markup=markup,
            )
            edited += 1
        except TelegramBadRequest as exc:
            text = str(exc).lower()
            if _NOT_MODIFIED_ERROR in text:
                edited += 1
                continue
            if _EDIT_NOT_FOUND_ERROR in text:
                await mark_post_dead(post.id)
                failed += 1
                continue
            logger.warning("Failed to mark post %s of product %s %s: %s", post.id, product_id, action, exc)
            failed += 1
        except Exception:
            logger.exception("Unexpected error marking post %s of product %s %s", post.id, product_id, action)
            failed += 1
    return {"edited": edited, "failed": failed}


async def mark_sold(bot: Bot, product_id: int) -> dict[str, int]:
    """Mark a product sold and stamp the sold banner on its live posts; returns edit/fail counts."""
    product = await get_product(product_id)
    if product is None:
        raise ValueError(f"Product {product_id} not found")
    await set_status(product_id, "sold")
    product = await ensure_caption_id(product)
    return await _edit_live_posts(bot, product_id, with_sold_banner(product.caption_html), None, "sold")


async def mark_available(bot: Bot, product_id: int) -> dict[str, int]:
    """Reverse mark_sold: restore the normal caption and buy button on live posts."""
    product = await get_product(product_id)
    if product is None:
        raise ValueError(f"Product {product_id} not found")
    await set_status(product_id, "active")
    product = await ensure_caption_id(product)
    me = await bot.me()
    markup = _buy_markup(me.username or "", product_id)
    return await _edit_live_posts(bot, product_id, product.caption_html, markup, "available")


async def refresh_live_posts(bot: Bot, product_id: int) -> dict[str, int]:
    """Push the stored caption to the live posts without changing the product's status.

    Sold products keep the sold banner and no buy button; removed products are left alone.
    """
    product = await get_product(product_id)
    if product is None:
        raise ValueError(f"Product {product_id} not found")
    if product.status == "removed":
        return {"edited": 0, "failed": 0}
    product = await ensure_caption_id(product)
    if product.status == "sold":
        return await _edit_live_posts(bot, product_id, with_sold_banner(product.caption_html), None, "refreshed")
    me = await bot.me()
    markup = _buy_markup(me.username or "", product_id)
    return await _edit_live_posts(bot, product_id, product.caption_html, markup, "refreshed")

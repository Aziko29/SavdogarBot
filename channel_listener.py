"""Watches the source channel for new/edited posts and turns them into pending products."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.filters import BaseFilter
from aiogram.types import Message

from config import settings
from db.chats import is_source_chat
from db.products import create_product_pending, get_by_source, reset_for_reprocess
from utils import fire_and_forget
from worker import enqueue

logger = logging.getLogger("channel_listener")

router = Router(name="channel_listener")



class _SourceChat(BaseFilter):
    """True when the update comes from a source channel registered by the head admin (RAM lookup)."""

    async def __call__(self, message: Message) -> bool:
        return is_source_chat(message.chat.id)


_IS_SOURCE_CHAT = _SourceChat()

# One buffer per in-flight media_group_id; entries are removed once the album is finalized.
_album_buffers: dict[str, "_AlbumBuffer"] = {}
_album_tasks: set[asyncio.Task[None]] = set()


@dataclass
class _AlbumBuffer:
    """The one photo and the first non-empty caption collected from an album so far."""

    chat_id: int
    first_msg_id: int
    photo_file_id: str
    photo_file_unique_id: str
    caption: str


def _extract_text(message: Message) -> str:
    """Return the post's caption or text, or an empty string."""
    return message.caption or message.text or ""


async def _create_from_photo(
    chat_id: int, msg_id: int, tg_file_id: str, file_unique_id: str, original_text: str
) -> None:
    """Create a pending product and enqueue it for AI; duplicates are silently ignored."""
    pid = await create_product_pending(
        source_chat_id=chat_id,
        source_msg_id=msg_id,
        tg_file_id=tg_file_id,
        file_unique_id=file_unique_id,
        original_text=original_text,
    )
    if pid is not None:
        enqueue(pid)


async def _finalize_album(group_id: str) -> None:
    """Wait for the rest of an album to arrive, then create one product from its first photo."""
    await asyncio.sleep(settings.album_wait_sec)
    buf = _album_buffers.pop(group_id, None)
    if buf is None:
        return
    await _create_from_photo(buf.chat_id, buf.first_msg_id, buf.photo_file_id, buf.photo_file_unique_id, buf.caption)


def _schedule_album(message: Message) -> None:
    """Buffer an album message: the first one starts the finalize timer and supplies the photo."""
    group_id = message.media_group_id
    if group_id is None or not message.photo:
        return
    text = _extract_text(message)
    buf = _album_buffers.get(group_id)
    if buf is None:
        photo = message.photo[-1]
        buf = _AlbumBuffer(
            chat_id=message.chat.id,
            first_msg_id=message.message_id,
            photo_file_id=photo.file_id,
            photo_file_unique_id=photo.file_unique_id,
            caption=text,
        )
        _album_buffers[group_id] = buf
        task = fire_and_forget(_finalize_album(group_id))
        _album_tasks.add(task)
        task.add_done_callback(_album_tasks.discard)
        return
    if not buf.caption and text:
        buf.caption = text


@router.channel_post(_IS_SOURCE_CHAT, F.photo)
async def on_channel_post(message: Message) -> None:
    """Handle a new post in the source channel: single photo or the start/continuation of an album."""
    try:
        if message.media_group_id:
            _schedule_album(message)
        else:
            await _create_from_photo(
                message.chat.id,
                message.message_id,
                message.photo[-1].file_id,
                message.photo[-1].file_unique_id,
                _extract_text(message),
            )
    except Exception:
        logger.exception("Failed to handle channel post %s", message.message_id)


@router.edited_channel_post(_IS_SOURCE_CHAT, F.photo)
async def on_edited_channel_post(message: Message) -> None:
    """Handle an edit to a source-channel post: reprocess it only if the text actually changed."""
    try:
        product = await get_by_source(message.chat.id, message.message_id)
        if product is None:
            return
        new_text = _extract_text(message)
        if new_text == product.original_text:
            return
        await reset_for_reprocess(product.id, new_text)
        enqueue(product.id)
    except Exception:
        logger.exception("Failed to handle edited channel post %s", message.message_id)

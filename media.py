"""Photo handling: download a Telegram file and normalize it into a bounded RGB JPEG."""
from __future__ import annotations

import asyncio
import io
import logging
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramAPIError
from PIL import Image, UnidentifiedImageError

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger("media")

_JPEG_QUALITY = 85


def shrink_jpeg(data: bytes, max_side: int) -> bytes:
    """Re-encode arbitrary image bytes as an RGB JPEG whose longest side is <= max_side (sync)."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            rgb = img.convert("RGB")
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError(f"not a valid image: {exc!r}") from exc

    width, height = rgb.size
    longest = max(width, height)
    if longest > max_side > 0:
        scale = max_side / longest
        new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
        rgb = rgb.resize(new_size, Image.LANCZOS)

    buffer = io.BytesIO()
    rgb.save(buffer, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
    return buffer.getvalue()


async def download_photo(bot: Bot, file_id: str, max_side: int) -> bytes:
    """Download a Telegram photo by file_id and return it as a normalized JPEG."""
    buffer = io.BytesIO()
    try:
        result = await bot.download(file_id, destination=buffer)
    except TelegramAPIError as exc:
        raise ValueError(f"telegram download failed for file_id {file_id}: {exc!r}") from exc
    if result is None:
        raise ValueError(f"telegram returned no data for file_id {file_id}")

    raw = buffer.getvalue()
    if not raw:
        raise ValueError(f"telegram returned an empty file for file_id {file_id}")
    return await asyncio.to_thread(shrink_jpeg, raw, max_side)

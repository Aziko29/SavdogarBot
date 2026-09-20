"""Caption building: HTML template rendering, the sold banner, and plain-text fallback."""
from __future__ import annotations

import html
import json
import logging
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from db.models import Product

logger = logging.getLogger("caption")

_MAX_CAPTION = 1024
_SOLD_BANNER = "<b>\u274c BU MAHSULOT SOTILIB TUGADI</b>\n\n"

_DIVIDER = "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"

_TEMPLATE = (
    "\u2728 <b>{name}</b> \u2728\n\n"
    "{sales_pitch}\n\n"
    f"{_DIVIDER}\n"
    "{id_line}"
    "\U0001f4b0 <b>Narxi:</b> {price}\n"
    "\U0001f4cf <b>O'lchami:</b> {size}\n"
    "\U0001f9f5 <b>Mato:</b> {fabric}\n"
    "\U0001f4e6 <b>Mavjud:</b> {stock}\n"
    f"{_DIVIDER}\n\n"
    "{hashtags}"
)

# The ID line ties a channel post to its database row: the number is products.id, the same
# number the admin panel shows as "#<id>" and the buy button's deep link carries (prod_<id>).
_ID_LINE = "\U0001f194 <b>ID:</b> <code>{product_id}</code>\n"
_ID_RE = re.compile(r"\U0001f194 <b>ID:</b> <code>(\d+)</code>")

_TAG_RE = re.compile(r"<[^>]*>")
_B_OPEN_RE = re.compile(r"<b>")
_B_CLOSE_RE = re.compile(r"</b>")
_SENTENCE_END_RE = re.compile(r"[.!?\u2026]")
_TRIM_STEP = 20


def _render(escaped_fields: Mapping[str, str], sales_pitch_html: str, id_line: str = "") -> str:
    """Fill the template with already-HTML-escaped field values."""
    return _TEMPLATE.format(
        name=escaped_fields.get("name", ""),
        sales_pitch=sales_pitch_html,
        id_line=id_line,
        price=escaped_fields.get("price", ""),
        size=escaped_fields.get("size", ""),
        fabric=escaped_fields.get("fabric", ""),
        stock=escaped_fields.get("stock", ""),
        hashtags=escaped_fields.get("hashtags", ""),
    )


def _trim_at_sentence(text: str, max_len: int) -> str:
    """Cut plain text to at most max_len chars, preferring the last sentence boundary."""
    if max_len <= 0:
        return ""
    if len(text) <= max_len:
        return text.strip()
    window = text[:max_len]
    last_end = max((m.end() for m in _SENTENCE_END_RE.finditer(window)), default=0)
    if last_end > 0:
        return window[:last_end].strip()
    last_space = window.rfind(" ")
    if last_space > 0:
        return window[:last_space].strip()
    return window.strip()


def build_caption(fields: Mapping[str, str], product_id: int | None = None) -> str:
    """Render the product caption as HTML, shortening sales_pitch to stay within 1024 chars.

    With `product_id` the caption carries the "ID" line (the products.id database key).
    """
    raw_pitch = str(fields.get("sales_pitch", "")).strip()
    escaped = {k: html.escape(str(v), quote=False) for k, v in fields.items() if k != "sales_pitch"}
    id_line = _ID_LINE.format(product_id=int(product_id)) if product_id is not None else ""

    caption = _render(escaped, html.escape(raw_pitch, quote=False), id_line)
    if len(caption) <= _MAX_CAPTION:
        return caption

    budget = len(raw_pitch)
    while True:
        overflow = len(caption) - _MAX_CAPTION
        budget = max(0, min(budget, len(raw_pitch)) - overflow)
        trimmed = _trim_at_sentence(raw_pitch, budget)
        caption = _render(escaped, html.escape(trimmed, quote=False), id_line)
        if len(caption) <= _MAX_CAPTION or budget <= 0:
            break
        budget -= _TRIM_STEP

    if len(caption) > _MAX_CAPTION:
        logger.warning("Caption still over %d chars after trimming sales_pitch; hard-cutting", _MAX_CAPTION)
        caption = caption[:_MAX_CAPTION]
    return caption


def with_sold_banner(caption_html: str) -> str:
    """Prepend the sold-out banner to an existing caption, re-applying the 1024-char limit."""
    combined = _SOLD_BANNER + caption_html
    if len(combined) <= _MAX_CAPTION:
        return combined

    cut = _MAX_CAPTION
    last_lt = combined.rfind("<", 0, cut)
    last_gt = combined.rfind(">", 0, cut)
    if last_lt > last_gt:
        cut = last_lt
    truncated = combined[:cut].rstrip()

    open_b = len(_B_OPEN_RE.findall(truncated))
    close_b = len(_B_CLOSE_RE.findall(truncated))
    if open_b > close_b:
        truncated += "</b>"
    return truncated


def strip_html(text: str) -> str:
    """Plain-text fallback: drop tags and unescape entities."""
    return html.unescape(_TAG_RE.sub("", text)).strip()


def shorten_at_sentence(text: str, max_len: int) -> str:
    """Cut plain text to at most max_len chars, preferring the last sentence boundary."""
    return _trim_at_sentence(text.strip(), max_len)


def caption_id(caption_html: str) -> int | None:
    """Return the product ID printed in a caption, or None when the caption has no ID line."""
    match = _ID_RE.search(caption_html)
    return int(match.group(1)) if match else None


def has_product_id(caption_html: str, product_id: int) -> bool:
    """True when the caption's ID line exists and equals the given database id."""
    return caption_id(caption_html) == product_id


def parse_ai_json(raw: str | None) -> dict[str, Any]:
    """Parse a stored products.ai_json into a dict; bad or empty data yields {}."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Corrupt ai_json: %r", raw[:100])
        return {}
    return data if isinstance(data, dict) else {}


def product_caption_fields(product: Product) -> dict[str, str]:
    """The seven caption fields of a product as currently stored (sales_pitch lives in ai_json)."""
    ai_data = parse_ai_json(product.ai_json)
    return {
        "name": product.name,
        "price": product.price,
        "size": product.size,
        "fabric": product.fabric,
        "stock": product.stock,
        "hashtags": product.hashtags,
        "sales_pitch": str(ai_data.get("sales_pitch", "")),
    }

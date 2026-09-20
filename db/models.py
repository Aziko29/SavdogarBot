"""Dataclasses for DB entities and row -> dataclass converters."""
from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

logger = logging.getLogger("db.models")

ProductStatus = Literal["active", "sold", "removed"]
Category = Literal["new", "mid", "old"]
AiStatus = Literal["pending", "processing", "done", "failed", "blocked"]
RepostPolicy = Literal["delete_previous", "keep"]
OrderStatus = Literal["pending", "accepted", "rejected"]

PRODUCT_STATUSES: tuple[str, ...] = ("active", "sold", "removed")
CATEGORIES: tuple[str, ...] = ("new", "mid", "old")
AI_STATUSES: tuple[str, ...] = ("pending", "processing", "done", "failed", "blocked")
REPOST_POLICIES: tuple[str, ...] = ("delete_previous", "keep")
CHAT_ROLES: tuple[str, ...] = ("source", "target")
CHAT_TYPES: tuple[str, ...] = ("channel", "group", "supergroup")
ORDER_STATUSES: tuple[str, ...] = ("pending", "accepted", "rejected")


@dataclass(slots=True)
class Product:
    """A product created from a source-channel post."""

    id: int
    source_chat_id: int
    source_msg_id: int
    file_unique_id: str
    tg_file_id: str
    original_text: str
    ai_json: str | None
    name: str
    price: str
    size: str
    fabric: str
    stock: str
    hashtags: str
    caption_html: str
    status: ProductStatus
    category: Category
    category_locked: bool
    ai_status: AiStatus
    attempts: int
    next_try_at: datetime | None
    needs_review: bool
    last_posted_at: datetime | None
    post_count: int
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class BotSettings:
    """Runtime-editable bot settings (single row, id=1)."""

    new_multi: int
    mid_multi: int
    old_multi: int
    interval_mins: int
    night_start: str
    night_end: str
    new_keep: int
    mid_keep: int
    autopost_enabled: bool
    repost_policy: RepostPolicy


@dataclass(slots=True)
class PostLog:
    """One message published to the target channel."""

    id: int
    product_id: int
    chat_id: int
    message_id: int
    is_text_only: bool
    posted_at: datetime
    is_live: bool


@dataclass(slots=True)
class Order:
    """A customer order request."""

    order_id: int
    product_id: int
    user_id: int
    username: str | None
    user_fullname: str
    comment: str
    status: OrderStatus
    created_at: datetime
    updated_at: datetime
    admin_msg_ids: dict[int, int] = field(default_factory=dict)
    chat_open: bool = False


def _mapping(row: Any) -> Mapping[str, Any]:
    """Return a string-keyed mapping for a SQLAlchemy Row or an existing mapping."""
    if isinstance(row, Mapping):
        return row
    return row._mapping  # type: ignore[no-any-return]


def dump_admin_msg_ids(msgs: Mapping[int, int]) -> str:
    """Serialize {admin_id: message_id} to the JSON text stored in orders.admin_msg_ids."""
    return json.dumps({str(k): int(v) for k, v in msgs.items()}, separators=(",", ":"))


def parse_admin_msg_ids(raw: str | None) -> dict[int, int]:
    """Parse orders.admin_msg_ids JSON into {admin_id: message_id}; bad data yields {}."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Corrupt admin_msg_ids JSON: %r", raw[:100])
        return {}
    if not isinstance(data, dict):
        logger.warning("admin_msg_ids is not a JSON object: %r", raw[:100])
        return {}
    try:
        return {int(k): int(v) for k, v in data.items()}
    except (TypeError, ValueError):
        logger.warning("admin_msg_ids contains non-integer values: %r", raw[:100])
        return {}


def product_from_row(row: Any) -> Product:
    """Convert a products row into a Product."""
    m = _mapping(row)
    return Product(
        id=int(m["id"]),
        source_chat_id=int(m["source_chat_id"]),
        source_msg_id=int(m["source_msg_id"]),
        file_unique_id=m["file_unique_id"],
        tg_file_id=m["tg_file_id"],
        original_text=m["original_text"] or "",
        ai_json=m["ai_json"],
        name=m["name"] or "",
        price=m["price"] or "",
        size=m["size"] or "",
        fabric=m["fabric"] or "",
        stock=m["stock"] or "",
        hashtags=m["hashtags"] or "",
        caption_html=m["caption_html"] or "",
        status=m["status"],
        category=m["category"],
        category_locked=bool(m["category_locked"]),
        ai_status=m["ai_status"],
        attempts=int(m["attempts"]),
        next_try_at=m["next_try_at"],
        needs_review=bool(m["needs_review"]),
        last_posted_at=m["last_posted_at"],
        post_count=int(m["post_count"]),
        created_at=m["created_at"],
        updated_at=m["updated_at"],
    )


def bot_settings_from_row(row: Any) -> BotSettings:
    """Convert a settings row into BotSettings."""
    m = _mapping(row)
    return BotSettings(
        new_multi=int(m["new_multi"]),
        mid_multi=int(m["mid_multi"]),
        old_multi=int(m["old_multi"]),
        interval_mins=int(m["interval_mins"]),
        night_start=m["night_start"],
        night_end=m["night_end"],
        new_keep=int(m["new_keep"]),
        mid_keep=int(m["mid_keep"]),
        autopost_enabled=bool(m["autopost_enabled"]),
        repost_policy=m["repost_policy"],
    )


def post_log_from_row(row: Any) -> PostLog:
    """Convert a post_log row into a PostLog."""
    m = _mapping(row)
    return PostLog(
        id=int(m["id"]),
        product_id=int(m["product_id"]),
        chat_id=int(m["chat_id"]),
        message_id=int(m["message_id"]),
        is_text_only=bool(m["is_text_only"]),
        posted_at=m["posted_at"],
        is_live=bool(m["is_live"]),
    )


def order_from_row(row: Any) -> Order:
    """Convert an orders row into an Order."""
    m = _mapping(row)
    return Order(
        order_id=int(m["order_id"]),
        product_id=int(m["product_id"]),
        user_id=int(m["user_id"]),
        username=m["username"],
        user_fullname=m["user_fullname"] or "",
        comment=m["comment"] or "",
        status=m["status"],
        admin_msg_ids=parse_admin_msg_ids(m["admin_msg_ids"]),
        created_at=m["created_at"],
        updated_at=m["updated_at"],
        chat_open=bool(m["chat_open"]) if "chat_open" in m else False,
    )

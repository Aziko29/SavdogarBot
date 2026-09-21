"""SQLAlchemy Core table definitions (all timestamps stored as naive UTC, returned as aware UTC)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
    false,
    text,
    true,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from db.models import (
    AI_STATUSES,
    CATEGORIES,
    CHAT_ROLES,
    CHAT_TYPES,
    ORDER_STATUSES,
    PRODUCT_STATUSES,
    REPOST_POLICIES,
)


class UTCDateTime(TypeDecorator[datetime]):
    """Stores aware datetimes as naive UTC and restores them as aware UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """Convert an aware datetime to naive UTC; naive input is a bug and is rejected."""
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Naive datetime is not allowed; use an aware UTC datetime")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """Attach UTC tzinfo to a value read from the database."""
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


def _utcnow() -> datetime:
    """Aware UTC now (column default)."""
    return datetime.now(timezone.utc)


def _in_list(column: str, values: tuple[str, ...]) -> str:
    """Build a `column IN ('a','b')` SQL fragment for a CHECK constraint."""
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


NAMING_CONVENTION: dict[str, Any] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)

products_t = Table(
    "products",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("source_chat_id", BigInteger, nullable=False),
    Column("source_msg_id", BigInteger, nullable=False),
    Column("file_unique_id", String(128), nullable=False),
    Column("tg_file_id", Text, nullable=False),
    Column("original_text", Text, nullable=False, server_default=""),
    Column("ai_json", Text, nullable=True),
    Column("name", Text, nullable=False, server_default=""),
    Column("price", Text, nullable=False, server_default=""),
    Column("size", Text, nullable=False, server_default=""),
    Column("fabric", Text, nullable=False, server_default=""),
    Column("stock", Text, nullable=False, server_default=""),
    Column("hashtags", Text, nullable=False, server_default=""),
    Column("caption_html", Text, nullable=False, server_default=""),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("category", String(16), nullable=False, server_default="new"),
    Column("category_locked", Boolean, nullable=False, server_default=false()),
    Column("ai_status", String(16), nullable=False, server_default="pending"),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("next_try_at", UTCDateTime, nullable=True),
    Column("needs_review", Boolean, nullable=False, server_default=false()),
    Column("last_posted_at", UTCDateTime, nullable=True),
    Column("post_count", Integer, nullable=False, server_default=text("0")),
    Column("created_at", UTCDateTime, nullable=False, default=_utcnow),
    Column("updated_at", UTCDateTime, nullable=False, default=_utcnow, onupdate=_utcnow),
    UniqueConstraint("source_chat_id", "source_msg_id"),
    UniqueConstraint("file_unique_id"),
    CheckConstraint(_in_list("status", PRODUCT_STATUSES), name="status_valid"),
    CheckConstraint(_in_list("category", CATEGORIES), name="category_valid"),
    CheckConstraint(_in_list("ai_status", AI_STATUSES), name="ai_status_valid"),
    CheckConstraint("attempts >= 0", name="attempts_nonneg"),
    CheckConstraint("post_count >= 0", name="post_count_nonneg"),
    Index("ix_products_status_category", "status", "category"),
    Index("ix_products_ai_status_next_try_at", "ai_status", "next_try_at"),
    sqlite_autoincrement=True,
)

settings_t = Table(
    "settings",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("new_multi", Integer, nullable=False, server_default=text("5")),
    Column("mid_multi", Integer, nullable=False, server_default=text("3")),
    Column("old_multi", Integer, nullable=False, server_default=text("1")),
    Column("interval_mins", Integer, nullable=False, server_default=text("30")),
    Column("night_start", String(5), nullable=False, server_default="23:00"),
    Column("night_end", String(5), nullable=False, server_default="08:00"),
    Column("new_keep", Integer, nullable=False, server_default=text("10")),
    Column("mid_keep", Integer, nullable=False, server_default=text("20")),
    Column("autopost_enabled", Boolean, nullable=False, server_default=true()),
    Column("repost_policy", String(16), nullable=False, server_default="delete_previous"),
    CheckConstraint("id = 1", name="single_row"),
    CheckConstraint("new_multi >= 0 AND mid_multi >= 0 AND old_multi >= 0", name="multi_nonneg"),
    CheckConstraint("interval_mins >= 1", name="interval_positive"),
    CheckConstraint("new_keep >= 0 AND mid_keep >= 0", name="keep_nonneg"),
    CheckConstraint(_in_list("repost_policy", REPOST_POLICIES), name="repost_policy_valid"),
)

post_log_t = Table(
    "post_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column(
        "product_id",
        Integer,
        ForeignKey("products.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("chat_id", BigInteger, nullable=False),
    Column("message_id", BigInteger, nullable=False),
    Column("is_text_only", Boolean, nullable=False, server_default=false()),
    Column("posted_at", UTCDateTime, nullable=False, default=_utcnow),
    Column("is_live", Boolean, nullable=False, server_default=true()),
    Index("ix_post_log_product_id_is_live", "product_id", "is_live"),
    Index("ix_post_log_posted_at", "posted_at"),
    sqlite_autoincrement=True,
)

orders_t = Table(
    "orders",
    metadata,
    Column("order_id", Integer, primary_key=True, autoincrement=True),
    Column(
        "product_id",
        Integer,
        ForeignKey("products.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("user_id", BigInteger, nullable=False),
    Column("username", Text, nullable=True),
    Column("user_fullname", Text, nullable=False, server_default=""),
    Column("comment", Text, nullable=False, server_default=""),
    Column("status", String(16), nullable=False, server_default="pending"),
    Column("admin_msg_ids", Text, nullable=False, server_default="{}"),
    Column("chat_open", Boolean, nullable=False, server_default=false()),
    Column("quantity", Integer, nullable=False, server_default=text("1")),
    Column("phone", Text, nullable=False, server_default=""),
    Column("address", Text, nullable=False, server_default=""),
    Column("outcome", String(16), nullable=False, server_default=""),
    Column("remind_count", Integer, nullable=False, server_default=text("0")),
    Column("reminded_at", UTCDateTime, nullable=True),
    Column("created_at", UTCDateTime, nullable=False, default=_utcnow),
    Column("updated_at", UTCDateTime, nullable=False, default=_utcnow, onupdate=_utcnow),
    CheckConstraint(_in_list("status", ORDER_STATUSES), name="status_valid"),
    Index("ix_orders_status", "status"),
    Index("ix_orders_user_id_product_id_created_at", "user_id", "product_id", "created_at"),
    Index("ix_orders_user_id_chat_open", "user_id", "chat_open"),
    Index("ix_orders_status_outcome", "status", "outcome"),
    sqlite_autoincrement=True,
)

chat_relay_t = Table(
    "chat_relay",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column(
        "order_id",
        Integer,
        ForeignKey("orders.order_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("admin_id", BigInteger, nullable=False),
    Column("message_id", BigInteger, nullable=False),
    Column("created_at", UTCDateTime, nullable=False, default=_utcnow),
    UniqueConstraint("admin_id", "message_id", name="uq_admin_message"),
    Index("ix_chat_relay_order_id", "order_id"),
    sqlite_autoincrement=True,
)

# Customer inquiries: a live chat with the admins that is NOT tied to an order (the customer tapped
# "contact the admin" under a product). One row per customer; `is_open` + `updated_at` decide
# whether his messages are still relayed. `claimed_by` is the one admin currently handling the
# chat (NULL until someone accepts it); `idle_notice_sent` guards the 1-hour "still there?" ping
# from firing more than once per idle spell.
inquiries_t = Table(
    "inquiries",
    metadata,
    Column("user_id", BigInteger, primary_key=True, autoincrement=False),
    Column("product_id", Integer, nullable=True),
    Column("user_fullname", Text, nullable=False, server_default=""),
    Column("is_open", Boolean, nullable=False, server_default=false()),
    Column("claimed_by", BigInteger, nullable=True),
    Column("idle_notice_sent", Boolean, nullable=False, server_default=false()),
    Column("updated_at", UTCDateTime, nullable=False, default=_utcnow, onupdate=_utcnow),
)

# Which message in which admin's chat belongs to which customer's inquiry (so a reply reaches him).
inquiry_relay_t = Table(
    "inquiry_relay",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", BigInteger, nullable=False),
    Column("admin_id", BigInteger, nullable=False),
    Column("message_id", BigInteger, nullable=False),
    Column("created_at", UTCDateTime, nullable=False, default=_utcnow),
    UniqueConstraint("admin_id", "message_id", name="uq_inquiry_admin_message"),
    Index("ix_inquiry_relay_user_id", "user_id"),
    sqlite_autoincrement=True,
)

# The currently-live "accept this chat" / "continue or end?" card message sent to each admin for
# one inquiry. Replaced every time a fresh card goes out, and popped (read + deleted) as soon as
# it is resolved (claimed, continued or closed), so a stale reference can never outlive its button.
inquiry_notice_t = Table(
    "inquiry_notice",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", BigInteger, nullable=False),
    Column("admin_id", BigInteger, nullable=False),
    Column("message_id", BigInteger, nullable=False),
    Column("created_at", UTCDateTime, nullable=False, default=_utcnow),
    Index("ix_inquiry_notice_user_id", "user_id"),
    sqlite_autoincrement=True,
)

# Transcript of one inquiry, in order: enough to replay the conversation (via copy_message, from
# the original chat_id/message_id) to whichever admin ends up owning it later. `sender` is
# "customer" or "admin".
inquiry_history_t = Table(
    "inquiry_history",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", BigInteger, nullable=False),
    Column("chat_id", BigInteger, nullable=False),
    Column("message_id", BigInteger, nullable=False),
    Column("sender", String(10), nullable=False),
    Column("created_at", UTCDateTime, nullable=False, default=_utcnow),
    CheckConstraint(_in_list("sender", ("customer", "admin")), name="sender_valid"),
    Index("ix_inquiry_history_user_id", "user_id"),
    sqlite_autoincrement=True,
)

api_key_state_t = Table(
    "api_key_state",
    metadata,
    Column("kid", String(16), primary_key=True),
    Column("exhausted_until", UTCDateTime, nullable=True),
    Column("invalid", Boolean, nullable=False, server_default=false()),
    Column("usage_today", Integer, nullable=False, server_default=text("0")),
    Column("usage_date", String(10), nullable=True),
    Column("last_reason", Text, nullable=True),
)

bad_pairs_t = Table(
    "bad_pairs",
    metadata,
    Column("kid", String(16), nullable=False),
    Column("model", String(200), nullable=False),
    PrimaryKeyConstraint("kid", "model"),
)

admins_t = Table(
    "admins",
    metadata,
    Column("user_id", BigInteger, primary_key=True, autoincrement=False),
    Column("added_by", BigInteger, nullable=False),
    Column("created_at", UTCDateTime, nullable=False, default=_utcnow),
)

chats_t = Table(
    "chats",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("chat_id", BigInteger, nullable=False),
    Column("role", String(10), nullable=False),
    Column("chat_type", String(12), nullable=False),
    Column("title", String(255), nullable=False, server_default=text("''")),
    Column("username", String(64), nullable=True),
    Column("added_by", BigInteger, nullable=False),
    Column("created_at", UTCDateTime, nullable=False, default=_utcnow),
    UniqueConstraint("chat_id", "role"),
    CheckConstraint(_in_list("role", CHAT_ROLES), name="role"),
    CheckConstraint(_in_list("chat_type", CHAT_TYPES), name="chat_type"),
    sqlite_autoincrement=True,
)

pending_chats_t = Table(
    "pending_chats",
    metadata,
    Column("chat_id", BigInteger, primary_key=True, autoincrement=False),
    Column("chat_type", String(12), nullable=False),
    Column("title", String(255), nullable=False, server_default=text("''")),
    Column("added_by", BigInteger, nullable=True),
    Column("added_at", UTCDateTime, nullable=False, default=_utcnow),
    Column("expires_at", UTCDateTime, nullable=False),
    CheckConstraint(_in_list("chat_type", CHAT_TYPES), name="chat_type"),
    Index("ix_pending_chats_expires_at", "expires_at"),
)

schema_version_t = Table(
    "schema_version",
    metadata,
    Column("version", Integer, primary_key=True, autoincrement=False),
    Column("applied_at", UTCDateTime, nullable=False, default=_utcnow),
)

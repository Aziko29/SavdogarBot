"""Products CRUD: creation, AI pipeline state, categories, posting counters, listing."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.sql.elements import ColumnElement

from db.engine import dialect_insert, engine, logged_db
from db.models import AI_STATUSES, CATEGORIES, PRODUCT_STATUSES, Product, product_from_row
from db.schema import products_t
from utils import utcnow

logger = logging.getLogger("db.products")

_STALE_PROCESSING_MINUTES = 10
_CHUNK = 500
_AI_TEXT_FIELDS = ("name", "price", "size", "fabric", "stock", "hashtags")
_PROTECTED = frozenset(
    {"id", "source_chat_id", "source_msg_id", "file_unique_id", "created_at", "updated_at"}
)
_EDITABLE = frozenset(products_t.c.keys()) - _PROTECTED
_LIST_FILTERS = ("active", "sold", "review", "failed", "all")


def _check_choice(name: str, value: str, allowed: tuple[str, ...]) -> None:
    """Raise ValueError unless value is one of the allowed choices."""
    if value not in allowed:
        raise ValueError(f"{name} must be one of {', '.join(allowed)}, got {value!r}")


@logged_db
async def create_product_pending(
    source_chat_id: int,
    source_msg_id: int,
    tg_file_id: str,
    file_unique_id: str,
    original_text: str,
) -> int | None:
    """Insert a pending product; returns its id, or None if it is a duplicate."""
    stmt = (
        dialect_insert(products_t)
        .values(
            source_chat_id=source_chat_id,
            source_msg_id=source_msg_id,
            tg_file_id=tg_file_id,
            file_unique_id=file_unique_id,
            original_text=original_text,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        .on_conflict_do_nothing()
        .returning(products_t.c.id)
    )
    async with engine.begin() as conn:
        row = (await conn.execute(stmt)).first()
    if row is None:
        logger.info("Duplicate product ignored (chat=%s msg=%s)", source_chat_id, source_msg_id)
        return None
    return int(row[0])


@logged_db
async def get_product(pid: int) -> Product | None:
    """Fetch a product by id."""
    async with engine.connect() as conn:
        row = (await conn.execute(select(products_t).where(products_t.c.id == pid))).first()
    return product_from_row(row) if row is not None else None


@logged_db
async def get_by_source(source_chat_id: int, source_msg_id: int) -> Product | None:
    """Fetch a product by its source channel message."""
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(products_t).where(
                    products_t.c.source_chat_id == source_chat_id,
                    products_t.c.source_msg_id == source_msg_id,
                )
            )
        ).first()
    return product_from_row(row) if row is not None else None


@logged_db
async def claim_for_ai(pid: int) -> Product | None:
    """Atomically move pending -> processing (attempts+1); None if it was not pending."""
    stmt = (
        update(products_t)
        .where(products_t.c.id == pid, products_t.c.ai_status == "pending")
        .values(ai_status="processing", attempts=products_t.c.attempts + 1)
        .returning(products_t)
    )
    async with engine.begin() as conn:
        row = (await conn.execute(stmt)).first()
    return product_from_row(row) if row is not None else None


@logged_db
async def set_ai_result(pid: int, fields: dict[str, Any], caption_html: str, needs_review: bool) -> None:
    """Store the AI result and mark the product ai_status='done'."""
    values: dict[str, Any] = {}
    for key in _AI_TEXT_FIELDS:
        raw = fields.get(key)
        values[key] = "" if raw is None else str(raw)
    async with engine.begin() as conn:
        result = await conn.execute(
            update(products_t)
            .where(products_t.c.id == pid)
            .values(
                **values,
                ai_json=json.dumps(fields, ensure_ascii=False, default=str),
                caption_html=caption_html,
                needs_review=needs_review,
                ai_status="done",
                next_try_at=None,
            )
        )
    if result.rowcount == 0:
        logger.warning("set_ai_result: product %s not found", pid)


@logged_db
async def set_ai_status(pid: int, status: str, next_try_at: datetime | None = None) -> None:
    """Set ai_status and the next retry time (None clears it)."""
    _check_choice("ai_status", status, AI_STATUSES)
    async with engine.begin() as conn:
        await conn.execute(
            update(products_t)
            .where(products_t.c.id == pid)
            .values(ai_status=status, next_try_at=next_try_at)
        )


@logged_db
async def reset_for_reprocess(pid: int, original_text: str | None = None) -> None:
    """Put a product back to ai_status='pending' with attempts=0 (optionally new source text)."""
    values: dict[str, Any] = {"ai_status": "pending", "attempts": 0, "next_try_at": None}
    if original_text is not None:
        values["original_text"] = original_text
    async with engine.begin() as conn:
        await conn.execute(update(products_t).where(products_t.c.id == pid).values(**values))


@logged_db
async def recoverable_ids() -> list[int]:
    """Reset stuck 'processing' rows to pending and return the ids of due pending products."""
    now = utcnow()
    cutoff = now - timedelta(minutes=_STALE_PROCESSING_MINUTES)
    async with engine.begin() as conn:
        reset = await conn.execute(
            update(products_t)
            .where(products_t.c.ai_status == "processing", products_t.c.updated_at < cutoff)
            .values(ai_status="pending", next_try_at=None)
        )
        if reset.rowcount:
            logger.warning("Reset %s stuck 'processing' product(s) to pending", reset.rowcount)
        rows = await conn.execute(
            select(products_t.c.id)
            .where(
                products_t.c.ai_status == "pending",
                or_(products_t.c.next_try_at.is_(None), products_t.c.next_try_at <= now),
            )
            .order_by(products_t.c.id)
        )
        return [int(r[0]) for r in rows]


@logged_db
async def set_status(pid: int, status: str) -> None:
    """Set product status: active | sold | removed."""
    _check_choice("status", status, PRODUCT_STATUSES)
    async with engine.begin() as conn:
        await conn.execute(update(products_t).where(products_t.c.id == pid).values(status=status))


@logged_db
async def set_category(pid: int, category: str, lock: bool) -> None:
    """Set the category and whether automatic recomputation may change it."""
    _check_choice("category", category, CATEGORIES)
    async with engine.begin() as conn:
        await conn.execute(
            update(products_t)
            .where(products_t.c.id == pid)
            .values(category=category, category_locked=lock)
        )


@logged_db
async def recompute_categories(new_keep: int, mid_keep: int) -> None:
    """Rank non-locked active products newest first: top new_keep=new, next mid_keep=mid, rest=old."""
    if new_keep < 0 or mid_keep < 0:
        raise ValueError("new_keep and mid_keep must be >= 0")
    async with engine.begin() as conn:
        rows = await conn.execute(
            select(products_t.c.id)
            .where(products_t.c.status == "active", products_t.c.category_locked.is_(False))
            .order_by(products_t.c.created_at.desc(), products_t.c.id.desc())
        )
        ids = [int(r[0]) for r in rows]
        buckets = {
            "new": ids[:new_keep],
            "mid": ids[new_keep : new_keep + mid_keep],
            "old": ids[new_keep + mid_keep :],
        }
        for category, bucket in buckets.items():
            for i in range(0, len(bucket), _CHUNK):
                chunk = bucket[i : i + _CHUNK]
                await conn.execute(
                    update(products_t)
                    .where(products_t.c.id.in_(chunk), products_t.c.category != category)
                    .values(category=category)
                )


@logged_db
async def list_postable() -> list[Product]:
    """Return active products whose AI result is ready."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            select(products_t)
            .where(products_t.c.status == "active", products_t.c.ai_status == "done")
            .order_by(products_t.c.id)
        )
        return [product_from_row(r) for r in rows]


@logged_db
async def mark_posted(pid: int) -> None:
    """Atomically set last_posted_at=now and post_count = post_count + 1."""
    async with engine.begin() as conn:
        await conn.execute(
            update(products_t)
            .where(products_t.c.id == pid)
            .values(last_posted_at=utcnow(), post_count=products_t.c.post_count + 1)
        )


@logged_db
async def update_fields(pid: int, **fields: Any) -> None:
    """Update whitelisted product columns (validated); unknown or protected columns raise ValueError."""
    if not fields:
        return
    for key, value in fields.items():
        if key not in _EDITABLE:
            raise ValueError(f"Column {key!r} cannot be updated")
        if value is None and not products_t.c[key].nullable:
            raise ValueError(f"Column {key!r} cannot be None")
    if "status" in fields:
        _check_choice("status", fields["status"], PRODUCT_STATUSES)
    if "category" in fields:
        _check_choice("category", fields["category"], CATEGORIES)
    if "ai_status" in fields:
        _check_choice("ai_status", fields["ai_status"], AI_STATUSES)
    async with engine.begin() as conn:
        await conn.execute(update(products_t).where(products_t.c.id == pid).values(**fields))


def _filter_clause(flt: str) -> ColumnElement[bool] | None:
    """Translate a list filter name into a WHERE clause (None = no filter)."""
    if flt == "active":
        return products_t.c.status == "active"
    if flt == "sold":
        return products_t.c.status == "sold"
    if flt == "review":
        return and_(products_t.c.needs_review.is_(True), products_t.c.status != "removed")
    if flt == "failed":
        return products_t.c.ai_status.in_(("failed", "blocked"))
    if flt == "all":
        return None
    raise ValueError(f"flt must be one of {', '.join(_LIST_FILTERS)}, got {flt!r}")


@logged_db
async def list_products(flt: str, offset: int, limit: int) -> tuple[list[Product], int]:
    """Return one page of products (newest first) and the total count for the filter."""
    clause = _filter_clause(flt)
    if offset < 0 or limit < 1:
        raise ValueError("offset must be >= 0 and limit must be >= 1")
    page = select(products_t).order_by(products_t.c.created_at.desc(), products_t.c.id.desc())
    count = select(func.count()).select_from(products_t)
    if clause is not None:
        page = page.where(clause)
        count = count.where(clause)
    async with engine.connect() as conn:
        total = int((await conn.execute(count)).scalar_one())
        rows = await conn.execute(page.offset(offset).limit(limit))
        items = [product_from_row(r) for r in rows]
    return items, total

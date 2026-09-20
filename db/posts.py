"""Post log CRUD: messages published to the target channel."""
from __future__ import annotations

import logging

from sqlalchemy import func, insert, select, update

from db.engine import engine, logged_db
from db.models import PostLog, post_log_from_row
from db.schema import post_log_t

logger = logging.getLogger("db.posts")


@logged_db
async def add_post_log(
    product_id: int, chat_id: int, message_id: int, is_text_only: bool = False
) -> int:
    """Record a published message and return its post_log id."""
    async with engine.begin() as conn:
        result = await conn.execute(
            insert(post_log_t).values(
                product_id=product_id,
                chat_id=chat_id,
                message_id=message_id,
                is_text_only=is_text_only,
            )
        )
    return int(result.inserted_primary_key[0])


@logged_db
async def live_posts(product_id: int) -> list[PostLog]:
    """Return the still-live posts of a product, oldest first."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            select(post_log_t)
            .where(post_log_t.c.product_id == product_id, post_log_t.c.is_live.is_(True))
            .order_by(post_log_t.c.id)
        )
        return [post_log_from_row(r) for r in rows]


@logged_db
async def mark_post_dead(post_id: int) -> None:
    """Mark a post as no longer live (deleted or unreachable)."""
    async with engine.begin() as conn:
        await conn.execute(update(post_log_t).where(post_log_t.c.id == post_id).values(is_live=False))


@logged_db
async def recent_posted_product_ids(n: int) -> list[int]:
    """Return up to n distinct product ids, most recently posted first."""
    if n <= 0:
        return []
    async with engine.connect() as conn:
        rows = await conn.execute(
            select(post_log_t.c.product_id)
            .group_by(post_log_t.c.product_id)
            .order_by(func.max(post_log_t.c.id).desc())
            .limit(n)
        )
        return [int(r[0]) for r in rows]

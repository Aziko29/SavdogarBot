"""Customer inquiries: a live chat with the admins that is not tied to an order.

A customer who taps "contact the admin" under a product gets an *inquiry*. While it is open
(and was active within `INQUIRY_TTL_HOURS`) every message he sends is relayed to the admins,
and any admin can answer by replying to the relayed message.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, update

from db.engine import dialect_insert, engine, logged_db
from db.schema import inquiries_t, inquiry_relay_t
from utils import utcnow

logger = logging.getLogger("db.inquiries")

INQUIRY_TTL_HOURS = 24  # an inquiry with no customer activity for this long stops relaying


@dataclass(slots=True)
class Inquiry:
    """One customer's open question chat."""

    user_id: int
    product_id: int | None
    user_fullname: str


@logged_db
async def open_inquiry(user_id: int, product_id: int | None, fullname: str) -> None:
    """Open (or re-open) this customer's inquiry about a product."""
    values = {
        "product_id": product_id,
        "user_fullname": fullname,
        "is_open": True,
        "updated_at": utcnow(),
    }
    stmt = dialect_insert(inquiries_t).values(user_id=user_id, **values)
    stmt = stmt.on_conflict_do_update(index_elements=["user_id"], set_=values)
    async with engine.begin() as conn:
        await conn.execute(stmt)


@logged_db
async def get_open_inquiry(user_id: int) -> Inquiry | None:
    """The customer's inquiry if it is open and still fresh, else None."""
    cutoff = utcnow() - timedelta(hours=INQUIRY_TTL_HOURS)
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(inquiries_t).where(
                    inquiries_t.c.user_id == user_id,
                    inquiries_t.c.is_open.is_(True),
                    inquiries_t.c.updated_at >= cutoff,
                )
            )
        ).first()
    if row is None:
        return None
    return Inquiry(int(row.user_id), int(row.product_id) if row.product_id is not None else None, row.user_fullname)


@logged_db
async def touch_inquiry(user_id: int) -> bool:
    """Record activity, restarting the inactivity window; True if the inquiry is open (not closed by anyone)."""
    async with engine.begin() as conn:
        result = await conn.execute(
            update(inquiries_t)
            .where(inquiries_t.c.user_id == user_id, inquiries_t.c.is_open.is_(True))
            .values(updated_at=utcnow())
        )
    return result.rowcount == 1


@logged_db
async def close_inquiry_if_open(user_id: int) -> bool:
    """Atomically close the inquiry; True only for the caller that actually closed it."""
    async with engine.begin() as conn:
        result = await conn.execute(
            update(inquiries_t)
            .where(inquiries_t.c.user_id == user_id, inquiries_t.c.is_open.is_(True))
            .values(is_open=False)
        )
    return result.rowcount == 1


@logged_db
async def record_inquiry_relay(user_id: int, admin_id: int, message_id: int) -> None:
    """Remember that this message, in this admin's chat, belongs to this customer's inquiry."""
    stmt = (
        dialect_insert(inquiry_relay_t)
        .values(user_id=user_id, admin_id=admin_id, message_id=message_id)
        .on_conflict_do_nothing(index_elements=["admin_id", "message_id"])
    )
    async with engine.begin() as conn:
        await conn.execute(stmt)


@logged_db
async def find_inquiry_user_by_relay(admin_id: int, message_id: int) -> int | None:
    """Which customer an admin's replied-to message belongs to (None if it is not an inquiry message)."""
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(inquiry_relay_t.c.user_id).where(
                    inquiry_relay_t.c.admin_id == admin_id,
                    inquiry_relay_t.c.message_id == message_id,
                )
            )
        ).first()
    return int(row.user_id) if row is not None else None

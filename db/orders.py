"""Orders CRUD: customer order requests and their admin decisions."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import timedelta

from sqlalchemy import exists, insert, select, update

from db.engine import dialect_insert, engine, logged_db
from db.models import ORDER_STATUSES, Order, dump_admin_msg_ids, order_from_row
from db.schema import chat_relay_t, orders_t
from utils import utcnow

logger = logging.getLogger("db.orders")

_DECISIONS = tuple(s for s in ORDER_STATUSES if s != "pending")


@logged_db
async def create_order(
    product_id: int, user_id: int, username: str | None, fullname: str, comment: str
) -> int:
    """Create a pending order and return its order_id."""
    async with engine.begin() as conn:
        result = await conn.execute(
            insert(orders_t).values(
                product_id=product_id,
                user_id=user_id,
                username=username,
                user_fullname=fullname,
                comment=comment,
            )
        )
    return int(result.inserted_primary_key[0])


@logged_db
async def get_order(order_id: int) -> Order | None:
    """Fetch an order by id."""
    async with engine.connect() as conn:
        row = (await conn.execute(select(orders_t).where(orders_t.c.order_id == order_id))).first()
    return order_from_row(row) if row is not None else None


@logged_db
async def set_admin_msgs(order_id: int, msgs: Mapping[int, int]) -> None:
    """Store {admin_id: message_id} of the notifications sent for this order."""
    async with engine.begin() as conn:
        await conn.execute(
            update(orders_t)
            .where(orders_t.c.order_id == order_id)
            .values(admin_msg_ids=dump_admin_msg_ids(msgs))
        )


@logged_db
async def decide_order(order_id: int, new_status: str) -> bool:
    """Atomically move a pending order to accepted/rejected; True only for the caller that changed it."""
    if new_status not in _DECISIONS:
        raise ValueError(f"new_status must be one of {', '.join(_DECISIONS)}, got {new_status!r}")
    async with engine.begin() as conn:
        result = await conn.execute(
            update(orders_t)
            .where(orders_t.c.order_id == order_id, orders_t.c.status == "pending")
            .values(status=new_status)
        )
    return result.rowcount == 1


@logged_db
async def recent_pending_exists(user_id: int, product_id: int, minutes: int = 10) -> bool:
    """True if this user already has a pending order for the product created within `minutes`."""
    cutoff = utcnow() - timedelta(minutes=minutes)
    stmt = select(
        exists().where(
            orders_t.c.user_id == user_id,
            orders_t.c.product_id == product_id,
            orders_t.c.status == "pending",
            orders_t.c.created_at >= cutoff,
        )
    )
    async with engine.connect() as conn:
        return bool((await conn.execute(stmt)).scalar())


@logged_db
async def list_pending(limit: int = 20) -> list[Order]:
    """Return pending orders, oldest first."""
    if limit < 1:
        raise ValueError("limit must be >= 1")
    async with engine.connect() as conn:
        rows = await conn.execute(
            select(orders_t)
            .where(orders_t.c.status == "pending")
            .order_by(orders_t.c.created_at, orders_t.c.order_id)
            .limit(limit)
        )
        return [order_from_row(r) for r in rows]


@logged_db
async def close_chat_if_open(order_id: int) -> bool:
    """Atomically close the chat relay; True only for the admin whose tap actually closed it.

    Plain read-then-write (get_order().chat_open, then set_chat_open(False)) races when two
    admins tap "end chat" within the same instant: both would read chat_open=True before
    either writes, so both would proceed and the customer would get the close notice twice.
    The WHERE clause makes only one UPDATE's rowcount come back 1.
    """
    async with engine.begin() as conn:
        result = await conn.execute(
            update(orders_t)
            .where(orders_t.c.order_id == order_id, orders_t.c.chat_open.is_(True))
            .values(chat_open=False)
        )
    return result.rowcount == 1


@logged_db
async def set_chat_open(order_id: int, is_open: bool) -> None:
    """Open or close the live-chat relay for an order."""
    async with engine.begin() as conn:
        await conn.execute(
            update(orders_t).where(orders_t.c.order_id == order_id).values(chat_open=is_open)
        )


@logged_db
async def get_open_order_for_user(user_id: int) -> Order | None:
    """Return this user's most recent order with an open live-chat relay, if any."""
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(orders_t)
                .where(orders_t.c.user_id == user_id, orders_t.c.chat_open.is_(True))
                .order_by(orders_t.c.created_at.desc())
                .limit(1)
            )
        ).first()
    return order_from_row(row) if row is not None else None


@logged_db
async def record_relay_message(order_id: int, admin_id: int, message_id: int) -> None:
    """Remember that this message, in this admin's chat, belongs to this order's live chat.

    Lets any admin reply to any relayed message (the original order card or a later
    customer message) and have it routed to the right customer, even much later.
    """
    stmt = (
        dialect_insert(chat_relay_t)
        .values(order_id=order_id, admin_id=admin_id, message_id=message_id)
        .on_conflict_do_nothing(index_elements=["admin_id", "message_id"])
    )
    async with engine.begin() as conn:
        await conn.execute(stmt)


@logged_db
async def find_order_id_by_relay(admin_id: int, message_id: int) -> int | None:
    """Look up which order a message (that an admin just replied to) belongs to."""
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(chat_relay_t.c.order_id).where(
                    chat_relay_t.c.admin_id == admin_id, chat_relay_t.c.message_id == message_id
                )
            )
        ).first()
    return int(row.order_id) if row is not None else None

"""Orders CRUD: customer order requests and their admin decisions."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import timedelta

from sqlalchemy import and_, delete, exists, func, insert, or_, select, update

from db.engine import dialect_insert, engine, logged_db
from db.models import ORDER_OUTCOMES, ORDER_STATUSES, Order, dump_admin_msg_ids, order_from_row
from db.schema import chat_relay_t, orders_t
from utils import utcnow

logger = logging.getLogger("db.orders")

_DECISIONS = tuple(s for s in ORDER_STATUSES if s != "pending")
_FINAL_OUTCOMES = tuple(o for o in ORDER_OUTCOMES if o)

# An order is "finished" once nothing more can happen to it: the seller declined it (or the customer
# withdrew it), or an accepted order was closed as completed / cancelled.
_FINISHED = or_(
    orders_t.c.status == "rejected",
    and_(orders_t.c.status == "accepted", orders_t.c.outcome != ""),
)
_DELETE_CHUNK = 500  # keeps the number of bound parameters far below every database's limit


@logged_db
async def create_order(
    product_id: int,
    user_id: int,
    username: str | None,
    fullname: str,
    comment: str,
    *,
    quantity: int = 1,
    phone: str = "",
    address: str = "",
) -> int:
    """Create a pending order and return its order_id."""
    if quantity < 1:
        raise ValueError(f"quantity must be >= 1, got {quantity}")
    async with engine.begin() as conn:
        result = await conn.execute(
            insert(orders_t).values(
                product_id=product_id,
                user_id=user_id,
                username=username,
                user_fullname=fullname,
                comment=comment,
                quantity=quantity,
                phone=phone,
                address=address,
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


@logged_db
async def finish_order(order_id: int, outcome: str) -> bool:
    """Atomically close an accepted, still-open order as completed/cancelled; True only for the caller that did it.

    The live chat is closed in the same statement, so a finished order can never keep relaying messages.
    """
    if outcome not in _FINAL_OUTCOMES:
        raise ValueError(f"outcome must be one of {', '.join(_FINAL_OUTCOMES)}, got {outcome!r}")
    async with engine.begin() as conn:
        result = await conn.execute(
            update(orders_t)
            .where(
                orders_t.c.order_id == order_id,
                orders_t.c.status == "accepted",
                orders_t.c.outcome == "",
            )
            .values(outcome=outcome, chat_open=False)
        )
    return result.rowcount == 1


@logged_db
async def cancel_pending_by_user(order_id: int, user_id: int) -> bool:
    """Let the customer withdraw his own order while it is still pending; True if it was withdrawn."""
    async with engine.begin() as conn:
        result = await conn.execute(
            update(orders_t)
            .where(
                orders_t.c.order_id == order_id,
                orders_t.c.user_id == user_id,
                orders_t.c.status == "pending",
            )
            .values(status="rejected", outcome="cancelled", chat_open=False)
        )
    return result.rowcount == 1


@logged_db
async def list_user_orders(user_id: int, limit: int = 10) -> list[Order]:
    """Return this customer's orders, newest first."""
    if limit < 1:
        raise ValueError("limit must be >= 1")
    async with engine.connect() as conn:
        rows = await conn.execute(
            select(orders_t)
            .where(orders_t.c.user_id == user_id)
            .order_by(orders_t.c.created_at.desc(), orders_t.c.order_id.desc())
            .limit(limit)
        )
        return [order_from_row(r) for r in rows]


@logged_db
async def list_in_progress(limit: int = 20) -> list[Order]:
    """Return accepted orders that are neither completed nor cancelled yet, oldest first."""
    if limit < 1:
        raise ValueError("limit must be >= 1")
    async with engine.connect() as conn:
        rows = await conn.execute(
            select(orders_t)
            .where(orders_t.c.status == "accepted", orders_t.c.outcome == "")
            .order_by(orders_t.c.created_at, orders_t.c.order_id)
            .limit(limit)
        )
        return [order_from_row(r) for r in rows]


@logged_db
async def list_due_for_reminder(
    after_minutes: int, every_minutes: int, max_reminders: int, limit: int = 20
) -> list[Order]:
    """Pending orders waiting longer than `after_minutes` whose next reminder is due.

    A reminder is due when fewer than `max_reminders` were sent and the previous one (if any)
    is at least `every_minutes` old. Oldest orders first.
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    now = utcnow()
    waited_since = now - timedelta(minutes=after_minutes)
    last_reminder_before = now - timedelta(minutes=every_minutes)
    async with engine.connect() as conn:
        rows = await conn.execute(
            select(orders_t)
            .where(
                orders_t.c.status == "pending",
                orders_t.c.remind_count < max_reminders,
                orders_t.c.created_at <= waited_since,
                or_(orders_t.c.reminded_at.is_(None), orders_t.c.reminded_at <= last_reminder_before),
            )
            .order_by(orders_t.c.created_at, orders_t.c.order_id)
            .limit(limit)
        )
        return [order_from_row(r) for r in rows]


@logged_db
async def mark_reminded(order_id: int) -> None:
    """Record that one more reminder about this still-pending order was sent (atomic counter)."""
    async with engine.begin() as conn:
        await conn.execute(
            update(orders_t)
            .where(orders_t.c.order_id == order_id, orders_t.c.status == "pending")
            .values(remind_count=orders_t.c.remind_count + 1, reminded_at=utcnow())
        )


@logged_db
async def count_order_groups() -> tuple[int, int, int]:
    """Return (pending, in progress, finished) order counts for the admin orders menu."""

    async def count(*conditions: object) -> int:
        async with engine.connect() as conn:
            value = (await conn.execute(select(func.count()).select_from(orders_t).where(*conditions))).scalar()
        return int(value or 0)

    pending = await count(orders_t.c.status == "pending")
    in_progress = await count(orders_t.c.status == "accepted", orders_t.c.outcome == "")
    finished = await count(_FINISHED)
    return pending, in_progress, finished


@logged_db
async def list_finished(limit: int = 20) -> list[Order]:
    """Return finished orders (declined, withdrawn, completed, cancelled), newest first."""
    if limit < 1:
        raise ValueError("limit must be >= 1")
    async with engine.connect() as conn:
        rows = await conn.execute(
            select(orders_t)
            .where(_FINISHED)
            .order_by(orders_t.c.updated_at.desc(), orders_t.c.order_id.desc())
            .limit(limit)
        )
        return [order_from_row(r) for r in rows]


@logged_db
async def count_finished(older_than_days: int | None = None) -> int:
    """How many finished orders a clean-up would remove (optionally only those untouched for N days)."""
    stmt = select(func.count()).select_from(orders_t).where(_FINISHED)
    if older_than_days is not None:
        stmt = stmt.where(orders_t.c.updated_at < utcnow() - timedelta(days=older_than_days))
    async with engine.connect() as conn:
        return int((await conn.execute(stmt)).scalar() or 0)


@logged_db
async def purge_finished_orders(older_than_days: int | None = None) -> tuple[int, list[tuple[int, int]]]:
    """Delete finished orders (optionally only those untouched for N days); pending and in-progress orders are never touched.

    Returns (number of deleted orders, [(admin_id, message_id), ...]) where the pairs are every
    message the bot put into the admins' chats for those orders (order cards and relayed customer
    messages), so the caller can delete them from the chats too. All in one transaction.
    """
    if older_than_days is not None and older_than_days < 0:
        raise ValueError("older_than_days must be >= 0")
    select_stmt = select(orders_t).where(_FINISHED)
    if older_than_days is not None:
        select_stmt = select_stmt.where(orders_t.c.updated_at < utcnow() - timedelta(days=older_than_days))

    messages: set[tuple[int, int]] = set()
    async with engine.begin() as conn:
        orders = [order_from_row(r) for r in await conn.execute(select_stmt)]
        ids = [o.order_id for o in orders]
        for order in orders:
            messages.update((admin_id, msg_id) for admin_id, msg_id in order.admin_msg_ids.items())
        deleted = 0
        for start in range(0, len(ids), _DELETE_CHUNK):
            chunk = ids[start : start + _DELETE_CHUNK]
            relay_rows = await conn.execute(
                select(chat_relay_t.c.admin_id, chat_relay_t.c.message_id).where(chat_relay_t.c.order_id.in_(chunk))
            )
            messages.update((int(r.admin_id), int(r.message_id)) for r in relay_rows)
            await conn.execute(delete(chat_relay_t).where(chat_relay_t.c.order_id.in_(chunk)))
            # The status check is repeated so an order that changed meanwhile can never be deleted.
            result = await conn.execute(delete(orders_t).where(orders_t.c.order_id.in_(chunk), _FINISHED))
            deleted += int(result.rowcount or 0)
    return deleted, sorted(messages)

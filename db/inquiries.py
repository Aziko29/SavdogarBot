"""Customer inquiries: a live chat with the admins that is not tied to an order.

A customer who taps "contact the admin" under a product gets an *inquiry*. It starts unclaimed:
the opening notice goes to every admin, and whoever taps "Qabul qilish" first (or is the first to
reply to it) becomes the one admin who receives everything from then on (see `try_claim_inquiry`).
If the claimed admin goes quiet for a while, `inquiry_idle.py` re-offers the chat to everyone (with
"continue" / "end chat" buttons) so it never gets stuck with someone who stepped away; the first admin
to tap "continue" gets it (see `try_continue_inquiry`).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, insert, select, update

from db.engine import dialect_insert, engine, logged_db
from db.schema import inquiries_t, inquiry_history_t, inquiry_notice_t, inquiry_relay_t
from utils import utcnow

logger = logging.getLogger("db.inquiries")

INQUIRY_TTL_HOURS = 24  # an inquiry with no customer activity for this long stops relaying


@dataclass(slots=True)
class Inquiry:
    """One customer's open question chat."""

    user_id: int
    product_id: int | None
    user_fullname: str
    claimed_by: int | None = None


@dataclass(slots=True)
class ClaimResult:
    """Outcome of trying to claim (or continue) an inquiry."""

    success: bool  # True: the caller now owns the chat
    newly_claimed: bool  # True only if it was unclaimed right before this call
    is_open: bool  # False: the inquiry is closed/expired, `success` is always False too
    claimed_by: int | None  # who owns it now (None if nobody does, e.g. it just got closed)


@dataclass(slots=True)
class HistoryEntry:
    """One transcript entry: where to `copy_message` it from, and who sent it."""

    chat_id: int
    message_id: int
    sender: str  # "customer" | "admin"


@logged_db
async def open_inquiry(user_id: int, product_id: int | None, fullname: str) -> None:
    """Open (or re-open) this customer's inquiry about a product, unclaimed, with a clean transcript."""
    values = {
        "product_id": product_id,
        "user_fullname": fullname,
        "is_open": True,
        "claimed_by": None,
        "idle_notice_sent": False,
        "updated_at": utcnow(),
    }
    stmt = dialect_insert(inquiries_t).values(user_id=user_id, **values)
    stmt = stmt.on_conflict_do_update(index_elements=["user_id"], set_=values)
    async with engine.begin() as conn:
        await conn.execute(stmt)
        # A re-opened inquiry starts a fresh conversation; nothing from an earlier round applies.
        await conn.execute(delete(inquiry_history_t).where(inquiry_history_t.c.user_id == user_id))
        await conn.execute(delete(inquiry_notice_t).where(inquiry_notice_t.c.user_id == user_id))


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
    return Inquiry(
        int(row.user_id),
        int(row.product_id) if row.product_id is not None else None,
        row.user_fullname,
        int(row.claimed_by) if row.claimed_by is not None else None,
    )


@logged_db
async def touch_inquiry(user_id: int) -> bool:
    """Record activity, restarting the inactivity window; True if the inquiry is open (not closed by anyone).

    Also clears the idle flag: any activity (customer or admin) means the chat is not actually
    quiet, so the 1-hour "still there?" check is free to fire again after the next quiet spell.
    """
    async with engine.begin() as conn:
        result = await conn.execute(
            update(inquiries_t)
            .where(inquiries_t.c.user_id == user_id, inquiries_t.c.is_open.is_(True))
            .values(updated_at=utcnow(), idle_notice_sent=False)
        )
    return result.rowcount == 1


@logged_db
async def close_inquiry_if_open(user_id: int) -> bool:
    """Atomically close the inquiry; True only for the caller that actually closed it."""
    async with engine.begin() as conn:
        result = await conn.execute(
            update(inquiries_t)
            .where(inquiries_t.c.user_id == user_id, inquiries_t.c.is_open.is_(True))
            .values(is_open=False, claimed_by=None)
        )
        if result.rowcount == 1:
            await conn.execute(delete(inquiry_history_t).where(inquiry_history_t.c.user_id == user_id))
            await conn.execute(delete(inquiry_notice_t).where(inquiry_notice_t.c.user_id == user_id))
    return result.rowcount == 1


@logged_db
async def try_claim_inquiry(user_id: int, admin_id: int) -> ClaimResult:
    """Assign the inquiry to `admin_id` if nobody else already owns it (atomic: only one caller wins).

    Calling it again with the same admin_id is a harmless no-op success (so a reply from the
    admin who already owns the chat doesn't need special-casing by the caller).
    """
    async with engine.begin() as conn:
        # The single atomic UPDATE is what actually decides the race: only one concurrent caller
        # can match `claimed_by IS NULL`, so only one gets rowcount == 1 here, same as decide_order().
        won = await conn.execute(
            update(inquiries_t)
            .where(
                inquiries_t.c.user_id == user_id,
                inquiries_t.c.is_open.is_(True),
                inquiries_t.c.claimed_by.is_(None),
            )
            .values(claimed_by=admin_id)
        )
        if won.rowcount == 1:
            return ClaimResult(success=True, newly_claimed=True, is_open=True, claimed_by=admin_id)

        row = (
            await conn.execute(
                select(inquiries_t.c.is_open, inquiries_t.c.claimed_by).where(inquiries_t.c.user_id == user_id)
            )
        ).first()
    if row is None or not row.is_open:
        return ClaimResult(success=False, newly_claimed=False, is_open=False, claimed_by=None)
    current = int(row.claimed_by) if row.claimed_by is not None else None
    return ClaimResult(success=current == admin_id, newly_claimed=False, is_open=True, claimed_by=current)


@logged_db
async def try_continue_inquiry(user_id: int, admin_id: int) -> ClaimResult:
    """Hand a quiet inquiry to `admin_id` after he tapped "continue" on the idle notice (first tap wins).

    Only works while the idle notice is outstanding (`idle_notice_sent`): the single atomic UPDATE
    clears that flag, so of several admins tapping at once exactly one gets `success=True`, and a
    stale notice (the chat woke up on its own, or someone already continued it) fails cleanly with
    `claimed_by` naming whoever owns the chat now.
    """
    async with engine.begin() as conn:
        won = await conn.execute(
            update(inquiries_t)
            .where(
                inquiries_t.c.user_id == user_id,
                inquiries_t.c.is_open.is_(True),
                inquiries_t.c.idle_notice_sent.is_(True),
            )
            .values(claimed_by=admin_id, idle_notice_sent=False, updated_at=utcnow())
        )
        if won.rowcount == 1:
            return ClaimResult(success=True, newly_claimed=True, is_open=True, claimed_by=admin_id)
        row = (
            await conn.execute(
                select(inquiries_t.c.is_open, inquiries_t.c.claimed_by).where(inquiries_t.c.user_id == user_id)
            )
        ).first()
    if row is None or not row.is_open:
        return ClaimResult(success=False, newly_claimed=False, is_open=False, claimed_by=None)
    current = int(row.claimed_by) if row.claimed_by is not None else None
    return ClaimResult(success=False, newly_claimed=False, is_open=True, claimed_by=current)


@logged_db
async def list_idle_claimed_inquiries(older_than_hours: float) -> list[Inquiry]:
    """Open, claimed inquiries with no activity for `older_than_hours` that haven't been pinged yet."""
    cutoff = utcnow() - timedelta(hours=older_than_hours)
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(inquiries_t).where(
                    inquiries_t.c.is_open.is_(True),
                    inquiries_t.c.claimed_by.is_not(None),
                    inquiries_t.c.idle_notice_sent.is_(False),
                    inquiries_t.c.updated_at < cutoff,
                )
            )
        ).all()
    return [
        Inquiry(
            int(r.user_id),
            int(r.product_id) if r.product_id is not None else None,
            r.user_fullname,
            int(r.claimed_by) if r.claimed_by is not None else None,
        )
        for r in rows
    ]


@logged_db
async def mark_idle_notice_sent(user_id: int, older_than_hours: float | None = None) -> bool:
    """Atomically record that the idle notice goes out now; True only for the caller that flipped the flag.

    With `older_than_hours`, the inquiry must still be claimed, un-pinged and quiet for that long, so
    a message that arrives between the sweep's read and this call cancels the ping instead of racing it.
    `updated_at` is deliberately written back unchanged: the ping itself is not chat activity, and the
    column's onupdate hook would otherwise restart the very inactivity window being measured.
    """
    conditions = [
        inquiries_t.c.user_id == user_id,
        inquiries_t.c.is_open.is_(True),
        inquiries_t.c.idle_notice_sent.is_(False),
    ]
    if older_than_hours is not None:
        conditions.append(inquiries_t.c.claimed_by.is_not(None))
        conditions.append(inquiries_t.c.updated_at < utcnow() - timedelta(hours=older_than_hours))
    async with engine.begin() as conn:
        result = await conn.execute(
            update(inquiries_t).where(*conditions).values(idle_notice_sent=True, updated_at=inquiries_t.c.updated_at)
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


@logged_db
async def replace_inquiry_notice_cards(user_id: int, cards: list[tuple[int, int]]) -> None:
    """Remember the (admin_id, message_id) card(s) just sent, replacing whatever was tracked before."""
    async with engine.begin() as conn:
        await conn.execute(delete(inquiry_notice_t).where(inquiry_notice_t.c.user_id == user_id))
        if cards:
            await conn.execute(
                insert(inquiry_notice_t),
                [{"user_id": user_id, "admin_id": admin_id, "message_id": message_id} for admin_id, message_id in cards],
            )


@logged_db
async def pop_inquiry_notice_cards(user_id: int) -> list[tuple[int, int]]:
    """Return and forget the tracked card(s) for this inquiry (resolved: claimed, continued or closed)."""
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(inquiry_notice_t.c.admin_id, inquiry_notice_t.c.message_id).where(
                    inquiry_notice_t.c.user_id == user_id
                )
            )
        ).all()
        await conn.execute(delete(inquiry_notice_t).where(inquiry_notice_t.c.user_id == user_id))
    return [(int(r.admin_id), int(r.message_id)) for r in rows]


@logged_db
async def record_inquiry_history(user_id: int, chat_id: int, message_id: int, sender: str) -> None:
    """Append one transcript entry (its origin chat/message, so it can be copy_message'd back later)."""
    async with engine.begin() as conn:
        await conn.execute(
            insert(inquiry_history_t).values(
                user_id=user_id, chat_id=chat_id, message_id=message_id, sender=sender
            )
        )


@logged_db
async def get_inquiry_history(user_id: int) -> list[HistoryEntry]:
    """The inquiry's transcript so far, oldest first."""
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(inquiry_history_t.c.chat_id, inquiry_history_t.c.message_id, inquiry_history_t.c.sender)
                .where(inquiry_history_t.c.user_id == user_id)
                .order_by(inquiry_history_t.c.id)
            )
        ).all()
    return [HistoryEntry(int(r.chat_id), int(r.message_id), r.sender) for r in rows]

"""Source / target chats managed from inside the bot (head admin only).

`source` = a channel the bot reads new posts from (it must be admin there);
`target` = a channel or group the bot publishes products to.

The set is mirrored in RAM so the (synchronous) source filter and the poster never touch the
database per update. Call `load_chats()` once at startup after `init_db()`.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, insert, select, update

from db.engine import engine, logged_db
from db.models import CHAT_ROLES, CHAT_TYPES
from db.schema import chats_t, pending_chats_t

logger = logging.getLogger("db.chats")

ROLE_SOURCE = "source"
ROLE_TARGET = "target"

_MAX_TITLE_LEN = 255
_MAX_USERNAME_LEN = 64


@dataclass(frozen=True, slots=True)
class ChatEntry:
    """One registered chat."""

    id: int
    chat_id: int
    role: str
    chat_type: str
    title: str
    username: str | None
    added_by: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PendingChat:
    """A chat the bot was added to but that is not registered; the bot leaves at `expires_at`."""

    chat_id: int
    chat_type: str
    title: str
    added_by: int | None
    added_at: datetime
    expires_at: datetime


_cache: tuple[ChatEntry, ...] = ()
_source_ids: frozenset[int] = frozenset()
_lock = asyncio.Lock()


def _to_entry(row: Any) -> ChatEntry:
    return ChatEntry(
        id=int(row.id),
        chat_id=int(row.chat_id),
        role=str(row.role),
        chat_type=str(row.chat_type),
        title=str(row.title or ""),
        username=row.username,
        added_by=int(row.added_by),
        created_at=row.created_at,
    )


def is_source_chat(chat_id: int | None) -> bool:
    """True when `chat_id` is a registered source channel (RAM lookup, no DB access)."""
    return chat_id is not None and chat_id in _source_ids


def source_chats() -> list[ChatEntry]:
    """Registered source channels, oldest first (RAM)."""
    return [c for c in _cache if c.role == ROLE_SOURCE]


def target_chats() -> list[ChatEntry]:
    """Registered posting channels/groups, oldest first (RAM)."""
    return [c for c in _cache if c.role == ROLE_TARGET]


async def _reload() -> None:
    global _cache, _source_ids
    async with engine.connect() as conn:
        rows = (await conn.execute(select(chats_t).order_by(chats_t.c.created_at, chats_t.c.id))).all()
    entries = tuple(_to_entry(r) for r in rows)
    _cache = entries
    _source_ids = frozenset(e.chat_id for e in entries if e.role == ROLE_SOURCE)


@logged_db
async def load_chats() -> None:
    """(Re)load the registered chats into the RAM cache."""
    async with _lock:
        await _reload()
    logger.info("Chats loaded: %d source, %d target", len(source_chats()), len(target_chats()))


@logged_db
async def list_chats(role: str | None = None) -> list[ChatEntry]:
    """Registered chats straight from the database, oldest first; optionally only one role."""
    stmt = select(chats_t).order_by(chats_t.c.created_at, chats_t.c.id)
    if role is not None:
        stmt = stmt.where(chats_t.c.role == role)
    async with engine.connect() as conn:
        rows = (await conn.execute(stmt)).all()
    return [_to_entry(r) for r in rows]


@logged_db
async def get_chat_entry(row_id: int) -> ChatEntry | None:
    """One registered chat by its row id, or None."""
    async with engine.connect() as conn:
        row = (await conn.execute(select(chats_t).where(chats_t.c.id == row_id))).first()
    return _to_entry(row) if row is not None else None


@logged_db
async def add_chat(
    chat_id: int,
    role: str,
    chat_type: str,
    title: str,
    username: str | None,
    added_by: int,
) -> bool:
    """Register a chat under a role; True if newly added, False if it only refreshed the details.

    Raises ValueError for invalid input, for a source that is not a channel, and when the same
    chat already has the other role (a chat must never be both source and target: the bot's own
    posts would feed back into the source).
    """
    if isinstance(chat_id, bool) or not isinstance(chat_id, int) or chat_id == 0:
        raise ValueError(f"Invalid chat id: {chat_id!r}")
    if role not in CHAT_ROLES:
        raise ValueError(f"Invalid role: {role!r}")
    if chat_type not in CHAT_TYPES:
        raise ValueError(f"Unsupported chat type: {chat_type!r}")
    if role == ROLE_SOURCE and chat_type != "channel":
        raise ValueError("The source must be a channel")

    title = (title or "")[:_MAX_TITLE_LEN]
    username = username[:_MAX_USERNAME_LEN] if username else None
    other_role = ROLE_TARGET if role == ROLE_SOURCE else ROLE_SOURCE

    async with _lock:
        async with engine.begin() as conn:
            clash = (
                await conn.execute(
                    select(chats_t.c.id).where(chats_t.c.chat_id == chat_id, chats_t.c.role == other_role)
                )
            ).first()
            if clash is not None:
                raise ValueError("Bu chat allaqachon boshqa vazifaga belgilangan (bir chat ham manba, ham post joyi bo'lolmaydi)")
            existing = (
                await conn.execute(
                    select(chats_t.c.id).where(chats_t.c.chat_id == chat_id, chats_t.c.role == role)
                )
            ).first()
            if existing is None:
                await conn.execute(
                    insert(chats_t).values(
                        chat_id=chat_id,
                        role=role,
                        chat_type=chat_type,
                        title=title,
                        username=username,
                        added_by=added_by,
                    )
                )
            else:
                await conn.execute(
                    update(chats_t)
                    .where(chats_t.c.id == existing[0])
                    .values(chat_type=chat_type, title=title, username=username)
                )
            # A registered chat is no longer "foreign": stop its leave-countdown.
            await conn.execute(delete(pending_chats_t).where(pending_chats_t.c.chat_id == chat_id))
        await _reload()
    added = existing is None
    if added:
        logger.info("Chat %s registered as %s by %s", chat_id, role, added_by)
    return added


@logged_db
async def remove_chat(row_id: int) -> ChatEntry | None:
    """Unregister one chat by row id; returns the removed entry or None if it was already gone."""
    async with _lock:
        async with engine.begin() as conn:
            row = (await conn.execute(select(chats_t).where(chats_t.c.id == row_id))).first()
            if row is None:
                return None
            await conn.execute(delete(chats_t).where(chats_t.c.id == row_id))
        await _reload()
    entry = _to_entry(row)
    logger.info("Chat %s (%s) unregistered", entry.chat_id, entry.role)
    return entry


@logged_db
async def remove_chats_for(chat_id: int) -> list[ChatEntry]:
    """Unregister a chat under every role (used when the bot is removed from it)."""
    async with _lock:
        async with engine.begin() as conn:
            rows = (await conn.execute(select(chats_t).where(chats_t.c.chat_id == chat_id))).all()
            await conn.execute(delete(pending_chats_t).where(pending_chats_t.c.chat_id == chat_id))
            if not rows:
                return []
            await conn.execute(delete(chats_t).where(chats_t.c.chat_id == chat_id))
        await _reload()
    entries = [_to_entry(r) for r in rows]
    logger.info("Chat %s unregistered from %d role(s) (bot removed)", chat_id, len(entries))
    return entries


# ---------------------------------------------------------------- unregistered chats (leave countdown)


def _to_pending(row: Any) -> PendingChat:
    return PendingChat(
        chat_id=int(row.chat_id),
        chat_type=str(row.chat_type),
        title=str(row.title or ""),
        added_by=int(row.added_by) if row.added_by is not None else None,
        added_at=row.added_at,
        expires_at=row.expires_at,
    )


@logged_db
async def track_pending(
    chat_id: int, chat_type: str, title: str, added_by: int | None, expires_at: datetime
) -> bool:
    """Start the leave-countdown for an unregistered chat; True if it was newly tracked.

    Nothing happens for a registered chat. A chat that is already tracked keeps its ORIGINAL
    deadline (only the title is refreshed), so repeated membership updates cannot extend it.
    """
    if chat_type not in CHAT_TYPES:
        raise ValueError(f"Unsupported chat type: {chat_type!r}")
    if expires_at.tzinfo is None:
        raise ValueError("expires_at must be an aware UTC datetime")
    title = (title or "")[:_MAX_TITLE_LEN]
    async with _lock:
        async with engine.begin() as conn:
            registered = (await conn.execute(select(chats_t.c.id).where(chats_t.c.chat_id == chat_id))).first()
            if registered is not None:
                return False
            existing = (
                await conn.execute(select(pending_chats_t.c.chat_id).where(pending_chats_t.c.chat_id == chat_id))
            ).first()
            if existing is None:
                await conn.execute(
                    insert(pending_chats_t).values(
                        chat_id=chat_id,
                        chat_type=chat_type,
                        title=title,
                        added_by=added_by,
                        expires_at=expires_at,
                    )
                )
            else:
                await conn.execute(
                    update(pending_chats_t)
                    .where(pending_chats_t.c.chat_id == chat_id)
                    .values(chat_type=chat_type, title=title)
                )
    created = existing is None
    if created:
        logger.info("Chat %s is not registered; the bot leaves it at %s unless it gets registered", chat_id, expires_at)
    return created


@logged_db
async def clear_pending(chat_id: int) -> bool:
    """Stop tracking a chat (registered, left or gone); True if a row was removed."""
    async with _lock:
        async with engine.begin() as conn:
            result = await conn.execute(delete(pending_chats_t).where(pending_chats_t.c.chat_id == chat_id))
    return result.rowcount == 1


@logged_db
async def list_pending_chats() -> list[PendingChat]:
    """Tracked unregistered chats, the soonest deadline first."""
    async with engine.connect() as conn:
        rows = (await conn.execute(select(pending_chats_t).order_by(pending_chats_t.c.expires_at))).all()
    return [_to_pending(r) for r in rows]


@logged_db
async def due_pending_chats(now: datetime) -> list[PendingChat]:
    """Tracked chats whose deadline has passed (the bot must leave them)."""
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(pending_chats_t).where(pending_chats_t.c.expires_at <= now).order_by(pending_chats_t.c.expires_at)
            )
        ).all()
    return [_to_pending(r) for r in rows]

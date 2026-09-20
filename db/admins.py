"""Admins: the head admin (BOSH_ADMIN_ID) lives in .env, all other admins live in the DB.

The set of DB admins is mirrored in RAM so the (synchronous) AdminFilter and the notification
loops never touch the database. Call `load_admins()` once at startup after `init_db()`.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select

from config import settings
from db.engine import dialect_insert, engine, logged_db
from db.schema import admins_t

logger = logging.getLogger("db.admins")

_MAX_USER_ID = 2**53  # Telegram user IDs fit comfortably below this

_extra: frozenset[int] = frozenset()
_lock = asyncio.Lock()


@dataclass(slots=True)
class AdminEntry:
    """A regular (DB-stored) admin."""

    user_id: int
    added_by: int
    created_at: datetime


def is_head_admin(user_id: int | None) -> bool:
    """True for the main admin from .env."""
    return user_id is not None and user_id == settings.head_admin_id


def is_admin(user_id: int | None) -> bool:
    """True for the head admin and for every admin stored in the DB."""
    return user_id is not None and (user_id == settings.head_admin_id or user_id in _extra)


def admin_ids() -> list[int]:
    """Every admin ID, head admin first, the rest sorted (stable order for notifications)."""
    return [settings.head_admin_id, *sorted(_extra - {settings.head_admin_id})]


def validate_user_id(user_id: int) -> int:
    """Return `user_id` if it is a plausible Telegram user ID; raise ValueError otherwise."""
    if isinstance(user_id, bool) or not isinstance(user_id, int) or not 0 < user_id < _MAX_USER_ID:
        raise ValueError(f"Invalid Telegram user ID: {user_id!r}")
    return user_id


async def _reload() -> None:
    global _extra
    async with engine.connect() as conn:
        rows = (await conn.execute(select(admins_t.c.user_id))).all()
    _extra = frozenset(int(row[0]) for row in rows)


@logged_db
async def load_admins() -> None:
    """(Re)load the DB admins into the RAM cache."""
    async with _lock:
        await _reload()
    logger.info("Admins loaded: 1 head admin + %d regular admin(s)", len(_extra))


@logged_db
async def list_admins() -> list[AdminEntry]:
    """Regular admins, oldest first (the head admin is not stored in the DB)."""
    async with engine.connect() as conn:
        rows = (
            await conn.execute(select(admins_t).order_by(admins_t.c.created_at, admins_t.c.user_id))
        ).all()
    return [AdminEntry(int(r.user_id), int(r.added_by), r.created_at) for r in rows]


@logged_db
async def add_admin(user_id: int, added_by: int) -> bool:
    """Add a regular admin; True if newly added, False if already a regular admin.

    Raises ValueError for an invalid ID or for the head admin (who is always an admin already).
    """
    validate_user_id(user_id)
    if is_head_admin(user_id):
        raise ValueError("The head admin is already an admin")
    async with _lock:
        async with engine.begin() as conn:
            result = await conn.execute(
                dialect_insert(admins_t)
                .values(user_id=user_id, added_by=added_by)
                .on_conflict_do_nothing(index_elements=["user_id"])
            )
        added = result.rowcount == 1
        await _reload()
    if added:
        logger.info("Admin %s added by %s", user_id, added_by)
    return added


@logged_db
async def remove_admin(user_id: int) -> bool:
    """Remove a regular admin; True if one was removed. The head admin can never be removed."""
    validate_user_id(user_id)
    if is_head_admin(user_id):
        raise ValueError("The head admin cannot be removed")
    async with _lock:
        async with engine.begin() as conn:
            result = await conn.execute(delete(admins_t).where(admins_t.c.user_id == user_id))
        removed = result.rowcount == 1
        await _reload()
    if removed:
        logger.info("Admin %s removed", user_id)
    return removed

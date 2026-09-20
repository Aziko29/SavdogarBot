"""API key state: exhaustion, invalid keys, unsupported (key, model) pairs and daily usage."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import case, select

from db.engine import dialect_insert, engine, logged_db
from db.schema import api_key_state_t, bad_pairs_t
from utils import local_now

logger = logging.getLogger("db.keystate")


def _today() -> str:
    """Current local date as ISO text (the usage counter's reset boundary)."""
    return local_now().date().isoformat()


@logged_db
async def mark_key_exhausted(kid: str, until: datetime, reason: str) -> None:
    """Mark a key as unusable until the given aware datetime."""
    stmt = dialect_insert(api_key_state_t).values(kid=kid, exhausted_until=until, last_reason=reason)
    async with engine.begin() as conn:
        await conn.execute(
            stmt.on_conflict_do_update(
                index_elements=["kid"],
                set_={"exhausted_until": until, "last_reason": reason},
            )
        )


@logged_db
async def mark_key_invalid(kid: str) -> None:
    """Permanently mark a key as invalid (rejected by the provider)."""
    stmt = dialect_insert(api_key_state_t).values(kid=kid, invalid=True, last_reason="invalid key")
    async with engine.begin() as conn:
        await conn.execute(
            stmt.on_conflict_do_update(
                index_elements=["kid"],
                set_={"invalid": True, "last_reason": "invalid key"},
            )
        )


@logged_db
async def load_key_states() -> dict[str, dict[str, Any]]:
    """Return {kid: {"exhausted_until", "invalid", "usage_today"}}; stale-day usage counts as 0."""
    today = _today()
    async with engine.connect() as conn:
        rows = await conn.execute(
            select(
                api_key_state_t.c.kid,
                api_key_state_t.c.exhausted_until,
                api_key_state_t.c.invalid,
                api_key_state_t.c.usage_today,
                api_key_state_t.c.usage_date,
            )
        )
        return {
            r.kid: {
                "exhausted_until": r.exhausted_until,
                "invalid": bool(r.invalid),
                "usage_today": int(r.usage_today) if r.usage_date == today else 0,
            }
            for r in rows
        }


@logged_db
async def add_bad_pair(kid: str, model: str) -> None:
    """Remember that this key cannot use this model."""
    stmt = dialect_insert(bad_pairs_t).values(kid=kid, model=model).on_conflict_do_nothing()
    async with engine.begin() as conn:
        await conn.execute(stmt)


@logged_db
async def load_bad_pairs() -> set[tuple[str, str]]:
    """Return all remembered (kid, model) pairs."""
    async with engine.connect() as conn:
        rows = await conn.execute(select(bad_pairs_t.c.kid, bad_pairs_t.c.model))
        return {(r.kid, r.model) for r in rows}


@logged_db
async def incr_usage(kid: str) -> None:
    """Atomically add 1 to today's usage; the counter restarts at 1 when the local date changes."""
    today = _today()
    stmt = dialect_insert(api_key_state_t).values(kid=kid, usage_today=1, usage_date=today)
    async with engine.begin() as conn:
        await conn.execute(
            stmt.on_conflict_do_update(
                index_elements=["kid"],
                set_={
                    "usage_today": case(
                        (
                            api_key_state_t.c.usage_date == today,
                            api_key_state_t.c.usage_today + 1,
                        ),
                        else_=1,
                    ),
                    "usage_date": today,
                },
            )
        )

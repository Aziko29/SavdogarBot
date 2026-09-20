"""Bot settings: single-row table with a RAM cache and validated updates."""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
from typing import Any

from sqlalchemy import select

from db.engine import dialect_insert, engine, logged_db
from db.models import REPOST_POLICIES, BotSettings, bot_settings_from_row
from db.schema import settings_t

logger = logging.getLogger("db.settings")

_MULTI_FIELDS = ("new_multi", "mid_multi", "old_multi")
MULTI_MIN = 1  # selection weight of a category: 1x ...
MULTI_MAX = 5  # ... to 5x
_KEEP_FIELDS = ("new_keep", "mid_keep")
_TIME_FIELDS = ("night_start", "night_end")
_ALLOWED = frozenset(
    (*_MULTI_FIELDS, *_KEEP_FIELDS, *_TIME_FIELDS, "interval_mins", "autopost_enabled", "repost_policy")
)
_MAX_KEEP = 100_000
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")

_cache: BotSettings | None = None
_lock = asyncio.Lock()


def _as_int(name: str, value: Any, lo: int, hi: int) -> int:
    """Validate an integer within [lo, hi]."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer, got {value!r}")
    if not lo <= value <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi}, got {value}")
    return value


def _as_hhmm(name: str, value: Any) -> str:
    """Validate a real HH:MM time and normalise it to zero-padded form."""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string like '23:00', got {value!r}")
    match = _TIME_RE.match(value.strip())
    if match is None:
        raise ValueError(f"{name} must be in HH:MM format, got {value!r}")
    hours, minutes = int(match.group(1)), int(match.group(2))
    if hours > 23 or minutes > 59:
        raise ValueError(f"{name} is not a real time of day: {value!r}")
    return f"{hours:02d}:{minutes:02d}"


def _validate(fields: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalise settings changes; raises ValueError on the first problem."""
    unknown = sorted(set(fields) - _ALLOWED)
    if unknown:
        raise ValueError(f"Unknown setting(s): {', '.join(unknown)}")
    clean: dict[str, Any] = {}
    for name, value in fields.items():
        if name in _MULTI_FIELDS:
            clean[name] = _as_int(name, value, MULTI_MIN, MULTI_MAX)
        elif name == "interval_mins":
            clean[name] = _as_int(name, value, 1, 1440)
        elif name in _KEEP_FIELDS:
            clean[name] = _as_int(name, value, 0, _MAX_KEEP)
        elif name in _TIME_FIELDS:
            clean[name] = _as_hhmm(name, value)
        elif name == "autopost_enabled":
            if not isinstance(value, bool):
                raise ValueError(f"autopost_enabled must be true or false, got {value!r}")
            clean[name] = value
        elif name == "repost_policy":
            if value not in REPOST_POLICIES:
                raise ValueError(
                    f"repost_policy must be one of {', '.join(REPOST_POLICIES)}, got {value!r}"
                )
            clean[name] = value
    return clean


async def _read() -> BotSettings:
    """Read the settings row straight from the database."""
    async with engine.connect() as conn:
        row = (await conn.execute(select(settings_t).where(settings_t.c.id == 1))).first()
    if row is None:
        raise RuntimeError("Settings row is missing; call init_db() first")
    return bot_settings_from_row(row)


@logged_db
async def get_settings() -> BotSettings:
    """Return the current settings (RAM-cached; callers get a private copy)."""
    global _cache
    cached = _cache
    if cached is not None:
        return dataclasses.replace(cached)
    async with _lock:
        if _cache is None:
            _cache = await _read()
        return dataclasses.replace(_cache)


@logged_db
async def update_settings(**fields: Any) -> BotSettings:
    """Validate and upsert settings row id=1, invalidate the cache and return the new settings."""
    global _cache
    clean = _validate(fields)
    async with _lock:
        _cache = None
        if clean:
            async with engine.begin() as conn:
                stmt = dialect_insert(settings_t).values(id=1, **clean)
                await conn.execute(stmt.on_conflict_do_update(index_elements=["id"], set_=clean))
            logger.info("Settings updated: %s", ", ".join(sorted(clean)))
        _cache = await _read()
        return dataclasses.replace(_cache)

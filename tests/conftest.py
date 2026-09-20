"""Pytest setup: test environment, import path, automatic asyncio marking and the in-memory DB fixture."""
from __future__ import annotations

import inspect
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# config.py validates the environment at import time, so this must run before any project import.
for _name in [n for n in os.environ if n.startswith(("GEMINI_API_KEYS_", "FALLBACK_"))]:
    os.environ.pop(_name, None)
os.environ.update(
    {
        "BOT_TOKEN": "123456:TEST_TOKEN",
        "BOSH_ADMIN_ID": "111",
        # Set TEST_DATABASE_URL=postgresql://user:pass@host/db to run the whole suite on PostgreSQL
        # (the tables of that database are dropped and recreated for every test).
        "DATABASE_URL": os.environ.get("TEST_DATABASE_URL", "").strip() or "sqlite+aiosqlite:///:memory:",
        "TZ": "Asia/Tashkent",
        "LOG_LEVEL": "WARNING",
        "GEMINI_API_KEYS": "test-key-aaaaaaaa1,test-key-bbbbbbbb2",
        "GEMINI_MODELS": "m1,m2",
        "FALLBACK_PROVIDERS": "",
        "MIN_REPOST_GAP_HOURS": "6",
        "NO_REPEAT_LAST_N": "3",
    }
)


def pytest_collection_modifyitems(items: list[Any]) -> None:
    """Mark every coroutine test as asyncio so no pytest.ini is needed."""
    for item in items:
        if inspect.iscoroutinefunction(getattr(item, "function", None)):
            item.add_marker(pytest.mark.asyncio)


@pytest_asyncio.fixture
async def db() -> AsyncIterator[None]:
    """Fresh in-memory SQLite schema for one test; the engine is disposed afterwards."""
    import db.admins as db_admins
    import db.chats as db_chats
    import db.settings as db_settings
    from db.engine import IS_POSTGRES, engine, init_db
    from db.schema import metadata

    async def wipe() -> None:
        if IS_POSTGRES:  # a real server keeps its tables between tests; SQLite :memory: does not
            async with engine.begin() as conn:
                await conn.run_sync(metadata.drop_all)

    db_settings._cache = None
    db_admins._extra = frozenset()
    db_chats._cache = ()
    db_chats._source_ids = frozenset()
    await wipe()
    await init_db()
    yield
    db_settings._cache = None
    db_admins._extra = frozenset()
    db_chats._cache = ()
    db_chats._source_ids = frozenset()
    await wipe()
    await engine.dispose()

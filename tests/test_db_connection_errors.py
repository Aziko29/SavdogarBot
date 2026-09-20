from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError, SQLAlchemyError

import db.engine as engine_mod


@pytest.fixture
def pg_like(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend every OSError is a (transient) connection error, as asyncpg's are on PostgreSQL."""
    monkeypatch.setattr(engine_mod, "_CONNECT_ERRORS", (OSError,))
    monkeypatch.setattr(engine_mod, "_TRANSIENT_CONNECT_ERRORS", (OSError,))

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(engine_mod.asyncio, "sleep", no_sleep)


async def test_logged_db_turns_a_connection_error_into_a_sqlalchemy_error(pg_like: None) -> None:
    @engine_mod.logged_db
    async def query() -> None:
        raise ConnectionRefusedError("refused")

    with pytest.raises(SQLAlchemyError) as info:
        await query()
    assert isinstance(info.value, OperationalError)


async def test_init_db_retries_a_transient_failure(pg_like: None, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def flaky() -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ConnectionRefusedError("still starting")

    monkeypatch.setattr(engine_mod, "_init_db_once", flaky)
    await engine_mod.init_db()
    assert calls == 3


async def test_init_db_gives_up_with_a_readable_error(pg_like: None, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def dead() -> None:
        nonlocal calls
        calls += 1
        raise ConnectionRefusedError("down")

    monkeypatch.setattr(engine_mod, "_init_db_once", dead)
    with pytest.raises(RuntimeError, match="Check DATABASE_URL"):
        await engine_mod.init_db()
    assert calls == engine_mod._START_ATTEMPTS


async def test_init_db_does_not_retry_a_permanent_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_mod, "_CONNECT_ERRORS", (ValueError,))
    monkeypatch.setattr(engine_mod, "_TRANSIENT_CONNECT_ERRORS", ())
    calls = 0

    async def wrong_password() -> None:
        nonlocal calls
        calls += 1
        raise ValueError("password authentication failed")

    monkeypatch.setattr(engine_mod, "_init_db_once", wrong_password)
    with pytest.raises(RuntimeError, match="password authentication failed"):
        await engine_mod.init_db()
    assert calls == 1

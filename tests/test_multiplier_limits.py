from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from db.settings import MULTI_MAX, MULTI_MIN, get_settings, update_settings
from handlers import admin


class _FakeCallback:
    """Just enough of a CallbackQuery for on_adjust."""

    def __init__(self) -> None:
        self.answers: list[tuple[str | None, bool]] = []
        self.message = SimpleNamespace()

    async def answer(self, text: str | None = None, show_alert: bool = False, **_: Any) -> None:
        self.answers.append((text, show_alert))


@pytest.fixture
def shown(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []

    async def fake_show(_callback: Any) -> None:
        calls.append(1)

    monkeypatch.setattr(admin, "_show_settings", fake_show)
    return calls


async def _press(field: str, sign: int) -> _FakeCallback:
    callback = _FakeCallback()
    await admin.on_adjust(callback, admin.SetCB(op="adj", field=field, sign=sign))
    return callback


def test_limits_are_one_to_five() -> None:
    assert (MULTI_MIN, MULTI_MAX) == (1, 5)


@pytest.mark.parametrize("field", ["new_multi", "mid_multi", "old_multi"])
@pytest.mark.parametrize("bad", [0, -1, 6, 100])
async def test_database_layer_rejects_weights_outside_1_to_5(db: None, field: str, bad: int) -> None:
    with pytest.raises(ValueError):
        await update_settings(**{field: bad})


@pytest.mark.parametrize("good", [1, 2, 5])
async def test_database_layer_accepts_weights_inside_1_to_5(db: None, good: int) -> None:
    saved = await update_settings(new_multi=good)
    assert saved.new_multi == good


async def test_plus_stops_at_five(db: None, shown: list[int]) -> None:
    await update_settings(new_multi=5)  # the default for "new"
    callback = await _press("new_multi", +1)
    assert (await get_settings()).new_multi == 5
    assert callback.answers and "5" in (callback.answers[0][0] or "")
    assert not shown  # nothing changed, so the panel is not redrawn


async def test_minus_stops_at_one(db: None, shown: list[int]) -> None:
    await update_settings(old_multi=1)  # the default for "old"
    await _press("old_multi", -1)
    assert (await get_settings()).old_multi == 1
    assert not shown


async def test_steps_move_by_one_inside_the_range(db: None, shown: list[int]) -> None:
    await update_settings(mid_multi=3)
    await _press("mid_multi", +1)
    assert (await get_settings()).mid_multi == 4
    await _press("mid_multi", -1)
    await _press("mid_multi", -1)
    assert (await get_settings()).mid_multi == 2
    assert len(shown) == 3


async def test_a_stored_value_above_five_is_pulled_back(db: None, shown: list[int]) -> None:
    """A value saved before the limit existed (for example 8) is corrected by the next press."""
    from sqlalchemy import update

    from db import settings as db_settings
    from db.engine import engine
    from db.schema import settings_t

    async with engine.begin() as conn:
        await conn.execute(update(settings_t).where(settings_t.c.id == 1).values(new_multi=8))
    db_settings._cache = None
    await _press("new_multi", -1)
    assert (await get_settings()).new_multi == 5
    await _press("new_multi", +1)  # already at the limit
    assert (await get_settings()).new_multi == 5


async def test_keep_counts_are_not_limited_by_the_weight_range(db: None, shown: list[int]) -> None:
    await update_settings(new_keep=10)
    await _press("new_keep", +1)
    assert (await get_settings()).new_keep == 15

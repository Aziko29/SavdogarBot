"""Admin panel: only the head admin can open or change the settings; regular admins can't, even by hand-made taps."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User

from config import settings
from db import admins
from db.settings import get_settings
from handlers import admin

HEAD = settings.head_admin_id
REGULAR = 555


@pytest.fixture
def bot(monkeypatch: pytest.MonkeyPatch) -> Bot:
    """A Bot whose API calls are recorded instead of sent (`bot.calls` holds the method objects)."""
    fake = Bot("123456:TEST_TOKEN")
    calls: list[Any] = []

    async def record(self: Bot, method: Any, request_timeout: Any = None) -> bool:
        calls.append(method)
        return True

    monkeypatch.setattr(Bot, "__call__", record)
    fake.calls = calls  # type: ignore[attr-defined]
    return fake


def _callback(bot: Bot, user_id: int, data: str) -> CallbackQuery:
    user = User(id=user_id, is_bot=False, first_name=f"U{user_id}")
    return CallbackQuery(id="1", from_user=user, chat_instance="ci", data=data).as_(bot)


async def _tap(bot: Bot, user_id: int, data: str) -> Any:
    cb = _callback(bot, user_id, data)
    return await admin.router.propagate_event("callback_query", cb, bot=bot)


def _alerts(bot: Bot) -> list[tuple[str | None, bool]]:
    return [(getattr(m, "text", None), bool(getattr(m, "show_alert", False))) for m in bot.calls]  # type: ignore[attr-defined]


def _buttons(markup: Any) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


# ------------------------------------------------ the menu


async def test_only_the_head_admin_sees_the_settings_button(db: None) -> None:
    await admins.add_admin(REGULAR, added_by=HEAD)
    settings_cb = admin.MenuCB(action="settings").pack()
    assert settings_cb in _buttons(admin._root_markup(HEAD))
    regular = _buttons(admin._root_markup(REGULAR))
    assert settings_cb not in regular
    # everything else a regular admin used is still there
    assert admin.MenuCB(action="status").pack() in regular
    assert admin.MenuCB(action="postnow").pack() in regular
    assert admin.PRODUCTS_CALLBACK in regular and admin.ORDERS_CALLBACK in regular


# ------------------------------------------------ taps


@pytest.mark.parametrize(
    "data",
    [
        admin.MenuCB(action="settings").pack(),
        admin.SetCB(op="auto").pack(),
        admin.SetCB(op="policy").pack(),
        admin.SetCB(op="adj", field="new_multi", sign=1).pack(),
        admin.SetCB(op="interval", value=30).pack(),
        admin.SetCB(op="interval_custom").pack(),
        admin.SetCB(op="night_start").pack(),
        admin.SetCB(op="noop").pack(),
        admin.SetCB(op="cancel").pack(),
    ],
)
async def test_a_regular_admin_is_refused_on_every_settings_button(db: None, bot: Bot, data: str) -> None:
    await admins.add_admin(REGULAR, added_by=HEAD)
    before = await get_settings()
    await _tap(bot, REGULAR, data)
    assert _alerts(bot) == [(admin._HEAD_ONLY_TEXT, True)]
    assert await get_settings() == before  # nothing changed


async def test_the_head_admin_can_still_change_settings(db: None, bot: Bot, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_show(_cb: Any) -> None:
        return None

    monkeypatch.setattr(admin, "_show_settings", fake_show)
    before = await get_settings()
    await _tap(bot, HEAD, admin.SetCB(op="auto").pack())
    after = await get_settings()
    assert after.autopost_enabled is (not before.autopost_enabled)
    assert (admin._HEAD_ONLY_TEXT, True) not in _alerts(bot)


async def test_a_regular_admin_can_still_use_the_rest_of_the_panel(db: None, bot: Bot, monkeypatch: pytest.MonkeyPatch) -> None:
    await admins.add_admin(REGULAR, added_by=HEAD)
    edits: list[Any] = []

    async def fake_edit(_cb: Any, text: str, markup: Any) -> None:
        edits.append(markup)

    monkeypatch.setattr(admin, "_edit", fake_edit)
    cb = _callback(bot, REGULAR, admin.MenuCB(action="root").pack())
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=bot.id, chat_id=REGULAR, user_id=REGULAR))
    await admin.router.propagate_event("callback_query", cb, bot=bot, state=state)
    assert len(edits) == 1  # the menu was redrawn ...
    assert admin.MenuCB(action="settings").pack() not in _buttons(edits[0])  # ... without the settings button
    assert (admin._HEAD_ONLY_TEXT, True) not in _alerts(bot)


# ------------------------------------------------ typed input


async def test_a_regular_admin_stuck_in_an_input_state_cannot_edit_settings(db: None, bot: Bot) -> None:
    await admins.add_admin(REGULAR, added_by=HEAD)
    storage = MemoryStorage()
    key = StorageKey(bot_id=bot.id, chat_id=REGULAR, user_id=REGULAR)
    state = FSMContext(storage=storage, key=key)
    await state.set_state(admin.AdminInput.interval)
    before = await get_settings()

    user = User(id=REGULAR, is_bot=False, first_name="U")
    message = Message(
        message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=REGULAR, type="private"), from_user=user, text="7"
    ).as_(bot)
    await admin.router.propagate_event(
        "message", message, bot=bot, state=state, raw_state=admin.AdminInput.interval.state
    )

    assert await get_settings() == before  # the "7" was NOT applied as an interval
    assert await state.get_state() is None  # and the state was dropped
    assert [getattr(m, "text", None) for m in bot.calls] == [admin._HEAD_ONLY_TEXT]  # type: ignore[attr-defined]

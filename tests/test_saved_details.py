"""Repeat customers: the order form remembers phone + address and offers them with one tap."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

from db import customers, orders, products
from handlers import client

USER = 4242
PHONE = "+998901234567"
ADDRESS = "Chilonzor 9, 12-uy"


def _state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=USER, user_id=USER))


def _message() -> MagicMock:
    """The bot's own prompt (what callback.message is): its chat id is the customer's id."""
    msg = MagicMock(spec=Message)
    msg.chat = SimpleNamespace(id=USER)
    msg.answer = AsyncMock()
    msg.edit_reply_markup = AsyncMock()
    msg.edit_text = AsyncMock()
    return msg


def _callback(message: MagicMock) -> SimpleNamespace:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=USER, username="ali", full_name="Ali V"), message=message, answer=AsyncMock()
    )


async def _add_product() -> int:
    pid = await products.create_product_pending(-1001, 1, "file1", "uniq1", "Kurtka 150000 som")
    assert pid is not None
    await products.update_fields(pid, name="Kurtka", price="150000 so'm")
    return pid


async def _start_form(state: FSMContext, pid: int) -> None:
    await state.set_state(client.OrderFlow.waiting_quantity)
    await state.set_data({"product_id": pid, "expires_at": 9e12})


def _buttons(markup: Any) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


# ------------------------------------------------ storage


async def test_details_are_saved_updated_kept_and_deleted(db: None) -> None:
    assert await customers.get_saved_details(USER) is None
    assert await customers.save_details(USER, PHONE, ADDRESS) is True  # new
    assert await customers.save_details(USER, PHONE, ADDRESS) is False  # nothing new: no write, no notice
    saved = await customers.get_saved_details(USER)
    assert saved is not None and (saved.phone, saved.address, saved.usable) == (PHONE, ADDRESS, True)

    assert await customers.save_details(USER, "+998907654321", "Yunusobod 1") is True  # changed
    assert await customers.save_details(USER, "+998911111111", None) is True  # None keeps the address
    saved = await customers.get_saved_details(USER)
    assert saved is not None and (saved.phone, saved.address) == ("+998911111111", "Yunusobod 1")

    assert await customers.delete_saved_details(USER) is True
    assert await customers.delete_saved_details(USER) is False
    assert await customers.get_saved_details(USER) is None


async def test_details_without_an_address_are_not_usable(db: None) -> None:
    await customers.save_details(USER, PHONE, None)  # e.g. a first order that was a pickup
    saved = await customers.get_saved_details(USER)
    assert saved is not None and saved.usable is False


# ------------------------------------------------ the order form


async def test_first_time_customer_goes_straight_to_the_phone_step(db: None) -> None:
    state, msg = _state(), _message()
    await client._accept_quantity(msg, state, 2)
    assert await state.get_state() == client.OrderFlow.waiting_phone.state
    assert msg.answer.call_args.args[0] == client._ASK_PHONE_TEXT
    assert (await state.get_data())["quantity"] == 2


async def test_repeat_customer_is_offered_the_saved_details(db: None) -> None:
    await customers.save_details(USER, PHONE, ADDRESS)
    state, msg = _state(), _message()
    await client._accept_quantity(msg, state, 3)  # msg is the bot's prompt: the id comes from the chat
    assert await state.get_state() == client.OrderFlow.waiting_saved.state
    text = msg.answer.call_args.args[0]
    assert PHONE in text and ADDRESS in text
    assert _buttons(msg.answer.call_args.kwargs["reply_markup"]) == [
        client.SavedDetailsCB(action="use").pack(),
        client.SavedDetailsCB(action="edit").pack(),
    ]


async def test_using_the_saved_details_skips_phone_and_address(db: None) -> None:
    await customers.save_details(USER, PHONE, ADDRESS)
    state = _state()
    await _start_form(state, 1)
    await client._accept_quantity(_message(), state, 1)

    msg = _message()
    cb = _callback(msg)
    await client.on_saved_details(cb, client.SavedDetailsCB(action="use"), state)  # type: ignore[arg-type]
    assert await state.get_state() == client.OrderFlow.waiting_note.state  # straight to the optional note
    data = await state.get_data()
    assert (data["phone"], data["address"]) == (PHONE, ADDRESS)
    assert msg.answer.call_args.args[0] == client._ASK_NOTE_TEXT
    msg.edit_reply_markup.assert_awaited_once_with(reply_markup=None)


async def test_choosing_to_change_runs_the_normal_steps(db: None) -> None:
    await customers.save_details(USER, PHONE, ADDRESS)
    state = _state()
    await _start_form(state, 1)
    await client._accept_quantity(_message(), state, 1)

    msg = _message()
    await client.on_saved_details(_callback(msg), client.SavedDetailsCB(action="edit"), state)  # type: ignore[arg-type]
    assert await state.get_state() == client.OrderFlow.waiting_phone.state
    assert msg.answer.call_args.args[0] == client._ASK_PHONE_TEXT
    assert "phone" not in await state.get_data()


async def test_an_expired_form_refuses_the_saved_details_tap(db: None) -> None:
    state = _state()
    await state.set_state(client.OrderFlow.waiting_saved)
    await state.set_data({"product_id": 1, "expires_at": 1.0, "saved_phone": PHONE, "saved_address": ADDRESS})
    cb = _callback(_message())
    await client.on_saved_details(cb, client.SavedDetailsCB(action="use"), state)  # type: ignore[arg-type]
    assert cb.answer.call_args.args[0] == client._EXPIRED_TEXT
    assert await state.get_state() is None


async def test_typing_at_the_saved_question_points_back_at_the_buttons(db: None) -> None:
    msg = _message()
    await client.on_saved_details_text(msg)
    assert msg.answer.call_args.args[0] == client._USE_THE_BUTTONS_TEXT


def test_the_saved_question_is_part_of_the_form_states() -> None:
    # /cancel and the stale-input guard are keyed on all OrderFlow states, so the new one is covered.
    assert client.OrderFlow.waiting_saved in client.OrderFlow.__all_states__


# ------------------------------------------------ remembering after a confirmed order


async def _confirm(state: FSMContext, pid: int, phone: str, address: str, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    monkeypatch.setattr(client, "_notify_admins", AsyncMock())
    monkeypatch.setattr(client, "_forward_media_to_admins", AsyncMock())
    await state.set_state(client.OrderFlow.confirming)
    await state.set_data(
        {"product_id": pid, "expires_at": 9e12, "quantity": 1, "phone": phone, "address": address, "note": ""}
    )
    msg = _message()
    await client.on_confirm(_callback(msg), client.ConfirmCB(action="yes"), state, MagicMock())  # type: ignore[arg-type]
    return msg


async def test_a_confirmed_order_saves_the_details_and_says_so_once(db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    pid = await _add_product()
    first = await _confirm(_state(), pid, PHONE, ADDRESS, monkeypatch)
    said = [c.args[0] for c in first.answer.call_args_list]
    assert client._DETAILS_REMEMBERED_TEXT in said
    saved = await customers.get_saved_details(USER)
    assert saved is not None and (saved.phone, saved.address) == (PHONE, ADDRESS)

    await orders.decide_order((await orders.list_user_orders(USER, 1))[0].order_id, "rejected")  # allow another order
    second = await _confirm(_state(), pid, PHONE, ADDRESS, monkeypatch)  # same details: no repeated notice
    assert client._DETAILS_REMEMBERED_TEXT not in [c.args[0] for c in second.answer.call_args_list]


async def test_a_pickup_order_keeps_the_saved_delivery_address(db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    await customers.save_details(USER, PHONE, ADDRESS)
    pid = await _add_product()
    await _confirm(_state(), pid, "+998907654321", client._PICKUP_ADDRESS, monkeypatch)
    saved = await customers.get_saved_details(USER)
    assert saved is not None and (saved.phone, saved.address) == ("+998907654321", ADDRESS)


async def test_a_storage_failure_never_breaks_an_order(db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*_: Any, **__: Any) -> bool:
        raise RuntimeError("disk full")

    monkeypatch.setattr(client, "save_details", boom)
    pid = await _add_product()
    msg = await _confirm(_state(), pid, PHONE, ADDRESS, monkeypatch)
    assert any("Buyurtmangiz" in c.args[0] for c in msg.answer.call_args_list)  # the order went through
    assert len(await orders.list_user_orders(USER, 5)) == 1


# ------------------------------------------------ /malumotlarim


async def test_my_data_screen_shows_and_deletes(db: None) -> None:
    user = SimpleNamespace(id=USER)
    empty = SimpleNamespace(from_user=user, answer=AsyncMock())
    await client.cmd_my_data(empty)  # type: ignore[arg-type]
    assert empty.answer.call_args.args[0] == client._NO_SAVED_DATA_TEXT

    await customers.save_details(USER, PHONE, ADDRESS)
    shown = SimpleNamespace(from_user=user, answer=AsyncMock())
    await client.cmd_my_data(shown)  # type: ignore[arg-type]
    assert PHONE in shown.answer.call_args.args[0] and ADDRESS in shown.answer.call_args.args[0]
    assert _buttons(shown.answer.call_args.kwargs["reply_markup"]) == [client.MyDataCB(action="delete").pack()]

    msg = _message()
    await client.on_my_data_delete(_callback(msg))  # type: ignore[arg-type]
    assert await customers.get_saved_details(USER) is None
    msg.edit_text.assert_awaited_once_with(client._SAVED_DELETED_TEXT)


async def test_saved_details_are_html_escaped(db: None) -> None:
    await customers.save_details(USER, PHONE, "<b>x</b> & y")
    state, msg = _state(), _message()
    await client._accept_quantity(msg, state, 1)
    assert "&lt;b&gt;x&lt;/b&gt; &amp; y" in msg.answer.call_args.args[0]

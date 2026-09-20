"""Order module: details, lifecycle (accept -> done/cancelled, customer withdrawal), reminders, migration, cards."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text, update

import order_reminder
from config import ConfigError, load_settings
from db import orders, products
from db.engine import IS_POSTGRES, engine
from db.schema import orders_t
from handlers import client
from utils import utcnow


async def _add_product(n: int = 1) -> int:
    """Insert a product with a unique source message and photo."""
    pid = await products.create_product_pending(-1001, n, f"file{n}", f"uniq{n}", "Kurtka 150000 som")
    assert pid is not None
    return pid


async def _add_order(pid: int, user_id: int = 555, **details: Any) -> int:
    """Create a pending order for `user_id`."""
    return await orders.create_order(pid, user_id, "buyer", "Buyer B", "izoh", **details)


async def _backdate(order_id: int, *, created_min: int | None = None, reminded_min: int | None = None) -> None:
    """Move an order's creation / last-reminder time into the past."""
    values: dict[str, Any] = {}
    if created_min is not None:
        values["created_at"] = utcnow() - timedelta(minutes=created_min)
    if reminded_min is not None:
        values["reminded_at"] = utcnow() - timedelta(minutes=reminded_min)
    async with engine.begin() as conn:
        await conn.execute(update(orders_t).where(orders_t.c.order_id == order_id).values(**values))


# ------------------------------------------------ details and lifecycle


async def test_order_keeps_quantity_phone_and_address(db: None) -> None:
    pid = await _add_product()
    oid = await _add_order(pid, quantity=3, phone="+998901234567", address="Chilonzor 5")
    order = await orders.get_order(oid)
    assert order is not None
    assert (order.quantity, order.phone, order.address, order.outcome) == (3, "+998901234567", "Chilonzor 5", "")
    assert (order.remind_count, order.reminded_at) == (0, None)


async def test_order_details_default_for_plain_orders(db: None) -> None:
    pid = await _add_product()
    order = await orders.get_order(await _add_order(pid))
    assert order is not None
    assert (order.quantity, order.phone, order.address) == (1, "", "")


async def test_quantity_must_be_positive(db: None) -> None:
    pid = await _add_product()
    with pytest.raises(ValueError):
        await _add_order(pid, quantity=0)


async def test_finish_order_only_after_acceptance_and_only_once(db: None) -> None:
    pid = await _add_product()
    oid = await _add_order(pid)
    assert await orders.finish_order(oid, "completed") is False  # still pending

    assert await orders.decide_order(oid, "accepted") is True
    await orders.set_chat_open(oid, True)
    assert await orders.finish_order(oid, "completed") is True
    assert await orders.finish_order(oid, "cancelled") is False  # already closed
    order = await orders.get_order(oid)
    assert order is not None
    assert (order.status, order.outcome, order.chat_open) == ("accepted", "completed", False)
    assert await orders.get_open_order_for_user(555) is None

    with pytest.raises(ValueError):
        await orders.finish_order(oid, "pending")
    with pytest.raises(ValueError):
        await orders.finish_order(oid, "")


async def test_rejected_order_cannot_be_finished(db: None) -> None:
    pid = await _add_product()
    oid = await _add_order(pid)
    await orders.decide_order(oid, "rejected")
    assert await orders.finish_order(oid, "completed") is False


async def test_customer_can_withdraw_only_his_own_pending_order(db: None) -> None:
    pid = await _add_product()
    oid = await _add_order(pid, user_id=555)
    assert await orders.cancel_pending_by_user(oid, 999) is False  # somebody else's order
    assert await orders.cancel_pending_by_user(oid, 555) is True
    assert await orders.cancel_pending_by_user(oid, 555) is False
    order = await orders.get_order(oid)
    assert order is not None and (order.status, order.outcome) == ("rejected", "cancelled")
    assert await orders.decide_order(oid, "accepted") is False  # a withdrawn order cannot be accepted anymore
    assert await orders.list_pending() == []


async def test_accepted_order_cannot_be_withdrawn_by_the_customer(db: None) -> None:
    pid = await _add_product()
    oid = await _add_order(pid)
    await orders.decide_order(oid, "accepted")
    assert await orders.cancel_pending_by_user(oid, 555) is False


async def test_user_and_in_progress_lists(db: None) -> None:
    pid = await _add_product()
    first = await _add_order(pid, user_id=555)
    second = await _add_order(pid, user_id=555)
    other = await _add_order(pid, user_id=777)
    assert [o.order_id for o in await orders.list_user_orders(555)] == [second, first]
    assert [o.order_id for o in await orders.list_user_orders(555, limit=1)] == [second]

    await orders.decide_order(first, "accepted")
    await orders.decide_order(other, "accepted")
    assert [o.order_id for o in await orders.list_in_progress()] == [first, other]
    await orders.finish_order(first, "completed")
    assert [o.order_id for o in await orders.list_in_progress()] == [other]


# ------------------------------------------------ reminders


async def test_orders_become_due_after_the_wait_and_respect_the_gap_and_the_maximum(db: None) -> None:
    pid = await _add_product()
    oid = await _add_order(pid)

    async def due() -> list[int]:
        return [o.order_id for o in await orders.list_due_for_reminder(15, 15, 2)]

    assert await due() == []  # brand new
    await _backdate(oid, created_min=20)
    assert await due() == [oid]

    await orders.mark_reminded(oid)
    assert await due() == []  # just reminded
    await _backdate(oid, reminded_min=16)
    assert await due() == [oid]

    await orders.mark_reminded(oid)  # second (= maximum) reminder
    await _backdate(oid, reminded_min=60)
    assert await due() == []
    order = await orders.get_order(oid)
    assert order is not None and order.remind_count == 2


async def test_decided_orders_are_never_due(db: None) -> None:
    pid = await _add_product()
    oid = await _add_order(pid)
    await _backdate(oid, created_min=60)
    await orders.decide_order(oid, "accepted")
    assert await orders.list_due_for_reminder(15, 15, 3) == []


class _FakeBot:
    """Records send_message calls."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str, dict[str, Any]]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> None:
        self.sent.append((chat_id, text, kwargs))


@pytest.fixture
def reminder_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two admins (111, 222) and reminders after 15 min, every 15 min, at most 2 times."""
    monkeypatch.setattr(
        order_reminder,
        "settings",
        SimpleNamespace(order_remind_after_min=15, order_remind_every_min=15, order_remind_max=2),
    )
    monkeypatch.setattr(order_reminder, "admin_ids", lambda: [111, 222])


async def test_reminder_goes_to_every_admin_as_a_reply_to_the_card(db: None, reminder_env: None) -> None:
    pid = await _add_product()
    oid = await _add_order(pid, quantity=2)
    await orders.set_admin_msgs(oid, {111: 900})
    await _backdate(oid, created_min=20)

    bot = _FakeBot()
    assert await order_reminder.remind_pending_orders(bot) == 1  # type: ignore[arg-type]
    assert [chat for chat, _, _ in bot.sent] == [111, 222]
    (_, body, first_kwargs), (_, _, second_kwargs) = bot.sent
    assert f"#{oid}" in body and "1/2" in body and "\u00d7 2" in body
    assert first_kwargs["reply_parameters"].message_id == 900  # admin 111 has the card
    assert second_kwargs["reply_parameters"] is None  # admin 222 never got a card

    order = await orders.get_order(oid)
    assert order is not None and order.remind_count == 1


async def test_reminders_stop_after_the_maximum_and_never_repeat_too_soon(db: None, reminder_env: None) -> None:
    pid = await _add_product()
    oid = await _add_order(pid)
    await _backdate(oid, created_min=20)
    bot = _FakeBot()

    assert await order_reminder.remind_pending_orders(bot) == 1  # type: ignore[arg-type]
    assert await order_reminder.remind_pending_orders(bot) == 0  # type: ignore[arg-type]  # too soon
    await _backdate(oid, reminded_min=16)
    assert await order_reminder.remind_pending_orders(bot) == 1  # type: ignore[arg-type]
    await _backdate(oid, reminded_min=16)
    assert await order_reminder.remind_pending_orders(bot) == 0  # type: ignore[arg-type]  # maximum reached
    assert len(bot.sent) == 4  # 2 reminders x 2 admins


async def test_no_reminder_for_a_decided_order(db: None, reminder_env: None) -> None:
    pid = await _add_product()
    oid = await _add_order(pid)
    await _backdate(oid, created_min=30)
    await orders.decide_order(oid, "rejected")
    bot = _FakeBot()
    assert await order_reminder.remind_pending_orders(bot) == 0  # type: ignore[arg-type]
    assert bot.sent == []


async def test_reminders_can_be_switched_off(db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        order_reminder,
        "settings",
        SimpleNamespace(order_remind_after_min=15, order_remind_every_min=15, order_remind_max=0),
    )
    pid = await _add_product()
    oid = await _add_order(pid)
    await _backdate(oid, created_min=60)
    bot = _FakeBot()
    assert await order_reminder.remind_pending_orders(bot) == 0  # type: ignore[arg-type]
    assert bot.sent == []


def test_reminder_settings_defaults_and_ranges() -> None:
    env = {"BOT_TOKEN": "123456:TEST_TOKEN", "BOSH_ADMIN_ID": "111", "GEMINI_API_KEYS": "k1", "GEMINI_MODELS": "m1"}
    s = load_settings(env)
    assert (s.order_remind_after_min, s.order_remind_every_min, s.order_remind_max) == (15, 15, 3)
    custom = load_settings({**env, "ORDER_REMIND_AFTER_MIN": "30", "ORDER_REMIND_EVERY_MIN": "10", "ORDER_REMIND_MAX": "0"})
    assert (custom.order_remind_after_min, custom.order_remind_every_min, custom.order_remind_max) == (30, 10, 0)
    for name, bad in (("ORDER_REMIND_AFTER_MIN", "0"), ("ORDER_REMIND_EVERY_MIN", "1441"), ("ORDER_REMIND_MAX", "21")):
        with pytest.raises(ConfigError):
            load_settings({**env, name: bad})


# ------------------------------------------------ migration


@pytest.mark.skipif(IS_POSTGRES, reason="raw SQLite DDL")
async def test_migration_adds_the_order_columns_to_an_old_table_and_is_repeatable(db: None) -> None:
    from db.engine import _add_order_details

    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE chat_relay"))
        await conn.execute(text("DROP TABLE orders"))
        await conn.execute(
            text(
                "CREATE TABLE orders (order_id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL, "
                "user_id BIGINT NOT NULL, username TEXT, user_fullname TEXT NOT NULL DEFAULT '', "
                "comment TEXT NOT NULL DEFAULT '', status VARCHAR(16) NOT NULL DEFAULT 'pending', "
                "admin_msg_ids TEXT NOT NULL DEFAULT '{}', chat_open BOOLEAN NOT NULL DEFAULT 0, "
                "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO orders (product_id, user_id, created_at, updated_at) "
                "VALUES (1, 5, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
        )
        await conn.run_sync(_add_order_details)
        await conn.run_sync(_add_order_details)  # running it again must not fail
        row = (
            await conn.execute(text("SELECT quantity, phone, address, outcome, remind_count, reminded_at FROM orders"))
        ).one()
    assert tuple(row) == (1, "", "", "", 0, None)


# ------------------------------------------------ admin card, buttons, customer texts


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+998 90 123-45-67", "+998901234567"),
        ("(90) 123 45 67", "+998901234567"),
        ("901234567", "+998901234567"),
        ("998901234567", "+998901234567"),
        ("+1 (555) 123-4567", "+15551234567"),
        ("12345", None),
        ("abc", None),
        ("+998 90 xx", None),
        ("1" * 16, None),
    ],
)
def test_phone_normalization(raw: str, expected: str | None) -> None:
    assert client._normalize_phone(raw) == expected


def _button_data(markup: Any) -> list[str]:
    """Flatten the callback data of every button of an inline keyboard."""
    return [b.callback_data for row in markup.inline_keyboard for b in row]


async def test_admin_card_shows_the_order_details_and_state(db: None) -> None:
    pid = await _add_product()
    oid = await _add_order(pid, quantity=2, phone="+998901234567", address="Chilonzor <5>")
    product = await products.get_product(pid)
    order = await orders.get_order(oid)
    assert product is not None and order is not None

    card = client._build_admin_text(order, product)
    assert "Soni: <b>2</b>" in card
    assert "+998901234567" in card
    assert "Chilonzor &lt;5&gt;" in card  # customer text is escaped
    assert "Qabul qilindi" not in card and "Rad etildi" not in card

    await orders.decide_order(oid, "accepted")
    accepted = await orders.get_order(oid)
    assert accepted is not None
    assert "Qabul qilindi" in client._build_admin_text(accepted, product)
    await orders.finish_order(oid, "completed")
    done = await orders.get_order(oid)
    assert done is not None
    assert "Bajarildi" in client._build_admin_text(done, product)


async def test_buttons_follow_the_order_state(db: None) -> None:
    pid = await _add_product()
    product = await products.get_product(pid)
    oid = await _add_order(pid)

    pending = await orders.get_order(oid)
    assert pending is not None
    assert [d.split(":")[0] for d in _button_data(client.order_markup(pending, product))] == ["odec", "odec"]

    await orders.decide_order(oid, "accepted")
    await orders.set_chat_open(oid, True)
    accepted = await orders.get_order(oid)
    assert accepted is not None
    assert [d.split(":")[0] for d in _button_data(client.order_markup(accepted, product))] == [
        "ofin",
        "ofin",
        "chatcl",
        "osold",
    ]

    await orders.close_chat_if_open(oid)
    chat_closed = await orders.get_order(oid)
    assert chat_closed is not None
    assert "chatcl" not in [d.split(":")[0] for d in _button_data(client.order_markup(chat_closed, product))]

    await orders.finish_order(oid, "completed")
    finished = await orders.get_order(oid)
    assert finished is not None
    assert client.order_markup(finished, product) is None


async def test_mark_sold_button_is_dropped_for_a_sold_product(db: None) -> None:
    pid = await _add_product()
    oid = await _add_order(pid)
    await orders.decide_order(oid, "accepted")
    await products.set_status(pid, "sold")
    product = await products.get_product(pid)
    order = await orders.get_order(oid)
    assert product is not None and order is not None
    assert "osold" not in [d.split(":")[0] for d in _button_data(client.order_markup(order, product))]


async def test_without_mark_sold_keeps_the_other_buttons(db: None) -> None:
    pid = await _add_product()
    oid = await _add_order(pid)
    await orders.decide_order(oid, "accepted")
    order = await orders.get_order(oid)
    assert order is not None
    markup = client.order_markup(order, None)
    trimmed = client._without_mark_sold(markup)
    assert trimmed is not None
    assert [d.split(":")[0] for d in _button_data(trimmed)] == ["ofin", "ofin"]


def test_customer_status_texts() -> None:
    def order(status: str, outcome: str = "") -> Any:
        return SimpleNamespace(status=status, outcome=outcome)

    assert "ko'rib chiqilmoqda" in client._user_status_text(order("pending"))
    assert "qabul qilindi" in client._user_status_text(order("accepted"))
    assert "bajarildi" in client._user_status_text(order("accepted", "completed"))
    assert "sotuvchi bekor qildi" in client._user_status_text(order("accepted", "cancelled"))
    assert "siz bekor qildingiz" in client._user_status_text(order("rejected", "cancelled"))
    assert "qolmagan" in client._user_status_text(order("rejected"))

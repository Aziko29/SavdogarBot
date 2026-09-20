"""Inquiry chat (customer <-> admin without an order), orders menu counts and finished-order clean-up."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from db import inquiries, orders, products
from db.engine import engine
from db.schema import chat_relay_t, inquiries_t, orders_t
from handlers import client
from utils import utcnow


async def _add_product(n: int = 1) -> int:
    pid = await products.create_product_pending(-1001, n, f"file{n}", f"uniq{n}", "Kurtka 150000 som")
    assert pid is not None
    return pid


async def _add_order(pid: int, user_id: int = 555, **details: Any) -> int:
    return await orders.create_order(pid, user_id, "buyer", "Buyer B", "izoh", **details)


async def _backdate_order(order_id: int, days: int) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            update(orders_t).where(orders_t.c.order_id == order_id).values(updated_at=utcnow() - timedelta(days=days))
        )


# ------------------------------------------------ product choice buttons


def test_product_buttons_offer_order_and_contact() -> None:
    markup = client._product_actions_markup(42)
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert len(buttons) == 2
    parsed = [client.ProductActionCB.unpack(b.callback_data) for b in buttons]
    assert [(p.action, p.product_id) for p in parsed] == [("order", 42), ("ask", 42)]


# ------------------------------------------------ inquiries


async def test_inquiry_open_relay_close(db: None) -> None:
    assert await inquiries.get_open_inquiry(7) is None
    await inquiries.open_inquiry(7, 3, "Ali")
    found = await inquiries.get_open_inquiry(7)
    assert found is not None and (found.user_id, found.product_id, found.user_fullname) == (7, 3, "Ali")

    await inquiries.record_inquiry_relay(7, 111, 900)
    await inquiries.record_inquiry_relay(7, 111, 900)  # duplicate is ignored
    assert await inquiries.find_inquiry_user_by_relay(111, 900) == 7
    assert await inquiries.find_inquiry_user_by_relay(111, 901) is None

    assert await inquiries.touch_inquiry(7) is True
    assert await inquiries.close_inquiry_if_open(7) is True
    assert await inquiries.close_inquiry_if_open(7) is False  # only the first close counts
    assert await inquiries.get_open_inquiry(7) is None
    assert await inquiries.touch_inquiry(7) is False


async def test_inquiry_reopens_and_updates_product(db: None) -> None:
    await inquiries.open_inquiry(7, 3, "Ali")
    await inquiries.close_inquiry_if_open(7)
    await inquiries.open_inquiry(7, 9, "Ali V")
    found = await inquiries.get_open_inquiry(7)
    assert found is not None and (found.product_id, found.user_fullname) == (9, "Ali V")


async def test_inquiry_expires_after_inactivity(db: None) -> None:
    await inquiries.open_inquiry(7, 3, "Ali")
    async with engine.begin() as conn:
        await conn.execute(
            update(inquiries_t)
            .where(inquiries_t.c.user_id == 7)
            .values(updated_at=utcnow() - timedelta(hours=inquiries.INQUIRY_TTL_HOURS + 1))
        )
    assert await inquiries.get_open_inquiry(7) is None
    assert await inquiries.touch_inquiry(7) is True  # still open in the DB: an admin reply revives it
    assert await inquiries.get_open_inquiry(7) is not None


# ------------------------------------------------ orders menu and clean-up


async def _one_of_each() -> dict[str, int]:
    """One pending, one in-progress, one completed, one rejected and one accepted+cancelled order."""
    pid = await _add_product()
    ids = {name: await _add_order(pid, user_id=500 + i) for i, name in enumerate(
        ["pending", "progress", "completed", "rejected", "cancelled"]
    )}
    for name in ("progress", "completed", "cancelled"):
        assert await orders.decide_order(ids[name], "accepted")
    assert await orders.decide_order(ids["rejected"], "rejected")
    assert await orders.finish_order(ids["completed"], "completed")
    assert await orders.finish_order(ids["cancelled"], "cancelled")
    return ids


async def test_order_group_counts_and_finished_list(db: None) -> None:
    ids = await _one_of_each()
    assert await orders.count_order_groups() == (1, 1, 3)
    finished = await orders.list_finished(10)
    assert {o.order_id for o in finished} == {ids["completed"], ids["rejected"], ids["cancelled"]}
    assert await orders.count_finished() == 3


async def test_purge_removes_only_finished_orders_and_reports_messages(db: None) -> None:
    ids = await _one_of_each()
    await orders.set_admin_msgs(ids["completed"], {111: 10, 222: 11})
    await orders.set_admin_msgs(ids["progress"], {111: 20})
    await orders.record_relay_message(ids["completed"], 111, 12)  # a relayed customer message
    await orders.record_relay_message(ids["progress"], 111, 21)

    deleted, refs = await orders.purge_finished_orders()
    assert deleted == 3
    assert refs == [(111, 10), (111, 12), (222, 11)]  # nothing of the in-progress order

    assert await orders.count_order_groups() == (1, 1, 0)
    assert await orders.get_order(ids["completed"]) is None
    assert await orders.get_order(ids["progress"]) is not None
    async with engine.connect() as conn:
        remaining = (await conn.execute(select(chat_relay_t.c.message_id))).scalars().all()
    assert sorted(remaining) == [21]


async def test_purge_older_than_days_keeps_recent_ones(db: None) -> None:
    ids = await _one_of_each()
    await _backdate_order(ids["completed"], days=10)
    assert await orders.count_finished(7) == 1
    deleted, _ = await orders.purge_finished_orders(7)
    assert deleted == 1
    assert await orders.get_order(ids["completed"]) is None
    assert await orders.get_order(ids["rejected"]) is not None


async def test_purge_rejects_negative_days(db: None) -> None:
    with pytest.raises(ValueError):
        await orders.purge_finished_orders(-1)

"""Smoke test for the /katalog DB helpers: count_active_by_category, list_catalog_products."""
from __future__ import annotations

import pytest

from db.engine import engine
from db.products import (
    count_active_by_category,
    create_product_pending,
    list_catalog_products,
    set_category,
    set_status,
)
from db.schema import products_t
from sqlalchemy import update


async def _make_product(i: int) -> int:
    pid = await create_product_pending(1, i, f"file{i}", f"uniq{i}", f"text{i}")
    assert pid is not None
    async with engine.begin() as conn:
        await conn.execute(update(products_t).where(products_t.c.id == pid).values(status="active"))
    return pid


@pytest.mark.asyncio
async def test_catalog_counts_and_paging(db: None) -> None:
    ids = [await _make_product(i) for i in range(5)]
    await set_category(ids[0], "new", lock=True)
    await set_category(ids[1], "new", lock=True)
    await set_category(ids[2], "mid", lock=True)
    await set_status(ids[3], "sold")  # sold -> excluded even though category defaults to "new"
    await set_category(ids[4], "old", lock=True)

    counts = await count_active_by_category()
    assert counts == {"new": 2, "mid": 1, "old": 1}

    items, total = await list_catalog_products("new", 0, 1)
    assert total == 2
    assert len(items) == 1
    assert items[0].id == ids[1]  # newest first (higher id created later)

    items2, total2 = await list_catalog_products("new", 1, 1)
    assert total2 == 2
    assert items2[0].id == ids[0]

    items3, total3 = await list_catalog_products("mid", 5, 1)  # out of range
    assert total3 == 1
    assert items3 == []

    empty_items, empty_total = await list_catalog_products("old", 1, 1)
    assert empty_total == 1
    assert empty_items == []

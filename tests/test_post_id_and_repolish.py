"""Tests for the post ID system (channel ID == database id) and the AI re-polish after an admin edit."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

import utils
from ai import router
from ai.copywriter import PolishedCopy, _validate_polish
from ai.errors import QuotaExceededError
from ai.providers import GenerateRequest
from caption import build_caption, caption_id, has_product_id, with_sold_banner
from db import products
from db.posts import add_post_log, live_posts
from poster import publish_product, refresh_live_posts
from post_id import backfill_caption_ids, ensure_caption_id
from repolish import repolish_after_edit

CHANNEL = -1001000000002
FIELDS = {
    "name": "Kurtka",
    "price": "150 000 so'm",
    "size": "M",
    "fabric": "Paxta",
    "stock": "5 dona",
    "hashtags": "#kurtka #qish #savdo",
    "sales_pitch": "Issiq va qulay kurtka.",
}
GOOD_JSON = json.dumps(
    {"sales_pitch": "Sovuq kunlar uchun issiq, qulay va zamonaviy kurtka.", "hashtags": "#kurtka #issiq #moda"}
)


# ------------------------------------------------------------------ helpers


class FakeBot:
    """Just enough of aiogram's Bot for poster.py: send_photo, edit_message_caption, delete_message."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self._next_id = 500

    async def me(self) -> Any:
        return SimpleNamespace(username="savdo_bot")

    async def send_photo(self, chat_id: int, photo: str, **kwargs: Any) -> Any:
        self._next_id += 1
        self.sent.append({"chat_id": chat_id, "message_id": self._next_id, **kwargs})
        return SimpleNamespace(message_id=self._next_id)

    async def edit_message_caption(self, chat_id: int, message_id: int, **kwargs: Any) -> None:
        self.edits.append({"chat_id": chat_id, "message_id": message_id, **kwargs})

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        return None


class ScriptedProvider:
    """Returns one scripted answer (or raises it); `hook` runs first, e.g. to simulate a concurrent edit."""

    name = "gemini"

    def __init__(self) -> None:
        self.outcome: Exception | str = GOOD_JSON
        self.hook: Callable[[], Awaitable[None]] | None = None
        self.requests: list[GenerateRequest] = []

    async def generate(self, api_key: str, model: str, req: GenerateRequest) -> str:
        self.requests.append(req)
        if self.hook is not None:
            await self.hook()
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest_asyncio.fixture
async def ai(db: None, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ScriptedProvider]:
    """Router wired to a ScriptedProvider."""
    provider = ScriptedProvider()
    monkeypatch.setattr(router, "GEMINI_PROVIDER", provider)
    router._alert_hook = None
    await router.init_router()
    yield provider
    pending = [t for t in utils._bg_tasks if not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    router._alert_hook = None


async def _done_product(n: int, *, with_id: bool = False) -> int:
    """A product whose AI result is ready; the stored caption has no ID line unless with_id (legacy shape)."""
    pid = await products.create_product_pending(-1001, n, f"file{n}", f"uniq{n}", "Kurtka 150000 som")
    assert pid is not None
    caption = build_caption(FIELDS, pid if with_id else None)
    await products.set_ai_result(pid, dict(FIELDS), caption, False)
    return pid


# --------------------------------------------------------- system 1: post ID


def test_caption_carries_the_id_and_stays_within_the_limit() -> None:
    long_fields = {**FIELDS, "sales_pitch": "Juda chiroyli mahsulot. " * 200}
    caption = build_caption(long_fields, 42)
    assert len(caption) <= 1024
    assert caption_id(caption) == 42
    assert has_product_id(caption, 42) and not has_product_id(caption, 43)
    assert has_product_id(with_sold_banner(caption), 42)  # the sold banner never cuts the ID line
    assert caption_id(build_caption(FIELDS)) is None  # no id given -> no ID line (old behaviour)


async def test_publish_prints_the_database_id_and_logs_the_channel_message(db: None) -> None:
    from db.chats import ROLE_TARGET, add_chat

    await add_chat(CHANNEL, ROLE_TARGET, "channel", "Test kanal", None, 111)  # a posting place must be registered
    pid = await _done_product(1)  # legacy caption without an ID line
    product = await products.get_product(pid)
    assert product is not None and caption_id(product.caption_html) is None

    bot = FakeBot()
    message_id = await publish_product(bot, product)  # type: ignore[arg-type]

    assert caption_id(bot.sent[0]["caption"]) == pid  # what the channel shows == products.id
    stored = await products.get_product(pid)
    assert stored is not None and caption_id(stored.caption_html) == pid  # ...and the DB caption matches
    posts = await live_posts(pid)
    assert [(p.chat_id, p.message_id) for p in posts] == [(CHANNEL, message_id)]  # message id is on file
    assert f"start=prod_{pid}" in bot.sent[0]["reply_markup"].inline_keyboard[0][0].url


async def test_ensure_caption_id_replaces_a_wrong_id(db: None) -> None:
    pid = await _done_product(1)
    await products.update_fields(pid, caption_html=build_caption(FIELDS, pid + 99))
    product = await products.get_product(pid)
    assert product is not None
    fixed = await ensure_caption_id(product)
    assert caption_id(fixed.caption_html) == pid
    assert caption_id((await products.get_product(pid)).caption_html) == pid  # type: ignore[union-attr]


async def test_backfill_fixes_only_captions_without_the_right_id(db: None) -> None:
    legacy_a = await _done_product(1)
    legacy_b = await _done_product(2)
    modern = await _done_product(3, with_id=True)
    pending = await products.create_product_pending(-1001, 4, "file4", "uniq4", "x")  # no caption yet
    assert pending is not None

    assert await backfill_caption_ids() == 2
    for pid in (legacy_a, legacy_b, modern):
        product = await products.get_product(pid)
        assert product is not None and has_product_id(product.caption_html, pid)
    untouched = await products.get_product(pending)
    assert untouched is not None and untouched.caption_html == ""
    assert await backfill_caption_ids() == 0  # idempotent


# ------------------------------------------------ system 2: AI re-polish


def test_polish_validation_only_accepts_numbers_from_the_admin_data() -> None:
    fields = {**FIELDS, "price": "120 000 so'm"}
    ok = _validate_polish(PolishedCopy(sales_pitch="Atigi 120 000 so'm, M o'lchamda.", hashtags="a b c"), fields)
    assert ok is not None and ok.hashtags == "#a #b #c"
    assert _validate_polish(PolishedCopy(sales_pitch="Endi 99 000 so'm!", hashtags="a b c"), fields) is None
    assert _validate_polish(PolishedCopy(sales_pitch="Hozir 150 000 so'm.", hashtags="a b c"), fields) is None  # stale price
    assert _validate_polish(PolishedCopy(sales_pitch="  ", hashtags="a b c"), fields) is None
    long = _validate_polish(PolishedCopy(sales_pitch="Chiroyli kurtka. " * 60, hashtags="a b c"), fields)
    assert long is not None and len(long.sales_pitch) <= 450


async def _edit_price(pid: int, price: str) -> None:
    """What the admin handler stores before the polish starts: new value + rebuilt caption."""
    fields = {**FIELDS, "price": price}
    await products.update_fields(
        pid, price=price, caption_html=build_caption(fields, pid), ai_json=json.dumps(fields, ensure_ascii=False)
    )


async def test_repolish_stores_the_new_pitch_and_edits_the_live_post(ai: ScriptedProvider) -> None:
    pid = await _done_product(1, with_id=True)
    await add_post_log(pid, CHANNEL, 777)
    await _edit_price(pid, "120 000 so'm")
    bot = FakeBot()

    outcome = await repolish_after_edit(bot, pid, "price", "150 000 so'm")  # type: ignore[arg-type]

    assert outcome.polished and outcome.live_edited == 1 and outcome.live_failed == 0
    product = await products.get_product(pid)
    assert product is not None
    assert product.price == "120 000 so'm"  # the admin's value is never rewritten by the AI
    assert product.hashtags == "#kurtka #issiq #moda"
    assert json.loads(product.ai_json or "{}")["sales_pitch"].startswith("Sovuq kunlar")
    assert "120 000 so'm" in product.caption_html and has_product_id(product.caption_html, pid)
    assert [(e["message_id"], e["caption"]) for e in bot.edits] == [(777, product.caption_html)]
    assert "old value: 150 000 so'm" in ai.requests[0].user_text  # the model is told what changed


async def test_repolish_falls_back_to_the_admins_caption_when_the_ai_invents_a_number(ai: ScriptedProvider) -> None:
    ai.outcome = json.dumps({"sales_pitch": "Endi atigi 99 000 so'm!", "hashtags": "a b c"})
    pid = await _done_product(1, with_id=True)
    await add_post_log(pid, CHANNEL, 777)
    await _edit_price(pid, "120 000 so'm")
    bot = FakeBot()

    outcome = await repolish_after_edit(bot, pid, "price", "150 000 so'm")  # type: ignore[arg-type]

    assert not outcome.polished and outcome.reason == "invalid"
    product = await products.get_product(pid)
    assert product is not None and "99 000" not in product.caption_html
    assert "120 000 so'm" in bot.edits[0]["caption"]  # the channel still gets the admin's data


async def test_repolish_survives_an_unavailable_ai(ai: ScriptedProvider) -> None:
    ai.outcome = QuotaExceededError("quota")
    pid = await _done_product(1, with_id=True)
    await add_post_log(pid, CHANNEL, 777)
    await _edit_price(pid, "120 000 so'm")
    bot = FakeBot()

    outcome = await repolish_after_edit(bot, pid, "price", "150 000 so'm")  # type: ignore[arg-type]

    assert not outcome.polished and outcome.reason == "unavailable" and outcome.live_edited == 1


async def test_repolish_is_dropped_when_a_newer_edit_arrives_meanwhile(ai: ScriptedProvider) -> None:
    pid = await _done_product(1, with_id=True)
    await add_post_log(pid, CHANNEL, 777)
    await _edit_price(pid, "120 000 so'm")

    async def newer_edit() -> None:
        await _edit_price(pid, "130 000 so'm")

    ai.hook = newer_edit
    bot = FakeBot()

    outcome = await repolish_after_edit(bot, pid, "price", "150 000 so'm")  # type: ignore[arg-type]

    assert outcome.stale and not outcome.polished
    product = await products.get_product(pid)
    assert product is not None and product.price == "130 000 so'm"
    assert "Sovuq kunlar" not in product.caption_html  # the outdated AI answer was not stored
    assert bot.edits == []  # ...and did not reach the channel


async def test_refresh_keeps_the_sold_banner_and_skips_removed_products(db: None) -> None:
    pid = await _done_product(1, with_id=True)
    await add_post_log(pid, CHANNEL, 777)
    bot = FakeBot()

    await products.set_status(pid, "sold")
    await refresh_live_posts(bot, pid)  # type: ignore[arg-type]
    assert bot.edits[0]["caption"].startswith("<b>\u274c BU MAHSULOT SOTILIB TUGADI")
    assert bot.edits[0]["reply_markup"] is None

    await products.set_status(pid, "removed")
    assert await refresh_live_posts(bot, pid) == {"edited": 0, "failed": 0}  # type: ignore[arg-type]
    assert len(bot.edits) == 1

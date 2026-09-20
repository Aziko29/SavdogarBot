"""Focused tests for scheduling, DB rules, caption/copy validation, the AI router and queue recovery."""
from __future__ import annotations

import asyncio
import random
from collections import Counter
from collections.abc import AsyncIterator, Callable
from datetime import datetime, time, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import update

import scheduler
import utils
import worker
from ai import router
from ai.copywriter import _MISSING, ProductCopy, _fix_hashtags, _validate
from ai.errors import (
    AllProvidersFailed,
    ModelNotSupportedError,
    QuotaExceededError,
    RequestTooLargeError,
)
from ai.providers import GenerateRequest
from caption import build_caption, strip_html, with_sold_banner
from config import settings
from db import keystate, orders, products
from db.engine import engine
from db.models import BotSettings, Product
from db.schema import products_t
from handlers.client import _DEEP_LINK_RE
from utils import key_id, utcnow

K1, K2 = settings.gemini_groups[0]
OK_JSON = '{"ok": true}'


# ------------------------------------------------------------------ helpers


async def _drain_background() -> None:
    """Wait for fire_and_forget tasks (router state persistence, alerts)."""
    pending = [t for t in utils._bg_tasks if not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def _add_product(n: int, text: str = "Kurtka 150000 som") -> int:
    """Insert a pending product with a unique source message and photo."""
    pid = await products.create_product_pending(-1001, n, f"file{n}", f"uniq{n}", text)
    assert pid is not None
    return pid


async def _categories() -> dict[int, str]:
    """Map product id -> category for every stored product."""
    items, _ = await products.list_products("all", 0, 100)
    return {p.id: p.category for p in items}


def _make_product(pid: int, category: str, last_posted_at: datetime | None = None) -> Product:
    """In-memory Product ready for posting (no DB involved)."""
    now = utcnow()
    return Product(
        id=pid, source_chat_id=-1001, source_msg_id=pid, file_unique_id=f"u{pid}", tg_file_id=f"f{pid}",
        original_text="", ai_json=None, name=f"P{pid}", price="", size="", fabric="", stock="",
        hashtags="", caption_html="", status="active", category=category, category_locked=False,
        ai_status="done", attempts=0, next_try_at=None, needs_review=False,
        last_posted_at=last_posted_at, post_count=0, created_at=now, updated_at=now,
    )


def _bot_settings(**over: Any) -> BotSettings:
    """BotSettings with the default weights 5/3/1, overridable per test."""
    base: dict[str, Any] = dict(
        new_multi=5, mid_multi=3, old_multi=1, interval_mins=30, night_start="23:00", night_end="08:00",
        new_keep=10, mid_keep=20, autopost_enabled=True, repost_policy="delete_previous",
    )
    base.update(over)
    return BotSettings(**base)


# ---------------------------------------------------------------- is_night


@pytest.mark.parametrize(
    ("t", "expected"),
    [
        (time(23, 0), True),
        (time(0, 0), True),
        (time(7, 59), True),
        (time(8, 0), False),
        (time(22, 59), False),
        (time(12, 0), False),
    ],
)
def test_is_night_crossing_midnight(t: time, expected: bool) -> None:
    assert scheduler.is_night(t, time(23, 0), time(8, 0)) is expected


def test_is_night_same_day_window() -> None:
    assert scheduler.is_night(time(1, 0), time(1, 0), time(5, 0)) is True
    assert scheduler.is_night(time(5, 0), time(1, 0), time(5, 0)) is False
    assert scheduler.is_night(time(0, 59), time(1, 0), time(5, 0)) is False


# -------------------------------------------------------- weighted selection


@pytest.fixture
def post_env(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[Product]]:
    """Patch scheduler dependencies; returns a configurator and collects published products."""
    published: list[Product] = []

    async def fake_publish(bot: Any, product: Product) -> int:
        published.append(product)
        return 1

    def configure(
        candidates: list[Product], bot_settings: BotSettings | None = None, recent: list[int] | None = None
    ) -> list[Product]:
        async def fake_candidates() -> list[Product]:
            return candidates

        async def fake_settings() -> BotSettings:
            return bot_settings or _bot_settings()

        async def fake_recent(n: int) -> list[int]:
            return list(recent or [])

        monkeypatch.setattr(scheduler, "list_postable", fake_candidates)
        monkeypatch.setattr(scheduler, "get_settings", fake_settings)
        monkeypatch.setattr(scheduler, "recent_posted_product_ids", fake_recent)
        monkeypatch.setattr(scheduler, "publish_product", fake_publish)
        published.clear()
        return published

    return configure


async def test_weighted_selection_follows_weights(post_env: Callable[..., list[Product]]) -> None:
    published = post_env([_make_product(1, "new"), _make_product(2, "mid"), _make_product(3, "old")])
    random.seed(20260920)
    for _ in range(900):
        await scheduler.post_next(None, force=True)  # type: ignore[arg-type]
    counts = Counter(p.category for p in published)
    assert abs(counts["new"] - 500) < 70
    assert abs(counts["mid"] - 300) < 70
    assert abs(counts["old"] - 100) < 60


async def test_weighted_selection_seeded_and_zero_weight(post_env: Callable[..., list[Product]]) -> None:
    items = [_make_product(1, "new"), _make_product(2, "mid"), _make_product(3, "old")]
    published = post_env(items)
    runs: list[list[int]] = []
    for _ in range(2):
        published.clear()
        random.seed(7)
        for _ in range(40):
            await scheduler.post_next(None, force=True)  # type: ignore[arg-type]
        runs.append([p.id for p in published])
    assert runs[0] == runs[1]

    published = post_env(items, _bot_settings(old_multi=0))
    random.seed(7)
    for _ in range(200):
        await scheduler.post_next(None, force=True)  # type: ignore[arg-type]
    assert {p.category for p in published} == {"new", "mid"}


async def test_repost_gap_and_no_repeat_filters(post_env: Callable[..., list[Product]]) -> None:
    recent_post = _make_product(1, "new", last_posted_at=utcnow() - timedelta(hours=1))
    fresh = _make_product(2, "new")
    published = post_env([recent_post, fresh])
    for _ in range(30):
        await scheduler.post_next(None, force=True)  # type: ignore[arg-type]
    assert {p.id for p in published} == {2}

    published = post_env([recent_post])  # nothing eligible -> falls back to all candidates
    await scheduler.post_next(None, force=True)  # type: ignore[arg-type]
    assert [p.id for p in published] == [1]

    published = post_env([_make_product(1, "new"), _make_product(2, "new")], recent=[1])
    for _ in range(30):
        await scheduler.post_next(None, force=True)  # type: ignore[arg-type]
    assert {p.id for p in published} == {2}


# ------------------------------------------------ categories and duplicates


async def test_recompute_categories_and_locks(db: None) -> None:
    ids = [await _add_product(n) for n in range(1, 7)]
    await products.set_category(ids[0], "new", lock=True)  # oldest product is pinned as "new"

    await products.recompute_categories(new_keep=2, mid_keep=2)
    cats = await _categories()
    assert [cats[i] for i in ids] == ["new", "old", "mid", "mid", "new", "new"]

    await products.set_status(ids[5], "sold")  # sold products leave the ranking untouched
    await products.recompute_categories(new_keep=2, mid_keep=2)
    cats = await _categories()
    assert cats[ids[5]] == "new"
    assert [cats[i] for i in ids[:5]] == ["new", "mid", "mid", "new", "new"]

    with pytest.raises(ValueError):
        await products.recompute_categories(-1, 0)


async def test_duplicate_source_or_photo_is_ignored(db: None) -> None:
    first = await products.create_product_pending(-1001, 1, "fileA", "uniqA", "x")
    assert first is not None
    assert await products.create_product_pending(-1001, 1, "fileB", "uniqB", "x") is None  # same message
    assert await products.create_product_pending(-1001, 2, "fileC", "uniqA", "x") is None  # same photo


# ------------------------------------------------- copy validation helpers


def _copy(price: str) -> ProductCopy:
    return ProductCopy(
        name="Ko'ylak", price=price, size="M", fabric="Paxta", stock="bor",
        hashtags="#a #b #c", sales_pitch="Zo'r mahsulot.",
    )


@pytest.mark.parametrize(
    ("price", "source", "kept"),
    [
        ("150 000 so'm", "Narxi: 150000 so'm", True),
        ("150 ming", "Narx 150000", True),
        ("150k", "narx 150k", True),
        ("Kelishiladi", "Narx 150000", True),
        ("200 000 so'm", "Narxi: 150000 so'm", False),
    ],
)
def test_price_grounding(price: str, source: str, kept: bool) -> None:
    fields, needs_review = _validate(_copy(price), source)
    if kept:
        assert fields["price"] == price
        assert needs_review is False
    else:
        # An invented price is rejected; the keyword fallback may then copy the real price from the source.
        assert fields["price"] != price
        assert fields["price"] in (_MISSING, "150000 so'm")
        assert needs_review is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("#Kurta #Yozgi #Sale", "#kurta #yozgi #sale"),
        ("kurta", "#kurta #mahsulot #savdo"),
        ("#a #a #b #c #d", "#a #b #c"),
        ("", "#mahsulot #savdo #tashkent"),
    ],
)
def test_hashtag_normalization(raw: str, expected: str) -> None:
    assert _fix_hashtags(raw) == expected


# --------------------------------------------------------------- captions


def _caption_fields(**over: str) -> dict[str, str]:
    base = dict(
        name="Ko'ylak", sales_pitch="Chiroyli.", price="150 000 so'm", size="M",
        fabric="Paxta", stock="bor", hashtags="#a #b #c",
    )
    base.update(over)
    return base


def test_caption_limit_and_html_escaping() -> None:
    fields = _caption_fields(name="<script>alert(1)</script> & Co", sales_pitch="Juda chiroyli mahsulot. " * 200)
    caption = build_caption(fields)
    assert len(caption) <= 1024
    assert "<script>" not in caption
    assert "&lt;script&gt;" in caption
    assert "&amp; Co" in caption
    assert "#a #b #c" in caption  # trimming shortens the pitch, never the footer


def test_sold_banner_respects_limit() -> None:
    short = build_caption(_caption_fields())
    banner = with_sold_banner(short)
    assert "SOTILIB TUGADI" in banner
    assert banner.endswith(short)

    long_caption = build_caption(_caption_fields(sales_pitch="Juda chiroyli mahsulot. " * 200))
    assert len(with_sold_banner(long_caption)) <= 1024


def test_strip_html() -> None:
    assert strip_html("<b>A &amp; B</b>\n") == "A & B"


# ------------------------------------------------------------- AI router


class FakeProvider:
    """Scripted provider: per-(key, model) outcomes, then a default outcome."""

    name = "gemini"

    def __init__(self) -> None:
        self.script: dict[tuple[str, str], list[Exception | str]] = {}
        self.default: Exception | str = OK_JSON
        self.too_large: set[str] = set()
        self.calls: list[tuple[str, str, str]] = []

    async def generate(self, api_key: str, model: str, req: GenerateRequest) -> str:
        self.calls.append((api_key, model, req.user_text))
        if req.user_text in self.too_large:
            raise RequestTooLargeError("payload too large")
        queue = self.script.get((api_key, model))
        outcome = queue.pop(0) if queue else self.default
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest_asyncio.fixture
async def fake_ai(db: None, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeProvider]:
    """Router wired to a FakeProvider with keys K1/K2 and models m1/m2 (priority order)."""
    provider = FakeProvider()
    monkeypatch.setattr(router, "GEMINI_PROVIDER", provider)
    router._alert_hook = None
    await router.init_router()
    yield provider
    await _drain_background()
    router._alert_hook = None


def _req(text: str = "u", alt: GenerateRequest | None = None) -> GenerateRequest:
    return GenerateRequest(system_prompt="s", user_text=text, alt=alt)


async def test_router_quota_moves_to_next_pair(fake_ai: FakeProvider) -> None:
    fake_ai.script[(K1, "m1")] = [QuotaExceededError("rate limit")]
    assert await router.generate_json(_req()) == OK_JSON
    assert [c[:2] for c in fake_ai.calls] == [(K1, "m1"), (K1, "m2")]
    status = router.router_status()
    assert status["cooling_pairs"] == 1
    assert not any(k["exhausted"] for k in status["keys"])
    assert all(K1 not in str(k) and K2 not in str(k) for k in status["keys"])  # labels only

    assert await router.generate_json(_req()) == OK_JSON  # the cooling pair is skipped
    assert fake_ai.calls[-1][:2] == (K1, "m2")


async def test_router_unsupported_model_becomes_persisted_bad_pair(fake_ai: FakeProvider) -> None:
    fake_ai.script[(K1, "m1")] = [ModelNotSupportedError("404 model not found")]
    assert await router.generate_json(_req()) == OK_JSON
    await _drain_background()
    assert (key_id(K1), "m1") in await keystate.load_bad_pairs()

    await router.init_router()  # simulates a restart: the bad pair is loaded from the DB
    assert router.router_status()["bad_pairs"] == 1
    assert await router.generate_json(_req()) == OK_JSON
    assert fake_ai.calls[-1][:2] == (K1, "m2")


async def test_router_too_large_retries_with_lighter_request(fake_ai: FakeProvider) -> None:
    fake_ai.too_large = {"heavy"}
    result = await router.generate_json(_req("heavy", alt=_req("light")))
    assert result == OK_JSON
    assert fake_ai.calls == [(K1, "m1", "heavy"), (K1, "m1", "light")]


async def test_router_all_failed_raises_and_alerts(fake_ai: FakeProvider) -> None:
    alerts: list[tuple[str, str]] = []

    async def hook(text: str, throttle_key: str) -> None:
        alerts.append((text, throttle_key))

    router.set_alert_hook(hook)
    fake_ai.default = QuotaExceededError("quota")
    with pytest.raises(AllProvidersFailed):
        await router.generate_json(_req())
    await _drain_background()
    assert len(fake_ai.calls) == 4  # 2 keys x 2 models, each tried once
    assert [key for _, key in alerts] == ["ai_all_failed"]


async def test_router_sticky_only_within_top_priority_group(fake_ai: FakeProvider) -> None:
    def entry(kid: str, group: int) -> router._Entry:
        return router._Entry(fake_ai, group, "secret", kid, "m", False, True)  # type: ignore[arg-type]

    a, b, c = entry("a", 0), entry("b", 0), entry("c", 1)
    router._sticky["text"] = ("b", "gemini", "m")
    assert router._apply_sticky([a, b, c], "text") == [b, a, c]
    router._sticky["text"] = ("c", "gemini", "m")  # lower-priority group: priority order wins
    assert router._apply_sticky([a, b, c], "text") == [a, b, c]
    router._sticky["text"] = None
    assert router._apply_sticky([a, b, c], "text") == [a, b, c]


# ------------------------------------------------ orders, links, recovery


async def test_decide_order_is_idempotent(db: None) -> None:
    pid = await _add_product(1)
    oid = await orders.create_order(pid, 555, "buyer", "Buyer B", "kerak")
    assert await orders.recent_pending_exists(555, pid) is True

    assert await orders.decide_order(oid, "accepted") is True
    assert await orders.decide_order(oid, "rejected") is False
    assert await orders.decide_order(oid, "accepted") is False
    order = await orders.get_order(oid)
    assert order is not None and order.status == "accepted"
    assert await orders.recent_pending_exists(555, pid) is False
    assert await orders.decide_order(9999, "accepted") is False
    with pytest.raises(ValueError):
        await orders.decide_order(oid, "pending")


@pytest.mark.parametrize(
    ("payload", "product_id"),
    [("prod_12", 12), ("prod_123456789012", 123456789012)],
)
def test_deep_link_accepts_valid_payloads(payload: str, product_id: int) -> None:
    match = _DEEP_LINK_RE.match(payload)
    assert match is not None and int(match.group(1)) == product_id


@pytest.mark.parametrize("payload", ["", "prod_", "prod_-1", "prod_1234567890123", "PROD_1", "x prod_1", "prod_1 "])
def test_deep_link_rejects_invalid_payloads(payload: str) -> None:
    assert _DEEP_LINK_RE.match(payload) is None


async def test_queue_recovery_requeues_due_and_stuck_products(db: None) -> None:
    due = await _add_product(1)
    delayed = await _add_product(2)
    await products.set_ai_status(delayed, "pending", next_try_at=utcnow() + timedelta(hours=1))
    stuck = await _add_product(3)
    assert await products.claim_for_ai(stuck) is not None
    async with engine.begin() as conn:
        await conn.execute(
            update(products_t).where(products_t.c.id == stuck).values(updated_at=utcnow() - timedelta(minutes=30))
        )
    busy = await _add_product(4)
    assert await products.claim_for_ai(busy) is not None  # fresh 'processing' must be left alone
    finished = await _add_product(5)
    await products.set_ai_result(finished, {"name": "N"}, "<b>N</b>", False)

    while not worker.PRODUCT_QUEUE.empty():
        worker.PRODUCT_QUEUE.get_nowait()

    assert await worker.recover_pending() == 2
    queued = [worker.PRODUCT_QUEUE.get_nowait() for _ in range(worker.PRODUCT_QUEUE.qsize())]
    assert queued == [due, stuck]
    stuck_after = await products.get_product(stuck)
    busy_after = await products.get_product(busy)
    assert stuck_after is not None and stuck_after.ai_status == "pending"
    assert busy_after is not None and busy_after.ai_status == "processing"

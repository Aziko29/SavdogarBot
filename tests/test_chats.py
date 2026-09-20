"""Runtime-managed source/target chats: DB + RAM cache, source filter and multi-target publishing."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramForbiddenError

import poster
from db import chats, posts, products


async def _add(chat_id: int, role: str, chat_type: str = "channel", title: str = "T") -> bool:
    return await chats.add_chat(
        chat_id=chat_id, role=role, chat_type=chat_type, title=title, username=None, added_by=111
    )


async def test_add_populates_the_ram_cache(db) -> None:
    assert await _add(-1001, "source", title="Manba") is True
    assert await _add(-1002, "target", title="Kanal") is True
    assert await _add(-1003, "target", "supergroup", "Guruh") is True

    assert chats.is_source_chat(-1001)
    assert not chats.is_source_chat(-1002)
    assert not chats.is_source_chat(None)
    assert [c.chat_id for c in chats.source_chats()] == [-1001]
    assert [c.chat_id for c in chats.target_chats()] == [-1002, -1003]


async def test_load_chats_restores_the_cache(db) -> None:
    await _add(-1001, "source")
    await _add(-1002, "target")
    chats._cache = ()
    chats._source_ids = frozenset()
    await chats.load_chats()
    assert chats.is_source_chat(-1001)
    assert [c.chat_id for c in chats.target_chats()] == [-1002]


async def test_re_adding_refreshes_details_and_reports_false(db) -> None:
    assert await _add(-1002, "target", title="Old") is True
    assert await _add(-1002, "target", title="New") is False
    (entry,) = chats.target_chats()
    assert entry.title == "New"


async def test_one_chat_cannot_be_source_and_target(db) -> None:
    await _add(-1001, "source")
    with pytest.raises(ValueError):
        await _add(-1001, "target")
    await _add(-1002, "target")
    with pytest.raises(ValueError):
        await _add(-1002, "source")


@pytest.mark.parametrize(
    ("chat_id", "role", "chat_type"),
    [(0, "target", "channel"), (-1, "boss", "channel"), (-1, "target", "private"), (-1, "source", "supergroup")],
)
async def test_invalid_input_is_rejected(db, chat_id: int, role: str, chat_type: str) -> None:
    with pytest.raises(ValueError):
        await _add(chat_id, role, chat_type)


async def test_remove_chat_and_remove_everywhere(db) -> None:
    await _add(-1001, "source")
    await _add(-1002, "target")
    entries = await chats.list_chats("target")
    removed = await chats.remove_chat(entries[0].id)
    assert removed is not None and removed.chat_id == -1002
    assert await chats.remove_chat(entries[0].id) is None
    assert chats.target_chats() == []

    gone = await chats.remove_chats_for(-1001)
    assert [e.role for e in gone] == ["source"]
    assert not chats.is_source_chat(-1001)
    assert await chats.remove_chats_for(-1001) == []


# ---------------------------------------------------------------- publishing to several targets


class _FakeBot:
    def __init__(self, forbidden: set[int] | None = None) -> None:
        self.forbidden = forbidden or set()
        self.sent: list[tuple[int, str]] = []
        self.deleted: list[tuple[int, int]] = []
        self._next_id = 100

    async def me(self) -> SimpleNamespace:
        return SimpleNamespace(username="savdo_bot")

    async def send_photo(self, chat_id: int, photo: str, **_: object) -> SimpleNamespace:
        if chat_id in self.forbidden:
            raise TelegramForbiddenError(method=None, message="Forbidden: bot was kicked")
        self._next_id += 1
        self.sent.append((chat_id, photo))
        return SimpleNamespace(message_id=self._next_id)

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.deleted.append((chat_id, message_id))

    async def send_message(self, *_: object, **__: object) -> None:  # admin alerts
        return None


async def _ready_product():
    pid = await products.create_product_pending(-1001, 1, "fileA", "uniqA", "x")
    assert pid is not None
    await products.set_ai_result(pid, {"name": "N"}, "<b>N</b>", False)
    product = await products.get_product(pid)
    assert product is not None
    return product


async def test_publish_without_a_target_raises(db) -> None:
    product = await _ready_product()
    with pytest.raises(poster.NoTargetError):
        await poster.publish_product(_FakeBot(), product)


async def test_publish_goes_to_every_target_and_counts_once(db) -> None:
    await _add(-1002, "target")
    await _add(-1003, "target", "supergroup", "Guruh")
    product = await _ready_product()
    bot = _FakeBot()

    first_id = await poster.publish_product(bot, product)

    assert [chat for chat, _ in bot.sent] == [-1002, -1003]
    assert first_id == 101
    assert {p.chat_id for p in await posts.live_posts(product.id)} == {-1002, -1003}
    assert (await products.get_product(product.id)).post_count == 1


async def test_one_failing_target_does_not_block_the_others(db) -> None:
    await _add(-1002, "target")
    await _add(-1003, "target")
    product = await _ready_product()
    bot = _FakeBot(forbidden={-1002})

    await poster.publish_product(bot, product)

    assert [chat for chat, _ in bot.sent] == [-1003]
    assert [p.chat_id for p in await posts.live_posts(product.id)] == [-1003]


async def test_all_targets_failing_raises_publish_error(db) -> None:
    await _add(-1002, "target")
    product = await _ready_product()
    with pytest.raises(poster.PublishError):
        await poster.publish_product(_FakeBot(forbidden={-1002}), product)
    assert (await products.get_product(product.id)).post_count == 0


async def test_repost_deletes_the_previous_posts_in_every_target(db) -> None:
    await _add(-1002, "target")
    await _add(-1003, "target")
    product = await _ready_product()
    bot = _FakeBot()

    await poster.publish_product(bot, product)
    await poster.publish_product(bot, (await products.get_product(product.id)))

    assert sorted(chat for chat, _ in bot.deleted) == [-1003, -1002]
    assert len(await posts.live_posts(product.id)) == 2


# ---------------------------------------------------------------- unregistered chats: leave countdown

from datetime import timedelta  # noqa: E402

from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError  # noqa: E402

import chat_guard  # noqa: E402
from utils import utcnow  # noqa: E402


async def test_track_pending_keeps_the_original_deadline(db) -> None:
    first = utcnow() + timedelta(hours=6)
    assert await chats.track_pending(-2001, "group", "G", 5, first) is True
    assert await chats.track_pending(-2001, "group", "G2", 5, first + timedelta(hours=6)) is False
    (pending,) = await chats.list_pending_chats()
    assert pending.title == "G2"
    assert pending.expires_at == first


async def test_registering_clears_the_countdown_and_registered_chats_are_never_tracked(db) -> None:
    await chats.track_pending(-2001, "channel", "C", 5, utcnow() + timedelta(hours=6))
    await _add(-2001, "target")
    assert await chats.list_pending_chats() == []
    assert await chats.track_pending(-2001, "channel", "C", 5, utcnow() + timedelta(hours=6)) is False


async def test_bot_removed_from_a_chat_clears_the_countdown(db) -> None:
    await chats.track_pending(-2001, "group", "G", 5, utcnow() + timedelta(hours=6))
    assert await chats.remove_chats_for(-2001) == []
    assert await chats.list_pending_chats() == []


async def test_due_pending_returns_only_expired_chats(db) -> None:
    await chats.track_pending(-2001, "group", "old", 5, utcnow() - timedelta(minutes=1))
    await chats.track_pending(-2002, "group", "new", 5, utcnow() + timedelta(hours=1))
    assert [p.chat_id for p in await chats.due_pending_chats(utcnow())] == [-2001]


class _LeaveBot:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.left: list[int] = []
        self.messages: list[str] = []

    async def leave_chat(self, chat_id: int) -> None:
        if self.error is not None:
            raise self.error
        self.left.append(chat_id)

    async def send_message(self, chat_id: int, text: str, **_: object) -> None:
        self.messages.append(text)


async def test_sweep_leaves_only_expired_unregistered_chats_and_tells_the_head_admin(db) -> None:
    await chats.track_pending(-3001, "group", "Expired", 5, utcnow() - timedelta(minutes=1))
    await chats.track_pending(-3002, "group", "Waiting", 5, utcnow() + timedelta(hours=1))
    bot = _LeaveBot()

    assert await chat_guard.sweep_pending_chats(bot) == 1

    assert bot.left == [-3001]
    assert len(bot.messages) == 1
    assert [p.chat_id for p in await chats.list_pending_chats()] == [-3002]


async def test_sweep_drops_a_chat_the_bot_is_already_out_of(db) -> None:
    await chats.track_pending(-3001, "group", "Gone", 5, utcnow() - timedelta(minutes=1))
    bot = _LeaveBot(TelegramBadRequest(method=None, message="Bad Request: chat not found"))

    assert await chat_guard.sweep_pending_chats(bot) == 0

    assert await chats.list_pending_chats() == []
    assert bot.messages == []


async def test_sweep_keeps_the_chat_for_a_retry_on_a_network_error(db) -> None:
    await chats.track_pending(-3001, "group", "Retry", 5, utcnow() - timedelta(minutes=1))
    bot = _LeaveBot(TelegramNetworkError(method=None, message="connection reset"))

    assert await chat_guard.sweep_pending_chats(bot) == 0

    assert [p.chat_id for p in await chats.list_pending_chats()] == [-3001]

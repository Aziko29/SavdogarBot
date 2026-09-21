"""Inquiry single-admin ownership (claim), the 1-hour idle notice and the hand-over to whoever continues."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetMe
from aiogram.types import Message
from sqlalchemy import select, update

import inquiry_idle
import scheduler
from db import inquiries, products
from db.engine import engine
from db.schema import inquiries_t
from handlers import client
from utils import utcnow

ADMINS = [111, 222]
CUSTOMER = 7


class _FakeBot:
    """Records what the bot sends/edits; message ids are handed out sequentially."""

    def __init__(self, missing: set[int] | None = None) -> None:
        self.sent: list[tuple[int, str, dict[str, Any]]] = []
        self.copied: list[tuple[int, int, int]] = []  # (to_chat, from_chat, from_message)
        self.edited: list[tuple[int, int, str]] = []
        self.missing = missing or set()  # source messages that "no longer exist"
        self.sent_ids: list[int] = []  # ids the fake Telegram gave to sent messages
        self.copy_ids: list[int] = []  # ...and to copied ones
        self._next = 1000

    def _id(self) -> int:
        self._next += 1
        return self._next

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> SimpleNamespace:
        self.sent.append((chat_id, text, kwargs))
        self.sent_ids.append(self._id())
        return SimpleNamespace(message_id=self.sent_ids[-1])

    async def copy_message(self, chat_id: int, from_chat_id: int, message_id: int, **kwargs: Any) -> SimpleNamespace:
        if message_id in self.missing:
            raise TelegramBadRequest(method=GetMe(), message="message to copy not found")
        self.copied.append((chat_id, from_chat_id, message_id))
        self.copy_ids.append(self._id())
        return SimpleNamespace(message_id=self.copy_ids[-1])

    async def edit_message_text(self, text: str, chat_id: int, message_id: int, **kwargs: Any) -> None:
        self.edited.append((chat_id, message_id, text))

    async def get_chat(self, chat_id: int) -> SimpleNamespace:
        return SimpleNamespace(first_name=f"Admin{chat_id}", last_name=None, username=None)

    def texts_to(self, chat_id: int) -> list[str]:
        return [text for to, text, _ in self.sent if to == chat_id]


def _callback(admin_id: int, message_id: int = 500) -> SimpleNamespace:
    """A CallbackQuery stand-in whose .message passes the handlers' isinstance(Message) check."""
    msg = MagicMock(spec=Message)
    msg.message_id = message_id
    msg.edit_reply_markup = AsyncMock()
    return SimpleNamespace(
        from_user=SimpleNamespace(id=admin_id, full_name=f"Admin {admin_id}"), message=msg, answer=AsyncMock()
    )


def _answered(callback: SimpleNamespace) -> tuple[str | None, bool]:
    args, kwargs = callback.answer.call_args
    return args[0], kwargs.get("show_alert", False)


def _customer_message(text_id: int = 50) -> SimpleNamespace:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=CUSTOMER, full_name="Ali"),
        chat=SimpleNamespace(id=CUSTOMER),
        message_id=text_id,
    )


def _admin_reply(admin_id: int, message_id: int = 60) -> SimpleNamespace:
    msg = SimpleNamespace(
        from_user=SimpleNamespace(id=admin_id, full_name=f"Admin {admin_id}"),
        chat=SimpleNamespace(id=admin_id),
        message_id=message_id,
        reply=AsyncMock(),
    )
    return msg


@pytest.fixture(autouse=True)
def _two_admins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "admin_ids", lambda: ADMINS)


async def _open(product_id: int | None = None) -> None:
    await inquiries.open_inquiry(CUSTOMER, product_id, "Ali")


async def _make_product() -> int:
    pid = await products.create_product_pending(-1001, 1, "file1", "uniq1", "Kurtka 150000 som")
    assert pid is not None
    await products.update_fields(pid, name="Kurtka", price="150000 so'm")
    return pid


async def _idle_for(hours: float, user_id: int = CUSTOMER) -> None:
    """Pretend the last activity was `hours` ago (bypasses the column's onupdate hook by setting it explicitly)."""
    async with engine.begin() as conn:
        await conn.execute(
            update(inquiries_t).where(inquiries_t.c.user_id == user_id).values(updated_at=utcnow() - timedelta(hours=hours))
        )


async def _flag() -> bool:
    async with engine.connect() as conn:
        return bool((await conn.execute(select(inquiries_t.c.idle_notice_sent))).scalar_one())


# ------------------------------------------------ claiming


async def test_first_admin_to_claim_wins_and_the_second_is_refused(db: None) -> None:
    await _open()
    first = await inquiries.try_claim_inquiry(CUSTOMER, 111)
    assert (first.success, first.newly_claimed, first.claimed_by) == (True, True, 111)

    second = await inquiries.try_claim_inquiry(CUSTOMER, 222)
    assert (second.success, second.newly_claimed, second.is_open, second.claimed_by) == (False, False, True, 111)

    again = await inquiries.try_claim_inquiry(CUSTOMER, 111)  # the owner "claiming" again is a harmless no-op
    assert (again.success, again.newly_claimed) == (True, False)


async def test_concurrent_claims_have_exactly_one_winner(db: None) -> None:
    await _open()
    results = await asyncio.gather(*(inquiries.try_claim_inquiry(CUSTOMER, a) for a in (111, 222, 333)))
    winners = [r for r in results if r.success]
    assert len(winners) == 1 and winners[0].newly_claimed
    found = await inquiries.get_open_inquiry(CUSTOMER)
    assert found is not None and found.claimed_by == winners[0].claimed_by


async def test_claiming_a_closed_inquiry_fails(db: None) -> None:
    await _open()
    await inquiries.close_inquiry_if_open(CUSTOMER)
    res = await inquiries.try_claim_inquiry(CUSTOMER, 111)
    assert (res.success, res.is_open, res.claimed_by) == (False, False, None)


async def test_reopening_starts_unclaimed(db: None) -> None:
    await _open()
    await inquiries.try_claim_inquiry(CUSTOMER, 111)
    await _open()
    found = await inquiries.get_open_inquiry(CUSTOMER)
    assert found is not None and found.claimed_by is None


# ------------------------------------------------ routing of the customer's messages


async def test_unclaimed_inquiry_reaches_every_admin_then_only_the_owner(db: None) -> None:
    await _open()
    bot = _FakeBot()
    await client._relay_inquiry_message(_customer_message(50), bot)  # type: ignore[arg-type]
    assert {to for to, _, _ in bot.copied} == set(ADMINS)  # nobody owns it yet: everyone sees it

    await inquiries.try_claim_inquiry(CUSTOMER, 222)
    bot2 = _FakeBot()
    await client._relay_inquiry_message(_customer_message(51), bot2)  # type: ignore[arg-type]
    assert [to for to, _, _ in bot2.copied] == [222]  # the bug fixed: only the owner gets it now
    assert bot2.texts_to(111) == []


async def test_claim_button_first_tap_wins_and_retires_the_other_cards(db: None) -> None:
    await _open()
    await inquiries.replace_inquiry_notice_cards(CUSTOMER, [(111, 900), (222, 901)])
    bot = _FakeBot()

    winner = _callback(111, 900)
    await client.on_inquiry_claim(winner, client.InquiryClaimCB(user_id=CUSTOMER), bot)  # type: ignore[arg-type]
    assert _answered(winner) == (client._INQUIRY_CLAIMED_TEXT, False)
    assert [(chat, mid) for chat, mid, _ in bot.edited] == [(222, 901)]  # the loser's card became a plain notice
    assert "Admin 111" in bot.edited[0][2]

    loser = _callback(222, 901)
    await client.on_inquiry_claim(loser, client.InquiryClaimCB(user_id=CUSTOMER), bot)  # type: ignore[arg-type]
    text, alert = _answered(loser)
    assert alert and "Admin111" in (text or "")
    found = await inquiries.get_open_inquiry(CUSTOMER)
    assert found is not None and found.claimed_by == 111


async def test_replying_claims_for_the_first_admin_and_is_refused_for_the_rest(db: None) -> None:
    await _open()
    await inquiries.record_inquiry_relay(CUSTOMER, 111, 900)
    await inquiries.record_inquiry_relay(CUSTOMER, 222, 901)
    bot = _FakeBot()

    reply1 = _admin_reply(111)
    await client._reply_to_inquiry(reply1, bot, 900)  # type: ignore[arg-type]
    assert bot.copied == [(CUSTOMER, 111, 60)]  # delivered to the customer
    found = await inquiries.get_open_inquiry(CUSTOMER)
    assert found is not None and found.claimed_by == 111

    reply2 = _admin_reply(222, 61)
    await client._reply_to_inquiry(reply2, bot, 901)  # type: ignore[arg-type]
    assert len(bot.copied) == 1  # nothing more reached the customer
    assert "111" in reply2.reply.call_args.args[0] or "Admin111" in reply2.reply.call_args.args[0]


# ------------------------------------------------ idle detection (db)


async def test_only_claimed_quiet_unpinged_inquiries_are_idle(db: None) -> None:
    await _open()
    assert await inquiries.list_idle_claimed_inquiries(1) == []  # fresh
    await _idle_for(2)
    assert await inquiries.list_idle_claimed_inquiries(1) == []  # quiet but unclaimed: everyone already sees it
    await inquiries.try_claim_inquiry(CUSTOMER, 111)  # claiming counts as activity...
    assert await inquiries.list_idle_claimed_inquiries(1) == []
    await _idle_for(2)  # ...so go quiet again
    idle = await inquiries.list_idle_claimed_inquiries(1)
    assert [(i.user_id, i.claimed_by) for i in idle] == [(CUSTOMER, 111)]
    assert await inquiries.list_idle_claimed_inquiries(3) == []  # 2 h of quiet is not 3 h


async def test_idle_notice_is_marked_once_and_does_not_restart_the_clock(db: None) -> None:
    await _open()
    await inquiries.try_claim_inquiry(CUSTOMER, 111)
    await _idle_for(2)
    assert await inquiries.mark_idle_notice_sent(CUSTOMER, older_than_hours=1) is True
    assert await inquiries.mark_idle_notice_sent(CUSTOMER, older_than_hours=1) is False  # only one pinger wins
    assert await inquiries.list_idle_claimed_inquiries(1) == []
    async with engine.connect() as conn:
        updated = (await conn.execute(select(inquiries_t.c.updated_at))).scalar_one()
    assert utcnow() - updated.replace(tzinfo=updated.tzinfo or utcnow().tzinfo) > timedelta(hours=1.5)


async def test_activity_cancels_a_pending_notice_and_rearms_the_next_one(db: None) -> None:
    await _open()
    await inquiries.try_claim_inquiry(CUSTOMER, 111)
    await _idle_for(2)
    await inquiries.touch_inquiry(CUSTOMER)  # a message arrived after the sweep read the row
    assert await inquiries.mark_idle_notice_sent(CUSTOMER, older_than_hours=1) is False

    await _idle_for(2)
    assert await inquiries.mark_idle_notice_sent(CUSTOMER, older_than_hours=1) is True
    assert await _flag() is True
    await inquiries.touch_inquiry(CUSTOMER)
    assert await _flag() is False  # the next quiet spell may ping again


async def test_continue_needs_an_outstanding_notice_and_first_tap_wins(db: None) -> None:
    await _open()
    await inquiries.try_claim_inquiry(CUSTOMER, 111)

    early = await inquiries.try_continue_inquiry(CUSTOMER, 222)  # no notice yet: the chat is not idle
    assert (early.success, early.is_open, early.claimed_by) == (False, True, 111)

    await _idle_for(2)
    await inquiries.mark_idle_notice_sent(CUSTOMER, older_than_hours=1)
    first = await inquiries.try_continue_inquiry(CUSTOMER, 222)
    assert (first.success, first.claimed_by) == (True, 222)
    second = await inquiries.try_continue_inquiry(CUSTOMER, 333)
    assert (second.success, second.claimed_by) == (False, 222)

    await inquiries.close_inquiry_if_open(CUSTOMER)
    closed = await inquiries.try_continue_inquiry(CUSTOMER, 111)
    assert (closed.success, closed.is_open) == (False, False)


# ------------------------------------------------ the sweep (idle notice to every admin)


async def _claimed_and_quiet(product_id: int | None = None, owner: int = 111) -> None:
    await _open(product_id)
    await inquiries.try_claim_inquiry(CUSTOMER, owner)
    await _idle_for(2)


async def test_sweep_sends_continue_or_end_buttons_to_every_admin_once(db: None) -> None:
    pid = await _make_product()
    await _claimed_and_quiet(pid)
    bot = _FakeBot()

    assert await inquiry_idle.sweep_idle_inquiries(bot) == 1  # type: ignore[arg-type]
    assert [to for to, _, _ in bot.sent] == ADMINS  # everyone, owner included
    for _, text, kwargs in bot.sent:
        assert "Kurtka" in text and f"id={pid}" in text  # the product, with its id
        assert "Admin111" in text  # who has been handling it
        buttons = [b for row in kwargs["reply_markup"].inline_keyboard for b in row]
        kinds = {b.callback_data.split(":")[0] for b in buttons}
        assert kinds == {client.InquiryContinueCB.__prefix__, client.InquiryCloseCB.__prefix__}
    cards = await inquiries.pop_inquiry_notice_cards(CUSTOMER)
    assert sorted(admin for admin, _ in cards) == ADMINS

    assert await inquiry_idle.sweep_idle_inquiries(bot) == 0  # type: ignore[arg-type]  # never twice per quiet spell
    assert len(bot.sent) == 2
    found = await inquiries.get_open_inquiry(CUSTOMER)
    assert found is not None and found.claimed_by == 111  # and the chat was NOT closed


async def test_sweep_ignores_fresh_and_unclaimed_inquiries(db: None) -> None:
    await _open()
    await _idle_for(2)  # quiet but nobody has taken it: not this job's business
    bot = _FakeBot()
    assert await inquiry_idle.sweep_idle_inquiries(bot) == 0  # type: ignore[arg-type]
    await inquiries.try_claim_inquiry(CUSTOMER, 111)  # fresh again
    assert await inquiry_idle.sweep_idle_inquiries(bot) == 0  # type: ignore[arg-type]
    assert bot.sent == []


async def test_sweep_survives_an_admin_who_never_started_the_bot(db: None) -> None:
    from aiogram.exceptions import TelegramForbiddenError

    await _claimed_and_quiet()

    class _OneBlocked(_FakeBot):
        async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> SimpleNamespace:
            if chat_id == 111:
                raise TelegramForbiddenError(method=GetMe(), message="bot was blocked by the user")
            return await super().send_message(chat_id, text, **kwargs)

    bot = _OneBlocked()
    assert await inquiry_idle.sweep_idle_inquiries(bot) == 1  # type: ignore[arg-type]
    assert [to for to, _, _ in bot.sent] == [222]


def test_scheduler_runs_the_idle_sweep() -> None:
    async def build() -> Any:
        return scheduler.create_scheduler(_FakeBot())  # type: ignore[arg-type]

    sched = asyncio.run(build())
    job = sched.get_job("inquiry_idle")
    assert job is not None and job.func is inquiry_idle.sweep_idle_inquiries


# ------------------------------------------------ "Davom ettirish" (continue)


async def test_continue_hands_the_whole_conversation_to_the_first_admin_only(db: None) -> None:
    pid = await _make_product()
    await _claimed_and_quiet(pid)
    await inquiries.record_inquiry_history(CUSTOMER, CUSTOMER, 50, "customer")
    await inquiries.record_inquiry_history(CUSTOMER, 111, 51, "admin")
    await inquiries.record_inquiry_history(CUSTOMER, CUSTOMER, 52, "customer")
    await inquiries.record_inquiry_history(CUSTOMER, CUSTOMER, 53, "customer")
    bot = _FakeBot()
    await inquiry_idle.sweep_idle_inquiries(bot)  # type: ignore[arg-type]
    bot.sent.clear()
    bot.sent_ids.clear()
    cards = {admin: mid for admin, mid in await inquiries.pop_inquiry_notice_cards(CUSTOMER)}
    await inquiries.replace_inquiry_notice_cards(CUSTOMER, list(cards.items()))  # put them back for the handler

    tap = _callback(222, cards[222])
    await client.on_inquiry_continue(tap, client.InquiryContinueCB(user_id=CUSTOMER), bot)  # type: ignore[arg-type]
    assert _answered(tap)[1] is False

    found = await inquiries.get_open_inquiry(CUSTOMER)
    assert found is not None and found.claimed_by == 222 and await _flag() is False
    # 222 is shown the customer, the product (name + id), then the transcript in order...
    intro = bot.texts_to(222)[0]
    assert "Ali" in intro and "Kurtka" in intro and f"id={pid}" in intro
    assert [m for _, _, m in bot.copied] == [50, 51, 52, 53]
    assert all(to == 222 for to, _, _ in bot.copied)
    labels = [t for t in bot.texts_to(222) if t.endswith(":") and "tarixi" not in t]
    assert len(labels) == 3  # customer, admin, customer: consecutive messages share one label
    # ...and 111 (the previous owner) only sees his card retired.
    assert bot.texts_to(111) == []
    assert [(chat, mid) for chat, mid, _ in bot.edited] == [(111, cards[111])]
    assert "Admin 222" in bot.edited[0][2]

    # From now on the customer's messages go to 222 alone, and he can answer any replayed message.
    bot2 = _FakeBot()
    await client._relay_inquiry_message(_customer_message(70), bot2)  # type: ignore[arg-type]
    assert [to for to, _, _ in bot2.copied] == [222]
    # the card he tapped, the intro, every replayed message and the closing prompt all map to the inquiry
    for message_id in (cards[222], bot.sent_ids[0], *bot.copy_ids, bot.sent_ids[-1]):
        assert await inquiries.find_inquiry_user_by_relay(222, message_id) == CUSTOMER

    # A late tap by the other admin is turned away.
    late = _callback(111, cards[111])
    bot3 = _FakeBot()
    await client.on_inquiry_continue(late, client.InquiryContinueCB(user_id=CUSTOMER), bot3)  # type: ignore[arg-type]
    text, alert = _answered(late)
    assert alert and "Admin222" in (text or "")
    assert bot3.sent == [] and bot3.copied == []
    late.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)


async def test_history_replay_skips_deleted_messages_and_caps_the_length(db: None) -> None:
    await _claimed_and_quiet()
    total = client._INQUIRY_HISTORY_MAX + 5
    for i in range(total):
        await inquiries.record_inquiry_history(CUSTOMER, CUSTOMER, 100 + i, "customer")
    newest = 100 + total - 1
    bot = _FakeBot(missing={newest})  # the newest source message was deleted meanwhile
    await inquiries.mark_idle_notice_sent(CUSTOMER, older_than_hours=1)

    await client.on_inquiry_continue(_callback(222), client.InquiryContinueCB(user_id=CUSTOMER), bot)  # type: ignore[arg-type]
    assert len(bot.copied) == client._INQUIRY_HISTORY_MAX - 1  # capped, minus the one that no longer exists
    assert bot.copied[0][2] == 100 + 5  # the oldest 5 were left out
    assert any(f"{total}" in t for t in bot.texts_to(222))  # and the admin is told it was cut
    assert client._INQUIRY_CONTINUE_READY_TEXT in bot.texts_to(222)[-1]  # the hand-over still completed


async def test_continue_on_a_closed_inquiry_is_refused(db: None) -> None:
    await _claimed_and_quiet()
    await inquiries.mark_idle_notice_sent(CUSTOMER, older_than_hours=1)
    await inquiries.close_inquiry_if_open(CUSTOMER)
    bot = _FakeBot()
    tap = _callback(222)
    await client.on_inquiry_continue(tap, client.InquiryContinueCB(user_id=CUSTOMER), bot)  # type: ignore[arg-type]
    assert _answered(tap) == (client._CHAT_ALREADY_CLOSED_TEXT, True)
    assert bot.sent == [] and bot.copied == []


async def test_stale_continue_card_after_the_chat_woke_up_is_refused(db: None) -> None:
    await _claimed_and_quiet()
    bot = _FakeBot()
    await inquiry_idle.sweep_idle_inquiries(bot)  # type: ignore[arg-type]
    await client._relay_inquiry_message(_customer_message(80), bot)  # type: ignore[arg-type]  # customer wrote again
    edited_chats = sorted(chat for chat, _, _ in bot.edited)
    assert edited_chats == ADMINS  # both cards were retired right away
    assert all(client._INQUIRY_ACTIVE_AGAIN_TEXT == text for _, _, text in bot.edited)

    tap = _callback(222)
    await client.on_inquiry_continue(tap, client.InquiryContinueCB(user_id=CUSTOMER), bot)  # type: ignore[arg-type]
    assert _answered(tap)[1] is True  # nothing to continue: it never went quiet
    found = await inquiries.get_open_inquiry(CUSTOMER)
    assert found is not None and found.claimed_by == 111  # ownership untouched


async def test_owner_replying_after_the_notice_retires_the_cards(db: None) -> None:
    await _claimed_and_quiet()
    await inquiries.record_inquiry_relay(CUSTOMER, 111, 900)
    bot = _FakeBot()
    await inquiry_idle.sweep_idle_inquiries(bot)  # type: ignore[arg-type]
    await client._reply_to_inquiry(_admin_reply(111), bot, 900)  # type: ignore[arg-type]
    assert sorted(chat for chat, _, _ in bot.edited) == ADMINS
    assert bot.copied == [(CUSTOMER, 111, 60)]
    assert await inquiries.pop_inquiry_notice_cards(CUSTOMER) == []


async def test_ending_the_chat_from_the_idle_card_closes_it_for_everyone(db: None) -> None:
    await _claimed_and_quiet()
    bot = _FakeBot()
    await inquiry_idle.sweep_idle_inquiries(bot)  # type: ignore[arg-type]
    bot.sent.clear()
    tap = _callback(222)
    await client.on_inquiry_close(tap, client.InquiryCloseCB(user_id=CUSTOMER), bot)  # type: ignore[arg-type]
    assert await inquiries.get_open_inquiry(CUSTOMER) is None
    assert [to for to, _, _ in bot.sent] == [CUSTOMER]  # the customer is told it ended
    assert sorted(chat for chat, _, _ in bot.edited) == [111]  # the other admin's card is retired

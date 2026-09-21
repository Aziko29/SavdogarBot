"""Customer-facing notes on an inquiry: "admin saw it" on accepting, "admins are busy" / "answer in the morning" while nobody has."""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import GetMe
from aiogram.types import Message
from sqlalchemy import select, update

import inquiry_wait
import scheduler
from config import settings
from db import inquiries
from db.engine import engine
from db.schema import inquiries_t, inquiry_history_t
from handlers import client
from utils import utcnow

CUSTOMER = 7
ADMINS = [111, 222]
_TZ = ZoneInfo("Asia/Tashkent")
DAY = datetime(2026, 5, 4, 14, 0, tzinfo=_TZ)  # outside the default 23:00-08:00 night window
NIGHT = datetime(2026, 5, 4, 3, 0, tzinfo=_TZ)  # inside it


class _Bot:
    """Records what is sent to whom."""

    def __init__(self, blocked: set[int] | None = None) -> None:
        self.sent: list[tuple[int, str]] = []
        self.blocked = blocked or set()
        self._next = 1000

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> SimpleNamespace:
        if chat_id in self.blocked:
            raise TelegramForbiddenError(method=GetMe(), message="bot was blocked by the user")
        self.sent.append((chat_id, text))
        self._next += 1
        return SimpleNamespace(message_id=self._next)

    async def edit_message_text(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def get_chat(self, chat_id: int) -> SimpleNamespace:
        return SimpleNamespace(first_name=f"Admin{chat_id}", last_name=None, username=None)

    def texts_to(self, chat_id: int) -> list[str]:
        return [text for to, text in self.sent if to == chat_id]


def _callback(admin_id: int) -> SimpleNamespace:
    msg = MagicMock(spec=Message)
    msg.message_id = 500
    msg.edit_reply_markup = AsyncMock()
    return SimpleNamespace(
        from_user=SimpleNamespace(id=admin_id, full_name=f"Admin {admin_id}"), message=msg, answer=AsyncMock()
    )


@pytest.fixture(autouse=True)
def _defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "admin_ids", lambda: ADMINS)
    monkeypatch.setattr(inquiry_wait, "local_now", lambda: DAY)


async def _open() -> None:
    await inquiries.open_inquiry(CUSTOMER, None, "Ali")


async def _customer_wrote(minutes_ago: float, user_id: int = CUSTOMER) -> None:
    """A customer message recorded `minutes_ago` minutes back."""
    await inquiries.record_inquiry_history(user_id, user_id, 50, "customer")
    async with engine.begin() as conn:
        await conn.execute(
            update(inquiry_history_t)
            .where(inquiry_history_t.c.user_id == user_id)
            .values(created_at=utcnow() - timedelta(minutes=minutes_ago))
        )


async def _flag() -> bool:
    async with engine.connect() as conn:
        return bool((await conn.execute(select(inquiries_t.c.wait_notice_sent))).scalar_one())


# ------------------------------------------------ "admin saw your question" on accepting


async def test_accepting_by_button_tells_the_customer_his_question_was_seen(db: None) -> None:
    await _open()
    await _customer_wrote(1)
    bot = _Bot()
    tap = _callback(111)
    await client.on_inquiry_claim(tap, client.InquiryClaimCB(user_id=CUSTOMER), bot)  # type: ignore[arg-type]
    assert bot.texts_to(CUSTOMER) == [client._INQUIRY_SEEN_USER_TEXT]


async def test_accepting_before_the_customer_wrote_asks_for_the_question(db: None) -> None:
    await _open()
    bot = _Bot()
    await client.on_inquiry_claim(_callback(111), client.InquiryClaimCB(user_id=CUSTOMER), bot)  # type: ignore[arg-type]
    assert bot.texts_to(CUSTOMER) == [client._INQUIRY_JOINED_USER_TEXT]


async def test_losing_the_race_does_not_message_the_customer_again(db: None) -> None:
    await _open()
    await _customer_wrote(1)
    bot = _Bot()
    await client.on_inquiry_claim(_callback(111), client.InquiryClaimCB(user_id=CUSTOMER), bot)  # type: ignore[arg-type]
    await client.on_inquiry_claim(_callback(222), client.InquiryClaimCB(user_id=CUSTOMER), bot)  # type: ignore[arg-type]
    assert len(bot.texts_to(CUSTOMER)) == 1


async def test_customer_who_blocked_the_bot_does_not_break_accepting(db: None) -> None:
    await _open()
    bot = _Bot(blocked={CUSTOMER})
    tap = _callback(111)
    await client.on_inquiry_claim(tap, client.InquiryClaimCB(user_id=CUSTOMER), bot)  # type: ignore[arg-type]
    found = await inquiries.get_open_inquiry(CUSTOMER)
    assert found is not None and found.claimed_by == 111
    assert tap.answer.await_args.args[0] == client._INQUIRY_CLAIMED_TEXT


# ------------------------------------------------ which inquiries are "waiting"


async def test_wait_is_counted_from_the_customers_first_message(db: None) -> None:
    await _open()
    assert await inquiries.list_unanswered_inquiries(7) == []  # nothing written yet: nobody is waiting
    await _customer_wrote(3)
    assert await inquiries.list_unanswered_inquiries(7) == []  # written, but not long enough ago
    async with engine.begin() as conn:
        await conn.execute(update(inquiry_history_t).values(created_at=utcnow() - timedelta(minutes=8)))
    waiting = await inquiries.list_unanswered_inquiries(7)
    assert [(i.user_id, i.claimed_by) for i in waiting] == [(CUSTOMER, None)]


async def test_claimed_or_closed_inquiries_are_not_waiting(db: None) -> None:
    await _open()
    await _customer_wrote(10)
    await inquiries.try_claim_inquiry(CUSTOMER, 111)
    assert await inquiries.list_unanswered_inquiries(7) == []
    await inquiries.close_inquiry_if_open(CUSTOMER)
    assert await inquiries.list_unanswered_inquiries(7) == []


async def test_mark_wait_notice_is_once_only_and_keeps_the_expiry_clock(db: None) -> None:
    await _open()
    async with engine.connect() as conn:
        before = (await conn.execute(select(inquiries_t.c.updated_at))).scalar_one()
    assert await inquiries.mark_wait_notice_sent(CUSTOMER) is True
    assert await inquiries.mark_wait_notice_sent(CUSTOMER) is False
    async with engine.connect() as conn:
        after = (await conn.execute(select(inquiries_t.c.updated_at))).scalar_one()
    assert after == before  # the note is not chat activity
    await inquiries.touch_inquiry(CUSTOMER)
    assert await _flag() is True  # customer activity does not re-arm it either


async def test_mark_wait_notice_fails_once_an_admin_has_the_chat(db: None) -> None:
    await _open()
    await inquiries.try_claim_inquiry(CUSTOMER, 111)
    assert await inquiries.mark_wait_notice_sent(CUSTOMER) is False


async def test_reopening_rearms_the_note(db: None) -> None:
    await _open()
    await inquiries.mark_wait_notice_sent(CUSTOMER)
    await _open()
    assert await _flag() is False


# ------------------------------------------------ the sweep


async def test_sweep_says_admins_are_busy_once_by_day(db: None) -> None:
    await _open()
    await _customer_wrote(10)
    bot = _Bot()
    assert await inquiry_wait.sweep_unanswered_inquiries(bot) == 1  # type: ignore[arg-type]
    assert bot.sent == [(CUSTOMER, inquiry_wait.BUSY_TEXT)]
    assert await inquiry_wait.sweep_unanswered_inquiries(bot) == 0  # type: ignore[arg-type]
    assert len(bot.sent) == 1


async def test_sweep_says_answer_in_the_morning_at_night(db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inquiry_wait, "local_now", lambda: NIGHT)
    await _open()
    await _customer_wrote(10)
    bot = _Bot()
    assert await inquiry_wait.sweep_unanswered_inquiries(bot) == 1  # type: ignore[arg-type]
    assert bot.sent == [(CUSTOMER, inquiry_wait.NIGHT_TEXT)]


async def test_sweep_leaves_fresh_and_accepted_chats_alone(db: None) -> None:
    await _open()
    await _customer_wrote(2)  # fresh
    bot = _Bot()
    assert await inquiry_wait.sweep_unanswered_inquiries(bot) == 0  # type: ignore[arg-type]
    async with engine.begin() as conn:
        await conn.execute(update(inquiry_history_t).values(created_at=utcnow() - timedelta(minutes=30)))
    await inquiries.try_claim_inquiry(CUSTOMER, 111)  # an admin took it before the sweep ran
    assert await inquiry_wait.sweep_unanswered_inquiries(bot) == 0  # type: ignore[arg-type]
    assert bot.sent == []


async def test_sweep_survives_a_customer_who_blocked_the_bot(db: None) -> None:
    await _open()
    await _customer_wrote(10)
    bot = _Bot(blocked={CUSTOMER})
    assert await inquiry_wait.sweep_unanswered_inquiries(bot) == 0  # type: ignore[arg-type]
    assert await _flag() is True  # not retried every minute


async def test_sweep_is_off_when_the_wait_is_zero(db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inquiry_wait, "settings", SimpleNamespace(inquiry_wait_min=0))
    await _open()
    await _customer_wrote(600)
    bot = _Bot()
    assert await inquiry_wait.sweep_unanswered_inquiries(bot) == 0  # type: ignore[arg-type]
    assert bot.sent == []


def test_scheduler_runs_the_wait_sweep() -> None:
    import asyncio

    async def build() -> Any:
        return scheduler.create_scheduler(_Bot())  # type: ignore[arg-type]

    sched = asyncio.run(build())
    job = sched.get_job("inquiry_wait")
    assert settings.inquiry_wait_min > 0
    assert job is not None and job.func is inquiry_wait.sweep_unanswered_inquiries

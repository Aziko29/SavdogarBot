"""Admins screen (head admin): every admin is shown with his name, not just a bare ID."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetChat

from config import settings
from db import admins
from handlers import admin_manage

HEAD = settings.head_admin_id


class _Bot:
    """get_chat answers from a dict; ids listed in `hidden` fail like an admin who never started the bot."""

    def __init__(self, people: dict[int, tuple[str | None, str | None, str | None]], hidden: set[int] | None = None) -> None:
        self.people = people  # id -> (first_name, last_name, username)
        self.hidden = hidden or set()
        self.asked: list[int] = []

    async def get_chat(self, chat_id: int) -> SimpleNamespace:
        self.asked.append(chat_id)
        if chat_id in self.hidden:
            raise TelegramBadRequest(method=GetChat(chat_id=chat_id), message="chat not found")
        first, last, username = self.people[chat_id]
        return SimpleNamespace(first_name=first, last_name=last, username=username)


def _buttons(markup: Any) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


async def test_list_shows_each_admins_name_username_and_id(db: None) -> None:
    await admins.add_admin(555, added_by=HEAD)
    await admins.add_admin(777, added_by=HEAD)
    bot = _Bot({HEAD: ("Boss", None, "bossman"), 555: ("Ali", "Valiyev", "ali_v"), 777: ("Zarina", None, None)})

    text, markup = await admin_manage._render_list(bot)  # type: ignore[arg-type]

    assert f'<a href="tg://user?id={HEAD}">Boss</a> (@bossman) \u00b7 <code>{HEAD}</code>' in text
    assert '1. <a href="tg://user?id=555">Ali Valiyev</a> (@ali_v) \u00b7 <code>555</code>' in text
    assert '2. <a href="tg://user?id=777">Zarina</a> \u00b7 <code>777</code>' in text
    # the remove buttons carry the same numbers and names
    assert _buttons(markup)[1:3] == ["\U0001f5d1 1. Ali Valiyev", "\U0001f5d1 2. Zarina"]


async def test_an_admin_whose_name_cannot_be_read_is_still_listed_by_id(db: None) -> None:
    await admins.add_admin(555, added_by=HEAD)
    await admins.add_admin(777, added_by=HEAD)
    bot = _Bot({HEAD: ("Boss", None, None), 777: ("Zarina", None, None)}, hidden={555})

    text, markup = await admin_manage._render_list(bot)  # type: ignore[arg-type]

    assert "1. <code>555</code> (ismi noma'lum)" in text  # never started the bot: ID only, screen still works
    assert "Zarina" in text
    assert "\U0001f5d1 1. 555" in _buttons(markup)


async def test_only_a_username_is_used_when_there_is_no_name(db: None) -> None:
    await admins.add_admin(555, added_by=HEAD)
    bot = _Bot({HEAD: ("Boss", None, None), 555: (None, None, "just_user")})
    text, _ = await admin_manage._render_list(bot)  # type: ignore[arg-type]
    assert '<a href="tg://user?id=555">@just_user</a> \u00b7 <code>555</code>' in text


async def test_names_are_html_escaped_and_long_button_labels_are_cut(db: None) -> None:
    await admins.add_admin(555, added_by=HEAD)
    bot = _Bot({HEAD: ("Boss", None, None), 555: ("<b>Ali</b> & Sons Trading Company", None, None)})
    text, markup = await admin_manage._render_list(bot)  # type: ignore[arg-type]
    assert "&lt;b&gt;Ali&lt;/b&gt; &amp; Sons Trading Company" in text  # cannot inject markup into the message
    label = _buttons(markup)[1]
    assert label.endswith("\u2026") and len(label) < 30


async def test_empty_list_and_a_slow_lookup_do_not_break_the_screen(db: None, monkeypatch: Any) -> None:
    class _Slow(_Bot):
        async def get_chat(self, chat_id: int) -> SimpleNamespace:
            await asyncio.sleep(1)
            return await super().get_chat(chat_id)

    monkeypatch.setattr(admin_manage, "_NAME_LOOKUP_TIMEOUT_SEC", 0.01)
    text, _ = await admin_manage._render_list(_Slow({HEAD: ("Boss", None, None)}))  # type: ignore[arg-type]
    assert f"<code>{HEAD}</code> (ismi noma'lum)" in text
    assert "Boshqa adminlar hali yo'q." in text


async def test_the_delete_confirmation_names_the_admin(db: None, monkeypatch: Any) -> None:
    await admins.add_admin(555, added_by=HEAD)
    bot = _Bot({555: ("Ali", "Valiyev", None)})
    sent: list[str] = []

    async def fake_edit(_cb: Any, text: str, markup: Any) -> None:
        sent.append(text)

    async def answer(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(admin_manage, "_edit", fake_edit)
    cb = SimpleNamespace(answer=answer)
    await admin_manage.on_ask_delete(cb, admin_manage.AdminActCB(action="ask_del", user_id=555), bot)  # type: ignore[arg-type]
    assert "Ali Valiyev" in sent[0] and "<code>555</code>" in sent[0]

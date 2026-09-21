"""The "/" command menu (customers and admins) and the /yordam help."""
from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import bot_commands
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import DeleteMyCommands
from aiogram.types import BotCommandScopeAllPrivateChats, BotCommandScopeChat, Message

from db import admins
from handlers import admin, client

HEAD = 111  # BOSH_ADMIN_ID from conftest
OTHER_ADMIN = 222222222
CUSTOMER = 5555


def _bot() -> MagicMock:
    bot = MagicMock()
    bot.set_my_commands = AsyncMock()
    bot.delete_my_commands = AsyncMock()
    return bot


def _no_chat_error() -> TelegramBadRequest:
    return TelegramBadRequest(method=DeleteMyCommands(), message="Bad Request: chat not found")


def _message(user_id: int) -> MagicMock:
    msg = MagicMock(spec=Message)
    msg.from_user = SimpleNamespace(id=user_id, full_name="Ali V")
    msg.answer = AsyncMock()
    return msg


def setup_function() -> None:
    bot_commands._admin_menu_done.clear()


# ------------------------------------------------ the command lists


def test_command_names_are_valid_for_telegram() -> None:
    for command in (*bot_commands.CUSTOMER_COMMANDS, *bot_commands.ADMIN_COMMANDS):
        assert re.fullmatch(r"[a-z0-9_]{1,32}", command.command), command.command
        assert 1 <= len(command.description) <= 256


def test_customer_menu_has_the_requested_commands() -> None:
    names = [c.command for c in bot_commands.CUSTOMER_COMMANDS]
    assert {"buyurtmalarim", "cancel", "yordam"} <= set(names)
    assert len(names) == len(set(names))


def test_admin_menu_offers_admin_panel() -> None:
    names = [c.command for c in bot_commands.ADMIN_COMMANDS]
    assert "admin" in names and "yordam" in names


# ------------------------------------------------ publishing


async def test_setup_publishes_customer_menu_and_every_admin_menu(db: None) -> None:
    await admins.add_admin(OTHER_ADMIN, added_by=HEAD)
    bot = _bot()

    await bot_commands.setup_bot_commands(bot)

    scopes = [call.kwargs["scope"] for call in bot.set_my_commands.await_args_list]
    assert any(isinstance(s, BotCommandScopeAllPrivateChats) for s in scopes)
    admin_chats = {s.chat_id for s in scopes if isinstance(s, BotCommandScopeChat)}
    assert admin_chats == {HEAD, OTHER_ADMIN}
    first = bot.set_my_commands.await_args_list[0]
    assert [c.command for c in first.args[0]] == [c.command for c in bot_commands.CUSTOMER_COMMANDS]


async def test_setup_survives_telegram_errors(db: None) -> None:
    bot = _bot()
    bot.set_my_commands.side_effect = _no_chat_error()

    await bot_commands.setup_bot_commands(bot)  # must not raise

    assert bot_commands._admin_menu_done == set()  # nothing was published, so the next call retries


async def test_admin_without_a_chat_is_retried_later() -> None:
    bot = _bot()
    bot.set_my_commands.side_effect = _no_chat_error()
    assert await bot_commands.ensure_admin_commands(bot, OTHER_ADMIN) is False

    bot.set_my_commands.side_effect = None  # he pressed Start meanwhile
    assert await bot_commands.ensure_admin_commands(bot, OTHER_ADMIN) is True
    assert bot.set_my_commands.await_count == 2


async def test_admin_menu_is_published_only_once_per_run() -> None:
    bot = _bot()
    assert await bot_commands.ensure_admin_commands(bot, OTHER_ADMIN) is True
    assert await bot_commands.ensure_admin_commands(bot, OTHER_ADMIN) is True
    assert bot.set_my_commands.await_count == 1


async def test_dropping_an_admin_menu_restores_the_customer_menu() -> None:
    bot = _bot()
    await bot_commands.ensure_admin_commands(bot, OTHER_ADMIN)

    await bot_commands.drop_admin_commands(bot, OTHER_ADMIN)

    scope = bot.delete_my_commands.await_args.kwargs["scope"]
    assert isinstance(scope, BotCommandScopeChat) and scope.chat_id == OTHER_ADMIN
    # published again if he is ever re-added
    await bot_commands.ensure_admin_commands(bot, OTHER_ADMIN)
    assert bot.set_my_commands.await_count == 2


async def test_dropping_an_admin_menu_ignores_telegram_errors() -> None:
    bot = _bot()
    bot.delete_my_commands.side_effect = _no_chat_error()
    await bot_commands.drop_admin_commands(bot, OTHER_ADMIN)  # must not raise


# ------------------------------------------------ /yordam and /start


async def test_help_for_a_customer_lists_his_commands(db: None) -> None:
    msg = _message(CUSTOMER)

    await client.cmd_help(msg)

    text = msg.answer.await_args.args[0]
    for command in ("/buyurtmalarim", "/malumotlarim", "/cancel", "/yordam"):
        assert command in text
    assert "/admin" not in text
    assert msg.answer.await_args.kwargs["parse_mode"] == "HTML"


async def test_help_for_an_admin_points_to_the_panel(db: None) -> None:
    msg = _message(HEAD)

    await client.cmd_help(msg)

    text = msg.answer.await_args.args[0]
    assert "/admin" in text and "/buyurtmalarim" not in text


async def test_every_command_in_the_help_is_in_the_customer_menu() -> None:
    documented = set(re.findall(r"^/([a-z_]+)", client._HELP_TEXT, flags=re.M))
    assert documented == {c.command for c in bot_commands.CUSTOMER_COMMANDS}


async def test_start_greeting_mentions_help() -> None:
    assert "/yordam" in client._PLAIN_START_TEXT


async def test_admin_start_publishes_his_menu(db: None) -> None:
    bot = _bot()
    msg = _message(HEAD)

    await client.cmd_start_plain(msg, bot)

    scope = bot.set_my_commands.await_args.kwargs["scope"]
    assert isinstance(scope, BotCommandScopeChat) and scope.chat_id == HEAD
    msg.answer.assert_awaited_once()


async def test_customer_start_does_not_touch_the_menu(db: None) -> None:
    bot = _bot()
    msg = _message(CUSTOMER)

    await client.cmd_start_plain(msg, bot)

    bot.set_my_commands.assert_not_awaited()
    msg.answer.assert_awaited_once()


def test_help_and_admin_commands_are_registered_on_the_routers() -> None:
    """Guards against a rename: the menu must point at commands that some handler answers."""
    handled = set()
    for router in (client.router, admin.router):
        for handler in router.message.handlers:
            for flt in handler.filters:
                commands = getattr(flt.callback, "commands", None)
                if commands:
                    handled.update(str(c) for c in commands)
    menu = {c.command for c in (*bot_commands.CUSTOMER_COMMANDS, *bot_commands.ADMIN_COMMANDS)}
    assert menu <= handled


# ------------------------------------------------ admin add / remove keep the menu in sync


class _AdminBot:
    """Just enough of a Bot for the admin screens: menu calls, messages and name lookups."""

    def __init__(self) -> None:
        self.set_my_commands = AsyncMock()
        self.delete_my_commands = AsyncMock()
        self.send_message = AsyncMock()
        self.get_chat = AsyncMock(return_value=SimpleNamespace(first_name="Ali", last_name=None, username=None))


async def test_adding_an_admin_gives_him_the_admin_menu(db: None) -> None:
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from handlers import admin_manage

    bot = _AdminBot()
    msg = _message(HEAD)
    msg.forward_origin = None
    msg.text = str(OTHER_ADMIN)
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=HEAD, user_id=HEAD))

    await admin_manage.on_id_input(msg, state, bot)  # type: ignore[arg-type]

    assert admins.is_admin(OTHER_ADMIN)
    scope = bot.set_my_commands.await_args.kwargs["scope"]
    assert isinstance(scope, BotCommandScopeChat) and scope.chat_id == OTHER_ADMIN


async def test_removing_an_admin_takes_the_admin_menu_away(db: None, monkeypatch: object) -> None:
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from handlers import admin_manage

    await admins.add_admin(OTHER_ADMIN, added_by=HEAD)
    bot = _AdminBot()
    monkeypatch.setattr(admin_manage, "_edit", AsyncMock())  # type: ignore[attr-defined]
    callback = SimpleNamespace(answer=AsyncMock(), from_user=SimpleNamespace(id=HEAD))
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=HEAD, user_id=HEAD))

    await admin_manage.on_delete(callback, admin_manage.AdminActCB(action="del", user_id=OTHER_ADMIN), state, bot)  # type: ignore[arg-type]

    assert not admins.is_admin(OTHER_ADMIN)
    scope = bot.delete_my_commands.await_args.kwargs["scope"]
    assert isinstance(scope, BotCommandScopeChat) and scope.chat_id == OTHER_ADMIN

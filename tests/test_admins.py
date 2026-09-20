"""Admin tiers: head admin (.env) + regular admins (DB, RAM-cached) and the related config rules."""
from __future__ import annotations

import pytest

from config import ConfigError, load_settings, settings
from db import admins

_BASE_ENV = {
    "BOT_TOKEN": "123456:TEST_TOKEN",
    "BOSH_ADMIN_ID": "111",
    "GEMINI_API_KEYS": "k1",
    "GEMINI_MODELS": "m1",
}


# ---------------------------------------------------------------- config


def test_config_reads_head_admin_owner_and_lock_port() -> None:
    s = load_settings({**_BASE_ENV, "OWNER_ID": "222", "LOCK_PORT": "47401"})
    assert (s.head_admin_id, s.owner_id, s.lock_port) == (111, 222, 47401)


def test_config_defaults_owner_unset_and_lock_port_47400() -> None:
    s = load_settings(_BASE_ENV)
    assert (s.owner_id, s.lock_port) == (0, 47400)


def test_config_log_send_hours_default_custom_and_range() -> None:
    assert load_settings(_BASE_ENV).log_send_hours == 24
    assert load_settings({**_BASE_ENV, "LOG_SEND_HOURS": "0"}).log_send_hours == 0
    assert load_settings({**_BASE_ENV, "LOG_SEND_HOURS": "6"}).log_send_hours == 6
    with pytest.raises(ConfigError):
        load_settings({**_BASE_ENV, "LOG_SEND_HOURS": "169"})


def test_config_requires_head_admin() -> None:
    env = {k: v for k, v in _BASE_ENV.items() if k != "BOSH_ADMIN_ID"}
    with pytest.raises(ConfigError) as exc:
        load_settings(env)
    assert any("BOSH_ADMIN_ID is required" in e for e in exc.value.errors)


def test_config_legacy_admin_ids_gives_a_clear_hint() -> None:
    env = {k: v for k, v in _BASE_ENV.items() if k != "BOSH_ADMIN_ID"}
    with pytest.raises(ConfigError) as exc:
        load_settings({**env, "ADMIN_IDS": "1,2"})
    assert any("ADMIN_IDS is no longer used" in e for e in exc.value.errors)


@pytest.mark.parametrize("bad", ["abc", "0", "-5"])
def test_config_rejects_bad_user_ids(bad: str) -> None:
    with pytest.raises(ConfigError):
        load_settings({**_BASE_ENV, "BOSH_ADMIN_ID": bad})


# ---------------------------------------------------------------- roles


async def test_head_admin_is_admin_and_head(db: None) -> None:
    assert admins.is_head_admin(settings.head_admin_id)
    assert admins.is_admin(settings.head_admin_id)
    assert not admins.is_admin(None)
    assert not admins.is_head_admin(None)


async def test_regular_admin_is_admin_but_not_head(db: None) -> None:
    assert not admins.is_admin(555)
    assert await admins.add_admin(555, added_by=settings.head_admin_id) is True
    assert admins.is_admin(555)
    assert not admins.is_head_admin(555)


async def test_add_twice_reports_false_and_keeps_one_row(db: None) -> None:
    assert await admins.add_admin(555, added_by=settings.head_admin_id) is True
    assert await admins.add_admin(555, added_by=settings.head_admin_id) is False
    assert [e.user_id for e in await admins.list_admins()] == [555]


async def test_remove_admin_revokes_access(db: None) -> None:
    await admins.add_admin(555, added_by=settings.head_admin_id)
    assert await admins.remove_admin(555) is True
    assert not admins.is_admin(555)
    assert await admins.remove_admin(555) is False


async def test_head_admin_can_never_be_added_or_removed(db: None) -> None:
    with pytest.raises(ValueError):
        await admins.add_admin(settings.head_admin_id, added_by=settings.head_admin_id)
    with pytest.raises(ValueError):
        await admins.remove_admin(settings.head_admin_id)
    assert admins.is_head_admin(settings.head_admin_id)


@pytest.mark.parametrize("bad", [0, -1, 2**60])
async def test_invalid_ids_are_rejected(db: None, bad: int) -> None:
    with pytest.raises(ValueError):
        await admins.add_admin(bad, added_by=settings.head_admin_id)


async def test_admin_ids_lists_head_first_then_sorted(db: None) -> None:
    await admins.add_admin(900, added_by=settings.head_admin_id)
    await admins.add_admin(300, added_by=settings.head_admin_id)
    assert admins.admin_ids() == [settings.head_admin_id, 300, 900]


async def test_load_admins_restores_the_cache_from_the_db(db: None) -> None:
    await admins.add_admin(555, added_by=settings.head_admin_id)
    admins._extra = frozenset()
    assert not admins.is_admin(555)
    await admins.load_admins()
    assert admins.is_admin(555)


# ---------------------------------------------------------------- routing regression


async def test_not_admin_filter_lets_admin_messages_through_to_the_admin_router(db: None) -> None:
    from types import SimpleNamespace

    from handlers.filters import AdminFilter, HeadAdminFilter, NotAdminFilter

    await admins.add_admin(555, added_by=settings.head_admin_id)

    def event(user_id: int | None) -> SimpleNamespace:
        return SimpleNamespace(from_user=None if user_id is None else SimpleNamespace(id=user_id))

    head, regular, stranger = event(settings.head_admin_id), event(555), event(999)
    assert await AdminFilter()(head) and await AdminFilter()(regular)
    assert not await AdminFilter()(stranger)
    assert await HeadAdminFilter()(head) and not await HeadAdminFilter()(regular)
    assert not await NotAdminFilter()(head) and not await NotAdminFilter()(regular)
    assert await NotAdminFilter()(stranger)


def test_customer_catch_all_handler_is_not_applied_to_admins() -> None:
    """The client router runs first; its catch-all must not match admins or /admin would be swallowed."""
    from handlers import client
    from handlers.filters import NotAdminFilter

    handler = next(h for h in client.router.message.handlers if h.callback is client.on_customer_chat_message)
    assert any(isinstance(getattr(f, "callback", None), NotAdminFilter) for f in handler.filters)

from __future__ import annotations

import pytest
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateTable

from config import normalize_database_url
from db.schema import metadata


def test_sqlite_url_is_left_alone() -> None:
    assert normalize_database_url("sqlite+aiosqlite:///data/bot.db") == "sqlite+aiosqlite:///data/bot.db"


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "postgresql+asyncpg"])
def test_postgres_schemes_become_asyncpg(scheme: str) -> None:
    url = normalize_database_url(f"{scheme}://user:pw@db.example.com:5432/app")
    assert url == "postgresql+asyncpg://user:pw@db.example.com:5432/app"


def test_neon_style_options_are_translated() -> None:
    raw = "postgresql://u:p%40ss@ep-x-pooler.neon.tech/neondb?sslmode=require&channel_binding=require"
    assert normalize_database_url(raw) == "postgresql+asyncpg://u:p%40ss@ep-x-pooler.neon.tech/neondb?ssl=require"


def test_an_explicit_ssl_option_wins_over_sslmode() -> None:
    url = normalize_database_url("postgresql://u:p@h/d?ssl=verify-full&sslmode=require")
    assert url.endswith("?ssl=verify-full")


@pytest.mark.parametrize(
    "bad",
    ["", "abc", "mysql://u:p@h/d", "postgresql://u:p@/d", "postgresql://u:p@h", "http://example.com"],
)
def test_unusable_urls_are_rejected_without_echoing_secrets(bad: str) -> None:
    with pytest.raises(ValueError) as info:
        normalize_database_url(bad)
    assert "p@" not in str(info.value)


def test_schema_ddl_is_valid_for_postgres_boolean_defaults() -> None:
    """PostgreSQL refuses 'DEFAULT 0' on a boolean column; the DDL must use false/true there."""
    products = str(CreateTable(metadata.tables["products"]).compile(dialect=postgresql.dialect()))
    assert "needs_review BOOLEAN DEFAULT false NOT NULL" in products
    assert "category_locked BOOLEAN DEFAULT false NOT NULL" in products
    settings = str(CreateTable(metadata.tables["settings"]).compile(dialect=postgresql.dialect()))
    assert "autopost_enabled BOOLEAN DEFAULT true NOT NULL" in settings


def test_schema_ddl_for_sqlite_keeps_numeric_boolean_defaults() -> None:
    products = str(CreateTable(metadata.tables["products"]).compile(dialect=sqlite.dialect()))
    assert "needs_review BOOLEAN DEFAULT 0 NOT NULL" in products

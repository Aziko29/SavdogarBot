"""Async engine (SQLite or PostgreSQL), per-connection setup, schema bootstrap and versioned migrations."""
from __future__ import annotations

import asyncio
import functools
import logging
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

from sqlalchemy import func, insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import URL, Connection, make_url
from sqlalchemy.event import listens_for
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from config import BASE_DIR, settings
from db.schema import (
    admins_t,
    chats_t,
    inquiries_t,
    inquiry_history_t,
    inquiry_notice_t,
    inquiry_relay_t,
    metadata,
    pending_chats_t,
    schema_version_t,
    settings_t,
)

logger = logging.getLogger("db.engine")

P = ParamSpec("P")
R = TypeVar("R")

try:  # asyncpg is only needed (and only imported) for PostgreSQL
    import asyncpg
except ImportError:  # pragma: no cover - SQLite-only installs
    asyncpg = None  # type: ignore[assignment]

_USES_POSTGRES = asyncpg is not None and settings.database_url.startswith("postgresql")

# asyncpg raises these while opening a connection, before SQLAlchemy can wrap them (PostgreSQL only).
_CONNECT_ERRORS: tuple[type[BaseException], ...] = (
    (OSError, asyncio.TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError) if _USES_POSTGRES else ()
)
# The subset that is worth retrying at start-up (server waking up, network blip, too many clients).
_TRANSIENT_CONNECT_ERRORS: tuple[type[BaseException], ...] = (
    (
        OSError,
        asyncio.TimeoutError,
        asyncpg.CannotConnectNowError,
        asyncpg.TooManyConnectionsError,
        asyncpg.ConnectionDoesNotExistError,
    )
    if _USES_POSTGRES
    else ()
)
_START_ATTEMPTS = 5


def logged_db(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Log SQLAlchemy errors raised by an async DB function, then re-raise them."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await fn(*args, **kwargs)
        except SQLAlchemyError:
            logging.getLogger(fn.__module__).exception("Database error in %s", fn.__name__)
            raise
        except _CONNECT_ERRORS as exc:
            # Present a lost connection like any other database error, so the callers'
            # `except SQLAlchemyError` blocks keep working on PostgreSQL.
            logging.getLogger(fn.__module__).exception("Database connection error in %s", fn.__name__)
            raise OperationalError("database connection", {}, exc) from exc

    return wrapper

Migration = tuple[int, str, Callable[[Connection], None]]

def _add_orders_chat_open(conn: Connection) -> None:
    """Add orders.chat_open for DBs created before the live-chat relay feature (idempotent)."""
    if conn.dialect.name == "postgresql":
        conn.execute(text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS chat_open BOOLEAN NOT NULL DEFAULT FALSE"))
        return
    try:
        conn.execute(text("ALTER TABLE orders ADD COLUMN chat_open BOOLEAN NOT NULL DEFAULT 0"))
    except OperationalError as exc:
        if "duplicate column" not in str(exc).lower():
            raise


# (column, SQLite type, PostgreSQL type) of the order details added with the real order flow.
_ORDER_DETAIL_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("quantity", "INTEGER NOT NULL DEFAULT 1", "INTEGER NOT NULL DEFAULT 1"),
    ("phone", "TEXT NOT NULL DEFAULT ''", "TEXT NOT NULL DEFAULT ''"),
    ("address", "TEXT NOT NULL DEFAULT ''", "TEXT NOT NULL DEFAULT ''"),
    ("outcome", "VARCHAR(16) NOT NULL DEFAULT ''", "VARCHAR(16) NOT NULL DEFAULT ''"),
    ("remind_count", "INTEGER NOT NULL DEFAULT 0", "INTEGER NOT NULL DEFAULT 0"),
    ("reminded_at", "DATETIME", "TIMESTAMP"),
)


def _add_order_details(conn: Connection) -> None:
    """Add quantity/phone/address/outcome/reminder columns to orders (idempotent)."""
    postgres = conn.dialect.name == "postgresql"
    for name, sqlite_type, pg_type in _ORDER_DETAIL_COLUMNS:
        if postgres:
            conn.execute(text(f"ALTER TABLE orders ADD COLUMN IF NOT EXISTS {name} {pg_type}"))
            continue
        try:
            conn.execute(text(f"ALTER TABLE orders ADD COLUMN {name} {sqlite_type}"))
        except OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_orders_status_outcome ON orders (status, outcome)"))


def _create_admins_table(conn: Connection) -> None:
    """Create the admins table for DBs created before the admin-management feature (idempotent)."""
    admins_t.create(bind=conn, checkfirst=True)


def _create_chats_table(conn: Connection) -> None:
    """Create the chats table (source/target channels managed from the bot) for older DBs (idempotent)."""
    chats_t.create(bind=conn, checkfirst=True)


def _create_pending_chats_table(conn: Connection) -> None:
    """Create the pending_chats table (unregistered chats the bot will leave) for older DBs (idempotent)."""
    pending_chats_t.create(bind=conn, checkfirst=True)


def _create_inquiry_tables(conn: Connection) -> None:
    """Create the inquiries + inquiry_relay tables (customer <-> admin chat without an order) for older DBs."""
    inquiries_t.create(bind=conn, checkfirst=True)
    inquiry_relay_t.create(bind=conn, checkfirst=True)


def _add_inquiry_claim_columns(conn: Connection) -> None:
    """Add inquiries.claimed_by/idle_notice_sent for the single-admin claim feature (idempotent)."""
    if conn.dialect.name == "postgresql":
        conn.execute(text("ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS claimed_by BIGINT"))
        conn.execute(
            text("ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS idle_notice_sent BOOLEAN NOT NULL DEFAULT FALSE")
        )
        return
    for ddl in (
        "ALTER TABLE inquiries ADD COLUMN claimed_by BIGINT",
        "ALTER TABLE inquiries ADD COLUMN idle_notice_sent BOOLEAN NOT NULL DEFAULT 0",
    ):
        try:
            conn.execute(text(ddl))
        except OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise


def _create_inquiry_claim_tables(conn: Connection) -> None:
    """Create inquiry_notice + inquiry_history (claim cards and conversation transcript) for older DBs."""
    inquiry_notice_t.create(bind=conn, checkfirst=True)
    inquiry_history_t.create(bind=conn, checkfirst=True)


# Ordered (version, description, fn) entries; version 1 is the baseline created by create_all().
# Migration fns must be idempotent (CREATE ... IF NOT EXISTS) because create_all() runs first.
_MIGRATIONS: tuple[Migration, ...] = (
    (2, "add orders.chat_open for the live-chat relay feature", _add_orders_chat_open),
    (3, "add the admins table for bot-managed admins", _create_admins_table),
    (4, "add the chats table: source/target channels and groups managed from the bot", _create_chats_table),
    (5, "add the pending_chats table: unregistered chats the bot leaves after a grace period", _create_pending_chats_table),
    (6, "add order details (quantity, phone, address), outcome and reminder columns to orders", _add_order_details),
    (7, "add the inquiries tables: customer <-> admin chat that is not tied to an order", _create_inquiry_tables),
    (8, "add inquiries.claimed_by/idle_notice_sent for the single-admin inquiry claim feature", _add_inquiry_claim_columns),
    (9, "add inquiry_notice + inquiry_history for the claim cards and conversation replay", _create_inquiry_claim_tables),
)

BASELINE_VERSION = 1
LATEST_VERSION: int = max([BASELINE_VERSION, *(v for v, _, _ in _MIGRATIONS)])


def _is_file_db(url: URL) -> bool:
    """True when the URL points to a real SQLite file (not memory / URI mode)."""
    if url.get_backend_name() != "sqlite":
        return False
    database = url.database
    return bool(database) and database != ":memory:" and not database.startswith("file:")


def _resolve_url() -> URL:
    """Parse DATABASE_URL and anchor a relative SQLite path to the project directory."""
    url = make_url(settings.database_url)
    if _is_file_db(url):
        path = Path(str(url.database))
        if not path.is_absolute():
            path = BASE_DIR / path
        url = url.set(database=str(path))
    return url


_DB_URL: URL = _resolve_url()
IS_POSTGRES: bool = _DB_URL.get_backend_name() == "postgresql"

# One advisory-lock key for schema setup: two instances starting together (for example during a
# zero-downtime deploy) must not run CREATE TABLE / migrations at the same time.
_SCHEMA_LOCK_KEY = 7_340_190_001


def _make_engine() -> AsyncEngine:
    """Create the async engine for the configured backend."""
    if IS_POSTGRES:
        return create_async_engine(
            _DB_URL,
            pool_size=3,
            max_overflow=2,
            pool_pre_ping=True,  # free hosts (Neon, Supabase) drop idle connections
            pool_recycle=300,
            connect_args={
                "timeout": 30,
                "command_timeout": 60,
                # Connection poolers in transaction mode (Neon "-pooler", Supabase :6543) cannot
                # share prepared statements between connections, so do not cache them.
                "statement_cache_size": 0,
                "prepared_statement_cache_size": 0,
                "prepared_statement_name_func": lambda: f"__asyncpg_{uuid.uuid4()}__",
            },
        )
    if _is_file_db(_DB_URL):
        return create_async_engine(_DB_URL, connect_args={"timeout": 30})
    return create_async_engine(_DB_URL, connect_args={"timeout": 30}, poolclass=StaticPool)


engine: AsyncEngine = _make_engine()


def dialect_insert(table: Any) -> Any:
    """INSERT construct of the active backend (supports on_conflict_do_nothing / on_conflict_do_update)."""
    return pg_insert(table) if IS_POSTGRES else sqlite_insert(table)


if not IS_POSTGRES:

    @listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
        """Apply WAL and safety PRAGMAs on every new SQLite connection."""
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


def _ensure_data_dir() -> None:
    """Create the directory that holds the SQLite file, if needed."""
    if not _is_file_db(_DB_URL):
        return
    parent = Path(str(_DB_URL.database)).parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.exception("Cannot create database directory %s", parent)
        raise


async def _seed_settings(conn: AsyncConnection) -> None:
    """Insert the default settings row (id=1) if it does not exist."""
    await conn.execute(
        dialect_insert(settings_t).values(id=1).on_conflict_do_nothing(index_elements=["id"])
    )


async def _apply_migrations(conn: AsyncConnection) -> None:
    """Record the schema version on a fresh DB, or run pending migrations in order."""
    current = (await conn.execute(select(func.max(schema_version_t.c.version)))).scalar()
    if current is None:
        await conn.execute(insert(schema_version_t).values(version=LATEST_VERSION))
        logger.info("Fresh database initialised at schema v%s", LATEST_VERSION)
        return
    if current > LATEST_VERSION:
        raise RuntimeError(
            f"Database schema v{current} is newer than this code (v{LATEST_VERSION}); "
            "update the bot before starting it"
        )
    for version, description, fn in sorted(_MIGRATIONS, key=lambda item: item[0]):
        if version <= current:
            continue
        logger.info("Applying migration v%s: %s", version, description)
        await conn.run_sync(fn)
        await conn.execute(insert(schema_version_t).values(version=version))
        current = version
    logger.info("Database schema is at v%s", current)


async def _init_db_once() -> None:
    """One attempt to create tables, seed the settings and run pending migrations."""
    async with engine.begin() as conn:
        if IS_POSTGRES:
            await conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _SCHEMA_LOCK_KEY})
        await conn.run_sync(metadata.create_all)
        await _seed_settings(conn)
        await _apply_migrations(conn)


async def init_db() -> None:
    """Create tables, seed default settings and run pending migrations.

    On PostgreSQL a database that is still waking up or a short network failure is retried a few
    times; a wrong password or database name fails at once with a readable message.
    """
    _ensure_data_dir()
    for attempt in range(1, _START_ATTEMPTS + 1):
        try:
            await _init_db_once()
            return
        except SQLAlchemyError:
            logger.exception("Database initialisation failed")
            raise
        except _CONNECT_ERRORS as exc:
            transient = isinstance(exc, _TRANSIENT_CONNECT_ERRORS)
            if transient and attempt < _START_ATTEMPTS:
                delay = 2 ** attempt
                logger.warning(
                    "Database not reachable (%s: %s); retry %d/%d in %ds",
                    type(exc).__name__, exc, attempt, _START_ATTEMPTS - 1, delay,
                )
                await asyncio.sleep(delay)
                continue
            raise RuntimeError(
                f"Cannot connect to the database ({type(exc).__name__}: {exc}). Check DATABASE_URL: host, port, user, password and database name."
            ) from exc


async def dispose_db() -> None:
    """Checkpoint the SQLite WAL file (if any) and close all pooled connections."""
    if _is_file_db(_DB_URL):
        try:
            async with engine.connect() as conn:
                await conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
        except SQLAlchemyError:
            logger.warning("WAL checkpoint on shutdown failed", exc_info=True)
    await engine.dispose()

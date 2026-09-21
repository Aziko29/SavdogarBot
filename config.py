"""Application configuration loaded from environment variables (.env)."""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

BASE_DIR: Path = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BUILTIN_BASE_URLS: dict[str, str] = {
    "groq": "https://api.groq.com/openai/v1",
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "qwen": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
}

_MAX_GEMINI_GROUPS = 20
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_NAME_RE = re.compile(r"^[a-z0-9_-]+$")
_DEFAULT_VISION_HINTS = "vision,llama-4,scout,maverick,gemini,gpt-4o,-vl,qwen2.5-vl"


_SQLITE_PREFIX = "sqlite+aiosqlite:"
_PG_DRIVERS = frozenset({"postgres", "postgresql", "postgresql+asyncpg"})
_DB_URL_HELP = (
    "DATABASE_URL must start with 'sqlite+aiosqlite:' or 'postgresql://' "
    "(the 'postgres://' and 'postgresql+asyncpg://' forms are accepted too)"
)


def normalize_database_url(raw: str) -> str:
    """Return a SQLAlchemy async URL for the given DATABASE_URL; raises ValueError if it is unusable.

    Provider connection strings (Neon, Supabase, Render...) start with ``postgres://`` or
    ``postgresql://`` and carry libpq options that the asyncpg driver does not know. They are
    rewritten here: the driver becomes ``postgresql+asyncpg``, ``sslmode=X`` becomes ``ssl=X`` and
    ``channel_binding`` is dropped. SQLite URLs are returned unchanged.
    """
    value = raw.strip()
    if value.startswith(_SQLITE_PREFIX):
        return value
    try:
        url = make_url(value)
    except ArgumentError as exc:
        raise ValueError(_DB_URL_HELP) from exc
    if url.drivername not in _PG_DRIVERS:
        raise ValueError(_DB_URL_HELP)
    if not url.host:
        raise ValueError("DATABASE_URL has no host name (expected postgresql://user:password@host/dbname)")
    if not url.database:
        raise ValueError("DATABASE_URL has no database name (expected postgresql://user:password@host/dbname)")
    query = dict(url.query)
    sslmode = query.pop("sslmode", None)
    query.pop("channel_binding", None)
    if sslmode and "ssl" not in query:
        query["ssl"] = sslmode if isinstance(sslmode, str) else sslmode[-1]
    return url.set(drivername="postgresql+asyncpg", query=query).render_as_string(hide_password=False)


class ConfigError(Exception):
    """Raised when the environment configuration is invalid."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class FallbackProvider:
    name: str
    base_url: str
    keys: list[str]
    models: list[str]


@dataclass(frozen=True)
class Settings:
    bot_token: str
    owner_id: int  # server owner (optional, 0 = unset): receives the logs, NO admin-panel rights
    log_send_hours: int  # how often new log lines are sent to the owner (0 = never)
    head_admin_id: int  # main admin (the client): full panel + manages the other admins
    lock_port: int  # local TCP port of the single-instance lock (unique per bot on one server)
    database_url: str
    tz: str
    log_level: str
    gemini_groups: list[list[str]]
    gemini_models: list[str]
    fallbacks: list[FallbackProvider]
    ai_workers: int
    queue_maxsize: int
    ai_attempt_timeout_sec: float
    ai_overall_budget_sec: float
    key_exhausted_minutes: int
    min_repost_gap_hours: float
    no_repeat_last_n: int
    photo_max_side: int
    album_wait_sec: float
    foreign_chat_grace_hours: float  # unregistered chats are left after this many hours
    vision_model_hints: tuple[str, ...]
    order_remind_after_min: int = 15  # a pending order is first reminded about after this many minutes
    order_remind_every_min: int = 15  # minimum gap between two reminders about the same order
    order_remind_max: int = 3  # reminders per order (0 = reminders off)
    inquiry_idle_hours: float = 1.0  # a claimed inquiry with no activity this long gets a "continue?" ping


def _is_placeholder(value: str) -> bool:
    return "<" in value or ">" in value


def _split_csv(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


class _Reader:
    """Reads typed values from a mapping and collects validation errors."""

    def __init__(self, env: Mapping[str, str]) -> None:
        self.env = env
        self.errors: list[str] = []

    def text(self, name: str, default: str = "") -> str:
        return (self.env.get(name) or "").strip() or default

    def required(self, name: str) -> str:
        value = self.text(name)
        if not value:
            self.errors.append(f"{name} is required")
            return ""
        if _is_placeholder(value):
            self.errors.append(f"{name} still contains a placeholder; fill in a real value")
            return ""
        return value

    def csv(self, name: str) -> list[str]:
        items = _dedupe(_split_csv(self.text(name)))
        if any(_is_placeholder(item) for item in items):
            self.errors.append(f"{name} still contains a placeholder; fill in real values")
            return []
        return items

    def integer(self, name: str, default: int, lo: int, hi: int) -> int:
        raw = self.text(name)
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            self.errors.append(f"{name} must be an integer, got {raw!r}")
            return default
        if not lo <= value <= hi:
            self.errors.append(f"{name} must be between {lo} and {hi}, got {value}")
            return default
        return value

    def number(self, name: str, default: float, lo: float, hi: float) -> float:
        raw = self.text(name)
        if not raw:
            return default
        try:
            value = float(raw)
        except ValueError:
            self.errors.append(f"{name} must be a number, got {raw!r}")
            return default
        if not lo <= value <= hi:
            self.errors.append(f"{name} must be between {lo} and {hi}, got {value}")
            return default
        return value

    def user_id(self, name: str, *, required: bool) -> int:
        """A positive Telegram user ID; 0 when an optional value is not set."""
        raw = self.required(name) if required else self.text(name)
        if not raw:
            return 0
        try:
            value = int(raw)
        except ValueError:
            self.errors.append(f"{name} must be an integer user ID, got {raw!r}")
            return 0
        if value <= 0:
            self.errors.append(f"{name} must be a positive user ID, got {value}")
            return 0
        return value


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build and validate Settings; raises ConfigError listing every problem."""
    r = _Reader(os.environ if env is None else env)

    bot_token = r.required("BOT_TOKEN")
    if bot_token and ":" not in bot_token:
        r.errors.append("BOT_TOKEN does not look like a Telegram bot token (expected '<id>:<secret>')")

    head_admin_id = r.user_id("BOSH_ADMIN_ID", required=True)
    owner_id = r.user_id("OWNER_ID", required=False)
    if not r.text("BOSH_ADMIN_ID") and r.text("ADMIN_IDS"):
        r.errors.append(
            "ADMIN_IDS is no longer used: put the main admin's ID into BOSH_ADMIN_ID "
            "(the other admins are added inside the bot by the main admin)"
        )
    lock_port = r.integer("LOCK_PORT", 47400, 1024, 65535)
    log_send_hours = r.integer("LOG_SEND_HOURS", 24, 0, 168)


    database_url = r.text("DATABASE_URL", "sqlite+aiosqlite:///data/bot.db")
    try:
        database_url = normalize_database_url(database_url)
    except ValueError as exc:
        r.errors.append(str(exc))

    tz = r.text("TZ", "Asia/Tashkent")
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        r.errors.append(f"TZ {tz!r} is not a valid IANA time zone (on Windows run: pip install tzdata)")

    log_level = r.text("LOG_LEVEL", "INFO").upper()
    if log_level not in _LOG_LEVELS:
        r.errors.append(f"LOG_LEVEL must be one of {sorted(_LOG_LEVELS)}, got {log_level!r}")
        log_level = "INFO"

    gemini_groups: list[list[str]] = []
    for suffix in [""] + [f"_{i}" for i in range(2, _MAX_GEMINI_GROUPS + 1)]:
        keys = r.csv(f"GEMINI_API_KEYS{suffix}")
        if keys:
            gemini_groups.append(keys)
    gemini_models = r.csv("GEMINI_MODELS")
    if gemini_groups and not gemini_models:
        r.errors.append("GEMINI_MODELS is required when GEMINI_API_KEYS is set")

    fallbacks: list[FallbackProvider] = []
    seen_names: set[str] = set()
    for raw_name in _split_csv(r.text("FALLBACK_PROVIDERS")):
        name = raw_name.lower()
        if name in seen_names:
            continue
        seen_names.add(name)
        if not _NAME_RE.match(name):
            r.errors.append(f"FALLBACK_PROVIDERS contains an invalid name: {raw_name!r}")
            continue
        prefix = re.sub(r"[^A-Z0-9]", "_", name.upper())
        keys = r.csv(f"{prefix}_API_KEYS")
        models = r.csv(f"{prefix}_MODELS")
        base_url = (r.text(f"{prefix}_BASE_URL") or BUILTIN_BASE_URLS.get(name, "")).rstrip("/")
        if not keys:
            r.errors.append(f"FALLBACK_PROVIDERS lists '{name}' but {prefix}_API_KEYS is empty")
        if not models:
            r.errors.append(f"FALLBACK_PROVIDERS lists '{name}' but {prefix}_MODELS is empty")
        if not base_url:
            r.errors.append(f"{prefix}_BASE_URL is required for unknown provider '{name}'")
        elif not base_url.startswith(("http://", "https://")):
            r.errors.append(f"{prefix}_BASE_URL must start with http:// or https://")
            base_url = ""
        if keys and models and base_url:
            fallbacks.append(FallbackProvider(name=name, base_url=base_url, keys=keys, models=models))

    if not gemini_groups and not fallbacks:
        r.errors.append(
            "No AI provider configured: set GEMINI_API_KEYS (+ GEMINI_MODELS) and/or FALLBACK_PROVIDERS"
        )

    ai_workers = r.integer("AI_WORKERS", 2, 1, 16)
    queue_maxsize = r.integer("QUEUE_MAXSIZE", 200, 1, 10_000)
    attempt_timeout = r.number("AI_ATTEMPT_TIMEOUT_SEC", 20.0, 1.0, 300.0)
    overall_budget = r.number("AI_OVERALL_BUDGET_SEC", 60.0, 1.0, 600.0)
    if overall_budget < attempt_timeout:
        r.errors.append("AI_OVERALL_BUDGET_SEC must be >= AI_ATTEMPT_TIMEOUT_SEC")
    key_exhausted_minutes = r.integer("KEY_EXHAUSTED_MINUTES", 15, 1, 1440)
    min_repost_gap_hours = r.number("MIN_REPOST_GAP_HOURS", 6.0, 0.0, 720.0)
    no_repeat_last_n = r.integer("NO_REPEAT_LAST_N", 3, 0, 100)
    photo_max_side = r.integer("PHOTO_MAX_SIDE", 1600, 256, 4096)
    album_wait_sec = r.number("ALBUM_WAIT_SEC", 3.0, 0.0, 30.0)
    foreign_chat_grace_hours = r.number("FOREIGN_CHAT_GRACE_HOURS", 6.0, 0.1, 168.0)
    order_remind_after_min = r.integer("ORDER_REMIND_AFTER_MIN", 15, 1, 1440)
    order_remind_every_min = r.integer("ORDER_REMIND_EVERY_MIN", 15, 1, 1440)
    order_remind_max = r.integer("ORDER_REMIND_MAX", 3, 0, 20)
    inquiry_idle_hours = r.number("INQUIRY_IDLE_HOURS", 1.0, 0.05, 24.0)
    vision_hints = tuple(
        hint.lower() for hint in _split_csv(r.text("VISION_MODEL_HINTS", _DEFAULT_VISION_HINTS))
    )

    if r.errors:
        raise ConfigError(r.errors)

    return Settings(
        bot_token=bot_token,
        owner_id=owner_id,
        log_send_hours=log_send_hours,
        head_admin_id=head_admin_id,
        lock_port=lock_port,
        database_url=database_url,
        tz=tz,
        log_level=log_level,
        gemini_groups=gemini_groups,
        gemini_models=gemini_models,
        fallbacks=fallbacks,
        ai_workers=ai_workers,
        queue_maxsize=queue_maxsize,
        ai_attempt_timeout_sec=attempt_timeout,
        ai_overall_budget_sec=overall_budget,
        key_exhausted_minutes=key_exhausted_minutes,
        min_repost_gap_hours=min_repost_gap_hours,
        no_repeat_last_n=no_repeat_last_n,
        photo_max_side=photo_max_side,
        album_wait_sec=album_wait_sec,
        foreign_chat_grace_hours=foreign_chat_grace_hours,
        vision_model_hints=vision_hints,
        order_remind_after_min=order_remind_after_min,
        order_remind_every_min=order_remind_every_min,
        order_remind_max=order_remind_max,
        inquiry_idle_hours=inquiry_idle_hours,
    )


def _load_or_exit() -> Settings:
    try:
        return load_settings()
    except ConfigError as exc:
        raise SystemExit(
            "Configuration error(s) in .env:\n" + "\n".join(f"  - {m}" for m in exc.errors)
        ) from None


settings: Settings = _load_or_exit()

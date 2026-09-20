"""Cascade router: chooses (provider, key, model) attempts, handles errors, cooldowns and sticky success."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.exc import SQLAlchemyError

from ai.errors import (
    AllProvidersFailed,
    BlockedContentError,
    InvalidKeyError,
    InvalidResponseError,
    ModelNotSupportedError,
    ProviderError,
    QuotaExceededError,
    RequestTooLargeError,
    TransientError,
)
from ai.providers import GEMINI_PROVIDER, GenerateRequest, Provider, make_openai_provider
from config import settings
from db import keystate
from utils import fire_and_forget, key_id, local_now, mask_key, utcnow

logger = logging.getLogger("ai.router")

_PAIR_COOLDOWN_SEC = 60.0
_BACKOFFS: tuple[float, ...] = (1.0, 2.0, 4.0)
_MAX_INVALID_RETRIES = 3
_MAX_BLOCKED = 2
_RETRY_TEMPERATURE = 0.2
_TIMEOUT_GRACE_SEC = 1.0  # the provider's own timeout fires first; wait_for is the safety net
_LA_TZ = ZoneInfo("America/Los_Angeles")
_DAILY_RE = re.compile(r"per[\s_-]?day|daily|\brpd\b|\btpd\b", re.IGNORECASE)
_MAX_ERRORS_KEPT = 12

_ALERT_ALL_FAILED = "ai_all_failed"

AlertHook = Callable[[str, str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _Entry:
    """One attempt candidate: a (provider, key, model) triple in a priority group."""

    provider: Provider
    group: int
    api_key: str = field(repr=False)
    kid: str
    model: str
    is_fallback: bool
    vision_ok: bool

    @property
    def label(self) -> str:
        """Log-safe label; the raw key is never included."""
        return f"{self.provider.name}/{mask_key(self.api_key)}/{self.model}"


@dataclass(slots=True)
class _KeyState:
    """In-memory mirror of a key's persisted state."""

    exhausted_until: datetime | None = None
    invalid: bool = False
    usage: int = 0
    usage_date: str = ""


@dataclass(slots=True)
class _KeyInfo:
    """Static facts about a configured key (label only, never the raw key)."""

    label: str
    provider: str
    group: int
    models: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _Run:
    """Bookkeeping for one generate_json() call."""

    deadline: float
    blocked_count: int = 0
    last_blocked: BlockedContentError | None = None
    saw_transient: bool = False
    budget_exceeded: bool = False
    failures: int = 0
    attempts: int = 0
    errors: list[str] = field(default_factory=list)

    def remaining(self) -> float:
        """Seconds left in the overall budget."""
        return self.deadline - time.monotonic()

    def fail(self, entry: _Entry, reason: str) -> None:
        """Record a non-block failure of an attempt."""
        self.failures += 1
        self.errors.append(f"{entry.label}: {reason[:120]}")
        del self.errors[:-_MAX_ERRORS_KEPT]


_entries: list[_Entry] = []
_key_info: dict[str, _KeyInfo] = {}
_state: dict[str, _KeyState] = {}
_bad_pairs: set[tuple[str, str]] = set()
_cooldowns: dict[tuple[str, str], float] = {}
_sticky: dict[str, tuple[str, str, str] | None] = {"text": None, "media": None}
_alert_hook: AlertHook | None = None
_initialized = False
_init_lock = asyncio.Lock()


# ------------------------------------------------------------------ public API


def set_alert_hook(fn: AlertHook) -> None:
    """Register fn(text, throttle_key), awaited to notify admins about router problems."""
    global _alert_hook
    _alert_hook = fn


async def init_router() -> None:
    """Build the attempt list from settings and load persisted key state from the DB."""
    global _initialized
    entries: list[_Entry] = []
    infos: dict[str, _KeyInfo] = {}
    seen: set[tuple[str, str, str]] = set()

    def add(provider: Provider, group: int, api_key: str, model: str, is_fallback: bool) -> None:
        kid = key_id(api_key)
        ident = (provider.name, kid, model)
        if ident in seen:
            return
        seen.add(ident)
        info = infos.get(kid)
        if info is None:
            info = infos[kid] = _KeyInfo(f"{provider.name}/{mask_key(api_key)}", provider.name, group)
        if model not in info.models:
            info.models.append(model)
        vision_ok = not is_fallback or any(h in model.lower() for h in settings.vision_model_hints)
        entries.append(_Entry(provider, group, api_key, kid, model, is_fallback, vision_ok))

    for group, keys in enumerate(settings.gemini_groups):
        for api_key in keys:
            for model in settings.gemini_models:
                add(GEMINI_PROVIDER, group, api_key, model, False)
    base = len(settings.gemini_groups)
    for index, fallback in enumerate(settings.fallbacks):
        provider = make_openai_provider(fallback.name, fallback.base_url)
        for api_key in fallback.keys:
            for model in fallback.models:
                add(provider, base + index, api_key, model, True)

    persisted: dict[str, dict[str, Any]] = {}
    bad_pairs: set[tuple[str, str]] = set()
    try:
        persisted = await keystate.load_key_states()
        bad_pairs = await keystate.load_bad_pairs()
    except SQLAlchemyError:
        logger.warning("Could not load key state from the database; starting with a clean slate")

    today = local_now().date().isoformat()
    state: dict[str, _KeyState] = {}
    for kid in infos:
        saved = persisted.get(kid)
        if saved is None:
            state[kid] = _KeyState()
        else:
            state[kid] = _KeyState(
                exhausted_until=saved["exhausted_until"],
                invalid=bool(saved["invalid"]),
                usage=int(saved["usage_today"]),
                usage_date=today,
            )

    _entries[:] = entries
    _key_info.clear()
    _key_info.update(infos)
    _state.clear()
    _state.update(state)
    _bad_pairs.clear()
    _bad_pairs.update(bad_pairs)
    _cooldowns.clear()
    _sticky.update({"text": None, "media": None})
    _initialized = True
    logger.info(
        "Router ready: %d attempt(s) across %d key(s); %d bad pair(s), %d invalid key(s)",
        len(entries),
        len(infos),
        len(bad_pairs),
        sum(1 for s in state.values() if s.invalid),
    )


async def generate_json(req: GenerateRequest) -> str:
    """Return raw JSON text from the best available provider.

    Raises BlockedContentError when the content itself is blocked, AllProvidersFailed otherwise.
    """
    await _ensure_init()
    kind = "media" if req.images else "text"
    run = _Run(deadline=time.monotonic() + settings.ai_overall_budget_sec)

    text = await _run_stage(run, req, kind, lambda ign: _usable(bool(req.images), ign), sticky=True)
    if text is None and req.images and not run.budget_exceeded:
        text_req = _strip_images(req)
        text = await _run_stage(
            run, text_req, kind, lambda ign: _vision_skipped(ign), sticky=False
        )
        if text is not None:
            logger.warning("Served a media request by text-only degradation (images dropped)")
    if text is not None:
        return text

    if run.blocked_count and run.failures == 0 and run.last_blocked is not None:
        raise run.last_blocked
    summary = "; ".join(run.errors) or "no usable provider/key/model"
    _alert(
        f"All AI providers failed after {run.attempts} attempt(s). Last errors: {summary[:600]}",
        _ALERT_ALL_FAILED,
    )
    raise AllProvidersFailed(f"{run.attempts} attempt(s) failed: {summary}")


def router_status() -> dict[str, Any]:
    """Snapshot of router state for admin views (labels only, never raw keys)."""
    now = utcnow()
    today = local_now().date().isoformat()
    now_m = time.monotonic()
    keys: list[dict[str, Any]] = []
    for kid, info in _key_info.items():
        st = _state.get(kid, _KeyState())
        exhausted = st.exhausted_until is not None and st.exhausted_until > now
        keys.append(
            {
                "label": info.label,
                "provider": info.provider,
                "group": info.group + 1,
                "models": list(info.models),
                "exhausted": exhausted,
                "exhausted_until": st.exhausted_until.isoformat() if exhausted and st.exhausted_until else None,
                "invalid": st.invalid,
                "usage_today": st.usage if st.usage_date == today else 0,
            }
        )
    sticky: dict[str, str | None] = {}
    for kind, value in _sticky.items():
        if value is None:
            sticky[kind] = None
        else:
            kid, _provider, model = value
            info = _key_info.get(kid)
            sticky[kind] = f"{info.label if info else '?'}/{model}"
    return {
        "initialized": _initialized,
        "keys": keys,
        "bad_pairs": len(_bad_pairs),
        "cooling_pairs": sum(1 for until in _cooldowns.values() if until > now_m),
        "sticky": sticky,
    }


# ---------------------------------------------------------------- candidate lists


async def _ensure_init() -> None:
    """Initialise the router lazily if init_router() was not called explicitly."""
    if _initialized:
        return
    async with _init_lock:
        if not _initialized:
            await init_router()


def _is_available(entry: _Entry, ignore_exhausted: bool, now: datetime, now_m: float) -> bool:
    """True if the entry may be attempted right now."""
    st = _state.get(entry.kid)
    if st is None or st.invalid:
        return False
    if (entry.kid, entry.model) in _bad_pairs:
        return False
    if ignore_exhausted:
        return True
    if st.exhausted_until is not None and st.exhausted_until > now:
        return False
    return _cooldowns.get((entry.kid, entry.model), 0.0) <= now_m


def _usable(images: bool, ignore_exhausted: bool) -> list[_Entry]:
    """Available entries in priority order; with images, fallbacks need a vision-capable model."""
    now, now_m = utcnow(), time.monotonic()
    return [
        e
        for e in _entries
        if (e.vision_ok or not images) and _is_available(e, ignore_exhausted, now, now_m)
    ]


def _vision_skipped(ignore_exhausted: bool) -> list[_Entry]:
    """Available fallback entries that were skipped for lacking vision (text-only degradation)."""
    now, now_m = utcnow(), time.monotonic()
    return [
        e
        for e in _entries
        if e.is_fallback and not e.vision_ok and _is_available(e, ignore_exhausted, now, now_m)
    ]


def _apply_sticky(entries: list[_Entry], kind: str) -> list[_Entry]:
    """Move the last successful entry first, but only within the highest-priority available group."""
    sticky = _sticky.get(kind)
    if sticky is None or not entries:
        return entries
    top_group = entries[0].group
    for index, entry in enumerate(entries):
        if entry.group != top_group:
            break
        if (entry.kid, entry.provider.name, entry.model) == sticky:
            return entries if index == 0 else [entry, *entries[:index], *entries[index + 1 :]]
    return entries


def _strip_images(req: GenerateRequest) -> GenerateRequest:
    """Text-only copy of a request (including its lighter alternative)."""
    alt = dataclasses.replace(req.alt, images=[]) if req.alt is not None else None
    return dataclasses.replace(req, images=[], alt=alt)


# --------------------------------------------------------------------- execution


async def _run_stage(
    run: _Run,
    req: GenerateRequest,
    kind: str,
    entries_fn: Callable[[bool], list[_Entry]],
    *,
    sticky: bool,
) -> str | None:
    """Walk the attempt list (with backoff rounds); returns JSON text or None."""
    ignore_mode = False
    stage_attempts = 0
    round_no = 0
    while True:
        entries = entries_fn(ignore_mode)
        if not entries and not ignore_mode and stage_attempts == 0:
            entries = entries_fn(True)
            if entries:
                ignore_mode = True
                logger.warning("All AI keys look exhausted; trying every usable pair once anyway")
        if not entries:
            return None
        if sticky:
            entries = _apply_sticky(entries, kind)

        run.saw_transient = False
        for entry in entries:
            if run.remaining() <= 0:
                run.budget_exceeded = True
                return None
            if not _is_available(entry, ignore_mode, utcnow(), time.monotonic()):
                continue  # state changed during this pass (invalid key, daily quota, bad pair, cooldown)
            stage_attempts += 1
            run.attempts += 1
            text = await _try_entry(run, entry, req)
            if text is not None:
                if sticky:
                    _sticky[kind] = (entry.kid, entry.provider.name, entry.model)
                return text
            if run.blocked_count >= _MAX_BLOCKED and run.last_blocked is not None:
                raise run.last_blocked

        if ignore_mode or not run.saw_transient or round_no >= len(_BACKOFFS):
            return None
        base = _BACKOFFS[round_no]
        delay = base + random.uniform(0.0, base * 0.5)
        if run.remaining() <= delay + 1.0:
            run.budget_exceeded = True
            return None
        logger.info("AI pass finished with transient errors; backing off %.1fs", delay)
        await asyncio.sleep(delay)
        round_no += 1


def _is_valid_json(text: str) -> bool:
    """True if text parses as a JSON object or array."""
    try:
        return isinstance(json.loads(text), (dict, list))
    except (ValueError, TypeError):
        return False


async def _try_entry(run: _Run, entry: _Entry, req: GenerateRequest) -> str | None:
    """Attempt one (key, model) pair with per-error handling; None means 'move on'."""
    attempt_req = req
    invalid_fails = 0
    too_large_used = False
    while True:
        remaining = run.remaining()
        if remaining <= 0:
            run.budget_exceeded = True
            return None
        timeout = min(settings.ai_attempt_timeout_sec, remaining)
        call_req = dataclasses.replace(attempt_req, timeout_sec=timeout)
        started = time.monotonic()
        bad: str
        try:
            text = await asyncio.wait_for(
                entry.provider.generate(entry.api_key, entry.model, call_req),
                timeout=timeout + _TIMEOUT_GRACE_SEC,
            )
        except QuotaExceededError as exc:
            logger.warning("Quota hit on %s: %s", entry.label, exc)
            _on_quota(entry, exc)
            run.fail(entry, f"QuotaExceededError {exc}")
            return None
        except ModelNotSupportedError as exc:
            logger.warning("Model not supported on %s: %s", entry.label, exc)
            _on_bad_pair(entry)
            run.fail(entry, f"ModelNotSupportedError {exc}")
            return None
        except InvalidKeyError as exc:
            logger.error("Invalid key on %s: %s", entry.label, exc)
            _on_invalid_key(entry)
            run.fail(entry, f"InvalidKeyError {exc}")
            return None
        except RequestTooLargeError as exc:
            if not too_large_used and attempt_req.alt is not None:
                too_large_used = True
                attempt_req = attempt_req.alt
                logger.warning("Request too large on %s; retrying with the lighter version", entry.label)
                continue
            logger.warning("Request too large on %s: %s", entry.label, exc)
            run.fail(entry, f"RequestTooLargeError {exc}")
            return None
        except BlockedContentError as exc:
            logger.warning("Content blocked on %s: %s", entry.label, exc)
            run.blocked_count += 1
            run.last_blocked = exc
            return None
        except (TransientError, TimeoutError) as exc:
            logger.warning("Transient failure on %s: %r", entry.label, exc)
            run.saw_transient = True
            run.fail(entry, f"{type(exc).__name__} {exc}")
            return None
        except InvalidResponseError as exc:
            _bump_usage(entry)
            bad = f"InvalidResponseError {exc}"
        except ProviderError as exc:
            logger.warning("Provider error on %s: %r", entry.label, exc)
            run.fail(entry, f"{type(exc).__name__} {exc}")
            return None
        except Exception as exc:  # the router must never crash on an unexpected provider bug
            logger.exception("Unexpected error on %s", entry.label)
            run.fail(entry, f"unexpected {type(exc).__name__}")
            return None
        else:
            _bump_usage(entry)
            if _is_valid_json(text):
                _on_success(entry)
                logger.info("AI ok via %s in %.1fs", entry.label, time.monotonic() - started)
                return text
            bad = "response is not valid JSON"

        invalid_fails += 1
        logger.warning("Bad response from %s (%d/%d): %s", entry.label, invalid_fails, _MAX_INVALID_RETRIES + 1, bad[:200])
        if invalid_fails > _MAX_INVALID_RETRIES:
            run.fail(entry, bad)
            return None
        attempt_req = dataclasses.replace(attempt_req, temperature=_RETRY_TEMPERATURE)


# ------------------------------------------------------------------ state changes


def _next_la_midnight() -> datetime:
    """Next midnight in America/Los_Angeles as aware UTC (Gemini's daily quota reset)."""
    now_la = datetime.now(_LA_TZ)
    nxt = (now_la + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return nxt.astimezone(timezone.utc)


def _set_exhausted(entry: _Entry, until: datetime, reason: str) -> None:
    """Mark a key exhausted (never shortening an existing longer exhaustion) and persist it."""
    st = _state[entry.kid]
    if st.exhausted_until is not None and st.exhausted_until >= until:
        return
    st.exhausted_until = until
    logger.warning("Key %s exhausted until %s (%s)", _key_info[entry.kid].label, until.isoformat(), reason)
    fire_and_forget(keystate.mark_key_exhausted(entry.kid, until, reason))


def _on_quota(entry: _Entry, exc: QuotaExceededError) -> None:
    """Cool down the pair; escalate to key exhaustion on daily quota or when every model is cooling."""
    now_m = time.monotonic()
    _cooldowns[(entry.kid, entry.model)] = now_m + _PAIR_COOLDOWN_SEC
    if _DAILY_RE.search(str(exc)):
        _set_exhausted(entry, _next_la_midnight(), "daily quota")
        return
    models = _key_info[entry.kid].models
    if all(
        (entry.kid, m) in _bad_pairs or _cooldowns.get((entry.kid, m), 0.0) > now_m for m in models
    ):
        until = utcnow() + timedelta(minutes=settings.key_exhausted_minutes)
        _set_exhausted(entry, until, "all models rate-limited")


def _on_bad_pair(entry: _Entry) -> None:
    """Persistently skip a (key, model) pair the provider does not support."""
    pair = (entry.kid, entry.model)
    if pair in _bad_pairs:
        return
    _bad_pairs.add(pair)
    fire_and_forget(keystate.add_bad_pair(entry.kid, entry.model))


def _on_invalid_key(entry: _Entry) -> None:
    """Disable a rejected key, persist it and alert the admins once."""
    st = _state[entry.kid]
    if st.invalid:
        return
    st.invalid = True
    fire_and_forget(keystate.mark_key_invalid(entry.kid))
    _alert(
        f"AI key {_key_info[entry.kid].label} was rejected as invalid and has been disabled. "
        "Replace it in .env and restart the bot.",
        f"ai_invalid_key:{entry.kid}",
    )


def _bump_usage(entry: _Entry) -> None:
    """Count one answered request for the key (RAM + DB)."""
    st = _state[entry.kid]
    today = local_now().date().isoformat()
    if st.usage_date != today:
        st.usage = 0
        st.usage_date = today
    st.usage += 1
    fire_and_forget(keystate.incr_usage(entry.kid))


def _on_success(entry: _Entry) -> None:
    """Clear cooldown/exhaustion flags of a pair that just worked."""
    _cooldowns.pop((entry.kid, entry.model), None)
    st = _state[entry.kid]
    now = utcnow()
    if st.exhausted_until is not None and st.exhausted_until > now:
        st.exhausted_until = now
        fire_and_forget(keystate.mark_key_exhausted(entry.kid, now, "recovered"))


def _alert(text: str, throttle_key: str) -> None:
    """Send an admin alert through the registered hook without blocking the caller."""
    hook = _alert_hook
    if hook is None:
        logger.warning("No alert hook registered; alert dropped: %s", text)
        return
    fire_and_forget(_call_hook(hook, text, throttle_key))


async def _call_hook(hook: AlertHook, text: str, throttle_key: str) -> None:
    """Run the alert hook, logging (not raising) its failures."""
    try:
        await hook(text, throttle_key)
    except Exception:  # an alerting problem must never affect AI generation
        logger.exception("Alert hook failed")

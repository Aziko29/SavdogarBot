"""Re-polish system: after an admin edits a product's data, the AI rewrites the post once more.

Flow (started by handlers/admin_products.py right after the admin's value is saved):
  1. the admin's value is already stored and the caption rebuilt, so the data is safe if anything fails;
  2. the AI rewrites only the sales pitch and hashtags around the admin-verified fields;
  3. the result is discarded if a newer edit arrived meanwhile (that edit runs its own polish);
  4. the polished caption is stored and the live channel posts are edited to match the database.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramAPIError

from ai.copywriter import polish_post
from ai.errors import AllProvidersFailed, BlockedContentError
from caption import build_caption, parse_ai_json, product_caption_fields
from db.products import get_product, update_fields
from poster import refresh_live_posts

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger("repolish")

_CORE_FIELDS = ("name", "price", "size", "fabric", "stock")

# One lock per product: two quick edits are polished one after the other, never in parallel.
_locks: dict[int, asyncio.Lock] = {}


@dataclass(slots=True)
class RepolishOutcome:
    """What happened after an admin edit."""

    polished: bool = False  # the AI rewrite was validated, stored and pushed
    stale: bool = False  # a newer edit superseded this one; nothing was changed by this run
    caption_html: str = ""  # caption stored in the DB when this run finished
    live_edited: int = 0
    live_failed: int = 0
    reason: str = ""  # why the AI step was skipped: blocked | unavailable | invalid | error | missing


def _core(fields: dict[str, str]) -> tuple[str, ...]:
    """The admin-editable values, used to detect a newer edit."""
    return tuple(fields[k] for k in _CORE_FIELDS)


async def repolish_after_edit(bot: Bot, pid: int, changed_field: str, old_value: str) -> RepolishOutcome:
    """Polish product `pid` after the admin changed `changed_field` (was `old_value`); never raises."""
    lock = _locks.setdefault(pid, asyncio.Lock())
    async with lock:
        try:
            return await _run(bot, pid, changed_field, old_value)
        except Exception:
            logger.exception("Re-polish of product %s failed unexpectedly", pid)
            return RepolishOutcome(reason="error")


async def _run(bot: Bot, pid: int, changed_field: str, old_value: str) -> RepolishOutcome:
    """One polish pass; see the module docstring."""
    product = await get_product(pid)
    if product is None:
        return RepolishOutcome(reason="missing")
    snapshot = product_caption_fields(product)

    result = None
    reason = ""
    try:
        result = await polish_post(snapshot, changed_field, old_value)
        if result is None:
            reason = "invalid"
    except BlockedContentError:
        reason = "blocked"
    except AllProvidersFailed:
        reason = "unavailable"
    except Exception:
        logger.exception("AI polish of product %s failed", pid)
        reason = "error"

    latest = await get_product(pid)
    if latest is None:
        return RepolishOutcome(reason="missing")
    if _core(product_caption_fields(latest)) != _core(snapshot):
        logger.info("Polish of product %s dropped: a newer edit arrived meanwhile", pid)
        return RepolishOutcome(stale=True, caption_html=latest.caption_html)

    caption_html = latest.caption_html
    if result is not None:
        fields = dict(snapshot)
        fields["sales_pitch"] = result.sales_pitch
        fields["hashtags"] = result.hashtags
        caption_html = build_caption(fields, pid)
        ai_data = parse_ai_json(latest.ai_json)
        ai_data.update(fields)
        await update_fields(
            pid,
            hashtags=result.hashtags,
            caption_html=caption_html,
            ai_json=json.dumps(ai_data, ensure_ascii=False),
        )

    outcome = RepolishOutcome(polished=result is not None, caption_html=caption_html, reason=reason)
    try:
        counts = await refresh_live_posts(bot, pid)
    except (TelegramAPIError, ValueError):
        logger.exception("Could not refresh the live posts of product %s", pid)
        return outcome
    outcome.live_edited = counts.get("edited", 0)
    outcome.live_failed = counts.get("failed", 0)
    return outcome

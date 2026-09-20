"""Shared aiogram filters used across the handlers package."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram.filters import BaseFilter

from db.admins import is_admin, is_head_admin

if TYPE_CHECKING:
    from aiogram.types import CallbackQuery, Message


class AdminFilter(BaseFilter):
    """True when the sender is the head admin or a regular admin (checked against the RAM cache)."""

    async def __call__(self, event: Message | CallbackQuery, **kwargs: Any) -> bool:
        user = event.from_user
        return user is not None and is_admin(user.id)


class HeadAdminFilter(BaseFilter):
    """True only for the head admin (BOSH_ADMIN_ID); guards the admin-management section."""

    async def __call__(self, event: Message | CallbackQuery, **kwargs: Any) -> bool:
        user = event.from_user
        return user is not None and is_head_admin(user.id)


class NotAdminFilter(BaseFilter):
    """True when the sender is NOT an admin; keeps catch-all customer handlers away from admin messages."""

    async def __call__(self, event: Message | CallbackQuery, **kwargs: Any) -> bool:
        user = event.from_user
        return user is None or not is_admin(user.id)

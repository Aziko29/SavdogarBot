"""Saved order details: the phone and delivery address a customer used last time.

They are stored only after a confirmed order and only so the next order form can offer them with one
tap. The customer can see and delete them himself (`/malumotlarim`); nothing here is shown to admins.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, select

from db.engine import dialect_insert, engine, logged_db
from db.schema import customers_t
from utils import utcnow


@dataclass(slots=True)
class SavedDetails:
    """A customer's remembered phone and address (either may be empty)."""

    phone: str
    address: str

    @property
    def usable(self) -> bool:
        """True when both are present, i.e. the order form can skip its phone and address steps."""
        return bool(self.phone and self.address)


@logged_db
async def get_saved_details(user_id: int) -> SavedDetails | None:
    """The customer's saved details, or None if he has none."""
    async with engine.connect() as conn:
        row = (await conn.execute(select(customers_t).where(customers_t.c.user_id == user_id))).first()
    return SavedDetails(row.phone, row.address) if row is not None else None


@logged_db
async def save_details(user_id: int, phone: str, address: str | None) -> bool:
    """Remember the phone (and the address); True only if something new or different was stored.

    `address=None` keeps the address already on file: a one-off "I'll pick it up myself" order must not
    overwrite a real delivery address.
    """
    existing = await get_saved_details(user_id)
    new_address = address if address is not None else (existing.address if existing is not None else "")
    if existing is not None and (existing.phone, existing.address) == (phone, new_address):
        return False
    values = {"phone": phone, "address": new_address, "updated_at": utcnow()}
    stmt = dialect_insert(customers_t).values(user_id=user_id, **values)
    stmt = stmt.on_conflict_do_update(index_elements=["user_id"], set_=values)
    async with engine.begin() as conn:
        await conn.execute(stmt)
    return True


@logged_db
async def delete_saved_details(user_id: int) -> bool:
    """Forget the customer's details; True if there was anything to delete."""
    async with engine.begin() as conn:
        result = await conn.execute(delete(customers_t).where(customers_t.c.user_id == user_id))
    return result.rowcount == 1

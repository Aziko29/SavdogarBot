"""Post ID system: every caption carries the product's database id, verified before it reaches the channel.

The number printed in a channel post ("ID: 42") is `products.id`, the same key the admin panel
shows as "#42" and the buy button's deep link carries (`?start=prod_42`). Captions stored before
this feature existed have no ID line, so it is added lazily (`ensure_caption_id`, called right
before publishing or editing a post) and eagerly once at startup (`backfill_caption_ids`).
"""
from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING

from caption import build_caption, has_product_id, product_caption_fields
from db.products import list_products, update_fields

if TYPE_CHECKING:
    from db.models import Product

logger = logging.getLogger("post_id")

_BACKFILL_PAGE = 200


async def ensure_caption_id(product: Product) -> Product:
    """Return the product with a caption whose ID line equals product.id, fixing the DB if needed.

    Products whose AI result is not ready have no caption yet and are returned unchanged; so is a
    product without a stored sales pitch (the caption cannot be rebuilt safely without it).
    """
    if product.ai_status != "done" or has_product_id(product.caption_html, product.id):
        return product
    fields = product_caption_fields(product)
    if not fields["sales_pitch"].strip():
        logger.warning("Product %s has no stored sales pitch; cannot add its ID line", product.id)
        return product
    caption = build_caption(fields, product.id)
    await update_fields(product.id, caption_html=caption)
    logger.info("Added the ID line to the caption of product %s", product.id)
    return dataclasses.replace(product, caption_html=caption)


async def backfill_caption_ids() -> int:
    """Give every stored caption its ID line (one pass at startup); returns how many were fixed."""
    fixed = 0
    offset = 0
    while True:
        items, total = await list_products("all", offset, _BACKFILL_PAGE)
        for product in items:
            if product.ai_status != "done" or has_product_id(product.caption_html, product.id):
                continue
            if (await ensure_caption_id(product)).caption_html != product.caption_html:
                fixed += 1
        offset += _BACKFILL_PAGE
        if offset >= total:
            return fixed

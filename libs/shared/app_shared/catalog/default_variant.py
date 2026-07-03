"""Default-variant derivation (`contracts/default-variant.md`, FR-005/FR-006).

Pure, framework-agnostic (no FastAPI, no DB — plain ``Mapping``/``dict``
in, ``dict``/``list`` out). Used by ``POST /v1/products``
(``apps/api/app/routers/products.py``) and, later, bulk-upsert
(SPEC-04 US2) to guarantee every product ends up with at least one
variant.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app_shared.enums import VariantStatus

DEFAULT_VARIANT_TITLE = "Default"


def derive_default_variant(product: Mapping[str, Any]) -> dict[str, Any]:
    """Return the field dict for the single default variant of ``product``.

    - ``title`` = ``product["title"]`` if present and non-empty, else
      ``"Default"``.
    - ``sku``/``url`` are inherited from the product payload (``None``
      when absent).
    - ``current_price``/``currency`` come from ``product["price"]`` /
      ``product["currency"]`` — a "simple product" create MUST supply
      these (``product_variants.current_price``/``currency`` are
      ``NOT NULL`` per §22); no default/fallback is applied here.
    - ``option_values`` is always ``None``; ``status`` is always
      ``VariantStatus.ACTIVE``.
    - ``product_id`` is deliberately NOT set here — the caller fills it
      in once the parent product row exists (id assignment happens at
      insert time).
    """
    title = product.get("title") or DEFAULT_VARIANT_TITLE
    return {
        "title": title,
        "sku": product.get("sku"),
        "url": product.get("url"),
        "current_price": product.get("price"),
        "currency": product.get("currency"),
        "option_values": None,
        "status": VariantStatus.ACTIVE,
    }


def ensure_at_least_one(
    product: Mapping[str, Any], variants: list[Any]
) -> list[Any]:
    """Return ``variants`` unchanged if non-empty, else one derived default.

    A product **with** explicit variants gets no extra default
    (FR-005). A product **without** variants gets exactly one default
    (SC-001) — never zero, never two.
    """
    if variants:
        return variants
    return [derive_default_variant(product)]

"""Input caps for the public API (PLAN §7.4, risks P5/P6).

One module so the numbers are greppable and identical everywhere. Each
cap exists to bound work a customer can ask for in a single request; the
guards raise before any per-item work happens, so an over-cap request
costs parsing and nothing else.
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import HTTPException

#: Maximum items in one bulk-upsert request (PLAN §7.4). Matches the
#: Connect API's `catalog/batch` cap (§7.5) so a plugin can use one
#: chunk size against both surfaces.
MAX_BULK_ITEMS = 500

#: Distinct competitor domains one workspace may register (PLAN §7.4).
#: Admin-raisable; the default is generous for a real store and narrow
#: enough that a hostile customer cannot fan out across the web.
#: Consumed by Task 9, not this task -- defined here so the numbers stay
#: greppable in one place; intentionally unused in this module.
MAX_DOMAINS_PER_WORKSPACE = 50

#: Protected-marketplace links per product (PLAN §5.1 policy guard).
#: Keeps the worst-case Marketplace check profitable (§5.7). Consumed by
#: Task 9, not this task -- see note above.
MAX_PROTECTED_LINKS_PER_PRODUCT = 4


def enforce_batch_cap(items: Sequence[object], *, what: str) -> None:
    """Raise 422 `BATCH_TOO_LARGE` when a batch exceeds `MAX_BULK_ITEMS`."""
    if len(items) > MAX_BULK_ITEMS:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": "BATCH_TOO_LARGE",
                    "message": (
                        f"A single request may contain at most {MAX_BULK_ITEMS} "
                        f"{what}; received {len(items)}."
                    ),
                }
            },
        )

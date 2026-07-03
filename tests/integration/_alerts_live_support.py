"""Shared support for the SPEC-09 alerts/price-analysis live-stack
integration tests (`test_price_analysis_recompute_live.py`,
`test_alert_events_history_live.py`, `test_alerts_isolation_live.py`,
`test_currency_mismatch_live.py`, `test_recompute_dedup_live.py`).

**Not a test module** — its filename deliberately does not match
pytest's default `test_*.py` collection pattern (mirrors
`_scrapyd_spider_live_support.py`'s established convention), so it is
never collected itself; it exists purely as an importable helper
library for its sibling live test files.

Builds on `_scrapyd_spider_live_support.seed_workspace_with_variant` /
`seed_competitor` / `seed_match` / `cleanup_seeded_workspace` (the
ws/product/variant/competitor/match seeding boilerplate every SPEC-09
live test also needs), adding:

1. `alerts_live_reachable` — Postgres (with the SPEC-09 alerts tables
   present) + Redis reachability probe (mirrors
   `_scrapyd_spider_live_support.live_stack_reachable`, substituting the
   SPEC-09 table set for the SPEC-07 one).
2. `set_variant_price` — update a seeded variant's `current_price`/
   `currency` directly (the shared seed helper defaults to
   `0.0000`/`USD`, which is unsuitable for the deterministic §23
   boundary scenarios these tests drive).
3. `seed_match_current_price` — insert one `match_current_prices` row
   directly (a comparable competitor observation), bypassing the spider.
4. `run_recompute_variant` — invoke
   `app.workers.tasks_analysis.recompute_variant` in its **own
   subprocess**. `apps/api` and `apps/workers` each ship their own
   top-level `app` package (both declare `packages = ["app"]" in their
   `pyproject.toml`), so importing `app.workers.tasks_analysis` in the
   same interpreter as an `app.main` `TestClient` (which several of
   these live tests also need, for the read-endpoint assertions) is
   ambiguous — exactly the reason
   `tests/unit/test_price_analysis_task.py` already runs in a fresh
   subprocess. Live tests hit the real `DATABASE_URL`/`REDIS_URL` (no
   fakes), so no `_ENV`/fake-session scaffolding is needed here — just
   the `sys.path` isolation.
5. `cleanup_alerts_rows` — delete `variant_price_states`/
   `variant_alert_states`/`price_alert_events` rows for a seeded
   workspace, on top of `cleanup_seeded_workspace`'s existing table set.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

_REQUIRED_ALERTS_TABLES = frozenset(
    {
        "workspaces",
        "products",
        "product_variants",
        "competitors",
        "competitor_product_matches",
        "match_current_prices",
        "variant_price_states",
        "variant_alert_states",
        "price_alert_events",
    }
)


def alerts_live_reachable(*, need_redis: bool = True) -> bool:
    """Best-effort probe: Postgres (with the SPEC-09 alerts tables present)
    + (optionally) Redis. Mirrors
    `_scrapyd_spider_live_support.live_stack_reachable`.
    """
    try:
        from app_shared.config import get_settings

        settings = get_settings()
    except Exception:
        return False

    if not settings.DATABASE_URL:
        return False
    if need_redis and not settings.REDIS_URL:
        return False

    try:
        from sqlalchemy import inspect

        from app_shared.database import check_connection, get_engine

        check_connection()
        table_names = set(inspect(get_engine()).get_table_names())
        if not _REQUIRED_ALERTS_TABLES <= table_names:
            return False

        if need_redis:
            from app_shared.redis_client import get_redis_client

            get_redis_client().ping()
    except Exception:
        return False

    return True


def set_variant_price(variant_id: uuid.UUID, *, price: Decimal | str, currency: str) -> None:
    """Update a seeded variant's `current_price`/`currency` directly."""
    from app_shared.database import get_session
    from app_shared.models.catalog import ProductVariant

    with get_session() as session:
        variant = session.get(ProductVariant, variant_id)
        assert variant is not None
        variant.current_price = Decimal(price)
        variant.currency = currency
        session.commit()


def seed_match_current_price(
    seeded: Any,
    match_id: uuid.UUID,
    competitor_id: uuid.UUID,
    *,
    price: Decimal | str | None,
    currency: str = "USD",
    success: bool = True,
    comparable: bool = True,
    error_code: Any = None,
) -> uuid.UUID:
    """Insert one `match_current_prices` row directly (bypasses the spider).

    `seeded` is a `_scrapyd_spider_live_support.SeededWorkspace`.
    """
    from datetime import UTC, datetime

    from app_shared.database import get_session
    from app_shared.models.observations import MatchCurrentPrice

    with get_session() as session:
        row = MatchCurrentPrice(
            workspace_id=seeded.workspace_id,
            match_id=match_id,
            product_id=seeded.product_id,
            product_variant_id=seeded.product_variant_id,
            competitor_id=competitor_id,
            price=None if price is None else Decimal(price),
            currency=currency,
            comparable=comparable,
            success=success,
            error_code=error_code,
            scraped_at=datetime.now(UTC),
        )
        session.add(row)
        session.commit()
        return row.id


_RECOMPUTE_RUNNER_TEMPLATE = """
import sys
sys.path.insert(0, "apps/workers")
import app.workers.tasks_analysis as tasks_analysis

tasks_analysis.recompute_variant(
    workspace_id={workspace_id!r},
    product_variant_id={product_variant_id!r},
    product_id={product_id!r},
    scrape_job_id={scrape_job_id!r},
)
"""


def run_recompute_variant(
    *,
    workspace_id: uuid.UUID | str,
    product_variant_id: uuid.UUID | str,
    product_id: uuid.UUID | str | None = None,
    scrape_job_id: uuid.UUID | str | None = None,
    timeout: float = 60.0,
) -> subprocess.CompletedProcess[str]:
    """Run `recompute_variant` directly (not `.delay()`) in a fresh
    subprocess against the real `DATABASE_URL`/`REDIS_URL` — no fakes.

    Returns the completed subprocess for the caller to assert
    `returncode == 0` on (surfacing `stderr` on failure).
    """
    script = _RECOMPUTE_RUNNER_TEMPLATE.format(
        workspace_id=str(workspace_id),
        product_variant_id=str(product_variant_id),
        product_id=(None if product_id is None else str(product_id)),
        scrape_job_id=(None if scrape_job_id is None else str(scrape_job_id)),
    )
    return subprocess.run(  # noqa: S603 - fixed interpreter, generated script, test-only
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(REPO_ROOT),
        env=os.environ.copy(),
    )


def cleanup_alerts_rows(workspace_id: uuid.UUID) -> None:
    """Delete `variant_price_states`/`variant_alert_states`/
    `price_alert_events` rows for `workspace_id` — call BEFORE
    `cleanup_seeded_workspace` (no FK between these tables, but tidy
    ordering mirrors the deepest-dependent-first convention)."""
    from sqlalchemy import text

    from app_shared.database import get_session

    with get_session() as session:
        session.execute(
            text("DELETE FROM price_alert_events WHERE workspace_id = :ws"),
            {"ws": workspace_id},
        )
        session.execute(
            text("DELETE FROM variant_alert_states WHERE workspace_id = :ws"),
            {"ws": workspace_id},
        )
        session.execute(
            text("DELETE FROM variant_price_states WHERE workspace_id = :ws"),
            {"ws": workspace_id},
        )
        session.commit()

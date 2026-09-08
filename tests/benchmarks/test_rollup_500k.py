"""Daily-rollup scale benchmark: 500k observations (EPA C7, F12).

**Marker `benchmark`. NOT run by the unit gate and NOT run by C7 — the
plan defers the run itself to D1.** It exists now so D1 has something to
run rather than something to write.

WHAT IT MEASURES
----------------
The claim C7 makes is not "the rollup is faster"; it is that the rollup's
cost is now ``ceil(pairs / batch_size)`` statements instead of
``1 + 3 * pairs``, and that one Celery invocation therefore finishes, or
makes bounded durable progress and resumes. Wall-clock alone would not
show that -- a fast machine can hide an N+1 -- so this asserts BOTH:

* the statement count is the keyset-batch count, not a multiple of the
  variant count (the actual N+1 regression guard); and
* the whole day completes inside the invocation budget
  ``ROLLUP_INVOCATION_BUDGET_SECONDS``, with the measured wall clock
  reported for the D1 record.

SIZE AND COST
-------------
`ROLLUP_BENCHMARK_VARIANTS` (default 5,000) variants x
`ROLLUP_BENCHMARK_OBSERVATIONS_PER_VARIANT` (default 100) = 500,000
observations, seeded with `COPY`-style batched inserts into ONE
workspace and ONE day. That is several hundred MB of table plus index --
**check free disk before running.** Both knobs are environment
variables so D1 can scale the run down on a constrained host without
editing the file; the assertions are expressed as ratios, so they hold
at any size.

The seeded data is deleted in a `finally` (whole-partition drops are
retention's job, not a test's -- this test seeds into the live monthly
partition and must clean up after itself by workspace).
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.benchmark


VARIANTS = int(os.environ.get("ROLLUP_BENCHMARK_VARIANTS", "5000"))
OBSERVATIONS_PER_VARIANT = int(
    os.environ.get("ROLLUP_BENCHMARK_OBSERVATIONS_PER_VARIANT", "100")
)
#: Competitors per variant. Fewer than observations per variant on
#: purpose: the point of the dataset is that most observations are
#: REPEAT readings of the same competitor, which is what the
#: latest-eligible-per-match collapse has to chew through.
MATCHES_PER_VARIANT = int(os.environ.get("ROLLUP_BENCHMARK_MATCHES", "10"))
BATCH_SIZE = int(os.environ.get("ROLLUP_BENCHMARK_BATCH_SIZE", "5000"))


def _reachable() -> bool:
    try:
        from sqlalchemy import inspect

        from app_shared.config import get_settings
        from app_shared.database import check_connection, get_engine

        if not get_settings().DATABASE_URL:
            return False
        check_connection()
        tables = set(inspect(get_engine()).get_table_names())
        return {
            "price_observations",
            "variant_price_states",
            "variant_price_daily_rollups",
            "rollup_completion",
        } <= tables
    except Exception:
        return False


class _CountingSession:
    """Wraps a real Session and counts `execute` calls.

    The N+1 guard needs the statement count as the code issues it, which
    `pg_stat_statements` cannot give per-call without extra privileges
    this test must not require.
    """

    def __init__(self, session) -> None:
        self._session = session
        self.executes = 0

    def execute(self, *args, **kwargs):
        self.executes += 1
        return self._session.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._session, name)


@pytest.mark.skipif(
    not _reachable(),
    reason="Needs a reachable Postgres with the SPEC-07/09/15 tables and rollup_completion.",
)
def test_five_hundred_thousand_observations_roll_up_in_keyset_batches() -> None:
    from app_shared.config import get_settings
    from app_shared.database import get_session, get_system_sessionmaker
    from app_shared.enums import ProductStatus, VariantStatus, WorkspaceStatus
    from app_shared.maintenance.rollups import run_daily_rollup
    from app_shared.models import Workspace
    from app_shared.models.catalog import Product, ProductVariant

    day = (datetime.now(timezone.utc) - timedelta(days=2)).date()
    noon = datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc)
    unique = uuid.uuid4().hex[:8]

    with get_session() as session:
        workspace = Workspace(
            name=f"rollup-benchmark {unique}",
            slug=f"rollup-benchmark-{unique}",
            status=WorkspaceStatus.ACTIVE,
        )
        session.add(workspace)
        session.flush()
        product = Product(
            workspace_id=workspace.id,
            title=f"rollup benchmark {unique}",
            status=ProductStatus.ACTIVE,
        )
        session.add(product)
        session.flush()
        workspace_id = workspace.id
        product_id = product.id
        variant_ids = []
        for index in range(VARIANTS):
            variant = ProductVariant(
                workspace_id=workspace_id,
                product_id=product_id,
                title=f"v{index}",
                current_price=Decimal("100.0000"),
                currency="USD",
                status=VariantStatus.ACTIVE,
            )
            session.add(variant)
            variant_ids.append(variant.id)
        session.commit()

    try:
        _seed_states(workspace_id, product_id, variant_ids)
        _seed_observations(workspace_id, product_id, variant_ids, noon)

        with get_system_sessionmaker()() as raw:
            session = _CountingSession(raw)
            started = time.monotonic()
            report = run_daily_rollup(
                session, target_date=day, batch_limit=BATCH_SIZE
            )
            elapsed = time.monotonic() - started

        expected_batches = -(-VARIANTS // BATCH_SIZE) + (
            1 if VARIANTS % BATCH_SIZE == 0 else 0
        )
        print(
            f"\nrollup_benchmark variants={VARIANTS} "
            f"observations={VARIANTS * OBSERVATIONS_PER_VARIANT} "
            f"batches={report.batches} statements={session.executes} "
            f"seconds={elapsed:.1f}"
        )

        assert report.complete is True
        assert report.rollups_upserted == VARIANTS
        assert report.batches == expected_batches
        # The N+1 guard: statements must scale with BATCHES, not with
        # variants. Each batch costs one statement plus one checkpoint
        # write, plus a single capability probe for the whole run.
        assert session.executes <= 2 * report.batches + 2
        assert session.executes < VARIANTS, (
            "statement count still scales with the variant count -- the N+1 is back"
        )
        assert elapsed < get_settings().ROLLUP_INVOCATION_BUDGET_SECONDS, (
            "a single day did not fit in one Celery invocation's budget"
        )
    finally:
        _cleanup(workspace_id, day)


def _seed_states(workspace_id, product_id, variant_ids) -> None:
    from app_shared.database import get_session

    with get_session() as session:
        for chunk in _chunks(variant_ids, 1000):
            session.execute(
                text(
                    """
                    INSERT INTO variant_price_states (
                        id, workspace_id, product_id, product_variant_id,
                        client_price, currency, comparable_competitor_count,
                        latest_alert_type, latest_alert_severity, calculated_at,
                        created_at, updated_at)
                    SELECT gen_random_uuid(), :ws, :product, v, 100.0000, 'USD', 0,
                           'NO_COMPETITOR_DATA', 'INFO', now(), now(), now()
                    FROM unnest(CAST(:variants AS uuid[])) AS v
                    """
                ),
                {
                    "ws": workspace_id,
                    "product": product_id,
                    "variants": [str(v) for v in chunk],
                },
            )
        session.commit()


def _seed_observations(workspace_id, product_id, variant_ids, noon) -> None:
    """`OBSERVATIONS_PER_VARIANT` rows per variant spread over
    `MATCHES_PER_VARIANT` competitors and over the day's hours, so most
    rows are repeat readings the collapse must discard."""
    from app_shared.database import get_session

    with get_session() as session:
        for chunk in _chunks(variant_ids, 200):
            session.execute(
                text(
                    """
                    INSERT INTO price_observations (
                        id, workspace_id, scraped_at, match_id, product_id,
                        product_variant_id, price, currency, success, comparable,
                        created_at, updated_at)
                    SELECT gen_random_uuid(), :ws,
                           :noon + make_interval(secs => (n % 86000)),
                           md5(v::text || (n % :matches)::text)::uuid,
                           :product, v,
                           10.0000 + (n % 50), 'USD', TRUE, TRUE, now(), now()
                    FROM unnest(CAST(:variants AS uuid[])) AS v,
                         generate_series(0, :per_variant - 1) AS n
                    """
                ),
                {
                    "ws": workspace_id,
                    "product": product_id,
                    "noon": noon.replace(hour=0),
                    "variants": [str(v) for v in chunk],
                    "per_variant": OBSERVATIONS_PER_VARIANT,
                    "matches": MATCHES_PER_VARIANT,
                },
            )
            session.commit()


def _cleanup(workspace_id, day: date) -> None:
    from app_shared.database import get_session, get_system_sessionmaker

    with get_system_sessionmaker()() as session:
        session.execute(
            text("DELETE FROM rollup_completion WHERE date = :day"), {"day": day}
        )
        session.commit()
    with get_session() as session:
        for table in (
            "price_observations",
            "variant_price_daily_rollups",
            "variant_price_states",
            "product_variants",
            "products",
        ):
            session.execute(
                text(f"DELETE FROM {table} WHERE workspace_id = :ws"),
                {"ws": workspace_id},
            )
        session.execute(
            text("DELETE FROM workspaces WHERE id = :ws"), {"ws": workspace_id}
        )
        session.commit()


def _chunks(items: list, size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]

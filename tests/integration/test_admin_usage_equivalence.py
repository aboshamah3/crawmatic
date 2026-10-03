"""E6 (2026-09-29): `/v1/admin/usage` is fast AND returns exactly what it did.

Production: a busy one-hour window took 28.9 s (the SaaS importer gives up
at 5 s), an empty one 0.45 s. The query OR-joined `network_operations` on
`network_request_id = X OR parent_operation_id = X` with no bound on the
ledger's partition key and no index on `parent_operation_id`, and applied
the keyset cursor as a HAVING after aggregating the whole window.

The rewrite is only acceptable if it changes NOTHING the SaaS importer can
see: field values, row order, and what each cursor returns -- the importer
derives exactly-once identity from them. So this module seeds one realistic
ledger (retries, shared operations, browser children, direct and proxied
egress, discovery traffic, jobs whose `created_at` predates the window,
observations) and compares every page of the new query with every page of
the frozen pre-E6 query (`_admin_usage_oracle.py`), then measures the new
one on a production-sized ledger.

Database: the scratch Postgres named by `NETWORK_OPS_TEST_DATABASE_URL` (the
same variable, and the same skip-when-unset rule, as
`test_admin_usage_fixtures.py`). The scratch superuser bypasses RLS on
purpose: this suite is about the aggregate, RLS has its own suites.
"""

from __future__ import annotations

import os
import random
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.services.admin_usage import UsageCursor, build_usage_query
from ._admin_usage_oracle import build_usage_query_oracle

REPO_ROOT = Path(__file__).resolve().parents[2]
_DSN_ENV = "NETWORK_OPS_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(not os.environ.get(_DSN_ENV), reason=f"{_DSN_ENV} unset")

#: The equivalence ledger lives in April, the performance ledger in May, so
#: the two datasets never share a window.
EQ_SINCE = datetime(2026, 4, 14, 20, 0, tzinfo=timezone.utc)
EQ_UNTIL = EQ_SINCE + timedelta(hours=3)
PERF_SINCE = datetime(2026, 5, 20, 20, 0, tzinfo=timezone.utc)
PERF_UNTIL = PERF_SINCE + timedelta(hours=1)
FRACTION_SCALE = 1_000_000_000


@pytest.fixture(scope="module")
def engine():  # type: ignore[no-untyped-def]
    dsn = os.environ[_DSN_ENV]
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"db_url={dsn}", "upgrade", "head"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.fail(f"alembic upgrade head failed:\n{proc.stdout}\n{proc.stderr}")
    eng = create_engine(dsn, future=True)
    for moment in (EQ_SINCE, PERF_SINCE):
        _ensure_partitions(eng, moment)
    try:
        yield eng
    finally:
        eng.dispose()


def _ensure_partitions(engine, moment: datetime) -> None:  # type: ignore[no-untyped-def]
    start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(month=start.month + 1)
    with engine.begin() as conn:
        for parent in ("request_attempts", "price_observations", "network_operations"):
            conn.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS {parent}_{start:%Y_%m} PARTITION OF {parent} "
                    f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
                )
            )


class Ledger:
    """Bulk writer for workspaces, matches, jobs, attempts, operations, observations."""

    def __init__(self, engine, rng: random.Random) -> None:  # type: ignore[no-untyped-def]
        self.engine = engine
        self.rng = rng
        self.ops: list[dict[str, Any]] = []
        self.attempts: list[dict[str, Any]] = []
        self.observations: list[dict[str, Any]] = []
        self.allocations: list[dict[str, Any]] = []

    def tenant(self, n_products: int, matches_per_product: int) -> dict[str, Any]:
        ws = uuid.uuid4()
        comp = uuid.uuid4()
        products = []
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO workspaces (id, name, slug, status, created_at, updated_at) "
                    "VALUES (:id, :n, :n, 'ACTIVE', now(), now())"
                ),
                {"id": ws, "n": f"ws-{ws.hex[:10]}"},
            )
            conn.execute(
                text(
                    "INSERT INTO competitors (id, workspace_id, name, domain, status, "
                    "legal_status, robots_policy, created_at, updated_at) VALUES "
                    "(:id, :ws, 'Rival', 'rival.test', 'ACTIVE', 'REVIEW_REQUIRED', "
                    "'RESPECT', now(), now())"
                ),
                {"id": comp, "ws": ws},
            )
            for _ in range(n_products):
                product, variant = uuid.uuid4(), uuid.uuid4()
                conn.execute(
                    text(
                        "INSERT INTO products (id, workspace_id, title, status, created_at, "
                        "updated_at) VALUES (:id, :ws, 'W', 'ACTIVE', now(), now())"
                    ),
                    {"id": product, "ws": ws},
                )
                conn.execute(
                    text(
                        "INSERT INTO product_variants (id, workspace_id, product_id, title, "
                        "current_price, currency, status, created_at, updated_at) VALUES "
                        "(:id, :ws, :p, 'W1', 10, 'SAR', 'ACTIVE', now(), now())"
                    ),
                    {"id": variant, "ws": ws, "p": product},
                )
                matches = []
                for _ in range(matches_per_product):
                    match = uuid.uuid4()
                    url = f"https://rival.test/{match.hex}"
                    conn.execute(
                        text(
                            "INSERT INTO competitor_product_matches (id, workspace_id, "
                            "product_id, product_variant_id, competitor_id, competitor_url, "
                            "normalized_competitor_url, url_pattern, url_pattern_version, "
                            "priority, status, health_status, consecutive_failures, "
                            "created_at, updated_at) VALUES (:id, :ws, :p, :v, :c, :u, :u, "
                            ":u, 1, 'NORMAL', 'ACTIVE', 'UNKNOWN', 0, now(), now())"
                        ),
                        {"id": match, "ws": ws, "p": product, "v": variant, "c": comp, "u": url},
                    )
                    matches.append(match)
                products.append({"id": product, "variant": variant, "matches": matches})
        return {"ws": ws, "products": products}

    def job(self, ws: uuid.UUID, created_at: datetime) -> uuid.UUID:
        job = uuid.uuid4()
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO scrape_jobs (id, workspace_id, type, scope, status, priority, "
                    "total_targets, success_count, failure_count, skipped_count, source, "
                    "created_at) VALUES (:id, :ws, 'SCHEDULED', 'WORKSPACE', 'COMPLETED', "
                    "'NORMAL', 0, 0, 0, 0, 'SCHEDULER', :at)"
                ),
                {"id": job, "ws": ws, "at": created_at},
            )
        return job

    def op(
        self,
        at: datetime,
        *,
        transport: str,
        provider: str,
        bytes_compressed: int,
        cost: int,
        parent: uuid.UUID | None = None,
        allocate_to: uuid.UUID | None = None,
    ) -> uuid.UUID:
        nrid = uuid.uuid4()
        self.ops.append(
            {
                "id": uuid.uuid4(),
                "nrid": nrid,
                "parent": parent,
                "hash": nrid.hex,
                "prov": provider,
                "tr": transport,
                "at": at,
                "bytes": bytes_compressed,
                "cost": cost if allocate_to is not None else 0,
            }
        )
        if allocate_to is not None and cost:
            self.allocations.append(
                {"id": uuid.uuid4(), "ws": allocate_to, "op": nrid, "cost": cost}
            )
        return nrid

    def attempt(self, **row: Any) -> None:
        self.attempts.append({"id": uuid.uuid4(), **row})

    def observation(self, **row: Any) -> None:
        self.observations.append({"id": uuid.uuid4(), **row})

    def flush(self) -> None:
        with self.engine.begin() as conn:
            if self.ops:
                conn.execute(
                    text(
                        "INSERT INTO network_operations (id, network_request_id, "
                        "parent_operation_id, canonical_url_hash, domain, http_method, "
                        "provider, transport, created_at, closed_at, bytes_compressed, "
                        "estimated_cost_micro_units, currency) VALUES (:id, :nrid, :parent, "
                        ":hash, 'rival.test', 'GET', :prov, :tr, :at, :at, :bytes, :cost, 'USD')"
                    ),
                    self.ops,
                )
            if self.allocations:
                conn.execute(
                    text(
                        "INSERT INTO network_operation_allocations (id, workspace_id, "
                        "operation_id, fraction_ppb, allocated_cost_micro_units, currency, "
                        f"created_at) VALUES (:id, :ws, :op, {FRACTION_SCALE}, :cost, 'USD', now())"
                    ),
                    self.allocations,
                )
            if self.attempts:
                conn.execute(
                    text(
                        "INSERT INTO request_attempts (id, created_at, workspace_id, match_id, "
                        "scrape_job_id, url, attempt_number, access_method, proxy_provider_id, "
                        "success, origin, network_operation_id) VALUES (:id, :at, :ws, :m, "
                        ":job, 'https://rival.test/p', :n, :am, :ppid, :ok, :origin, :op)"
                    ),
                    self.attempts,
                )
            if self.observations:
                conn.execute(
                    text(
                        "INSERT INTO price_observations (id, workspace_id, match_id, "
                        "product_id, product_variant_id, scraped_at, success, comparable, "
                        "scrape_job_id) VALUES (:id, :ws, :m, :p, :v, :at, :ok, true, :job)"
                    ),
                    self.observations,
                )
        self.ops, self.attempts, self.observations, self.allocations = [], [], [], []


def _seed_equivalence(engine) -> None:  # type: ignore[no-untyped-def]
    rng = random.Random(20260929)
    ledger = Ledger(engine, rng)
    methods = ["DIRECT_HTTP", "PROXY_HTTP", "PLAYWRIGHT_DIRECT", "PLAYWRIGHT_PROXY"]
    for _t in range(3):
        tenant = ledger.tenant(n_products=5, matches_per_product=3)
        ws = tenant["ws"]
        provider = uuid.uuid4()
        # A job created the evening before: its attempts' cycle_ts is the
        # job's hour, OUTSIDE the window -- the exported rows must match.
        jobs = [
            ledger.job(ws, EQ_SINCE - timedelta(hours=20)),
            ledger.job(ws, EQ_SINCE + timedelta(minutes=10)),
            None,
        ]
        shared: dict[uuid.UUID, uuid.UUID] = {}
        for product in tenant["products"]:
            for match in product["matches"]:
                for attempt_no in range(1, rng.randint(1, 3) + 1):
                    at = EQ_SINCE + timedelta(minutes=rng.randint(-40, 220))
                    method = rng.choice(methods)
                    proxied = method.endswith("PROXY") or method == "PROXY_HTTP"
                    transport = "BROWSER" if method.startswith("PLAYWRIGHT") else (
                        "PROXY" if proxied else "DIRECT"
                    )
                    prov = str(provider) if proxied else (
                        "browser" if transport == "BROWSER" else "direct"
                    )
                    op_at = at + timedelta(minutes=rng.randint(-5, 5))
                    if product["id"] in shared and rng.random() < 0.3:
                        op_id = shared[product["id"]]  # one fetch serving two links
                    else:
                        op_id = ledger.op(
                            op_at,
                            transport=transport,
                            provider=prov,
                            bytes_compressed=rng.randint(1_000, 900_000),
                            cost=rng.randint(0, 3_000) if proxied else 0,
                            allocate_to=ws if proxied else None,
                        )
                        shared.setdefault(product["id"], op_id)
                        if transport == "BROWSER":
                            for _c in range(rng.randint(0, 4)):
                                ledger.op(
                                    op_at + timedelta(seconds=rng.randint(0, 50)),
                                    transport=transport,
                                    provider=prov,
                                    bytes_compressed=rng.randint(100, 200_000),
                                    cost=rng.randint(0, 500) if proxied else 0,
                                    parent=op_id,
                                    allocate_to=ws if proxied else None,
                                )
                    ledger.attempt(
                        at=at,
                        ws=ws,
                        m=match,
                        job=rng.choice(jobs),
                        n=attempt_no,
                        am=method,
                        ppid=provider if proxied else None,
                        ok=rng.random() < 0.6,
                        origin=rng.choice(["scrape", "scrape", "scrape", "discovery"]),
                        op=op_id if rng.random() < 0.95 else None,
                    )
                if rng.random() < 0.7:
                    ledger.observation(
                        ws=ws,
                        m=match,
                        p=product["id"],
                        v=product["variant"],
                        at=EQ_SINCE + timedelta(minutes=rng.randint(-30, 200)),
                        ok=rng.random() < 0.5,
                        job=rng.choice(jobs),
                    )
    ledger.flush()


def _pages(session: Session, builder, since, until, limit: int) -> list[list[tuple]]:  # type: ignore[no-untyped-def]
    pages: list[list[tuple]] = []
    after: UsageCursor | None = None
    while True:
        rows = session.execute(builder(since=since, until=until, after=after, limit=limit)).all()
        page = [tuple(row) for row in rows[:limit]]
        pages.append(page)
        if len(rows) <= limit:
            return pages
        last = rows[limit - 1]
        after = UsageCursor(last.cycle_ts, last.workspace_id, last.product_id)
        assert len(pages) < 500, "pagination did not terminate"


@pytest.fixture(scope="module")
def seeded(engine):  # type: ignore[no-untyped-def]
    _seed_equivalence(engine)
    return engine


@pytest.mark.parametrize("limit", [1, 4, 7, 1000])
def test_every_page_matches_the_pre_e6_query(seeded, limit: int) -> None:  # type: ignore[no-untyped-def]
    with Session(seeded) as session:
        new = _pages(session, build_usage_query, EQ_SINCE, EQ_UNTIL, limit)
        old = _pages(session, build_usage_query_oracle, EQ_SINCE, EQ_UNTIL, limit)
    assert sum(len(p) for p in old) > 20, "the fixture must produce a real export"
    assert new == old


def test_a_cursor_from_the_middle_resumes_identically(seeded) -> None:  # type: ignore[no-untyped-def]
    with Session(seeded) as session:
        full = [r for p in _pages(session, build_usage_query_oracle, EQ_SINCE, EQ_UNTIL, 1000) for r in p]
        mid = full[len(full) // 2]
        after = UsageCursor(mid[2], mid[0], mid[1])
        new = session.execute(
            build_usage_query(since=EQ_SINCE, until=EQ_UNTIL, after=after, limit=1000)
        ).all()
        old = session.execute(
            build_usage_query_oracle(since=EQ_SINCE, until=EQ_UNTIL, after=after, limit=1000)
        ).all()
    assert [tuple(r) for r in new] == [tuple(r) for r in old]


# ---------------------------------------------------------------------------
# Performance: a production-sized hour
# ---------------------------------------------------------------------------


def _seed_performance(engine) -> None:  # type: ignore[no-untyped-def]
    """~2,000 attempts in one hour over a ledger of >= 200,000 operations
    (production had 213,954 operations and 106,492 attempts)."""
    ledger = Ledger(engine, random.Random(7))
    tenant = ledger.tenant(n_products=100, matches_per_product=2)
    ws = tenant["ws"]
    matches = [(p, m) for p in tenant["products"] for m in p["matches"]]
    with engine.begin() as conn:
        # 200k background operations spread over the month (the ledger the
        # old query seq-scanned per attempt), 1 in 3 a child.
        conn.execute(
            text(
                "INSERT INTO network_operations (id, network_request_id, parent_operation_id, "
                "canonical_url_hash, domain, http_method, provider, transport, created_at, "
                "closed_at, bytes_compressed, estimated_cost_micro_units, currency) "
                "SELECT gen_random_uuid(), gen_random_uuid(), "
                "CASE WHEN g % 3 = 0 THEN gen_random_uuid() END, md5(g::text), "
                "'bg.test', 'GET', 'direct', 'DIRECT', "
                ":start + (g * interval '12 seconds'), :start + (g * interval '12 seconds'), "
                "1000, 0, 'USD' FROM generate_series(1, 200000) AS g"
            ),
            {"start": PERF_SINCE.replace(day=1)},
        )
    rng = random.Random(11)
    for i in range(2000):
        product, match = matches[i % len(matches)]
        at = PERF_SINCE + timedelta(seconds=rng.randint(0, 3599))
        op_id = ledger.op(
            at, transport="BROWSER", provider="browser", bytes_compressed=5000, cost=0
        )
        for _c in range(3):
            ledger.op(at, transport="BROWSER", provider="browser", bytes_compressed=900, cost=0, parent=op_id)
        ledger.attempt(
            at=at, ws=ws, m=match, job=None, n=1, am="PLAYWRIGHT_DIRECT", ppid=None,
            ok=True, origin="scrape", op=op_id,
        )
        if i % 2 == 0:
            ledger.observation(ws=ws, m=match, p=product["id"], v=product["variant"], at=at, ok=True, job=None)
    ledger.flush()
    with engine.begin() as conn:
        conn.execute(text("ANALYZE network_operations"))
        conn.execute(text("ANALYZE request_attempts"))
        conn.execute(text("ANALYZE price_observations"))


@pytest.fixture(scope="module")
def perf_seeded(engine):  # type: ignore[no-untyped-def]
    _seed_performance(engine)
    return engine


def test_a_production_sized_hour_answers_in_under_two_seconds(perf_seeded) -> None:  # type: ignore[no-untyped-def]
    with Session(perf_seeded) as session:
        start = time.monotonic()
        rows = session.execute(
            build_usage_query(since=PERF_SINCE, until=PERF_UNTIL, after=None, limit=1000)
        ).all()
        elapsed = time.monotonic() - start
    assert len(rows) > 0
    assert elapsed < 2.0, f"export took {elapsed:.2f}s"


def test_the_plan_never_seq_scans_the_ledger(perf_seeded) -> None:  # type: ignore[no-untyped-def]
    stmt = build_usage_query(since=PERF_SINCE, until=PERF_UNTIL, after=None, limit=1000)
    compiled = stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    with perf_seeded.connect() as conn:
        plan = "\n".join(row[0] for row in conn.execute(text(f"EXPLAIN {compiled}")))
    offending = [
        line for line in plan.splitlines() if "Seq Scan" in line and "network_operations" in line
    ]
    assert not offending, plan

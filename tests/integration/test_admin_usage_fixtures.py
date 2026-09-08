"""EPA C6 / F17 — the usage export's provider dimension, against real rows.

`tests/unit/test_admin_usage_sql.py` proves the statement *compiles* into
the shape F17 asks for. It cannot prove the shape produces the right
numbers, because every claim F17 makes is a claim about how Postgres
folds real ledger rows:

* a **direct browser** navigation is free fleet egress and must NEVER
  appear in `proxied_browser_attempted` — the B3 bug that motivated F17,
  and the one a compile-only test is structurally unable to see;
* a **proxied browser** navigation must appear exactly once;
* **retries** are two physical operations of one logical link, so
  `links_total` stays 1 while the proxied counters go to 2;
* **child resources** (a browser page's subresources) have a
  `parent_operation_id` and no `request_attempts` row of their own —
  their bytes belong to the cycle and the old equi-join lost all of them;
* **one fetch shared by three matches** is ONE physical operation. Before
  F17 it was summed once per match; now `COUNT(DISTINCT
  network_request_id)` returns 1 and the money comes from
  `network_operation_allocations`, so the invariant **allocated cost ≤
  physical cost** holds instead of being violated threefold.

Database, and how this skips
----------------------------
Runs against the scratch database named by `NETWORK_OPS_TEST_DATABASE_URL`
— deliberately the SAME dedicated variable C1's own ledger tests use, read
from `os.environ` only, so a developer's configured `DATABASE_URL` can
never select itself here. Unset, or unreachable, and the module SKIPS; it
never fails a suite for a missing daemon.

The scratch container's superuser is used, so forced RLS is bypassed:
this suite is about the aggregate's arithmetic, and RLS has its own
dedicated suites. `request_attempts` is monthly-partitioned and the
partition for the fixture window is created on demand, so the scratch
database needs no maintenance job to have run first.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.services.admin_usage import build_usage_query

REPO_ROOT = Path(__file__).resolve().parents[2]
_DSN_ENV = "NETWORK_OPS_TEST_DATABASE_URL"

#: Everything the fixtures write lands in one hour, so every row folds
#: into ONE `cycle_ts` bucket and a row-count assertion is unambiguous.
CYCLE = datetime(2026, 3, 11, 9, 30, tzinfo=timezone.utc)
SINCE = CYCLE - timedelta(hours=2)
UNTIL = CYCLE + timedelta(hours=2)

#: `network_operations.provider` for fleet egress (`FLEET_PROVIDER_DIRECT`).
#: Spelled literally here on purpose: this suite is the check that the
#: service module and the ledger agree on the string, so importing the
#: same constant it uses would make the test agree with itself.
DIRECT = "direct"

pytestmark = pytest.mark.skipif(
    not os.environ.get(_DSN_ENV), reason=f"{_DSN_ENV} unset"
)


# ---------------------------------------------------------------------------
# Scratch database
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def engine():  # type: ignore[no-untyped-def]
    dsn = os.environ.get(_DSN_ENV)
    if not dsn:
        pytest.skip(f"{_DSN_ENV} unset — live usage-export tests skipped")
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"db_url={dsn}", "upgrade", "head"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.fail(f"alembic upgrade head failed:\n{proc.stdout}\n{proc.stderr}")
    eng = create_engine(dsn, future=True)
    _ensure_attempt_partition(eng, CYCLE)
    try:
        yield eng
    finally:
        eng.dispose()


def _ensure_attempt_partition(engine, moment: datetime) -> None:  # type: ignore[no-untyped-def]
    start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS request_attempts_{start:%Y_%m} "
                "PARTITION OF request_attempts "
                f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
            )
        )


class Fixture:
    """A workspace with one product, and helpers to write ledger rows.

    Every helper returns the ids it minted, so a test reads as the story
    it is asserting ("a direct browser fetch of one match") rather than as
    a wall of INSERTs.
    """

    def __init__(self, engine) -> None:  # type: ignore[no-untyped-def]
        self.engine = engine
        self.workspace_id = uuid.uuid4()
        self.product_id = uuid.uuid4()
        self.variant_id = uuid.uuid4()
        self.competitor_id = uuid.uuid4()
        suffix = self.workspace_id.hex[:8]
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO workspaces (id, name, slug, status, created_at, "
                    "updated_at) VALUES (:id, :n, :s, 'ACTIVE', now(), now())"
                ),
                {"id": self.workspace_id, "n": f"ws-{suffix}", "s": f"ws-{suffix}"},
            )
            conn.execute(
                text(
                    "INSERT INTO products (id, workspace_id, title, status, "
                    "created_at, updated_at) "
                    "VALUES (:id, :ws, 'Widget', 'ACTIVE', now(), now())"
                ),
                {"id": self.product_id, "ws": self.workspace_id},
            )
            conn.execute(
                text(
                    "INSERT INTO product_variants (id, workspace_id, product_id, "
                    "title, current_price, currency, status, created_at, updated_at) "
                    "VALUES (:id, :ws, :p, 'Widget v1', 10.0000, 'SAR', 'ACTIVE', "
                    "now(), now())"
                ),
                {"id": self.variant_id, "ws": self.workspace_id, "p": self.product_id},
            )
            conn.execute(
                text(
                    "INSERT INTO competitors (id, workspace_id, name, domain, "
                    "status, legal_status, robots_policy, created_at, updated_at) "
                    "VALUES (:id, :ws, 'Rival', 'rival.test', 'ACTIVE', "
                    "        'REVIEW_REQUIRED', 'RESPECT', now(), now())"
                ),
                {"id": self.competitor_id, "ws": self.workspace_id},
            )

    # -- writers ----------------------------------------------------------

    def match(self, url: str) -> uuid.UUID:
        match_id = uuid.uuid4()
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO competitor_product_matches "
                    "(id, workspace_id, product_id, product_variant_id, "
                    " competitor_id, competitor_url, normalized_competitor_url, "
                    " url_pattern, url_pattern_version, priority, status, "
                    " health_status, consecutive_failures, created_at, updated_at) "
                    "VALUES (:id, :ws, :p, :v, :c, :u, :u, :u, 1, 'NORMAL', "
                    "        'ACTIVE', 'UNKNOWN', 0, now(), now())"
                ),
                {
                    "id": match_id,
                    "ws": self.workspace_id,
                    "p": self.product_id,
                    "v": self.variant_id,
                    "c": self.competitor_id,
                    "u": url,
                },
            )
        return match_id

    def operation(
        self,
        *,
        transport: str,
        provider: str,
        bytes_compressed: int,
        cost_micro_units: int = 0,
        parent_operation_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        operation_id = uuid.uuid4()
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO network_operations "
                    "(id, network_request_id, parent_operation_id, canonical_url_hash, "
                    " domain, http_method, provider, transport, created_at, closed_at, "
                    " bytes_compressed, estimated_cost_micro_units, currency) "
                    "VALUES (:id, :nrid, :parent, :hash, 'rival.test', 'GET', :prov, "
                    "        :tr, :at, :at, :bytes, :cost, 'USD')"
                ),
                {
                    "id": uuid.uuid4(),
                    "nrid": operation_id,
                    "parent": parent_operation_id,
                    "hash": operation_id.hex,
                    "prov": provider,
                    "tr": transport,
                    "at": CYCLE,
                    "bytes": bytes_compressed,
                    "cost": cost_micro_units,
                },
            )
        return operation_id

    def allocate(
        self, operation_id: uuid.UUID, *, cost_micro_units: int, fraction_ppb: int
    ) -> None:
        """One workspace's share of one physical operation.

        A single-tenant operation gets `fraction_ppb = FRACTION_SCALE` and
        the whole cost; the deferred constraint trigger re-checks at
        COMMIT that an operation's allocations sum EXACTLY to its
        `estimated_cost_micro_units`, so a wrong split here fails loudly
        rather than producing a plausible wrong number.
        """
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO network_operation_allocations "
                    "(id, workspace_id, operation_id, fraction_ppb, "
                    " allocated_cost_micro_units, currency, created_at) "
                    "VALUES (:id, :ws, :op, :frac, :cost, 'USD', now())"
                ),
                {
                    "id": uuid.uuid4(),
                    "ws": self.workspace_id,
                    "op": operation_id,
                    "frac": fraction_ppb,
                    "cost": cost_micro_units,
                },
            )

    def attempt(
        self,
        *,
        match_id: uuid.UUID,
        operation_id: uuid.UUID | None,
        access_method: str,
        proxy_provider_id: uuid.UUID | None,
        success: bool = True,
        attempt_number: int = 1,
        minutes: int = 0,
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO request_attempts "
                    "(id, created_at, workspace_id, match_id, url, attempt_number, "
                    " access_method, proxy_provider_id, success, origin, "
                    " network_operation_id) "
                    "VALUES (:id, :at, :ws, :m, :url, :n, :am, :ppid, :ok, 'scrape', "
                    "        :op)"
                ),
                {
                    "id": uuid.uuid4(),
                    "at": CYCLE + timedelta(minutes=minutes),
                    "ws": self.workspace_id,
                    "m": match_id,
                    "url": "https://rival.test/p",
                    "n": attempt_number,
                    "am": access_method,
                    "ppid": proxy_provider_id,
                    "ok": success,
                    "op": operation_id,
                },
            )

    # -- reader -----------------------------------------------------------

    def export_row(self) -> Any:
        """The ONE export row for this workspace's single cycle."""
        with Session(self.engine) as session:
            rows = [
                row
                for row in session.execute(
                    build_usage_query(since=SINCE, until=UNTIL, after=None, limit=100)
                ).all()
                if row.workspace_id == self.workspace_id
            ]
        assert len(rows) == 1, rows
        return rows[0]

    def physical_cost(self) -> int:
        """What the FLEET was charged for every operation this cycle used."""
        with self.engine.begin() as conn:
            return int(
                conn.execute(
                    text(
                        "SELECT COALESCE(SUM(o.estimated_cost_micro_units), 0) "
                        "FROM network_operations o "
                        "WHERE o.network_request_id IN ("
                        "  SELECT a.network_operation_id FROM request_attempts a "
                        "  WHERE a.workspace_id = :ws"
                        ") OR o.parent_operation_id IN ("
                        "  SELECT a.network_operation_id FROM request_attempts a "
                        "  WHERE a.workspace_id = :ws)"
                    ),
                    {"ws": self.workspace_id},
                ).scalar_one()
            )


@pytest.fixture()
def fx(engine) -> Iterator[Fixture]:  # type: ignore[no-untyped-def]
    yield Fixture(engine)


# ---------------------------------------------------------------------------
# The five fixtures F17 names
# ---------------------------------------------------------------------------


def test_direct_browser_is_never_reported_as_proxied(fx: Fixture) -> None:
    """The B3 bug, stated as a test. A browser navigation made from the
    fleet's own egress IP costs browser CPU and NOT ONE PROXY BYTE;
    `transport = 'BROWSER'` said otherwise and billed it as proxied."""
    match_id = fx.match("https://rival.test/direct")
    operation_id = fx.operation(
        transport="BROWSER", provider="browser", bytes_compressed=900_000
    )
    fx.attempt(
        match_id=match_id,
        operation_id=operation_id,
        access_method="PLAYWRIGHT_DIRECT",
        proxy_provider_id=None,
    )

    row = fx.export_row()
    assert row.links_total == 1
    assert row.proxied_browser_attempted == 0
    assert row.proxied_http_attempted == 0
    assert row.proxy_bytes == 0


def test_a_provider_named_direct_is_never_reported_as_proxied(fx: Fixture) -> None:
    """The other half of the conjunction: a `DIRECT`-transport operation
    whose attempt somehow carries a `proxy_provider_id` is still direct
    egress, because `provider` says so."""
    match_id = fx.match("https://rival.test/plain")
    operation_id = fx.operation(
        transport="DIRECT", provider=DIRECT, bytes_compressed=40_000
    )
    fx.attempt(
        match_id=match_id,
        operation_id=operation_id,
        access_method="DIRECT_HTTP",
        proxy_provider_id=uuid.uuid4(),
    )

    row = fx.export_row()
    assert row.proxied_http_attempted == 0
    assert row.proxied_browser_attempted == 0
    assert row.proxy_bytes == 0


def test_proxied_browser_is_counted_once_with_its_bytes(fx: Fixture) -> None:
    provider_id = uuid.uuid4()
    match_id = fx.match("https://rival.test/proxied")
    operation_id = fx.operation(
        transport="BROWSER",
        provider=str(provider_id),
        bytes_compressed=1_200_000,
        cost_micro_units=2_400,
    )
    fx.allocate(operation_id, cost_micro_units=2_400, fraction_ppb=1_000_000_000)
    fx.attempt(
        match_id=match_id,
        operation_id=operation_id,
        access_method="PLAYWRIGHT_PROXY",
        proxy_provider_id=provider_id,
    )

    row = fx.export_row()
    assert row.proxied_browser_attempted == 1
    assert row.proxied_http_attempted == 0
    assert row.proxy_bytes == 1_200_000
    assert row.allocated_cost_micro_units == 2_400


def test_a_retry_is_two_physical_operations_of_one_logical_link(fx: Fixture) -> None:
    """`links_total` folds retries (a link is billed once no matter how
    often it was retried); the physical counters must NOT — the fleet
    paid for both fetches."""
    provider_id = uuid.uuid4()
    match_id = fx.match("https://rival.test/retried")
    for index in range(2):
        operation_id = fx.operation(
            transport="PROXY",
            provider=str(provider_id),
            bytes_compressed=100_000,
            cost_micro_units=500,
        )
        fx.allocate(operation_id, cost_micro_units=500, fraction_ppb=1_000_000_000)
        fx.attempt(
            match_id=match_id,
            operation_id=operation_id,
            access_method="PROXY_HTTP",
            proxy_provider_id=provider_id,
            success=index == 1,
            attempt_number=index + 1,
            minutes=index,
        )

    row = fx.export_row()
    assert row.links_total == 1
    assert row.links_succeeded == 1
    assert row.proxied_http_attempted == 2
    assert row.proxy_bytes == 200_000
    assert row.allocated_cost_micro_units == 1_000


def test_child_resources_are_included_and_their_bytes_counted(fx: Fixture) -> None:
    """A browser page's subresources are `network_operations` rows with a
    `parent_operation_id` and NO `request_attempts` row of their own. The
    equi-join B3 used lost every one — which is most of a page's bytes."""
    provider_id = uuid.uuid4()
    match_id = fx.match("https://rival.test/page")
    parent_id = fx.operation(
        transport="BROWSER",
        provider=str(provider_id),
        bytes_compressed=300_000,
        cost_micro_units=600,
    )
    fx.allocate(parent_id, cost_micro_units=600, fraction_ppb=1_000_000_000)
    for _ in range(2):
        child_id = fx.operation(
            transport="BROWSER",
            provider=str(provider_id),
            bytes_compressed=50_000,
            cost_micro_units=100,
            parent_operation_id=parent_id,
        )
        fx.allocate(child_id, cost_micro_units=100, fraction_ppb=1_000_000_000)
    fx.attempt(
        match_id=match_id,
        operation_id=parent_id,
        access_method="PLAYWRIGHT_PROXY",
        proxy_provider_id=provider_id,
    )

    row = fx.export_row()
    # Parent + 2 children = 3 distinct physical browser operations.
    assert row.proxied_browser_attempted == 3
    assert row.proxy_bytes == 300_000 + 50_000 + 50_000
    assert row.allocated_cost_micro_units == 600 + 100 + 100
    assert row.allocated_cost_micro_units <= fx.physical_cost()


def test_one_fetch_shared_by_three_matches_is_counted_once(fx: Fixture) -> None:
    """THE over-count F17 removes. One fetch of one competitor URL can
    satisfy three matches of the same product; B3 re-summed the same
    `network_request_id` three times and billed the tenant threefold."""
    provider_id = uuid.uuid4()
    operation_id = fx.operation(
        transport="PROXY",
        provider=str(provider_id),
        bytes_compressed=150_000,
        cost_micro_units=900,
    )
    fx.allocate(operation_id, cost_micro_units=900, fraction_ppb=1_000_000_000)
    for index in range(3):
        fx.attempt(
            match_id=fx.match(f"https://rival.test/shared?v={index}"),
            operation_id=operation_id,
            access_method="PROXY_HTTP",
            proxy_provider_id=provider_id,
        )

    row = fx.export_row()
    # Three logical links...
    assert row.links_total == 3
    # ...one physical operation.
    assert row.proxied_http_attempted == 1
    assert row.proxy_bytes == 150_000
    assert row.allocated_cost_micro_units == 900


def test_allocated_cost_never_exceeds_physical_cost(fx: Fixture) -> None:
    """The invariant, over the whole mixed cycle: direct + proxied +
    retried + shared. Equality only where the workspace is the sole
    tenant of every operation — which it is here, so this also proves the
    export is not silently under-reporting."""
    provider_id = uuid.uuid4()
    direct_match = fx.match("https://rival.test/free")
    fx.attempt(
        match_id=direct_match,
        operation_id=fx.operation(
            transport="DIRECT", provider=DIRECT, bytes_compressed=20_000
        ),
        access_method="DIRECT_HTTP",
        proxy_provider_id=None,
    )
    shared_id = fx.operation(
        transport="PROXY",
        provider=str(provider_id),
        bytes_compressed=80_000,
        cost_micro_units=1_200,
    )
    fx.allocate(shared_id, cost_micro_units=1_200, fraction_ppb=1_000_000_000)
    for index in range(3):
        fx.attempt(
            match_id=fx.match(f"https://rival.test/multi?v={index}"),
            operation_id=shared_id,
            access_method="PROXY_HTTP",
            proxy_provider_id=provider_id,
        )

    row = fx.export_row()
    physical = fx.physical_cost()
    assert row.allocated_cost_micro_units <= physical
    assert row.allocated_cost_micro_units == 1_200
    assert physical == 1_200
    # The direct fetch contributed links and NO proxy money or bytes.
    assert row.links_total == 4
    assert row.proxied_http_attempted == 1
    assert row.proxy_bytes == 80_000

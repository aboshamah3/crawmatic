"""EPA C1 (READY-005, fixes P0.7): the ``network_operations`` physical ledger.

Three separate append-only facts, never one mutable row:

1. ``network_operations`` — the PHYSICAL operation. Immutable intent
   fields written at open; immutable outcome fields written exactly once
   at close. **Fleet-owned**: no ``workspace_id`` column at all — tenant
   ownership lives only in the allocations table. After close the row is
   never updated again, and that is enforced by a DATABASE TRIGGER, not
   by application discipline.
2. ``network_operation_allocations`` — the tenant-visible, RLS-protected
   split of one physical operation's cost across the workspaces that
   caused it. Rounding is largest-remainder so the parts sum EXACTLY to
   the operation cost, and a DEFERRED constraint trigger enforces that
   total at COMMIT (not per-statement — an allocation set is written a
   row at a time and is only meaningful complete).
3. ``network_operation_settlements`` — append-only provider
   reconciliation facts (written later by C5). Corrections append a new
   ``settlement_version``; historical rows never mutate, enforced by a
   trigger that rejects UPDATE and DELETE outright.

Two test classes:

* the **offline** ones (always run) assert schema SHAPE from the mapped
  metadata and the migration source — money amounts are integer micro
  units (never float, never a bare numeric), the operation row carries
  no ``workspace_id``, the required indexes are declared, RLS is emitted
  for the allocations table ONLY, and ``RequestAttempt`` gained its
  nullable FK;
* the **live-Postgres** ones (skipped unless
  ``NETWORK_OPS_TEST_DATABASE_URL`` names a reachable scratch database)
  prove the three enforcement behaviours that only a real database can
  show: the closed-row UPDATE rejection, the settlement UPDATE/DELETE
  rejection, and the deferred allocation-total constraint firing at
  COMMIT rather than at INSERT.

``NETWORK_OPS_TEST_DATABASE_URL`` is a DEDICATED variable, deliberately
not ``DATABASE_URL``/``MIGRATION_DATABASE_URL``: this module runs DDL and
writes rows, so it must be impossible for it to reach a database that
some other part of the environment configured. It is read from
``os.environ`` only — ``.env`` is loaded by pydantic-settings into
``Settings``, never into ``os.environ``, so a ``.env`` DSN can never
select itself here.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import BigInteger, Date, Integer, String, Text, Uuid, create_engine, text

from app_shared.models.base import TZDateTime
from app_shared.models.network_operations import (
    BILLING_RATE_SCALE,
    FRACTION_SCALE,
    NetworkOperation,
    NetworkOperationAllocation,
    NetworkOperationSettlement,
    NetworkTransport,
    SettlementMethod,
    allocate_cost_largest_remainder,
)
from app_shared.models.observations import RequestAttempt

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATION_PATH = (
    REPO_ROOT / "alembic" / "versions" / "c4b19e7a2f08_network_operations_ledger.py"
)

#: Every column in the ledger that carries an amount of money. All four
#: are INTEGER micro-USD (never float, never a bare NUMERIC) — the §19
#: `app_shared.money.Money` no-float contract, expressed as scaled
#: integers so there is no Decimal-vs-float ambiguity at any boundary.
MONEY_COLUMNS = (
    (NetworkOperation, "estimated_cost_micro_units"),
    (NetworkOperation, "billing_rate_micro_units"),
    (NetworkOperationAllocation, "allocated_cost_micro_units"),
    (NetworkOperationSettlement, "reconciled_cost_micro_units"),
)


def _column(model: type, name: str) -> sa.Column:
    return model.__table__.columns[name]


class TestLedgerShape:
    """Offline schema-shape assertions — no database required."""

    def test_three_separate_tables(self) -> None:
        assert NetworkOperation.__tablename__ == "network_operations"
        assert NetworkOperationAllocation.__tablename__ == "network_operation_allocations"
        assert NetworkOperationSettlement.__tablename__ == "network_operation_settlements"

    def test_operation_row_is_fleet_owned_no_workspace_id(self) -> None:
        """The physical operation is a FLEET fact: tenant ownership lives
        only in allocations, so a ``workspace_id`` column here would be a
        lie the moment one browser fetch serves two workspaces."""
        assert "workspace_id" not in NetworkOperation.__table__.columns
        assert "workspace_id" not in NetworkOperationSettlement.__table__.columns
        # ...and the allocations table is where it DOES live.
        assert "workspace_id" in NetworkOperationAllocation.__table__.columns

    def test_money_columns_are_integer_micro_units_never_float(self) -> None:
        for model, name in MONEY_COLUMNS:
            column = _column(model, name)
            assert isinstance(column.type, BigInteger), (
                f"{model.__tablename__}.{name} must be BigInteger micro-USD, "
                f"got {column.type!r}"
            )
            # No float, and no bare NUMERIC either — a NUMERIC money column
            # reintroduces exactly the Decimal-vs-float ambiguity §19 exists
            # to remove.
            assert not isinstance(column.type, (sa.Float, sa.Numeric))

    def test_every_amount_is_paired_with_a_currency_code(self) -> None:
        for model, _name in MONEY_COLUMNS:
            currency = _column(model, "currency")
            assert isinstance(currency.type, String)
            assert currency.type.length == 3

    def test_no_float_column_anywhere_in_the_ledger(self) -> None:
        for model in (
            NetworkOperation,
            NetworkOperationAllocation,
            NetworkOperationSettlement,
        ):
            for column in model.__table__.columns:
                assert not isinstance(column.type, sa.Float), (
                    f"{model.__tablename__}.{column.name} is a float column"
                )

    def test_fraction_is_a_scaled_integer_not_a_float(self) -> None:
        fraction = _column(NetworkOperationAllocation, "fraction_ppb")
        assert isinstance(fraction.type, BigInteger)
        assert FRACTION_SCALE == 1_000_000_000
        assert BILLING_RATE_SCALE == 1_000_000

    def test_intent_and_outcome_fields_present(self) -> None:
        columns = NetworkOperation.__table__.columns
        intent = {
            "network_request_id",
            "retry_parent_id",
            "scrape_job_id",
            "canonical_url_hash",
            "domain",
            "http_method",
            "region",
            "provider",
            "provider_account",
            "transport",
            "parent_operation_id",
            "authorization_id",
            "entitlement_version",
            "budget_decision_version",
            "breaker_decision",
        }
        outcome = {
            "bytes_compressed",
            "bytes_decompressed",
            "response_status",
            "duration_ms",
            "failure_reason",
            "extraction_result",
            "identity_confidence",
            "comparability",
            "estimated_cost_micro_units",
            "currency",
            "billing_unit",
            "billing_rate_micro_units",
            "rate_effective_date",
        }
        assert intent <= set(columns.keys())
        assert outcome <= set(columns.keys())
        # `closed_at` is what makes "written exactly once at close" a
        # checkable predicate rather than a convention.
        assert isinstance(columns["closed_at"].type, TZDateTime)
        assert columns["closed_at"].nullable is True
        assert isinstance(columns["rate_effective_date"].type, Date)
        assert isinstance(columns["response_status"].type, Integer)
        assert isinstance(columns["duration_ms"].type, Integer)
        assert isinstance(columns["bytes_compressed"].type, BigInteger)
        assert isinstance(columns["bytes_decompressed"].type, BigInteger)

    def test_network_request_id_is_the_unique_key(self) -> None:
        """Unique PER MONTH since EPA C9 (F14), not globally.

        `network_operations` is partitioned by `created_at`, and
        PostgreSQL requires every partition-key column in a partitioned
        table's unique constraint — so the pre-dispatch identity's
        uniqueness is now `(network_request_id, created_at)`. The
        weakening is real and deliberate: `network_request_id` is a
        caller-minted UUIDv7, so a cross-month collision is not a thing
        that happens, but it is no longer a thing the database forbids.
        Asserted here rather than left to a migration docstring.
        """
        column = _column(NetworkOperation, "network_request_id")
        assert isinstance(column.type, Uuid)
        assert column.nullable is False
        assert column.unique is not True

        unique_cols = {
            tuple(c.name for c in constraint.columns)
            for constraint in NetworkOperation.__table__.constraints
            if isinstance(constraint, sa.UniqueConstraint)
        }
        assert ("network_request_id", "created_at") in unique_cols

        # The single-column lookup every ledger writer does must stay
        # index-backed even though it is no longer unique.
        assert "ix_network_operations_network_request_id" in {
            ix.name for ix in NetworkOperation.__table__.indexes
        }

    def test_the_ledger_is_monthly_partitioned(self) -> None:
        """EPA C9 (F14): the whole reason the key above changed shape."""
        assert (
            NetworkOperation.__table__.dialect_options["postgresql"]["partition_by"]
            == "RANGE (created_at)"
        )
        assert {c.name for c in NetworkOperation.__table__.primary_key.columns} == {
            "id",
            "created_at",
        }

    def test_transport_and_settlement_method_vocabularies(self) -> None:
        assert {t.value for t in NetworkTransport} == {"DIRECT", "PROXY", "BROWSER"}
        assert {m.value for m in SettlementMethod} == {
            "PRO_RATA_BYTES",
            "EXACT",
            "CORRECTION",
        }

    def test_required_indexes_declared(self) -> None:
        names = {ix.name for ix in NetworkOperation.__table__.indexes}
        assert "ix_network_operations_provider_created_at" in names
        assert "ix_network_operations_scrape_job_id" in names
        assert "ix_network_operations_canonical_url_hash_created_at" in names

        by_name = {ix.name: ix for ix in NetworkOperation.__table__.indexes}
        assert [c.name for c in by_name["ix_network_operations_provider_created_at"].columns] == [
            "provider",
            "created_at",
        ]
        assert [
            c.name
            for c in by_name["ix_network_operations_canonical_url_hash_created_at"].columns
        ] == ["canonical_url_hash", "created_at"]

    def test_settlements_are_versioned_per_operation(self) -> None:
        columns = NetworkOperationSettlement.__table__.columns
        assert {"operation_id", "settlement_version", "reconciled_cost_micro_units",
                "provider_usage_record_id", "method", "created_at"} <= set(columns.keys())
        unique_names = {
            c.name
            for c in NetworkOperationSettlement.__table__.constraints
            if isinstance(c, sa.UniqueConstraint)
        }
        assert "uq_nos_operation_id_settlement_version" in unique_names

    def test_request_attempt_gains_a_nullable_operation_fk(self) -> None:
        column = RequestAttempt.__table__.columns["network_operation_id"]
        assert isinstance(column.type, Uuid)
        assert column.nullable is True, (
            "the FK must be nullable — coverage is C3/C4's invariant, not this schema's"
        )
        # The FK itself is GONE as of EPA C9 (F14). A foreign key must
        # reference a UNIQUE constraint, and the ledger's only remaining
        # one now includes `created_at` — which a `request_attempts` row
        # does not carry (its own `created_at` is the attempt's, not the
        # operation's). Re-establishing it would mean denormalising the
        # parent's timestamp onto four tables, one of them this very
        # partitioned hot path. The link is now CHECKED rather than
        # constrained:
        # `app_shared.maintenance.ledger_summaries.find_orphan_references`.
        targets = {
            fk.target_fullname
            for fk in RequestAttempt.__table__.foreign_keys
            if fk.parent.name == "network_operation_id"
        }
        assert targets == set()

    def test_migration_applies_rls_to_allocations_only(self) -> None:
        source = MIGRATION_PATH.read_text(encoding="utf-8")
        assert 'emit_rls_policy("network_operation_allocations")' in source
        assert 'emit_rls_policy("network_operations")' not in source
        assert 'emit_rls_policy("network_operation_settlements")' not in source

    def test_migration_has_a_working_downgrade(self) -> None:
        source = MIGRATION_PATH.read_text(encoding="utf-8")
        assert "def downgrade() -> None:" in source
        for table in (
            "network_operation_settlements",
            "network_operation_allocations",
            "network_operations",
        ):
            assert f'op.drop_table("{table}")' in source
        assert 'op.drop_column("request_attempts", "network_operation_id")' in source


class TestLargestRemainder:
    """The rounding rule that makes the deferred total constraint satisfiable."""

    def test_three_workspaces_sum_exactly(self) -> None:
        # 100 micro-USD split three ways: naive rounding gives 33+33+33
        # = 99 and one lost micro-unit; largest-remainder gives back the
        # remainder to the largest fractional parts.
        parts = allocate_cost_largest_remainder(100, [1, 1, 1])
        assert sum(parts) == 100
        assert sorted(parts) == [33, 33, 34]

    def test_weighted_split_sums_exactly(self) -> None:
        parts = allocate_cost_largest_remainder(1_000_003, [7, 11, 13])
        assert sum(parts) == 1_000_003

    def test_single_tenant_gets_the_whole_cost(self) -> None:
        assert allocate_cost_largest_remainder(4_237, [1]) == [4_237]

    def test_zero_cost_allocates_zeroes(self) -> None:
        assert allocate_cost_largest_remainder(0, [1, 2, 3]) == [0, 0, 0]

    def test_rejects_float_weights(self) -> None:
        with pytest.raises(TypeError):
            allocate_cost_largest_remainder(100, [0.5, 0.5])  # type: ignore[list-item]

    def test_rejects_float_total(self) -> None:
        with pytest.raises(TypeError):
            allocate_cost_largest_remainder(100.0, [1, 1])  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Live-Postgres enforcement tests
# --------------------------------------------------------------------------

_DSN_ENV = "NETWORK_OPS_TEST_DATABASE_URL"


def _scratch_dsn() -> str | None:
    return os.environ.get(_DSN_ENV)


@pytest.fixture(scope="module")
def engine():  # type: ignore[no-untyped-def]
    dsn = _scratch_dsn()
    if not dsn:
        pytest.skip(f"{_DSN_ENV} unset — live ledger enforcement tests skipped")
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"db_url={dsn}", "upgrade", "head"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.fail(f"alembic upgrade head failed:\n{proc.stdout}\n{proc.stderr}")
    eng = create_engine(dsn, future=True)
    try:
        yield eng
    finally:
        eng.dispose()


def _open_operation(conn, **overrides) -> uuid.UUID:  # type: ignore[no-untyped-def]
    network_request_id = uuid.uuid4()
    params = {
        "id": uuid.uuid4(),
        "network_request_id": network_request_id,
        "canonical_url_hash": "sha256:" + network_request_id.hex,
        "domain": "example.test",
        "http_method": "GET",
        "provider": "test-provider",
        "transport": NetworkTransport.DIRECT.value,
    }
    params.update(overrides)
    conn.execute(
        text(
            "INSERT INTO network_operations "
            "(id, network_request_id, canonical_url_hash, domain, http_method, "
            " provider, transport) "
            "VALUES (:id, :network_request_id, :canonical_url_hash, :domain, "
            "        :http_method, :provider, :transport)"
        ),
        params,
    )
    return network_request_id


def _close_operation(conn, network_request_id: uuid.UUID, cost: int | None = 100) -> None:  # type: ignore[no-untyped-def]
    conn.execute(
        text(
            "UPDATE network_operations SET closed_at = now(), response_status = 200, "
            "duration_ms = 42, bytes_compressed = 1000, bytes_decompressed = 4000, "
            "estimated_cost_micro_units = :cost, currency = 'USD' "
            "WHERE network_request_id = :nrid"
        ),
        {"cost": cost, "nrid": network_request_id},
    )


def _workspace(conn) -> uuid.UUID:  # type: ignore[no-untyped-def]
    ws_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO workspaces (id, name, slug, status, created_at, updated_at) "
            "VALUES (:id, :name, :slug, 'ACTIVE', now(), now())"
        ),
        {"id": ws_id, "name": f"ws-{ws_id.hex[:8]}", "slug": f"ws-{ws_id.hex[:8]}"},
    )
    return ws_id


class TestLiveEnforcement:
    """Behaviours only a real Postgres can demonstrate."""

    def test_update_on_a_closed_operation_is_rejected(self, engine) -> None:  # type: ignore[no-untyped-def]
        with engine.begin() as conn:
            nrid = _open_operation(conn)
            _close_operation(conn, nrid)
        with engine.begin() as conn:
            with pytest.raises(sa.exc.DBAPIError) as excinfo:
                conn.execute(
                    text(
                        "UPDATE network_operations SET failure_reason = 'rewritten' "
                        "WHERE network_request_id = :nrid"
                    ),
                    {"nrid": nrid},
                )
        assert "closed" in str(excinfo.value).lower()

    def test_intent_fields_are_immutable_even_before_close(self, engine) -> None:  # type: ignore[no-untyped-def]
        with engine.begin() as conn:
            nrid = _open_operation(conn)
        with engine.begin() as conn:
            with pytest.raises(sa.exc.DBAPIError) as excinfo:
                conn.execute(
                    text(
                        "UPDATE network_operations SET provider = 'switched' "
                        "WHERE network_request_id = :nrid"
                    ),
                    {"nrid": nrid},
                )
        assert "immutable" in str(excinfo.value).lower()

    def test_closing_an_open_operation_is_allowed_exactly_once(self, engine) -> None:  # type: ignore[no-untyped-def]
        with engine.begin() as conn:
            nrid = _open_operation(conn)
            _close_operation(conn, nrid)
            row = conn.execute(
                text(
                    "SELECT response_status, estimated_cost_micro_units, currency "
                    "FROM network_operations WHERE network_request_id = :nrid"
                ),
                {"nrid": nrid},
            ).one()
        assert row.response_status == 200
        assert row.estimated_cost_micro_units == 100
        assert row.currency == "USD"

    def test_settlement_update_is_rejected(self, engine) -> None:  # type: ignore[no-untyped-def]
        with engine.begin() as conn:
            nrid = _open_operation(conn)
            _close_operation(conn, nrid)
            conn.execute(
                text(
                    "INSERT INTO network_operation_settlements "
                    "(id, operation_id, settlement_version, reconciled_cost_micro_units, "
                    " currency, method, created_at) "
                    "VALUES (:id, :nrid, 1, 97, 'USD', 'EXACT', now())"
                ),
                {"id": uuid.uuid4(), "nrid": nrid},
            )
        with engine.begin() as conn:
            with pytest.raises(sa.exc.DBAPIError) as excinfo:
                conn.execute(
                    text(
                        "UPDATE network_operation_settlements "
                        "SET reconciled_cost_micro_units = 1 WHERE operation_id = :nrid"
                    ),
                    {"nrid": nrid},
                )
        assert "append-only" in str(excinfo.value).lower()

    def test_settlement_delete_is_rejected(self, engine) -> None:  # type: ignore[no-untyped-def]
        with engine.begin() as conn:
            nrid = _open_operation(conn)
            _close_operation(conn, nrid)
            conn.execute(
                text(
                    "INSERT INTO network_operation_settlements "
                    "(id, operation_id, settlement_version, reconciled_cost_micro_units, "
                    " currency, method, created_at) "
                    "VALUES (:id, :nrid, 1, 97, 'USD', 'PRO_RATA_BYTES', now())"
                ),
                {"id": uuid.uuid4(), "nrid": nrid},
            )
        with engine.begin() as conn:
            with pytest.raises(sa.exc.DBAPIError) as excinfo:
                conn.execute(
                    text(
                        "DELETE FROM network_operation_settlements WHERE operation_id = :nrid"
                    ),
                    {"nrid": nrid},
                )
        assert "append-only" in str(excinfo.value).lower()

    def test_corrections_append_a_new_version(self, engine) -> None:  # type: ignore[no-untyped-def]
        with engine.begin() as conn:
            nrid = _open_operation(conn)
            _close_operation(conn, nrid)
            for version, cost, method in ((1, 97, "EXACT"), (2, 103, "CORRECTION")):
                conn.execute(
                    text(
                        "INSERT INTO network_operation_settlements "
                        "(id, operation_id, settlement_version, "
                        " reconciled_cost_micro_units, currency, method, created_at) "
                        "VALUES (:id, :nrid, :v, :cost, 'USD', :method, now())"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "nrid": nrid,
                        "v": version,
                        "cost": cost,
                        "method": method,
                    },
                )
            current = conn.execute(
                text(
                    "SELECT reconciled_cost_micro_units FROM network_operation_settlements "
                    "WHERE operation_id = :nrid ORDER BY settlement_version DESC LIMIT 1"
                ),
                {"nrid": nrid},
            ).scalar_one()
        assert current == 103

    def test_largest_remainder_allocations_satisfy_the_deferred_total(self, engine) -> None:  # type: ignore[no-untyped-def]
        """Three workspaces, one operation, 100 micro-USD — the whole
        set commits because largest-remainder makes 34+33+33 exact."""
        with engine.begin() as conn:
            nrid = _open_operation(conn)
            _close_operation(conn, nrid, cost=100)
            workspaces = [_workspace(conn) for _ in range(3)]
            parts = allocate_cost_largest_remainder(100, [1, 1, 1])
            fractions = allocate_cost_largest_remainder(FRACTION_SCALE, [1, 1, 1])
            for ws_id, part, fraction in zip(workspaces, parts, fractions, strict=True):
                conn.execute(
                    text(
                        "INSERT INTO network_operation_allocations "
                        "(id, operation_id, workspace_id, fraction_ppb, "
                        " allocated_cost_micro_units, currency, created_at) "
                        "VALUES (:id, :nrid, :ws, :fraction, :cost, 'USD', now())"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "nrid": nrid,
                        "ws": ws_id,
                        "fraction": fraction,
                        "cost": part,
                    },
                )
        with engine.connect() as conn:
            total = conn.execute(
                text(
                    "SELECT SUM(allocated_cost_micro_units) "
                    "FROM network_operation_allocations WHERE operation_id = :nrid"
                ),
                {"nrid": nrid},
            ).scalar_one()
        assert total == 100

    def test_allocation_shortfall_is_rejected_at_commit_not_at_insert(self, engine) -> None:  # type: ignore[no-untyped-def]
        """The constraint is DEFERRED: each naive 33+33+33 INSERT succeeds,
        and only COMMIT rejects the set. That is the whole point — an
        allocation set is written a row at a time and is only meaningful
        complete."""
        conn = engine.connect()
        trans = conn.begin()
        nrid = _open_operation(conn)
        _close_operation(conn, nrid, cost=100)
        fraction = FRACTION_SCALE // 3
        for _ in range(3):
            ws_id = _workspace(conn)
            # Each of these INSERTs must SUCCEED — a non-deferred check
            # would blow up on the first row.
            conn.execute(
                text(
                    "INSERT INTO network_operation_allocations "
                    "(id, operation_id, workspace_id, fraction_ppb, "
                    " allocated_cost_micro_units, currency, created_at) "
                    "VALUES (:id, :nrid, :ws, :fraction, 33, 'USD', now())"
                ),
                {"id": uuid.uuid4(), "nrid": nrid, "ws": ws_id, "fraction": fraction},
            )
        with pytest.raises(sa.exc.DBAPIError) as excinfo:
            trans.commit()
        try:
            trans.rollback()
        except Exception:  # pragma: no cover - transaction already dead
            pass
        conn.close()
        assert "allocation" in str(excinfo.value).lower()

    def test_single_tenant_operation_takes_one_full_allocation(self, engine) -> None:  # type: ignore[no-untyped-def]
        with engine.begin() as conn:
            nrid = _open_operation(conn)
            _close_operation(conn, nrid, cost=4_237)
            ws_id = _workspace(conn)
            conn.execute(
                text(
                    "INSERT INTO network_operation_allocations "
                    "(id, operation_id, workspace_id, fraction_ppb, "
                    " allocated_cost_micro_units, currency, created_at) "
                    "VALUES (:id, :nrid, :ws, :fraction, 4237, 'USD', now())"
                ),
                {
                    "id": uuid.uuid4(),
                    "nrid": nrid,
                    "ws": ws_id,
                    "fraction": FRACTION_SCALE,
                },
            )
        with engine.connect() as conn:
            fraction = conn.execute(
                text(
                    "SELECT fraction_ppb FROM network_operation_allocations "
                    "WHERE operation_id = :nrid"
                ),
                {"nrid": nrid},
            ).scalar_one()
        assert fraction == FRACTION_SCALE

    def test_rls_is_forced_on_allocations_and_absent_elsewhere(self, engine) -> None:  # type: ignore[no-untyped-def]
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname IN "
                    "('network_operations', 'network_operation_allocations', "
                    " 'network_operation_settlements')"
                )
            ).all()
            policies = conn.execute(
                text(
                    "SELECT c.relname, p.polname FROM pg_policy p "
                    "JOIN pg_class c ON c.oid = p.polrelid "
                    "WHERE c.relname LIKE 'network_operation%'"
                )
            ).all()
        by_name = {r.relname: r for r in rows}
        assert by_name["network_operation_allocations"].relrowsecurity is True
        assert by_name["network_operation_allocations"].relforcerowsecurity is True
        assert by_name["network_operations"].relrowsecurity is False
        assert by_name["network_operation_settlements"].relrowsecurity is False
        assert {p.relname for p in policies} == {"network_operation_allocations"}

    def test_request_attempt_fk_targets_the_operation_unique_key(self, engine) -> None:  # type: ignore[no-untyped-def]
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT a.attname, a.attnotnull FROM pg_attribute a "
                    "JOIN pg_class c ON c.oid = a.attrelid "
                    "WHERE c.relname = 'request_attempts' "
                    "AND a.attname = 'network_operation_id'"
                )
            ).one()
            # One row per relation carrying the constraint: the partitioned
            # PARENT plus every partition it propagated to. All of them must
            # point at the same referenced table — a partition whose FK
            # pointed elsewhere would be an isolation-shaped surprise of
            # exactly the kind the partition-RLS work already found once.
            targets = conn.execute(
                text(
                    "SELECT DISTINCT confrelid::regclass::text AS target "
                    "FROM pg_constraint WHERE conname = "
                    "'fk_request_attempts_network_operation_id_network_operations'"
                )
            ).scalars().all()
        assert row.attnotnull is False
        assert set(targets) == {"network_operations"}

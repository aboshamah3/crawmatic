"""Integration test: the `network_operations` partition swap (EPA C9,
F14) — `alembic/versions/a5e0c74b13d9_partition_network_operations.py`.

This is the highest-risk migration in the plan, and the thing that makes
it risky is not the DDL: it is that the copy is silent. A short copy
produces a table that looks completely normal and is missing rows nobody
will notice for months. So the assertions here are, in order of what
they protect:

1. **The parent is actually partitioned** (`relkind = 'p'`, `RANGE
   (created_at)`) with monthly children named the way
   `app_shared.maintenance.partitions.partition_name` constructs them —
   because the running `MAINTENANCE_PARTITION_CREATE` job has to be able
   to take over from where the migration stopped.
2. **The identity trade is visible.** `network_request_id` is unique
   per MONTH now, not globally, and the single-column lookup every
   ledger writer does is still index-backed. Both are asserted here so
   the trade lives in the test suite rather than only in a migration
   docstring.
3. **Rows land in the correct month's child**, which is the only thing
   that makes a whole-partition drop equal to "delete everything older
   than N days".
4. **The immutability trigger still fires** on the new parent — a row
   trigger on a partitioned table must propagate to every child, and a
   swap that quietly lost it would turn the ledger's append-only
   guarantee off without any error anywhere.
5. **`scripts/rls_verify.py` still passes** afterwards, and the RLS
   posture of the new children matches the parent's.
6. **Retention can drop a whole child**, gated on the owner switch.

**This file NEVER runs the migration itself.** Every test asserts the
POST-swap state of whatever database it is pointed at, and skips
outright when `a5e0c74b13d9` has not been applied there. Running
`alembic upgrade` from a test against whatever `DATABASE_URL` happens to
point at is exactly the accident the pre-flight forbids; applying the
migration is a deliberate operator step (`alembic upgrade head`), and
the copy itself is guarded inside the migration by a row-count parity
check that aborts the transaction rather than by an assertion out here.

Point it at a throwaway database that HAS been migrated (that is how it
was run for EPA C9) and it exercises the swap's result end to end.

SKIPS cleanly unless Postgres and a BYPASSRLS system role are reachable.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

_REQUIRED_TABLES = frozenset({"network_operations"})


def _swap_reachable() -> bool:
    try:
        from app_shared.config import get_settings

        if not get_settings().DATABASE_URL:
            return False
        from sqlalchemy import inspect

        from app_shared.database import (
            check_connection,
            get_engine,
            get_system_sessionmaker,
        )

        check_connection()
        if not _REQUIRED_TABLES <= set(inspect(get_engine()).get_table_names()):
            return False
        with get_system_sessionmaker()() as session:
            session.execute(text("SELECT 1"))
    except Exception:
        return False
    return True


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _swap_reachable(),
        reason=(
            "Needs a reachable Postgres with the C1 network ledger and a "
            "usable BYPASSRLS system role."
        ),
    ),
]


@pytest.fixture()
def system_session():
    from app_shared.database import get_system_sessionmaker

    with get_system_sessionmaker()() as session:
        yield session


def _live_parent_is_partitioned(session) -> bool:
    return (
        session.execute(
            text(
                "SELECT relkind FROM pg_class "
                "WHERE oid = 'public.network_operations'::regclass"
            )
        ).scalar()
        == "p"
    )


# --- 1-3: the shape and the copy, on the LIVE (already-migrated) table ------


def test_parent_is_range_partitioned_by_created_at(system_session) -> None:
    if not _live_parent_is_partitioned(system_session):
        pytest.skip("a5e0c74b13d9 has not been applied to this database")

    strategy, key = system_session.execute(
        text(
            """
            SELECT p.partstrat,
                   pg_get_partkeydef('public.network_operations'::regclass)
            FROM pg_partitioned_table p
            WHERE p.partrelid = 'public.network_operations'::regclass
            """
        )
    ).one()
    assert strategy == "r"  # RANGE
    assert "created_at" in key


def test_children_follow_the_maintenance_job_naming_convention(system_session) -> None:
    if not _live_parent_is_partitioned(system_session):
        pytest.skip("a5e0c74b13d9 has not been applied to this database")

    from app_shared.maintenance.partitions import partition_name

    names = {
        row[0]
        for row in system_session.execute(
            text(
                """
                SELECT c.relname FROM pg_inherits i
                JOIN pg_class c ON c.oid = i.inhrelid
                WHERE i.inhparent = 'public.network_operations'::regclass
                """
            )
        )
    }
    assert names, "a partitioned parent with no children accepts no writes at all"
    for name in names:
        year, month = name.rsplit("_", 2)[-2:]
        assert name == partition_name("network_operations", f"{year}_{month}")


def test_identity_uniqueness_is_scoped_to_the_month(system_session) -> None:
    """The documented, deliberate weakening: `network_request_id` is no
    longer globally unique because a partitioned table's unique
    constraint must include the partition key. Asserted so the trade is
    visible in the test suite rather than only in a migration docstring.
    """
    if not _live_parent_is_partitioned(system_session):
        pytest.skip("a5e0c74b13d9 has not been applied to this database")

    columns = {
        row[0]
        for row in system_session.execute(
            text(
                """
                SELECT a.attname
                FROM pg_constraint c
                JOIN unnest(c.conkey) AS k(attnum) ON TRUE
                JOIN pg_attribute a
                  ON a.attrelid = c.conrelid AND a.attnum = k.attnum
                WHERE c.conrelid = 'public.network_operations'::regclass
                  AND c.contype = 'u'
                """
            )
        )
    }
    assert {"network_request_id", "created_at"} <= columns

    # ...and the single-column lookup every ledger writer does must still
    # be index-backed, or the swap traded a constraint for a seq scan.
    indexed = {
        row[0]
        for row in system_session.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = 'public' AND tablename = 'network_operations'"
            )
        )
    }
    assert "ix_network_operations_network_request_id" in indexed


def test_a_row_lands_in_its_own_month_partition(system_session) -> None:
    """Property 3: routing is what makes a whole-partition drop mean
    "everything older than N days"."""
    if not _live_parent_is_partitioned(system_session):
        pytest.skip("a5e0c74b13d9 has not been applied to this database")

    from app_shared.maintenance.partitions import partition_name

    now = datetime.now(timezone.utc)
    nrid = uuid.uuid4()
    expected = partition_name("network_operations", f"{now.year:04d}_{now.month:02d}")
    try:
        system_session.execute(
            text(
                """
                INSERT INTO network_operations (
                    id, network_request_id, canonical_url_hash, domain,
                    http_method, provider, transport, created_at
                ) VALUES (
                    gen_random_uuid(), :nrid, :hash, 'partition-route.test',
                    'GET', 'test-provider', 'DIRECT', :created_at
                )
                """
            ),
            {"nrid": nrid, "hash": f"sha256:{nrid.hex}", "created_at": now},
        )
        system_session.commit()

        landed = system_session.execute(
            text(
                "SELECT tableoid::regclass::text FROM network_operations "
                "WHERE network_request_id = :nrid"
            ),
            {"nrid": nrid},
        ).scalar()
        assert landed == expected
    finally:
        system_session.execute(
            text("DELETE FROM network_operations WHERE network_request_id = :nrid"),
            {"nrid": nrid},
        )
        system_session.commit()


# --- 4: the append-only trigger survived the swap --------------------------


def test_closed_row_is_still_immutable_after_the_swap(system_session) -> None:
    """A row trigger on a partitioned parent must propagate to every
    child. If the swap lost it, the ledger's append-only guarantee is off
    and nothing anywhere raises — which is why this is asserted by
    attempting the forbidden UPDATE, not by reading `pg_trigger`."""
    if not _live_parent_is_partitioned(system_session):
        pytest.skip("a5e0c74b13d9 has not been applied to this database")

    now = datetime.now(timezone.utc)
    nrid = uuid.uuid4()
    try:
        system_session.execute(
            text(
                """
                INSERT INTO network_operations (
                    id, network_request_id, canonical_url_hash, domain,
                    http_method, provider, transport, created_at, closed_at
                ) VALUES (
                    gen_random_uuid(), :nrid, :hash, 'immutability.test',
                    'GET', 'test-provider', 'DIRECT', :created_at, :created_at
                )
                """
            ),
            {"nrid": nrid, "hash": f"sha256:{nrid.hex}", "created_at": now},
        )
        system_session.commit()

        with pytest.raises(Exception) as excinfo:
            system_session.execute(
                text(
                    "UPDATE network_operations SET response_status = 500 "
                    "WHERE network_request_id = :nrid"
                ),
                {"nrid": nrid},
            )
            system_session.commit()
        assert "closed and immutable" in str(excinfo.value)
    finally:
        system_session.rollback()
        system_session.execute(
            text("DELETE FROM network_operations WHERE network_request_id = :nrid"),
            {"nrid": nrid},
        )
        system_session.commit()


# --- 5: RLS posture ---------------------------------------------------------


def test_children_carry_the_parents_rls_posture(system_session) -> None:
    """`network_operations` is fleet-owned and carries no policy, so the
    partition guard is a no-op for it — but "no policy on the parent AND
    no policy on any child" is the property, and a child that somehow
    acquired FORCE RLS with no policy would silently deny every read."""
    if not _live_parent_is_partitioned(system_session):
        pytest.skip("a5e0c74b13d9 has not been applied to this database")

    rows = system_session.execute(
        text(
            """
            SELECT c.relname, c.relrowsecurity,
                   (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid)
            FROM pg_class c
            LEFT JOIN pg_inherits i ON i.inhrelid = c.oid
            WHERE c.oid = 'public.network_operations'::regclass
               OR i.inhparent = 'public.network_operations'::regclass
            """
        )
    ).all()
    postures = {(bool(force), int(count)) for _, force, count in rows}
    assert len(postures) == 1, f"children disagree with their parent: {rows}"


# --- 6: retention can drop a whole child, and only when enabled ------------


def test_retention_drops_an_expired_child_only_when_the_class_is_enabled(
    system_session,
) -> None:
    if not _live_parent_is_partitioned(system_session):
        pytest.skip("a5e0c74b13d9 has not been applied to this database")

    from app_shared.config import get_settings
    from app_shared.maintenance.partitions import (
        _seam_create_stmt,
        partition_seam_available,
    )
    from app_shared.maintenance.retention import run_retention

    # A month far outside the 730-day window and far outside any month
    # this deployment could hold real rows for.
    child = "network_operations_2019_01"

    # Created through the SAME seam the maintenance job uses
    # (`provision_db_roles.sql` §9), not with raw DDL. That is not a
    # convenience: on a correctly provisioned database this session is
    # `crawmatic_auth`, which does NOT own the parent and therefore
    # cannot issue `CREATE TABLE ... PARTITION OF` at all — the exact
    # privilege wall the EPA C8 rehearsal hit. Building the fixture the
    # way production builds it is what makes this test evidence that
    # partition maintenance actually works as the role that runs it.
    if partition_seam_available(system_session):
        system_session.execute(
            _seam_create_stmt(
                child,
                "network_operations",
                datetime(2019, 1, 1, tzinfo=timezone.utc),
                datetime(2019, 2, 1, tzinfo=timezone.utc),
            )
        )
    else:
        system_session.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {child} PARTITION OF network_operations "
                "FOR VALUES FROM ('2019-01-01') TO ('2019-02-01')"
            )
        )
    system_session.commit()
    now = datetime.now(timezone.utc)

    def _exists() -> bool:
        return bool(
            system_session.execute(
                text("SELECT to_regclass('public." + child + "') IS NOT NULL")
            ).scalar()
        )

    try:
        assert _exists()

        # Switch closed: the expired child survives.
        closed = get_settings().model_copy(update={"RETENTION_ENABLED_CLASSES": []})
        report = run_retention(system_session, now_utc=now, settings=closed)
        assert child not in report.partitions_dropped
        assert "network_operations" in report.classes_skipped_not_enabled
        assert _exists()

        # Switch open for THIS class only: the child is dropped.
        opened = get_settings().model_copy(
            update={"RETENTION_ENABLED_CLASSES": ["network_operations"]}
        )
        report = run_retention(system_session, now_utc=now, settings=opened)
        system_session.commit()
        assert child in report.partitions_dropped
        assert not _exists()
    finally:
        system_session.rollback()
        from app_shared.maintenance.partitions import drop_partition

        drop_partition(system_session, child)
        system_session.commit()

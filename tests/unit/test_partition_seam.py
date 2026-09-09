"""Unit tests for the partition-maintenance seam (EPA C9, F14) —
`app_shared.maintenance.partitions.partition_seam_available` and the two
statements that route through `provision_db_roles.sql` §9.

Why the seam exists, in one paragraph, because the tests below only make
sense with it: creating or reclaiming a partition requires OWNERSHIP of
the parent — PostgreSQL has no grantable "attach a partition" privilege.
`provision_db_roles.sql` §8 correctly moves ownership of every table to
`crawmatic_migrate`, a role no service logs in as, while the maintenance
job runs as `crawmatic_auth`. The EPA C8 rehearsal found the result: on a
correctly provisioned database, partition creation and retention were
both broken, silently, forever. §9 grants exactly those two operations
back through SECURITY DEFINER functions; this module is the caller.

Pure and DB-independent. Three properties:

1. **The probe is TOTAL.** A session that raises, returns nothing, or
   knows nothing about `to_regprocedure` must read as "no seam" — never
   propagate. The fallback is always at least as capable as the seam, so
   a maintenance job that crashed on a capability probe would be strictly
   worse than one that never probed.
2. **Absent seam ⇒ byte-identical legacy behaviour.** Every database
   provisioned before §9 must keep issuing exactly the DDL it always did.
3. **Present seam ⇒ the function call, with the same arguments.** The
   bound parameters must carry the same parent, child and half-open
   bounds the direct DDL rendered as literals.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.dialects import postgresql

from app_shared.maintenance.partitions import (
    _create_partition_stmt,
    _drop_partition_stmt,
    _seam_create_stmt,
    _seam_drop_stmt,
    _seam_probe_stmt,
    create_missing_partitions,
    drop_partition,
    partition_name,
    partition_seam_available,
)
from app_shared.maintenance.registry import PARTITIONED_TABLES


def _compiled(stmt) -> str:
    return str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


class _Result:
    def __init__(self, value) -> None:
        self._value = value

    def scalar(self):
        return self._value


class _SeamSession:
    """Answers the `to_regclass` gate from a set and the seam probe from
    a flag, recording everything else."""

    def __init__(self, existing: set[str], *, seam: bool) -> None:
        self.existing = existing
        self.seam = seam
        self.executed: list[str] = []

    def execute(self, stmt, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        sql = _compiled(stmt)
        if "to_regclass" in sql:
            qualified = stmt.compile().params["qualified_name"]
            name = qualified.split(".", 1)[1]
            return _Result(name if name in self.existing else None)
        if "to_regprocedure" in sql:
            return _Result(self.seam)
        self.executed.append(sql)
        return _Result(None)


class _RaisingProbeSession(_SeamSession):
    """A session whose probe blows up — a role with no catalog access, or
    a fake that refuses unrecognised statements."""

    def execute(self, stmt, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        sql = _compiled(stmt)
        if "to_regprocedure" in sql:
            raise RuntimeError("permission denied for schema pg_catalog")
        return super().execute(stmt, *args, **kwargs)


# --- 1. the probe is total --------------------------------------------------


def test_probe_sql_uses_to_regprocedure_for_both_functions() -> None:
    sql = _compiled(_seam_probe_stmt())
    assert "to_regprocedure" in sql
    assert "crawmatic_create_partition(text, text, text, text)" in sql
    assert "crawmatic_drop_partition(text)" in sql


def test_probe_returns_false_when_the_session_raises() -> None:
    session = _RaisingProbeSession(set(), seam=True)
    assert partition_seam_available(session) is False


def test_probe_reflects_the_catalog_answer() -> None:
    assert partition_seam_available(_SeamSession(set(), seam=True)) is True
    assert partition_seam_available(_SeamSession(set(), seam=False)) is False


# --- 2. absent seam => unchanged legacy DDL ---------------------------------


def test_without_the_seam_creation_issues_the_original_ddl() -> None:
    existing = {entry.name for entry in PARTITIONED_TABLES}
    session = _SeamSession(existing, seam=False)
    now = datetime(2026, 7, 15, tzinfo=timezone.utc)

    create_missing_partitions(session, now_utc=now, lookahead_months=0)

    child = partition_name("price_observations", "2026_07")
    assert any(f"CREATE TABLE IF NOT EXISTS {child} PARTITION OF" in s for s in session.executed)
    assert not any("crawmatic_create_partition" in s for s in session.executed)


def test_without_the_seam_reclaim_issues_the_original_ddl() -> None:
    session = _SeamSession(set(), seam=False)
    drop_partition(session, "price_observations_2026_01")
    assert session.executed == [_compiled(_drop_partition_stmt("price_observations_2026_01"))]


# --- 3. present seam => the function call, same arguments -------------------


def test_with_the_seam_creation_calls_the_function() -> None:
    existing = {entry.name for entry in PARTITIONED_TABLES}
    session = _SeamSession(existing, seam=True)
    now = datetime(2026, 12, 20, tzinfo=timezone.utc)

    create_missing_partitions(session, now_utc=now, lookahead_months=1)

    assert all("crawmatic_create_partition" in s for s in session.executed if "PARTITION" in s or "crawmatic" in s)
    assert not any("CREATE TABLE IF NOT EXISTS" in s for s in session.executed)


def test_with_the_seam_reclaim_calls_the_function() -> None:
    session = _SeamSession(set(), seam=True)
    drop_partition(session, "price_observations_2026_01")
    assert len(session.executed) == 1
    assert "crawmatic_drop_partition" in session.executed[0]
    assert "price_observations_2026_01" in session.executed[0]


def test_seam_call_carries_the_same_bounds_the_direct_ddl_rendered() -> None:
    """The Dec->Jan rollover, because that is where an off-by-one month
    would actually show up."""
    start = datetime(2026, 12, 1, tzinfo=timezone.utc)
    end = datetime(2027, 1, 1, tzinfo=timezone.utc)

    direct = _compiled(_create_partition_stmt("t_2026_12", "t", start, end))
    seam = _compiled(_seam_create_stmt("t_2026_12", "t", start, end))

    for fragment in ("t_2026_12", "'2026-12-01'", "'2027-01-01'"):
        assert fragment in direct
        assert fragment in seam


def test_seam_is_probed_once_per_run_not_once_per_partition() -> None:
    """A catalog read whose answer cannot change mid-run. Probing per
    partition would multiply round trips by the registry's size for no
    information at all."""

    class _CountingSession(_SeamSession):
        probes = 0

        def execute(self, stmt, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            if "to_regprocedure" in _compiled(stmt):
                type(self).probes += 1
            return super().execute(stmt, *args, **kwargs)

    _CountingSession.probes = 0
    session = _CountingSession({entry.name for entry in PARTITIONED_TABLES}, seam=True)
    create_missing_partitions(
        session, now_utc=datetime(2026, 12, 20, tzinfo=timezone.utc), lookahead_months=1
    )
    assert _CountingSession.probes == 1

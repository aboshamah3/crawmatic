"""Integration test: ledger child summarization (EPA C9, F14) —
`app_shared.maintenance.ledger_summaries`, `docs/RETENTION_POLICY.md`
§2.9.b.

Exercises the summarise-then-delete family end to end against a real
Postgres. Four properties, and the middle two are the ones that make the
family safe rather than merely small:

1. **The owner switch defaults closed.** With
   `RETENTION_ENABLED_CLASSES` empty, a settled, month-old parent with
   children is untouched and the run reports `class_not_enabled`.
2. **An UNSETTLED parent keeps every child, however old.** Provider
   reconciliation apportions a period charge across operations *by their
   transport-observed bytes*, and a navigation's bytes are its
   children's; summarising before the invoice lands would make the
   eventual settlement quietly wrong. The parent is counted in
   `parents_deferred_unsettled` instead.
3. **A SETTLED parent's children are summarised and removed, and the
   totals are equal before and after** — child count, per-host-class
   bytes and summed duration all survive on the
   `network_operation_resource_summaries` row, and the PARENT's own
   `bytes_compressed`/`duration_ms`/`estimated_cost_micro_units` are
   byte-for-byte unchanged (they are immutable-by-trigger facts about
   the navigation, and summarising its children must not restate them).
4. **Dry run computes the identical summary and writes nothing** — the
   rehearsal mode D1 drives.

Needs a reachable Postgres with the C1 ledger tables and the EPA C9
`network_operation_resource_summaries` migration (`d1f7a3c9e284`)
applied, plus a usable BYPASSRLS system role. SKIPS cleanly otherwise
(never faked).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

_REQUIRED_TABLES = frozenset(
    {
        "network_operations",
        "network_operation_settlements",
        "network_operation_resource_summaries",
    }
)

PARENT_DOMAIN = "shop.example.test"
THIRD_PARTY_DOMAIN = "cdn.other-example.test"


def _ledger_summaries_reachable() -> bool:
    """Best-effort probe: Postgres with the C1 ledger + the C9 summary
    table, and a usable BYPASSRLS system session."""
    try:
        from app_shared.config import get_settings

        settings = get_settings()
    except Exception:
        return False

    if not settings.DATABASE_URL:
        return False

    try:
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
        not _ledger_summaries_reachable(),
        reason=(
            "Needs a reachable Postgres with the C1 network ledger and the "
            "EPA C9 network_operation_resource_summaries migration "
            "(d1f7a3c9e284) applied, plus a usable BYPASSRLS system role."
        ),
    ),
]


# --- seeding helpers --------------------------------------------------------


def _settings(enabled: str | None):
    """A `Settings` copy with the retention switch set as the test needs.

    `model_copy(update=...)` rather than env mutation: the process-wide
    `get_settings()` singleton is cached, and a test that mutated the
    environment would leak into every other test in the session.
    """
    from app_shared.config import get_settings

    return get_settings().model_copy(
        update={"RETENTION_ENABLED_CLASSES": [] if enabled is None else [enabled]}
    )


def _insert_operation(
    session,
    *,
    network_request_id: uuid.UUID,
    created_at: datetime,
    domain: str,
    parent_operation_id: uuid.UUID | None = None,
    bytes_compressed: int | None = None,
    duration_ms: int | None = None,
    estimated_cost_micro_units: int | None = None,
) -> None:
    session.execute(
        text(
            """
            INSERT INTO network_operations (
                id, network_request_id, parent_operation_id, canonical_url_hash,
                domain, http_method, provider, transport, created_at, closed_at,
                bytes_compressed, duration_ms, estimated_cost_micro_units, currency
            ) VALUES (
                gen_random_uuid(), :nrid, :parent, :hash,
                :domain, 'GET', 'test-provider', 'BROWSER', :created_at, :created_at,
                :bytes, :duration, :cost, :currency
            )
            """
        ),
        {
            "nrid": network_request_id,
            "parent": parent_operation_id,
            "hash": f"sha256:{network_request_id.hex}",
            "domain": domain,
            "created_at": created_at,
            "bytes": bytes_compressed,
            "duration": duration_ms,
            "cost": estimated_cost_micro_units,
            # An explicit parameter rather than a `CASE WHEN :cost IS NULL`
            # expression: reusing one placeholder in two positions leaves
            # Postgres unable to infer its type at all when the value is
            # NULL (`could not determine data type of parameter`), and the
            # `ck_network_operations_no_cost_requires_currency` check is
            # what makes the pairing load-bearing.
            "currency": None if estimated_cost_micro_units is None else "USD",
        },
    )


def _settle(session, operation_id: uuid.UUID) -> None:
    session.execute(
        text(
            """
            INSERT INTO network_operation_settlements (
                id, operation_id, settlement_version, reconciled_cost_micro_units,
                currency, method
            ) VALUES (gen_random_uuid(), :op, 1, 1000, 'USD', 'EXACT')
            """
        ),
        {"op": operation_id},
    )


def _seed_navigation(session, *, created_at: datetime, settled: bool):
    """One parent navigation plus three children: two first-party (one of
    them on a SUBDOMAIN, which must still classify first-party) and one
    third-party. Returns the parent id and the expected aggregate."""
    parent_id = uuid.uuid4()
    _insert_operation(
        session,
        network_request_id=parent_id,
        created_at=created_at,
        domain=PARENT_DOMAIN,
        bytes_compressed=1_000,
        duration_ms=250,
        estimated_cost_micro_units=7_500,
    )
    children = [
        (PARENT_DOMAIN, 400, 40),
        (f"assets.{PARENT_DOMAIN}", 600, 60),
        (THIRD_PARTY_DOMAIN, 900, 90),
    ]
    for domain, nbytes, duration in children:
        _insert_operation(
            session,
            network_request_id=uuid.uuid4(),
            created_at=created_at + timedelta(seconds=1),
            domain=domain,
            parent_operation_id=parent_id,
            bytes_compressed=nbytes,
            duration_ms=duration,
        )
    if settled:
        _settle(session, parent_id)
    session.commit()
    return parent_id


def _cleanup(session, parent_ids: list[uuid.UUID]) -> None:
    for parent_id in parent_ids:
        session.execute(
            text(
                "DELETE FROM network_operation_resource_summaries "
                "WHERE parent_operation_id = :p"
            ),
            {"p": parent_id},
        )
        # Settlement rows are deliberately NOT cleaned up: the
        # `trg_network_operation_settlements_append_only` trigger rejects
        # DELETE by design, and disabling it would need table ownership
        # the maintenance role correctly does not have. What is left
        # behind is precisely the orphan this schema tolerates — a
        # settlement naming an operation that is gone — which is the
        # condition `ledger_summaries.find_orphan_references` REPORTS
        # rather than repairs. Every assertion in this file is scoped to
        # its own freshly-minted `parent_operation_id`, so the residue
        # cannot leak between tests.
        session.execute(
            text("DELETE FROM network_operations WHERE parent_operation_id = :p"),
            {"p": parent_id},
        )
        session.execute(
            text("DELETE FROM network_operations WHERE network_request_id = :p"),
            {"p": parent_id},
        )
    session.commit()


def _child_count(session, parent_id: uuid.UUID) -> int:
    return int(
        session.execute(
            text(
                "SELECT count(*) FROM network_operations "
                "WHERE parent_operation_id = :p"
            ),
            {"p": parent_id},
        ).scalar()
    )


def _parent_totals(session, parent_id: uuid.UUID):
    return session.execute(
        text(
            "SELECT bytes_compressed, duration_ms, estimated_cost_micro_units "
            "FROM network_operations WHERE network_request_id = :p"
        ),
        {"p": parent_id},
    ).one()


@pytest.fixture()
def system_session():
    from app_shared.database import get_system_sessionmaker

    with get_system_sessionmaker()() as session:
        yield session


# --- the tests --------------------------------------------------------------


def test_class_not_enabled_leaves_everything_alone(system_session) -> None:
    """Property 1 — the ratification switch defaults closed."""
    from app_shared.maintenance.ledger_summaries import run_ledger_child_summarization

    now = datetime.now(timezone.utc)
    parent_id = _seed_navigation(
        system_session, created_at=now - timedelta(days=90), settled=True
    )
    try:
        report = run_ledger_child_summarization(
            system_session, now_utc=now, settings=_settings(None)
        )
        assert report.class_not_enabled is True
        assert report.parents_summarized == []
        assert _child_count(system_session, parent_id) == 3
    finally:
        _cleanup(system_session, [parent_id])


def test_unsettled_parent_keeps_its_children(system_session) -> None:
    """Property 2 — age alone is never enough.

    The parent is 90 days old against a 30-day window, so gate 1 passes
    comfortably. Gate 2 (a settlement row exists) does not, and that
    single fact is what keeps the rows the eventual invoice will be
    reconciled against.
    """
    from app_shared.maintenance.ledger_summaries import run_ledger_child_summarization

    now = datetime.now(timezone.utc)
    parent_id = _seed_navigation(
        system_session, created_at=now - timedelta(days=90), settled=False
    )
    try:
        report = run_ledger_child_summarization(
            system_session,
            now_utc=now,
            settings=_settings("network_operation_children"),
        )
        assert str(parent_id) not in report.parents_summarized
        assert report.parents_deferred_unsettled >= 1
        assert _child_count(system_session, parent_id) == 3
        assert (
            system_session.execute(
                text(
                    "SELECT count(*) FROM network_operation_resource_summaries "
                    "WHERE parent_operation_id = :p"
                ),
                {"p": parent_id},
            ).scalar()
            == 0
        )
    finally:
        _cleanup(system_session, [parent_id])


def test_settled_parent_is_summarized_and_totals_are_equal(system_session) -> None:
    """Property 3 — the totals survive the rows.

    Before: three children carrying 400+600+900 = 1900 bytes and
    40+60+90 = 190 ms. After: zero children and one summary row carrying
    exactly those numbers, split first_party 1000 / third_party 900 —
    the subdomain child classifying first_party is the part a naive
    string equality would get wrong.
    """
    from app_shared.maintenance.ledger_summaries import run_ledger_child_summarization

    now = datetime.now(timezone.utc)
    parent_id = _seed_navigation(
        system_session, created_at=now - timedelta(days=45), settled=True
    )
    try:
        before = _parent_totals(system_session, parent_id)

        report = run_ledger_child_summarization(
            system_session,
            now_utc=now,
            settings=_settings("network_operation_children"),
        )

        assert str(parent_id) in report.parents_summarized
        assert _child_count(system_session, parent_id) == 0

        row = system_session.execute(
            text(
                "SELECT child_count, bytes_by_host_class, bytes_total, "
                "duration_ms_sum FROM network_operation_resource_summaries "
                "WHERE parent_operation_id = :p"
            ),
            {"p": parent_id},
        ).one()
        child_count, by_host, bytes_total, duration_sum = row

        assert child_count == 3
        assert bytes_total == 1900
        assert duration_sum == 190
        assert by_host == {"first_party": 1000, "third_party": 900}
        assert sum(by_host.values()) == bytes_total

        # The parent is untouched — immutable by trigger, and summarising
        # its children must not restate what the navigation itself did.
        assert _parent_totals(system_session, parent_id) == before
    finally:
        _cleanup(system_session, [parent_id])


def test_rerun_is_a_no_op(system_session) -> None:
    """Idempotence: the summary row makes the parent ineligible, so a
    second pass neither duplicates it nor raises on the unique key."""
    from app_shared.maintenance.ledger_summaries import run_ledger_child_summarization

    now = datetime.now(timezone.utc)
    parent_id = _seed_navigation(
        system_session, created_at=now - timedelta(days=45), settled=True
    )
    try:
        settings = _settings("network_operation_children")
        run_ledger_child_summarization(system_session, now_utc=now, settings=settings)
        second = run_ledger_child_summarization(
            system_session, now_utc=now, settings=settings
        )
        assert str(parent_id) not in second.parents_summarized
        assert (
            system_session.execute(
                text(
                    "SELECT count(*) FROM network_operation_resource_summaries "
                    "WHERE parent_operation_id = :p"
                ),
                {"p": parent_id},
            ).scalar()
            == 1
        )
    finally:
        _cleanup(system_session, [parent_id])


def test_dry_run_computes_the_same_summary_and_writes_nothing(system_session) -> None:
    """Property 4 — the rehearsal mode returns exactly what the real run
    would have persisted, which is what makes a D1 rehearsal meaningful
    rather than merely reassuring."""
    from app_shared.maintenance.ledger_summaries import run_ledger_child_summarization

    now = datetime.now(timezone.utc)
    parent_id = _seed_navigation(
        system_session, created_at=now - timedelta(days=45), settled=True
    )
    try:
        settings = _settings("network_operation_children")
        dry = run_ledger_child_summarization(
            system_session, now_utc=now, settings=settings, dry_run=True
        )
        dry_summary = next(
            s for s in dry.summaries if str(s.parent_operation_id) == str(parent_id)
        )
        assert _child_count(system_session, parent_id) == 3
        assert (
            system_session.execute(
                text(
                    "SELECT count(*) FROM network_operation_resource_summaries "
                    "WHERE parent_operation_id = :p"
                ),
                {"p": parent_id},
            ).scalar()
            == 0
        )

        wet = run_ledger_child_summarization(
            system_session, now_utc=now, settings=settings
        )
        wet_summary = next(
            s for s in wet.summaries if str(s.parent_operation_id) == str(parent_id)
        )
        assert dry_summary.child_count == wet_summary.child_count
        assert dry_summary.bytes_by_host_class == wet_summary.bytes_by_host_class
        assert dry_summary.bytes_total == wet_summary.bytes_total
        assert dry_summary.duration_ms_sum == wet_summary.duration_ms_sum
    finally:
        _cleanup(system_session, [parent_id])


def test_young_parent_is_not_summarized(system_session) -> None:
    """Gate 1 on its own: a settled parent inside the 30-day window keeps
    its children."""
    from app_shared.maintenance.ledger_summaries import run_ledger_child_summarization

    now = datetime.now(timezone.utc)
    parent_id = _seed_navigation(
        system_session, created_at=now - timedelta(days=2), settled=True
    )
    try:
        report = run_ledger_child_summarization(
            system_session,
            now_utc=now,
            settings=_settings("network_operation_children"),
        )
        assert str(parent_id) not in report.parents_summarized
        assert _child_count(system_session, parent_id) == 3
    finally:
        _cleanup(system_session, [parent_id])

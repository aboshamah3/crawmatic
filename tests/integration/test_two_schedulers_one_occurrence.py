"""Two schedulers, one due rule, exactly one job per occurrence (EPA B3 / F07).

The claim under test is a statement about PostgreSQL, not about Python:
"however two scheduler replicas interleave, one due occurrence produces
exactly one `scrape_jobs` row". No fake session can make that statement —
it needs real row locks, a real primary key and real concurrent
transactions — so this suite drives `fire_refresh_rule` from two threads
on two sessions against a real database, 50 attempts each, and counts the
jobs.

What it would have caught
-------------------------
Before B3, `fire_refresh_rule` locked the rule row with `FOR UPDATE SKIP
LOCKED` and nothing else. That settles a *simultaneous* race and leaves
the *sequential* one wide open:

    A loads the candidate list        (rule R is due)
    B locks R, fires it, advances R   (R is no longer due)
    A locks R -- nobody holds it now -- and fires it again

`SKIP LOCKED` is not violated at any step. Two jobs for one occurrence
is simply what the code did. The recheck (`next_run_at <= now` inside the
locking select) plus the `refresh_rule_occurrences` primary key close it,
and the 50-round loop below is sized to actually hit the window rather
than to assert that a single lucky ordering worked.

Scratch database, never the developer's
---------------------------------------
This module brings up its OWN throwaway ``postgres:18-alpine`` container
on a dedicated port and migrates it with ``alembic -x db_url=...``. It
never reads ``.env``, never touches ``DATABASE_URL``/
``MIGRATION_DATABASE_URL``, and removes the container it created (by
name) on teardown. It SKIPS cleanly when Docker is unavailable. Same
pattern, deliberately, as ``tests/integration/test_fair_scheduling.py``
and ``test_cost_authorization.py`` — and the reason B3's migration is
never applied to a database anyone else is using.

The scratch container's superuser is used, so forced RLS is bypassed and
these tests exercise the SCHEDULER's logic rather than the policy — RLS
has its own dedicated suites.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[2]

from app_shared.enums import (  # noqa: E402
    CompetitorStatus,
    MatchPriority,
    MatchStatus,
    ProductStatus,
    ScrapeScope,
)
from app_shared.models.competitors_matches import (  # noqa: E402
    Competitor,
    CompetitorProductMatch,
)
from app_shared.models.jobs import ScrapeJob  # noqa: E402
from app_shared.models.refresh_rule_occurrences import (  # noqa: E402
    refresh_rule_occurrences,
)
from app_shared.models.refresh_rules import RefreshRule  # noqa: E402


def _load_scheduler_app():
    """Import ``apps/scheduler``'s ``app.scheduler.scheduler_app``, safely.

    ``apps/api``, ``apps/workers`` and ``apps/scheduler`` each ship their
    own top-level ``app`` package and all three are on ``sys.path``, with
    ``apps/api`` first — so a bare ``import app.scheduler`` in the shared
    test process resolves to the API's package and fails. This suite needs
    the module in-process (it drives real database fixtures), so it puts
    ``apps/scheduler`` at the FRONT of ``sys.path`` for exactly the
    duration of this one import and then puts everything back — the same
    helper, for the same reason, as ``test_fair_scheduling.py``.
    """
    scheduler_root = str(REPO_ROOT / "apps" / "scheduler")
    saved_path = list(sys.path)
    saved_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "app" or name.startswith("app.")
    }
    for name in saved_modules:
        sys.modules.pop(name, None)
    sys.path.insert(0, scheduler_root)
    try:
        from app.scheduler import scheduler_app as module

        return module
    finally:
        sys.path[:] = saved_path
        for name in [
            n for n in list(sys.modules) if n == "app" or n.startswith("app.")
        ]:
            sys.modules.pop(name, None)
        sys.modules.update(saved_modules)


scheduler_app = _load_scheduler_app()

# A port and a container name nothing else in this repository uses, so a
# stale container from another suite can never be mistaken for this one's
# (and so this suite can never delete another's).
#
# The port is deliberately BELOW this host's ephemeral range
# (`net.ipv4.ip_local_port_range`, 32768-60999 on Linux by default).
# `test_fair_scheduling.py`'s 55498 sits inside it, which means an
# unrelated suite's outbound client socket can be holding that port in
# TIME_WAIT when the container tries to bind it -- observed, and it
# manifests as that whole suite silently SKIPPING. A privileged-range
# port cannot be taken by an ephemeral socket.
_PG_IMAGE = "postgres:18-alpine"
_PG_CONTAINER = "cm-b3-occurrence-pg"
_PG_PORT = 24897
_PG_PASSWORD = "occurrence-scratch"  # noqa: S105 - throwaway container, never a secret
_PG_DB = "crawmatic_occurrence"

DOMAIN = "occurrence-merchant.example"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode
            == 0
        )
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _docker_available(),
    reason="No Docker daemon — this suite provisions its own scratch Postgres",
)


def _dsn() -> str:
    return f"postgresql+psycopg://postgres:{_PG_PASSWORD}@127.0.0.1:{_PG_PORT}/{_PG_DB}"


@pytest.fixture(scope="module")
def engine():
    """A scratch Postgres migrated to alembic head. Removed on teardown."""
    subprocess.run(["docker", "rm", "-f", _PG_CONTAINER], capture_output=True)
    up = subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", _PG_CONTAINER,
            "-e", f"POSTGRES_PASSWORD={_PG_PASSWORD}",
            "-e", f"POSTGRES_DB={_PG_DB}",
            "-p", f"127.0.0.1:{_PG_PORT}:5432",
            _PG_IMAGE,
        ],
        capture_output=True,
        text=True,
    )
    if up.returncode != 0:
        pytest.skip(f"could not start {_PG_IMAGE}: {up.stderr.strip()[:200]}")

    try:
        eng = None
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                eng = create_engine(_dsn(), pool_size=20, max_overflow=20)
                with eng.connect() as conn:
                    conn.execute(text("SELECT 1"))
                break
            except Exception:
                if eng is not None:
                    eng.dispose()
                eng = None
                time.sleep(1)
        if eng is None:
            pytest.skip("scratch Postgres never became reachable")

        migrate = subprocess.run(
            ["uv", "run", "alembic", "-x", f"db_url={_dsn()}", "upgrade", "head"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=900,
        )
        assert migrate.returncode == 0, migrate.stderr[-4000:]

        yield eng
        eng.dispose()
    finally:
        subprocess.run(["docker", "rm", "-f", _PG_CONTAINER], capture_output=True)


@pytest.fixture()
def sessions(engine):
    """A ``sessionmaker`` + a clean slate for each test."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE refresh_rule_occurrences, refresh_rules, "
                "competitor_product_matches, competitors, product_variants, "
                "products, scrape_job_targets, scrape_jobs, outbox_messages, "
                "workspaces RESTART IDENTITY CASCADE"
            )
        )
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def _scope(factory) -> Iterator[Session]:
    session = factory()
    try:
        yield session
    finally:
        session.close()


def _seed_one_scrapeable_rule(
    sessions, *, now: datetime, overdue_seconds: int = 60
) -> tuple[uuid.UUID, uuid.UUID]:
    """A workspace whose COMPETITOR-scope rule resolves to one ACTIVE match.

    The match matters: `create_scope_job` creates NO job for a scope that
    resolves to zero matches (FR-015), so a rule without one would make
    "exactly one job" true for the wrong reason.
    """
    workspace_id = uuid.uuid4()
    competitor_id = uuid.uuid4()
    product_id = uuid.uuid4()
    variant_id = uuid.uuid4()
    rule_id = uuid.uuid4()

    with _scope(sessions) as session:
        session.execute(
            text(
                "INSERT INTO workspaces (id, name, slug, status, created_at, updated_at) "
                "VALUES (:id, 'occ', :slug, 'active', now(), now())"
            ),
            {"id": workspace_id, "slug": f"occ-{workspace_id.hex[:10]}"},
        )
        session.execute(
            text(
                "INSERT INTO products (id, workspace_id, title, status, created_at, "
                "updated_at) VALUES (:id, :ws, 'p', :status, now(), now())"
            ),
            {"id": product_id, "ws": workspace_id, "status": ProductStatus.ACTIVE.value},
        )
        session.execute(
            text(
                "INSERT INTO product_variants (id, workspace_id, product_id, title, "
                "current_price, currency, status, created_at, updated_at) "
                "VALUES (:id, :ws, :product, 'v', 10, 'USD', :status, now(), now())"
            ),
            {
                "id": variant_id,
                "ws": workspace_id,
                "product": product_id,
                "status": ProductStatus.ACTIVE.value,
            },
        )
        session.add(
            Competitor(
                id=competitor_id,
                workspace_id=workspace_id,
                name=DOMAIN,
                domain=DOMAIN,
                status=CompetitorStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            CompetitorProductMatch(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                product_id=product_id,
                product_variant_id=variant_id,
                competitor_id=competitor_id,
                competitor_url=f"https://{DOMAIN}/p",
                normalized_competitor_url=f"https://{DOMAIN}/p",
                url_pattern=DOMAIN,
                url_pattern_version=1,
                priority=MatchPriority.NORMAL,
                status=MatchStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            RefreshRule(
                id=rule_id,
                workspace_id=workspace_id,
                name="every 15m",
                scope=ScrapeScope.COMPETITOR,
                competitor_id=competitor_id,
                interval_minutes=15,
                enabled=True,
                next_run_at=now - timedelta(seconds=overdue_seconds),
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    return workspace_id, rule_id


def _count(sessions, entity) -> int:
    with _scope(sessions) as session:
        return int(session.execute(select(func.count()).select_from(entity)).scalar_one())


def test_two_schedulers_produce_one_job_per_occurrence(sessions):
    """The acceptance criterion, run 50 times against a real database.

    Two threads, two independent sessions, one due rule, both hammering
    `fire_refresh_rule` with the same clock — the worst case, because a
    shared ``now`` means every attempt after the first is a stale
    candidate racing a just-advanced row.

    Exactly one occurrence and exactly one job must exist afterwards, and
    exactly one of the 100 calls may report ``True``: a second ``True``
    would mean two claimants each believed they owned the occurrence,
    which is the bug whether or not a job happened to be created.
    """
    now = datetime.now(timezone.utc)
    _workspace_id, rule_id = _seed_one_scrapeable_rule(sessions, now=now)

    results: list[bool] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    start = threading.Barrier(2)

    def worker() -> None:
        start.wait(timeout=30)
        for _ in range(50):
            try:
                with _scope(sessions) as session:
                    fired = scheduler_app.fire_refresh_rule(
                        session, rule_id=rule_id, now=now
                    )
                with lock:
                    results.append(fired)
            except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
                with lock:
                    errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=180)

    assert not errors, errors[:3]
    assert len(results) == 100, len(results)
    assert sum(results) == 1, f"{sum(results)} claimants believed they won"
    assert _count(sessions, refresh_rule_occurrences) == 1
    assert _count(sessions, ScrapeJob) == 1


def test_a_second_occurrence_is_claimable_and_only_once(sessions):
    """One job per OCCURRENCE — not one job per rule, ever.

    The dedupe must not turn into "this rule already ran once". Winding
    the clock forward past the advanced `next_run_at` produces a genuinely
    new occurrence, and the same two-thread hammer must produce exactly
    one more job for it.
    """
    now = datetime.now(timezone.utc)
    _workspace_id, rule_id = _seed_one_scrapeable_rule(sessions, now=now)

    with _scope(sessions) as session:
        assert scheduler_app.fire_refresh_rule(session, rule_id=rule_id, now=now) is True

    with _scope(sessions) as session:
        advanced = session.execute(
            select(RefreshRule.next_run_at).where(RefreshRule.id == rule_id)
        ).scalar_one()
    assert advanced > now

    later = advanced + timedelta(seconds=1)
    results: list[bool] = []
    lock = threading.Lock()
    start = threading.Barrier(2)

    def worker() -> None:
        start.wait(timeout=30)
        for _ in range(25):
            with _scope(sessions) as session:
                fired = scheduler_app.fire_refresh_rule(
                    session, rule_id=rule_id, now=later
                )
            with lock:
                results.append(fired)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=180)

    assert sum(results) == 1, f"{sum(results)} claimants won the second occurrence"
    assert _count(sessions, refresh_rule_occurrences) == 2
    assert _count(sessions, ScrapeJob) == 2

    with _scope(sessions) as session:
        rows = session.execute(
            select(
                refresh_rule_occurrences.c.scheduled_for,
                refresh_rule_occurrences.c.scrape_job_id,
            ).order_by(refresh_rule_occurrences.c.scheduled_for)
        ).all()
    # Distinct occurrences, each stamped with the job it actually created.
    assert len({row.scheduled_for for row in rows}) == 2
    assert all(row.scrape_job_id is not None for row in rows)
    assert all(row.scheduled_for.microsecond == 0 for row in rows), rows


def test_a_stale_candidate_is_refused_without_creating_anything(sessions):
    """The sequential race, deterministically — no threads, no luck.

    Replica B fires; replica A then arrives holding the candidate it
    loaded before B ran. Its lock succeeds (nobody holds the row) and the
    only things standing between it and a duplicate job are the recheck
    and the occurrence key.
    """
    now = datetime.now(timezone.utc)
    _workspace_id, rule_id = _seed_one_scrapeable_rule(sessions, now=now)

    with _scope(sessions) as session:  # replica B
        assert scheduler_app.fire_refresh_rule(session, rule_id=rule_id, now=now) is True

    with _scope(sessions) as session:  # replica A, same stale clock
        assert scheduler_app.fire_refresh_rule(session, rule_id=rule_id, now=now) is False

    assert _count(sessions, refresh_rule_occurrences) == 1
    assert _count(sessions, ScrapeJob) == 1

    # And with the recheck defeated outright (clock wound back to the
    # occurrence that was already claimed), the primary key alone holds.
    with _scope(sessions) as session:
        occurrence_at = session.execute(
            select(refresh_rule_occurrences.c.scheduled_for)
        ).scalar_one()
        session.execute(
            text("UPDATE refresh_rules SET next_run_at = :t WHERE id = :id"),
            {"t": occurrence_at, "id": rule_id},
        )
        session.commit()

    with _scope(sessions) as session:
        assert scheduler_app.fire_refresh_rule(session, rule_id=rule_id, now=now) is False
    assert _count(sessions, refresh_rule_occurrences) == 1
    assert _count(sessions, ScrapeJob) == 1


def test_the_due_window_gives_every_workspace_a_slot_before_a_second(sessions):
    """Work-level fairness in `load_due_candidates` (B3 step 3).

    A backlogged tenant used to fill the candidate window outright, and a
    workspace absent from the window cannot be arbitrated for by any
    downstream fair-share pass, however fair that pass is. The
    ``DISTINCT ON (workspace_id)`` first page fixes that at the source.
    """
    now = datetime.now(timezone.utc)
    noisy_ws, _rule = _seed_one_scrapeable_rule(sessions, now=now, overdue_seconds=3600)
    quiet_ws, quiet_rule = _seed_one_scrapeable_rule(sessions, now=now, overdue_seconds=5)

    # Nine more, all far more overdue than the quiet tenant's single rule.
    with _scope(sessions) as session:
        for index in range(9):
            session.add(
                RefreshRule(
                    id=uuid.uuid4(),
                    workspace_id=noisy_ws,
                    name=f"noisy-{index}",
                    scope=ScrapeScope.WORKSPACE,
                    interval_minutes=15,
                    enabled=True,
                    next_run_at=now - timedelta(seconds=3600 + index),
                    created_at=now,
                    updated_at=now,
                )
            )
        session.commit()

    with _scope(sessions) as session:
        # A window of 2 is the sharp case: strict due-order would spend
        # both slots on the noisy tenant's oldest two rules.
        candidates = scheduler_app.load_due_candidates(session, now=now, limit=2)

    workspaces = {candidate.workspace_id for candidate in candidates}
    assert len(candidates) == 2, candidates
    assert workspaces == {str(noisy_ws), str(quiet_ws)}, workspaces
    assert str(quiet_rule) in {candidate.key for candidate in candidates}

    # The remainder page still fills a larger window to the brim.
    with _scope(sessions) as session:
        full = scheduler_app.load_due_candidates(session, now=now, limit=50)
    assert len(full) == 11, len(full)
    assert len({candidate.key for candidate in full}) == 11, "no candidate twice"

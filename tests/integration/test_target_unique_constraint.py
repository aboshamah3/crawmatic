"""Live verification of ``uq_scrape_job_targets_scrape_job_id_match_id``
(SPEC-11 US2 T018, `contracts/match-lock.md` "Job-level dedup", FR-015)
— ⏸ DEFERRED.

Per FR-015/data-model.md §3: SPEC-11 **verifies** the SPEC-08
``unique(scrape_job_id, match_id)`` constraint on ``scrape_job_targets``
still exists (it prevents a duplicate target within one job *before* any
match lock is ever attempted, US2 AS5) — it does **not** re-add it, and
this test authors no new schema/migration.

Two checks:
1. **Introspection** — the named unique constraint is present on the
   live ``scrape_job_targets`` table (via SQLAlchemy `Inspector`).
2. **Behavioral** — inserting a second ``ScrapeJobTarget`` row for the
   same ``(scrape_job_id, match_id)`` pair raises `IntegrityError`
   (belt-and-suspenders: proves the constraint is actually enforced by
   Postgres, not merely declared in the ORM model).

Needs a reachable Postgres (`DATABASE_URL`) with the SPEC-08 migration
applied. Not runnable in the no-Docker-daemon build environment used to
author this feature — SKIPS cleanly whenever Postgres isn't usable or
the table doesn't exist.

Author now; leave unchecked (DEFERRED — needs a Postgres host with the
SPEC-08 migration applied).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

_CONSTRAINT_NAME = "uq_scrape_job_targets_scrape_job_id_match_id"


def _postgres_reachable_with_table() -> bool:
    """Best-effort probe: Postgres reachable AND `scrape_job_targets` exists.

    Mirrors `tests/integration/test_db_connectivity.py`'s
    `_postgres_reachable` convention -- any failure (missing config, no
    reachable server, table absent) is treated as "not reachable" so this
    test skips cleanly rather than erroring.
    """
    try:
        from app_shared.config import get_settings

        settings = get_settings()
    except Exception:
        return False

    if not settings.DATABASE_URL:
        return False

    try:
        from sqlalchemy import inspect

        from app_shared.database import check_connection, get_engine

        check_connection()
        table_names = set(inspect(get_engine()).get_table_names())
        if "scrape_job_targets" not in table_names:
            return False
    except Exception:
        return False

    return True


pytestmark = pytest.mark.skipif(
    not _postgres_reachable_with_table(),
    reason="No reachable Postgres (with the SPEC-08 migration applied) in this environment",
)


def test_unique_constraint_exists_on_scrape_job_targets() -> None:
    """Introspect the live table -- the named unique constraint is present
    (FR-015 "verify only, do NOT add")."""
    from sqlalchemy import inspect

    from app_shared.database import get_engine

    constraints = inspect(get_engine()).get_unique_constraints("scrape_job_targets")
    names = {c["name"] for c in constraints}

    assert _CONSTRAINT_NAME in names
    matching = next(c for c in constraints if c["name"] == _CONSTRAINT_NAME)
    assert set(matching["column_names"]) == {"scrape_job_id", "match_id"}


@pytest.fixture()
def seeded_job() -> Iterator[tuple[uuid.UUID, uuid.UUID, uuid.UUID]]:
    """One workspace + one ``ScrapeJob`` + one product/variant/competitor/
    match, ready for two competing ``ScrapeJobTarget`` inserts."""
    from app_shared.database import get_session
    from app_shared.enums import (
        ProductStatus,
        ScrapeJobSource,
        ScrapeJobStatus,
        ScrapeJobType,
        ScrapeScope,
        VariantStatus,
        WorkspaceStatus,
    )
    from app_shared.models import Workspace
    from app_shared.models.catalog import Product, ProductVariant
    from app_shared.models.competitors_matches import Competitor, CompetitorProductMatch
    from app_shared.models.jobs import ScrapeJob

    unique = uuid.uuid4().hex[:8]
    now = datetime.now(UTC)

    with get_session() as session:
        workspace = Workspace(
            name=f"unique-constraint-test {unique}",
            slug=f"unique-constraint-test-{unique}",
            status=WorkspaceStatus.ACTIVE,
        )
        session.add(workspace)
        session.flush()

        from decimal import Decimal

        product = Product(workspace_id=workspace.id, title=f"product {unique}", status=ProductStatus.ACTIVE)
        session.add(product)
        session.flush()

        variant = ProductVariant(
            workspace_id=workspace.id,
            product_id=product.id,
            title="Default",
            current_price=Decimal("0.0000"),
            currency="USD",
            status=VariantStatus.ACTIVE,
        )
        session.add(variant)
        session.flush()

        competitor = Competitor(
            workspace_id=workspace.id,
            name=f"competitor {unique}",
            domain=f"unique-constraint-{unique}.invalid",
        )
        session.add(competitor)
        session.flush()

        match = CompetitorProductMatch(
            workspace_id=workspace.id,
            product_id=product.id,
            product_variant_id=variant.id,
            competitor_id=competitor.id,
            competitor_url=f"https://unique-constraint-{unique}.invalid/p",
            normalized_competitor_url=f"https://unique-constraint-{unique}.invalid/p",
            url_pattern=f"https://unique-constraint-{unique}.invalid/p",
            url_pattern_version=1,
        )
        session.add(match)
        session.flush()

        job = ScrapeJob(
            workspace_id=workspace.id,
            type=ScrapeJobType.MANUAL,
            scope=ScrapeScope.MATCH,
            status=ScrapeJobStatus.RUNNING,
            source=ScrapeJobSource.API,
            total_targets=1,
            started_at=now,
            created_at=now,
        )
        session.add(job)
        session.flush()
        session.commit()

        workspace_id, job_id, match_id = workspace.id, job.id, match.id

    yield workspace_id, job_id, match_id

    from sqlalchemy import text

    with get_session() as session:
        session.execute(text("DELETE FROM scrape_job_targets WHERE workspace_id = :ws"), {"ws": workspace_id})
        session.execute(text("DELETE FROM scrape_jobs WHERE workspace_id = :ws"), {"ws": workspace_id})
        session.execute(text("DELETE FROM competitor_product_matches WHERE workspace_id = :ws"), {"ws": workspace_id})
        session.execute(text("DELETE FROM competitors WHERE workspace_id = :ws"), {"ws": workspace_id})
        session.execute(text("DELETE FROM product_variants WHERE workspace_id = :ws"), {"ws": workspace_id})
        session.execute(text("DELETE FROM products WHERE workspace_id = :ws"), {"ws": workspace_id})
        session.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": workspace_id})
        session.commit()


def test_duplicate_target_within_one_job_raises_integrity_error(
    seeded_job: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """A second `(scrape_job_id, match_id)` insert within the SAME job is
    rejected by Postgres itself (FR-015, US2 AS5) -- proves the
    constraint is enforced, not merely declared."""
    from sqlalchemy.exc import IntegrityError

    from app_shared.database import get_session
    from app_shared.enums import ScrapeTargetStatus
    from app_shared.models.jobs import ScrapeJobTarget

    workspace_id, job_id, match_id = seeded_job
    now = datetime.now(UTC)

    with get_session() as session:
        session.add(
            ScrapeJobTarget(
                workspace_id=workspace_id,
                scrape_job_id=job_id,
                match_id=match_id,
                status=ScrapeTargetStatus.PENDING,
                created_at=now,
            )
        )
        session.commit()

    with pytest.raises(IntegrityError):
        with get_session() as session:
            session.add(
                ScrapeJobTarget(
                    workspace_id=workspace_id,
                    scrape_job_id=job_id,
                    match_id=match_id,
                    status=ScrapeTargetStatus.PENDING,
                    created_at=now,
                )
            )
            session.commit()

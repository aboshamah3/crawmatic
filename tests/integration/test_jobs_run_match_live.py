"""Live run-match end-to-end test (SPEC-08 US1, FR-006, SC-001) — ⏸ DEFERRED.

`tests/unit/test_jobs_router.py` + `tests/unit/test_jobs_service.py` +
`tests/unit/test_jobs_dispatch_task.py` already prove `create_match_job`
and `POST /v1/jobs/run/match/{id}`'s shapes exhaustively against a
dependency-overridden session + fake `enqueue`. This live test's
distinguishing contribution is proving the same flow against the real
integration points a fake cannot stand in for: a real Postgres row
(`scrape_jobs` + `scrape_job_targets`, `unique`/FK constraints actually
enforced) and a real Redis `scrape_dispatch` queue (does
`app_shared.messaging.enqueue` actually publish a message a real Celery
consumer could pick up, not just call a stubbed `send_task`).

Per `contracts/api-jobs.md` (US1):

1. `POST /v1/jobs/run/match/{id}` for a valid in-workspace match creates
   one `ScrapeJob` (`scope=MATCH`, `type=MANUAL`, `source=API`,
   `requested_by`=the calling principal, `total_targets=1`) + exactly
   one `ScrapeJobTarget` (`status=PENDING`), returns **202**
   `{id, status=PENDING}`, and enqueues real work onto the real
   `scrape_dispatch` Redis queue (queue length increases by exactly 1).
2. Unknown / cross-workspace match id -> **404**, no job row created,
   no dispatch enqueued (US1-AS4).
3. `GET /v1/jobs/{id}` / `GET /v1/jobs/{id}/results` read back the job
   header / the one target, workspace-scoped.

Needs a reachable Postgres (`DATABASE_URL`, SPEC-08 migration applied)
AND a reachable Redis (`REDIS_URL`) — the dispatch enqueue is a real
`send_task` publish. Not runnable in the no-Docker-daemon build
environment used to author this feature — SKIPS cleanly whenever either
isn't reachable or the `scrape_jobs`/`scrape_job_targets` tables don't
exist yet.

Author now; leave unchecked (DEFERRED — needs a Postgres+Redis-capable
host with the SPEC-08 migration applied).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from ._scrapyd_spider_live_support import (
    cleanup_seeded_workspace,
    seed_competitor,
    seed_match,
    seed_workspace_with_variant,
)

_REQUIRED_TABLES = frozenset(
    {"workspaces", "api_keys", "products", "product_variants", "competitors",
     "competitor_product_matches", "scrape_jobs", "scrape_job_targets"}
)


def _jobs_live_reachable() -> bool:
    try:
        from app_shared.config import get_settings

        settings = get_settings()
    except Exception:
        return False

    if not settings.DATABASE_URL or not settings.REDIS_URL:
        return False

    try:
        from sqlalchemy import inspect

        from app_shared.database import check_connection, get_engine
        from app_shared.redis_client import get_redis_client

        check_connection()
        table_names = set(inspect(get_engine()).get_table_names())
        if not _REQUIRED_TABLES <= table_names:
            return False
        get_redis_client().ping()
    except Exception:
        return False

    return True


pytestmark = pytest.mark.skipif(
    not _jobs_live_reachable(),
    reason=(
        "Needs a reachable Postgres (DATABASE_URL, SPEC-08 migration applied) "
        "and a reachable Redis (REDIS_URL) in this environment."
    ),
)


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


@pytest.fixture()
def seeded_match():
    """One workspace + product/variant + competitor + ACTIVE match, plus a
    `jobs:read`/`jobs:write`-scoped API key. Cleaned up after."""
    from app_shared.database import get_session
    from app_shared.enums import ApiKeyStatus
    from app_shared.models import ApiKey
    from app_shared.security.api_keys import generate_api_key

    seeded = seed_workspace_with_variant("jobs-run-match-live")
    competitor_id = seed_competitor(seeded, "Jobs Live Competitor")
    match_id = seed_match(
        seeded, competitor_id, "https://jobs-run-match-live.invalid/p/1"
    )

    with get_session() as session:
        full_secret, key_prefix, key_hash = generate_api_key()
        api_key = ApiKey(
            workspace_id=seeded.workspace_id,
            name="jobs-run-match-live-key",
            key_prefix=key_prefix,
            key_hash=key_hash,
            scopes=["jobs:read", "jobs:write"],
            status=ApiKeyStatus.ACTIVE,
        )
        session.add(api_key)
        session.commit()
        api_key_id = api_key.id

    try:
        yield {
            "workspace_id": seeded.workspace_id,
            "match_id": match_id,
            "api_key": full_secret,
            "api_key_id": api_key_id,
        }
    finally:
        from sqlalchemy import text

        with get_session() as session:
            session.execute(
                text("DELETE FROM scrape_job_targets WHERE workspace_id = :ws"),
                {"ws": seeded.workspace_id},
            )
            session.execute(
                text("DELETE FROM scrape_jobs WHERE workspace_id = :ws"),
                {"ws": seeded.workspace_id},
            )
            session.execute(
                text("DELETE FROM api_keys WHERE workspace_id = :ws"),
                {"ws": seeded.workspace_id},
            )
            session.commit()
        cleanup_seeded_workspace(seeded)


def _auth_headers(seeded_match: dict[str, object]) -> dict[str, str]:
    return {"Authorization": f"Bearer {seeded_match['api_key']}"}


# --- SC-001: run-match creates a job + 1 target, dispatch enqueued ----------


def test_run_match_creates_job_and_one_target_and_enqueues_dispatch(
    client, seeded_match: dict[str, object]
) -> None:
    from app_shared.redis_client import get_redis_client

    redis_client = get_redis_client()
    before = redis_client.llen("scrape_dispatch")

    response = client.post(
        f"/v1/jobs/run/match/{seeded_match['match_id']}",
        headers=_auth_headers(seeded_match),
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "PENDING"
    job_id = body["id"]

    after = redis_client.llen("scrape_dispatch")
    assert after == before + 1

    from app_shared.database import get_session, set_workspace_context
    from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget
    from app_shared.repository import scoped_select

    with get_session() as session:
        set_workspace_context(session, seeded_match["workspace_id"])
        job = session.execute(
            scoped_select(ScrapeJob, seeded_match["workspace_id"]).where(
                ScrapeJob.id == uuid.UUID(job_id)
            )
        ).scalar_one()
        assert job.scope == "MATCH"
        assert job.type == "MANUAL"
        assert job.source == "API"
        assert str(job.requested_by) == str(seeded_match["api_key_id"])
        assert job.total_targets == 1
        assert job.status == "PENDING"

        targets = list(
            session.execute(
                scoped_select(ScrapeJobTarget, seeded_match["workspace_id"]).where(
                    ScrapeJobTarget.scrape_job_id == job.id
                )
            )
            .scalars()
            .all()
        )
        assert len(targets) == 1
        assert targets[0].match_id == seeded_match["match_id"]
        assert targets[0].status == "PENDING"


# --- unknown / cross-workspace match -> 404, no job, no enqueue --------------


def test_run_match_unknown_match_is_404_and_creates_no_job(
    client, seeded_match: dict[str, object]
) -> None:
    from app_shared.redis_client import get_redis_client

    redis_client = get_redis_client()
    before = redis_client.llen("scrape_dispatch")

    response = client.post(
        f"/v1/jobs/run/match/{uuid.uuid4()}",
        headers=_auth_headers(seeded_match),
    )
    assert response.status_code == 404
    assert response.json()["detail"]["error"]["code"] == "NOT_FOUND"

    after = redis_client.llen("scrape_dispatch")
    assert after == before  # nothing enqueued


# --- get / results read back the created job + target ------------------------


def test_get_job_and_results_reflect_the_created_target(
    client, seeded_match: dict[str, object]
) -> None:
    create = client.post(
        f"/v1/jobs/run/match/{seeded_match['match_id']}",
        headers=_auth_headers(seeded_match),
    )
    assert create.status_code == 202
    job_id = create.json()["id"]

    get_resp = client.get(f"/v1/jobs/{job_id}", headers=_auth_headers(seeded_match))
    assert get_resp.status_code == 200
    body = get_resp.json()
    assert body["id"] == job_id
    assert body["scope"] == "MATCH"
    assert body["total_targets"] == 1
    assert body["success_count"] == 0
    assert body["failure_count"] == 0

    results_resp = client.get(
        f"/v1/jobs/{job_id}/results", headers=_auth_headers(seeded_match)
    )
    assert results_resp.status_code == 200
    items = results_resp.json()["items"]
    assert len(items) == 1
    assert items[0]["match_id"] == str(seeded_match["match_id"])
    assert items[0]["status"] == "PENDING"


def test_get_job_missing_id_is_404(client, seeded_match: dict[str, object]) -> None:
    response = client.get(f"/v1/jobs/{uuid.uuid4()}", headers=_auth_headers(seeded_match))
    assert response.status_code == 404

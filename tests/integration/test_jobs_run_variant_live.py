"""Live run-variant end-to-end test (SPEC-08 US2, FR-007/FR-020, SC-002) —
⏸ DEFERRED.

`tests/unit/test_jobs_service.py` + `tests/unit/test_jobs_router.py`
already prove `create_variant_job`/`POST /v1/jobs/run/variant/{id}`'s
shapes exhaustively against fakes. This live test's distinguishing
contribution: a real `unique(scrape_job_id, match_id)` constraint
actually enforced by Postgres (not just asserted at the app layer), and
a real dispatch-enqueue side effect on Redis.

Per `contracts/api-jobs.md` (US2):

1. A variant with several ACTIVE matches + at least one inactive match
   -> one job (`scope=VARIANT`), exactly one target per ACTIVE match
   (`total_targets = N`), inactive excluded, dispatch enqueued once.
2. A variant with **zero** active matches -> job created,
   `total_targets=0`, `status=COMPLETED` immediately, **no** dispatch
   enqueued (FR-020, US2-AS4).

Needs a reachable Postgres (`DATABASE_URL`, SPEC-08 migration applied)
AND a reachable Redis (`REDIS_URL`). Not runnable in the
no-Docker-daemon build environment used to author this feature — SKIPS
cleanly whenever either isn't reachable or the
`scrape_jobs`/`scrape_job_targets` tables don't exist yet.

Author now; leave unchecked (DEFERRED — needs a Postgres+Redis-capable
host with the SPEC-08 migration applied).
"""

from __future__ import annotations

import uuid

import pytest

from ._scrapyd_spider_live_support import (
    cleanup_seeded_workspace,
    seed_competitor,
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


def _seed_match_with_status(seeded, competitor_id, url, status):
    from app_shared.database import get_session
    from app_shared.models.competitors_matches import CompetitorProductMatch

    with get_session() as session:
        match = CompetitorProductMatch(
            workspace_id=seeded.workspace_id,
            product_id=seeded.product_id,
            product_variant_id=seeded.product_variant_id,
            competitor_id=competitor_id,
            competitor_url=url,
            normalized_competitor_url=url,
            url_pattern=url,
            url_pattern_version=1,
            status=status,
        )
        session.add(match)
        session.commit()
        seeded._match_ids.append(match.id)
        return match.id


@pytest.fixture()
def seeded_variant_with_mixed_matches():
    """One workspace/variant, 2 ACTIVE matches + 1 PAUSED (inactive) match,
    plus a `jobs:*`-scoped API key. Cleaned up after."""
    from app_shared.database import get_session
    from app_shared.enums import ApiKeyStatus, MatchStatus
    from app_shared.models import ApiKey
    from app_shared.security.api_keys import generate_api_key

    seeded = seed_workspace_with_variant("jobs-run-variant-live")
    competitor_id = seed_competitor(seeded, "Jobs Variant Live Competitor")

    active_ids = [
        _seed_match_with_status(
            seeded, competitor_id, f"https://jobs-run-variant-live.invalid/p/{i}",
            MatchStatus.ACTIVE,
        )
        for i in range(2)
    ]
    inactive_id = _seed_match_with_status(
        seeded, competitor_id, "https://jobs-run-variant-live.invalid/p/inactive",
        MatchStatus.PAUSED,
    )

    with get_session() as session:
        full_secret, key_prefix, key_hash = generate_api_key()
        api_key = ApiKey(
            workspace_id=seeded.workspace_id,
            name="jobs-run-variant-live-key",
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
            "variant_id": seeded.product_variant_id,
            "active_match_ids": active_ids,
            "inactive_match_id": inactive_id,
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


@pytest.fixture()
def seeded_variant_with_no_active_matches():
    """One workspace/variant with zero matches at all, plus an API key."""
    from app_shared.database import get_session
    from app_shared.enums import ApiKeyStatus
    from app_shared.models import ApiKey
    from app_shared.security.api_keys import generate_api_key

    seeded = seed_workspace_with_variant("jobs-run-variant-zero-live")

    with get_session() as session:
        full_secret, key_prefix, key_hash = generate_api_key()
        api_key = ApiKey(
            workspace_id=seeded.workspace_id,
            name="jobs-run-variant-zero-live-key",
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
            "variant_id": seeded.product_variant_id,
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


def _auth_headers(fixture: dict[str, object]) -> dict[str, str]:
    return {"Authorization": f"Bearer {fixture['api_key']}"}


# --- SC-002: one target per ACTIVE match, inactive excluded ------------------


def test_run_variant_creates_one_target_per_active_match_inactive_excluded(
    client, seeded_variant_with_mixed_matches: dict[str, object]
) -> None:
    fixture = seeded_variant_with_mixed_matches
    from app_shared.redis_client import get_redis_client

    redis_client = get_redis_client()
    before = redis_client.llen("scrape_dispatch")

    response = client.post(
        f"/v1/jobs/run/variant/{fixture['variant_id']}", headers=_auth_headers(fixture)
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "PENDING"
    job_id = body["id"]

    after = redis_client.llen("scrape_dispatch")
    assert after == before + 1

    results = client.get(
        f"/v1/jobs/{job_id}/results", headers=_auth_headers(fixture)
    )
    assert results.status_code == 200
    match_ids = {item["match_id"] for item in results.json()["items"]}
    assert match_ids == {str(m) for m in fixture["active_match_ids"]}
    assert str(fixture["inactive_match_id"]) not in match_ids

    job = client.get(f"/v1/jobs/{job_id}", headers=_auth_headers(fixture))
    assert job.status_code == 200
    assert job.json()["total_targets"] == len(fixture["active_match_ids"])
    assert job.json()["scope"] == "VARIANT"


# --- FR-020: zero active matches -> COMPLETED immediately, no dispatch ------


def test_run_variant_zero_active_matches_completes_immediately_no_dispatch(
    client, seeded_variant_with_no_active_matches: dict[str, object]
) -> None:
    fixture = seeded_variant_with_no_active_matches
    from app_shared.redis_client import get_redis_client

    redis_client = get_redis_client()
    before = redis_client.llen("scrape_dispatch")

    response = client.post(
        f"/v1/jobs/run/variant/{fixture['variant_id']}", headers=_auth_headers(fixture)
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "COMPLETED"
    job_id = body["id"]

    after = redis_client.llen("scrape_dispatch")
    assert after == before  # no dispatch enqueued

    job = client.get(f"/v1/jobs/{job_id}", headers=_auth_headers(fixture))
    assert job.status_code == 200
    assert job.json()["total_targets"] == 0
    assert job.json()["status"] == "COMPLETED"
    assert job.json()["completed_at"] is not None

    results = client.get(
        f"/v1/jobs/{job_id}/results", headers=_auth_headers(fixture)
    )
    assert results.status_code == 200
    assert results.json()["items"] == []


# --- unknown / cross-workspace variant -> 404, no job -----------------------


def test_run_variant_unknown_variant_is_404_and_creates_no_job(
    client, seeded_variant_with_no_active_matches: dict[str, object]
) -> None:
    fixture = seeded_variant_with_no_active_matches

    response = client.post(
        f"/v1/jobs/run/variant/{uuid.uuid4()}", headers=_auth_headers(fixture)
    )
    assert response.status_code == 404
    assert response.json()["detail"]["error"]["code"] == "NOT_FOUND"

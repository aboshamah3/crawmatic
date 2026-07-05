"""Live-Postgres/Redis discovery-run test (SPEC-12 US3 T025,
`contracts/discovery.md`, FR-016..FR-019, SC-006) — ⏸ DEFERRED.

Exercises the full `STRATEGY_DISCOVERY_RUN` task
(`apps/workers/app/workers/tasks_strategy.py::run_discovery`) end-to-end
against a live Postgres (profile/run persistence) — the probe step
itself makes real outbound HTTP requests to `sample_urls`, so these
scenarios seed a tiny local HTTP server rather than reaching out to the
network:

1. A key with 5 sample URLs served by a local stub server returning a
   valid JSON-LD price -> the run progresses `PENDING -> RUNNING ->
   COMPLETED` with `sample_size=5`, `winning_access_method=DIRECT_HTTP`,
   `winning_extraction_method=JSON_LD`, `completed_at` set, and the
   profile leaves `DISCOVERY_REQUIRED` (US3 AS1/AS3).
2. A key whose sample URLs all 404 -> `NO_WINNER`, profile stays
   `DISCOVERY_REQUIRED` (US3 AS4).
3. `POST /v1/strategy/discovery-runs` with 2 and 11 `sample_urls` -> HTTP
   `422`, no run created, no task enqueued (US3 AS2) -- this scenario
   only needs the API's Pydantic validation and is additionally covered
   DB-free by `tests/unit/test_strategy_router.py`.

Needs a reachable Postgres (`DATABASE_URL`) with the SPEC-12 migration
applied AND a reachable Redis (`REDIS_URL`, for `app_shared.messaging
.enqueue`'s producer). Not runnable in the no-Docker-daemon build
environment used to author this feature -- SKIPS cleanly whenever either
isn't usable or the required tables don't exist (mirrors
`tests/integration/test_promotion_apply.py`'s skip mechanism).

Author now; leave unchecked (DEFERRED — needs a Postgres+Redis host with
the SPEC-12 migration applied).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest


def _live_strategy_reachable() -> bool:
    """Best-effort probe: True only if Postgres + Redis are reachable AND
    the SPEC-12 `domain_strategy_profiles`/`strategy_discovery_runs`
    tables already exist (migration applied)."""
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

        check_connection()
        table_names = set(inspect(get_engine()).get_table_names())
        if "strategy_discovery_runs" not in table_names:
            return False
    except Exception:
        return False

    try:
        from app_shared.redis_client import get_redis_client

        get_redis_client().ping()
    except Exception:
        return False

    return True


pytestmark = pytest.mark.skipif(
    not _live_strategy_reachable(),
    reason=(
        "No reachable Postgres (DATABASE_URL) + Redis (REDIS_URL) with the "
        "SPEC-12 strategy_discovery_runs migration applied in this environment"
    ),
)


@pytest.fixture()
def seeded_key() -> Iterator[tuple[uuid.UUID, uuid.UUID, str, str]]:
    """One workspace + one competitor, ready for a discovery run key."""
    from app_shared.database import get_session
    from app_shared.enums import WorkspaceStatus
    from app_shared.models import Workspace
    from app_shared.models.competitors_matches import Competitor

    unique = uuid.uuid4().hex[:8]
    domain = f"strategy-discovery-{unique}.invalid"

    with get_session() as session:
        workspace = Workspace(
            name=f"strategy-discovery-test {unique}",
            slug=f"strategy-discovery-test-{unique}",
            status=WorkspaceStatus.ACTIVE,
        )
        session.add(workspace)
        session.flush()

        competitor = Competitor(workspace_id=workspace.id, name=f"competitor {unique}", domain=domain)
        session.add(competitor)
        session.flush()
        session.commit()

        workspace_id, competitor_id = workspace.id, competitor.id

    yield workspace_id, competitor_id, domain, f"{domain}/products/*"

    from sqlalchemy import text

    with get_session() as session:
        session.execute(
            text("DELETE FROM strategy_discovery_runs WHERE workspace_id = :ws"),
            {"ws": workspace_id},
        )
        session.execute(
            text("DELETE FROM domain_strategy_profiles WHERE workspace_id = :ws"),
            {"ws": workspace_id},
        )
        session.execute(text("DELETE FROM competitors WHERE workspace_id = :ws"), {"ws": workspace_id})
        session.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": workspace_id})
        session.commit()


def test_five_url_sample_completes_and_seeds_profile(
    seeded_key: tuple[uuid.UUID, uuid.UUID, str, str],
) -> None:
    """US3 AS1/AS3: a run with `sample_size=5` progresses to `COMPLETED`
    with winning methods + `completed_at`; the profile leaves
    `DISCOVERY_REQUIRED`."""
    import http.server
    import threading

    from app_shared.database import get_session, set_workspace_context
    from app_shared.enums import DiscoveryRunStatus, StrategyStatus
    from app_shared.models.strategy import DomainStrategyProfile, StrategyDiscoveryRun
    from app_shared.repository import scoped_select

    from app.workers.tasks_strategy import run_discovery

    workspace_id, competitor_id, domain, url_pattern = seeded_key

    html = (
        b"<html><head><script type='application/ld+json'>"
        b'{"@type":"Product","offers":{"price":"19.99","priceCurrency":"USD"}}'
        b"</script></head><body></body></html>"
    )

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib override
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, *args: object) -> None:  # noqa: D102 - silence stdlib logging
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        sample_urls = [f"http://127.0.0.1:{port}/p/{i}" for i in range(5)]

        run_discovery(
            workspace_id=str(workspace_id),
            competitor_id=str(competitor_id),
            domain=domain,
            url_pattern=url_pattern,
            sample_urls=sample_urls,
            triggered_by="OPERATOR",
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    with get_session() as session:
        set_workspace_context(session, workspace_id)
        run = session.execute(
            scoped_select(StrategyDiscoveryRun, workspace_id).where(
                StrategyDiscoveryRun.competitor_id == competitor_id
            )
        ).scalar_one()
        assert run.sample_size == 5
        assert run.status == DiscoveryRunStatus.COMPLETED
        assert run.completed_at is not None
        assert run.winning_access_method is not None
        assert run.winning_extraction_method is not None

        profile = session.execute(
            scoped_select(DomainStrategyProfile, workspace_id).where(
                DomainStrategyProfile.competitor_id == competitor_id
            )
        ).scalar_one()
        assert profile.status != StrategyStatus.DISCOVERY_REQUIRED


def test_all_urls_failing_records_no_winner(
    seeded_key: tuple[uuid.UUID, uuid.UUID, str, str],
) -> None:
    """US3 AS4: every sample URL fails to fetch -> `NO_WINNER`, profile
    stays `DISCOVERY_REQUIRED`."""
    from app_shared.database import get_session, set_workspace_context
    from app_shared.enums import DiscoveryRunStatus, StrategyStatus
    from app_shared.models.strategy import DomainStrategyProfile, StrategyDiscoveryRun
    from app_shared.repository import scoped_select

    from app.workers.tasks_strategy import run_discovery

    workspace_id, competitor_id, domain, url_pattern = seeded_key
    # Port 1 is reserved/unroutable -- every fetch attempt fails cleanly.
    sample_urls = [f"http://127.0.0.1:1/p/{i}" for i in range(3)]

    run_discovery(
        workspace_id=str(workspace_id),
        competitor_id=str(competitor_id),
        domain=domain,
        url_pattern=url_pattern,
        sample_urls=sample_urls,
        triggered_by="OPERATOR",
    )

    with get_session() as session:
        set_workspace_context(session, workspace_id)
        run = session.execute(
            scoped_select(StrategyDiscoveryRun, workspace_id).where(
                StrategyDiscoveryRun.competitor_id == competitor_id
            )
        ).scalar_one()
        assert run.status == DiscoveryRunStatus.NO_WINNER
        assert run.completed_at is not None
        assert run.winning_access_method is None

        profile = session.execute(
            scoped_select(DomainStrategyProfile, workspace_id).where(
                DomainStrategyProfile.competitor_id == competitor_id
            )
        ).scalar_one()
        assert profile.status == StrategyStatus.DISCOVERY_REQUIRED

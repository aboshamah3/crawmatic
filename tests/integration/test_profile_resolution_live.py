"""Live-Postgres + Redis batch config-resolution test (SPEC-06 US3 T040,
FR-014..FR-019, SC-003/SC-004/SC-005) — ⏸ DEFERRED.

Exercises `apps.api.app.services.profile_resolution.resolve_profiles_for_matches`
/ `invalidate_resolution_cache` (the Redis-driving cache orchestrator,
`contracts/config-resolution.md` "Orchestrator" section) against a real
database + a real Redis instance — no running server/container required,
only Postgres and Redis need to be live.

Proves the halves of the config-resolution contract that the pure-core
unit tests (`tests/unit/test_profile_resolution.py`,
`tests/unit/test_profile_resolution_cache_key.py`) cannot, because they
need a real DB + Redis:

1. **End-to-end precedence.** A match with no override falls through to
   its competitor default; clearing the competitor default falls through
   to the workspace default; clearing that falls through to the global
   default; clearing that resolves to `NONE_RESOLVED` (FR-014/FR-016).
   A match-level override (when visible) always wins outright
   regardless of what the other three levels are set to (FR-014
   scenario 1).
2. **Grouped, not per-match, DB access (SC-004).** A batch of matches
   spread over a handful of distinct `(competitor_id, url_pattern)`
   groups performs a bounded number of DB statements — proportional to
   the number of *groups*, not the number of matches — proven by
   instrumenting `Session.execute` and comparing statement counts across
   two batch sizes that share the same group count.
3. **Redis cache hit within TTL (SC-005).** A second resolution call for
   the same groups within `PROFILE_RESOLUTION_CACHE_TTL_SECONDS` reuses
   the cached group result — proven by clearing the DB-side evidence
   (deleting the competitor default row's backing data would be
   destructive, so instead this is proven by counting `resolve_group`
   invocations via a monkeypatch spy: zero further chain-walks on the
   second call).
4. **Write invalidation (FR-019).** Changing a competitor's default
   profile and calling `invalidate_resolution_cache` makes the very next
   resolution reflect the new value immediately (not waiting out the
   TTL); relying on TTL expiry alone also eventually reflects the write.

Needs a reachable Postgres instance with `DATABASE_URL` (app role, RLS
enforced) usable AND the SPEC-06 migration already applied AND a
reachable Redis (`REDIS_URL`). Not runnable in the no-Docker-daemon build
environment used to author this feature — SKIPS cleanly whenever any of
those aren't usable or a real connection attempt fails (mirrors
`tests/integration/test_status_cache.py`'s dual Postgres+Redis
reachability probe and `tests/integration/test_profile_assignment_live.py`'s
`scrape_profiles`-table probe).

Author now; leave unchecked (DEFERRED — needs a Postgres + Redis host
with the SPEC-06 migration applied).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest


def _live_resolution_reachable() -> bool:
    """Best-effort probe: True only if Postgres (with the SPEC-06 tables)
    AND Redis both work."""
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
        inspector = inspect(get_engine())
        table_names = set(inspector.get_table_names())
        if not {"scrape_profiles", "competitors", "competitor_product_matches"} <= table_names:
            return False
        get_redis_client().ping()
    except Exception:
        return False

    return True


pytestmark = pytest.mark.skipif(
    not _live_resolution_reachable(),
    reason=(
        "No reachable Postgres (DATABASE_URL, with the SPEC-06 scrape_profiles "
        "migration applied) / Redis (REDIS_URL) configured in this environment"
    ),
)


# --- fixtures ----------------------------------------------------------------


@pytest.fixture()
def resolution_fixture() -> Iterator[dict[str, object]]:
    """One workspace, one competitor, one product+variant, a `global_default`
    profile, a workspace-default profile, a competitor-default profile, and
    three matches sharing a single `(competitor_id, url_pattern)` group (one
    with a match-level override, two without) — cleaned up after."""
    from app_shared.database import get_session
    from app_shared.enums import (
        CompetitorStatus,
        LegalStatus,
        MatchPriority,
        MatchStatus,
        ProductStatus,
        RobotsPolicy,
        VariantStatus,
        WorkspaceStatus,
    )
    from app_shared.models import Workspace
    from app_shared.models.catalog import Product, ProductVariant
    from app_shared.models.competitors_matches import Competitor, CompetitorProductMatch
    from app_shared.models.scrape_profiles import ScrapeProfile

    unique = uuid.uuid4().hex[:8]

    with get_session() as session:
        workspace = Workspace(
            name=f"Resolution Live Test {unique}",
            slug=f"resolution-live-test-{unique}",
            status=WorkspaceStatus.ACTIVE,
        )
        session.add(workspace)
        session.flush()
        ws_id = workspace.id

        competitor = Competitor(
            workspace_id=ws_id,
            name="Resolution Live Competitor",
            domain=f"resolution-live-{unique}.example.com",
            status=CompetitorStatus.ACTIVE,
            legal_status=LegalStatus.APPROVED,
            robots_policy=RobotsPolicy.RESPECT,
        )
        session.add(competitor)
        session.flush()
        competitor_id = competitor.id

        product = Product(
            workspace_id=ws_id, title="Resolution Live Product", status=ProductStatus.ACTIVE
        )
        session.add(product)
        session.flush()

        # Out-of-band global default (research D11) -- the tenant API can
        # never produce this row; seeded directly here, mirroring
        # test_scrape_profiles_isolation_live.py.
        global_profile = ScrapeProfile(workspace_id=None, name="global_default")
        workspace_profile = ScrapeProfile(workspace_id=ws_id, name=f"ws-default-{unique}")
        competitor_profile = ScrapeProfile(workspace_id=ws_id, name=f"competitor-default-{unique}")
        override_profile = ScrapeProfile(workspace_id=ws_id, name=f"match-override-{unique}")
        session.add_all(
            [global_profile, workspace_profile, competitor_profile, override_profile]
        )
        session.flush()

        workspace.default_scrape_profile_id = workspace_profile.id
        competitor.default_scrape_profile_id = competitor_profile.id
        session.flush()

        shared_url_pattern = "/p/{sku}"
        variants = []
        matches = []
        for i in range(3):
            variant = ProductVariant(
                workspace_id=ws_id,
                product_id=product.id,
                title=f"Variant {i}",
                current_price=Decimal("9.9900"),
                currency="USD",
                status=VariantStatus.ACTIVE,
            )
            session.add(variant)
            session.flush()
            variants.append(variant)

            match = CompetitorProductMatch(
                workspace_id=ws_id,
                product_id=product.id,
                product_variant_id=variant.id,
                competitor_id=competitor_id,
                competitor_url=f"https://resolution-live-{unique}.example.com/p/{i}",
                normalized_competitor_url=f"https://resolution-live-{unique}.example.com/p/{i}",
                url_pattern=shared_url_pattern,
                url_pattern_version=1,
                priority=MatchPriority.NORMAL,
                status=MatchStatus.ACTIVE,
                scrape_profile_id=override_profile.id if i == 0 else None,
            )
            session.add(match)
            session.flush()
            matches.append(match)

        session.commit()

        ids = {
            "workspace_id": ws_id,
            "competitor_id": competitor_id,
            "global_profile_id": global_profile.id,
            "workspace_profile_id": workspace_profile.id,
            "competitor_profile_id": competitor_profile.id,
            "override_profile_id": override_profile.id,
            "match_ids": [m.id for m in matches],
            "match_with_override_id": matches[0].id,
            "matches_without_override_ids": [matches[1].id, matches[2].id],
            "url_pattern": shared_url_pattern,
        }

    try:
        yield ids
    finally:
        from sqlalchemy import text

        with get_session() as session:
            for match_id in ids["match_ids"]:
                session.execute(
                    text("DELETE FROM competitor_product_matches WHERE id = :id"),
                    {"id": match_id},
                )
            session.execute(
                text("DELETE FROM product_variants WHERE product_id IN "
                     "(SELECT id FROM products WHERE workspace_id = :ws)"),
                {"ws": ids["workspace_id"]},
            )
            session.execute(
                text("DELETE FROM products WHERE workspace_id = :ws"), {"ws": ids["workspace_id"]}
            )
            session.execute(
                text("DELETE FROM competitors WHERE id = :id"), {"id": ids["competitor_id"]}
            )
            for profile_id in (
                ids["global_profile_id"],
                ids["workspace_profile_id"],
                ids["competitor_profile_id"],
                ids["override_profile_id"],
            ):
                session.execute(
                    text("DELETE FROM scrape_profiles WHERE id = :id"), {"id": profile_id}
                )
            session.execute(
                text("DELETE FROM workspaces WHERE id = :ws"), {"ws": ids["workspace_id"]}
            )
            session.commit()


def _resolve(fixture: dict[str, object]) -> dict[uuid.UUID, object]:
    from app_shared.database import get_session
    from app_shared.models.competitors_matches import CompetitorProductMatch
    from app_shared.redis_client import get_redis_client

    from app.services.profile_resolution import resolve_profiles_for_matches

    with get_session() as session:
        rows = (
            session.query(CompetitorProductMatch)
            .filter(CompetitorProductMatch.id.in_(fixture["match_ids"]))
            .all()
        )
        return resolve_profiles_for_matches(
            session, get_redis_client(), fixture["workspace_id"], rows
        )


# --- end-to-end precedence (FR-014, FR-016) ---------------------------------


def test_match_override_wins_over_every_other_level(resolution_fixture) -> None:
    result = _resolve(resolution_fixture)
    override_result = result[resolution_fixture["match_with_override_id"]]
    assert override_result.profile_id == resolution_fixture["override_profile_id"]
    assert override_result.level == "match"


def test_no_override_falls_through_to_competitor_default(resolution_fixture) -> None:
    result = _resolve(resolution_fixture)
    for match_id in resolution_fixture["matches_without_override_ids"]:
        resolved = result[match_id]
        assert resolved.profile_id == resolution_fixture["competitor_profile_id"]
        assert resolved.level == "competitor"


def test_clearing_competitor_default_falls_through_to_workspace_default(
    resolution_fixture,
) -> None:
    from app_shared.database import get_session
    from app_shared.models.competitors_matches import Competitor

    with get_session() as session:
        competitor = session.get(Competitor, resolution_fixture["competitor_id"])
        competitor.default_scrape_profile_id = None
        session.commit()

    from app_shared.redis_client import get_redis_client

    from app.services.profile_resolution import invalidate_resolution_cache

    invalidate_resolution_cache(
        get_redis_client(), resolution_fixture["workspace_id"], resolution_fixture["competitor_id"]
    )

    result = _resolve(resolution_fixture)
    for match_id in resolution_fixture["matches_without_override_ids"]:
        resolved = result[match_id]
        assert resolved.profile_id == resolution_fixture["workspace_profile_id"]
        assert resolved.level == "workspace"


def test_clearing_workspace_and_competitor_defaults_falls_through_to_global(
    resolution_fixture,
) -> None:
    from app_shared.database import get_session
    from app_shared.models import Workspace
    from app_shared.models.competitors_matches import Competitor
    from app_shared.redis_client import get_redis_client

    from app.services.profile_resolution import invalidate_resolution_cache

    with get_session() as session:
        competitor = session.get(Competitor, resolution_fixture["competitor_id"])
        competitor.default_scrape_profile_id = None
        workspace = session.get(Workspace, resolution_fixture["workspace_id"])
        workspace.default_scrape_profile_id = None
        session.commit()

    invalidate_resolution_cache(
        get_redis_client(), resolution_fixture["workspace_id"], resolution_fixture["competitor_id"]
    )

    result = _resolve(resolution_fixture)
    for match_id in resolution_fixture["matches_without_override_ids"]:
        resolved = result[match_id]
        assert resolved.profile_id == resolution_fixture["global_profile_id"]
        assert resolved.level == "global"


def test_no_level_resolved_returns_none_resolved(resolution_fixture) -> None:
    from app_shared.database import get_session
    from app_shared.models import Workspace
    from app_shared.models.competitors_matches import Competitor
    from app_shared.redis_client import get_redis_client
    from sqlalchemy import text

    from app.services.profile_resolution import invalidate_resolution_cache
    from app_shared.profiles.resolution import NONE_RESOLVED

    with get_session() as session:
        competitor = session.get(Competitor, resolution_fixture["competitor_id"])
        competitor.default_scrape_profile_id = None
        workspace = session.get(Workspace, resolution_fixture["workspace_id"])
        workspace.default_scrape_profile_id = None
        # Temporarily rename the global default so it no longer resolves
        # by the reserved name (restored in the finally block).
        session.execute(
            text("UPDATE scrape_profiles SET name = name || '-renamed' WHERE id = :id"),
            {"id": resolution_fixture["global_profile_id"]},
        )
        session.commit()

    invalidate_resolution_cache(
        get_redis_client(), resolution_fixture["workspace_id"], resolution_fixture["competitor_id"]
    )

    try:
        result = _resolve(resolution_fixture)
        for match_id in resolution_fixture["matches_without_override_ids"]:
            assert result[match_id] is NONE_RESOLVED
    finally:
        with get_session() as session:
            session.execute(
                text(
                    "UPDATE scrape_profiles SET name = replace(name, '-renamed', '') "
                    "WHERE id = :id"
                ),
                {"id": resolution_fixture["global_profile_id"]},
            )
            session.commit()


# --- grouped, not per-match, DB access (FR-018, SC-004) ---------------------


def test_batch_db_statement_count_is_proportional_to_groups_not_matches(
    resolution_fixture,
) -> None:
    """Two batches sharing the same single group (all matches share
    `(competitor_id, url_pattern)`) but differing sizes (2 vs 3 matches)
    must issue the SAME bounded number of DB statements -- proving the
    orchestrator's DB access scales with `len(groups)`, not `len(matches)`
    (SC-004). A real per-match N+1 implementation would issue more
    statements for the larger batch; a grouped one issues identical
    counts."""
    from app_shared.database import get_session
    from app_shared.models.competitors_matches import CompetitorProductMatch
    from app_shared.redis_client import get_redis_client

    from app.services.profile_resolution import resolve_profiles_for_matches

    def _count_statements(match_ids: list[uuid.UUID]) -> int:
        with get_session() as session:
            rows = (
                session.query(CompetitorProductMatch)
                .filter(CompetitorProductMatch.id.in_(match_ids))
                .all()
            )
            statement_count = 0
            real_execute = session.execute

            def _counting_execute(*args, **kwargs):
                nonlocal statement_count
                statement_count += 1
                return real_execute(*args, **kwargs)

            session.execute = _counting_execute  # type: ignore[method-assign]
            resolve_profiles_for_matches(
                session, get_redis_client(), resolution_fixture["workspace_id"], rows
            )
            return statement_count

    two_match_count = _count_statements(resolution_fixture["matches_without_override_ids"][:2])
    three_match_count = _count_statements(resolution_fixture["match_ids"])

    assert two_match_count == three_match_count


# --- Redis cache hit within TTL (FR-019, SC-005) -----------------------------


def test_second_resolution_within_ttl_skips_the_chain_walk(resolution_fixture) -> None:
    import app_shared.profiles.resolution as resolution_module

    call_count = 0
    real_resolve_group = resolution_module.resolve_group

    def _counting_resolve_group(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return real_resolve_group(*args, **kwargs)

    resolution_module.resolve_group = _counting_resolve_group
    try:
        _resolve(resolution_fixture)
        first_call_count = call_count
        assert first_call_count >= 1

        _resolve(resolution_fixture)
        # A second resolution within the TTL for the same single group
        # must be entirely a cache hit -- zero further chain-walks.
        assert call_count == first_call_count
    finally:
        resolution_module.resolve_group = real_resolve_group


# --- write invalidation (FR-019) --------------------------------------------


def test_invalidate_resolution_cache_reflects_write_immediately(resolution_fixture) -> None:
    from app_shared.database import get_session
    from app_shared.models.competitors_matches import Competitor
    from app_shared.models.scrape_profiles import ScrapeProfile
    from app_shared.redis_client import get_redis_client

    from app.services.profile_resolution import invalidate_resolution_cache

    # Warm the cache with the original competitor default.
    _resolve(resolution_fixture)

    with get_session() as session:
        new_profile = ScrapeProfile(
            workspace_id=resolution_fixture["workspace_id"],
            name=f"new-competitor-default-{uuid.uuid4().hex[:8]}",
        )
        session.add(new_profile)
        session.flush()
        new_profile_id = new_profile.id

        competitor = session.get(Competitor, resolution_fixture["competitor_id"])
        competitor.default_scrape_profile_id = new_profile_id
        session.commit()

    invalidate_resolution_cache(
        get_redis_client(), resolution_fixture["workspace_id"], resolution_fixture["competitor_id"]
    )

    try:
        result = _resolve(resolution_fixture)
        for match_id in resolution_fixture["matches_without_override_ids"]:
            resolved = result[match_id]
            assert resolved.profile_id == new_profile_id
            assert resolved.level == "competitor"
    finally:
        from sqlalchemy import text

        with get_session() as session:
            session.execute(text("DELETE FROM scrape_profiles WHERE id = :id"), {"id": new_profile_id})
            session.commit()


def test_ttl_expiry_alone_eventually_reflects_write_without_explicit_invalidate(
    resolution_fixture,
) -> None:
    from app_shared.config import get_settings
    from app_shared.database import get_session
    from app_shared.models.competitors_matches import Competitor
    from app_shared.models.scrape_profiles import ScrapeProfile

    # Warm the cache with the original competitor default (no invalidate
    # call this time -- relies purely on TTL expiry, SC-005 "within the
    # TTL", not necessarily instantaneous).
    _resolve(resolution_fixture)

    with get_session() as session:
        new_profile = ScrapeProfile(
            workspace_id=resolution_fixture["workspace_id"],
            name=f"ttl-competitor-default-{uuid.uuid4().hex[:8]}",
        )
        session.add(new_profile)
        session.flush()
        new_profile_id = new_profile.id

        competitor = session.get(Competitor, resolution_fixture["competitor_id"])
        competitor.default_scrape_profile_id = new_profile_id
        session.commit()

    try:
        ttl_seconds = get_settings().PROFILE_RESOLUTION_CACHE_TTL_SECONDS
        time.sleep(ttl_seconds + 2)

        result = _resolve(resolution_fixture)
        for match_id in resolution_fixture["matches_without_override_ids"]:
            resolved = result[match_id]
            assert resolved.profile_id == new_profile_id
    finally:
        from sqlalchemy import text

        with get_session() as session:
            session.execute(text("DELETE FROM scrape_profiles WHERE id = :id"), {"id": new_profile_id})
            session.commit()

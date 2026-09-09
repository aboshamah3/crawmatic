"""The synthetic 100 x 5,000 fleet seeder's SHAPE and its guards (EPA D1, F22).

Nothing here connects to anything: every assertion is against the pure
derivation layer of `scripts/seed_synthetic_fleet.py` (counts, start
offsets, URLs, row dicts) and against the refusal paths, which is
exactly the part that must be right BEFORE the script is ever pointed at
a database — it writes ~1.6 million rows.

Two things get the most attention:

1. **The audit §10 arithmetic.** 100 workspaces x 5,000 products x the
   pilot's 1.185 matches/product must land on 592,500 matches and a
   14.4-minute (864 s) stagger that spans exactly 24 hours over 100
   stores. Those are the numbers the whole capacity model is built on;
   an off-by-one in the rounding would quietly change the offered load
   the fleet test measures.
2. **The guard.** It is an allowlist, and every refusal reason gets its
   own test, because the failure that matters is the one where the
   script runs against production.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# `scripts/` has no __init__.py -- same sys.path convention as
# `tests/unit/test_backfill_daily_rollups.py`.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.seed_synthetic_fleet import (  # noqa: E402
    DAILY_INTERVAL_MINUTES,
    DEFAULT_MATCHES_PER_PRODUCT,
    DEFAULT_PRODUCTS_PER_STORE,
    DEFAULT_SPACING_SECONDS,
    DEFAULT_STORES,
    OriginUrlRefusal,
    StagingRefusal,
    build_plan,
    competitor_row,
    extra_match_product_indexes,
    main,
    match_urls_for_product,
    refresh_rule_row,
    require_staging,
    store_slug,
    validate_origin_base_url,
    workspace_row,
    workspace_start_offsets,
)
from scripts import seed_synthetic_fleet as seeder  # noqa: E402

PUBLIC_ORIGIN = "https://fixture-origin-staging.up.railway.app"


def _args(**overrides) -> argparse.Namespace:
    base = {
        "i_know_this_is_staging": True,
        "database_url": "postgresql+psycopg://u:p@fleet-staging.internal-host:5432/db",
        "host_allowlist_token": [],
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# --------------------------------------------------------------------------
# Audit §10 arithmetic
# --------------------------------------------------------------------------


def test_default_fleet_is_the_audit_s_fleet() -> None:
    assert (DEFAULT_STORES, DEFAULT_PRODUCTS_PER_STORE) == (100, 5_000)
    assert DEFAULT_MATCHES_PER_PRODUCT == 1.185
    plan = build_plan(
        stores=DEFAULT_STORES,
        products_per_store=DEFAULT_PRODUCTS_PER_STORE,
        matches_per_product=DEFAULT_MATCHES_PER_PRODUCT,
    )
    assert plan.matches_per_store == 5_925
    assert plan.extra_matches_per_store == 925
    assert plan.total_products == 500_000
    assert plan.total_variants == 500_000
    assert plan.total_matches == 592_500


def test_start_times_are_jittered_14_point_4_minutes_apart() -> None:
    assert DEFAULT_SPACING_SECONDS == 864  # 14.4 min
    offsets = workspace_start_offsets(100)
    assert offsets[0] == 0
    assert offsets[1] - offsets[0] == 864
    assert len(offsets) == 100
    # 100 stores x 14.4 min covers exactly one 24-hour cycle.
    assert offsets[-1] + DEFAULT_SPACING_SECONDS == 86_400


def test_plan_rejects_impossible_shapes() -> None:
    with pytest.raises(ValueError):
        build_plan(stores=0, products_per_store=10, matches_per_product=1.0)
    with pytest.raises(ValueError):
        build_plan(stores=1, products_per_store=0, matches_per_product=1.0)
    with pytest.raises(ValueError, match="at least one match"):
        build_plan(stores=1, products_per_store=10, matches_per_product=0.5)
    with pytest.raises(ValueError, match="third distinct competitor URL"):
        build_plan(stores=1, products_per_store=10, matches_per_product=2.5)


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


def test_extra_match_products_are_deterministic_and_store_specific() -> None:
    plan = build_plan(stores=4, products_per_store=1_000, matches_per_product=1.185)
    first = extra_match_product_indexes("fleet-000", plan)
    assert len(first) == plan.extra_matches_per_store == 185
    assert first == extra_match_product_indexes("fleet-000", plan)
    # A different store gets a different (not merely shifted) subset --
    # otherwise the fleet is 100 identical catalogues and one hot key
    # would look like a fleet-wide effect.
    second = extra_match_product_indexes("fleet-001", plan)
    assert second != first
    assert len(first & second) < len(first)


def test_match_urls_point_at_the_fixture_origin_and_are_distinct() -> None:
    primary = match_urls_for_product(PUBLIC_ORIGIN, "fleet-000", 0, extra=False)
    assert primary == [f"{PUBLIC_ORIGIN}/p/fleet-000/p000001"]
    both = match_urls_for_product(PUBLIC_ORIGIN, "fleet-000", 0, extra=True)
    assert len(both) == 2
    assert both[0] != both[1]
    # The second match uses the JSON endpoint variant so the fleet
    # exercises both extraction paths at the same offered load.
    assert both[1].startswith(f"{PUBLIC_ORIGIN}/api/p/fleet-000/")


def test_trailing_slash_on_the_base_url_does_not_double_up() -> None:
    assert match_urls_for_product(PUBLIC_ORIGIN + "/", "fleet-000", 0, extra=False) == [
        f"{PUBLIC_ORIGIN}/p/fleet-000/p000001"
    ]


def test_every_seeded_url_passes_the_production_ssrf_validator() -> None:
    from app_shared.url_safety import validate_competitor_url

    plan = build_plan(stores=1, products_per_store=50, matches_per_product=1.185)
    extras = extra_match_product_indexes("fleet-000", plan)
    for index in range(plan.products_per_store):
        for url in match_urls_for_product(
            PUBLIC_ORIGIN, "fleet-000", index, extra=index in extras
        ):
            validate_competitor_url(url)  # raises on anything unsafe


def test_the_two_matches_on_one_variant_normalise_differently() -> None:
    """The match unique key is `(workspace_id, product_variant_id,
    competitor_id, normalized_competitor_url)` -- a product with two
    matches whose URLs normalised to the same string would silently
    collapse to one match and quietly shrink the fleet."""
    from app_shared.url_pattern import derive_match_url_fields

    urls = match_urls_for_product(PUBLIC_ORIGIN, "fleet-000", 3, extra=True)
    normalized = {derive_match_url_fields(url)[0] for url in urls}
    assert len(normalized) == 2


def test_store_slug_matches_the_fixture_origin_s_store_ids() -> None:
    assert store_slug(0) == "fleet-000"
    assert store_slug(99) == "fleet-099"


# --------------------------------------------------------------------------
# Row shapes
# --------------------------------------------------------------------------


def test_refresh_rule_is_one_daily_workspace_rule_with_a_jittered_start() -> None:
    from app_shared.enums import ScrapeScope

    anchor = datetime(2026, 9, 10, tzinfo=timezone.utc)
    row = refresh_rule_row(
        workspace_id=workspace_row(0, prefix="fleet")["id"],
        index=3,
        anchor=anchor,
        spacing_seconds=DEFAULT_SPACING_SECONDS,
    )
    assert row["scope"] is ScrapeScope.WORKSPACE
    assert row["interval_minutes"] == DAILY_INTERVAL_MINUTES == 1440
    # `exactly_one_cadence` CHECK: a cron expression alongside the
    # interval would violate the table constraint. 14.4 minutes is also
    # not expressible in a 5-field cron, which is why the stagger lives
    # in next_run_at.
    assert row["cron_expression"] is None
    assert row["enabled"] is True
    assert (row["next_run_at"] - anchor).total_seconds() == 3 * 864


def test_competitor_is_approved_because_it_is_our_own_fixture() -> None:
    from app_shared.enums import CompetitorStatus, LegalStatus, RobotsPolicy

    row = competitor_row(workspace_row(0, prefix="fleet")["id"], "fixture.example")
    assert row["status"] is CompetitorStatus.ACTIVE
    # Leaving these at their REVIEW_REQUIRED defaults would make the
    # whole fleet ineligible and the test would measure nothing. It is
    # only defensible because the "competitor" is our own service.
    assert row["legal_status"] is LegalStatus.APPROVED
    assert row["robots_policy"] is RobotsPolicy.IGNORE_AFTER_APPROVAL


def test_workspace_slugs_are_unique_and_prefixed() -> None:
    slugs = {workspace_row(index, prefix="fleet")["slug"] for index in range(100)}
    assert len(slugs) == 100
    assert "fleet-042" in slugs


def test_generated_match_rows_hit_the_planned_count_exactly() -> None:
    plan = build_plan(stores=1, products_per_store=200, matches_per_product=1.185)
    workspace = workspace_row(0, prefix="fleet")
    competitor = competitor_row(workspace["id"], "fixture.example")
    pairs = list(seeder._product_rows(workspace["id"], "fleet-000", plan))
    assert len(pairs) == 200
    rows = list(
        seeder._match_rows(
            workspace["id"],
            competitor["id"],
            "fleet-000",
            plan,
            PUBLIC_ORIGIN,
            pairs,
        )
    )
    assert len(rows) == plan.matches_per_store == 237
    keys = {
        (row["product_variant_id"], row["competitor_id"], row["normalized_competitor_url"])
        for row in rows
    }
    assert len(keys) == len(rows), "a duplicate would collapse under the unique key"
    assert all(row["workspace_id"] == workspace["id"] for row in rows)


# --------------------------------------------------------------------------
# The staging guard
# --------------------------------------------------------------------------


def test_guard_refuses_without_the_acknowledgement() -> None:
    with pytest.raises(StagingRefusal, match="i-know-this-is-staging"):
        require_staging(_args(i_know_this_is_staging=False), env={})


def test_guard_refuses_without_an_explicit_database_url() -> None:
    with pytest.raises(StagingRefusal, match="never read from the"):
        require_staging(_args(database_url=None), env={})


def test_guard_never_falls_back_to_the_ambient_database_url(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@prod.example:5432/db")
    with pytest.raises(StagingRefusal):
        require_staging(_args(database_url=None), env={})


def test_guard_refuses_a_host_outside_the_allowlist() -> None:
    with pytest.raises(StagingRefusal, match="not on the staging allowlist"):
        require_staging(
            _args(database_url="postgresql://u:p@prod-db.railway.app:5432/railway"),
            env={},
        )


def test_guard_refusal_never_prints_the_credential() -> None:
    url = "postgresql://someuser:sup3rsecret@prod-db.example:5432/railway"
    with pytest.raises(StagingRefusal) as caught:
        require_staging(_args(database_url=url), env={})
    message = str(caught.value)
    assert "sup3rsecret" not in message
    assert "someuser" not in message
    assert "prod-db.example" in message


def test_guard_refuses_inside_a_production_railway_environment() -> None:
    with pytest.raises(StagingRefusal, match="production environment"):
        require_staging(
            _args(database_url="postgresql://u:p@staging-db.example:5432/db"),
            env={"RAILWAY_ENVIRONMENT_NAME": "production"},
        )


def test_guard_matches_the_d2_fault_injection_guard_on_environment_names() -> None:
    """Same posture as `tests/load/fault_injection/_common.py` (EPA D2):
    two implementations, one rule."""
    from tests.load.fault_injection import _common

    assert set(seeder.PRODUCTION_ENVIRONMENT_NAMES) == set(
        _common.PRODUCTION_ENVIRONMENT_NAMES
    )


@pytest.mark.parametrize(
    "host",
    ["localhost", "127.0.0.1", "postgres", "pgbouncer", "fleet-staging.example", "x-fixture.example"],
)
def test_guard_accepts_staging_and_local_hosts(host) -> None:
    require_staging(_args(database_url=f"postgresql://u:p@{host}:5432/db"), env={})


def test_guard_accepts_an_explicitly_allowlisted_token() -> None:
    args = _args(
        database_url="postgresql://u:p@shadow-db.example:5432/db",
        host_allowlist_token=["shadow-db"],
    )
    require_staging(args, env={})


# --------------------------------------------------------------------------
# The origin URL guard
# --------------------------------------------------------------------------


def test_origin_url_must_be_a_public_domain() -> None:
    assert validate_origin_base_url(PUBLIC_ORIGIN + "/") == PUBLIC_ORIGIN


@pytest.mark.parametrize(
    "url",
    [
        "http://fixture-origin.railway.internal:8080",
        "http://localhost:8080",
        "http://10.0.0.5:8080",
        "http://169.254.169.254",
    ],
)
def test_origin_url_refuses_internal_targets(url) -> None:
    with pytest.raises(OriginUrlRefusal):
        validate_origin_base_url(url)


def test_allow_loopback_widens_to_loopback_only() -> None:
    assert (
        validate_origin_base_url("http://127.0.0.1:8931", allow_loopback=True)
        == "http://127.0.0.1:8931"
    )
    # ...and to nothing else: a private LAN address and the cloud
    # metadata address stay refused even with the flag on.
    for url in ("http://10.0.0.5:8080", "http://169.254.169.254", "http://192.168.1.9"):
        with pytest.raises(OriginUrlRefusal):
            validate_origin_base_url(url, allow_loopback=True)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_cli_dry_run_connects_to_nothing(capsys) -> None:
    assert main(["--dry-run", "--stores", "100", "--products-per-store", "5000"]) == 0
    out = capsys.readouterr().out
    assert "total_matches: 592500" in out
    assert "/p/fleet-000/p000001" in out


def test_cli_refuses_without_the_acknowledgement(capsys) -> None:
    code = main(["--stores", "2", "--products-per-store", "5"])
    assert code == 2
    assert "REFUSED" in capsys.readouterr().err


def test_cli_refuses_a_production_host(capsys) -> None:
    code = main(
        [
            "--i-know-this-is-staging",
            "--database-url",
            "postgresql://u:p@prod-db.railway.app:5432/railway",
            "--origin-base-url",
            PUBLIC_ORIGIN,
        ]
    )
    assert code == 2
    assert "staging allowlist" in capsys.readouterr().err


def test_cli_refuses_an_internal_origin_url(capsys) -> None:
    code = main(
        [
            "--i-know-this-is-staging",
            "--database-url",
            "postgresql://u:p@fleet-staging.example:5432/db",
            "--origin-base-url",
            "http://fixture-origin.railway.internal:8080",
        ]
    )
    assert code == 2
    assert "not a valid competitor URL" in capsys.readouterr().err

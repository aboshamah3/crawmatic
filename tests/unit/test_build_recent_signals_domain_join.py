"""`build_recent_signals` domain-scoped join fix (Task 3.3,
proxy-cost-reduction plan §3.3), behind `STRATEGY_SIGNALS_DOMAIN_JOIN`
(default OFF).

Under `Settings.STRATEGY_PROFILE_SCOPE="domain"`, `resolve_or_create_strategy_profile`
stamps `profile.url_pattern` with the bare competitor domain (no path) --
but `competitor_product_matches.url_pattern` is `derive_url_pattern`'s
host+path grouping key. `build_recent_signals`'s join compares these two
directly (`CompetitorProductMatch.url_pattern == profile.url_pattern`),
which is a category mismatch: a path-bearing pattern can never equal a
bare domain, so it matches 0 of 4,588 rows measured 2026-08-16 --
rediscovery conditions 3, 5, 6, 7, 8 are silently dead code for every
domain-scoped profile.

`build_recent_signals` is DB-touching (a real join across
`request_attempts`/`competitor_product_matches`/`price_observations`/
`scrape_profiles`) with no live-Postgres unit coverage in this repo
(`test_build_recent_signals_origin_filter.py`'s precedent) -- so, same
technique: a query-shape test capturing the `Select` handed to
`session.execute` and asserting on its compiled SQL (`literal_binds`),
without needing a live database. Plus a direct behavioral test of the
pure `_domain_match_terms` helper the query-building leans on.
"""

from __future__ import annotations

import uuid

import app_shared.strategy.rediscovery as rediscovery
from app_shared.enums import AccessMethod
from app_shared.models.strategy import DomainStrategyProfile
from app_shared.strategy.rediscovery import _domain_match_terms, build_recent_signals


class _EmptyResult:
    def all(self) -> list[object]:
        return []


class _CapturingSession:
    """Fake `Session` whose only job is to remember the `Select` it was
    asked to execute -- never touches a real database (mirrors
    `test_build_recent_signals_origin_filter.py`)."""

    def __init__(self) -> None:
        self.captured_stmt: object | None = None

    def execute(self, stmt: object) -> _EmptyResult:
        self.captured_stmt = stmt
        return _EmptyResult()


class _StubSettings:
    """Minimal `get_settings()` stand-in -- only the two knobs
    `build_recent_signals` reads, mirroring
    `test_promotion_degraded_repromotion.py`'s `_StubFlushSettings`
    seam style."""

    def __init__(self, *, domain_join: bool, scope: str) -> None:
        self.STRATEGY_SIGNALS_DOMAIN_JOIN = domain_join
        self.STRATEGY_PROFILE_SCOPE = scope


def _domain_profile(*, domain: str = "extra.com") -> DomainStrategyProfile:
    return DomainStrategyProfile(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        competitor_id=uuid.uuid4(),
        domain=domain,
        url_pattern=domain,  # domain-scope contract: url_pattern == bare domain
        url_pattern_version=1,
        preferred_access_method=AccessMethod.DIRECT_HTTP,
    )


def _compiled_sql(stmt: object) -> str:
    compiled = stmt.compile(compile_kwargs={"literal_binds": True})  # type: ignore[attr-defined]
    return str(compiled)


# --- 1. Pure behavioral proof: registrable-domain matching, not exact
# pattern equality (reuses `_bare_host`, the 36fd624 www-stripping fix). ---


def test_domain_match_terms_strips_www_both_directions() -> None:
    assert _domain_match_terms("extra.com") == ("extra.com", "www.extra.com")
    # A `domain` value that itself carries `www.` (operator/discovery
    # supplied, not guaranteed normalized) still yields the same pair --
    # `_bare_host` reused, not a fresh strip.
    assert _domain_match_terms("www.Extra.com") == ("extra.com", "www.extra.com")


# --- 2. Query-shape: flag OFF is byte-identical to today's broken join,
# regardless of scope -- the rollback guarantee. ---


def test_domain_join_flag_off_preserves_exact_equality_join(monkeypatch) -> None:
    monkeypatch.setattr(
        rediscovery, "get_settings", lambda: _StubSettings(domain_join=False, scope="domain")
    )
    session = _CapturingSession()
    profile = _domain_profile()

    build_recent_signals(session, profile)

    sql = _compiled_sql(session.captured_stmt)
    assert "competitor_product_matches.url_pattern = 'extra.com'" in sql, sql
    assert "LIKE" not in sql, sql
    assert "www.extra.com" not in sql, sql


# --- 3. Query-shape: flag ON + scope != "domain" also falls back
# unchanged -- the domain join only ever applies under domain scope. ---


def test_domain_join_flag_on_ignored_outside_domain_scope(monkeypatch) -> None:
    monkeypatch.setattr(
        rediscovery,
        "get_settings",
        lambda: _StubSettings(domain_join=True, scope="url_pattern"),
    )
    session = _CapturingSession()
    profile = _domain_profile()

    build_recent_signals(session, profile)

    sql = _compiled_sql(session.captured_stmt)
    assert "competitor_product_matches.url_pattern = 'extra.com'" in sql, sql
    assert "LIKE" not in sql, sql


# --- 4. Query-shape: flag ON + domain scope aggregates across both
# www-prefixed and bare match patterns, any path suffix -- THE fix. ---


def test_domain_join_flag_on_matches_bare_and_www_patterns(monkeypatch) -> None:
    monkeypatch.setattr(
        rediscovery, "get_settings", lambda: _StubSettings(domain_join=True, scope="domain")
    )
    session = _CapturingSession()
    profile = _domain_profile(domain="extra.com")

    build_recent_signals(session, profile)

    sql = _compiled_sql(session.captured_stmt)
    # Bare host, no path (e.g. a match whose pattern is just the host).
    assert "competitor_product_matches.url_pattern = 'extra.com'" in sql, sql
    # Bare host with a path suffix -- what `derive_url_pattern` actually
    # stamps for a real product URL, e.g. "extra.com/products/*".
    assert "competitor_product_matches.url_pattern LIKE 'extra.com/%'" in sql, sql
    # www-prefixed forms -- the exact category the old exact-equality
    # join could never match under domain scope (0 of 4,588 rows).
    assert "competitor_product_matches.url_pattern = 'www.extra.com'" in sql, sql
    assert "competitor_product_matches.url_pattern LIKE 'www.extra.com/%'" in sql, sql
    # Never a naive equality against the bare profile.url_pattern alone --
    # this is an OR of four disjoint branches, not exact pattern equality.
    assert " OR " in sql, sql


def test_domain_join_flag_on_normalizes_www_prefixed_profile_domain(monkeypatch) -> None:
    """A profile whose own `domain` column carries `www.` (not guaranteed
    normalized, `competitors.domain` is operator/discovery-supplied)
    still produces the same bare/www comparison pair -- `_bare_host`
    reused symmetrically on both sides of the join."""
    monkeypatch.setattr(
        rediscovery, "get_settings", lambda: _StubSettings(domain_join=True, scope="domain")
    )
    session = _CapturingSession()
    profile = _domain_profile(domain="www.extra.com")

    build_recent_signals(session, profile)

    sql = _compiled_sql(session.captured_stmt)
    assert "competitor_product_matches.url_pattern = 'extra.com'" in sql, sql
    assert "competitor_product_matches.url_pattern LIKE 'www.extra.com/%'" in sql, sql
    # Never a double-`www.` artifact from stripping the wrong side twice.
    assert "www.www.extra.com" not in sql, sql

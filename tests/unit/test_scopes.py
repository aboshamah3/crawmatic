"""Unit tests for the API-key scope vocabulary (SPEC-03 T034, FR-013).

`app_shared.security.scopes` — no DB/Redis required.
"""

from __future__ import annotations

import pytest

from app_shared.security.scopes import Scope, has_scopes, validate_scopes

FULL_VOCABULARY = {
    "products:read",
    "products:write",
    "variants:read",
    "variants:write",
    "competitors:read",
    "competitors:write",
    "matches:read",
    "matches:write",
    "jobs:run",
    "jobs:read",
    "jobs:write",
    "results:read",
    "alerts:read",
    "webhooks:read",
    "webhooks:write",
    "scrape_profiles:read",
    "scrape_profiles:write",
    "proxy_providers:read",
    "proxy_providers:write",
    "access_policies:read",
    "access_policies:write",
    "domain_rules:read",
    "domain_rules:write",
    "refresh_rules:read",
    "refresh_rules:write",
}


def test_full_vocabulary_matches_spec() -> None:
    assert {member.value for member in Scope} == FULL_VOCABULARY
    assert len(FULL_VOCABULARY) == 25


def test_access_policy_scopes_are_in_the_vocabulary() -> None:
    """SPEC-10 T003 (FR-001/FR-002/FR-004): the six new access-control scopes
    (`proxy_providers:*`/`access_policies:*`/`domain_rules:*`) mint alongside
    the pre-existing vocabulary for the SPEC-10 CRUD routers."""
    assert Scope("proxy_providers:read") == Scope.PROXY_PROVIDERS_READ
    assert Scope("proxy_providers:write") == Scope.PROXY_PROVIDERS_WRITE
    assert Scope("access_policies:read") == Scope.ACCESS_POLICIES_READ
    assert Scope("access_policies:write") == Scope.ACCESS_POLICIES_WRITE
    assert Scope("domain_rules:read") == Scope.DOMAIN_RULES_READ
    assert Scope("domain_rules:write") == Scope.DOMAIN_RULES_WRITE
    assert validate_scopes(["proxy_providers:read", "domain_rules:write"]) == [
        "proxy_providers:read",
        "domain_rules:write",
    ]


def test_refresh_rules_scopes_are_in_the_vocabulary() -> None:
    """SPEC-13 T031 (FR-004/US1): the two `refresh_rules:*` scopes are minted
    alongside the pre-existing vocabulary for the `/v1/refresh-rules` CRUD
    router, which already gates every endpoint on these exact strings via
    `require_scopes(...)`."""
    assert Scope("refresh_rules:read") == Scope.REFRESH_RULES_READ
    assert Scope("refresh_rules:write") == Scope.REFRESH_RULES_WRITE
    assert validate_scopes(["refresh_rules:read", "refresh_rules:write"]) == [
        "refresh_rules:read",
        "refresh_rules:write",
    ]


def test_jobs_write_scope_is_in_the_vocabulary() -> None:
    """SPEC-08 T018 (FR-006/FR-007/FR-010): `jobs:write` is minted alongside
    the pre-existing `jobs:read`/`jobs:run` for the run-match/run-variant
    endpoints (which require write, following the `matches:*` precedent)."""
    assert Scope("jobs:write") == Scope.JOBS_WRITE
    assert "jobs:write" in {member.value for member in Scope}
    assert validate_scopes(["jobs:write"]) == ["jobs:write"]


def test_validate_scopes_accepts_known_scope() -> None:
    assert validate_scopes(["products:read"]) == ["products:read"]


def test_validate_scopes_accepts_multiple_known_scopes() -> None:
    result = validate_scopes(["products:read", "jobs:run"])
    assert result == ["products:read", "jobs:run"]


def test_validate_scopes_raises_on_unknown_scope() -> None:
    with pytest.raises(ValueError):
        validate_scopes(["bogus:read"])


def test_validate_scopes_raises_on_one_unknown_among_known() -> None:
    with pytest.raises(ValueError):
        validate_scopes(["products:read", "bogus:read"])


def test_has_scopes_true_when_all_required_are_granted() -> None:
    assert has_scopes(["a", "b"], ["a"]) is True


def test_has_scopes_false_when_a_required_scope_is_missing() -> None:
    assert has_scopes(["a"], ["a", "b"]) is False


def test_has_scopes_true_for_empty_required() -> None:
    assert has_scopes(["a"], []) is True


def test_has_scopes_false_for_empty_granted_nonempty_required() -> None:
    assert has_scopes([], ["a"]) is False

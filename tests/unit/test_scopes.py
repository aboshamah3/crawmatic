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
    "results:read",
    "alerts:read",
    "webhooks:read",
    "webhooks:write",
}


def test_full_vocabulary_matches_spec() -> None:
    assert {member.value for member in Scope} == FULL_VOCABULARY
    assert len(FULL_VOCABULARY) == 14


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

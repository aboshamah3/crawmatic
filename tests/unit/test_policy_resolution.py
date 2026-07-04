"""`app_shared/access/resolution.py` unit tests (SPEC-10 US2 T029,
`contracts/policy-resolution.md` Acceptance, FR-007, SC-004).

Pure in-memory exercises of the resolution chain -- no DB, no Redis. The
"a batch of N matches in one group walks the chain once" claim is an
orchestrator-level (T025, `apps/api/app/services/access_resolution.py`)
behavior, not something the pure `select_domain_rule`/
`resolve_effective_policy` functions exercised here can demonstrate on
their own -- this file focuses on those two pure functions plus the
cache-key/codec helpers.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app_shared.access.resolution import (
    NONE_RESOLVED,
    ResolvedPolicy,
    access_resolution_cache_key,
    decode_result,
    encode_result,
    resolve_effective_policy,
    select_domain_rule,
)


def _uuids(n: int) -> list[uuid.UUID]:
    return [uuid.uuid4() for _ in range(n)]


# --- resolve_effective_policy precedence (FR-007, SC-004) -------------------


def test_resolve_effective_policy_prefers_domain_rule_over_workspace_and_global() -> None:
    domain_rule, workspace, glob = _uuids(3)
    visible = {domain_rule, workspace, glob}

    result = resolve_effective_policy(
        domain_rule_policy_id=domain_rule,
        workspace_default_policy_id=workspace,
        global_default_policy_id=glob,
        visible_ids=visible,
    )

    assert result == ResolvedPolicy(policy_id=domain_rule, level="domain_rule")


def test_resolve_effective_policy_falls_through_to_workspace_when_domain_rule_unset() -> None:
    workspace, glob = _uuids(2)
    visible = {workspace, glob}

    result = resolve_effective_policy(
        domain_rule_policy_id=None,
        workspace_default_policy_id=workspace,
        global_default_policy_id=glob,
        visible_ids=visible,
    )

    assert result == ResolvedPolicy(policy_id=workspace, level="workspace")


def test_resolve_effective_policy_falls_through_to_global_when_domain_rule_and_workspace_unset() -> None:
    glob = uuid.uuid4()
    visible = {glob}

    result = resolve_effective_policy(
        domain_rule_policy_id=None,
        workspace_default_policy_id=None,
        global_default_policy_id=glob,
        visible_ids=visible,
    )

    assert result == ResolvedPolicy(policy_id=glob, level="global")


def test_resolve_effective_policy_prefers_workspace_over_global_when_domain_rule_unset() -> None:
    workspace, glob = _uuids(2)
    visible = {workspace, glob}

    result = resolve_effective_policy(
        domain_rule_policy_id=None,
        workspace_default_policy_id=workspace,
        global_default_policy_id=glob,
        visible_ids=visible,
    )

    assert result == ResolvedPolicy(policy_id=workspace, level="workspace")


def test_resolve_effective_policy_all_unset_returns_none_resolved() -> None:
    result = resolve_effective_policy(
        domain_rule_policy_id=None,
        workspace_default_policy_id=None,
        global_default_policy_id=None,
        visible_ids=set(),
    )

    assert result is NONE_RESOLVED


def test_resolve_effective_policy_dangling_domain_rule_falls_through_to_workspace() -> None:
    dangling_domain_rule = uuid.uuid4()
    workspace = uuid.uuid4()
    visible = {workspace}  # dangling_domain_rule deliberately not visible

    result = resolve_effective_policy(
        domain_rule_policy_id=dangling_domain_rule,
        workspace_default_policy_id=workspace,
        global_default_policy_id=None,
        visible_ids=visible,
    )

    assert result == ResolvedPolicy(policy_id=workspace, level="workspace")


def test_resolve_effective_policy_cross_workspace_workspace_default_falls_through_to_global() -> None:
    cross_ws_workspace_default = uuid.uuid4()
    glob = uuid.uuid4()
    visible = {glob}  # cross_ws_workspace_default belongs to another workspace

    result = resolve_effective_policy(
        domain_rule_policy_id=None,
        workspace_default_policy_id=cross_ws_workspace_default,
        global_default_policy_id=glob,
        visible_ids=visible,
    )

    assert result == ResolvedPolicy(policy_id=glob, level="global")


def test_resolve_effective_policy_all_ids_dangling_or_cross_ws_returns_none_resolved() -> None:
    domain_rule, workspace, glob = _uuids(3)
    visible: set[uuid.UUID] = set()

    result = resolve_effective_policy(
        domain_rule_policy_id=domain_rule,
        workspace_default_policy_id=workspace,
        global_default_policy_id=glob,
        visible_ids=visible,
    )

    assert result is NONE_RESOLVED


# --- select_domain_rule: enabled/disabled + specificity ---------------------


@dataclass(frozen=True)
class _FakeRule:
    id: uuid.UUID
    domain: str
    url_pattern: str | None
    access_policy_id: uuid.UUID
    enabled: bool = True


def test_select_domain_rule_disabled_rule_is_ignored() -> None:
    policy_id = uuid.uuid4()
    rule = _FakeRule(
        id=uuid.uuid4(), domain="shop.example.com", url_pattern=None, access_policy_id=policy_id, enabled=False
    )

    result = select_domain_rule([rule], domain="shop.example.com", url="https://shop.example.com/p/1")

    assert result is None


def test_select_domain_rule_domain_only_applies_when_no_pattern_matches() -> None:
    policy_id = uuid.uuid4()
    domain_only = _FakeRule(
        id=uuid.uuid4(), domain="shop.example.com", url_pattern=None, access_policy_id=policy_id
    )

    result = select_domain_rule(
        [domain_only], domain="shop.example.com", url="https://shop.example.com/anything"
    )

    assert result is domain_only


def test_select_domain_rule_url_pattern_beats_domain_only_for_a_matching_url() -> None:
    domain_only = _FakeRule(
        id=uuid.uuid4(), domain="shop.example.com", url_pattern=None, access_policy_id=uuid.uuid4()
    )
    pattern_rule = _FakeRule(
        id=uuid.uuid4(),
        domain="shop.example.com",
        url_pattern="/electronics/",
        access_policy_id=uuid.uuid4(),
    )

    result = select_domain_rule(
        [domain_only, pattern_rule],
        domain="shop.example.com",
        url="https://shop.example.com/electronics/laptop-1",
    )

    assert result is pattern_rule


def test_select_domain_rule_domain_only_applies_when_pattern_rule_does_not_match_this_url() -> None:
    domain_only = _FakeRule(
        id=uuid.uuid4(), domain="shop.example.com", url_pattern=None, access_policy_id=uuid.uuid4()
    )
    pattern_rule = _FakeRule(
        id=uuid.uuid4(),
        domain="shop.example.com",
        url_pattern="/electronics/",
        access_policy_id=uuid.uuid4(),
    )

    result = select_domain_rule(
        [domain_only, pattern_rule],
        domain="shop.example.com",
        url="https://shop.example.com/clothing/shirt-1",
    )

    assert result is domain_only


def test_select_domain_rule_longest_matching_pattern_wins() -> None:
    short_pattern = _FakeRule(
        id=uuid.uuid4(), domain="shop.example.com", url_pattern="/electronics/", access_policy_id=uuid.uuid4()
    )
    long_pattern = _FakeRule(
        id=uuid.uuid4(),
        domain="shop.example.com",
        url_pattern="/electronics/laptops/",
        access_policy_id=uuid.uuid4(),
    )

    result = select_domain_rule(
        [short_pattern, long_pattern],
        domain="shop.example.com",
        url="https://shop.example.com/electronics/laptops/model-1",
    )

    assert result is long_pattern


def test_select_domain_rule_different_domain_never_matches() -> None:
    rule = _FakeRule(
        id=uuid.uuid4(), domain="shop.example.com", url_pattern=None, access_policy_id=uuid.uuid4()
    )

    result = select_domain_rule([rule], domain="other.example.com", url="https://other.example.com/p/1")

    assert result is None


def test_select_domain_rule_no_rules_returns_none() -> None:
    assert select_domain_rule([], domain="shop.example.com", url="https://shop.example.com/p/1") is None


# --- cache key + codec -------------------------------------------------------


def test_access_resolution_cache_key_is_deterministic_and_bounded() -> None:
    workspace_id = uuid.uuid4()
    competitor_id = uuid.uuid4()

    key1 = access_resolution_cache_key(workspace_id, competitor_id, "shop.example.com", "/p/{sku}")
    key2 = access_resolution_cache_key(workspace_id, competitor_id, "shop.example.com", "/p/{sku}")
    key3 = access_resolution_cache_key(workspace_id, competitor_id, "shop.example.com", None)

    assert key1 == key2
    assert key1 != key3
    assert key1.startswith(f"accres:{workspace_id}:{competitor_id}:")


def test_encode_decode_round_trip_for_resolved_policy() -> None:
    result = ResolvedPolicy(policy_id=uuid.uuid4(), level="workspace")

    assert decode_result(encode_result(result)) == result


def test_encode_decode_round_trip_for_none_resolved() -> None:
    assert decode_result(encode_result(NONE_RESOLVED)) is NONE_RESOLVED


def test_decode_result_raises_on_corrupt_payload() -> None:
    import pytest

    with pytest.raises((ValueError, AttributeError)):
        decode_result("not-a-uuid|workspace")

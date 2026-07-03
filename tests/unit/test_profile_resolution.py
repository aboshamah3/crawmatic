"""`app_shared/profiles/resolution.py` unit tests (SPEC-06 US3 T037,
FR-014..FR-018, SC-003).

Pure in-memory exercises of the resolution chain -- no DB, no Redis.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app_shared.profiles.resolution import (
    NONE_RESOLVED,
    ResolvedProfile,
    apply_match_override,
    group_key,
    group_matches,
    resolve_group,
)


def _uuids(n: int) -> list[uuid.UUID]:
    return [uuid.uuid4() for _ in range(n)]


# --- resolve_group precedence chain (FR-014, FR-015) ------------------------


def test_resolve_group_prefers_competitor_over_workspace_and_global() -> None:
    competitor, workspace, glob = _uuids(3)
    visible = {competitor, workspace, glob}

    result = resolve_group(
        competitor_default_id=competitor,
        workspace_default_id=workspace,
        global_default_id=glob,
        visible_ids=visible,
    )

    assert result == ResolvedProfile(profile_id=competitor, level="competitor")


def test_resolve_group_falls_through_to_workspace_when_competitor_unset() -> None:
    workspace, glob = _uuids(2)
    visible = {workspace, glob}

    result = resolve_group(
        competitor_default_id=None,
        workspace_default_id=workspace,
        global_default_id=glob,
        visible_ids=visible,
    )

    assert result == ResolvedProfile(profile_id=workspace, level="workspace")


def test_resolve_group_falls_through_to_global_when_competitor_and_workspace_unset() -> None:
    glob = uuid.uuid4()
    visible = {glob}

    result = resolve_group(
        competitor_default_id=None,
        workspace_default_id=None,
        global_default_id=glob,
        visible_ids=visible,
    )

    assert result == ResolvedProfile(profile_id=glob, level="global")


def test_resolve_group_prefers_workspace_over_global_when_competitor_unset() -> None:
    workspace, glob = _uuids(2)
    visible = {workspace, glob}

    result = resolve_group(
        competitor_default_id=None,
        workspace_default_id=workspace,
        global_default_id=glob,
        visible_ids=visible,
    )

    assert result == ResolvedProfile(profile_id=workspace, level="workspace")


def test_resolve_group_all_unset_returns_none_resolved() -> None:
    result = resolve_group(
        competitor_default_id=None,
        workspace_default_id=None,
        global_default_id=None,
        visible_ids=set(),
    )

    assert result is NONE_RESOLVED


# --- visibility fall-through (FR-017): dangling/cross-ws id -> unset -------


def test_resolve_group_dangling_competitor_id_falls_through_to_workspace() -> None:
    dangling_competitor = uuid.uuid4()
    workspace = uuid.uuid4()
    # dangling_competitor is deliberately NOT in visible_ids.
    visible = {workspace}

    result = resolve_group(
        competitor_default_id=dangling_competitor,
        workspace_default_id=workspace,
        global_default_id=None,
        visible_ids=visible,
    )

    assert result == ResolvedProfile(profile_id=workspace, level="workspace")


def test_resolve_group_cross_workspace_workspace_default_falls_through_to_global() -> None:
    cross_ws_workspace_default = uuid.uuid4()
    glob = uuid.uuid4()
    # cross_ws_workspace_default not visible (belongs to another workspace).
    visible = {glob}

    result = resolve_group(
        competitor_default_id=None,
        workspace_default_id=cross_ws_workspace_default,
        global_default_id=glob,
        visible_ids=visible,
    )

    assert result == ResolvedProfile(profile_id=glob, level="global")


def test_resolve_group_dangling_global_default_returns_none_resolved() -> None:
    dangling_global = uuid.uuid4()
    visible: set[uuid.UUID] = set()

    result = resolve_group(
        competitor_default_id=None,
        workspace_default_id=None,
        global_default_id=dangling_global,
        visible_ids=visible,
    )

    assert result is NONE_RESOLVED


def test_resolve_group_all_ids_dangling_or_cross_ws_returns_none_resolved() -> None:
    competitor, workspace, glob = _uuids(3)
    # None of the three candidate ids are in visible_ids.
    visible: set[uuid.UUID] = set()

    result = resolve_group(
        competitor_default_id=competitor,
        workspace_default_id=workspace,
        global_default_id=glob,
        visible_ids=visible,
    )

    assert result is NONE_RESOLVED


# --- domain-strategy is a true no-op (FR-015) -------------------------------


def test_resolve_group_domain_strategy_none_is_a_true_noop() -> None:
    competitor = uuid.uuid4()
    visible = {competitor}

    with_default = resolve_group(
        competitor_default_id=competitor,
        workspace_default_id=None,
        global_default_id=None,
        visible_ids=visible,
    )
    with_explicit_none = resolve_group(
        competitor_default_id=competitor,
        workspace_default_id=None,
        global_default_id=None,
        visible_ids=visible,
        domain_strategy_id=None,
    )

    assert with_default == with_explicit_none == ResolvedProfile(
        profile_id=competitor, level="competitor"
    )


def test_resolve_group_domain_strategy_none_does_not_prevent_none_resolved() -> None:
    result = resolve_group(
        competitor_default_id=None,
        workspace_default_id=None,
        global_default_id=None,
        visible_ids=set(),
        domain_strategy_id=None,
    )

    assert result is NONE_RESOLVED


# --- group_key / group_matches (FR-018) -------------------------------------


@dataclass(frozen=True)
class _FakeMatch:
    id: uuid.UUID
    competitor_id: uuid.UUID
    url_pattern: str


def test_group_key_is_competitor_id_and_url_pattern() -> None:
    competitor_id = uuid.uuid4()
    match = _FakeMatch(id=uuid.uuid4(), competitor_id=competitor_id, url_pattern="/p/{sku}")

    assert group_key(match) == (competitor_id, "/p/{sku}")


def test_group_matches_yields_one_bucket_per_distinct_group() -> None:
    competitor_a, competitor_b = _uuids(2)
    m1 = _FakeMatch(id=uuid.uuid4(), competitor_id=competitor_a, url_pattern="/p/{sku}")
    m2 = _FakeMatch(id=uuid.uuid4(), competitor_id=competitor_a, url_pattern="/p/{sku}")
    m3 = _FakeMatch(id=uuid.uuid4(), competitor_id=competitor_a, url_pattern="/other")
    m4 = _FakeMatch(id=uuid.uuid4(), competitor_id=competitor_b, url_pattern="/p/{sku}")

    groups = group_matches([m1, m2, m3, m4])

    assert len(groups) == 3
    assert groups[(competitor_a, "/p/{sku}")] == [m1, m2]
    assert groups[(competitor_a, "/other")] == [m3]
    assert groups[(competitor_b, "/p/{sku}")] == [m4]


def test_group_matches_empty_input_yields_empty_dict() -> None:
    assert group_matches([]) == {}


# --- apply_match_override (FR-014 scenario 1, highest precedence) ----------


def test_apply_match_override_beats_group_result_when_visible() -> None:
    group_winner = uuid.uuid4()
    override = uuid.uuid4()
    group_result = ResolvedProfile(profile_id=group_winner, level="competitor")

    result = apply_match_override(group_result, override, visible_ids={override, group_winner})

    assert result == ResolvedProfile(profile_id=override, level="match")


def test_apply_match_override_none_returns_group_result_unchanged() -> None:
    group_winner = uuid.uuid4()
    group_result = ResolvedProfile(profile_id=group_winner, level="workspace")

    result = apply_match_override(group_result, None, visible_ids={group_winner})

    assert result is group_result


def test_apply_match_override_dangling_falls_back_to_group_result() -> None:
    group_winner = uuid.uuid4()
    dangling_override = uuid.uuid4()
    group_result = ResolvedProfile(profile_id=group_winner, level="global")

    # dangling_override deliberately not in visible_ids.
    result = apply_match_override(group_result, dangling_override, visible_ids={group_winner})

    assert result is group_result


def test_apply_match_override_beats_none_resolved_group_result() -> None:
    override = uuid.uuid4()

    result = apply_match_override(NONE_RESOLVED, override, visible_ids={override})

    assert result == ResolvedProfile(profile_id=override, level="match")


def test_apply_match_override_none_and_none_resolved_group_stays_none_resolved() -> None:
    result = apply_match_override(NONE_RESOLVED, None, visible_ids=set())

    assert result is NONE_RESOLVED

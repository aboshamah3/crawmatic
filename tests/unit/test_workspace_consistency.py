"""Unit tests for `app_shared.catalog.consistency` (T022, FR-009).

Pure, DB-independent — exercises `assert_refs_in_workspace`/
`exactly_one_of` against plain ids/maps only.
"""

from __future__ import annotations

import uuid

import pytest

from app_shared.catalog.consistency import (
    CrossWorkspaceReference,
    ExactlyOneOfViolation,
    MissingReference,
    assert_refs_in_workspace,
    exactly_one_of,
)


# --- assert_refs_in_workspace ------------------------------------------------


def test_assert_refs_in_workspace_accepts_in_workspace_refs() -> None:
    ws = uuid.uuid4()
    ref_a, ref_b = uuid.uuid4(), uuid.uuid4()
    resolved = {ref_a: ws, ref_b: ws}

    # No exception -> accepted.
    assert_refs_in_workspace(ws, [ref_a, ref_b], resolved)


def test_assert_refs_in_workspace_rejects_cross_workspace_ref() -> None:
    ws = uuid.uuid4()
    other_ws = uuid.uuid4()
    ref = uuid.uuid4()
    resolved = {ref: other_ws}

    with pytest.raises(CrossWorkspaceReference) as exc_info:
        assert_refs_in_workspace(ws, [ref], resolved)

    assert exc_info.value.ref_id == ref
    assert exc_info.value.expected_workspace_id == ws
    assert exc_info.value.actual_workspace_id == other_ws


def test_assert_refs_in_workspace_rejects_nonexistent_ref() -> None:
    ws = uuid.uuid4()
    ref = uuid.uuid4()
    resolved: dict[uuid.UUID, uuid.UUID] = {}

    with pytest.raises(MissingReference) as exc_info:
        assert_refs_in_workspace(ws, [ref], resolved)

    assert exc_info.value.ref_id == ref


def test_assert_refs_in_workspace_no_refs_is_a_noop() -> None:
    ws = uuid.uuid4()
    assert_refs_in_workspace(ws, [], {})


def test_assert_refs_in_workspace_mixed_batch_raises_on_first_offender() -> None:
    ws = uuid.uuid4()
    good_ref = uuid.uuid4()
    missing_ref = uuid.uuid4()
    resolved = {good_ref: ws}

    with pytest.raises(MissingReference):
        assert_refs_in_workspace(ws, [good_ref, missing_ref], resolved)


# --- exactly_one_of -----------------------------------------------------------


def test_exactly_one_of_accepts_product_id_only() -> None:
    exactly_one_of(uuid.uuid4(), None)


def test_exactly_one_of_accepts_variant_id_only() -> None:
    exactly_one_of(None, uuid.uuid4())


def test_exactly_one_of_rejects_both_none() -> None:
    with pytest.raises(ExactlyOneOfViolation):
        exactly_one_of(None, None)


def test_exactly_one_of_rejects_both_set() -> None:
    with pytest.raises(ExactlyOneOfViolation):
        exactly_one_of(uuid.uuid4(), uuid.uuid4())


# --- match reference consistency (SPEC-05 US4 T029, FR-006/SC-007) ----------
#
# `assert_refs_in_workspace` is entity-agnostic (plain ids/maps), so these
# cases exercise the same helper against match-shaped refs
# (competitor_id/product_variant_id/product_id) rather than a new module
# (`contracts/workspace-consistency.md`, research D7). `current_price_id`
# is a **soft** reference (no FK, no consistency check) — asserted by its
# absence from any call to the helper in `routers/matches.py`.


def test_match_competitor_ref_accepted_when_in_workspace() -> None:
    ws = uuid.uuid4()
    competitor_id = uuid.uuid4()
    resolved = {competitor_id: ws}

    # No exception -> the competitor ref resolves in the caller's workspace.
    assert_refs_in_workspace(ws, [competitor_id], resolved)


def test_match_competitor_ref_rejected_cross_workspace() -> None:
    ws = uuid.uuid4()
    other_ws = uuid.uuid4()
    competitor_id = uuid.uuid4()
    resolved = {competitor_id: other_ws}

    with pytest.raises(CrossWorkspaceReference) as exc_info:
        assert_refs_in_workspace(ws, [competitor_id], resolved)

    assert exc_info.value.ref_id == competitor_id
    assert exc_info.value.expected_workspace_id == ws
    assert exc_info.value.actual_workspace_id == other_ws


def test_match_competitor_ref_rejected_nonexistent() -> None:
    ws = uuid.uuid4()
    competitor_id = uuid.uuid4()

    with pytest.raises(MissingReference) as exc_info:
        assert_refs_in_workspace(ws, [competitor_id], {})

    assert exc_info.value.ref_id == competitor_id


def test_match_variant_ref_accepted_when_in_workspace() -> None:
    ws = uuid.uuid4()
    variant_id = uuid.uuid4()
    resolved = {variant_id: ws}

    assert_refs_in_workspace(ws, [variant_id], resolved)


def test_match_variant_ref_rejected_cross_workspace() -> None:
    ws = uuid.uuid4()
    other_ws = uuid.uuid4()
    variant_id = uuid.uuid4()
    resolved = {variant_id: other_ws}

    with pytest.raises(CrossWorkspaceReference) as exc_info:
        assert_refs_in_workspace(ws, [variant_id], resolved)

    assert exc_info.value.ref_id == variant_id
    assert exc_info.value.actual_workspace_id == other_ws


def test_match_variant_ref_rejected_nonexistent() -> None:
    ws = uuid.uuid4()
    variant_id = uuid.uuid4()

    with pytest.raises(MissingReference) as exc_info:
        assert_refs_in_workspace(ws, [variant_id], {})

    assert exc_info.value.ref_id == variant_id


def test_match_product_ref_accepted_when_in_workspace() -> None:
    """`product_id` on a match is derived server-side from the resolved
    variant's parent (research D4), but the same workspace-local
    consistency shape applies if it were ever checked independently."""
    ws = uuid.uuid4()
    product_id = uuid.uuid4()
    resolved = {product_id: ws}

    assert_refs_in_workspace(ws, [product_id], resolved)


def test_match_product_ref_rejected_cross_workspace() -> None:
    ws = uuid.uuid4()
    other_ws = uuid.uuid4()
    product_id = uuid.uuid4()
    resolved = {product_id: other_ws}

    with pytest.raises(CrossWorkspaceReference):
        assert_refs_in_workspace(ws, [product_id], resolved)


def test_match_multiple_refs_mixed_batch_rejects_on_first_offender() -> None:
    """A match's competitor + variant refs checked together (as
    `routers/matches.py` does per-kind): one in-workspace, one
    cross-workspace -> rejected."""
    ws = uuid.uuid4()
    other_ws = uuid.uuid4()
    competitor_id = uuid.uuid4()
    variant_id = uuid.uuid4()
    resolved = {competitor_id: ws, variant_id: other_ws}

    # Competitor ref alone: accepted.
    assert_refs_in_workspace(ws, [competitor_id], resolved)

    # Variant ref alone: rejected (cross-workspace).
    with pytest.raises(CrossWorkspaceReference):
        assert_refs_in_workspace(ws, [variant_id], resolved)


def test_current_price_id_is_a_soft_reference_not_consistency_checked() -> None:
    """`current_price_id` (FR-006, research D4) carries no FK and is never
    passed through `assert_refs_in_workspace` — a match may store any
    `current_price_id` (including one from another workspace, or a
    nonexistent one) without tripping workspace-consistency, because
    `routers/matches.py` never calls the helper for it. This test
    documents the soft-reference contract rather than exercising a code
    path: an id that would fail the composite-FK-backed refs (competitor/
    variant) is not even a candidate for this check."""
    ws = uuid.uuid4()
    current_price_id = uuid.uuid4()

    # A soft reference is never resolved into a {id: workspace_id} map for
    # the consistency check, so calling the helper with an *empty* resolved
    # map for it is a no-op precisely because the ref list passed by the
    # router never includes current_price_id at all.
    assert_refs_in_workspace(ws, [], {current_price_id: uuid.uuid4()})

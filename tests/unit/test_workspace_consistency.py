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

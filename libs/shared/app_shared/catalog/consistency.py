"""Workspace-consistency pre-check for catalog references (`contracts/workspace-consistency.md`, FR-009).

Pure, framework-agnostic — Layer 2 ("application pre-check") of the
two-layer isolation model. Layer 1 is structural: every catalog
composite FK (`app_shared/models/catalog.py`) is workspace-local by
construction (`(workspace_id, ref_id) -> parent(workspace_id, id)`), so
a cross-workspace reference is impossible at the DB, not merely
app-checked. This module exists purely so the API can answer a clean
`422`/`404` ("referenced entity not in this workspace") *before* an
insert/update would otherwise surface a raw `IntegrityError` (500).

Operates on plain id sets/maps only — no DB, no SQLAlchemy. The caller
(a router) is responsible for the single scoped/`id IN (...)` lookup
that produces the `{id: workspace_id}` map passed in here.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping


class WorkspaceConsistencyError(Exception):
    """Base class for a workspace-consistency pre-check rejection."""


class MissingReference(WorkspaceConsistencyError):
    """A referenced id does not resolve to any known row."""

    def __init__(self, ref_id: uuid.UUID) -> None:
        self.ref_id = ref_id
        super().__init__(f"referenced id {ref_id} does not exist")


class CrossWorkspaceReference(WorkspaceConsistencyError):
    """A referenced id resolves to a row in a *different* workspace."""

    def __init__(
        self,
        ref_id: uuid.UUID,
        expected_workspace_id: uuid.UUID,
        actual_workspace_id: uuid.UUID,
    ) -> None:
        self.ref_id = ref_id
        self.expected_workspace_id = expected_workspace_id
        self.actual_workspace_id = actual_workspace_id
        super().__init__(
            f"referenced id {ref_id} belongs to workspace {actual_workspace_id}, "
            f"not the caller's workspace {expected_workspace_id}"
        )


class ExactlyOneOfViolation(WorkspaceConsistencyError):
    """A group item set zero or both of its member references."""

    def __init__(self, product_id: object, product_variant_id: object) -> None:
        self.product_id = product_id
        self.product_variant_id = product_variant_id
        super().__init__(
            "a group item must reference exactly one of product_id/"
            f"product_variant_id (got product_id={product_id!r}, "
            f"product_variant_id={product_variant_id!r})"
        )


def assert_refs_in_workspace(
    workspace_id: uuid.UUID,
    ref_ids: Iterable[uuid.UUID],
    resolved: Mapping[uuid.UUID, uuid.UUID],
) -> None:
    """Assert every id in ``ref_ids`` resolves (via ``resolved``) to ``workspace_id``.

    ``resolved`` is the caller-loaded ``{id: workspace_id}`` map — built
    from exactly one scoped/id-in(...) lookup (see
    `contracts/workspace-consistency.md` Layer 2), never a per-id query.
    Raises `MissingReference` for an id absent from ``resolved``, and
    `CrossWorkspaceReference` for an id present but mapped to a
    different workspace. Raises on the *first* offending id — callers
    that want every offender reported should call this per-id inside
    their own loop.
    """
    for ref_id in ref_ids:
        if ref_id not in resolved:
            raise MissingReference(ref_id)
        actual_workspace_id = resolved[ref_id]
        if actual_workspace_id != workspace_id:
            raise CrossWorkspaceReference(ref_id, workspace_id, actual_workspace_id)


def exactly_one_of(
    product_id: uuid.UUID | None, product_variant_id: uuid.UUID | None
) -> None:
    """A group item must set exactly one of ``product_id``/``product_variant_id``.

    Both-null and both-set are rejected (`ExactlyOneOfViolation`); the
    DB allows either via nullable `MATCH SIMPLE` composite FKs, so this
    is purely an application-layer rule.
    """
    if (product_id is None) == (product_variant_id is None):
        raise ExactlyOneOfViolation(product_id, product_variant_id)

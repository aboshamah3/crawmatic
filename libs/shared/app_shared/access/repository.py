"""Dual-scope query helpers (`contracts/access-repository.md`, SPEC-10 D2).

The sanctioned query path for the two **dual-scope** tables
(``ProxyProvider``, ``AccessPolicy``) — deliberately **not** in
``app_shared.repository.WORKSPACE_OWNED_MODELS`` (its ``scoped_select``
would hide global rows). SQLAlchemy-only, framework-agnostic (no
FastAPI). Mirrors ``app_shared/profiles/repository.py`` (SPEC-06)
exactly.

``DomainAccessRule`` needs no dedicated repo here — it is tenant-only,
queried through the standard ``app_shared.repository.scoped_select``/
``scoped_get``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

from app_shared.catalog.consistency import CrossWorkspaceReference, MissingReference
from app_shared.models.access import AccessPolicy, ProxyProvider

# --- ProxyProvider -----------------------------------------------------


def visible_providers_select(workspace_id: uuid.UUID | str) -> Select[tuple[ProxyProvider]]:
    """Own (``workspace_id == ws``) OR global (``workspace_id IS NULL``), read-only.

    Used for list/get and for building the resolution ``visible_ids``
    set — a workspace sees its own providers plus every global provider.
    """
    return select(ProxyProvider).where(
        or_(ProxyProvider.workspace_id == workspace_id, ProxyProvider.workspace_id.is_(None))
    )


def owned_provider_select(workspace_id: uuid.UUID | str) -> Select[tuple[ProxyProvider]]:
    """Own-only (``workspace_id == ws``), never global — the manage (write) path.

    A global (``NULL``) or other-workspace id is simply absent from this
    query's results, so create/update/delete callers see "not found"
    through the tenant path (FR-006 read-only globals).
    """
    return select(ProxyProvider).where(ProxyProvider.workspace_id == workspace_id)


def owned_provider_get(
    session: Session, id_: uuid.UUID | str, workspace_id: uuid.UUID | str
) -> ProxyProvider | None:
    """Fetch a single row by BOTH ``id`` and own ``workspace_id`` — never a global row."""
    stmt = owned_provider_select(workspace_id).where(ProxyProvider.id == id_)
    return session.execute(stmt).scalar_one_or_none()


def _provider_visibility_map(
    session: Session, workspace_id: uuid.UUID | str, ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID | None]:
    """One ``visible_providers_select`` ``IN (...)`` lookup -> ``{id: workspace_id-or-None}``.

    Bounded (a single query regardless of ``len(ids)``).
    """
    id_list = list(ids)
    if not id_list:
        return {}
    stmt = visible_providers_select(workspace_id).where(ProxyProvider.id.in_(id_list))
    rows = session.execute(stmt).scalars().all()
    return {row.id: row.workspace_id for row in rows}


def assert_provider_assignable(
    session: Session, workspace_id: uuid.UUID | str, provider_id: uuid.UUID | str | None
) -> None:
    """Assignment-time visibility check for ``AccessPolicy.provider_id``.

    ``provider_id is None`` -> OK (clearing an assignment is always
    allowed). Otherwise resolves ``provider_id`` via exactly one
    `_provider_visibility_map` lookup (own OR global visible):

    - visible (own-workspace, or global i.e. ``workspace_id IS NULL``) -> OK.
    - absent from the map (dangling) -> raises `MissingReference`.
    - present but mapped to a *different*, non-``None`` workspace -> raises
      `CrossWorkspaceReference`.
    """
    if provider_id is None:
        return

    visibility = _provider_visibility_map(session, workspace_id, [provider_id])
    if provider_id not in visibility:
        raise MissingReference(provider_id)

    actual_workspace_id = visibility[provider_id]
    if actual_workspace_id is not None and actual_workspace_id != workspace_id:
        raise CrossWorkspaceReference(provider_id, workspace_id, actual_workspace_id)


# --- AccessPolicy — identical shape ------------------------------------


def visible_policies_select(workspace_id: uuid.UUID | str) -> Select[tuple[AccessPolicy]]:
    """Own (``workspace_id == ws``) OR global (``workspace_id IS NULL``), read-only."""
    return select(AccessPolicy).where(
        or_(AccessPolicy.workspace_id == workspace_id, AccessPolicy.workspace_id.is_(None))
    )


def owned_policy_select(workspace_id: uuid.UUID | str) -> Select[tuple[AccessPolicy]]:
    """Own-only (``workspace_id == ws``), never global — the manage (write) path."""
    return select(AccessPolicy).where(AccessPolicy.workspace_id == workspace_id)


def owned_policy_get(
    session: Session, id_: uuid.UUID | str, workspace_id: uuid.UUID | str
) -> AccessPolicy | None:
    """Fetch a single row by BOTH ``id`` and own ``workspace_id`` — never a global row."""
    stmt = owned_policy_select(workspace_id).where(AccessPolicy.id == id_)
    return session.execute(stmt).scalar_one_or_none()


def _policy_visibility_map(
    session: Session, workspace_id: uuid.UUID | str, ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID | None]:
    """One ``visible_policies_select`` ``IN (...)`` lookup -> ``{id: workspace_id-or-None}``."""
    id_list = list(ids)
    if not id_list:
        return {}
    stmt = visible_policies_select(workspace_id).where(AccessPolicy.id.in_(id_list))
    rows = session.execute(stmt).scalars().all()
    return {row.id: row.workspace_id for row in rows}


def assert_policy_assignable(
    session: Session, workspace_id: uuid.UUID | str, policy_id: uuid.UUID | str | None
) -> None:
    """Assignment-time visibility check for ``DomainAccessRule.access_policy_id``
    (and any other caller assigning an ``AccessPolicy``).

    ``policy_id is None`` -> OK (clearing an assignment is always
    allowed). Otherwise: visible (own or global) -> OK; dangling ->
    `MissingReference`; cross-workspace -> `CrossWorkspaceReference`.
    """
    if policy_id is None:
        return

    visibility = _policy_visibility_map(session, workspace_id, [policy_id])
    if policy_id not in visibility:
        raise MissingReference(policy_id)

    actual_workspace_id = visibility[policy_id]
    if actual_workspace_id is not None and actual_workspace_id != workspace_id:
        raise CrossWorkspaceReference(policy_id, workspace_id, actual_workspace_id)

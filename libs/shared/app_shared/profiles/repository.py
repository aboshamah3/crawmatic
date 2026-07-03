"""Dual-scope query helpers for `scrape_profiles` (`contracts/profiles-repository.md`, SPEC-06 US1/US2).

The single sanctioned query path for `ScrapeProfile` — it is deliberately
**not** in `app_shared.repository.WORKSPACE_OWNED_MODELS` (its
`scoped_select`/`scoped_get` constrain to `workspace_id = ctx`, which
would hide the global (`workspace_id IS NULL`) rows that reads and
resolution must see, FR-013/FR-021). SQLAlchemy-only, framework-agnostic
(no FastAPI).

`assert_profile_assignable` (assignment-time cross-workspace/dangling
check, FR-013/FR-017, SPEC-06 US2 T030) is called wherever a
`scrape_profile_id`/`default_scrape_profile_id` is set — see
`contracts/assignment-enforcement.md`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

from app_shared.catalog.consistency import CrossWorkspaceReference, MissingReference
from app_shared.models.scrape_profiles import ScrapeProfile

# The reserved name of the terminal global default (research D6).
GLOBAL_DEFAULT_PROFILE_NAME = "global_default"


def visible_profiles_select(workspace_id: uuid.UUID | str) -> Select[tuple[ScrapeProfile]]:
    """Own (``workspace_id == ws``) OR global (``workspace_id IS NULL``), read-only.

    Used for list/get and for building the resolution ``visible_ids``
    set — a workspace sees its own rows plus every global row (FR-013
    read side).
    """
    return select(ScrapeProfile).where(
        or_(ScrapeProfile.workspace_id == workspace_id, ScrapeProfile.workspace_id.is_(None))
    )


def owned_profile_select(workspace_id: uuid.UUID | str) -> Select[tuple[ScrapeProfile]]:
    """Own-only (``workspace_id == ws``), never global — the manage (write) path.

    A global (``NULL``) or other-workspace id is simply absent from this
    query's results, so create/update/delete callers see "not found"
    through the tenant path (FR-021).
    """
    return select(ScrapeProfile).where(ScrapeProfile.workspace_id == workspace_id)


def owned_profile_get(
    session: Session, id_: uuid.UUID | str, workspace_id: uuid.UUID | str
) -> ScrapeProfile | None:
    """Fetch a single row by BOTH ``id`` and own ``workspace_id`` — never a global row."""
    stmt = owned_profile_select(workspace_id).where(ScrapeProfile.id == id_)
    return session.execute(stmt).scalar_one_or_none()


def profile_visibility_map(
    session: Session, workspace_id: uuid.UUID | str, ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID | None]:
    """One `visible_profiles_select` ``IN (...)`` lookup -> ``{id: workspace_id-or-None}``.

    Bounded (a single query regardless of ``len(ids)``) — the caller
    (`assert_profile_assignable` or a batch resolution loader) never
    issues a per-id query.
    """
    id_list = list(ids)
    if not id_list:
        return {}
    stmt = visible_profiles_select(workspace_id).where(ScrapeProfile.id.in_(id_list))
    rows = session.execute(stmt).scalars().all()
    return {row.id: row.workspace_id for row in rows}


def assert_profile_assignable(
    session: Session, workspace_id: uuid.UUID | str, profile_id: uuid.UUID | str | None
) -> None:
    """Assignment-time visibility check (FR-013/FR-017, `contracts/assignment-enforcement.md`).

    ``profile_id is None`` -> OK (clearing an assignment is always
    allowed). Otherwise resolves ``profile_id`` via exactly one
    `profile_visibility_map` lookup (own OR global visible):

    - visible (own-workspace, or global i.e. ``workspace_id IS NULL``) -> OK.
    - absent from the map (dangling) -> raises `MissingReference`.
    - present but mapped to a *different*, non-``None`` workspace -> raises
      `CrossWorkspaceReference`.

    Raises the shared `app_shared.catalog.consistency` exceptions so
    callers (competitors/matches/scrape-profiles routers) map them to
    `404 NOT_FOUND` / `422 WORKSPACE_MISMATCH` exactly like the SPEC-05
    reference checks — no new error vocabulary here.
    """
    if profile_id is None:
        return

    visibility = profile_visibility_map(session, workspace_id, [profile_id])
    if profile_id not in visibility:
        raise MissingReference(profile_id)

    actual_workspace_id = visibility[profile_id]
    if actual_workspace_id is not None and actual_workspace_id != workspace_id:
        raise CrossWorkspaceReference(profile_id, workspace_id, actual_workspace_id)

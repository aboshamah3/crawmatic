"""Dual-scope query helpers for `scrape_profiles` (`contracts/profiles-repository.md`, SPEC-06 US1).

The single sanctioned query path for `ScrapeProfile` — it is deliberately
**not** in `app_shared.repository.WORKSPACE_OWNED_MODELS` (its
`scoped_select`/`scoped_get` constrain to `workspace_id = ctx`, which
would hide the global (`workspace_id IS NULL`) rows that reads and
resolution must see, FR-013/FR-021). SQLAlchemy-only, framework-agnostic
(no FastAPI).

`assert_profile_assignable` (assignment-time cross-workspace/dangling
check, FR-013) lands in Phase 4 (T030) — this module currently only
covers the T018 read/manage helpers.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

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

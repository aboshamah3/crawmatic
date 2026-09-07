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
from datetime import datetime, timezone

from sqlalchemy import Select, case, or_, select, update
from sqlalchemy.orm import Session

from app_shared.catalog.consistency import CrossWorkspaceReference, MissingReference
from app_shared.config import get_settings
from app_shared.models.scrape_profiles import ScrapeProfile, ScrapeProfileRevision

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


def visible_profile_revisions_select(
    workspace_id: uuid.UUID | str, profile_id: uuid.UUID | str
) -> Select[tuple[ScrapeProfileRevision]]:
    """Return one visible profile's immutable history, newest first."""
    return (
        select(ScrapeProfileRevision)
        .where(
            ScrapeProfileRevision.scrape_profile_id == profile_id,
            or_(
                ScrapeProfileRevision.workspace_id == workspace_id,
                ScrapeProfileRevision.workspace_id.is_(None),
            ),
        )
        .order_by(ScrapeProfileRevision.version.desc())
    )


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


# --- regex execution quarantine (A2/F02) --------------------------------------


def record_regex_timeout(
    session: Session,
    workspace_id: uuid.UUID | str,
    profile_id: uuid.UUID | str,
    *,
    threshold: int | None = None,
    now: datetime | None = None,
) -> bool:
    """Count one ``REGEX_TIMEOUT`` against a profile; quarantine at the threshold.

    Returns ``True`` iff this call is the one that stamped
    ``regex_quarantined_at`` (so the caller can log/alert exactly once per
    quarantine, not once per timeout).

    The UPDATE is **own-workspace-scoped** (``workspace_id = :ws``), the same
    scope as `owned_profile_select` — the sanctioned write path for this
    table. A global (``workspace_id IS NULL``) profile is therefore never
    auto-quarantined by one tenant's scrape: a shared row's behaviour must
    not be changeable from inside a single workspace. Operators quarantine
    and release global rows through the cross-workspace admin surface.

    ``threshold`` defaults to ``EXTRACTION_REGEX_QUARANTINE_AFTER``. The
    increment and the conditional stamp are one statement, so two concurrent
    scrapers cannot both read ``count = threshold - 1`` and both decide they
    are not the one to quarantine.
    """
    if threshold is None:
        try:
            threshold = int(get_settings().EXTRACTION_REGEX_QUARANTINE_AFTER)
        except Exception:  # noqa: BLE001 - shipped default; see Settings
            threshold = 3
    stamp = now or datetime.now(timezone.utc)

    next_count = ScrapeProfile.regex_timeout_count + 1
    stmt = (
        update(ScrapeProfile)
        .where(
            ScrapeProfile.id == profile_id,
            ScrapeProfile.workspace_id == workspace_id,
        )
        .values(
            regex_timeout_count=next_count,
            regex_quarantined_at=case(
                (
                    ScrapeProfile.regex_quarantined_at.is_(None) & (next_count >= threshold),
                    stamp,
                ),
                else_=ScrapeProfile.regex_quarantined_at,
            ),
        )
        .returning(ScrapeProfile.regex_timeout_count, ScrapeProfile.regex_quarantined_at)
        .execution_options(synchronize_session=False)
    )
    row = session.execute(stmt).first()
    if row is None:
        # Not ours (global, foreign, or deleted) — nothing counted, and
        # deliberately not an error: a scrape must never fail because its
        # bookkeeping had nowhere to go.
        return False
    count, quarantined_at = row
    return quarantined_at is not None and count == threshold


def clear_regex_quarantine(
    session: Session,
    profile_id: uuid.UUID | str,
    *,
    workspace_id: uuid.UUID | str | None = None,
) -> bool:
    """Release a profile's regex quarantine and reset its timeout counter.

    Returns ``True`` if a row was updated. ``workspace_id=None`` is the
    **cross-workspace operator** path (the service-token admin route, which
    must be able to release a global ``workspace_id IS NULL`` row); passing a
    workspace id restricts the release to that workspace's own rows.
    """
    stmt = update(ScrapeProfile).where(ScrapeProfile.id == profile_id)
    if workspace_id is not None:
        stmt = stmt.where(ScrapeProfile.workspace_id == workspace_id)  # noqa: workspace-scope
    result = session.execute(
        stmt.values(regex_timeout_count=0, regex_quarantined_at=None).execution_options(
            synchronize_session=False
        )
    )
    return bool(result.rowcount)

"""Workspace-scoped query helpers for the domain strategy optimizer (SPEC-12).

The sanctioned query path for :class:`~app_shared.models.strategy.DomainStrategyProfile`
and :class:`~app_shared.models.strategy.StrategyDiscoveryRun` (both registered in
``app_shared.repository.WORKSPACE_OWNED_MODELS`` — use ``scoped_select``/``scoped_get``
directly for anything not covered here) plus the **only** sanctioned way to read
:class:`~app_shared.models.strategy.StrategyAttemptStats`, which carries no
``workspace_id`` column of its own (research D3, FR-026): every stats read joins
through its scoped parent profile so an un-scoped stats query is structurally
impossible from this module. SQLAlchemy-only, framework-agnostic (no FastAPI),
the sibling of ``app_shared/access/repository.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app_shared.models.strategy import (
    DomainStrategyProfile,
    StrategyAttemptStats,
    StrategyDiscoveryRun,
)
from app_shared.repository import scoped_get, scoped_select


def resolve_profile(
    session: Session,
    workspace_id: uuid.UUID | str,
    competitor_id: uuid.UUID | str,
    domain: str,
    url_pattern: str,
) -> DomainStrategyProfile | None:
    """Fetch the profile for the unique ``(workspace, competitor, domain, url_pattern)``
    key, or ``None`` if none exists yet (the get-or-create seam, D5/D6)."""
    stmt = scoped_select(DomainStrategyProfile, workspace_id).where(
        DomainStrategyProfile.competitor_id == competitor_id,
        DomainStrategyProfile.domain == domain,
        DomainStrategyProfile.url_pattern == url_pattern,
    )
    return session.execute(stmt).scalar_one_or_none()


def get_profile(
    session: Session, profile_id: uuid.UUID | str, workspace_id: uuid.UUID | str
) -> DomainStrategyProfile | None:
    """Fetch a single profile by BOTH ``id`` and ``workspace_id`` (never a bare ``session.get``)."""
    return scoped_get(session, DomainStrategyProfile, profile_id, workspace_id)


def list_profiles_select(
    workspace_id: uuid.UUID | str,
    *,
    competitor_id: uuid.UUID | str | None = None,
    domain: str | None = None,
    status: str | None = None,
) -> Select[tuple[DomainStrategyProfile]]:
    """Cursor-list base query: workspace-scoped, optionally filtered (operator API, T039)."""
    stmt = scoped_select(DomainStrategyProfile, workspace_id)
    if competitor_id is not None:
        stmt = stmt.where(DomainStrategyProfile.competitor_id == competitor_id)
    if domain is not None:
        stmt = stmt.where(DomainStrategyProfile.domain == domain)
    if status is not None:
        stmt = stmt.where(DomainStrategyProfile.status == status)
    return stmt


def get_discovery_run(
    session: Session, run_id: uuid.UUID | str, workspace_id: uuid.UUID | str
) -> StrategyDiscoveryRun | None:
    """Fetch a single discovery run by BOTH ``id`` and ``workspace_id`` — 404 cross-workspace."""
    return scoped_get(session, StrategyDiscoveryRun, run_id, workspace_id)


def list_discovery_runs_select(
    workspace_id: uuid.UUID | str,
) -> Select[tuple[StrategyDiscoveryRun]]:
    """Cursor-list base query for discovery runs (operator API, T028/T039)."""
    return scoped_select(StrategyDiscoveryRun, workspace_id)


def stats_for_profile(
    session: Session, workspace_id: uuid.UUID | str, profile_id: uuid.UUID | str
) -> Sequence[StrategyAttemptStats]:
    """The **only** sanctioned read of ``strategy_attempt_stats`` (FR-026, D3).

    ``StrategyAttemptStats`` has no ``workspace_id`` column, so it can
    never appear in ``WORKSPACE_OWNED_MODELS``/``scoped_select`` — this
    helper enforces isolation by first resolving the parent profile
    through ``scoped_get`` (which *does* carry ``workspace_id`` and is
    registered there) and only then querying stats by that verified
    profile id. A profile absent/foreign to ``workspace_id`` yields an
    empty result, never another workspace's stats.
    """
    profile = get_profile(session, profile_id, workspace_id)
    if profile is None:
        return []
    stmt = select(StrategyAttemptStats).where(
        StrategyAttemptStats.domain_strategy_profile_id == profile.id
    )
    return session.execute(stmt).scalars().all()

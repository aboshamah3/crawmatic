"""Immutable scrape-profile revision snapshots."""

from __future__ import annotations

from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from app_shared.models.scrape_profiles import ScrapeProfile, ScrapeProfileRevision


PROFILE_CONFIG_COLUMNS: tuple[str, ...] = tuple(
    column.name
    for column in ScrapeProfile.__table__.columns
    if column.name not in {"id", "workspace_id", "version", "created_at", "updated_at"}
)


def profile_snapshot(profile: ScrapeProfile) -> dict[str, Any]:
    """Return a JSON-safe copy of every executable profile field."""
    snapshot: dict[str, Any] = {}
    for name in PROFILE_CONFIG_COLUMNS:
        value = getattr(profile, name)
        snapshot[name] = value.value if isinstance(value, Enum) else value
    return snapshot


def record_profile_revision(session: Session, profile: ScrapeProfile) -> ScrapeProfileRevision:
    """Append the profile's current revision inside the caller's transaction."""
    revision = ScrapeProfileRevision(
        workspace_id=profile.workspace_id,
        scrape_profile_id=profile.id,
        version=profile.version,
        snapshot=profile_snapshot(profile),
    )
    session.add(revision)
    return revision

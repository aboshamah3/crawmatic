"""API-key scope vocabulary (`contracts/security-scopes.md`, FR-013, §22).

Framework-agnostic. Scopes are string-backed (``StrEnum``, stored as
plain strings in ``api_keys.scopes`` JSONB) — never a Postgres-native
enum, consistent with ``app_shared.enums``.
"""

from __future__ import annotations

from collections.abc import Iterable

from app_shared.enums import StrEnum


class Scope(StrEnum):
    """The full API-key capability vocabulary (§22)."""

    PRODUCTS_READ = "products:read"
    PRODUCTS_WRITE = "products:write"
    VARIANTS_READ = "variants:read"
    VARIANTS_WRITE = "variants:write"
    COMPETITORS_READ = "competitors:read"
    COMPETITORS_WRITE = "competitors:write"
    MATCHES_READ = "matches:read"
    MATCHES_WRITE = "matches:write"
    JOBS_RUN = "jobs:run"
    JOBS_READ = "jobs:read"
    RESULTS_READ = "results:read"
    ALERTS_READ = "alerts:read"
    WEBHOOKS_READ = "webhooks:read"
    WEBHOOKS_WRITE = "webhooks:write"
    SCRAPE_PROFILES_READ = "scrape_profiles:read"
    SCRAPE_PROFILES_WRITE = "scrape_profiles:write"


def validate_scopes(values: Iterable[str]) -> list[str]:
    """Validate ``values`` against :class:`Scope`, returning them as plain strings.

    Raises ``ValueError`` on the first unknown scope (API-key create →
    ``422``, per contracts/api-keys.md).
    """
    validated: list[str] = []
    for value in values:
        try:
            validated.append(str(Scope(value)))
        except ValueError as exc:
            valid = ", ".join(member.value for member in Scope)
            raise ValueError(
                f"{value!r} is not a valid scope (expected one of: {valid})"
            ) from exc
    return validated


def has_scopes(granted: Iterable[str], required: Iterable[str]) -> bool:
    """Return ``True`` iff every scope in ``required`` is present in ``granted``."""
    granted_set = set(granted)
    return all(scope in granted_set for scope in required)

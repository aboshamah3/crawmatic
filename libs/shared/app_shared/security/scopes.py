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
    JOBS_WRITE = "jobs:write"
    RESULTS_READ = "results:read"
    ALERTS_READ = "alerts:read"
    WEBHOOKS_READ = "webhooks:read"
    WEBHOOKS_WRITE = "webhooks:write"
    SCRAPE_PROFILES_READ = "scrape_profiles:read"
    SCRAPE_PROFILES_WRITE = "scrape_profiles:write"
    PROXY_PROVIDERS_READ = "proxy_providers:read"
    PROXY_PROVIDERS_WRITE = "proxy_providers:write"
    ACCESS_POLICIES_READ = "access_policies:read"
    ACCESS_POLICIES_WRITE = "access_policies:write"
    DOMAIN_RULES_READ = "domain_rules:read"
    DOMAIN_RULES_WRITE = "domain_rules:write"
    REFRESH_RULES_READ = "refresh_rules:read"
    REFRESH_RULES_WRITE = "refresh_rules:write"
    # SPEC-12's strategy operator API (`apps/api/app/routers/strategy.py`)
    # gates on these two, but they were never added to this vocabulary --
    # `validate_scopes` 422'd any key requesting them, so no API-key
    # principal could ever reach a strategy endpoint (found live
    # 2026-07-12; JWT principals carry no scopes claim by design, which
    # left the whole strategy surface unreachable by anyone).
    STRATEGY_READ = "strategy:read"
    STRATEGY_WRITE = "strategy:write"


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

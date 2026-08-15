"""Database-infrastructure helpers that are not ORM models or connectivity.

Currently holds :mod:`app_shared.db.rls_guard` — the deploy-time
assertion that the *ordinary* application connection is confined by
``FORCE ROW LEVEL SECURITY`` (audit C3).
"""

from __future__ import annotations

from app_shared.db.rls_guard import (
    RlsRoleViolation,
    OrdinaryRoleFacts,
    assert_ordinary_role_cannot_bypass_rls,
    enforce_rls_role_on_startup,
    inspect_ordinary_role,
    production_check,
    register_production_check,
    reset_rls_guard_cache,
    rls_assertion_enabled,
)

__all__ = [
    "OrdinaryRoleFacts",
    "RlsRoleViolation",
    "assert_ordinary_role_cannot_bypass_rls",
    "enforce_rls_role_on_startup",
    "inspect_ordinary_role",
    "production_check",
    "register_production_check",
    "reset_rls_guard_cache",
    "rls_assertion_enabled",
]

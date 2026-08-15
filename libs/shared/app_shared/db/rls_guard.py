"""Deploy-time assertion: the ordinary app connection cannot bypass RLS.

Audit ``CORE_PRODUCT_PRODUCTION_READINESS_AUDIT_2026-08-15.md`` §C3.

Workspace isolation in this system rests on ``FORCE ROW LEVEL SECURITY``
plus the per-transaction ``app.workspace_id`` GUC
(:func:`app_shared.database.set_workspace_context`). That backstop is
worth exactly nothing if the PostgreSQL *role* behind ``DATABASE_URL``
can step over it. Three role attributes do so:

``rolsuper``
    A superuser is implicitly ``BYPASSRLS``; policies never apply.
``rolbypassrls``
    Explicit bypass; policies never apply.
table ownership
    ``FORCE ROW LEVEL SECURITY`` *does* subject the owner to its own
    policies, so ownership is not an immediate read leak — but the owner
    can ``ALTER TABLE ... NO FORCE`` or ``DROP POLICY`` at will, and any
    table that ever loses ``FORCE`` silently exempts it. Ownership is
    therefore treated as a violation too: the ordinary role must be a
    plain grantee.

Roles are intentionally created outside Alembic (see
``scripts/rls_provision.sql``), which means nothing in the deployment
pipeline guarantees they are correct. This module is that guarantee.

Design constraints (audit §C3, §11):

* **Cheap** — one query, once per process, cached. Never per request.
* **Never breaks local dev or tests** — off unless the process declares
  itself production (see :func:`rls_assertion_enabled`).
* **Explicitly bypassable** — the auth/system connections legitimately
  *are* ``BYPASSRLS`` (:func:`app_shared.database.get_auth_session`,
  :func:`~app_shared.database.get_system_session`); this guard is only
  ever pointed at the ordinary engine, and a deployment that knowingly
  runs the ordinary connection on a bypassing role can set
  ``RLS_ALLOW_BYPASSRLS_ORDINARY_ROLE=1`` to downgrade the failure to a
  logged warning.

Environment knobs (read from ``os.environ`` directly, deliberately not
from :class:`app_shared.config.Settings` — this must work in a
half-configured process and must not widen the Settings contract):

``RLS_ROLE_ASSERTION``
    ``auto`` (default) / ``on`` / ``off``. ``auto`` enables the check
    only when the environment looks like production.
``APP_ENV`` / ``ENVIRONMENT`` / ``RAILWAY_ENVIRONMENT_NAME`` / ``RAILWAY_ENVIRONMENT``
    Production detection for ``auto`` (any one of them equal to
    ``production``/``prod``, case-insensitive).
``RLS_ALLOW_BYPASSRLS_ORDINARY_ROLE``
    ``1``/``true``/``yes`` — the clearly-named escape hatch. Warns
    instead of raising.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

logger = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}
_PRODUCTION_VALUES = {"production", "prod"}
_ENV_NAME_VARS = (
    "APP_ENV",
    "ENVIRONMENT",
    "RAILWAY_ENVIRONMENT_NAME",
    "RAILWAY_ENVIRONMENT",
)

#: The single round trip this guard is allowed to make.
#:
#: ``pg_has_role(current_user, c.relowner, 'USAGE')`` is used rather than
#: ``c.relowner = current_user::regrole`` so that ownership held
#: *indirectly*, through a granted role, still counts as ownership.
_ROLE_FACTS_SQL = text(
    """
    SELECT
        current_user::text                                   AS role_name,
        r.rolsuper                                           AS is_superuser,
        r.rolbypassrls                                       AS has_bypassrls,
        (
            SELECT count(*)
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relkind IN ('r', 'p')
              AND pg_has_role(current_user, c.relowner, 'USAGE')
        )                                                    AS owned_public_tables,
        (
            SELECT count(*)
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relkind IN ('r', 'p')
              AND c.relrowsecurity
              AND NOT c.relforcerowsecurity
        )                                                    AS rls_without_force
    FROM pg_roles r
    WHERE r.rolname = current_user
    """
)


class RlsRoleViolation(RuntimeError):
    """The ordinary application role is not confined by RLS."""


@dataclass(frozen=True)
class OrdinaryRoleFacts:
    """Live attributes of the role behind the ordinary connection."""

    role_name: str
    is_superuser: bool
    has_bypassrls: bool
    owned_public_tables: int
    rls_without_force: int

    @property
    def violations(self) -> tuple[str, ...]:
        """Human-readable reasons this role can bypass RLS (empty = clean)."""
        problems: list[str] = []
        if self.is_superuser:
            problems.append(
                f"role {self.role_name!r} is a SUPERUSER "
                "(superusers are implicitly BYPASSRLS; every workspace policy "
                "is inert for this connection)"
            )
        if self.has_bypassrls:
            problems.append(
                f"role {self.role_name!r} has BYPASSRLS "
                "(row-level policies are never applied to this connection)"
            )
        if self.owned_public_tables:
            problems.append(
                f"role {self.role_name!r} owns {self.owned_public_tables} table(s) in "
                "schema public (an owner can ALTER TABLE ... NO FORCE ROW LEVEL "
                "SECURITY or DROP POLICY, so isolation is discretionary rather "
                "than enforced)"
            )
        return tuple(problems)

    @property
    def is_confined(self) -> bool:
        """``True`` when RLS actually constrains this connection."""
        return not self.violations


# Per-process cache. ``None`` = not checked yet.
_checked_engines: set[int] = set()


def reset_rls_guard_cache() -> None:
    """Forget which engines were already checked (tests, ``dispose_engine``)."""
    _checked_engines.clear()


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip().lower()


def _is_production() -> bool:
    return any(_env(var) in _PRODUCTION_VALUES for var in _ENV_NAME_VARS)


def rls_assertion_enabled() -> bool:
    """Whether the guard should run in this process.

    ``RLS_ROLE_ASSERTION=on``/``off`` is absolute. The default,
    ``auto``, enables the guard only when one of the environment-name
    variables says production — so local development, unit tests, and
    the integration suite are untouched unless they opt in.
    """
    mode = _env("RLS_ROLE_ASSERTION") or "auto"
    if mode == "on":
        return True
    if mode == "off":
        return False
    if mode != "auto":
        logger.warning(
            "RLS_ROLE_ASSERTION=%r is not one of auto/on/off; treating it as 'auto'",
            mode,
        )
    return _is_production()


def bypass_allowed() -> bool:
    """Whether the clearly-named escape hatch is set."""
    return _env("RLS_ALLOW_BYPASSRLS_ORDINARY_ROLE") in _TRUTHY


def inspect_ordinary_role(bind: Engine | Connection) -> OrdinaryRoleFacts:
    """Read the live role attributes behind ``bind`` (one query, no cache)."""
    if isinstance(bind, Connection):
        row = bind.execute(_ROLE_FACTS_SQL).one()
    else:
        with bind.connect() as connection:
            row = connection.execute(_ROLE_FACTS_SQL).one()
    return OrdinaryRoleFacts(
        role_name=str(row.role_name),
        is_superuser=bool(row.is_superuser),
        has_bypassrls=bool(row.has_bypassrls),
        owned_public_tables=int(row.owned_public_tables),
        rls_without_force=int(row.rls_without_force),
    )


def assert_ordinary_role_cannot_bypass_rls(
    bind: Engine | Connection,
    *,
    force: bool = False,
) -> OrdinaryRoleFacts | None:
    """Refuse to proceed when the ordinary connection can bypass RLS.

    Returns the observed :class:`OrdinaryRoleFacts` when the check ran,
    or ``None`` when it was skipped (not enabled for this process, or
    already checked for this engine and ``force`` is false).

    Raises :class:`RlsRoleViolation` when the role is a superuser, has
    ``BYPASSRLS``, or owns tables in ``public`` — unless
    ``RLS_ALLOW_BYPASSRLS_ORDINARY_ROLE`` is set, in which case the same
    facts are logged at ``ERROR`` and startup continues.

    **Never point this at the auth or system engine** — those are
    ``BYPASSRLS`` by design (see :mod:`app_shared.database`).
    """
    if not force and not rls_assertion_enabled():
        return None

    key = id(bind)
    if not force and key in _checked_engines:
        return None

    facts = inspect_ordinary_role(bind)
    _checked_engines.add(key)

    if facts.rls_without_force:
        # Not fatal on its own (the role may still be a plain grantee),
        # but it means an owner-equivalent connection would see everything.
        logger.warning(
            "rls_guard: %d public table(s) have ENABLE but not FORCE ROW LEVEL "
            "SECURITY; their owner is exempt from workspace isolation",
            facts.rls_without_force,
        )

    if facts.is_confined:
        logger.info(
            "rls_guard: ordinary connection role %r is confined by RLS "
            "(non-superuser, no BYPASSRLS, owns no public tables)",
            facts.role_name,
        )
        return facts

    detail = "; ".join(facts.violations)
    message = (
        "Refusing to start: the ordinary DATABASE_URL connection can bypass "
        f"row-level security. {detail}. Workspace isolation is NOT enforced "
        "for this connection. Point DATABASE_URL at the non-owner, "
        "non-superuser, NOBYPASSRLS application role (provision it with "
        "scripts/rls_provision.sql, verify it with scripts/rls_verify.py). "
        "The auth/scheduler connections that legitimately need BYPASSRLS use "
        "AUTH_DATABASE_URL / SYSTEM_DATABASE_URL and are not checked here. To "
        "knowingly run without this protection set "
        "RLS_ALLOW_BYPASSRLS_ORDINARY_ROLE=1."
    )

    if bypass_allowed():
        logger.error("rls_guard: %s (continuing: escape hatch set)", message)
        return facts

    raise RlsRoleViolation(message)


def enforce_rls_role_on_startup(bind: Engine | Connection | None = None) -> OrdinaryRoleFacts | None:
    """Named startup hook — safe to call unconditionally from any service.

    This is the extension point for the deploy-time configuration
    validator: call it once during API/worker/scheduler startup. With no
    argument it resolves the ordinary engine from
    :mod:`app_shared.database` itself (imported lazily so this module
    stays importable without a configured environment).

    Import path::

        from app_shared.db.rls_guard import enforce_rls_role_on_startup
    """
    if not rls_assertion_enabled():
        return None
    if bind is None:
        from app_shared.database import get_engine

        bind = get_engine()
    return assert_ordinary_role_cannot_bypass_rls(bind)


def production_check() -> str | None:
    """Adapter for ``app_shared.config_validation.EXTRA_PRODUCTION_CHECKS``.

    That module's aggregating startup validator wants zero-arg callables
    returning ``None`` (pass) or a failure message (``str``), so that one
    bad deploy reports *every* configuration problem at once instead of
    one per redeploy. This is the live-probe half of its §C3 extension
    point: it opens the ordinary connection and reads the real role
    attributes, which the static URL inspection in ``config_validation``
    deliberately does not do.

    Returns a message rather than raising, per that contract. A
    connection failure is reported as a finding too — an ordinary
    connection that cannot be opened at all is not a passing check.
    """
    try:
        facts = enforce_rls_role_on_startup()
    except RlsRoleViolation as exc:
        return str(exc)
    except Exception as exc:  # pragma: no cover - environment dependent
        return (
            "could not verify that the ordinary DATABASE_URL role is confined by "
            f"row-level security: {type(exc).__name__}: {exc}"
        )
    if facts is not None and not facts.is_confined and bypass_allowed():
        # Escape hatch is set: rls_guard already logged at ERROR. Surface
        # it here too so the aggregated startup report is honest about
        # running without the backstop.
        return (
            f"ordinary DATABASE_URL role {facts.role_name!r} can bypass RLS "
            "(" + "; ".join(facts.violations) + ") — permitted only because "
            "RLS_ALLOW_BYPASSRLS_ORDINARY_ROLE is set"
        )
    return None


def register_production_check() -> None:
    """Idempotently register :func:`production_check` with the validator.

    Safe to call more than once. Kept as an explicit call rather than an
    import-time side effect so importing this module never mutates
    another module's global state.
    """
    from app_shared.config_validation import EXTRA_PRODUCTION_CHECKS

    if production_check not in EXTRA_PRODUCTION_CHECKS:
        EXTRA_PRODUCTION_CHECKS.append(production_check)

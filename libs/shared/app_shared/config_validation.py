"""Production-mode startup config validation (audit §L1).

`CORE_PRODUCT_PRODUCTION_READINESS_AUDIT_2026-08-15.md` §L1 ("Local
deployment defaults are easy to misapply"): `.env.example` ships
`PGBOUNCER_AUTH_TYPE=trust`, default-like credentials, and the
PostgreSQL bootstrap identity (`crawmatic`, the DB *owner* — it BYPASSES
RLS) as the ordinary `DATABASE_URL` role. Those are legitimate,
documented local-only defaults; nothing here changes what a clean
checkout does. The audit's required fix is a startup assertion that
refuses to let the SAME defaults reach a *production* deploy.

Call :func:`assert_production_safe` once, early, at process startup —
before the app starts serving/consuming (`apps/api/app/main.py` does
this at import time; every other service should call it from its own
entrypoint, see the module-level TODO below). It is a **no-op** unless
:func:`is_production` is true, so it never affects local development or
CI's unit-suite job (docker-compose.yml carries no `ENVIRONMENT` var,
so a clean `cp .env.example .env` stays in "not production" mode).

Detection (:func:`is_production`): ``ENVIRONMENT`` (this repo's own
knob, case-insensitive ``production``/``prod``) OR Railway's
auto-injected ``RAILWAY_ENVIRONMENT_NAME`` reading production/prod —
checked directly against `os.environ`, not through
`app_shared.config.Settings` (deliberately: a Settings-construction
failure must never mask *this* assertion, and this module has to work
for every service, some of which extend `Settings` differently).

**Extension point for the C3 workstream** (RLS-bypass role probe — "is
the ordinary `DATABASE_URL` role BYPASSRLS/superuser/table-owner",
audit §C3): this module deliberately does NOT open a database
connection or run the live isolation probe — append a zero-arg callable
returning `None` (pass) or an error `str` (fail) to
:data:`EXTRA_PRODUCTION_CHECKS`, e.g.::

    from app_shared.config_validation import EXTRA_PRODUCTION_CHECKS

    def _assert_ordinary_role_cannot_bypass_rls() -> str | None:
        ...  # live probe against DATABASE_URL's role
        return None  # or a failure message

    EXTRA_PRODUCTION_CHECKS.append(_assert_ordinary_role_cannot_bypass_rls)

Every registered check runs on every :func:`assert_production_safe`
call; a check that returns a message contributes one more line to the
same aggregated :class:`ProductionConfigError`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from urllib.parse import urlsplit

from app_shared.config import Settings, get_settings

logger = logging.getLogger(__name__)

#: Extension point for other workstreams' production-only startup
#: checks (see module docstring — C3's RLS-bypass probe plugs in here).
#: Each callable takes no arguments and returns `None` (pass) or a
#: human-readable failure message (str).
EXTRA_PRODUCTION_CHECKS: list[Callable[[], str | None]] = []

#: The exact local-dev placeholder values `.env.example` ships. A
#: production deploy carrying any of these verbatim means someone
#: copied `.env.example` straight to a production environment without
#: generating real secrets.
_PLACEHOLDER_JWT_SECRET = "change-me-local-dev-secret-32-bytes-min"
_PLACEHOLDER_ENCRYPTION_KEY = "1:DDdqY9HwOBbYpfuS_6K-Z_fa75VD5fxAt0HNkdYP940="
_PLACEHOLDER_SCRAPYD_PASSWORD = "change-me"
_PLACEHOLDER_DB_PASSWORD = "crawmatic"

#: PostgreSQL bootstrap roles that are the database **owner** and
#: therefore BYPASS RLS by definition (table owners always bypass row
#: security) — production must use a distinct, least-privileged
#: application role for `DATABASE_URL`.
#:
#: `crawmatic` is `.env.example`'s `POSTGRES_USER`. `postgres` is the
#: managed-provider default and is what the live Railway deployment
#: actually connects as: checking only the compose-file name gave this
#: guard a clean bill of health against a deployment whose ordinary role
#: was a superuser *and* the owner of all 40 tables. A guard that cannot
#: see the real misconfiguration is worse than no guard, because it is
#: quoted as evidence — so match the role name set, not one example.
#:
#: This is a cheap string check on a URL. The authoritative test is the
#: live `rls_guard` probe wired into `EXTRA_PRODUCTION_CHECKS`, which
#: asks the database rather than guessing from a name.
_BOOTSTRAP_DB_ROLES = frozenset({"crawmatic", "postgres"})

#: Minimum acceptable length for a raw HMAC/Fernet-style secret,
#: mirroring PyJWT's own `InsecureKeyLengthWarning` threshold (32 bytes
#: for HS256).
_MIN_SECRET_LENGTH = 32


class ProductionConfigError(RuntimeError):
    """Raised by :func:`assert_production_safe` to refuse to start.

    Carries every violation found (not just the first) so a fail-closed
    boot failure is fixable in one pass, not a `deploy / crash / fix one
    thing / redeploy / crash again` loop.
    """


def is_production() -> bool:
    """True when this process should be treated as a production deploy.

    Checked directly against `os.environ` (never through `Settings`) —
    see module docstring for why. Recognizes this repo's own
    `ENVIRONMENT` var and Railway's auto-injected
    `RAILWAY_ENVIRONMENT_NAME`; either reading `production`/`prod`
    (case-insensitive) counts.
    """
    for var in ("ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME"):
        value = os.environ.get(var, "").strip().lower()
        if value in ("production", "prod"):
            return True
    return False


def _weak_secret_findings(settings: Settings) -> list[str]:
    findings: list[str] = []

    if settings.JWT_SECRET == _PLACEHOLDER_JWT_SECRET:
        findings.append(
            "JWT_SECRET is still the .env.example local-dev placeholder value."
        )
    elif len(settings.JWT_SECRET) < _MIN_SECRET_LENGTH:
        findings.append(
            f"JWT_SECRET is only {len(settings.JWT_SECRET)} bytes "
            f"(minimum {_MIN_SECRET_LENGTH} for HS256; generate with "
            "`openssl rand -hex 32`)."
        )

    if _PLACEHOLDER_ENCRYPTION_KEY in settings.ENCRYPTION_KEYS:
        findings.append(
            "ENCRYPTION_KEYS still contains the .env.example placeholder Fernet key."
        )

    if settings.SCRAPYD_PASSWORD == _PLACEHOLDER_SCRAPYD_PASSWORD:
        findings.append(
            "SCRAPYD_PASSWORD is still the .env.example placeholder value 'change-me'."
        )

    if settings.SAAS_SERVICE_TOKEN is not None:
        if not settings.SAAS_SERVICE_TOKEN.strip():
            findings.append(
                "SAAS_SERVICE_TOKEN is set but blank — either unset it entirely "
                "(disables /v1/admin, fail-closed) or set a real generated token."
            )
        elif len(settings.SAAS_SERVICE_TOKEN) < _MIN_SECRET_LENGTH:
            findings.append(
                f"SAAS_SERVICE_TOKEN is only {len(settings.SAAS_SERVICE_TOKEN)} bytes "
                f"(minimum {_MIN_SECRET_LENGTH}; generate with "
                '`python -c "import secrets; print(secrets.token_urlsafe(48))"`).'
            )

    return findings


def _pgbouncer_findings() -> list[str]:
    # PGBOUNCER_AUTH_TYPE is a compose-only var (edoburu/pgbouncer image
    # config, not read by any app service through Settings) — checked
    # directly against os.environ, same as `is_production`.
    auth_type = os.environ.get("PGBOUNCER_AUTH_TYPE", "").strip().lower()
    if auth_type == "trust":
        return [
            "PGBOUNCER_AUTH_TYPE=trust — production must use scram-sha-256 "
            "with a real userlist (trust auth accepts any password, "
            "including no password, for any listed user)."
        ]
    return []


def _ordinary_role_override_acknowledged() -> bool:
    """True when an operator has explicitly accepted an over-privileged
    ordinary role.

    Deliberately the *same* switch as :mod:`app_shared.db.rls_guard`'s
    ``RLS_ALLOW_BYPASSRLS_ORDINARY_ROLE``: both guards are asserting one
    fact — "the ordinary application role cannot bypass RLS" — one from
    the URL string and one from the live database. Two independent flags
    would let a deploy satisfy one and trip the other, which is how an
    operator learns to reach for whichever env var makes the boot loop
    stop. Read from the environment rather than importing ``rls_guard``,
    which imports this module to register its live probe.
    """
    return os.environ.get("RLS_ALLOW_BYPASSRLS_ORDINARY_ROLE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _database_role_findings(settings: Settings) -> list[str]:
    findings: list[str] = []
    parsed = urlsplit(settings.DATABASE_URL)
    role = parsed.username

    if not role:
        findings.append("DATABASE_URL has no explicit username — cannot verify its role.")
    elif role in _BOOTSTRAP_DB_ROLES and _ordinary_role_override_acknowledged():
        logger.error(
            "database_role_override_acknowledged role=%r "
            "reason=RLS_ALLOW_BYPASSRLS_ORDINARY_ROLE_set "
            "impact=tenant_isolation_backstop_inactive",
            role,
        )
    elif role in _BOOTSTRAP_DB_ROLES:
        findings.append(
            f"DATABASE_URL connects as {role!r}, a PostgreSQL "
            "bootstrap/owner role — table owners always bypass RLS. Production "
            "must set DATABASE_URL to a distinct, least-privileged application "
            "role (see the C3 workstream's role-provisioning + the "
            "EXTRA_PRODUCTION_CHECKS live BYPASSRLS probe hook)."
        )

    if parsed.password == _PLACEHOLDER_DB_PASSWORD:
        findings.append(
            "DATABASE_URL's password is still the .env.example placeholder value."
        )

    return findings


def assert_production_safe(settings: Settings | None = None) -> None:
    """Raise :class:`ProductionConfigError` if this looks like a
    production deploy running with local-dev-shaped configuration.

    No-op when :func:`is_production` is false. Call once, early, at
    process startup (see module docstring). `settings` is accepted for
    testability; defaults to :func:`app_shared.config.get_settings`.
    """
    if not is_production():
        return

    resolved = settings if settings is not None else get_settings()

    findings: list[str] = []
    findings.extend(_weak_secret_findings(resolved))
    findings.extend(_pgbouncer_findings())
    findings.extend(_database_role_findings(resolved))

    for check in EXTRA_PRODUCTION_CHECKS:
        result = check()
        if result:
            findings.append(result)

    if findings:
        bullet_list = "\n".join(f"  - {finding}" for finding in findings)
        raise ProductionConfigError(
            "Refusing to start in production mode with unsafe configuration:\n"
            f"{bullet_list}"
        )

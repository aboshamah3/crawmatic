"""Shared staging guard and CLI plumbing for the D2 fault-injection matrix
(EPA D2, audit §13 Durability / Tenant fairness / Host protection).

Every script in this package kills, pauses, or otherwise degrades a
service. That is exactly the class of action the worker contract's
destructive firewall forbids against anything but an explicit,
deliberately-named staging target — so this module is the ONE place the
guard is implemented, and every script imports it rather than rolling
its own check. A script with its own ad-hoc guard is a script that can
drift out of sync with this one; there is exactly one gate.

## The guard, precisely

``require_staging(args)`` refuses (raises :class:`StagingGuardRefusal`,
which ``main()`` in every script turns into a printed refusal and a
non-zero exit — never a warning, never a prompt, never a default-yes)
unless ALL of:

1. ``--staging-target`` was passed and, case-insensitively, equals the
   literal ``"staging"``. Nothing else is accepted — not a project name,
   not an environment id — so a copy-pasted production identifier can
   never satisfy the gate by accident.
2. Every connection string the script needs (``--database-url`` and/or
   ``--redis-url``, per script) was passed explicitly on the command
   line. None of these ever falls back to ``$DATABASE_URL``,
   ``app_shared.config.get_settings()``, or any other ambient
   environment value — an operator's shell inheriting a production
   credential must never be able to silently redirect a kill/pause
   action there.
3. ``RAILWAY_ENVIRONMENT_NAME`` (read, never printed) is not
   ``"production"``/``"prod"`` when set. Belt-and-braces for the case
   where this is invoked from inside a Railway shell that does carry
   that variable.

``--dry-run`` bypasses connection-string requirements (there is nothing
to connect to) but NOT requirement 1 — even the offline demonstration
path states its intent to target staging, so copy-pasting a real
invocation and stripping only ``--dry-run`` is safe by construction.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass

__all__ = [
    "STAGING_TARGET_LITERAL",
    "PRODUCTION_ENVIRONMENT_NAMES",
    "StagingGuardRefusal",
    "add_staging_arguments",
    "require_staging",
    "refuse_and_exit",
]

#: The one value ``--staging-target`` accepts (case-insensitive).
STAGING_TARGET_LITERAL = "staging"

#: ``RAILWAY_ENVIRONMENT_NAME`` values that refuse the guard outright.
PRODUCTION_ENVIRONMENT_NAMES = frozenset({"production", "prod"})


class StagingGuardRefusal(RuntimeError):
    """Raised by :func:`require_staging` for every refusal reason."""


def add_staging_arguments(
    parser: argparse.ArgumentParser,
    *,
    needs_database: bool = False,
    needs_redis: bool = False,
) -> None:
    """Register the arguments :func:`require_staging` checks.

    Called by every script's ``build_parser()`` so the ``--help`` text
    and the guard never drift apart.
    """
    parser.add_argument(
        "--staging-target",
        default=None,
        help=(
            "Must be exactly 'staging' (case-insensitive). The one thing "
            "this flag is for is stopping a copy-pasted invocation from "
            "running unattended against anything else."
        ),
    )
    if needs_database:
        parser.add_argument(
            "--database-url",
            default=None,
            help=(
                "Staging Postgres DSN. Required unless --dry-run. Never "
                "read from $DATABASE_URL or app settings — must be typed "
                "explicitly so an inherited production credential cannot "
                "silently redirect this script."
            ),
        )
    if needs_redis:
        parser.add_argument(
            "--redis-url",
            default=None,
            help="Staging Redis URL. Required unless --dry-run. Same rule as --database-url.",
        )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run the offline demonstration path: synthetic fixture data "
            "flows through the exact same measurement/printing code the "
            "live path uses, and nothing is connected to. Still requires "
            "--staging-target staging (see module docstring)."
        ),
    )


@dataclass(frozen=True)
class StagingArgs:
    staging_target: str | None
    database_url: str | None
    redis_url: str | None
    dry_run: bool


def require_staging(
    args: argparse.Namespace,
    *,
    needs_database: bool = False,
    needs_redis: bool = False,
) -> None:
    """Enforce the guard described in the module docstring.

    Raises :class:`StagingGuardRefusal` with a human-readable reason on
    any failure. Never returns a value — a bare return means "proceed".
    """
    target = (getattr(args, "staging_target", None) or "").strip().lower()
    if target != STAGING_TARGET_LITERAL:
        raise StagingGuardRefusal(
            "refusing: --staging-target must be exactly 'staging' "
            f"(got {getattr(args, 'staging_target', None)!r}). This script "
            "kills or pauses live services and will not run unattended "
            "against an unnamed or non-staging target."
        )

    railway_env = os.environ.get("RAILWAY_ENVIRONMENT_NAME", "").strip().lower()
    if railway_env in PRODUCTION_ENVIRONMENT_NAMES:
        raise StagingGuardRefusal(
            "refusing: RAILWAY_ENVIRONMENT_NAME="
            f"{railway_env!r} names a production environment."
        )

    dry_run = bool(getattr(args, "dry_run", False))
    if dry_run:
        return

    if needs_database and not getattr(args, "database_url", None):
        raise StagingGuardRefusal(
            "refusing: --database-url is required for a live run (or pass --dry-run)."
        )
    if needs_redis and not getattr(args, "redis_url", None):
        raise StagingGuardRefusal(
            "refusing: --redis-url is required for a live run (or pass --dry-run)."
        )


def refuse_and_exit(exc: StagingGuardRefusal) -> int:
    """Print ``exc`` to stderr and return the exit code ``main()`` should use."""
    print(f"REFUSED: {exc}", file=sys.stderr)
    return 2

#!/usr/bin/env python
"""domain_lifecycle.py — the operator CLI for the domain certification
lifecycle (EPA W4 gate-review follow-up F2, 2026-08-26).

``app_shared.domains.lifecycle.transition`` is the single writer of
``domain_playbooks.state`` and the only thing that appends to the
append-only ``domain_lifecycle_audit`` trail. Until this script it had
**no production caller** — the library was proven by unit tests and
consumed read-only (``get_domain_state``) by the dispatch path, but
nothing an operator could actually run moved a domain between states.
This is that caller, and it is deliberately the *thin* one:

* It never touches ``domain_playbooks.state``, ``profile_version``,
  ``last_canary_at`` or ``domain_lifecycle_audit`` itself. It calls
  ``transition()`` and lets every guard the library owns apply — the
  legal-edge graph, mandatory evidence, the capability-granting approval
  gate, and the one-audit-row-per-call rule. A guard that refuses is
  reported as a plain message and a non-zero exit, never worked around.
* It builds nothing an HTTP route would need to duplicate. A future
  ``POST /v1/domains/{domain}/transitions`` should call the same
  ``transition()`` with the same arguments; ``apps/api`` is owned by
  another task right now, so the route is a follow-up, not a blocker —
  the library call is identical either way.

Three subcommands::

    # every domain's certified state + profile version
    uv run python scripts/domain_lifecycle.py list

    # the append-only audit trail for one domain, oldest first
    uv run python scripts/domain_lifecycle.py history amazon.sa

    # move a domain, with mandatory evidence
    uv run python scripts/domain_lifecycle.py transition amazon.sa DEGRADED \\
        --evidence '{"reason": "failure signal spike", "window": "1h"}'

    # a capability-granting edge additionally needs a recorded human
    uv run python scripts/domain_lifecycle.py transition amazon.sa ACTIVE \\
        --evidence '{"recert": "5/5"}' --approver ops@crawmatic.com

``--evidence`` accepts a JSON object or free text (free text is stored as
``{"note": "..."}`` so the column's object shape is never violated).
Nothing is written until the transition succeeds; the commit is this
script's, one per invocation, and ``--dry-run`` rolls back instead so an
operator can see exactly what an edge would do first.

The database URL comes from the ordinary configuration
(``app_shared.database.get_system_sessionmaker`` ->
``Settings.SYSTEM_DATABASE_URL``/``DATABASE_URL``) — never a flag, never
printed. ``domain_playbooks`` is fleet-wide, operator-curated reference
data with no ``workspace_id`` at all (filed ``SYSTEM`` in
``scripts/rls_table_manifest.txt``), which is why the system sessionmaker
is the right connection here and no workspace context is set.

Only ``parse_args`` runs on a bare import: the database import lives
inside :func:`main`, so this module is safe to import (and its unit test
safe to collect) with zero environment variables set — the convention
``scripts/backfill_daily_rollups.py`` and ``scripts/seed_bootstrap.py``
established.

Exit codes: ``0`` success, ``1`` the lifecycle library refused (illegal
edge, missing evidence, missing approver, unknown domain), ``2`` bad
arguments.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable

from sqlalchemy import select

from app_shared.domains.lifecycle import LifecycleError, transition
from app_shared.models.domain_playbooks import DomainLifecycleAudit, DomainPlaybook, DomainState

_STATE_NAMES = [state.value for state in DomainState]


def parse_evidence(raw: str) -> dict:
    """``--evidence`` -> the JSONB object ``transition`` requires.

    A JSON **object** is used as-is. Anything else (free text, or a JSON
    scalar/array) is wrapped as ``{"note": <the raw string>}`` rather
    than rejected: an operator typing a sentence should not have to
    learn JSON to record why they quarantined a domain, and the column
    is declared as an object. Empty/whitespace-only input is left empty
    so ``transition``'s own mandatory-evidence guard is what refuses it
    — this function never invents evidence.
    """
    text = raw.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return {"note": text}
    if isinstance(parsed, dict):
        return parsed
    return {"note": text}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="domain_lifecycle.py",
        description=(
            "Inspect and move domain certification state through "
            "app_shared.domains.lifecycle.transition (the single writer)."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "list", help="show every domain's certified state and profile version"
    )

    history = subparsers.add_parser(
        "history", help="show the append-only audit trail for one domain"
    )
    history.add_argument("domain", help="bare domain, e.g. amazon.sa (no scheme, no www.)")
    history.add_argument(
        "--limit", type=int, default=50, help="most recent N rows (default 50)"
    )

    move = subparsers.add_parser(
        "transition", help="move one domain to a new certification state"
    )
    move.add_argument("domain", help="bare domain, e.g. amazon.sa (no scheme, no www.)")
    move.add_argument(
        "to_state",
        choices=_STATE_NAMES,
        help="destination state",
    )
    move.add_argument(
        "--evidence",
        required=True,
        help=(
            "why this transition is justified: a JSON object, or free text "
            "(stored as {\"note\": ...}). Mandatory for every transition."
        ),
    )
    move.add_argument(
        "--approver",
        default=None,
        help=(
            "identifier (email/handle) of the human approving this move. "
            "Required for any capability-granting edge -- the library "
            "decides which those are, from C3's authorization rule table."
        ),
    )
    move.add_argument(
        "--profile-owner",
        dest="profile_owner",
        default=None,
        help="record/replace the accountable owner for this domain's profile",
    )
    move.add_argument(
        "--dry-run",
        action="store_true",
        help="run every guard and report the outcome, then roll back instead of committing",
    )

    return parser.parse_args(argv)


# --------------------------------------------------------------------------
# Subcommands. Each takes an open session so the whole CLI is testable
# against a fake -- none of them opens a connection of its own.
# --------------------------------------------------------------------------


def cmd_list(session: Any) -> int:
    rows = (
        session.execute(select(DomainPlaybook).order_by(DomainPlaybook.domain))
        .scalars()
        .all()
    )
    if not rows:
        print("no domain_playbooks rows")
        return 0
    print(f"{'DOMAIN':40s} {'STATE':16s} {'VER':>4s}  {'OWNER':24s} LAST_CANARY_AT")
    for row in rows:
        state = row.state.value if isinstance(row.state, DomainState) else str(row.state)
        canary = row.last_canary_at.isoformat() if row.last_canary_at else "-"
        print(
            f"{row.domain:40s} {state:16s} {row.profile_version:>4d}  "
            f"{(row.profile_owner or '-'):24s} {canary}"
        )
    return 0


def cmd_history(session: Any, *, domain: str, limit: int) -> int:
    rows = (
        session.execute(
            select(DomainLifecycleAudit)
            .where(DomainLifecycleAudit.domain == domain)
            .order_by(DomainLifecycleAudit.created_at.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    if not rows:
        print(f"no domain_lifecycle_audit rows for domain={domain!r}")
        return 0
    # Oldest first reads as a story; the query orders newest-first only
    # so `--limit` means "the most recent N".
    for row in reversed(rows):
        when = row.created_at.isoformat() if row.created_at else "-"
        print(
            f"{when}  {row.from_state} -> {row.to_state}  v{row.profile_version}  "
            f"approver={row.approver or '-'}"
        )
        print(f"    evidence={json.dumps(row.evidence, sort_keys=True, default=str)}")
    return 0


def cmd_transition(
    session: Any,
    *,
    domain: str,
    to_state: str,
    evidence: dict,
    approver: str | None,
    profile_owner: str | None,
    dry_run: bool,
) -> int:
    """Call the library, report what it did, or report politely why it
    refused. Every guard belongs to ``transition``; this never
    second-guesses one or retries around it."""
    try:
        row = transition(
            session,
            domain,
            DomainState(to_state),
            evidence=evidence,
            approver=approver,
            profile_owner=profile_owner,
        )
    except LifecycleError as exc:
        session.rollback()
        print(f"refused: {exc}", file=sys.stderr)
        return 1

    if dry_run:
        session.rollback()
        print(
            f"DRY-RUN (rolled back): {domain} {row.from_state} -> {row.to_state} "
            f"would become profile_version={row.profile_version} "
            f"approver={row.approver or '-'}"
        )
        return 0

    session.commit()
    print(
        f"{domain}: {row.from_state} -> {row.to_state} "
        f"profile_version={row.profile_version} approver={row.approver or '-'}"
    )
    return 0


def main(
    argv: list[str] | None = None,
    *,
    session_factory: Callable[[], Any] | None = None,
) -> int:
    """Entry point. ``session_factory`` is injectable purely so the
    argument-to-library plumbing can be unit-tested against a fake
    session with no live database; production always resolves it from
    configuration (``get_system_sessionmaker``), never from a flag."""
    args = parse_args(argv)

    if session_factory is None:
        from app_shared.database import get_system_sessionmaker

        session_factory = get_system_sessionmaker()

    with session_factory() as session:
        if args.command == "list":
            return cmd_list(session)
        if args.command == "history":
            return cmd_history(session, domain=args.domain, limit=args.limit)
        if args.command == "transition":
            return cmd_transition(
                session,
                domain=args.domain,
                to_state=args.to_state,
                evidence=parse_evidence(args.evidence),
                approver=args.approver,
                profile_owner=args.profile_owner,
                dry_run=args.dry_run,
            )
    print(f"unknown command: {args.command!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

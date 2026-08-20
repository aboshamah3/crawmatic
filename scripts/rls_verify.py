#!/usr/bin/env python
"""Live proof that the ordinary application connection is confined by RLS.

Audit ``CORE_PRODUCT_PRODUCTION_READINESS_AUDIT_2026-08-15.md`` §C3:
"Prove the ordinary live DB role cannot bypass RLS; code structure alone
is insufficient."

This script is that proof. It connects as the ordinary application role,
sets workspace context exactly the way the application does
(``set_config('app.workspace_id', ..., true)`` — i.e. ``SET LOCAL``,
which is what :func:`app_shared.database.set_workspace_context` emits and
what is safe under PgBouncer transaction pooling), and then checks four
independent things:

A. **Role attributes.** The connected role must be non-superuser, must
   not hold ``BYPASSRLS``, and must own no tables in ``public``.
   (Reuses :mod:`app_shared.db.rls_guard`, the same check the services
   run at startup, so the script and the runtime can never disagree.)
B. **Forced RLS coverage.** Every workspace-scoped relation — anything
   in ``public`` carrying a ``workspace_id`` column, monthly partitions
   included — must have both ``relrowsecurity`` and
   ``relforcerowsecurity`` and at least one policy.
C. **Own context sees own rows.** With ``app.workspace_id`` set to a
   workspace that owns data, that data is visible — otherwise a passing
   isolation check would be meaningless (a broken connection also
   returns zero rows).
D. **Foreign context sees nothing, and no context sees nothing.** With
   ``app.workspace_id`` set to a *different* workspace, and again with
   no context at all, the probe tables must return **0** rows.

READ-ONLY by default. It issues only ``SELECT``/``set_config`` and takes
no locks. ``--txn-probe`` additionally inserts a throwaway second
workspace to make check D a real two-tenant comparison — that runs
inside a transaction which is **always rolled back**, including on
success, and is refused unless you pass the flag explicitly.

Usage::

    # against production, read-only, using the ordinary role's URL
    RLS_VERIFY_DATABASE_URL='postgresql+psycopg://crawmatic_app:...@host:6432/db' \
        uv run python scripts/rls_verify.py

    # falls back to DATABASE_URL when RLS_VERIFY_DATABASE_URL is unset
    uv run python scripts/rls_verify.py --verbose

Exit codes: ``0`` all checks passed, ``1`` at least one check failed,
``2`` could not run (no URL, connection refused, no data to probe).
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from dataclasses import dataclass, field

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

# Import the runtime guard so the script asserts exactly what the
# services assert. Requires the repo's workspaces to be installed
# (`uv run` handles this).
from app_shared.db.rls_guard import OrdinaryRoleFacts, inspect_ordinary_role

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CANNOT_RUN = 2

#: Tables to probe, most-interesting first. Filtered at runtime to those
#: that actually exist, carry a ``workspace_id`` column, have forced RLS,
#: and hold rows for the chosen workspace.
CANDIDATE_TABLES = (
    "products",
    "product_variants",
    "competitors",
    "competitor_product_matches",
    "match_current_prices",
    "api_keys",
    "users",
    "scrape_jobs",
    "webhook_endpoints",
)


@dataclass
class Report:
    """Accumulates check outcomes and renders the final report."""

    lines: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def ok(self, label: str, detail: str = "") -> None:
        self.lines.append(f"  PASS  {label}" + (f" — {detail}" if detail else ""))

    def fail(self, label: str, detail: str) -> None:
        self.lines.append(f"  FAIL  {label} — {detail}")
        self.failures.append(f"{label}: {detail}")

    def note(self, text_: str) -> None:
        self.lines.append(f"  ....  {text_}")

    def section(self, title: str) -> None:
        self.lines.append("")
        self.lines.append(title)

    def render(self) -> str:
        out = "\n".join(self.lines)
        out += "\n\n" + "=" * 72 + "\n"
        if self.failures:
            out += f"RESULT: FAILED ({len(self.failures)} check(s))\n"
            for failure in self.failures:
                out += f"  - {failure}\n"
        else:
            out += "RESULT: PASSED — the ordinary connection is confined by RLS.\n"
        return out


def resolve_url() -> str | None:
    """The ordinary-role URL to probe."""
    return os.environ.get("RLS_VERIFY_DATABASE_URL") or os.environ.get("DATABASE_URL")


def set_context(conn: Connection, workspace_id: str | None) -> None:
    """Set (or clear) ``app.workspace_id`` for the current transaction.

    Mirrors :func:`app_shared.database.set_workspace_context` exactly:
    bound parameter, ``is_local=true``. ``None`` clears it, which is the
    fail-closed case the policies' ``NULLIF(..., '')`` guard handles.
    """
    conn.execute(
        text("SELECT set_config('app.workspace_id', :wsid, true)"),
        {"wsid": "" if workspace_id is None else str(workspace_id)},
    )


def check_role(conn: Connection, report: Report) -> OrdinaryRoleFacts:
    """Check A — the connected role's attributes."""
    report.section("A. Connected role attributes")
    facts = inspect_ordinary_role(conn)
    report.note(
        f"role={facts.role_name} superuser={facts.is_superuser} "
        f"bypassrls={facts.has_bypassrls} "
        f"owned_public_tables={facts.owned_public_tables}"
    )
    if facts.is_superuser:
        report.fail("non-superuser", f"{facts.role_name} is a SUPERUSER (implicitly BYPASSRLS)")
    else:
        report.ok("non-superuser")

    if facts.has_bypassrls:
        report.fail("no BYPASSRLS", f"{facts.role_name} has rolbypassrls = true")
    else:
        report.ok("no BYPASSRLS")

    if facts.owned_public_tables:
        report.fail(
            "owns no tables",
            f"{facts.role_name} owns {facts.owned_public_tables} table(s) in schema public",
        )
    else:
        report.ok("owns no tables")
    return facts


def check_forced_rls(conn: Connection, report: Report) -> list[str]:
    """Check B — forced RLS coverage. Returns the probe-eligible tables.

    Partitions are deliberately **included** (security review A1,
    2026-08-20). This query used to carry ``AND c.relispartition IS
    FALSE``, which is the same blind spot `provision_roles.verify()` had
    from the other direction: the eight monthly partitions of
    ``request_attempts`` / ``price_observations`` / ``price_alert_events``
    / ``webhook_events`` each carry ``workspace_id``, each had no RLS at
    all, and each was excluded from the only check that would have said
    so. A partition holds real workspace rows and `crawmatic_app` can
    name it directly, so it is exactly as much of an isolation surface as
    its parent.

    The returned probe list is unaffected: it is intersected with
    :data:`CANDIDATE_TABLES`, which names parents only.
    """
    report.section("B. Forced RLS on workspace-scoped tables")
    rows = conn.execute(
        text(
            """
            SELECT c.relname                                      AS name,
                   c.relrowsecurity                               AS enabled,
                   c.relforcerowsecurity                          AS forced,
                   (SELECT count(*) FROM pg_policy p
                     WHERE p.polrelid = c.oid)                    AS policies
            FROM pg_class c
            JOIN pg_attribute a
              ON a.attrelid = c.oid
             AND a.attname = 'workspace_id'
             AND NOT a.attisdropped
            WHERE c.relnamespace = 'public'::regnamespace
              AND c.relkind IN ('r', 'p')
            ORDER BY c.relname
            """
        )
    ).all()

    if not rows:
        report.fail(
            "workspace-scoped tables visible",
            "no table with a workspace_id column is visible to this role",
        )
        return []

    unforced = [r.name for r in rows if not (r.enabled and r.forced)]
    unpolicied = [r.name for r in rows if r.enabled and r.policies == 0]
    if unforced:
        report.fail(
            "ENABLE + FORCE ROW LEVEL SECURITY",
            f"{len(unforced)} workspace-scoped table(s) not forced: {', '.join(unforced)}",
        )
    else:
        report.ok("ENABLE + FORCE ROW LEVEL SECURITY", f"{len(rows)} workspace-scoped tables")

    if unpolicied:
        report.fail("at least one policy", f"no policy on: {', '.join(unpolicied)}")
    else:
        report.ok("at least one policy per table")

    eligible = {r.name for r in rows if r.enabled and r.forced and r.policies}
    return [t for t in CANDIDATE_TABLES if t in eligible]


def pick_workspaces(conn: Connection, report: Report) -> tuple[str | None, list[str]]:
    """Return (workspace_a, all_workspace_ids).

    ``workspaces`` deliberately carries no RLS (it is the tenant root —
    see ``app_shared.models.identity.Workspace``), so the ordinary role
    can enumerate it.
    """
    try:
        ids = [str(r[0]) for r in conn.execute(text("SELECT id FROM workspaces ORDER BY created_at"))]
    except Exception as exc:  # pragma: no cover - environment dependent
        report.fail("enumerate workspaces", f"{type(exc).__name__}: {exc}")
        return None, []
    override = os.environ.get("RLS_VERIFY_WORKSPACE_ID")
    if override:
        return override, ids
    return (ids[0] if ids else None), ids


def probe_counts(
    conn: Connection,
    tables: list[str],
    workspace_id: str | None,
    *,
    owned_by: str | None = None,
) -> dict[str, int]:
    """Row counts per probe table under the given ``app.workspace_id``.

    ``owned_by`` restricts the count to rows belonging to that workspace.
    That distinction is the whole point of the foreign-context check: a
    confined role sees 0 of workspace A's rows while holding workspace
    B's context, whereas a ``BYPASSRLS``/superuser role happily returns
    all of them. Counting *all* visible rows instead would wrongly
    report B's own legitimate rows as a leak.

    Each count runs in its own transaction so the ``SET LOCAL`` context
    is scoped exactly like a real request.
    """
    counts: dict[str, int] = {}
    predicate = " WHERE workspace_id = :owner" if owned_by is not None else ""
    params = {"owner": owned_by} if owned_by is not None else {}
    for table in tables:
        # SQLAlchemy autobegins on the first statement; close any such
        # implicit transaction so each probe gets its own explicit one
        # (and therefore its own SET LOCAL scope).
        if conn.in_transaction():
            conn.rollback()
        with conn.begin():
            set_context(conn, workspace_id)
            counts[table] = int(
                conn.execute(text(f"SELECT count(*) FROM {table}{predicate}"), params).scalar_one()
            )
    return counts


def check_isolation(conn: Connection, tables: list[str], report: Report) -> None:
    """Checks C and D — own context sees rows, foreign/no context sees none."""
    report.section("C/D. Live workspace isolation probe")
    if not tables:
        report.fail("probe tables available", "no forced-RLS workspace table to probe")
        return

    workspace_a, all_ids = pick_workspaces(conn, report)
    if workspace_a is None:
        report.fail("probe workspace available", "no rows in `workspaces`")
        return

    own = probe_counts(conn, tables, workspace_a)
    populated = [t for t, n in own.items() if n > 0]
    report.note(
        f"workspace A = {workspace_a}; visible under its own context: "
        + ", ".join(f"{t}={own[t]}" for t in tables)
    )

    # C. Own context must actually see rows, else D proves nothing.
    if populated:
        report.ok(
            "own context sees own rows",
            f"{len(populated)} populated table(s), e.g. {populated[0]}={own[populated[0]]}",
        )
    else:
        report.fail(
            "own context sees own rows",
            "workspace A has no rows in any probe table — a zero result under a "
            "foreign context would prove nothing; pick a populated workspace via "
            "RLS_VERIFY_WORKSPACE_ID",
        )
        return

    # D1. A different real workspace, if one exists; otherwise a UUID
    # that is not any workspace. Either way the policy predicate
    # `workspace_id = <ctx>` must exclude every row of workspace A.
    others = [w for w in all_ids if w != workspace_a]
    if others:
        foreign = others[0]
        foreign_label = f"workspace B = {foreign} (a real second tenant)"
    else:
        foreign = str(uuid.uuid4())
        foreign_label = (
            f"synthetic context {foreign} — only one workspace exists in this "
            "database, so isolation is proven against a non-owning context"
        )
    report.note(foreign_label)

    foreign_counts = probe_counts(conn, populated, foreign, owned_by=workspace_a)
    leaked = {t: n for t, n in foreign_counts.items() if n > 0}
    if leaked:
        report.fail(
            "foreign context sees 0 of workspace A's rows",
            "RLS DID NOT CONFINE THIS CONNECTION: "
            + ", ".join(f"{t}={n} row(s) leaked" for t, n in leaked.items()),
        )
    else:
        report.ok(
            "foreign context sees 0 of workspace A's rows",
            f"{len(populated)} populated table(s) all returned 0",
        )

    # D2. Fail-closed: no context at all must also see nothing.
    none_counts = probe_counts(conn, populated, None)
    open_tables = {t: n for t, n in none_counts.items() if n > 0}
    if open_tables:
        report.fail(
            "no context sees 0 rows (fail closed)",
            ", ".join(f"{t}={n} row(s) visible with app.workspace_id unset" for t, n in open_tables.items()),
        )
    else:
        report.ok("no context sees 0 rows (fail closed)")


def check_txn_probe(conn: Connection, report: Report) -> None:
    """Optional two-tenant write probe inside an ALWAYS-rolled-back transaction."""
    report.section("E. Two-tenant write probe (transaction is always rolled back)")
    ws_b = uuid.uuid4()
    if conn.in_transaction():
        conn.rollback()
    trans = conn.begin()
    try:
        set_context(conn, None)
        conn.execute(
            text(
                "INSERT INTO workspaces (id, name, slug, status, created_at, updated_at) "
                "VALUES (:id, :name, :slug, 'ACTIVE', now(), now())"
            ),
            {"id": ws_b, "name": "rls-verify-throwaway", "slug": f"rls-verify-{ws_b}"},
        )
        set_context(conn, ws_b)
        visible = int(conn.execute(text("SELECT count(*) FROM products")).scalar_one())
        if visible:
            report.fail(
                "fresh workspace sees 0 foreign products",
                f"{visible} product row(s) visible to a brand-new workspace",
            )
        else:
            report.ok("fresh workspace sees 0 foreign products")
    except Exception as exc:
        report.fail("two-tenant write probe", f"{type(exc).__name__}: {exc}")
    finally:
        trans.rollback()
        report.note("transaction rolled back — no rows were persisted")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--txn-probe",
        action="store_true",
        help=(
            "additionally create a throwaway second workspace to make the "
            "isolation check a real two-tenant comparison; the transaction is "
            "always rolled back"
        ),
    )
    parser.add_argument("--verbose", action="store_true", help="echo the URL host being probed")
    args = parser.parse_args(argv)

    url = resolve_url()
    if not url:
        print(
            "rls_verify: set RLS_VERIFY_DATABASE_URL (or DATABASE_URL) to the "
            "ordinary application role's connection string.",
            file=sys.stderr,
        )
        return EXIT_CANNOT_RUN

    if args.verbose:
        # Never print credentials.
        print(f"rls_verify: probing {url.split('@')[-1]}")

    report = Report()
    report.lines.append("=" * 72)
    report.lines.append("RLS ISOLATION VERIFICATION (audit C3)")
    report.lines.append("=" * 72)

    engine = create_engine(url, connect_args={"prepare_threshold": None}, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            check_role(conn, report)
            tables = check_forced_rls(conn, report)
            check_isolation(conn, tables, report)
            if args.txn_probe:
                check_txn_probe(conn, report)
    except Exception as exc:
        print(f"rls_verify: could not run — {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_CANNOT_RUN
    finally:
        engine.dispose()

    print(report.render())
    return EXIT_FAILED if report.failures else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

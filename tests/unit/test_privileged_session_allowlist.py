"""Every use of a BYPASSRLS session is on a reviewed allow-list (2026-09-29, E8).

`get_auth_session` / `get_system_session` (and their sessionmakers and
engines) connect as a role that row-level security does not apply to. The
RLS probe now checks the TENANT role instead of the privileged one (see
test_ops_metrics_rls_probe.py) -- which is only honest if the privileged
path stays the narrow, deliberate exception it was designed to be. This test
pins that: a new reference anywhere in `apps/` or `libs/` fails until it is
added here with a reason a reviewer can check; a listed site that no longer
exists fails too, so the list cannot rot into a blanket pass.

AST, not grep (the `scripts/check_workspace_scoping.py` idiom): imports,
strings and comments never count; a reference passed as a value (e.g.
`scope = get_system_session`) counts exactly like a call.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_ROOTS = ("apps", "libs")
PRIVILEGED = frozenset(
    {
        "get_auth_session",
        "get_auth_sessionmaker",
        "get_auth_engine",
        "get_system_session",
        "get_system_sessionmaker",
        "get_system_engine",
    }
)
#: The module that DEFINES the seams is not a use of them.
EXEMPT_FILES = {"libs/shared/app_shared/database.py"}

_PRE_AUTH = "pre-auth credential/status lookup: no workspace context exists yet"
_FLEET_SCAN = "fleet-wide scan of refs across every workspace (per-workspace work then runs scoped)"

#: (repo-relative path, enclosing qualname) -> why this site must bypass RLS.
ALLOWLIST: dict[tuple[str, str], str] = {
    ("apps/api/app/deps.py", "_lookup_api_key_candidates"): _PRE_AUTH + " (api key by prefix)",
    ("apps/api/app/deps.py", "_fire_last_used_throttle"): "api key last_used_at stamp keyed by key id, before workspace context",
    ("apps/api/app/deps.py", "_authenticate_api_key"): _PRE_AUTH + " (workspace status cache fill)",
    ("apps/api/app/deps.py", "_authenticate_jwt"): _PRE_AUTH + " (user + workspace status cache fill)",
    ("apps/api/app/routers/auth.py", "_issue_pair"): "refresh-token issue at login, before a workspace is selected",
    ("apps/api/app/routers/auth.py", "login"): _PRE_AUTH + " (user by email)",
    ("apps/api/app/routers/auth.py", "refresh"): "refresh-token rotation: token lookup precedes workspace context",
    ("apps/api/app/routers/auth.py", "logout"): "refresh-token revocation by token hash",
    ("apps/api/app/routers/admin.py", "get_admin_session"): "platform-admin routes (usage export) aggregate across workspaces by design",
    ("apps/api/app/routers/control_plane.py", "get_control_plane_session"): "SaaS control plane (service token) writes entitlements for any workspace",
    ("apps/api/app/routers/catalog_index.py", "get_catalog_index_session"): "catalog index routes (service/index token, no workspace principal); every workspace-owned read carries an explicit workspace_id predicate",
    ("apps/api/app/routers/ops_metrics.py", "_get_ops_session"): "fleet aggregates for /ops/metrics; the tenant role is probed separately (E8)",
    ("apps/scheduler/app/scheduler/scheduler_app.py", "_run_refresh_pass_tick"): "cross-tenant due refresh-rule claim (SPEC-13 R2)",
    ("apps/scheduler/app/scheduler/scheduler_app.py", "_run_durable_cadence_tick"): "fleet maintenance cadence ledger",
    ("apps/scheduler/app/scheduler/scheduler_app.py", "_run_health_tick"): "fleet health heartbeat",
    ("apps/scheduler/app/scheduler/scheduler_app.py", "_run_ops_snapshot_tick"): "fleet ops snapshot; tenant role probed via get_engine (E8)",
    ("apps/scheduler/app/scheduler/scheduler_app.py", "_fair_queue_ledger"): "fair-scheduling ledger across workspaces",
    ("apps/scheduler/app/scheduler/scheduler_app.py", "_run_fair_scheduling_tick"): "fair-scheduling pass across workspaces",
    ("apps/workers/app/workers/tasks_jobs.py", "_scan_job_refs"): _FLEET_SCAN + " (jobs)",
    ("apps/workers/app/workers/tasks_jobs.py", "reap_stale_targets"): "fleet reaper: a wedged job in any workspace is the thing being fixed",
    ("apps/workers/app/workers/tasks_jobs.py", "reconcile_dispatch_intents"): "fleet dispatch-intent reconciliation",
    ("apps/workers/app/workers/tasks_jobs.py", "_purge_finalized_job_runs"): "cancel finished jobs' Scrapyd runs (reads intents with scoped_select) (E3.3)",
    ("apps/workers/app/workers/tasks_maintenance.py", "_system_session"): "fleet maintenance tasks' session seam",
    ("apps/workers/app/workers/tasks_outbox.py", "outbox_drain"): "outbox relay across workspaces",
    ("apps/workers/app/workers/tasks_outbox.py", "outbox_reconcile"): "outbox dead-letter sweep across workspaces",
    ("apps/workers/app/workers/tasks_strategy.py", "_scan_active_profile_refs"): _FLEET_SCAN + " (strategy profiles)",
    ("apps/workers/app/workers/tasks_strategy.py", "_scan_workspace_refs_with_profiles"): _FLEET_SCAN + " (workspaces with profiles)",
    ("apps/workers/app/workers/tasks_strategy.py", "_scan_stale_pattern_profile_refs"): _FLEET_SCAN + " (stale pattern profiles)",
    ("apps/workers/app/workers/tasks_strategy.py", "_scan_discovery_due_profile_refs"): _FLEET_SCAN + " (discovery due)",
    ("apps/workers/app/workers/tasks_strategy.py", "strategy_discovery_scan"): _FLEET_SCAN + " (discovery scan)",
    ("libs/shared/app_shared/costauth/service.py", "CostAuthorizationService._system_session"): "fleet budgets/breaker rows are not workspace-owned",
    ("libs/shared/app_shared/jobs/cancellation.py", "_resolve_workspace_id"): "resolve a job's workspace before a scoped cancel",
    ("libs/shared/app_shared/netledger/reconcile.py", "import_provider_usage"): "provider usage is fleet-level evidence",
    ("libs/shared/app_shared/netledger/reconcile.py", "reconcile_window"): "fleet ledger reconciliation against provider evidence",
    ("libs/shared/app_shared/netledger/recorder.py", "NetLedgerRecorder._session"): "network ledger is fleet-owned (C1)",
}


def _references() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for root in SCAN_ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel in EXEMPT_FILES or "/build/" in rel or "/tests/" in rel:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            stack: list[str] = []

            class _Visitor(ast.NodeVisitor):
                def _scope(self, node: ast.AST) -> None:
                    stack.append(node.name)  # type: ignore[attr-defined]
                    self.generic_visit(node)
                    stack.pop()

                visit_FunctionDef = _scope
                visit_AsyncFunctionDef = _scope
                visit_ClassDef = _scope

                def visit_Name(self, node: ast.Name) -> None:
                    if node.id in PRIVILEGED and isinstance(node.ctx, ast.Load):
                        found.add((rel, ".".join(stack) or "<module>"))

                def visit_Attribute(self, node: ast.Attribute) -> None:
                    if node.attr in PRIVILEGED and isinstance(node.ctx, ast.Load):
                        found.add((rel, ".".join(stack) or "<module>"))
                    self.generic_visit(node)

            _Visitor().visit(tree)
    return found


def test_every_privileged_session_site_is_allow_listed() -> None:
    unlisted = sorted(_references() - set(ALLOWLIST))
    assert not unlisted, (
        "new BYPASSRLS session use(s) -- add each to ALLOWLIST with the reason it "
        f"must bypass row-level security, or use get_session(): {unlisted}"
    )


def test_the_allow_list_has_no_stale_entries() -> None:
    stale = sorted(set(ALLOWLIST) - _references())
    assert not stale, f"allow-listed sites that no longer exist (remove them): {stale}"


def test_the_scan_is_not_vacuous() -> None:
    """If the AST walk silently matched nothing, the two tests above would
    pass on an empty set."""
    assert len(_references()) >= 30
    assert all(reason.strip() for reason in ALLOWLIST.values())

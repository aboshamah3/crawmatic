#!/usr/bin/env python3
"""check_workspace_scoping.py — static CI guard for Principle II (FR-020/SC-006).

Scans every ``.py`` file under ``apps/`` and ``libs/`` (repo root) and
exits non-zero, printing ``file:line: reason`` for each violation, when
it finds — for a workspace-owned model (``User``/``ApiKey``, imported
from ``app_shared.repository.WORKSPACE_OWNED_MODELS`` so the guarded set
never drifts from the runtime set):

1. ``<x>.get(User, ...)`` / ``<x>.get(ApiKey, ...)`` — an unscoped
   ``Session.get`` fetch-by-id.
2. ``select(User)`` / ``select(ApiKey)`` (and legacy
   ``<x>.query(User)``) **without** a ``workspace_id`` predicate
   (``.where(...workspace_id...)`` / ``.filter(...workspace_id...)`` /
   ``.filter_by(workspace_id=...)``) chained in the same expression.

Pure stdlib ``ast`` — no DB/Redis, runnable anywhere Python 3.13 is
available. Uses AST (not grep) so it understands call structure and
never matches inside strings/comments/prose (see
``specs/003-auth-api-keys-workspace-isolation/contracts/ci-scoping-guard.md``).

False-positive handling:

* **Path allowlist**: ``libs/shared/app_shared/repository.py`` (the
  sanctioned helper that constructs scoped selects generically) and any
  file under a ``tests/`` directory (or named like a test module) are
  exempt.
* **Line pragma**: a line carrying ``# noqa: workspace-scope`` is
  skipped.

CI wiring (alongside ``scripts/check_single_head.sh``)::

    - name: Workspace-scoping guard
      run: uv run python scripts/check_workspace_scoping.py
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCAN_ROOTS = ("apps", "libs")

# Files/paths exempt from scanning entirely (contract: path allowlist).
ALLOWLISTED_PATHS = {
    REPO_ROOT / "libs" / "shared" / "app_shared" / "repository.py",
}

NOQA_MARKER = "# noqa: workspace-scope"

# Method names that, chained after select(Model)/query(Model), count as a
# workspace_id predicate on that query.
_SCOPING_METHODS = ("where", "filter", "filter_by")


@dataclass(frozen=True)
class Violation:
    """A single guard finding."""

    path: Path
    line: int
    reason: str

    def __str__(self) -> str:  # pragma: no cover - trivial formatting
        try:
            rel = self.path.relative_to(REPO_ROOT)
        except ValueError:
            rel = self.path
        return f"{rel}:{self.line}: {self.reason}"


def _guarded_model_names() -> frozenset[str]:
    """Import the guarded set from app_shared.repository (single source of truth)."""
    from app_shared.repository import WORKSPACE_OWNED_MODELS

    return frozenset(model.__name__ for model in WORKSPACE_OWNED_MODELS)


def _is_test_path(path: Path) -> bool:
    parts = path.parts
    if "tests" in parts:
        return True
    name = path.name
    return name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"


def _is_allowlisted(path: Path) -> bool:
    if path in ALLOWLISTED_PATHS:
        return True
    return _is_test_path(path)


def _build_parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _name_or_attr_matches(node: ast.expr, guarded: frozenset[str]) -> str | None:
    """If ``node`` is a bare Name or an attribute access ending in a guarded model
    name (e.g. ``User`` or ``models.User``), return that name; else None."""
    if isinstance(node, ast.Name) and node.id in guarded:
        return node.id
    if isinstance(node, ast.Attribute) and node.attr in guarded:
        return node.attr
    return None


def _expr_references_workspace_id(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr == "workspace_id":
            return True
        if isinstance(sub, ast.Name) and sub.id == "workspace_id":
            return True
        if isinstance(sub, ast.keyword) and sub.arg == "workspace_id":
            return True
    return False


def _call_has_workspace_predicate(call: ast.Call) -> bool:
    for arg in call.args:
        if _expr_references_workspace_id(arg):
            return True
    for kw in call.keywords:
        if kw.arg == "workspace_id":
            return True
        if kw.value is not None and _expr_references_workspace_id(kw.value):
            return True
    return False


def _chain_is_scoped(
    call_node: ast.Call, parents: dict[ast.AST, ast.AST]
) -> bool:
    """Climb the fluent call-chain from ``call_node`` looking for a scoping method.

    Handles ``select(Model).where(...)``, ``session.query(Model).filter(...)``,
    ``...filter_by(workspace_id=...)`` and longer chains
    (``select(Model).options(...).where(...)``), stopping as soon as the
    chain is no longer a direct ``.attr(...)`` invocation on the current
    node (i.e. it's no longer syntactically the same call-chain).
    """
    current: ast.AST = call_node
    while True:
        parent = parents.get(current)
        if parent is None:
            return False
        if isinstance(parent, ast.Attribute) and parent.value is current:
            call_parent = parents.get(parent)
            if isinstance(call_parent, ast.Call) and call_parent.func is parent:
                if parent.attr in _SCOPING_METHODS and _call_has_workspace_predicate(
                    call_parent
                ):
                    return True
                current = call_parent
                continue
            return False
        return False


def _check_file(path: Path, guarded: frozenset[str]) -> list[Violation]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    lines = source.splitlines()
    parents = _build_parent_map(tree)
    violations: list[Violation] = []

    def _line_has_noqa(lineno: int) -> bool:
        if 1 <= lineno <= len(lines):
            return NOQA_MARKER in lines[lineno - 1]
        return False

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        func = node.func

        # Pattern 1: <x>.get(User|ApiKey, ...) — unscoped Session.get.
        if isinstance(func, ast.Attribute) and func.attr == "get" and node.args:
            model_name = _name_or_attr_matches(node.args[0], guarded)
            if model_name is not None and not _line_has_noqa(node.lineno):
                violations.append(
                    Violation(
                        path=path,
                        line=node.lineno,
                        reason=(
                            f"unscoped Session.get({model_name}, ...) on a "
                            "workspace-owned model — use "
                            "app_shared.repository.scoped_get() instead"
                        ),
                    )
                )
            continue

        # Pattern 2a: select(User|ApiKey) without a chained workspace_id predicate.
        if isinstance(func, ast.Name) and func.id == "select" and node.args:
            model_name = _name_or_attr_matches(node.args[0], guarded)
            if model_name is not None:
                if not _chain_is_scoped(node, parents) and not _line_has_noqa(
                    node.lineno
                ):
                    violations.append(
                        Violation(
                            path=path,
                            line=node.lineno,
                            reason=(
                                f"unscoped select({model_name}) on a "
                                "workspace-owned model — chain "
                                ".where(<Model>.workspace_id == ...) or use "
                                "app_shared.repository.scoped_select() instead"
                            ),
                        )
                    )
            continue

        # Pattern 2b: <x>.query(User|ApiKey) (legacy Query API) without a
        # chained workspace_id predicate.
        if isinstance(func, ast.Attribute) and func.attr == "query" and node.args:
            model_name = _name_or_attr_matches(node.args[0], guarded)
            if model_name is not None:
                if not _chain_is_scoped(node, parents) and not _line_has_noqa(
                    node.lineno
                ):
                    violations.append(
                        Violation(
                            path=path,
                            line=node.lineno,
                            reason=(
                                f"unscoped query({model_name}) on a "
                                "workspace-owned model — chain "
                                ".filter(<Model>.workspace_id == ...) / "
                                ".filter_by(workspace_id=...)"
                            ),
                        )
                    )
            continue

    return violations


def iter_scanned_files(repo_root: Path = REPO_ROOT) -> list[Path]:
    files: list[Path] = []
    for root_name in SCAN_ROOTS:
        root = repo_root / root_name
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts or ".venv" in path.parts:
                continue
            files.append(path)
    return files


def scan(repo_root: Path = REPO_ROOT) -> list[Violation]:
    guarded = _guarded_model_names()
    violations: list[Violation] = []
    for path in iter_scanned_files(repo_root):
        if _is_allowlisted(path):
            continue
        violations.extend(_check_file(path, guarded))
    return violations


def main(argv: list[str] | None = None) -> int:
    violations = scan()
    if violations:
        for violation in violations:
            print(str(violation))
        print(
            f"\ncheck_workspace_scoping: FAIL — {len(violations)} unscoped "
            "workspace-owned model access(es) found.",
            file=sys.stderr,
        )
        return 1
    print("check_workspace_scoping: OK — no unscoped workspace-owned model access.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

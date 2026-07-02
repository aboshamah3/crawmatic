# Contract: Workspace-scoping CI guard (`scripts/check_workspace_scoping.py`)

The build gate enforcing Principle II statically (FR-020/SC-006). Pure stdlib `ast` — **no DB/Redis**, runnable in this environment. Wired into CI exactly like `scripts/check_single_head.sh`.

## Behavior

Scans every `.py` under `apps/` and `libs/` and **exits non-zero** (printing file:line + reason) when it finds, for a **workspace-owned model** (`User`, `ApiKey` — imported from `app_shared.repository.WORKSPACE_OWNED_MODELS` so the guarded set matches the runtime set):

1. `<x>.get(User, ...)` / `<x>.get(ApiKey, ...)` — an unscoped `Session.get` fetch-by-id.
2. `select(User)` / `select(ApiKey)` (and legacy `<x>.query(User)`) **without** a `workspace_id` predicate (`.where(...workspace_id...)` / `.filter_by(workspace_id=...)`) in the same call-chain.

Exit 0 when no violations.

## False-positive handling

- **Path allowlist**: `libs/shared/app_shared/repository.py` (the sanctioned helper that constructs scoped selects generically) and test files that deliberately assert on violations are exempt.
- **Line pragma**: a line carrying `# noqa: workspace-scope` is skipped (discouraged; must be justified in review).

## Why AST (not grep)

AST understands call structure (attribute-call `x.get`, `select(<Name>)`) and ignores matches inside strings/comments, so it neither misses a real violation nor false-positives on prose — meeting "fails the build on 100% of introduced unscoped fetch-by-id / unscoped selects" (SC-006).

## CI wiring

```yaml
- name: Workspace-scoping guard
  run: uv run python scripts/check_workspace_scoping.py
```

Runs after `uv sync`, alongside `scripts/check_single_head.sh`.

## Tests (unit, no DB)

`test_workspace_scoping_guard.py`: the guard flags a planted `session.get(User, id)` and a planted unscoped `select(ApiKey)` in a temp file (exit non-zero), and passes a properly `scoped_select(...)` / `.where(User.workspace_id == ws)` snippet (exit 0).

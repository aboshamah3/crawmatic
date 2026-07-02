# Contract: Workspace-scoped repository helpers (`app_shared/repository.py`)

Application-layer defense-in-depth (FR-018, Principle II): the **sanctioned** way to query workspace-owned models. Framework-agnostic (SQLAlchemy only). This is the module the CI guard path-allowlists (it legitimately constructs scoped selects generically).

## Exposed symbols

```python
WORKSPACE_OWNED_MODELS: frozenset[type]      # {User, ApiKey} — extensible registry

def scoped_select(model, workspace_id):      # -> Select filtered by model.workspace_id == workspace_id
def scoped_get(session, model, id_, workspace_id):   # -> instance | None, filtered by BOTH id and workspace_id
def assert_workspace_owned_query_is_scoped(...): ...  # guard used by helpers/tests
```

## Guarantees

- `scoped_select(Model, ws_id)` returns `select(Model).where(Model.workspace_id == ws_id)` — a `workspace_id` predicate is **always** present.
- `scoped_get(session, Model, id_, ws_id)` fetches by `(id, workspace_id)` — **never** `session.get(Model, id_)` alone. For a workspace-owned model, calling the helpers **without** a `workspace_id` raises `ValueError` (FR-018).
- Callers use these helpers for all `User`/`ApiKey` access; combined with RLS (`set_workspace_context`) this is the two-layer isolation (app filter + DB RLS).
- `WORKSPACE_OWNED_MODELS` is the single source of truth the CI guard imports so the guarded set and the runtime set never drift.

## Tests (unit, no DB)

- `scoped_select(User, ws)` renders a `WHERE ... workspace_id = ...` clause.
- `scoped_get` requires a non-null `workspace_id` (raises otherwise).
- `WORKSPACE_OWNED_MODELS == {User, ApiKey}`.

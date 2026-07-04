# Contract: Dual-scope query helpers (`app_shared.access.repository`)

The sanctioned query path for the two **dual-scope** tables (`ProxyProvider`, `AccessPolicy`).
Deliberately **not** in `WORKSPACE_OWNED_MODELS` (its `scoped_select` would hide global rows).
SQLAlchemy-only, framework-agnostic. Mirrors `app_shared/profiles/repository.py` exactly.

## API

```python
# ProxyProvider
def visible_providers_select(workspace_id) -> Select   # own (== ws) OR global (IS NULL)
def owned_provider_select(workspace_id) -> Select      # own-only (write path)
def owned_provider_get(session, id_, workspace_id) -> ProxyProvider | None
def assert_provider_assignable(session, workspace_id, provider_id | None) -> None
    # None -> ok (clears). own+global -> ok. cross-workspace -> CrossWorkspaceReference.
    # dangling -> MissingReference. (reuses app_shared.catalog.consistency exceptions)

# AccessPolicy — identical shape
def visible_policies_select(workspace_id) -> Select
def owned_policy_select(workspace_id) -> Select
def owned_policy_get(session, id_, workspace_id) -> AccessPolicy | None
def assert_policy_assignable(session, workspace_id, policy_id | None) -> None
```

`DomainAccessRule` needs **no** dedicated repo — it is tenant-only, queried through the
standard `app_shared.repository.scoped_select(DomainAccessRule, workspace_id)` /
`scoped_get(...)`.

## Semantics

- `visible_*` = `where(or_(col.workspace_id == ws, col.workspace_id.is_(None)))` — read/list
  and the resolution `visible_ids` set (own+global).
- `owned_*` = `where(col.workspace_id == ws)` — create/update/delete; a global or cross-
  workspace id 404s through the tenant write path (FR-006 read-only globals).
- `assert_*_assignable` — cross-workspace → `422 WORKSPACE_MISMATCH`, dangling → `404`, own or
  global → OK, `None` → OK (clears the reference).

## Acceptance (skip-clean integration)

- A workspace sees its own rows + all global rows via `visible_*`; another workspace's tenant
  rows are absent.
- `owned_*` never returns a global row (write path cannot mutate a system default).
- No-context session → `visible_*` returns only globals (RLS), zero tenant rows.

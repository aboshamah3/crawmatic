# Contract: dual-scope profile repository (`app_shared/profiles/repository.py`)

The single sanctioned query path for `scrape_profiles` (which is dual-scope and therefore **not** in `WORKSPACE_OWNED_MODELS` / not usable via `scoped_select`). SQLAlchemy-only, framework-agnostic.

## Constant

```python
GLOBAL_DEFAULT_PROFILE_NAME = "global_default"   # the reserved name of the terminal global default (research D6)
```

## Read (own + global)

```python
def visible_profiles_select(workspace_id) -> Select:
    return select(ScrapeProfile).where(
        or_(ScrapeProfile.workspace_id == workspace_id,
            ScrapeProfile.workspace_id.is_(None)))
```

Used for list/get and for building the resolution `visible_ids` set. A workspace sees its own rows plus every global row (FR-013 read side).

## Manage (own only — never global)

```python
def owned_profile_select(workspace_id) -> Select:
    return select(ScrapeProfile).where(ScrapeProfile.workspace_id == workspace_id)

def owned_profile_get(session, id_, workspace_id) -> ScrapeProfile | None:
    # select where id == id_ AND workspace_id == workspace_id  (excludes global/other-ws)
```

Used by create/update/delete targets so a global (`NULL`) or other-workspace id yields "not found" on the tenant write path (FR-021). A tenant thus can never edit/delete a global profile through the API (belt-and-suspenders with the RLS write policy).

## Assignability (FR-013)

```python
def profile_visibility_map(session, workspace_id, ids) -> dict[uuid.UUID, uuid.UUID | None]:
    # one visible_profiles_select IN (...) lookup -> {id: workspace_id-or-None}

def assert_profile_assignable(session, workspace_id, profile_id) -> None:
    # profile_id is None -> OK (clearing an assignment is allowed)
    # profile_id resolves to own-ws OR global (workspace_id is None) -> OK
    # profile_id missing (dangling) -> NOT_FOUND
    # profile_id resolves to another workspace -> WORKSPACE_MISMATCH
```

Raises the shared consistency exceptions (`MissingReference` / `CrossWorkspaceReference` from `app_shared.catalog.consistency`) so the router maps them to `404` / `422` exactly like the SPEC-05 match/competitor reference checks. Called wherever a `scrape_profile_id`/`default_scrape_profile_id` is set (see `assignment-enforcement.md`).

## Tests (unit, no DB)

- `visible_profiles_select(ws)` compiles to a WHERE with `workspace_id = ws OR workspace_id IS NULL`.
- `owned_profile_select(ws)` compiles to `workspace_id = ws` only (no `IS NULL`).
- `assert_profile_assignable`: `None` → OK; own → OK; global (`None` workspace) → OK; missing → `MissingReference`; other-workspace → `CrossWorkspaceReference` (using an in-memory visibility map).

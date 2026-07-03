# Contract: Workspace-Consistency for Match References (reuse of `app_shared/catalog/consistency.py`)

No new module. This feature **reuses** the SPEC-04 workspace-consistency pre-check (`app_shared.catalog.consistency`) for the match's `product` / `product_variant` / `competitor` references — the helper is already framework- and entity-agnostic (operates on plain id sets/maps, no catalog coupling), so only new **call sites** in `routers/matches.py` are added (research D7).

## The two-layer model (Principle II)
- **Layer 1 (structural).** The three match composite FKs (`(workspace_id, ref_id) → parent(workspace_id, id)`) make a cross-workspace reference impossible at the DB. This is the guarantee.
- **Layer 2 (app pre-check).** `assert_refs_in_workspace(workspace_id, ref_ids, resolved)` turns a cross-workspace / nonexistent reference into a clean `422 WORKSPACE_MISMATCH` / `404 NOT_FOUND` **before** the FK would raise a raw `IntegrityError` (500). `resolved` is a `{id: workspace_id}` map the router builds from **one** scoped `IN(...)` lookup per referenced kind — never a per-id query.

## Router usage (`routers/matches.py`)
- **Competitor ref**: one scoped `select(Competitor.id, Competitor.workspace_id).where(Competitor.workspace_id == ws, Competitor.id.in_(ids))` → `{id: ws}` map → `assert_refs_in_workspace`.
- **Variant ref**: resolved by the variant lookup that also yields `product_id` (`contracts/matches-bulk-upsert.md`); a variant not returned by the scoped lookup is treated as an unresolved/out-of-workspace ref → rejected.
- **Product ref**: not client-supplied — derived from the resolved variant's parent, so it is always in-workspace and consistent with the variant (no separate check needed).
- `raise`d `MissingReference` → `404`; `CrossWorkspaceReference` → `422 WORKSPACE_MISMATCH`.

## Reused exception types (unchanged)
`WorkspaceConsistencyError`, `MissingReference`, `CrossWorkspaceReference` (the `ExactlyOneOfViolation` is catalog-group-only and unused here).

## Unit tests (no DB)
- In-workspace competitor/variant refs accepted; a ref mapped to another workspace → `CrossWorkspaceReference`; an absent ref → `MissingReference` (extend `test_workspace_consistency.py` with match-shaped cases, or assert reuse in the matches router tests).
(Live cross-workspace rejection → integration, PG host.)

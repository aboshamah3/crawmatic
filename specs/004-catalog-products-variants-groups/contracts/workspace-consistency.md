# Contract: Workspace-consistency of foreign references

Satisfies FR-009, §32, Principle II. Two layers:

## Layer 1 — Structural (composite FKs, the guarantee of record)
All catalog FKs include `workspace_id` and target a parent `unique(workspace_id, id)`:
- `product_variants(workspace_id, product_id) → products(workspace_id, id)`
- `product_group_items(workspace_id, product_group_id) → product_groups(workspace_id, id)`
- `product_group_items(workspace_id, product_id) → products(workspace_id, id)`
- `product_group_items(workspace_id, product_variant_id) → product_variants(workspace_id, id)`

A row can only reference a parent that shares its `workspace_id` — a cross-workspace reference is **impossible** at the DB, not merely app-checked. Nullable group-item FKs use `MATCH SIMPLE` (default): a NULL member column is unchecked (an item references either a product or a variant).

## Layer 2 — Application pre-check (`app_shared/catalog/consistency.py`, pure, UX)
Returns a clean typed rejection **before** the DB raises a raw `IntegrityError`, so the API answers `422`/`404` ("referenced entity not in this workspace") instead of a 500.
- `assert_refs_in_workspace(ws_id, refs: Mapping[model, id]) -> None` given a resolver — but the pure helper form takes the already-loaded `{id: workspace_id}` maps and asserts each referenced id exists and maps to `ws_id`; raises `CrossWorkspaceReference`/`MissingReference`.
- `exactly_one_of(product_id, product_variant_id) -> None` — a group item must set exactly one member (app rule; DB allows either via nullable FKs).

## Unit tests (no DB)
- In-workspace refs accepted; cross-workspace ref → `CrossWorkspaceReference`; nonexistent id → `MissingReference`.
- `exactly_one_of`: both-null and both-set → error; one-set → ok.
- (Live-DB) Inserting a variant/group-item with a cross-workspace parent raises a DB FK violation even if the app check were bypassed.

# Contract: Set-based Match Bulk-Upsert Core (`app_shared/matches/upsert.py`)

Pure — compiles SQLAlchemy Core (`postgresql` dialect) statements + plain-data resolution maps; **never executes anything and never opens a session**. `apps/api/app/routers/matches.py` executes the statement inside the request's already-workspace-scoped transaction. Implements FR-013/SC-006 (set-based, bounded, reject-and-report). Reuses `app_shared.catalog.upsert.dedup_last_wins` and `app_shared.catalog.consistency` unchanged (research D6/D7).

## Single arbiter (bounded — SC-006)
A match has exactly one unique key: `(workspace_id, product_variant_id, competitor_id, normalized_competitor_url)`. So — unlike the catalog upsert — there is **no identity-kind partitioning**: the whole safe batch is **one** `INSERT ... ON CONFLICT DO UPDATE`, regardless of `len(rows)`. No per-row loop anywhere.

## API
- `match_conflict_key(row: Mapping) -> tuple` → `(product_variant_id, competitor_id, normalized_competitor_url)` (the in-batch dedup + conflict key).
- `prepare_match_urls(rows: Sequence[Mapping]) -> tuple[list[dict], list[dict]]` → `(safe, rejected)`. For each row: `validate_competitor_url(row["competitor_url"])` then `derive_match_url_fields(...)`; on `UnsafeUrlError` append `{"index": i, "code": "UNSAFE_URL", "reason": err.reason, "url": row["competitor_url"]}` to `rejected` and drop the row; else stamp `normalized_competitor_url`/`url_pattern`/`url_pattern_version` onto a copy and append to `safe`. **Pure** — the reject-and-report policy is unit-testable without a DB.
- `variant_lookup_keys(rows) -> (external_ids: set, skus: set, variant_ids: set)` — which variant identities need a DB lookup (a row already carrying `product_variant_id` needs none).
- `resolve_match_variants(rows, *, by_external_id, by_sku, by_id) -> (resolved, unresolved)` — fill each row's `product_variant_id` **and** `product_id` (from the resolved variant's parent) from the maps the router built via **one** scoped `select(ProductVariant.id, .external_id, .sku, .product_id).where(...IN(...))`; rows naming an unknown variant go to `unresolved` (router rejects via the consistency helper → `422`/`404`).
- `build_matches_upsert(rows: Sequence[Mapping]) -> Insert` — one `pg_insert(CompetitorProductMatch).values(list(rows)).on_conflict_do_update(index_elements=[...4 cols...], set_={<updatable cols>, "updated_at": func.now()})`.

## Columns updated on conflict
`competitor_url`, `url_pattern`, `url_pattern_version`, `competitor_variant_identifier`, `competitor_variant_sku`, `competitor_variant_options`, `external_title`, `scrape_profile_id`, `access_policy_id`, `priority`, `status`, and `updated_at=func.now()`.

**Never** updated on conflict: the four conflict columns, `product_id`, `workspace_id`, `id`, `created_at`, and the **health fields** (`health_status`, `last_error_code`, `consecutive_failures`, `success_rate_7d`, `current_price_id`, `last_scraped_at`, `last_success_at`, `last_failed_at`) — those are owned by SPEC-07+ and must not be reset by an idempotent re-push.

## Router flow (`POST /v1/matches/bulk-upsert`)
1. `prepare_match_urls(payload rows)` → `(safe, rejected)`.
2. `dedup_last_wins(safe, match_conflict_key)` (in-batch last-wins).
3. Resolve variants: `variant_lookup_keys` → one scoped `IN(...)` select → `resolve_match_variants` (fills `product_variant_id` + `product_id`); `unresolved` → rejected/`422`.
4. Consistency-check `competitor_id`s in-workspace (one scoped `IN(...)` select + `assert_refs_in_workspace`).
5. `session.execute(build_matches_upsert(resolved))` — the single set-based statement.
6. Response: `{upserted: len(resolved), matches: [...], rejected: [...]}`.

## Unit tests (no DB)
- `build_matches_upsert` compiles to SQL with `ON CONFLICT (workspace_id, product_variant_id, competitor_id, normalized_competitor_url) DO UPDATE SET ...` including `updated_at = now()` and **excluding** the health columns; **one** statement for any batch size (assert single `Insert`, no per-row loop).
- `prepare_match_urls`: a mix of safe + unsafe URLs → safe rows carry stamped `normalized_competitor_url`/`url_pattern`/`url_pattern_version`; unsafe rows appear in `rejected[]` with the right index/code/reason and are absent from `safe` (the safe set is not aborted).
- `dedup_last_wins` on `match_conflict_key`: two rows with the same (variant, competitor, normalized URL) collapse last-wins.
- `resolve_match_variants`: fills `product_id` from the variant parent; a row naming an unknown variant lands in `unresolved`.

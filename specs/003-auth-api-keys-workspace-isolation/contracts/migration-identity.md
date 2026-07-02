# Contract: Identity migration + RLS + bootstrap seed

## Alembic migration `alembic/versions/<rev>_auth_identity_tables.py`

- `down_revision = "023a24e5717d"` (the SPEC-02 `_smoke_foundation` head) → keeps a **single linear history** (existing `scripts/check_single_head.sh` stays green).
- **Hand-authored** (no live Postgres for autogenerate), reproducing the ORM shapes in `data-model.md` exactly, honoring the SPEC-02 `NAMING_CONVENTION`.

### `upgrade()`

1. `op.create_table("workspaces", ...)` — id, name, slug (UNIQUE), status, default_scrape_profile_id (nullable, **no FK**), default_access_policy_id (nullable, **no FK**), created_at, updated_at.
2. `op.create_table("users", ...)` — id, workspace_id (**nullable**, FK→workspaces.id, indexed), email (UNIQUE), password_hash, role, status, created_at, updated_at.
3. `op.create_table("refresh_tokens", ...)` — id, user_id (FK→users.id, indexed), token_hash (UNIQUE/indexed), expires_at, revoked_at (nullable), created_at.
4. `op.create_table("api_keys", ...)` — id, workspace_id (NOT NULL, FK→workspaces.id, indexed), name, key_prefix (indexed), key_hash, scopes (JSONB), status, last_used_at (nullable), created_at, updated_at, revoked_at (nullable).
5. **RLS in the same migration** (FR-004, §32, Principle II):
   ```python
   from app_shared.models import emit_rls_policy
   for stmt in emit_rls_policy("users"):    op.execute(stmt)
   for stmt in emit_rls_policy("api_keys"): op.execute(stmt)
   ```
   Emits ENABLE + FORCE + the fail-closed `USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)` policy for each. `workspaces` and `refresh_tokens` get **no** RLS (tenant root / user-owned — data-model.md).

### `downgrade()`

`op.drop_table("api_keys")`, `"refresh_tokens"`, `"users"`, `"workspaces")` in FK-safe reverse order (dropping the tables drops their policies).

### Render guarantee

`alembic upgrade head --sql` (offline, no DB) renders all four `CREATE TABLE`s + the six RLS statements; `alembic heads` shows exactly one head. Verified by `tests/unit/test_migration_offline_auth.py`.

## Two-role note (research D4)

RLS FORCE applies to the table owner too. The request-serving role (`crawmatic_app`, `DATABASE_URL`) has **no** BYPASSRLS. A dedicated `crawmatic_auth` role with **BYPASSRLS** (via `AUTH_DATABASE_URL`) performs only the pre-context credential lookups (user-by-email, api-key-by-prefix). Creating these DB roles is a cluster-level ops step (documented in quickstart), not part of the migration DDL (roles are not schema objects Alembic manages here).

## Bootstrap seed `scripts/seed_bootstrap.py`

- Idempotent; run via the **direct** connection (`MIGRATION_DATABASE_URL`, privileged → bypasses RLS during bootstrap).
- Reads `BOOTSTRAP_ADMIN_EMAIL`, `BOOTSTRAP_ADMIN_PASSWORD`, optional `BOOTSTRAP_WORKSPACE_NAME`/`_SLUG` from env.
- Creates the first `workspaces` row (if absent) and a `SUPER_ADMIN` `users` row (`workspace_id=NULL`, argon2id-hashed password). No public signup (§38, spec Assumptions).

## Tests

- Unit: `test_migration_offline_auth.py` (offline render + single head); `test_rls_identity.py` (emit_rls_policy strings for users/api_keys contain ENABLE/FORCE + the fail-closed predicate).
- Live (deferred): `test_migration_job` variant runs `upgrade head` on real PG; the four tables exist and RLS is enabled; the seed creates workspace + SUPER_ADMIN.

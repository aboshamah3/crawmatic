-- =====================================================================
-- scripts/rls_provision.sql — idempotent PostgreSQL role provisioning
-- for workspace isolation.
--
-- Audit: CORE_PRODUCT_PRODUCTION_READINESS_AUDIT_2026-08-15.md §C3.
--
-- WHY THIS IS NOT AN ALEMBIC MIGRATION
-- ------------------------------------
-- Roles are cluster-level objects, not schema objects: they are shared
-- by every database in the cluster, they carry secrets (passwords), and
-- they must exist BEFORE the application that authenticates as them.
-- Alembic manages the schema of one database and is run by a privileged
-- role; putting CREATE ROLE in it would make every migration require
-- CREATEROLE and would put a password in the migration history. Roles
-- stay here, deliberately — but "outside Alembic" must not mean
-- "undocumented and unexecutable", which is what §C3 flagged.
--
-- THE ROLE MODEL
-- --------------
--   crawmatic_app   the ORDINARY connection (DATABASE_URL) used by
--                   api / worker / scheduler for every workspace-owned
--                   read and write. NOSUPERUSER, NOBYPASSRLS, owns
--                   nothing. FORCE ROW LEVEL SECURITY + the per-
--                   transaction app.workspace_id GUC confine it.
--   crawmatic_auth  narrowly-scoped BYPASSRLS role (AUTH_DATABASE_URL /
--                   SYSTEM_DATABASE_URL) for the three structurally
--                   cross-tenant seams only: pre-auth credential
--                   lookup, the SaaS admin control plane, and the
--                   scheduler's due-rule claim. See
--                   app_shared.database.get_auth_session /
--                   get_system_session.
--   <owner>         the migration/bootstrap role (MIGRATION_DATABASE_URL).
--                   Owns every table. Never used by a running service.
--
-- USAGE
-- -----
--   psql "$MIGRATION_DATABASE_URL" \
--        -v ON_ERROR_STOP=1 \
--        -v app_password="'...'" \
--        -v auth_password="'...'" \
--        -f scripts/rls_provision.sql
--
-- Note the doubled quoting on the -v values: the passwords are
-- interpolated as SQL literals, so pass them already single-quoted.
-- Omit a password variable to leave that role's existing password
-- untouched (the DO blocks below default both to NULL).
--
-- Safe to re-run: every statement is a CREATE-if-absent / ALTER-to-
-- desired-state / idempotent GRANT. It changes no rows; the only
-- schema it touches is the RLS posture it exists to REPAIR (a lost
-- FORCE, a partition with no policies of its own).
-- Verify the result with:  uv run python scripts/rls_verify.py
-- =====================================================================

\set ON_ERROR_STOP on

-- Default the password variables so the file runs without -v.
\if :{?app_password}  \else \set app_password  NULL \endif
\if :{?auth_password} \else \set auth_password NULL \endif

BEGIN;

-- psql does NOT interpolate :variables inside dollar-quoted bodies, so the
-- passwords are parked in transaction-local GUCs here (where interpolation
-- does happen) and read back out with current_setting() inside the DO
-- blocks below. `false` as the third argument would make them session-
-- scoped; `true` keeps them LOCAL to this transaction so they cannot
-- outlive the script.
SELECT set_config('rls_provision.app_password',  :app_password,  true);
SELECT set_config('rls_provision.auth_password', :auth_password, true);

-- The role/GRANT body itself lives in scripts/sql/rls_roles.sql so the
-- one-shot deploy step (`migrate.provision_roles`, which runs in an
-- image with no psql client) executes the exact same statements. `\ir`
-- resolves relative to THIS file, so the include works from any CWD.
\ir sql/rls_roles.sql

COMMIT;

-- ---------------------------------------------------------------------
-- 7. Report the resulting state (read-only).
-- ---------------------------------------------------------------------
\echo '--- roles ---'
SELECT rolname, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolcanlogin
FROM pg_roles
WHERE rolname IN ('crawmatic_app', 'crawmatic_auth')
ORDER BY rolname;

\echo '--- tables owned by a runtime role (must be zero rows) ---'
SELECT tablename, tableowner
FROM pg_tables
WHERE schemaname = 'public'
  AND tableowner IN ('crawmatic_app', 'crawmatic_auth');

\echo '--- RLS-enabled tables missing FORCE (must be zero rows) ---'
SELECT c.relname
FROM pg_class c
WHERE c.relnamespace = 'public'::regnamespace
  AND c.relkind IN ('r', 'p')
  AND c.relrowsecurity
  AND NOT c.relforcerowsecurity;

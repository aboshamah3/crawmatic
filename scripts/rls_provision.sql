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
-- desired-state / idempotent GRANT. It changes no rows and no schema.
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

-- ---------------------------------------------------------------------
-- 1. The ordinary application role — NOSUPERUSER, NOBYPASSRLS, owns
--    nothing. These attributes are re-asserted on every run, so a role
--    that was hand-created (or hand-escalated) is repaired in place.
-- ---------------------------------------------------------------------
DO $$
DECLARE
    pw text := nullif(current_setting('rls_provision.app_password', true), '');
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crawmatic_app') THEN
        EXECUTE 'CREATE ROLE crawmatic_app LOGIN';
        RAISE NOTICE 'created role crawmatic_app';
    END IF;

    -- Repair to the required attribute set regardless of how it was made.
    EXECUTE 'ALTER ROLE crawmatic_app '
            'LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION INHERIT';

    IF pw IS NOT NULL THEN
        EXECUTE format('ALTER ROLE crawmatic_app PASSWORD %L', pw);
    END IF;
END
$$;

-- ---------------------------------------------------------------------
-- 2. The narrowly-scoped BYPASSRLS role for the auth / admin / scheduler
--    system sessions. BYPASSRLS is intentional here and ONLY here; it is
--    still NOSUPERUSER, NOCREATEROLE and owns nothing.
-- ---------------------------------------------------------------------
DO $$
DECLARE
    pw text := nullif(current_setting('rls_provision.auth_password', true), '');
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crawmatic_auth') THEN
        EXECUTE 'CREATE ROLE crawmatic_auth LOGIN BYPASSRLS';
        RAISE NOTICE 'created role crawmatic_auth';
    END IF;

    EXECUTE 'ALTER ROLE crawmatic_auth '
            'LOGIN NOSUPERUSER BYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION INHERIT';

    IF pw IS NOT NULL THEN
        EXECUTE format('ALTER ROLE crawmatic_auth PASSWORD %L', pw);
    END IF;
END
$$;

-- ---------------------------------------------------------------------
-- 3. Connect + schema usage. USAGE only — never CREATE: neither runtime
--    role may add or alter schema objects.
-- ---------------------------------------------------------------------
DO $$
BEGIN
    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO crawmatic_app, crawmatic_auth',
        current_database()
    );
END
$$;

GRANT USAGE ON SCHEMA public TO crawmatic_app, crawmatic_auth;
REVOKE CREATE ON SCHEMA public FROM crawmatic_app, crawmatic_auth;

-- ---------------------------------------------------------------------
-- 4. Table + sequence privileges.
--
--    Both roles get plain DML on the application tables. They get no
--    DDL, no TRUNCATE, no REFERENCES: a tenant-facing connection must
--    not be able to disable its own isolation, and TRUNCATE is not
--    filtered by row-level policies at all.
--
--    alembic_version is read-only for both (GET /version reads it);
--    only the migration role writes it.
-- ---------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE
    ON ALL TABLES IN SCHEMA public
    TO crawmatic_app, crawmatic_auth;

GRANT USAGE, SELECT
    ON ALL SEQUENCES IN SCHEMA public
    TO crawmatic_app, crawmatic_auth;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = 'alembic_version'
    ) THEN
        EXECUTE 'REVOKE INSERT, UPDATE, DELETE ON public.alembic_version '
                'FROM crawmatic_app, crawmatic_auth';
    END IF;
END
$$;

-- ---------------------------------------------------------------------
-- 5. Default privileges, so a table created by a FUTURE migration is
--    reachable without re-running this file.
--
--    Default ACLs are recorded per granting role: they apply to objects
--    created BY the role named in FOR ROLE. That must be whichever role
--    runs Alembic (the current user here — this file is run with
--    MIGRATION_DATABASE_URL, the same role that runs migrations).
-- ---------------------------------------------------------------------
DO $$
DECLARE
    owner_role text := current_user;
BEGIN
    EXECUTE format(
        'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
        'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES '
        'TO crawmatic_app, crawmatic_auth',
        owner_role
    );
    EXECUTE format(
        'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
        'GRANT USAGE, SELECT ON SEQUENCES TO crawmatic_app, crawmatic_auth',
        owner_role
    );
END
$$;

-- ---------------------------------------------------------------------
-- 6. Repair FORCE ROW LEVEL SECURITY on any table that has RLS enabled
--    without it. Emitted by app_shared.models.rls at table-creation
--    time, so this should be a no-op — it exists because a table that
--    lost FORCE is invisible in every code review and exempts the owner.
-- ---------------------------------------------------------------------
DO $$
DECLARE
    rel record;
BEGIN
    FOR rel IN
        SELECT c.oid::regclass AS ident
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relkind IN ('r', 'p')
          AND c.relrowsecurity
          AND NOT c.relforcerowsecurity
    LOOP
        EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY', rel.ident);
        RAISE NOTICE 'restored FORCE ROW LEVEL SECURITY on %', rel.ident;
    END LOOP;
END
$$;

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

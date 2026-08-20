-- =====================================================================
-- scripts/sql/rls_roles.sql — the ROLE + GRANT body, pure SQL.
--
-- Split out of scripts/rls_provision.sql (which stays the psql
-- entry point) so the SAME statements can also be executed by
-- `migrate.provision_roles` — the deploy step that runs alongside
-- `alembic upgrade head`, in an image that has no psql client.
-- One source of truth for the GRANTs: whichever way you run it, these
-- are the statements that run.
--
-- Contains NO psql meta-commands (no \set / \if / \echo) and no
-- BEGIN/COMMIT: the caller opens the transaction and is responsible for
-- having set the two password GUCs first, e.g.
--
--   SELECT set_config('rls_provision.app_password',  '<pw>', true);
--   SELECT set_config('rls_provision.auth_password', '<pw>', true);
--
-- Both default to absent, in which case the corresponding role keeps
-- whatever password it already has (roles are still created/repaired).
--
-- Idempotent: every statement is a CREATE-if-absent / ALTER-to-desired-
-- state / idempotent GRANT. It changes no rows and no schema.
-- =====================================================================

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

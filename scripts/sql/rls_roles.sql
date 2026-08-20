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
-- state / idempotent GRANT. It changes no rows; the only schema it
-- touches is the RLS posture sections 6 and 7 exist to REPAIR
-- (a lost FORCE, a partition with no policies of its own).
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

-- ---------------------------------------------------------------------
-- 7. Every PARTITION inherits its parent's row-level security.
--
--    A partitioned parent's policies are applied to a query that names
--    the PARENT. A query that names a CHILD PARTITION is checked
--    against that child's own policies, and `CREATE TABLE ...
--    PARTITION OF` gives a child none — while section 4 above grants
--    crawmatic_app SELECT on every table in the schema, partitions
--    included. Until the 2026-08-20 security review, that meant
--    `SELECT * FROM request_attempts_2026_08` returned every
--    workspace's rows to a tenant connection that
--    `SELECT * FROM request_attempts` correctly confined: isolation
--    defeated by spelling the table name differently.
--
--    The block below mirrors each parent's ENABLE/FORCE switches and
--    each of its policies onto its children, deriving them from
--    `pg_policy` rather than restating them, so a dual-scope table's
--    read/write PAIR is copied as faithfully as a strict single policy
--    and no second definition of any parent's rules exists here to
--    drift.
--
--    Do NOT edit this block in place: it is byte-identical to
--    `app_shared.models.rls.PARTITION_RLS_INHERITANCE_SQL` (the same
--    statement the migration and the runtime partition-creation job
--    run), and `tests/unit/test_rls_policy.py` fails if they diverge.
-- ---------------------------------------------------------------------
-- BEGIN partition-rls-inheritance
DO $$
DECLARE
    part         record;
    pol          record;
    roles_clause text;
    stmt         text;
BEGIN
    FOR part IN
        SELECT child.oid AS child_oid,
               parent.oid AS parent_oid,
               quote_ident(ns.nspname) || '.' || quote_ident(child.relname) AS child_ident
        FROM pg_inherits
        JOIN pg_class AS child ON child.oid = pg_inherits.inhrelid
        JOIN pg_class AS parent ON parent.oid = pg_inherits.inhparent
        JOIN pg_namespace AS ns ON ns.oid = child.relnamespace
        WHERE ns.nspname = 'public'
          AND child.relkind = 'r'
          AND child.relispartition
          AND parent.relrowsecurity
    LOOP
        EXECUTE 'ALTER TABLE ' || part.child_ident || ' ENABLE ROW LEVEL SECURITY';
        EXECUTE 'ALTER TABLE ' || part.child_ident || ' FORCE ROW LEVEL SECURITY';

        FOR pol IN
            SELECT p.polname,
                   p.polpermissive,
                   p.polroles,
                   CASE p.polcmd
                       WHEN '*' THEN 'ALL'
                       WHEN 'r' THEN 'SELECT'
                       WHEN 'a' THEN 'INSERT'
                       WHEN 'w' THEN 'UPDATE'
                       WHEN 'd' THEN 'DELETE'
                   END AS cmd_text,
                   pg_get_expr(p.polqual, p.polrelid) AS using_expr,
                   pg_get_expr(p.polwithcheck, p.polrelid) AS check_expr
            FROM pg_policy p
            WHERE p.polrelid = part.parent_oid
        LOOP
            CONTINUE WHEN EXISTS (
                SELECT 1 FROM pg_policy q
                WHERE q.polrelid = part.child_oid
                  AND q.polname = pol.polname
            );

            SELECT string_agg(quote_ident(r.rolname), ', ')
              INTO roles_clause
              FROM pg_roles r
             WHERE r.oid = ANY (pol.polroles);

            stmt := 'CREATE POLICY ' || quote_ident(pol.polname)
                 || ' ON ' || part.child_ident
                 || CASE WHEN pol.polpermissive THEN ' AS PERMISSIVE' ELSE ' AS RESTRICTIVE' END
                 || ' FOR ' || pol.cmd_text;

            IF roles_clause IS NOT NULL THEN
                stmt := stmt || ' TO ' || roles_clause;
            END IF;
            IF pol.using_expr IS NOT NULL THEN
                stmt := stmt || ' USING (' || pol.using_expr || ')';
            END IF;
            IF pol.check_expr IS NOT NULL THEN
                stmt := stmt || ' WITH CHECK (' || pol.check_expr || ')';
            END IF;

            EXECUTE stmt;
        END LOOP;
    END LOOP;
END
$$;
-- END partition-rls-inheritance

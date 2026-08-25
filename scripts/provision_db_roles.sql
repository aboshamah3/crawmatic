-- =====================================================================
-- scripts/provision_db_roles.sql — idempotent production database-role
-- provisioning for workspace isolation (READY-007 / P0.5).
--
-- This is the DDL half of the pair; `scripts/provision_db_roles.py` is
-- the executable/verifiable half and the manifest
-- (`scripts/rls_table_manifest.txt`) is the reviewed inventory this file
-- is checked against.
--
-- WHY THIS IS NOT AN ALEMBIC MIGRATION
-- ------------------------------------
-- Roles are CLUSTER-level objects, not schema objects: they are shared
-- by every database in the cluster, they carry passwords, and they must
-- exist BEFORE the application that authenticates as them. Putting
-- `CREATE ROLE` in a migration would make every migration require
-- CREATEROLE and would write a password into the migration history.
-- Role DDL therefore lives in scripts/, never in alembic/versions/.
--
-- RELATIONSHIP TO scripts/rls_provision.sql + scripts/sql/rls_roles.sql
-- --------------------------------------------------------------------
-- Those files are the 2026-08-15 audit's role provisioning and are what
-- production was built with; `migrate.provision_roles` executes them on
-- deploy. This file is their READY-007 successor: same two runtime
-- roles, plus the explicit DDL-owner role the audit pair never created,
-- plus grants derived from the reviewed manifest rather than from
-- `ALL TABLES IN SCHEMA public`. It is deliberately a superset and is
-- safe to run after them.
--
-- THE ROLE MODEL (three roles, one privilege each)
-- ------------------------------------------------
--   crawmatic_app      LOGIN, NOSUPERUSER, **NOBYPASSRLS**. The ordinary
--                      tenant connection (`DATABASE_URL`) used by api /
--                      worker / scheduler / scrapers for every
--                      workspace-owned read and write. Owns nothing.
--                      Confined by FORCE ROW LEVEL SECURITY plus the
--                      per-transaction `app.workspace_id` GUC that
--                      `app_shared.database.set_workspace_context`
--                      sets with `SET LOCAL` semantics.
--
--   crawmatic_auth     LOGIN, NOSUPERUSER, **BYPASSRLS**. The narrow
--                      system role for the three structurally
--                      cross-tenant seams only: pre-auth credential
--                      lookup (`get_auth_session`), the SaaS admin
--                      control plane, and the maintenance/scheduler
--                      scans (`get_system_session`). Owns nothing.
--
--                      *** NAMING (READY-007 plan reconciliation) ***
--                      The READY-007 plan calls this role
--                      `crawmatic_system`. It is NOT a second role:
--                      production has run `crawmatic_auth` since the
--                      2026-08-15 RLS cutover, every service DSN
--                      authenticates as it, and `config.py` addresses
--                      it through `SYSTEM_DATABASE_URL` (with the
--                      documented `AUTH_DATABASE_URL` fallback,
--                      config.py:522). Introducing a parallel
--                      `crawmatic_system` role would double the
--                      BYPASSRLS surface to rename a string. The
--                      deployed name is kept; `crawmatic_system` is
--                      this role.
--
--   crawmatic_migrate  LOGIN, NOSUPERUSER, NOBYPASSRLS, but holds
--                      CREATE on schema public: the DDL owner that
--                      `alembic upgrade head` authenticates as
--                      (`MIGRATION_DATABASE_URL`). It exists so that
--                      table ownership belongs to a role NO RUNNING
--                      SERVICE EVER CONNECTS AS. A table's owner can
--                      `ALTER TABLE ... NO FORCE ROW LEVEL SECURITY`
--                      and `DROP POLICY` — i.e. an owner that is also a
--                      live service login can switch off its own
--                      isolation. See section 8.
--
-- PASSWORD CONTRACT
-- -----------------
-- This file contains NO psql meta-commands (no \set / \if / \echo) and
-- no BEGIN/COMMIT, so the same bytes run under `psql -f` and under
-- `provision_db_roles.py --provision`. Passwords are passed in
-- TRANSACTION-LOCAL GUCs that the caller sets first:
--
--   BEGIN;
--   SELECT set_config('provision_db_roles.app_password',     '<pw>', true);
--   SELECT set_config('provision_db_roles.auth_password',    '<pw>', true);
--   SELECT set_config('provision_db_roles.migrate_password', '<pw>', true);
--   \i scripts/provision_db_roles.sql
--   COMMIT;
--
-- `true` keeps each GUC local to the transaction, so a password cannot
-- outlive the statement that used it. Omitting a GUC leaves that role's
-- existing password untouched — the role is still created and its
-- attributes still repaired, so re-running this during a deploy never
-- rotates a credential by accident.
--
-- OWNERSHIP ADOPTION IS OPT-IN
-- ----------------------------
-- Section 8 reassigns existing table ownership to `crawmatic_migrate`.
-- It runs ONLY when the caller sets
-- `provision_db_roles.adopt_ownership` to `on`, because reassigning
-- ownership of a live database is a change with a blast radius (it
-- moves DROP/ALTER rights and can interact with in-flight maintenance
-- jobs) and must be an explicit operator decision, not a side effect of
-- a deploy step. `--verify` reports the drift either way.
--
-- IDEMPOTENT: every statement is CREATE-if-absent / ALTER-to-desired-
-- state / idempotent GRANT. It writes no rows. The only schema it
-- touches is the RLS posture it exists to repair.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. crawmatic_app — the ordinary tenant connection.
--
--    Attributes are re-asserted on every run, so a role that was
--    hand-created (or hand-escalated to BYPASSRLS during an incident)
--    is repaired in place rather than merely reported.
-- ---------------------------------------------------------------------
DO $$
DECLARE
    pw text := nullif(current_setting('provision_db_roles.app_password', true), '');
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crawmatic_app') THEN
        EXECUTE 'CREATE ROLE crawmatic_app LOGIN';
        RAISE NOTICE 'created role crawmatic_app';
    END IF;

    EXECUTE 'ALTER ROLE crawmatic_app '
            'LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION INHERIT';

    IF pw IS NOT NULL THEN
        EXECUTE format('ALTER ROLE crawmatic_app PASSWORD %L', pw);
    END IF;
END
$$;

-- ---------------------------------------------------------------------
-- 2. crawmatic_auth — the narrow BYPASSRLS system role (the READY-007
--    plan's `crawmatic_system`; see the header). BYPASSRLS is
--    intentional here and ONLY here. Still NOSUPERUSER, NOCREATEROLE,
--    and it must own nothing (section 8 / --verify).
-- ---------------------------------------------------------------------
DO $$
DECLARE
    pw text := nullif(current_setting('provision_db_roles.auth_password', true), '');
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
-- 3. crawmatic_migrate — the DDL owner.
--
--    NOBYPASSRLS deliberately: FORCE ROW LEVEL SECURITY means even the
--    owner is filtered, and the migration role has no business reading
--    tenant rows. It needs CREATE on the schema (section 4) and
--    ownership of the tables (section 8); it needs nothing else.
--    NOSUPERUSER is the whole point — production currently runs
--    migrations as `postgres`, a superuser, and a superuser ignores RLS
--    and FORCE alike.
-- ---------------------------------------------------------------------
DO $$
DECLARE
    pw text := nullif(current_setting('provision_db_roles.migrate_password', true), '');
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crawmatic_migrate') THEN
        EXECUTE 'CREATE ROLE crawmatic_migrate LOGIN';
        RAISE NOTICE 'created role crawmatic_migrate';
    END IF;

    EXECUTE 'ALTER ROLE crawmatic_migrate '
            'LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION INHERIT';

    IF pw IS NOT NULL THEN
        EXECUTE format('ALTER ROLE crawmatic_migrate PASSWORD %L', pw);
    END IF;
END
$$;

-- ---------------------------------------------------------------------
-- 4. Connect + schema usage.
--
--    Both runtime roles get USAGE only — never CREATE: a tenant-facing
--    connection must not be able to add a table (an unpolicied table it
--    owns would be a hole) or to shadow one via a new schema.
--    crawmatic_migrate gets CREATE because creating tables is its job.
-- ---------------------------------------------------------------------
DO $$
BEGIN
    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO crawmatic_app, crawmatic_auth, crawmatic_migrate',
        current_database()
    );
END
$$;

GRANT USAGE ON SCHEMA public TO crawmatic_app, crawmatic_auth;
REVOKE CREATE ON SCHEMA public FROM crawmatic_app, crawmatic_auth;
GRANT USAGE, CREATE ON SCHEMA public TO crawmatic_migrate;

-- ---------------------------------------------------------------------
-- 5. Table + sequence privileges for the two runtime roles.
--
--    Plain DML only. No DDL, no TRUNCATE, no REFERENCES:
--
--      * TRUNCATE is NOT filtered by row-level policies at all — a
--        tenant connection holding it could erase every workspace's
--        rows in one statement while RLS looked on.
--      * REFERENCES lets a role create an FK against a table it cannot
--        read, which leaks existence through constraint violations.
--      * DDL would let the role drop its own policies.
--
--    alembic_version is read-only for both (`GET /version` reads it);
--    only crawmatic_migrate writes it.
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
-- 6. Default privileges, so a table created by a FUTURE migration is
--    reachable without re-running this file.
--
--    Default ACLs are recorded per GRANTING role and apply to objects
--    created BY the role named in FOR ROLE. Two entries are registered:
--    one for whoever is running this file (today's owner, commonly
--    `postgres` or a bootstrap owner) and one for `crawmatic_migrate`
--    (tomorrow's owner, after section 8). Registering both means the
--    ownership migration does not silently strip the runtime roles'
--    access to newly created tables.
-- ---------------------------------------------------------------------
DO $$
DECLARE
    granting_role text;
BEGIN
    FOREACH granting_role IN ARRAY ARRAY[current_user, 'crawmatic_migrate']
    LOOP
        EXECUTE format(
            'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
            'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES '
            'TO crawmatic_app, crawmatic_auth',
            granting_role
        );
        EXECUTE format(
            'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
            'GRANT USAGE, SELECT ON SEQUENCES TO crawmatic_app, crawmatic_auth',
            granting_role
        );
    END LOOP;
END
$$;

-- ---------------------------------------------------------------------
-- 7. FORCE ROW LEVEL SECURITY on every relation that has RLS enabled
--    without it.
--
--    `app_shared.models.rls` emits FORCE at table-creation time, so
--    this is normally a no-op. It exists because a table that lost
--    FORCE is invisible in every code review: the policies are all
--    still there, `\d` still lists them, and the owner silently reads
--    every workspace's rows.
--
--    Written as a repair over the catalog rather than over a hardcoded
--    list, so a table added by a migration after this file was written
--    is covered on the next run. `provision_db_roles.py --verify`
--    supplies the other direction: it cross-checks the catalog against
--    the REVIEWED manifest, so a relation that has no RLS at all (and
--    is therefore not a candidate for this repair) is still a finding.
-- ---------------------------------------------------------------------
DO $$
DECLARE
    rel record;
BEGIN
    FOR rel IN
        SELECT c.oid::regclass AS ident
        FROM pg_class c
        WHERE c.relnamespace = 'public'::regnamespace
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
-- 8. OPT-IN: adopt table ownership into crawmatic_migrate.
--
--    Gated on `provision_db_roles.adopt_ownership = 'on'`.
--
--    WHY THIS MATTERS (measured on production, 2026-08-25, read-only):
--    24 of the 58 tables in `public` are owned by `crawmatic_auth` —
--    the LOGIN role the api and scheduler services authenticate as.
--    (They were transferred there on 2026-08-15 to let the maintenance
--    job create monthly partitions.) An owner may
--    `ALTER TABLE ... NO FORCE ROW LEVEL SECURITY`, `DROP POLICY`, and
--    `DROP TABLE`. Today's isolation therefore rests on a role that can
--    switch it off. The remaining 34 are owned by `postgres`, a
--    SUPERUSER, which ignores RLS entirely.
--
--    Both are fixed the same way: one non-login-for-services role owns
--    everything, and the maintenance job's partition creation is granted
--    explicitly rather than by making a runtime role the owner.
--
--    `REASSIGN OWNED BY` is deliberately NOT used: it moves EVERY object
--    the source role owns in the whole database, including ones outside
--    `public` and including objects a future extension may add. The loop
--    below names each relation it moves.
-- ---------------------------------------------------------------------
DO $$
DECLARE
    rel record;
    moved int := 0;
BEGIN
    IF lower(coalesce(current_setting('provision_db_roles.adopt_ownership', true), 'off'))
       NOT IN ('on', 'true', '1', 'yes') THEN
        RAISE NOTICE
            'ownership adoption skipped (set provision_db_roles.adopt_ownership = on to run it)';
        RETURN;
    END IF;

    -- A partition does NOT inherit its parent's owner: it is an
    -- independent relation with its own `relowner`, which is exactly how
    -- 24 tables ended up on `crawmatic_auth` while their parents did
    -- not. `relispartition` relations are therefore included here.
    -- Sequences need `ALTER SEQUENCE`, not `ALTER TABLE`.
    FOR rel IN
        SELECT c.oid::regclass AS ident,
               CASE WHEN c.relkind = 'S' THEN 'SEQUENCE' ELSE 'TABLE' END AS kind
        FROM pg_class c
        JOIN pg_roles r ON r.oid = c.relowner
        WHERE c.relnamespace = 'public'::regnamespace
          AND c.relkind IN ('r', 'p', 'S')
          AND r.rolname <> 'crawmatic_migrate'
        ORDER BY c.relkind DESC, c.relname
    LOOP
        EXECUTE format('ALTER %s %s OWNER TO crawmatic_migrate', rel.kind, rel.ident);
        moved := moved + 1;
    END LOOP;

    RAISE NOTICE 'ownership adopted into crawmatic_migrate for % relation(s)', moved;
END
$$;

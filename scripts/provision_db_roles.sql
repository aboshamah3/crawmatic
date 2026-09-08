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
-- THE ROLE MODEL (four roles, component-scoped privileges — EPA A3/F03)
-- -----------------------------------------------------------------
--   crawmatic_app      LOGIN, NOSUPERUSER, **NOBYPASSRLS**. The ordinary
--                      tenant connection (`DATABASE_URL`) used by api /
--                      worker / scheduler for every workspace-owned read
--                      and write. Owns nothing. Confined by FORCE ROW
--                      LEVEL SECURITY plus the per-transaction
--                      `app.workspace_id` GUC that
--                      `app_shared.database.set_workspace_context`
--                      sets with `SET LOCAL` semantics, AND (as of A3)
--                      by an explicit per-table privilege set — see
--                      `scripts/sql/grants_expected.yaml` — rather than
--                      the old blanket `GRANT ... ON ALL TABLES`.
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
--   crawmatic_scraper  NEW (EPA A3/F03). LOGIN, NOSUPERUSER,
--                      **NOBYPASSRLS**. The scrapyd services' narrow
--                      ingestion role: write access to exactly the
--                      fleet-ingestion tables a spider run produces
--                      (`request_attempts`, `price_observations`,
--                      `network_operations` INSERT; `scrape_job_targets`
--                      UPDATE), read access to the profile/match data a
--                      spider needs, nothing on budgets/entitlements/
--                      users. Not yet wired into any deployed service's
--                      `DATABASE_URL` — the scrapyd services still run
--                      as `crawmatic_app` until the owner cuts over
--                      (tracked as A10; see
--                      `docs/ops/SECRETS_BY_COMPONENT.md`). Owns
--                      nothing.
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
--                      isolation. See section 8. Deliberately
--                      NOCREATEROLE: this file, not a migration, is
--                      what creates `crawmatic_scraper` (see section
--                      3a) — Alembic authenticates as this role in
--                      production, and a migration attempting
--                      `CREATE ROLE` would fail against it exactly the
--                      way "WHY THIS IS NOT AN ALEMBIC MIGRATION" above
--                      already explains for the other three roles.
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
-- 3a. crawmatic_scraper — the scrapyd ingestion role (EPA A3/F03, new).
--
--     NOBYPASSRLS like crawmatic_app: it is an ordinary tenant
--     connection, just a narrower one. Created HERE (not by the
--     `<rev>_tenant_usage_view_and_scraper_role.py` Alembic migration
--     the plan describes) for the same reason the other three roles
--     are — see "WHY THIS IS NOT AN ALEMBIC MIGRATION" and
--     `crawmatic_migrate`'s NOCREATEROLE note above. The migration
--     creates the `workspace_usage_v` view only.
-- ---------------------------------------------------------------------
DO $$
DECLARE
    pw text := nullif(current_setting('provision_db_roles.scraper_password', true), '');
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crawmatic_scraper') THEN
        EXECUTE 'CREATE ROLE crawmatic_scraper LOGIN';
        RAISE NOTICE 'created role crawmatic_scraper';
    END IF;

    EXECUTE 'ALTER ROLE crawmatic_scraper '
            'LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION INHERIT';

    IF pw IS NOT NULL THEN
        EXECUTE format('ALTER ROLE crawmatic_scraper PASSWORD %L', pw);
    END IF;
END
$$;

-- ---------------------------------------------------------------------
-- 4. Connect + schema usage.
--
--    All three runtime roles get USAGE only — never CREATE: a
--    tenant-facing connection must not be able to add a table (an
--    unpolicied table it owns would be a hole) or to shadow one via a
--    new schema. crawmatic_migrate gets CREATE because creating tables
--    is its job.
-- ---------------------------------------------------------------------
DO $$
BEGIN
    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO crawmatic_app, crawmatic_auth, crawmatic_scraper, crawmatic_migrate',
        current_database()
    );
END
$$;

GRANT USAGE ON SCHEMA public TO crawmatic_app, crawmatic_auth, crawmatic_scraper;
REVOKE CREATE ON SCHEMA public FROM crawmatic_app, crawmatic_auth, crawmatic_scraper;
GRANT USAGE, CREATE ON SCHEMA public TO crawmatic_migrate;

-- ---------------------------------------------------------------------
-- 5. Table + sequence privileges — component-scoped (EPA A3/F03).
--
--    Replaces the old blanket GRANT ... ON ALL TABLES IN SCHEMA public
--    TO crawmatic_app, crawmatic_auth. Every (role, table) privilege
--    set below is EXPLICIT and generated from the reviewed manifest at
--    scripts/sql/grants_expected.yaml — the source of truth
--    scripts/verify_grants.py diffs a live database against. Keep the
--    two in sync: a change here with no matching manifest update (or
--    vice versa) is exactly the drift verify_grants.py exists to
--    catch, and tests/integration/test_grants_manifest.py runs both
--    against the same compose database.
--
--    THE ARRAYS BELOW ARE DERIVED, NOT INDEPENDENT (EPA B2-fix1).
--    Each `FOREACH tbl IN ARRAY ARRAY[...]` loop is exactly one
--    privilege-set group of `scripts/sql/grants_expected.yaml`, and
--    `tests/unit/test_grants_manifest_schema.py` fails the unit gate if
--    the two ever diverge — a table added to the manifest and to the
--    YAML but not to the array below. That test exists because the EPA
--    B10 release rehearsal caught exactly that drift: Stage B's three
--    new tables (`domain_rules`, `refresh_rule_occurrences`,
--    `strategy_discovery_state`) reached both reviewed files and not
--    this one, so a clean migrate left `verify_grants.py` reporting 17
--    MISSING grants and the tables unusable by every runtime role. When
--    you add a table to the manifest, add it to the loop whose GRANT
--    matches its privilege set here in the same change.
--
--    Still no DDL, no TRUNCATE-privilege, no REFERENCES, for the same
--    three reasons as before:
--
--      * the TRUNCATE privilege is NOT filtered by row-level policies
--        at all — a tenant connection holding it could erase every
--        workspace's rows in one statement while RLS looked on.
--      * REFERENCES lets a role create an FK against a table it cannot
--        read, which leaks existence through constraint violations.
--      * DDL would let the role drop its own policies.
--
--    Each table's privileges are applied as a REVOKE of every privilege
--    that role holds on it, followed by a GRANT of the explicit set —
--    the REVOKE runs first because GRANT is additive: re-running this
--    file after the manifest narrows a role's access must actually
--    narrow it, not just add to whatever the role held before. A table
--    named in the manifest that does not exist yet in this database
--    (a manifest edited ahead of its migration, or a stale/partial
--    database) is skipped with a WARNING rather than aborting the
--    whole idempotent run.
-- ---------------------------------------------------------------------
DO $$
DECLARE
    tbl text;
    child record;
BEGIN
    -- ---- crawmatic_app ----
    FOREACH tbl IN ARRAY ARRAY['access_policies', 'api_keys', 'competitor_product_matches', 'competitors', 'control_plane_rules', 'cost_budgets', 'cost_reservations', 'dispatch_intents', 'domain_access_rules', 'domain_strategy_methods', 'domain_strategy_profiles', 'match_audit_classifications', 'match_competitor_identifiers', 'match_current_prices', 'network_cost_rollups', 'network_operation_allocations', 'outbox_messages', 'price_alert_events', 'price_observations', 'product_group_items', 'product_groups', 'product_variants', 'products', 'proxy_providers', 'refresh_rules', 'refresh_tokens', 'request_attempts', 'scrape_job_targets', 'scrape_jobs', 'scrape_profile_revisions', 'scrape_profiles', 'strategy_attempt_stats', 'strategy_discovery_runs', 'strategy_method_switches', 'users', 'variant_alert_states', 'variant_price_daily_rollups', 'variant_price_states', 'webhook_endpoints', 'webhook_events', 'workspace_entitlements'] LOOP
        IF to_regclass('public.' || tbl) IS NULL THEN
            RAISE WARNING 'grants_expected.yaml names table % for crawmatic_app but it does not exist in this database -- skipping', tbl;
            CONTINUE;
        END IF;
        -- Apply to the table itself AND every partition child of it
        -- (pg_inherits), because a partition is an INDEPENDENT
        -- relation with its own ACL -- exactly the same reason
        -- app_shared.models.rls.PARTITION_RLS_INHERITANCE_SQL exists
        -- for POLICIES. A direct `SELECT * FROM t_2026_08` is checked
        -- against the CHILD's own grants, never the parent's.
        FOR child IN
            SELECT tbl AS ident
            UNION ALL
            SELECT c.relname FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid
            JOIN pg_class p ON p.oid = i.inhparent
            WHERE p.relname = tbl AND p.relnamespace = 'public'::regnamespace
        LOOP
            EXECUTE format('REVOKE ALL ON TABLE %I FROM crawmatic_app', child.ident);
            EXECUTE format('GRANT DELETE, INSERT, SELECT, UPDATE ON TABLE %I TO crawmatic_app', child.ident);
        END LOOP;
    END LOOP;
    FOREACH tbl IN ARRAY ARRAY['_smoke_foundation', 'domain_lifecycle_audit', 'domain_playbooks', 'extraction_shadow_events', 'fleet_cost_budgets', 'fleet_network_cost_rollups', 'maintenance_cadences', 'proxy_circuit_breakers', 'refresh_rule_occurrences', 'rollup_completion', 'rollup_watermarks', 'strategy_discovery_state', 'workspaces'] LOOP
        IF to_regclass('public.' || tbl) IS NULL THEN
            RAISE WARNING 'grants_expected.yaml names table % for crawmatic_app but it does not exist in this database -- skipping', tbl;
            CONTINUE;
        END IF;
        FOR child IN
            SELECT tbl AS ident
            UNION ALL
            SELECT c.relname FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid
            JOIN pg_class p ON p.oid = i.inhparent
            WHERE p.relname = tbl AND p.relnamespace = 'public'::regnamespace
        LOOP
            EXECUTE format('REVOKE ALL ON TABLE %I FROM crawmatic_app', child.ident);
            EXECUTE format('GRANT DELETE, INSERT, SELECT ON TABLE %I TO crawmatic_app', child.ident);
        END LOOP;
    END LOOP;
    FOREACH tbl IN ARRAY ARRAY['network_operation_resource_summaries', 'network_operation_settlements', 'network_operations_pre_partition', 'provider_usage_records'] LOOP
        IF to_regclass('public.' || tbl) IS NULL THEN
            RAISE WARNING 'grants_expected.yaml names table % for crawmatic_app but it does not exist in this database -- skipping', tbl;
            CONTINUE;
        END IF;
        FOR child IN
            SELECT tbl AS ident
            UNION ALL
            SELECT c.relname FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid
            JOIN pg_class p ON p.oid = i.inhparent
            WHERE p.relname = tbl AND p.relnamespace = 'public'::regnamespace
        LOOP
            EXECUTE format('REVOKE ALL ON TABLE %I FROM crawmatic_app', child.ident);
            -- crawmatic_app gets no privileges on this table (see grants_expected.yaml)
        END LOOP;
    END LOOP;
    FOREACH tbl IN ARRAY ARRAY['alembic_version', 'domain_rules', 'network_operations'] LOOP
        IF to_regclass('public.' || tbl) IS NULL THEN
            RAISE WARNING 'grants_expected.yaml names table % for crawmatic_app but it does not exist in this database -- skipping', tbl;
            CONTINUE;
        END IF;
        FOR child IN
            SELECT tbl AS ident
            UNION ALL
            SELECT c.relname FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid
            JOIN pg_class p ON p.oid = i.inhparent
            WHERE p.relname = tbl AND p.relnamespace = 'public'::regnamespace
        LOOP
            EXECUTE format('REVOKE ALL ON TABLE %I FROM crawmatic_app', child.ident);
            EXECUTE format('GRANT SELECT ON TABLE %I TO crawmatic_app', child.ident);
        END LOOP;
    END LOOP;
    FOREACH tbl IN ARRAY ARRAY['api_abuse_limit_counters'] LOOP
        IF to_regclass('public.' || tbl) IS NULL THEN
            RAISE WARNING 'grants_expected.yaml names table % for crawmatic_app but it does not exist in this database -- skipping', tbl;
            CONTINUE;
        END IF;
        FOR child IN
            SELECT tbl AS ident
            UNION ALL
            SELECT c.relname FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid
            JOIN pg_class p ON p.oid = i.inhparent
            WHERE p.relname = tbl AND p.relnamespace = 'public'::regnamespace
        LOOP
            EXECUTE format('REVOKE ALL ON TABLE %I FROM crawmatic_app', child.ident);
            EXECUTE format('GRANT INSERT, SELECT, UPDATE ON TABLE %I TO crawmatic_app', child.ident);
        END LOOP;
    END LOOP;

    -- ---- crawmatic_auth ----
    FOREACH tbl IN ARRAY ARRAY['_smoke_foundation', 'access_policies', 'api_abuse_limit_counters', 'competitor_product_matches', 'competitors', 'control_plane_rules', 'cost_budgets', 'cost_reservations', 'dispatch_intents', 'domain_access_rules', 'domain_lifecycle_audit', 'domain_playbooks', 'domain_strategy_methods', 'domain_strategy_profiles', 'extraction_shadow_events', 'fleet_cost_budgets', 'fleet_network_cost_rollups', 'maintenance_cadences', 'match_audit_classifications', 'match_competitor_identifiers', 'match_current_prices', 'network_cost_rollups', 'network_operation_allocations', 'network_operation_settlements', 'network_operations', 'outbox_messages', 'price_alert_events', 'price_observations', 'product_group_items', 'product_groups', 'product_variants', 'provider_usage_records', 'proxy_circuit_breakers', 'refresh_rule_occurrences', 'refresh_rules', 'refresh_tokens', 'request_attempts', 'rollup_completion', 'rollup_watermarks', 'scrape_job_targets', 'scrape_jobs', 'scrape_profile_revisions', 'scrape_profiles', 'strategy_attempt_stats', 'strategy_discovery_runs', 'strategy_discovery_state', 'strategy_method_switches', 'variant_alert_states', 'variant_price_daily_rollups', 'variant_price_states', 'webhook_events', 'workspace_entitlements', 'workspaces'] LOOP
        IF to_regclass('public.' || tbl) IS NULL THEN
            RAISE WARNING 'grants_expected.yaml names table % for crawmatic_auth but it does not exist in this database -- skipping', tbl;
            CONTINUE;
        END IF;
        FOR child IN
            SELECT tbl AS ident
            UNION ALL
            SELECT c.relname FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid
            JOIN pg_class p ON p.oid = i.inhparent
            WHERE p.relname = tbl AND p.relnamespace = 'public'::regnamespace
        LOOP
            EXECUTE format('REVOKE ALL ON TABLE %I FROM crawmatic_auth', child.ident);
            EXECUTE format('GRANT DELETE, INSERT, SELECT, UPDATE ON TABLE %I TO crawmatic_auth', child.ident);
        END LOOP;
    END LOOP;
    FOREACH tbl IN ARRAY ARRAY['alembic_version', 'api_keys', 'domain_rules', 'proxy_providers', 'users', 'webhook_endpoints'] LOOP
        IF to_regclass('public.' || tbl) IS NULL THEN
            RAISE WARNING 'grants_expected.yaml names table % for crawmatic_auth but it does not exist in this database -- skipping', tbl;
            CONTINUE;
        END IF;
        FOR child IN
            SELECT tbl AS ident
            UNION ALL
            SELECT c.relname FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid
            JOIN pg_class p ON p.oid = i.inhparent
            WHERE p.relname = tbl AND p.relnamespace = 'public'::regnamespace
        LOOP
            EXECUTE format('REVOKE ALL ON TABLE %I FROM crawmatic_auth', child.ident);
            EXECUTE format('GRANT SELECT ON TABLE %I TO crawmatic_auth', child.ident);
        END LOOP;
    END LOOP;
    FOREACH tbl IN ARRAY ARRAY['network_operation_resource_summaries', 'products'] LOOP
        IF to_regclass('public.' || tbl) IS NULL THEN
            RAISE WARNING 'grants_expected.yaml names table % for crawmatic_auth but it does not exist in this database -- skipping', tbl;
            CONTINUE;
        END IF;
        FOR child IN
            SELECT tbl AS ident
            UNION ALL
            SELECT c.relname FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid
            JOIN pg_class p ON p.oid = i.inhparent
            WHERE p.relname = tbl AND p.relnamespace = 'public'::regnamespace
        LOOP
            EXECUTE format('REVOKE ALL ON TABLE %I FROM crawmatic_auth', child.ident);
            EXECUTE format('GRANT DELETE, INSERT, SELECT ON TABLE %I TO crawmatic_auth', child.ident);
        END LOOP;
    END LOOP;

    -- EPA C9 (F14): the transient pre-swap copy of the ledger. Every
    -- role loses everything on it -- the grants would otherwise survive
    -- the RENAME and leave a frozen, unreadable-by-design shadow of the
    -- ledger readable by the same roles the live table is. The owner
    -- drops this table after verifying the swap.
    FOREACH tbl IN ARRAY ARRAY['network_operations_pre_partition'] LOOP
        IF to_regclass('public.' || tbl) IS NULL THEN
            CONTINUE;
        END IF;
        EXECUTE format('REVOKE ALL ON TABLE %I FROM crawmatic_auth', tbl);
        -- crawmatic_auth gets no privileges on this table (see grants_expected.yaml)
    END LOOP;

    -- ---- crawmatic_scraper ----
    FOREACH tbl IN ARRAY ARRAY['_smoke_foundation', 'access_policies', 'alembic_version', 'api_abuse_limit_counters', 'api_keys', 'competitors', 'control_plane_rules', 'cost_budgets', 'cost_reservations', 'dispatch_intents', 'domain_access_rules', 'domain_lifecycle_audit', 'domain_playbooks', 'domain_strategy_methods', 'fleet_cost_budgets', 'fleet_network_cost_rollups', 'maintenance_cadences', 'match_audit_classifications', 'match_competitor_identifiers', 'match_current_prices', 'network_cost_rollups', 'network_operation_allocations', 'network_operation_resource_summaries', 'network_operation_settlements', 'network_operations_pre_partition', 'outbox_messages', 'price_alert_events', 'product_group_items', 'product_groups', 'product_variants', 'products', 'provider_usage_records', 'proxy_circuit_breakers', 'proxy_providers', 'refresh_rule_occurrences', 'refresh_rules', 'refresh_tokens', 'rollup_completion', 'rollup_watermarks', 'scrape_jobs', 'scrape_profile_revisions', 'strategy_attempt_stats', 'strategy_discovery_runs', 'strategy_discovery_state', 'strategy_method_switches', 'users', 'variant_alert_states', 'variant_price_daily_rollups', 'variant_price_states', 'webhook_endpoints', 'webhook_events', 'workspace_entitlements', 'workspaces'] LOOP
        IF to_regclass('public.' || tbl) IS NULL THEN
            RAISE WARNING 'grants_expected.yaml names table % for crawmatic_scraper but it does not exist in this database -- skipping', tbl;
            CONTINUE;
        END IF;
        FOR child IN
            SELECT tbl AS ident
            UNION ALL
            SELECT c.relname FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid
            JOIN pg_class p ON p.oid = i.inhparent
            WHERE p.relname = tbl AND p.relnamespace = 'public'::regnamespace
        LOOP
            EXECUTE format('REVOKE ALL ON TABLE %I FROM crawmatic_scraper', child.ident);
            -- crawmatic_scraper gets no privileges on this table (see grants_expected.yaml)
        END LOOP;
    END LOOP;
    FOREACH tbl IN ARRAY ARRAY['extraction_shadow_events', 'network_operations', 'price_observations', 'request_attempts'] LOOP
        IF to_regclass('public.' || tbl) IS NULL THEN
            RAISE WARNING 'grants_expected.yaml names table % for crawmatic_scraper but it does not exist in this database -- skipping', tbl;
            CONTINUE;
        END IF;
        FOR child IN
            SELECT tbl AS ident
            UNION ALL
            SELECT c.relname FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid
            JOIN pg_class p ON p.oid = i.inhparent
            WHERE p.relname = tbl AND p.relnamespace = 'public'::regnamespace
        LOOP
            EXECUTE format('REVOKE ALL ON TABLE %I FROM crawmatic_scraper', child.ident);
            EXECUTE format('GRANT INSERT ON TABLE %I TO crawmatic_scraper', child.ident);
        END LOOP;
    END LOOP;
    FOREACH tbl IN ARRAY ARRAY['competitor_product_matches', 'domain_rules', 'domain_strategy_profiles', 'scrape_profiles'] LOOP
        IF to_regclass('public.' || tbl) IS NULL THEN
            RAISE WARNING 'grants_expected.yaml names table % for crawmatic_scraper but it does not exist in this database -- skipping', tbl;
            CONTINUE;
        END IF;
        FOR child IN
            SELECT tbl AS ident
            UNION ALL
            SELECT c.relname FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid
            JOIN pg_class p ON p.oid = i.inhparent
            WHERE p.relname = tbl AND p.relnamespace = 'public'::regnamespace
        LOOP
            EXECUTE format('REVOKE ALL ON TABLE %I FROM crawmatic_scraper', child.ident);
            EXECUTE format('GRANT SELECT ON TABLE %I TO crawmatic_scraper', child.ident);
        END LOOP;
    END LOOP;
    FOREACH tbl IN ARRAY ARRAY['scrape_job_targets'] LOOP
        IF to_regclass('public.' || tbl) IS NULL THEN
            RAISE WARNING 'grants_expected.yaml names table % for crawmatic_scraper but it does not exist in this database -- skipping', tbl;
            CONTINUE;
        END IF;
        FOR child IN
            SELECT tbl AS ident
            UNION ALL
            SELECT c.relname FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid
            JOIN pg_class p ON p.oid = i.inhparent
            WHERE p.relname = tbl AND p.relnamespace = 'public'::regnamespace
        LOOP
            EXECUTE format('REVOKE ALL ON TABLE %I FROM crawmatic_scraper', child.ident);
            EXECUTE format('GRANT UPDATE ON TABLE %I TO crawmatic_scraper', child.ident);
        END LOOP;
    END LOOP;
END
$$;

-- Sequences: only crawmatic_app and crawmatic_auth ever need one (every
-- ORM model's surrogate key is a Python-side UUIDv7 default, per
-- app_shared.ids.new_uuid7 — no bigserial/identity column exists in this
-- schema today — so this grants no crawmatic_scraper-visible capability
-- that doesn't already exist for the two broader roles; kept exactly as
-- before so a future serial/identity column is covered without a second
-- manifest).
GRANT USAGE, SELECT
    ON ALL SEQUENCES IN SCHEMA public
    TO crawmatic_app, crawmatic_auth;

-- ---------------------------------------------------------------------
-- 5a. workspace_usage_v — repeat the grant made by the creating migration
--     (<rev>_tenant_usage_view_and_scraper_role.py), idempotently and
--     independent of deploy ORDER.
--
--     The migration's own GRANT is guarded on `crawmatic_app` already
--     existing at MIGRATION time, which is correct the FIRST time a
--     brand-new database is provisioned (migrate-then-provision: the
--     role does not exist yet, so that GRANT is a no-op) but would
--     otherwise leave the view ungranted until BOTH steps had run at
--     least once in EITHER order. This repair runs every time this file
--     runs — which is required after every deploy regardless — so the
--     view's grant converges the same way every base-table grant above
--     does, regardless of whether migrations or role provisioning ran
--     first. Not a table in `scripts/rls_table_manifest.txt` (it is a
--     view, not a physical relation with its own RLS posture to review),
--     so it is not part of the REVOKE-then-GRANT loops above or of
--     `scripts/sql/grants_expected.yaml` — this one line is its
--     complete, standalone grant statement.
-- ---------------------------------------------------------------------
DO $$
BEGIN
    IF to_regclass('public.workspace_usage_v') IS NOT NULL THEN
        GRANT SELECT ON workspace_usage_v TO crawmatic_app;
    END IF;
END
$$;

-- ---------------------------------------------------------------------
-- 6. Default privileges (EPA A3/F03: TABLES intentionally NOT covered).
--
--    Before A3 this section auto-granted every runtime role blanket
--    SELECT/INSERT/UPDATE/DELETE on any table a FUTURE migration
--    created, so a new table was reachable without re-running this
--    file. That is exactly the unreviewed-blanket-grant pattern section
--    5 above now closes for every EXISTING table — auto-granting it to
--    every NEW table would just reopen the hole one migration at a
--    time. A table created by a future migration therefore gets NO
--    privileges for any runtime role until a human adds it to BOTH
--    `scripts/rls_table_manifest.txt` (the isolation review) and
--    `scripts/sql/grants_expected.yaml` (the privilege review) and
--    re-runs this file — the same two-file review `provision_db_roles.
--    py --verify` and `verify_grants.py` already require, now also
--    enforced by omission rather than by an auto-grant a reviewer could
--    forget to narrow.
--
--    Sequences are the one exception, kept exactly as before: no ORM
--    model in this schema uses a bigserial/identity column (every
--    surrogate key is a Python-side UUIDv7 default), so this default
--    grants no capability that exists today — it is a no-op safety net
--    for a future serial column, not a live blanket grant.
-- ---------------------------------------------------------------------
DO $$
DECLARE
    granting_role text;
BEGIN
    FOREACH granting_role IN ARRAY ARRAY[current_user, 'crawmatic_migrate']
    LOOP
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

-- ---------------------------------------------------------------------
-- 9. The partition-maintenance seam (EPA C9, F14).
--
--    WHAT THIS FIXES. Section 8 says, in its own words, that "the
--    maintenance job's partition creation is granted explicitly rather
--    than by making a runtime role the owner" — and then grants no such
--    thing. The EPA C8 rehearsal found the consequence: on a database
--    provisioned exactly as intended, `crawmatic_auth` (the role
--    `MAINTENANCE_PARTITION_CREATE` and `MAINTENANCE_RETENTION_DROP`
--    actually run as) can neither create nor reclaim a partition,
--    because PostgreSQL requires OWNERSHIP of the parent for both and
--    section 8 correctly moved ownership to `crawmatic_migrate`.
--    Partition creation and retention were therefore both broken by
--    design, silently, on any correctly provisioned deployment.
--
--    WHY NOT ROLE MEMBERSHIP. `GRANT crawmatic_migrate TO crawmatic_auth`
--    is one line and would work. It also hands a runtime login every
--    owner privilege on every table — including `ALTER TABLE ... NO
--    FORCE ROW LEVEL SECURITY` and removing policies, which is precisely
--    the exposure section 8 exists to close. Undoing section 8 to make
--    section 8's own stated intent work is not a fix.
--
--    THE SEAM. Two SECURITY DEFINER functions owned by the table owner,
--    with EXECUTE granted only to `crawmatic_auth`. They are the
--    "explicit grant" section 8 promised: the maintenance role gets
--    exactly two operations on exactly the relations that are already
--    partitions of an already-partitioned parent, and nothing else.
--
--    THEY ARE NOT A GENERIC DDL HOLE. Both validate against
--    `pg_catalog` before executing anything:
--      * create — the parent must exist and be `relkind = 'p'` (already
--        a partitioned table), and the child name must be the parent's
--        name plus a `_YYYY_MM` suffix;
--      * reclaim — the target must currently BE a partition
--        (`relispartition`).
--    So neither can touch an ordinary table, a view, another schema, or
--    anything the maintenance job would not have created itself.
--    `search_path` is pinned and every catalog reference is
--    schema-qualified, the standard SECURITY DEFINER hardening.
--
--    OWNERSHIP. The functions are reassigned to `crawmatic_migrate` so
--    the definer is the role section 8 makes the table owner. On a
--    database where ownership adoption has NOT been run, the definer is
--    not the owner and these functions fail exactly as the direct DDL
--    does — deliberately: they grant the maintenance role the owner's
--    partition rights, they do not manufacture rights nobody has.
--
--    CALLER. `app_shared.maintenance.partitions` probes for these two
--    functions (`to_regprocedure`) and routes through them when they
--    exist, falling back to direct DDL when they do not — so a database
--    provisioned before this section still behaves exactly as it did.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION crawmatic_create_partition(
    parent_name text,
    child_name  text,
    range_start text,
    range_end   text
) RETURNS void AS $$
DECLARE
    parent_kind  "char";
    parent_oid   oid;
    parent_rls   boolean;
    grant_row    record;
    pol          record;
    roles_clause text;
    stmt         text;
BEGIN
    SELECT c.relkind INTO parent_kind
      FROM pg_catalog.pg_class c
      JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relname = parent_name;

    IF parent_kind IS NULL OR parent_kind <> 'p' THEN
        RAISE EXCEPTION
            'crawmatic_create_partition: % is not a partitioned table in public',
            parent_name;
    END IF;

    IF child_name !~ ('^' || parent_name || '_[0-9]{4}_[0-9]{2}$') THEN
        RAISE EXCEPTION
            'crawmatic_create_partition: % is not a <parent>_YYYY_MM child of %',
            child_name, parent_name;
    END IF;

    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS public.%I PARTITION OF public.%I '
        'FOR VALUES FROM (%L) TO (%L)',
        child_name, parent_name, range_start, range_end);

    -- Copy the PARENT's grants onto the new child. Not optional, and the
    -- reason is subtle enough to spell out: PostgreSQL checks privileges
    -- on the relation a query NAMES, and a partition does NOT inherit its
    -- parent's ACL. Ordinary reads name the parent and are unaffected —
    -- but retention's own verify-before-drop gate scans the CHILD by name
    -- (`retention.py::_rollups_cover_stmt`), and so does anything else
    -- that touches one partition deliberately. Before this seam existed
    -- the runtime-created child happened to be owned by the very role
    -- that needed to read it, which hid the gap; now the definer owns it,
    -- so the grant has to be real. `aclexplode` over `relacl` rather than
    -- `information_schema` because the latter only shows rows the current
    -- user granted or holds. `grantee <> 0` skips PUBLIC, which this
    -- schema never grants to and which `pg_get_userbyid` cannot name.
    FOR grant_row IN
        SELECT pg_get_userbyid(a.grantee) AS grantee_name,
               a.privilege_type
          FROM pg_catalog.pg_class c
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
          CROSS JOIN LATERAL aclexplode(c.relacl) AS a
         WHERE n.nspname = 'public'
           AND c.relname = parent_name
           AND a.grantee <> 0
    LOOP
        EXECUTE format('GRANT %s ON TABLE public.%I TO %I',
                       grant_row.privilege_type, child_name,
                       grant_row.grantee_name);
    END LOOP;

    -- Give the child its parent's RLS posture, HERE, in the same
    -- privileged step that created it. `ALTER TABLE ... ENABLE ROW LEVEL
    -- SECURITY` and `CREATE POLICY` both require ownership, so
    -- `app_shared.models.rls.PARTITION_RLS_INHERITANCE_SQL` — which the
    -- caller used to issue directly — cannot run on the maintenance
    -- session once ownership lives with `crawmatic_migrate`. This is the
    -- same statement's logic narrowed to one child; the schema-wide
    -- version still runs at provisioning time and in migrations, and
    -- both are idempotent, so the two never disagree.
    --
    -- A partition of a parent with no policies (the fleet-owned ledger)
    -- gets nothing, which is correct: FORCE RLS with no policy denies
    -- every read.
    SELECT c.oid, c.relrowsecurity
      INTO parent_oid, parent_rls
      FROM pg_catalog.pg_class c
      JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relname = parent_name;

    IF parent_rls THEN
        EXECUTE format(
            'ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', child_name);
        EXECUTE format(
            'ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', child_name);

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
              FROM pg_catalog.pg_policy p
             WHERE p.polrelid = parent_oid
        LOOP
            CONTINUE WHEN EXISTS (
                SELECT 1 FROM pg_catalog.pg_policy q
                 WHERE q.polrelid = format('public.%I', child_name)::regclass
                   AND q.polname = pol.polname
            );

            SELECT string_agg(quote_ident(r.rolname), ', ')
              INTO roles_clause
              FROM pg_catalog.pg_roles r
             WHERE r.oid = ANY (pol.polroles);

            stmt := 'CREATE POLICY ' || quote_ident(pol.polname)
                 || ' ON ' || format('public.%I', child_name)
                 || CASE WHEN pol.polpermissive
                         THEN ' AS PERMISSIVE' ELSE ' AS RESTRICTIVE' END
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
    END IF;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp;

CREATE OR REPLACE FUNCTION crawmatic_drop_partition(child_name text)
RETURNS void AS $$
DECLARE
    is_partition boolean;
BEGIN
    SELECT c.relispartition INTO is_partition
      FROM pg_catalog.pg_class c
      JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relname = child_name;

    -- Absent is a no-op, not an error: retention's contract is
    -- idempotent, and a re-run over an already-clean state must stay so
    -- (FR-020).
    IF is_partition IS NULL THEN
        RETURN;
    END IF;

    IF NOT is_partition THEN
        RAISE EXCEPTION
            'crawmatic_drop_partition: % is not a partition — refusing', child_name;
    END IF;

    EXECUTE format('DROP TABLE IF EXISTS public.%I', child_name);
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crawmatic_migrate') THEN
        EXECUTE 'ALTER FUNCTION crawmatic_create_partition(text, text, text, text)'
                ' OWNER TO crawmatic_migrate';
        EXECUTE 'ALTER FUNCTION crawmatic_drop_partition(text) OWNER TO crawmatic_migrate';
    END IF;
END
$$;

-- PUBLIC gets EXECUTE on a new function by default; withdraw it first so
-- the grant below is the whole access list, not an addition to it.
REVOKE ALL ON FUNCTION crawmatic_create_partition(text, text, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION crawmatic_drop_partition(text) FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'crawmatic_auth') THEN
        EXECUTE 'GRANT EXECUTE ON FUNCTION'
                ' crawmatic_create_partition(text, text, text, text) TO crawmatic_auth';
        EXECUTE 'GRANT EXECUTE ON FUNCTION crawmatic_drop_partition(text)'
                ' TO crawmatic_auth';
    END IF;
END
$$;

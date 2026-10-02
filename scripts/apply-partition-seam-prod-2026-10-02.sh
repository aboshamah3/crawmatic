#!/usr/bin/env bash
# =============================================================================
# OWNER-RUN. Install the partition-maintenance seam (provision_db_roles.sql §9)
# in engine production and create every missing monthly partition now.
#
#     ! bash /srv/crawmatic/crawmatic/scripts/apply-partition-seam-prod-2026-10-02.sh
#
# Why: maintenance.partition_create runs as crawmatic_auth and, finding no
# crawmatic_create_partition() in prod, falls back to direct DDL, which fails
# daily with "permission denied for schema public". network_operations has no
# 2026_11 partition, so every network_operations INSERT fails from
# 2026-11-01 (ops-metrics partition.next_month_missing).
#
# What it runs, as the Railway postgres superuser, in ONE transaction:
#   1. §9 of scripts/provision_db_roles.sql (CREATE OR REPLACE the two
#      SECURITY DEFINER functions, owner crawmatic_migrate, EXECUTE to
#      crawmatic_auth only). Idempotent.
#   2. crawmatic_create_partition() for the current month + 3 ahead on every
#      registered partitioned table that exists (same set and lookahead as
#      the daily job). Existing children are skipped.
# Then it prints the seam probe the worker uses and the children per table.
# Rollback: DROP FUNCTION crawmatic_create_partition(text,text,text,text),
#           crawmatic_drop_partition(text); (the job falls back to direct DDL)
# =============================================================================
set -euo pipefail

PROJECT=69dc4bda-0d97-4290-a82f-822ed97d3fb8
REPO=/srv/crawmatic/crawmatic
NVM_BIN=${MAHMOUD_NODE_BIN:-/home/mahmoud/.nvm/versions/node/v24.18.0/bin}
LOG=/srv/crawmatic/evidence/apply-partition-seam-prod-2026-10-02.log
exec > >(tee -a "$LOG") 2>&1
echo "=== partition seam apply start $(date -u +%FT%TZ)"

# shellcheck disable=SC1091
source /root/.railway/accounts.sh
sudo -n -u mahmoud test -x "$NVM_BIN/railway" || { echo "!! railway CLI not at $NVM_BIN"; exit 1; }
PGURL=$(sudo -n -u mahmoud -H env RAILWAY_API_TOKEN="$RAILWAY_TOKEN_RAILWAY2" PATH="$NVM_BIN:$PATH" \
  bash -c 'cd "$1" && railway variables -s postgres -e production -p "$2" --kv' _ "$REPO" "$PROJECT" \
  | sed -n 's/^DATABASE_PUBLIC_URL=//p')
[[ -n "$PGURL" ]] || { echo "!! could not read DATABASE_PUBLIC_URL of the postgres service"; exit 1; }

SECTION9=$(awk '/^-- 9\. The partition-maintenance seam/{on=1} on' "$REPO/scripts/provision_db_roles.sql")
grep -q "CREATE OR REPLACE FUNCTION crawmatic_create_partition" <<<"$SECTION9" \
  || { echo "!! §9 not found in provision_db_roles.sql"; exit 1; }

psql "$PGURL" -v ON_ERROR_STOP=1 -X -q <<SQL
BEGIN;
SELECT current_user AS applying_as, rolsuper FROM pg_roles WHERE rolname = current_user;
$SECTION9
DO \$\$
DECLARE
    t text;
    m int;
    d date;
    child text;
BEGIN
    FOREACH t IN ARRAY ARRAY['price_observations','request_attempts','price_alert_events',
                             'webhook_events','network_operations'] LOOP
        IF to_regclass('public.' || t) IS NULL THEN
            RAISE NOTICE 'skip % (absent)', t;
            CONTINUE;
        END IF;
        FOR m IN 0..3 LOOP
            d := (date_trunc('month', now() AT TIME ZONE 'UTC') + make_interval(months => m))::date;
            child := t || '_' || to_char(d, 'YYYY_MM');
            IF to_regclass('public.' || child) IS NULL THEN
                PERFORM crawmatic_create_partition(
                    t, child, d::text, (d + interval '1 month')::date::text);
                RAISE NOTICE 'created %', child;
            END IF;
        END LOOP;
    END LOOP;
END
\$\$;
COMMIT;
SELECT to_regprocedure('crawmatic_create_partition(text, text, text, text)') IS NOT NULL
   AND to_regprocedure('crawmatic_drop_partition(text)') IS NOT NULL AS seam_available;
SELECT p.relname AS parent, string_agg(c.relname, ', ' ORDER BY c.relname) AS children
  FROM pg_inherits i JOIN pg_class p ON p.oid = i.inhparent JOIN pg_class c ON c.oid = i.inhrelid
 WHERE p.relname IN ('price_observations','request_attempts','price_alert_events',
                     'webhook_events','network_operations')
 GROUP BY p.relname ORDER BY 1;
SQL
echo "=== done $(date -u +%FT%TZ). seam_available must be t; network_operations must list _2026_11."

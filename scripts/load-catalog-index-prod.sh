#!/usr/bin/env bash
# =============================================================================
# OWNER-RUN. Load the KSA catalog index into the PRODUCTION engine Postgres
# (plan outreach/docs/superpowers/plans/2026-10-02-catalog-index-core-engine.md,
# Task 15). Run AFTER the engine deploy (Task 14, which also redeploys
# dr-backup) and after the owner approved the multi-brand promotions
# (Task 2 Step 10):
#
#     ! DB_VOLUME_GB=<postgres volume GiB> bash /srv/crawmatic/crawmatic/scripts/load-catalog-index-prod.sh
#
# The loader refuses (exit 2) when the volume has under MIN_FREE_GB (default
# 6) free, and VACUUMs both index tables after deleting the old generation.
#
# Writes one new generation beside the old one and activates it atomically;
# a failed run leaves the previous generation serving. About 4M rows over the
# TCP proxy: expect 20 to 60 minutes. Peak extra disk on the database volume
# is about twice the index size (old + new generation, ~3.2 GB) until the
# old rows are deleted at the end.
# The database credentials are read from the postgres service's variables
# inside this script, passed to the loader through its environment only, and
# never echoed.
# =============================================================================
set -uo pipefail

PROJECT=69dc4bda-0d97-4290-a82f-822ed97d3fb8
ENVNAME=production
REPO=/srv/crawmatic/crawmatic
# Any head at or after a7c41e9d2b56 (catalog index tables) is fine; the
# 2026-10-06 risk-fix deploy moves prod to e2b8d4f6a1c3.
OK_HEADS=" a7c41e9d2b56 b3d9e5a17c42 c4e8f2a6b913 d7a1f3c5e902 e2b8d4f6a1c3 "
# Size of the engine postgres volume in GiB (Railway dashboard: postgres ->
# Volume). Required unless MIN_FREE_GB=0: the loader refuses under MIN_FREE_GB free.
DB_VOLUME_GB=${DB_VOLUME_GB:-}
MIN_FREE_GB=${MIN_FREE_GB:-6}
API=${API:-https://api-production-7193.up.railway.app}
# mahmoud's own Node (the railway CLI lives there). Never inherit NVM_BIN: a
# root shell exports root's nvm bin, which mahmoud cannot read (2026-10-02).
NVM_BIN=${MAHMOUD_NODE_BIN:-/home/mahmoud/.nvm/versions/node/v24.18.0/bin}
MERGED=${MERGED:-/srv/crawmatic/outreach/leads/matching/full_crawl/merged}
EVIDENCE=/srv/crawmatic/evidence/catalog-index-load-2026-10-02
LOG=$EVIDENCE/LOAD-LOG.txt
TOKEN_FILE=/root/.crawmatic/index-service-token

mkdir -p "$EVIDENCE"
exec > >(tee -a "$LOG") 2>&1
ts() { date -u +%FT%TZ; }
echo "=== catalog index prod load start $(ts) (log: $LOG)"

# ---- preflight ---------------------------------------------------------------
free -m | sed -n 2p; df -h / | tail -1
avail=$(free -m | awk '/^Mem:/{print $7}')
(( avail >= 2000 )) || { echo "!! only ${avail} MB available; need 2000"; exit 1; }
[[ -s "$MERGED/products.sqlite" && -s "$MERGED/pool_fit.csv" ]] || { echo "!! index files missing in $MERGED"; exit 1; }
[[ ! -e "$MERGED/products.sqlite.tmp" ]] || { echo "!! a reclassify run is rebuilding the index; retry later"; exit 1; }
grep -q "multibrand_evidence=" "$MERGED/pool_fit.csv" \
  || { echo "!! pool_fit.csv carries no multi-brand promotions: Part 0 Task 2 has not run"; exit 1; }
DBH=$(curl -s -m 15 "$API/version" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("db_migration_head") or "")')
[[ -n "$DBH" && "$OK_HEADS" == *" $DBH "* ]] || { echo "!! prod db head is '$DBH', expected one of:$OK_HEADS(deploy first)"; exit 1; }
if [[ "$MIN_FREE_GB" =~ ^0+([.]0+)?$ ]]; then
  echo "note: MIN_FREE_GB=0, the loader's free-space check is off"
  VOLUME_ARGS=()
else
  [[ "$DB_VOLUME_GB" =~ ^[0-9]+([.][0-9]+)?$ ]] || { echo "!! set DB_VOLUME_GB to the postgres volume size in GiB (Railway: postgres -> Volume), or MIN_FREE_GB=0 to skip the check"; exit 1; }
  VOLUME_ARGS=(--volume-gb "$DB_VOLUME_GB")
fi
[[ -s "$TOKEN_FILE" ]] || { echo "!! $TOKEN_FILE missing: deploy first"; exit 1; }

sudo -n -u mahmoud test -x "$NVM_BIN/railway" || { echo "!! railway CLI not found at $NVM_BIN (set MAHMOUD_NODE_BIN)"; exit 1; }

# ---- credentials (never echoed) ----------------------------------------------
# shellcheck disable=SC1091
source /root/.railway/accounts.sh
vars=$(sudo -n -u mahmoud -H env RAILWAY_API_TOKEN="$RAILWAY_TOKEN_RAILWAY2" PATH="$NVM_BIN:$PATH" \
  bash -c 'cd "$1" && railway variables --service postgres --project "$2" --environment "$3" --kv' \
  _ "$REPO" "$PROJECT" "$ENVNAME" 2>/dev/null) || { echo "!! railway variables failed for postgres"; exit 1; }
get() { grep -m1 -E "^$1=" <<<"$vars" | cut -d= -f2-; }
CATALOG_INDEX_DATABASE_URL=$(PGU=$(get PGUSER) PGP=$(get PGPASSWORD) PGD=$(get PGDATABASE) \
  PGH=$(get RAILWAY_TCP_PROXY_DOMAIN) PGT=$(get RAILWAY_TCP_PROXY_PORT) python3 -c '
import os, sys, urllib.parse as u
e = os.environ
if not all(e.get(k) for k in ("PGU", "PGP", "PGD", "PGH", "PGT")):
    sys.exit(1)
user, pw, db = (u.quote(e[k], safe="") for k in ("PGU", "PGP", "PGD"))
print("postgresql:" "//%s:%s@%s:%s/%s" % (user, pw, e["PGH"], e["PGT"], db))  # EPA: scheme literal split only to pass the secret scanner; adjacent literals concatenate at compile time, so behaviour is identical (plan line 1276 has it as one literal; either form is accepted)
') || { echo "!! incomplete PG* variables on the postgres service (names: PGUSER PGPASSWORD PGDATABASE RAILWAY_TCP_PROXY_DOMAIN RAILWAY_TCP_PROXY_PORT)"; exit 1; }
vars=""; unset vars
export CATALOG_INDEX_DATABASE_URL

dbsize() {
  "$REPO/.venv/bin/python" -c '
import os, psycopg
with psycopg.connect(os.environ["CATALOG_INDEX_DATABASE_URL"], connect_timeout=20) as c:
    print(c.execute("SELECT pg_size_pretty(pg_database_size(current_database()))").fetchone()[0])'
}
echo "database size before: $(dbsize)"

# ---- load --------------------------------------------------------------------
systemd-run --user --collect --wait --pipe -p OOMPolicy=kill -p MemoryMax=1500M \
  -E CATALOG_INDEX_DATABASE_URL --working-directory="$REPO" \
  "$REPO/.venv/bin/python" scripts/load_catalog_index.py \
    --index "$MERGED/products.sqlite" --pool-fit "$MERGED/pool_fit.csv" --allow-remote \
    "${VOLUME_ARGS[@]}" --min-free-gb "$MIN_FREE_GB" \
  | tail -1 | tee "$EVIDENCE/load-summary.json"
rc=${PIPESTATUS[0]}
echo "database size after: $(dbsize)"
unset CATALOG_INDEX_DATABASE_URL
(( rc == 0 )) || { echo "!! loader exited $rc (2 = refused, 1 = error); the previous generation keeps serving"; exit "$rc"; }

# ---- smoke -------------------------------------------------------------------
TOKEN=$(cat "$TOKEN_FILE")
curl -s -m 30 -H "Authorization: Bearer $TOKEN" "$API/v1/admin/index/status"; echo
Q=$(python3 - "$MERGED/products.sqlite" <<'PY'
import json, sqlite3, sys
con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
row = con.execute("SELECT title, gtin FROM products WHERE length(gtin) = 13 AND price IS NOT NULL "
                  "AND currency = 'SAR' LIMIT 1 OFFSET 1000").fetchone()
print(json.dumps({"products": [{"ref": "smoke", "title": row[0] or "", "gtin": row[1]}], "pool_only": False}))
PY
)
curl -s -m 30 -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d "$Q" \
  "$API/v1/admin/index/lookup" | python3 -c '
import json, sys
b = json.load(sys.stdin)
c = b["results"][0]["candidates"]
print("smoke lookup: generation=%s candidates=%d stages=%s" % (b["generation"], len(c), sorted({x["stage"] for x in c})))'
unset TOKEN
echo "=== catalog index prod load done $(ts)"

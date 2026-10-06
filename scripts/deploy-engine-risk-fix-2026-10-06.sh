#!/usr/bin/env bash
# =============================================================================
# IN-REPO MIRROR (2026-10-06) of /srv/crawmatic/deploy-engine-security-2026-10-02.sh,
# reused for the risk-review fix branch. Same steps; differences from the original:
# BRANCH defaults to fix/risk-review-engine-2026-10-06 (pass DEPLOY_SHA either way).
# Either copy works; keep them in step if one is edited.
#
# OWNER-RUN. Crawmatic ENGINE production deploy of the 2026-10-02 security fixes
# (branch fix/security-complete-2026-10-02, base 4522a39 == prod engine SHA).
# NOT run by Claude: the auto-mode classifier denies every Railway mutation.
#
#     ! bash /srv/crawmatic/deploy-engine-security-2026-10-02.sh --dry-run   # prints every action, mutates nothing
#     ! bash /srv/crawmatic/deploy-engine-security-2026-10-02.sh             # the real thing
#
# Order:
#   0  preflight: clean checkout of the named commit, alembic single head, railway2 token,
#      INDEX_SERVICE_TOKEN present on the engine api service AND readable locally AND equal (H1)
#   1  competitor-domain DRY RUN against prod (read-only). STOPS on any collision/invalid
#      domain, BEFORE any Alembic step (A5: no automatic merging)
#   2  DR backup (rollback target)
#   3  JWT_LEGACY_AUD_GRACE_UNTIL (offered; now + one access TTL + build buffer)
#   4  migrate service (alembic b3d9e5a17c42 -> c4e8f2a6b913 -> d7a1f3c5e902 -> e2b8d4f6a1c3; upgrade only)
#   5  api -> worker -> scheduler -> scrapers -> scrapers-browser (from the clean checkout)
#   6  external_ref backfill steps (printed; owner-run SQL after the SaaS export)
#   7  post-deploy checks with the owner's test workspace key
#
# Secrets NEVER appear on argv or in output. The Railway token goes through a 0600 env
# file (shredded on exit); INDEX_SERVICE_TOKEN / workspace key / dry-run DSN are read from
# the environment or 0600 files and handed to curl/python via process-substitution header
# files or the environment.
#
# Inputs (env, all optional unless noted):
#   DEPLOY_SHA             commit to ship (default: tip of $BRANCH; printed before anything runs)
#   INDEX_SERVICE_TOKEN    or file $INDEX_TOKEN_FILE (default /root/.crawmatic/index-service-token)
#   DRYRUN_DATABASE_URL    or file $DRYRUN_DSN_FILE (default /root/.crawmatic/engine-prod-readonly-dsn,
#                          a prod engine DSN reachable from this box; REQUIRED for step 1)
#   SET_JWT_GRACE=1|0      answer the step-3 offer non-interactively (default: ask if a tty, else 0)
#   GRACE_BUFFER_SECONDS   build/rollout allowance added to the TTL (default 600)
#   SKIP_DR_BACKUP=1, SKIP_MIGRATE=1, ONLY_SERVICES="worker scheduler ..."  (resume helpers)
#   Post-deploy (step 7):  WS_KEY_FILE (default /root/.crawmatic/test-workspace-key, 0600),
#     TEST_COMPETITOR_ID + TEST_VARIANT_ID (foreign-host check),
#     TEST_MATCH_ID (a match on a robots-respecting fixture competitor, scrape check)
#
# Rollback: redeploy 4522a39 (previous release identity in
# /srv/crawmatic/evidence/deploy-catalog-index-2026-10-02/) to the services; the three
# migrations downgrade cleanly (proven on scratch PG) but prefer the DR set for schema
# rollback (crawmatic/docs/DEPLOY-ROLLBACK.md). Note b3d9e5a17c42's domain rewrite is not
# reversible by downgrade (canonical domains stay canonical).
# =============================================================================
set -euo pipefail

DRY=0
for a in "$@"; do case "$a" in --dry-run) DRY=1 ;; -h|--help) sed -n 2,53p "$0"; exit 0 ;; *) echo "unknown arg: $a"; exit 2 ;; esac; done

PROJECT=69dc4bda-0d97-4290-a82f-822ed97d3fb8     # Crawmatic engine (railway2)
ENVNAME=production
REPO=/srv/crawmatic/crawmatic
BRANCH=fix/risk-review-engine-2026-10-06
BASE_SHA=4522a39
OLD_HEAD=a7c41e9d2b56
NEW_HEAD=e2b8d4f6a1c3   # 2026-10-06 risk fix: e2b8d4f6a1c3 (cpm keyset index) sits on d7a1f3c5e902
API=${API:-https://api-production-7193.up.railway.app}
NVM_BIN=${MAHMOUD_NODE_BIN:-/home/mahmoud/.nvm/versions/node/v24.18.0/bin}
INDEX_TOKEN_FILE=${INDEX_TOKEN_FILE:-/root/.crawmatic/index-service-token}
DRYRUN_DSN_FILE=${DRYRUN_DSN_FILE:-/root/.crawmatic/engine-prod-readonly-dsn}
WS_KEY_FILE=${WS_KEY_FILE:-/root/.crawmatic/test-workspace-key}
GRACE_BUFFER_SECONDS=${GRACE_BUFFER_SECONDS:-600}
ACCESS_TTL_SECONDS=900                            # ACCESS_TOKEN_TTL_SECONDS default (libs/shared/app_shared/config.py)
EVIDENCE=/srv/crawmatic/evidence/deploy-engine-security-2026-10-02
LOG=$EVIDENCE/DEPLOY-LOG.txt

mkdir -p "$EVIDENCE"
exec > >(tee -a "$LOG") 2>&1
ts() { date -u +%FT%TZ; }
step() { echo; echo "----- $(ts)  $*"; }
echo "=== engine security deploy start $(ts) dry_run=$DRY (log: $LOG)"

# shellcheck disable=SC1091
source /root/.railway/accounts.sh
[[ -n "${RAILWAY_TOKEN_RAILWAY2:-}" ]] || { echo "!! RAILWAY_TOKEN_RAILWAY2 missing from /root/.railway/accounts.sh"; exit 1; }

# ---- secrets: one 0600 env file for the railway token, shredded on exit ------
ENVF=$(mktemp); chmod 600 "$ENVF"
printf 'RAILWAY_API_TOKEN=%q\n' "$RAILWAY_TOKEN_RAILWAY2" > "$ENVF"
chown mahmoud:mahmoud "$ENVF"
unset RAILWAY_TOKEN_RAILWAY2 RAILWAY_API_TOKEN 2>/dev/null || true
CLEAN=""
cleanup() {
  shred -u "$ENVF" 2>/dev/null || rm -f "$ENVF"
  if [[ -n "$CLEAN" && -d "$CLEAN" ]]; then git -C "$REPO" worktree remove --force "$CLEAN" 2>/dev/null || rm -rf "$CLEAN"; fi
}
trap cleanup EXIT

# railway CLI as mahmoud, token from the env file (never argv), cwd = the clean checkout
rw() {
  if (( DRY )); then echo "[dry-run] railway $*"; return 0; fi
  sudo -n -u mahmoud -H bash -c 'set -a; . "$1"; set +a; export PATH="$3:$PATH"; cd "$2" && shift 3 && railway "$@"' _ "$ENVF" "${CLEAN:-$REPO}" "$NVM_BIN" "$@"
}

# ---- local token readers (value never printed, never on argv) ----------------
index_token() {
  local tok=${INDEX_SERVICE_TOKEN:-}
  if [[ -z "$tok" && -r "$INDEX_TOKEN_FILE" ]]; then tok=$(tr -d '\r\n' < "$INDEX_TOKEN_FILE"); fi
  printf '%s' "$tok"
}
bearer_header() {   # bearer_header <token-producing-function> -> header line (for curl -H @<(...))
  local t; t=$("$1"); [[ -n "$t" ]] && printf 'Authorization: Bearer %s\n' "$t"; return 0
}
ws_key() { [[ -r "$WS_KEY_FILE" ]] && tr -d '\r\n' < "$WS_KEY_FILE"; return 0; }
version_json() { curl -s -m 15 -H @<(bearer_header index_token) "$API/version"; }
jv() { version_json | python3 -c "import sys,json
try: print(json.load(sys.stdin).get('$1') or '')
except Exception: print('')"; }
wait_for() {   # wait_for "<label>" <max_seconds> <cmd...>
  local label=$1 max=$2; shift 2; local t=0
  (( DRY )) && { echo "[dry-run] wait for: $label"; return 0; }
  until "$@"; do t=$((t+15)); (( t >= max )) && { echo "!! TIMEOUT after ${max}s: $label"; return 1; }; sleep 15; done
  echo "OK: $label ($(ts))"
}

# ---- 0. preflight --------------------------------------------------------------
step "0. preflight"
git -C "$REPO" rev-parse --verify -q "$BRANCH" >/dev/null || { echo "!! branch $BRANCH not found in $REPO"; exit 1; }
SHA=$(git -C "$REPO" rev-parse "${DEPLOY_SHA:-$BRANCH}")
git -C "$REPO" merge-base --is-ancestor "$BASE_SHA" "$SHA" || { echo "!! $SHA does not descend from base $BASE_SHA"; exit 1; }
echo "will deploy commit $SHA ($(git -C "$REPO" log -1 --format=%s "$SHA"))"
git -C "$REPO" log --oneline "$BASE_SHA..$SHA" | head -40
CLEAN=$(mktemp -d /tmp/engine-security-deploy.XXXXXX); rmdir "$CLEAN"
git -C "$REPO" worktree add --detach "$CLEAN" "$SHA" >/dev/null
chown -R mahmoud:mahmoud "$CLEAN"
echo "clean checkout: $CLEAN (removed on exit; the live run worktree is never uploaded)"
[[ -z "$(git -C "$CLEAN" status --porcelain)" ]] || { echo "!! clean checkout is dirty"; exit 1; }
heads=$(cd "$CLEAN" && sudo -n -u mahmoud /srv/crawmatic/crawmatic/.venv/bin/python -m alembic heads 2>/dev/null | awk '{print $1}')
[[ "$heads" == "$NEW_HEAD" ]] || { echo "!! alembic heads in $SHA = '$heads' (expected single $NEW_HEAD). Update NEW_HEAD or fix the branch."; exit 1; }
sudo -n -u mahmoud test -x "$NVM_BIN/railway" || { echo "!! railway CLI not at $NVM_BIN (set MAHMOUD_NODE_BIN)"; exit 1; }

echo "-- INDEX_SERVICE_TOKEN pre-check (H1: a hardened api answers /version with only {status:ok} without it)"
LOCAL_TOK_LEN=$(index_token | wc -c)
if (( LOCAL_TOK_LEN < 32 )); then
  echo "!! INDEX_SERVICE_TOKEN is not readable locally (env INDEX_SERVICE_TOKEN or 0600 file $INDEX_TOKEN_FILE), or is < 32 chars."
  exit 1
fi
echo "OK local token readable (length >= 32, value not shown)"
if (( DRY )); then
  echo "[dry-run] railway variable list -s api --json | verify INDEX_SERVICE_TOKEN exists, >= 32 chars, equals the local token"
else
  rw variable list -s api -e "$ENVNAME" -p "$PROJECT" --json 2>/dev/null | INDEX_TOKEN_FILE="$INDEX_TOKEN_FILE" python3 -c '
import os, sys, json
try:
    remote = (json.load(sys.stdin).get("INDEX_SERVICE_TOKEN") or "").strip()
except Exception:
    print("!! could not read api variables from railway2"); sys.exit(1)
local = (os.environ.get("INDEX_SERVICE_TOKEN") or "").strip()
if not local:
    try: local = open(os.environ["INDEX_TOKEN_FILE"]).read().strip()
    except OSError: local = ""
if not remote: print("!! INDEX_SERVICE_TOKEN is NOT set on the engine api service (set it first; the hardened /version, /health/scraping and admin index routes need it)"); sys.exit(1)
if len(remote) < 32: print("!! INDEX_SERVICE_TOKEN on api is shorter than 32 chars"); sys.exit(1)
if remote != local: print("!! the local token differs from the one set on the api service"); sys.exit(1)
print("OK INDEX_SERVICE_TOKEN set on api, >= 32 chars, equals the local token (value not shown)")' \
    || exit 1
fi

if [[ "$(jv git_sha)" == "" ]]; then echo "note: live /version shows no git_sha (token mismatch, or api down)"; fi
LIVE_HEAD=$(jv db_migration_head)
echo "live /version: git_sha=$(jv git_sha) db_head=${LIVE_HEAD:-?}"
# d7a1f3c5e902 = the 10-02 security head, live if that release shipped before the 10-06 risk fix.
if [[ -n "$LIVE_HEAD" && "$LIVE_HEAD" != "$OLD_HEAD" && "$LIVE_HEAD" != "d7a1f3c5e902" && "$LIVE_HEAD" != "$NEW_HEAD" ]]; then
  echo "!! live db head is '$LIVE_HEAD', expected $OLD_HEAD, d7a1f3c5e902 or $NEW_HEAD"; exit 1
fi
df -h / | tail -1

# ---- 1. competitor-domain dry run: STOP on collisions, BEFORE alembic ---------
step "1. competitor-domain dry run (read-only) - stops here on any collision"
DSN_SRC=""
if [[ -n "${DRYRUN_DATABASE_URL:-}" ]]; then DSN_SRC=env
elif [[ -r "$DRYRUN_DSN_FILE" ]]; then DRYRUN_DATABASE_URL=$(tr -d '\r\n' < "$DRYRUN_DSN_FILE"); export DRYRUN_DATABASE_URL; DSN_SRC=$DRYRUN_DSN_FILE
fi
if [[ -z "$DSN_SRC" ]]; then
  echo "!! no prod DSN for the dry run. Put a (read-only) prod engine DSN in the environment as DRYRUN_DATABASE_URL"
  echo "   or in the 0600 file $DRYRUN_DSN_FILE (use the railway2 public/proxy URL, never printed). The migration"
  echo "   refuses on collisions anyway, but this script must stop BEFORE the migrate service is uploaded."
  (( DRY )) || exit 1
  echo "[dry-run] would stop here"
else
  echo "DSN source: $DSN_SRC (value not shown)"
  if ! PYTHONPATH="$CLEAN/libs/shared:$CLEAN/libs/scrape-core" \
      /srv/crawmatic/crawmatic/.venv/bin/python "$CLEAN/scripts/security/competitor_domain_dryrun.py"; then
    echo
    echo "!! STOP: competitors collide or have invalid domains after canonicalisation (listed above)."
    echo "   Resolve them by hand in prod (merge or delete duplicates; no automatic merging, plan A5), re-run this script."
    echo "   Nothing was deployed, no migration ran."
    exit 1
  fi
  unset DRYRUN_DATABASE_URL
  echo "OK: no collisions. Review the FOREIGN-HOST MATCHES list above: those rows stay but can no longer be created/updated."
fi

# ---- 2. DR backup ----------------------------------------------------------------
if [[ "${SKIP_DR_BACKUP:-0}" == "1" || "$LIVE_HEAD" == "$NEW_HEAD" ]]; then
  step "2. DR backup skipped"
elif (( DRY )); then
  step "2. DR backup"; echo "[dry-run] DR_MIN_FREE_BYTES=0 DR_MIN_KEEP=999 flock -n /run/crawmatic-dr-backup.lock $REPO/scripts/dr/backup_prod.sh"
else
  step "2. DR backup (rollback target; DR_MIN_KEEP=999 prunes nothing)"
  DR_MIN_FREE_BYTES=0 DR_MIN_KEEP=999 flock -n /run/crawmatic-dr-backup.lock "$REPO/scripts/dr/backup_prod.sh"
  DRSET=$(ls -td /srv/crawmatic/backups/dr/sets/set-* | head -1); echo "ROLLBACK TARGET: $DRSET"
fi

# ---- 3. JWT legacy-aud grace (offer) ---------------------------------------------
step "3. JWT_LEGACY_AUD_GRACE_UNTIL (engine tokens now carry aud/iss; E8)"
echo "Without it every pre-deploy access token is refused at the cutover (users log in again; refresh tokens are unaffected)."
echo "With it, aud-less tokens are accepted until now + ${ACCESS_TTL_SECONDS}s access TTL + ${GRACE_BUFFER_SECONDS}s build buffer."
ANS=${SET_JWT_GRACE:-}
if [[ -z "$ANS" ]]; then
  if [[ -t 0 && $DRY -eq 0 ]]; then read -r -p "Set the grace window on api/worker? [y/N] " yn; [[ "$yn" =~ ^[Yy] ]] && ANS=1 || ANS=0; else ANS=0; fi
fi
if [[ "$ANS" == "1" ]]; then
  GRACE=$(date -u -d "+$((ACCESS_TTL_SECONDS + GRACE_BUFFER_SECONDS)) seconds" +%Y-%m-%dT%H:%M:%SZ)
  echo "JWT_LEGACY_AUD_GRACE_UNTIL=$GRACE  (not a secret)"
  rw variable set "JWT_LEGACY_AUD_GRACE_UNTIL=$GRACE" -s api -e "$ENVNAME" -p "$PROJECT" --skip-deploys
  echo "Afterwards (optional): railway variable delete JWT_LEGACY_AUD_GRACE_UNTIL -s api -e $ENVNAME -p $PROJECT"
else
  echo "not set (no grace). Re-run with SET_JWT_GRACE=1 to enable it."
fi

# ---- release identity for THIS commit, baked into the upload tree -----------------
step "3b. release identity for $SHA (never committed; removed with the clean checkout)"
if (( DRY )); then
  echo "[dry-run] build_release_manifest.py --emit-identity -> $CLEAN/libs/shared/app_shared/_baked_release.py"
else
  ( cd "$CLEAN" && sudo -n -u mahmoud /srv/crawmatic/crawmatic/.venv/bin/python scripts/build_release_manifest.py \
      --out /tmp/engine-sec-manifest.json --emit-identity /tmp/engine-sec-identity.json >/dev/null )
  python3 - "$CLEAN/libs/shared/app_shared/_baked_release.py" "$SHA" <<'PY'
import json, pprint, sys
ident = json.load(open("/tmp/engine-sec-identity.json"))
assert ident["expected_db_migration"], "identity has no expected_db_migration"
open(sys.argv[1], "w").write('"""GENERATED AT DEPLOY TIME. DO NOT COMMIT. Release identity for %s."""\n\nRELEASE_IDENTITY: dict = %s\n' % (sys.argv[2][:7], pprint.pformat(ident, sort_dicts=True)))
print("baked identity written; expected_db_migration =", ident["expected_db_migration"])
PY
  chown mahmoud:mahmoud "$CLEAN/libs/shared/app_shared/_baked_release.py"
  grep -q "\"$NEW_HEAD\"\|'$NEW_HEAD'" "$CLEAN/libs/shared/app_shared/_baked_release.py" || { echo "!! baked identity does not expect $NEW_HEAD"; exit 1; }
fi

db_head_is_new() { [[ "$(jv db_migration_head)" == "$NEW_HEAD" ]]; }
api_ready() { curl -s -m 15 "$API/ready" | grep -q '"ready":true'; }
deploy() {
  local svc=$1 dlog; dlog=$(mktemp "/tmp/deploy-$svc.XXXXXX.log")
  step "railway up $svc"
  if (( DRY )); then echo "[dry-run] railway up -s $svc -e $ENVNAME -p $PROJECT --ci  (from $CLEAN)"; return 0; fi
  rw up -s "$svc" -e "$ENVNAME" -p "$PROJECT" --ci 2>&1 | tee "$dlog" | grep -E 'Deployment:|Deploy complete|rror|failed' | head -20 || true
  grep -q 'Deploy complete' "$dlog" || { echo "!! $svc did not report 'Deploy complete' - see $dlog"; return 1; }
  echo "OK: $svc deploy complete"
}

# ---- 4. migrate ---------------------------------------------------------------------
if [[ "${SKIP_MIGRATE:-0}" == "1" || "$LIVE_HEAD" == "$NEW_HEAD" ]]; then
  step "4. migrate skipped"
else
  step "4. migrate: $OLD_HEAD -> b3d9e5a17c42 -> c4e8f2a6b913 -> d7a1f3c5e902 -> $NEW_HEAD"
  deploy migrate || { echo "!! migrate failed. Check: railway logs -s migrate -e $ENVNAME -p $PROJECT. Nothing else was deployed."; exit 1; }
  wait_for "live db head == $NEW_HEAD" 1200 db_head_is_new \
    || { echo "!! migrate did not reach $NEW_HEAD. Nothing else was deployed."; exit 1; }
fi

# ---- 5. services ------------------------------------------------------------------------
for svc in ${ONLY_SERVICES:-api worker scheduler scrapers scrapers-browser}; do
  if [[ "$svc" == "api" ]]; then rw variable set "GIT_SHA=$SHA" -s api -e "$ENVNAME" -p "$PROJECT" --skip-deploys >/dev/null 2>&1 || echo "note: GIT_SHA not set"; fi
  deploy "$svc" || { echo "!! stopping at $svc. Resume: ONLY_SERVICES=\"<remaining>\" SKIP_MIGRATE=1 SKIP_DR_BACKUP=1 bash $0"; exit 1; }
  if [[ "$svc" == "api" ]]; then
    wait_for "api /ready" 900 api_ready || { echo "!! api not ready"; exit 1; }
  fi
done

# ---- 6. external_ref backfill (owner steps; printed, not run) ----------------------------
step "6. workspaces.external_ref backfill (E5) - OWNER STEPS, run after this script"
cat <<'EOF'
Engine provisioning is now idempotent on workspaces.external_ref. Existing workspaces have it NULL
until backfilled; until then SaaS re-provisioning creates a duplicate workspace instead of reusing one.
  1. Export the SaaS mapping (SaaS prod DB via the rw003 project; creds from the environment, not argv):
       psql "$SAAS_DATABASE_URL" -At -F, -c \
         'SELECT "cmWorkspaceId", id FROM "Project" WHERE "cmWorkspaceId" IS NOT NULL' > /root/map.csv
  2. python scripts/security/backfill_workspace_external_ref.py /root/map.csv > /root/backfill.sql
       (read-only generator; refuses duplicate/malformed rows; only NULL rows are updated, re-runnable)
  3. Review /root/backfill.sql, then apply to the ENGINE DB in one transaction:
       psql "$ENGINE_DATABASE_URL" -v ON_ERROR_STOP=1 -1 -f /root/backfill.sql
  4. Cross-lane: the SaaS provisionTenant client MUST tolerate api_key = null for an existing external_ref
     and must send external_ref (SaaS lane). Confirm the SaaS side is deployed before re-provisioning anyone.
EOF

# ---- 7. post-deploy checks --------------------------------------------------------------------
step "7. post-deploy checks"
FAILS=0; SKIPPED=0
check() { if [[ "$2" == "0" ]]; then echo "PASS  $1"; else echo "FAIL  $1"; FAILS=$((FAILS+1)); fi; }
skip()  { echo "SKIP  $1"; SKIPPED=$((SKIPPED+1)); }
code()  { curl -s -o /dev/null -w '%{http_code}' -m 30 "$@"; }
if (( DRY )); then
  echo "[dry-run] checks: /ready 200; db head == $NEW_HEAD; /version without token has no git_sha; /version with token has git_sha=$SHA;"
  echo "[dry-run]   foreign-host POST /v1/matches -> 422; robots-respecting fixture scrape (TEST_MATCH_ID) succeeds; queue age re-check command"
else
  [[ "$(code "$API/ready")" == "200" ]]; check "/ready 200" $?
  [[ "$(jv db_migration_head)" == "$NEW_HEAD" ]]; check "db head is $NEW_HEAD" $?
  anon=$(curl -s -m 15 "$API/version"); ! grep -qE 'git_sha|migration|manifest' <<<"$anon"; check "/version without a token carries no SHA/heads/manifest (H1)" $?
  [[ "$(jv git_sha)" == "$SHA"* || "$SHA" == "$(jv git_sha)"* ]]; check "/version with the index bearer reports git_sha $SHA" $?
  [[ "$(code "$API/health/scraping")" == "401" || "$(code "$API/health/scraping")" == "403" ]]; check "/health/scraping refuses an anonymous caller" $?

  if [[ -z "$(ws_key)" ]]; then
    skip "workspace-key checks: no key in $WS_KEY_FILE (0600, the owner's TEST workspace key)"
  else
    if [[ -n "${TEST_COMPETITOR_ID:-}" && -n "${TEST_VARIANT_ID:-}" ]]; then
      body=$(printf '{"product_variant_id":"%s","competitor_id":"%s","competitor_url":"https://not-the-competitor.invalid.example.net/p/1"}' "$TEST_VARIANT_ID" "$TEST_COMPETITOR_ID")
      st=$(code -X POST -H @<(bearer_header ws_key) -H 'Content-Type: application/json' -d "$body" "$API/v1/matches")
      [[ "$st" == "422" ]]; check "foreign-host match is refused with 422 (got $st)" $?
    else skip "foreign-host check: set TEST_COMPETITOR_ID and TEST_VARIANT_ID (test workspace)"; fi

    if [[ -n "${TEST_MATCH_ID:-}" ]]; then
      resp=$(curl -s -m 30 -X POST -H @<(bearer_header ws_key) "$API/v1/jobs/run/match/$TEST_MATCH_ID")
      JOB=$(python3 -c 'import sys,json
try: d=json.loads(sys.argv[1]); print(d.get("job_id") or d.get("id") or "")
except Exception: print("")' "$resp")
      if [[ -z "$JOB" ]]; then check "robots-respecting fixture scrape: job accepted (response: ${resp:0:200})" 1
      else
        final=""; for _ in $(seq 1 40); do
          final=$(curl -s -m 20 -H @<(bearer_header ws_key) "$API/v1/jobs/$JOB" | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("status",""))
except Exception: print("")')
          case "$final" in SUCCEEDED|COMPLETED|succeeded|completed|FAILED|failed|PARTIAL|partial) break ;; esac; sleep 15
        done
        [[ "$final" == "SUCCEEDED" || "$final" == "COMPLETED" || "$final" == "succeeded" || "$final" == "completed" ]]
        check "robots-respecting fixture scrape finished (status=$final)" $?
      fi
    else skip "fixture scrape: set TEST_MATCH_ID (a match on a robots-respecting fixture competitor)"; fi
  fi
fi
echo
echo "Scheduler/queue check (run ~15 min after the deploy; oldest pending target age should stay small and not grow):"
echo "  curl -s -H @<(printf 'Authorization: Bearer %s\\n' \"\$(tr -d '\\r\\n' < $INDEX_TOKEN_FILE)\") $API/health/scraping | python3 -m json.tool"
echo "  (field oldest_pending_target_age_seconds; null = queue empty)"
if (( DRY )); then echo "=== DRY RUN COMPLETE $(ts): nothing mutated"; exit 0; fi
if (( FAILS == 0 )); then echo "=== DEPLOY VERIFIED $(ts) ($SKIPPED check(s) skipped - see SKIP lines)"; else echo "=== $FAILS CHECK(S) FAILED $(ts)"; exit 1; fi

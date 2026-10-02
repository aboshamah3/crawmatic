#!/usr/bin/env bash
# =============================================================================
# OWNER-RUN. Redeploy ONLY the engine `worker` with cfc72c4 (dispatch fix).
#
#     ! bash /srv/crawmatic/crawmatic/scripts/deploy-worker-dispatch-fix-2026-10-02.sh
#
# Why: 44b24ac registered `_denial_window_expired` under the Celery name
# scrape_dispatch.dispatch_job, so every dispatch raised TypeError and prod
# has scraped nothing since 2026-10-01 (ops-metrics: pipeline_silent,
# pending_job_age, pending_target_age, scrapyd.saturation). The 2 stuck jobs
# (4,374 targets) are re-offered every 60 s by redispatch_pending_jobs, so
# they dispatch on their own once the fixed worker is up.
#
# Uploads the feat/catalog-index-2026-10-02 tree (what `api` already runs,
# migration head a7c41e9d2b56 already applied) with the same baked identity.
# No migrate, no variables, no other service.
# Rollback: redeploy the previous worker deployment from the Railway dashboard.
# =============================================================================
set -uo pipefail

PROJECT=69dc4bda-0d97-4290-a82f-822ed97d3fb8
ENVNAME=production
REPO=/srv/crawmatic/crawmatic
BRANCH=feat/catalog-index-2026-10-02
FIX=cfc72c4
NVM_BIN=${MAHMOUD_NODE_BIN:-/home/mahmoud/.nvm/versions/node/v24.18.0/bin}
BAKED_SRC=${BAKED_SRC:-/srv/crawmatic/evidence/deploy-catalog-index-2026-10-02/_baked_release.py}
BAKED_DST=$REPO/libs/shared/app_shared/_baked_release.py
LOG=/srv/crawmatic/evidence/deploy-worker-dispatch-fix-2026-10-02.log

exec > >(tee -a "$LOG") 2>&1
ts() { date -u +%FT%TZ; }
echo "=== worker dispatch-fix deploy start $(ts)"

# shellcheck disable=SC1091
source /root/.railway/accounts.sh
[[ -n "${RAILWAY_TOKEN_RAILWAY2:-}" ]] || { echo "!! RAILWAY_TOKEN_RAILWAY2 missing"; exit 1; }
rw() {
  sudo -n -u mahmoud -H env RAILWAY_API_TOKEN="$RAILWAY_TOKEN_RAILWAY2" PATH="$NVM_BIN:$PATH" \
    bash -c 'cd "$1" && shift && railway "$@"' _ "$REPO" "$@"
}

[[ "$(git -C "$REPO" rev-parse --abbrev-ref HEAD)" == "$BRANCH" ]] || { echo "!! $REPO not on $BRANCH"; exit 1; }
git -C "$REPO" merge-base --is-ancestor "$FIX" HEAD || { echo "!! HEAD lacks $FIX"; exit 1; }
[[ -z "$(git -C "$REPO" status --porcelain --untracked-files=no)" ]] || { echo "!! tracked files dirty"; git -C "$REPO" status --short | head; exit 1; }
sudo -n -u mahmoud test -x "$NVM_BIN/railway" || { echo "!! railway CLI not at $NVM_BIN (set MAHMOUD_NODE_BIN)"; exit 1; }
[[ -f "$BAKED_SRC" ]] || { echo "!! $BAKED_SRC missing"; exit 1; }

cp "$BAKED_SRC" "$BAKED_DST" && chown mahmoud:mahmoud "$BAKED_DST" && chmod 644 "$BAKED_DST"
trap 'rm -f "$BAKED_DST"' EXIT

dlog=$(mktemp /tmp/deploy-worker.XXXXXX.log)
rw up -s worker -e "$ENVNAME" -p "$PROJECT" --ci 2>&1 | tee "$dlog" \
  | grep -E 'Deployment:|Deploy complete|rror|failed' | head -20
grep -q 'Deploy complete' "$dlog" || { echo "!! worker deploy did not report 'Deploy complete' - see $dlog"; exit 1; }
echo "OK: worker deploy complete $(ts)"

echo "waiting 150 s for redispatch_pending_jobs to re-offer the stuck jobs..."
sleep 150
out=$(rw logs -s worker -e "$ENVNAME" -p "$PROJECT" -n 400 2>&1)
if grep -q "_denial_window_expired() got an unexpected keyword" <<<"$out"; then
  echo "!! FAIL: the TypeError is still in the worker log"
  exit 1
fi
echo "dispatch lines since deploy:"
grep -E "scrape_dispatch.dispatch_job|dispatch:" <<<"$out" | tail -8
echo "=== done $(ts). Check the next ops mail (20 min) for pipeline_silent / pending_* to clear."

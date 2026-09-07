#!/usr/bin/env bash
# rehearse_upgrade.sh — release-day upgrade rehearsal on a RESTORED COPY.
#
# EPA task A10 (F20): "rehearse the upgrade on a restored production-sized
# database" before the real release window. This is Step 1 of
# docs/ops/RELEASE_1_2026-09.md and is read-only with respect to production —
# it never touches a live database, only the stored encrypted backup set.
#
# What it proves, in order, timing each step and stopping loudly (non-zero
# exit, `DR-ALERT` on stderr — see dr_lib.sh) the instant one fails:
#
#   1. The chosen backup set's ciphertext still matches its SHA256SUMS.
#   2. The PostgreSQL major the dump was taken FROM is READ from the backup
#      manifest (and cross-checked against the dump's own `pg_restore -l`
#      header) — never assumed — and a throwaway container of THAT major is
#      what the restore below actually targets. See "WHY NOT THE COMPOSE
#      POSTGRES SERVICE" below.
#   3. The dump restores cleanly (`pg_restore --no-owner --no-privileges`)
#      into that fresh instance.
#   4. `scripts/provision_db_roles.sql` (via `provision_db_roles.py
#      --provision --adopt-ownership`) provisions the four component roles
#      and reassigns table ownership to `crawmatic_migrate` — the same
#      ownership state a real deploy's roles step (RELEASE_1 §6) puts
#      production in — so step 5 rehearses `alembic upgrade head` running as
#      the SAME role and the SAME privilege level production migrations
#      actually use, not as a superuser that would mask a missing GRANT.
#   5. `alembic -x db_url=... upgrade head` reaches the current single head
#      from whatever revision the backup was taken at.
#   6. `scripts/preflight_regex_profiles.py` — no stored `scrape_profiles`
#      pattern blows the enforced regex deadline on adversarial input.
#   7. `scripts/verify_grants.py` — the post-migration/post-provision grants
#      match `scripts/sql/grants_expected.yaml` exactly (no drift).
#
# WHY NOT THE COMPOSE POSTGRES SERVICE
# -------------------------------------
# `docker-compose.yml`'s `postgres` service is pinned to
# `postgres:17.5-bookworm`. The backup manifest's `server_version` field
# (recorded by backup_prod.sh at dump time, cross-checked here against the
# dump's own `pg_restore -l` TOC header) says production is PostgreSQL
# **18.x** — so the compose pin is stale, not the plan text. Restoring an
# 18.x dump into a 17.5 server is exactly the wrong-direction major-version
# mismatch pg_restore refuses (a newer dump into an older server), so this
# script starts its OWN throwaway container image-pinned to the MAJOR THE
# MANIFEST ACTUALLY SAYS, every run, rather than hardcoding either number.
# This is logged loudly (`dr_log`) and must be copied into the release doc's
# Step 1 finding each time this script runs for real. Bumping
# `docker-compose.yml`'s pin to match is a separate, tiny follow-up PR —
# out of this task's file scope (A10 may create/modify only this script and
# the release doc) and is flagged as an owner item rather than made here.
#
# WHAT THIS SCRIPT NEVER DOES
# ----------------------------
# - Never writes a plaintext dump to disk: `dr_gpg_decrypt_stdout` pipes
#   straight into `pg_restore`, same as verify_restore.sh.
# - Never touches production: everything after "restore" runs against the
#   throwaway rehearsal container, torn down (`docker rm -f -v`) on exit —
#   success, failure or interrupt — by the same trap pattern verify_restore.sh
#   uses, so a restored copy of production data never lingers as a dangling
#   volume.
# - Never prints a credential. Role passwords for the rehearsal container are
#   generated locally with `openssl rand`, exported only into this script's
#   own environment, and unset before exit. `dr_scrub` still runs over every
#   logged line as a second layer.
#
# Usage:
#   sudo scripts/dr/rehearse_upgrade.sh [/srv/crawmatic/backups/dr/sets/<set>|<set-name>]
#   sudo scripts/dr/rehearse_upgrade.sh --report /path/to/report.md [<set>]
#
# With no set given, the newest set under $DR_SETS_DIR is used (same
# convention as verify_restore.sh). Must run as root (needs $DR_KEY_FILE).
#
# Exit codes: 0 all seven steps passed. 1 a step failed (see stderr for which
# — this is the ONLY non-owner-gate failure mode). 2 usage error.
#
# ENOSPC / no docker / disk-pressure handling: per ASSUMPTIONS.md answer 4,
# a resource failure (ENOSPC, docker unavailable) is reported via dr_die
# (loud, non-zero exit) and is logged as a BLOCKER by the caller — it is
# NOT this script's job to soften that into a silent skip.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=./dr_lib.sh
source "$HERE/dr_lib.sh"

REHEARSE_PG_IMAGE_SUFFIX="${REHEARSE_PG_IMAGE_SUFFIX:-alpine}"  # small: disk is ~97% used on this host
REHEARSE_MIN_FREE_BYTES="${REHEARSE_MIN_FREE_BYTES:-1073741824}"  # refuse under 1 GiB free

CONTAINER=""
WORK=""
REPORT=""
STEPS=()   # "name|seconds|status" — printed at the end and into --report

cleanup() {
  local rc=$?
  if [[ -n "$CONTAINER" ]]; then
    # -v: the postgres image declares an anonymous PGDATA volume; without
    # it, `docker rm` leaves the restored production data on disk as a
    # dangling, unencrypted volume across runs.
    docker rm -f -v "$CONTAINER" >/dev/null 2>&1 || true
    dr_log INFO "rehearsal container $CONTAINER and its data volume removed"
  fi
  [[ -n "$WORK" && -d "$WORK" ]] && rm -rf "$WORK"
  unset PGHOST PGPORT PGUSER PGPASSWORD PGDATABASE PGCONNECT_TIMEOUT || true
  unset CRAWMATIC_APP_DB_PASSWORD CRAWMATIC_AUTH_DB_PASSWORD \
        CRAWMATIC_SCRAPER_DB_PASSWORD CRAWMATIC_MIGRATE_DB_PASSWORD || true
  exit $rc
}
trap cleanup EXIT

usage() { echo "usage: $0 [--report PATH] [set-YYYYmmddTHHMMSSZ | /path/to/set-dir]" >&2; }

SET_ARG=""
while (( $# )); do
  case "$1" in
    --report) REPORT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    -*) usage; exit 2 ;;
    *) SET_ARG="$1"; shift ;;
  esac
done

free_port() {
  local p
  for p in $(seq 55620 55699); do
    ss -ltn "sport = :$p" 2>/dev/null | grep -q ":$p" || { echo "$p"; return 0; }
  done
  dr_die "no free loopback port in 55620-55699"
}

# Run one named step, timing it, recording it, and dying loudly on failure —
# "failing loudly on any non-zero exit" is a literal requirement (A10
# acceptance criteria), not just a log line.
run_step() {  # $1 = step name, $2.. = command
  local name="$1"; shift
  local t0 t1 rc=0
  dr_log INFO "── step: $name — start"
  t0=$(date -u +%s)
  if "$@"; then
    rc=0
  else
    rc=$?
  fi
  t1=$(date -u +%s)
  local secs=$(( t1 - t0 ))
  if (( rc == 0 )); then
    dr_log INFO "── step: $name — PASS (${secs}s)"
    STEPS+=("$name|$secs|PASS")
  else
    STEPS+=("$name|$secs|FAIL(exit $rc)")
    dr_alert "step '$name' FAILED with exit $rc after ${secs}s — rehearsal aborted, nothing further runs"
    write_summary
    exit 1
  fi
}

write_summary() {
  local out
  {
    echo "# Upgrade rehearsal — $(basename "$SET_DIR") ($(dr_ts))"
    echo
    echo "| Step | Seconds | Result |"
    echo "|---|---:|---|"
    local s
    for s in "${STEPS[@]}"; do
      IFS='|' read -r n secs st <<<"$s"
      printf '| %s | %s | %s |\n' "$n" "$secs" "$st"
    done
    echo
    echo "- Backup set: \`$SET_DIR\`"
    echo "- Dump server_version (manifest): \`${ENGINE_SERVER_VERSION:-unknown}\`"
    echo "- Rehearsal container image: \`${REHEARSE_PG_IMAGE:-unknown}\`"
    echo "- Compose \`postgres\` service pin: \`${COMPOSE_PG_IMAGE:-unknown}\` — $([[ "${MAJOR_MATCHES_COMPOSE:-0}" == 1 ]] && echo "MATCHES the dump major" || echo "DOES NOT MATCH the dump major (see script header)")"
  } > "${REPORT:-/dev/stdout}"
  if [[ -n "$REPORT" ]]; then
    chmod 600 "$REPORT" 2>/dev/null || true
    dr_log INFO "summary written to $REPORT"
  fi
}

main() {
  dr_require_root
  dr_ensure_key
  command -v docker >/dev/null || dr_die "docker not on PATH"
  command -v jq >/dev/null || dr_die "jq not on PATH"

  # ── resolve the backup set ────────────────────────────────────────────
  if [[ -n "$SET_ARG" && -d "$SET_ARG" ]]; then
    SET_DIR="$SET_ARG"
  elif [[ -n "$SET_ARG" ]]; then
    SET_DIR="$DR_SETS_DIR/$SET_ARG"
  else
    SET_DIR=$(find "$DR_SETS_DIR" -mindepth 1 -maxdepth 1 -type d -name 'set-*' 2>/dev/null | sort | tail -n1)
  fi
  [[ -n "$SET_DIR" && -d "$SET_DIR" ]] || dr_die "no backup set found (looked under $DR_SETS_DIR${SET_ARG:+ and at $SET_ARG})"
  local set_name; set_name=$(basename "$SET_DIR")
  local manifest="$SET_DIR/manifest.json"
  [[ -f "$manifest" ]] || dr_die "$manifest not found"

  dr_log INFO "=== upgrade rehearsal of $set_name start ==="

  # Disk floor before we do anything that writes bytes.
  local free_now; free_now=$(dr_free_bytes "$DR_ROOT")
  (( free_now >= REHEARSE_MIN_FREE_BYTES )) \
    || dr_die "only $(dr_human "$free_now") free under $DR_ROOT (< $(dr_human "$REHEARSE_MIN_FREE_BYTES") floor) — ENOSPC risk, refusing to start (BLOCKER, not a script failure)"

  # 711, not 700: mahmoud (running the venv commands below, per repo
  # convention) needs +x on $WORK to traverse into the mahmoud/ subdir below;
  # 711 grants traversal only, no listing/reading of root's own files in $WORK
  # (the gpg/pg_restore stderr captured further down).
  WORK=$(mktemp -d /tmp/dr-rehearse.XXXXXX); chmod 711 "$WORK"
  mkdir -p "$WORK/mahmoud"; chown mahmoud:mahmoud "$WORK/mahmoud"; chmod 700 "$WORK/mahmoud"

  local file sha; file=$(jq -r '.targets.engine.file' "$manifest")
  sha=$(jq -r '.targets.engine.sha256' "$manifest")
  ENGINE_SERVER_VERSION=$(jq -r '.targets.engine.server_version' "$manifest")
  [[ "$file" != "null" && -n "$file" ]] || dr_die "manifest has no .targets.engine.file"

  # ── step 1: ciphertext integrity ──────────────────────────────────────
  run_step "sha256sums verify" bash -c "cd '$SET_DIR' && sha256sum -c --status SHA256SUMS"

  # ── step 2: determine the Postgres MAJOR from the dump itself, not the
  #    plan text and not the compose pin (A10 acceptance criterion #3) ───
  determine_major() {
    local hdr_major manifest_major
    manifest_major=$(grep -oE '^[0-9]+' <<<"$ENGINE_SERVER_VERSION" || true)
    hdr_major=$(dr_gpg_decrypt_stdout "$SET_DIR/$file" 2>"$WORK/pgrestore_l.err" \
                | "$PG_BIN/pg_restore" -l 2>>"$WORK/pgrestore_l.err" \
                | grep -oE 'Dumped from database version: [0-9]+' \
                | grep -oE '[0-9]+$' || true)
    [[ -n "$manifest_major" ]] || dr_die "could not read .targets.engine.server_version from $manifest"
    [[ -n "$hdr_major" ]] || dr_die "could not read the PostgreSQL major from 'pg_restore -l' TOC header: $(tail -c 300 "$WORK/pgrestore_l.err" | tr '\n' ' ')"
    [[ "$manifest_major" == "$hdr_major" ]] \
      || dr_die "manifest server_version major ($manifest_major) != pg_restore -l TOC header major ($hdr_major) — the two sources of truth disagree, refusing to guess"
    DUMP_MAJOR="$manifest_major"
    dr_log INFO "dump PostgreSQL major = $DUMP_MAJOR (manifest server_version='$ENGINE_SERVER_VERSION', TOC header agrees)"
    COMPOSE_PG_IMAGE=$(grep -oE 'image:\s*postgres:[^[:space:]]+' "$REPO_ROOT/docker-compose.yml" | sed 's/image:\s*//' || echo "unknown")
    local compose_major; compose_major=$(grep -oE 'postgres:[0-9]+' <<<"$COMPOSE_PG_IMAGE" | grep -oE '[0-9]+$' || true)
    if [[ "$compose_major" == "$DUMP_MAJOR" ]]; then
      MAJOR_MATCHES_COMPOSE=1
      dr_log INFO "docker-compose.yml postgres pin ($COMPOSE_PG_IMAGE) MATCHES the dump major"
    else
      MAJOR_MATCHES_COMPOSE=0
      dr_alert "docker-compose.yml pins postgres major $compose_major but the dump is major $DUMP_MAJOR — compose pin is STALE (owner follow-up, not fixed by this script; see script header)"
    fi
  }
  run_step "determine Postgres major from dump" determine_major

  # ── step 3: bring up a throwaway container of the CORRECT major ───────
  local port pw
  port=$(free_port)
  pw=$(openssl rand -hex 24)
  REHEARSE_PG_IMAGE="postgres:${DUMP_MAJOR}-${REHEARSE_PG_IMAGE_SUFFIX}"
  CONTAINER="dr-rehearse-$$"
  start_container() {
    docker run -d --name "$CONTAINER" \
      -e POSTGRES_PASSWORD="$pw" \
      -p "127.0.0.1:$port:5432" "$REHEARSE_PG_IMAGE" >/dev/null
    local ready=0 i
    for i in $(seq 1 60); do
      docker exec "$CONTAINER" pg_isready -q -U postgres >/dev/null 2>&1 && { ready=1; break; }
      sleep 1
    done
    (( ready )) || return 1
  }
  run_step "start rehearsal container ($REHEARSE_PG_IMAGE, loopback:$port)" start_container

  dr_clear_pgenv
  export PGHOST=127.0.0.1 PGPORT="$port" PGUSER=postgres PGPASSWORD="$pw" PGDATABASE=postgres PGCONNECT_TIMEOUT=20
  local super_url="postgresql+psycopg://postgres:${pw}@127.0.0.1:${port}/crawmatic_rehearsal"

  # ── step 4: restore (no plaintext dump ever touches disk) ─────────────
  do_restore() {
    dr_psql -d postgres -c "CREATE DATABASE crawmatic_rehearsal;" >/dev/null
    # pipefail would otherwise make `set -e` abort this function on the
    # pipeline's exit status before PIPESTATUS can be inspected (same
    # guard verify_restore.sh uses around its own decrypt|restore pipe).
    set +e
    dr_gpg_decrypt_stdout "$SET_DIR/$file" 2>"$WORK/restore.gpg.err" \
      | "$PG_BIN/pg_restore" --no-owner --no-privileges -d crawmatic_rehearsal 2>"$WORK/restore.err"
    local st=("${PIPESTATUS[@]}")
    set -e
    [[ "${st[0]}" == 0 ]] || { dr_log ERROR "gpg decrypt exit ${st[0]}: $(tail -c 300 "$WORK/restore.gpg.err" | tr '\n' ' ')"; return 1; }
    [[ "${st[1]}" == 0 ]] || { dr_log ERROR "pg_restore exit ${st[1]}: $(tail -c 300 "$WORK/restore.err" | tr '\n' ' ')"; return 1; }
    return 0
  }
  run_step "restore dump into rehearsal container" do_restore

  # ── step 5: provision the four component roles + adopt ownership, the
  #    same state RELEASE_1 §6 puts production in, so alembic (step 6)
  #    rehearses under the SAME role/privilege level a real deploy uses ──
  do_provision() {
    # Generated once: this runs a SECOND time after migrate (step 6b) and
    # must reuse the same ephemeral passwords, not rotate crawmatic_migrate
    # out from under $migrate_url computed after pass 1.
    export CRAWMATIC_APP_DB_PASSWORD="${CRAWMATIC_APP_DB_PASSWORD:-$(openssl rand -hex 24)}"
    export CRAWMATIC_AUTH_DB_PASSWORD="${CRAWMATIC_AUTH_DB_PASSWORD:-$(openssl rand -hex 24)}"
    export CRAWMATIC_SCRAPER_DB_PASSWORD="${CRAWMATIC_SCRAPER_DB_PASSWORD:-$(openssl rand -hex 24)}"
    export CRAWMATIC_MIGRATE_DB_PASSWORD="${CRAWMATIC_MIGRATE_DB_PASSWORD:-$(openssl rand -hex 24)}"
    PROVISION_DB_ROLES_URL="$super_url" \
      sudo -u mahmoud -E "$REPO_ROOT/.venv/bin/python" "$REPO_ROOT/scripts/provision_db_roles.py" \
        --provision --adopt-ownership
  }
  run_step "provision_db_roles.py --provision --adopt-ownership" do_provision

  # ── step 5b: PRE-EXISTING GAP, found BY this rehearsal, not assumed ────
  #    scripts/provision_db_roles.sql section 8 ("adopt ownership") reassigns
  #    only TABLE and SEQUENCE ownership (`relkind IN ('r','p','S')`) into
  #    crawmatic_migrate — never FUNCTION/TRIGGER ownership. A fresh restore
  #    leaves every function owned by whichever role ran `pg_restore` (here,
  #    the superuser); the FIRST rehearsal run against this backup set hit
  #    exactly this: `alembic upgrade head` as crawmatic_migrate failed with
  #    `must be owner of function network_operation_allocations_check_total`
  #    on migration b7c1d2e3f4a5's `CREATE OR REPLACE FUNCTION`. Whether this
  #    also bites the REAL production migrate step depends on whether
  #    crawmatic_migrate already owns every function a pending migration
  #    touches there — unknown from here, and exactly why RELEASE_1's
  #    checklist gets a mandatory pre-flight query for it (see the release
  #    doc's step 6.5). This script closes the gap itself (scoped to this
  #    task's own throwaway database, never touching provision_db_roles.sql,
  #    which is out of A10's file scope) so the rehearsal can prove the
  #    MIGRATION logic itself is sound once ownership is where production
  #    needs it to be.
  do_adopt_function_ownership() {
    dr_psql -d crawmatic_rehearsal <<'SQL'
DO $$
DECLARE
    rel record;
    moved int := 0;
BEGIN
    FOR rel IN
        SELECT p.oid::regprocedure AS ident
        FROM pg_proc p
        JOIN pg_roles r ON r.oid = p.proowner
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
          AND r.rolname <> 'crawmatic_migrate'
    LOOP
        EXECUTE format('ALTER FUNCTION %s OWNER TO crawmatic_migrate', rel.ident);
        moved := moved + 1;
    END LOOP;
    RAISE NOTICE 'function/trigger-function ownership adopted into crawmatic_migrate for % function(s)', moved;
END
$$;
SQL
  }
  run_step "adopt FUNCTION ownership (gap: provision_db_roles.sql §8 covers tables/sequences only)" do_adopt_function_ownership

  local migrate_url="postgresql+psycopg://crawmatic_migrate:${CRAWMATIC_MIGRATE_DB_PASSWORD}@127.0.0.1:${port}/crawmatic_rehearsal"

  # ── step 6: alembic upgrade head, AS crawmatic_migrate (never postgres
  #    superuser) — a rehearsal that migrates as a superuser could pass
  #    while production, migrating as crawmatic_migrate, fails on a grant
  #    this rehearsal would never have exercised ─────────────────────────
  do_alembic() {
    sudo -u mahmoud -E "$REPO_ROOT/.venv/bin/alembic" -x "db_url=$migrate_url" upgrade head
  }
  run_step "alembic upgrade head (as crawmatic_migrate)" do_alembic

  # ── step 6b: SECOND, real-finding-driven pass of provision_db_roles.py ──
  #    provision_db_roles.sql's own section-5 comment documents this: "A
  #    table named in the manifest that does not exist yet in this database
  #    ... is skipped with a WARNING rather than aborting". Running provision
  #    only BEFORE migrate (RELEASE_1 §6 as literally ordered — needed
  #    because crawmatic_migrate must exist before alembic can even connect
  #    as it) therefore leaves every table a migration creates GRANT-less:
  #    the first rehearsal run against this backup set proved it —
  #    `control_plane_rules` (created by b7c1d2e3f4a5->c8d2e3f4a5b6) came out
  #    of `alembic upgrade head` with zero grants, and `verify_grants.py`
  #    correctly FAILed on it. provision_db_roles.sql is fully idempotent by
  #    design (its own docstring: "every statement is CREATE-if-absent /
  #    ALTER-to-desired-state / idempotent GRANT"), so the fix costs nothing:
  #    run it again, now that every table exists. RELEASE_1's checklist
  #    (step 6.5) carries this as a mandatory second pass, not optional.
  run_step "provision_db_roles.py --provision (pass 2, post-migrate: grants on tables the migration just created)" do_provision

  # ── step 7: regex ReDoS preflight, read-only ───────────────────────────
  do_preflight() {
    PREFLIGHT_DATABASE_URL="$super_url" \
      sudo -u mahmoud -E "$REPO_ROOT/.venv/bin/python" "$REPO_ROOT/scripts/preflight_regex_profiles.py" \
        --report "$WORK/mahmoud/preflight_report.json"
  }
  run_step "preflight_regex_profiles.py" do_preflight

  # ── step 8: grants diff, read-only ─────────────────────────────────────
  do_verify_grants() {
    VERIFY_GRANTS_URL="$super_url" \
      sudo -u mahmoud -E "$REPO_ROOT/.venv/bin/python" "$REPO_ROOT/scripts/verify_grants.py"
  }
  run_step "verify_grants.py" do_verify_grants

  write_summary
  dr_log INFO "=== upgrade rehearsal of $set_name PASSED — all $(( ${#STEPS[@]} )) steps green ==="
}

main "$@"

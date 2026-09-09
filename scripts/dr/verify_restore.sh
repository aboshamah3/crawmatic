#!/usr/bin/env bash
# Restore-verification job: proves a backup RESTORES, not that pg_dump exited 0.
#
# EPA task W5.4 / READY-010 step 2. Scheduled daily from /etc/cron.d/crawmatic-dr.
#
# What it asserts, for every target in the chosen backup set:
#
#   1. SHA256SUMS of the set still match (the ciphertext has not rotted).
#   2. The ciphertext decrypts and restores into a THROWAWAY PostgreSQL 18
#      container with exit status 0 and an empty stderr.
#   3. The SET of base tables read from the restored database's
#      information_schema is IDENTICAL to the set recorded in the manifest —
#      so a table added to production after this script was written cannot be
#      silently skipped, and a table lost in transit cannot pass unnoticed.
#   4. Every per-table row count matches the manifest exactly. The manifest's
#      counts were taken inside the dump's own snapshot (see dr_lib.sh), so
#      "exactly" is the correct bar: a mismatch is corruption, never drift.
#   5. Full-table content checksums for the sampled tables match — row counts
#      alone would pass a restore that preserved every row's existence and
#      corrupted every row's contents.
#
#   6. THE MIGRATION HEAD of the restored database equals the head recorded
#      in the manifest INSIDE the dump's own snapshot (dr_lib.sh). A restore
#      that lands at a different revision is a restore of something else.
#   7. THE SIDECARS a Postgres dump does not contain restore too (C10/F21):
#      the netledger durable buffer, the B1 result spool, the C5 evidence
#      directory listing and the config snapshot. Money already spent and
#      pages already fetched live in those two SQLite queues; a "successful"
#      restore that silently drops them is not a recovery point. Each one is
#      asserted PRESENT-AND-SOUND or SKIPPED-WITH-A-STATED-REASON — never a
#      silent pass.
#   8. THE RLS BACKFILL AUDIT (B2-fix1 follow-up, 2026-09-08): production's
#      fail-closed row-level security silently filters DML issued by
#      `crawmatic_migrate`, so a migration that backfills with a bare
#      UPDATE/INSERT reports "0 rows" and moves on. That is harmless in
#      production, where those migrations ran BEFORE the policies existed —
#      but a DR restore that replays the whole chain onto an empty database
#      would silently skip every one of them. This step pins the KNOWN-latent
#      set and fails if it grows, so a new migration of that shape cannot be
#      added without someone deciding what a fresh restore does about it.
#      §Restoring for real in RUNBOOK.md carries the operator procedure.
#
# Failure => non-zero exit AND a `DR-ALERT` line on stderr. On this host that
# IS the alert: there is no pager, no on-call rotation and no alert router.
# Wiring these exits to a real notification channel is an OWNER GATE, named in
# RUNBOOK.md §Alerting. Cron mails the output to root if a local MTA exists,
# which is a mailbox, not an alert.
#
# Every step is TIMED and the timings are in the report: an RTO number nobody
# measured is a wish.
#
# The scratch container holds a full plaintext copy of production data, so it
# is destroyed unconditionally on exit — success, failure, or interrupt.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./dr_lib.sh
source "$HERE/dr_lib.sh"

DR_PG_IMAGE="${DR_PG_IMAGE:-postgres:18-alpine}"
CONTAINER=""
WORK=""
SET_DIR=""
FAILURES=0

cleanup() {
  local rc=$?
  if [[ -n "$CONTAINER" ]]; then
    # `-v` is not optional: the postgres image declares an anonymous volume for
    # PGDATA, so `docker rm` without it leaves the restored production data on
    # disk as a dangling volume — invisible, unencrypted, and unbounded across
    # daily runs.
    docker rm -f -v "$CONTAINER" >/dev/null 2>&1 || true
    dr_log INFO "scratch container $CONTAINER and its data volume removed (held plaintext production data)"
  fi
  [[ -n "$WORK" && -d "$WORK" ]] && rm -rf "$WORK"
  exit $rc
}
trap cleanup EXIT

usage() { echo "usage: $0 [--set set-YYYYmmddTHHMMSSZ] [--report PATH]"; }

SET_ARG=""; REPORT=""
while (( $# )); do
  case "$1" in
    --set)    SET_ARG="$2"; shift 2 ;;
    --report) REPORT="$2";  shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

fail() { FAILURES=$((FAILURES + 1)); dr_alert "$*"; RESULTS+=("FAIL|$*"); }
pass() { dr_log INFO "PASS $*"; RESULTS+=("PASS|$*"); }
# A skip is a RECORDED FACT with a reason, never a quiet success: an operator
# reading this report must be able to see what was NOT proven.
skip() { dr_log WARN "SKIP $*"; RESULTS+=("SKIP|$*"); }
RESULTS=()
STEPS=()      # "label|seconds" — every step is timed (RTO evidence)

timed() {  # $1 = label, $2.. = command
  local label="$1"; shift
  local t0 t1 rc=0
  t0=$(date -u +%s)
  "$@" || rc=$?
  t1=$(date -u +%s)
  STEPS+=("$label|$(( t1 - t0 ))")
  dr_log INFO "── step: $label — $(( t1 - t0 ))s"
  return "$rc"
}

free_port() {
  local p
  for p in $(seq 55520 55599); do
    ss -ltn "sport = :$p" 2>/dev/null | grep -q ":$p" || { echo "$p"; return 0; }
  done
  dr_die "no free loopback port in 55520-55599"
}

main() {
  dr_require_root
  dr_ensure_key
  command -v docker >/dev/null || dr_die "docker not on PATH"
  [[ -x "$PG_BIN/pg_restore" ]] || dr_die "$PG_BIN/pg_restore not found"

  if [[ -n "$SET_ARG" ]]; then
    SET_DIR="$DR_SETS_DIR/$SET_ARG"
  else
    SET_DIR=$(find "$DR_SETS_DIR" -mindepth 1 -maxdepth 1 -type d -name 'set-*' 2>/dev/null | sort | tail -n1)
  fi
  [[ -n "$SET_DIR" && -d "$SET_DIR" ]] || dr_die "no backup set found under $DR_SETS_DIR"
  local set_name; set_name=$(basename "$SET_DIR")
  REPORT="${REPORT:-$DR_REPORTS_DIR/verify-$set_name.md}"
  install -d -m 700 "$DR_REPORTS_DIR"

  dr_log INFO "=== restore verification of $set_name start ==="
  local run_t0; run_t0=$(date -u +%s)

  # 1. integrity of the stored ciphertext
  if timed "sha256sums verify" bash -c "cd '$SET_DIR' && sha256sum -c --status SHA256SUMS"; then
    pass "SHA256SUMS of $set_name verify"
  else
    fail "SHA256SUMS of $set_name DO NOT verify — stored backup is damaged"
  fi

  WORK=$(mktemp -d /tmp/dr-verify.XXXXXX); chmod 700 "$WORK"

  # 2. isolated restore target
  local port pw
  port=$(free_port)
  pw=$(openssl rand -hex 24)
  CONTAINER="dr-verify-$$"
  start_scratch() {
    docker run -d --name "$CONTAINER" \
      -e POSTGRES_PASSWORD="$pw" \
      -p "127.0.0.1:$port:5432" "$DR_PG_IMAGE" >/dev/null
    dr_log INFO "scratch $DR_PG_IMAGE started as $CONTAINER on 127.0.0.1:$port (loopback only)"
    local ready=0 i
    for i in $(seq 1 60); do
      if docker exec "$CONTAINER" pg_isready -q -U postgres >/dev/null 2>&1; then ready=1; break; fi
      sleep 1
    done
    (( ready )) || dr_die "scratch container never became ready"
  }
  timed "scratch container up" start_scratch

  dr_clear_pgenv
  export PGHOST=127.0.0.1 PGPORT="$port" PGUSER=postgres PGPASSWORD="$pw" PGDATABASE=postgres

  local total_restore=0
  local target
  for target in $(jq -r '.targets | keys[]' "$SET_DIR/manifest.json"); do
    timed "restore + assert [$target]" verify_target "$target" || true
  done

  # The three things a `pg_restore` alone does not recover.
  timed "sidecars (netledger buffer, result spool, evidence listing)" verify_sidecars || true
  timed "config snapshot"        verify_config_snapshot || true
  timed "RLS backfill audit"     verify_rls_backfill_audit || true

  local run_t1; run_t1=$(date -u +%s)
  local elapsed=$(( run_t1 - run_t0 ))

  write_report "$set_name" "$elapsed" "$total_restore"

  if (( FAILURES )); then
    dr_alert "restore verification of $set_name FAILED with $FAILURES assertion failure(s) — report: $REPORT"
    return 1
  fi
  dr_log INFO "=== restore verification of $set_name PASSED in ${elapsed}s — report: $REPORT ==="
}

verify_target() {  # $1 = target name
  local name="$1"
  local file sha db
  file=$(jq -r --arg n "$name" '.targets[$n].file' "$SET_DIR/manifest.json")
  sha=$(jq  -r --arg n "$name" '.targets[$n].sha256' "$SET_DIR/manifest.json")
  db="restore_check_$name"

  local actual_sha; actual_sha=$(sha256sum "$SET_DIR/$file" | cut -d' ' -f1)
  if [[ "$actual_sha" == "$sha" ]]; then
    pass "[$name] ciphertext sha256 matches manifest (${sha:0:16}…)"
  else
    fail "[$name] ciphertext sha256 ${actual_sha:0:16}… != manifest ${sha:0:16}…"
    return 1
  fi

  dr_psql -d postgres -c "CREATE DATABASE \"$db\";" >/dev/null

  local t0 t1
  t0=$(date -u +%s)
  set +e
  dr_gpg_decrypt_stdout "$SET_DIR/$file" 2> "$WORK/$name.gpg.err" \
    | "$PG_BIN/pg_restore" --no-owner --no-privileges -d "$db" 2> "$WORK/$name.restore.err"
  local st=("${PIPESTATUS[@]}")
  set -e
  t1=$(date -u +%s)
  local secs=$(( t1 - t0 ))
  total_restore=$(( total_restore + secs ))

  if [[ "${st[0]}" == 0 ]]; then
    pass "[$name] gpg decrypt exit 0"
  else
    fail "[$name] gpg decrypt exit ${st[0]}: $(tail -c 300 "$WORK/$name.gpg.err" | tr '\n' ' ')"
    return 1
  fi
  if [[ "${st[1]}" == 0 && ! -s "$WORK/$name.restore.err" ]]; then
    pass "[$name] pg_restore exit 0, stderr empty, ${secs}s"
  elif [[ "${st[1]}" == 0 ]]; then
    fail "[$name] pg_restore exit 0 but wrote $(wc -l < "$WORK/$name.restore.err") stderr line(s): $(head -c 300 "$WORK/$name.restore.err" | tr '\n' ' ')"
  else
    fail "[$name] pg_restore exit ${st[1]}: $(tail -c 300 "$WORK/$name.restore.err" | tr '\n' ' ')"
    return 1
  fi

  # ── table SET equality (information_schema on BOTH sides) ────────────────
  jq -r --arg n "$name" '.targets[$n].tables[].table' "$SET_DIR/manifest.json" | sort > "$WORK/$name.expected_tables"
  dr_psql -d "$db" -c "$DR_CANONICAL_SETTINGS $DR_SQL_TABLE_LIST" | sort > "$WORK/$name.actual_tables"

  local missing extra
  missing=$(comm -23 "$WORK/$name.expected_tables" "$WORK/$name.actual_tables" | tr '\n' ' ')
  extra=$(comm   -13 "$WORK/$name.expected_tables" "$WORK/$name.actual_tables" | tr '\n' ' ')
  if [[ -z "$missing" && -z "$extra" ]]; then
    pass "[$name] table set identical ($(wc -l < "$WORK/$name.expected_tables") base tables)"
  else
    [[ -n "$missing" ]] && fail "[$name] tables in manifest but MISSING after restore: $missing"
    [[ -n "$extra"   ]] && fail "[$name] tables present after restore but absent from manifest: $extra"
  fi

  # ── per-table row counts ─────────────────────────────────────────────────
  jq -r --arg n "$name" '.targets[$n].tables[] | "\(.table)|\(.rows)"' "$SET_DIR/manifest.json" | sort > "$WORK/$name.expected_counts"
  dr_build_count_sql < "$WORK/$name.actual_tables" > "$WORK/$name.counts.sql"
  dr_psql -d "$db" -f "$WORK/$name.counts.sql" | sort > "$WORK/$name.actual_counts"

  # Count DISTINCT table names that differ, not lines: a table present on one
  # side only produces a single line, and reporting "0 tables differ" next to
  # a failure is the kind of detail that makes an operator distrust the whole
  # report.
  local diffnames diffcount
  diffnames=$(comm -3 "$WORK/$name.expected_counts" "$WORK/$name.actual_counts" | sed 's/^[[:space:]]*//' | cut -d'|' -f1 | sort -u | grep -c . || true)
  diffcount="$diffnames"
  if (( diffcount == 0 )); then
    pass "[$name] all $(wc -l < "$WORK/$name.expected_counts") per-table row counts match the dump-time snapshot exactly"
  else
    fail "[$name] $diffcount table(s) differ in row count — $(comm -3 "$WORK/$name.expected_counts" "$WORK/$name.actual_counts" | head -20 | tr '\n' ' ')"
  fi

  # ── content checksums ────────────────────────────────────────────────────
  local nck=0 ckfail=0 tbl want got
  while IFS='|' read -r tbl want; do
    [[ -n "$tbl" ]] || continue
    nck=$((nck + 1))
    got=$(dr_psql -d "$db" -c "$DR_CANONICAL_SETTINGS $(dr_build_cksum_sql "$tbl")" | cut -d'|' -f2-)
    if [[ "$got" != "$want" ]]; then
      ckfail=$((ckfail + 1))
      fail "[$name] content checksum mismatch on $tbl (manifest ${want:0:12}… restored ${got:0:12}…)"
    fi
  done < <(jq -r --arg n "$name" '.targets[$n].checksums[] | "\(.table)|\(.md5)"' "$SET_DIR/manifest.json")
  (( ckfail == 0 )) && pass "[$name] $nck full-table content checksum(s) match"

  # ── migration head equality ──────────────────────────────────────────────
  # The manifest's head was read inside the dump's own snapshot, so this is an
  # exact comparison, not a version-drift heuristic. Two probes, because
  # PostgreSQL resolves relations at parse time and not every target is
  # Alembic-migrated (the SaaS database is Prisma-migrated and legitimately
  # has no alembic_version).
  local want_head got_head
  want_head=$(jq -r --arg n "$name" '.targets[$n].alembic_head // ""' "$SET_DIR/manifest.json")
  if [[ -z "$want_head" ]]; then
    skip "[$name] migration head not recorded in this manifest (set predates C10) — cannot assert equality"
  elif [[ "$want_head" == "none" ]]; then
    pass "[$name] manifest records no alembic_version relation, and none is required for this target"
  else
    local has_rel
    has_rel=$(dr_psql -d "$db" -c "SELECT coalesce(to_regclass('public.alembic_version')::text, '');" | head -n1)
    if [[ -z "$has_rel" ]]; then
      fail "[$name] manifest records alembic head '$want_head' but the restored database has no alembic_version table"
    else
      got_head=$(dr_psql -d "$db" -c "SELECT coalesce(string_agg(version_num, ',' ORDER BY version_num), 'none') FROM alembic_version;" | head -n1)
      if [[ "$got_head" == "$want_head" ]]; then
        pass "[$name] migration head matches the dump-time snapshot exactly ($got_head)"
      else
        fail "[$name] migration head MISMATCH — manifest '$want_head', restored '$got_head'"
      fi
    fi
  fi

  dr_log INFO "[$name] restored $(wc -l < "$WORK/$name.actual_tables") tables in ${secs}s"
}

# ── Sidecars ───────────────────────────────────────────────────────────────
# A Postgres dump is not the whole recovery point (see the header, assertion
# 7). The producing side records each sidecar in the manifest as present or
# absent WITH A REASON; this restores the present ones and asserts they are
# sound, and reports the absent ones as SKIPs carrying that reason — an
# operator must be able to see what was not proven.
verify_sidecars() {
  local manifest="$SET_DIR/manifest.json"
  local n; n=$(jq -r '.sidecars // [] | length' "$manifest")
  if [[ "$n" == "0" ]]; then
    skip "sidecars: this set records none (produced before C10, or by the legacy host-side dump path)"
    return 0
  fi

  local bundle; bundle=$(jq -r '.sidecar_file // "sidecars.tar.gz.gpg"' "$manifest")
  local dir="$WORK/sidecars"; mkdir -p "$dir"
  local any_present; any_present=$(jq -r '[.sidecars[] | select(.present)] | length' "$manifest")
  if (( any_present > 0 )); then
    if [[ ! -f "$SET_DIR/$bundle" ]]; then
      fail "sidecars: manifest lists $any_present present sidecar(s) but $bundle is not in the set"
      return 1
    fi
    if dr_gpg_decrypt_stdout "$SET_DIR/$bundle" 2> "$WORK/sidecars.gpg.err" | tar -C "$dir" -xzf -; then
      pass "sidecars: $bundle decrypts and unpacks"
    else
      fail "sidecars: $bundle failed to decrypt/unpack: $(tail -c 300 "$WORK/sidecars.gpg.err" | tr '\n' ' ')"
      return 1
    fi
  fi

  # One jq call per field rather than a single `@tsv` row: TAB is an IFS
  # WHITESPACE character, so bash collapses runs of it and an empty field —
  # `.reason` on a captured sidecar is always empty — silently shifts every
  # later column left. The first run of this drill reported "sha256 is missing
  # from the bundle" for exactly that reason.
  local kind present reason f want_sha got_sha
  while IFS= read -r kind; do
    [[ -n "$kind" ]] || continue
    present=$(jq -r --arg k "$kind" '.sidecars[] | select(.kind==$k) | .present | tostring' "$manifest")
    reason=$( jq -r --arg k "$kind" '.sidecars[] | select(.kind==$k) | .reason  // ""' "$manifest")
    f=$(      jq -r --arg k "$kind" '.sidecars[] | select(.kind==$k) | .file    // ""' "$manifest")
    want_sha=$(jq -r --arg k "$kind" '.sidecars[] | select(.kind==$k) | .sha256 // ""' "$manifest")
    if [[ "$present" != "true" ]]; then
      skip "sidecar $kind NOT captured — ${reason:-no reason recorded} (OWNER GATE: mount the queue volume on dr-backup, RUNBOOK §Sidecars)"
      continue
    fi
    if [[ ! -s "$dir/$f" ]]; then
      fail "sidecar $kind: manifest says present but $f is missing from the bundle"
      continue
    fi
    got_sha=$(sha256sum "$dir/$f" | cut -d' ' -f1)
    if [[ -n "$want_sha" && "$got_sha" != "$want_sha" ]]; then
      fail "sidecar $kind: sha256 ${got_sha:0:12}… != manifest ${want_sha:0:12}…"
      continue
    fi
    case "$kind" in
      netledger_buffer|result_spool)
        # A restored queue that is corrupt is worse than an absent one: it
        # would be replayed. `integrity_check` is sqlite's own answer.
        local check counts
        check=$(dr_sqlite_integrity "$dir/$f")
        if [[ "$check" == "ok" ]]; then
          counts=$(dr_sqlite_counts "$dir/$f")
          pass "sidecar $kind restores and passes PRAGMA integrity_check ($counts)"
        else
          fail "sidecar $kind FAILS PRAGMA integrity_check: $check"
        fi ;;
      evidence_listing)
        local want_files got_files want_bytes got_bytes
        want_files=$(jq -r --arg k "$kind" '.sidecars[] | select(.kind==$k) | .files // 0' "$manifest")
        want_bytes=$(jq -r --arg k "$kind" '.sidecars[] | select(.kind==$k) | .blob_bytes // 0' "$manifest")
        got_files=$(grep -c . "$dir/$f" || true)
        got_bytes=$(awk '{s += $2} END {printf "%d", s + 0}' "$dir/$f")
        if [[ "$want_files" == "$got_files" && "$want_bytes" == "$got_bytes" ]]; then
          pass "sidecar $kind restores: $got_files evidence blob(s), $(dr_human "$got_bytes") referenced, matching the manifest"
        else
          fail "sidecar $kind: listing has $got_files file(s) / $got_bytes byte(s), manifest says $want_files / $want_bytes"
        fi ;;
      *)
        pass "sidecar $kind restores ($f, sha256 matches)" ;;
    esac
  done < <(jq -r '.sidecars[].kind' "$manifest")
}

# ── Config snapshot ────────────────────────────────────────────────────────
# Restored FROM THE MANIFEST to a file next to the report, so a recovery has
# the producing service's configuration in front of it instead of
# reconstructing it from memory. Also asserts the snapshot kept its promise:
# NAMES for everything, VALUES only for the declared allowlist.
verify_config_snapshot() {
  local manifest="$SET_DIR/manifest.json"
  local n; n=$(jq -r '.config_snapshot.variable_count // 0' "$manifest")
  if [[ "$n" == "0" ]]; then
    skip "config snapshot: this set records none (produced before C10, or by the legacy host-side dump path)"
    return 0
  fi
  local out="$DR_REPORTS_DIR/config-$(basename "$SET_DIR").json"
  jq '.config_snapshot' "$manifest" > "$out"; chmod 600 "$out"

  local leaked
  leaked=$(jq -r '
      (.config_snapshot.value_allowlist // []) as $allow
      | (.config_snapshot.values // {}) | keys[]
      | select(. as $k | ($allow | index($k)) | not)' "$manifest" | tr '\n' ' ')
  if [[ -n "${leaked// /}" ]]; then
    fail "config snapshot records VALUES for non-allowlisted variable(s): $leaked"
  else
    pass "config snapshot restored to $out ($n variable name(s); values only for the declared allowlist)"
  fi
}

# ── RLS backfill audit ─────────────────────────────────────────────────────
# See the header, assertion 8. Each migration is parsed with Python's `ast`
# so that only REAL SQL string arguments count: b6e5d1c94a72's docstring
# explains at length why a bare `UPDATE dispatch_intents` would be wrong, and
# a grep-based audit would happily report that prose as the bug it warns
# about.
DR_RLS_BACKFILL_KNOWN="${DR_RLS_BACKFILL_KNOWN:-5b9a86717a66_normalise_api_key_status.py|UPDATE|api_keys
6e4a9c8f2d10_versioned_strategy_methods.py|DELETE|strategy_attempt_stats
6e4a9c8f2d10_versioned_strategy_methods.py|INSERT|domain_strategy_methods
6e4a9c8f2d10_versioned_strategy_methods.py|INSERT|scrape_profile_revisions
6e4a9c8f2d10_versioned_strategy_methods.py|UPDATE|domain_strategy_profiles
6e4a9c8f2d10_versioned_strategy_methods.py|UPDATE|strategy_attempt_stats}"

verify_rls_backfill_audit() {
  local repo="${DR_REPO_ROOT:-$(cd "$HERE/../.." && pwd)}"
  local versions="$repo/alembic/versions"
  if [[ ! -d "$versions" ]]; then
    skip "RLS backfill audit: $versions not present (running outside a checkout)"
    return 0
  fi
  local found
  found=$(dr_rls_backfill_scan "$versions" 2>"$WORK/rls_audit.err") || true
  if [[ -s "$WORK/rls_audit.err" ]]; then
    fail "RLS backfill audit could not run: $(tail -c 300 "$WORK/rls_audit.err" | tr '\n' ' ')"
    return 1
  fi
  local n_known n_found new_ones
  n_known=$(printf '%s\n' "$DR_RLS_BACKFILL_KNOWN" | grep -c . || true)
  n_found=$(printf '%s\n' "$found" | grep -c . || true)
  new_ones=$(comm -13 <(printf '%s\n' "$DR_RLS_BACKFILL_KNOWN" | sort) <(printf '%s\n' "$found" | sort) | grep . || true)
  if [[ -n "$new_ones" ]]; then
    fail "RLS backfill audit: NEW migration(s) backfill an RLS-protected table with bare DML — a fresh-restore replay of the chain would silently no-op them: $(printf '%s' "$new_ones" | tr '\n' ' ')"
    return 1
  fi
  pass "RLS backfill audit: $n_found latent backfill(s), all in the known set of $n_known (RUNBOOK §Restoring for real, step 4, carries the operator procedure)"
}

write_report() {  # $1 = set name, $2 = wall seconds, $3 = restore seconds
  local set_name="$1" elapsed="$2" restore_secs="$3"
  local verdict="PASS"; (( FAILURES )) && verdict="FAIL"
  {
    echo "# Restore verification — $set_name"
    echo
    echo "- Verdict: **$verdict** ($FAILURES failed assertion(s), $(printf '%s\n' "${RESULTS[@]}" | grep -c '^SKIP|' || true) skipped)"
    echo "- Run (UTC): $(dr_ts)"
    echo "- Wall clock: ${elapsed}s   |   pg_restore time only: ${restore_secs}s"
    echo "- Backup set: \`$SET_DIR\`"
    echo "- Scratch target: throwaway \`$DR_PG_IMAGE\` container, loopback-only, destroyed at exit"
    echo "- Production access during this job: **none** (the job reads stored ciphertext only)"
    echo
    echo "## Assertions"
    echo
    printf '| Result | Assertion |\n|---|---|\n'
    local r
    for r in "${RESULTS[@]}"; do
      printf '| %s | %s |\n' "${r%%|*}" "$(printf '%s' "${r#*|}" | sed 's/|/\\|/g')"
    done
    echo
    echo "## Step timings (RTO evidence — a number nobody measured is a wish)"
    echo
    printf '| Step | Seconds |\n|---|---:|\n'
    local st
    for st in "${STEPS[@]}"; do
      printf '| %s | %s |\n' "${st%%|*}" "${st##*|}"
    done
    echo
    echo "## Manifest summary (names and counts only — no credential material)"
    echo
    echo '```json'
    jq '{backup_set, created_utc, encryption,
         private_network: (.private_network // null),
         targets: (.targets | map_values({file, bytes, bytes_exported, bytes_on_wire,
                                          sha256, server_version, alembic_head,
                                          table_count: (.tables | length),
                                          total_rows: ([.tables[].rows] | add),
                                          checksum_tables: [.checksums[].table]})),
         sidecars: (.sidecars // []),
         config_variable_count: (.config_snapshot.variable_count // 0)}' \
       "$SET_DIR/manifest.json"
    echo '```'
    echo
    if (( FAILURES )); then
      echo "> **DR-ALERT** — this backup did not verify. Do not treat it as a recovery point."
      echo "> With no pager configured on this host, this report plus the job's non-zero exit"
      echo "> is the alert. Real alert routing is an open owner gate (RUNBOOK.md §Alerting)."
    fi
  } > "$REPORT"
  chmod 600 "$REPORT"
}

main "$@"

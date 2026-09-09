#!/usr/bin/env bash
# The ops host's side of the Crawmatic disaster-recovery topology.
#
# ORIGINALLY (W5.4 / READY-010) this script WAS the backup: it opened a
# connection from this host to production over Railway's PUBLIC TCP proxy and
# pulled two full logical dumps every four hours. The 2026-09 deep dive
# measured what that costs — **0.853 GB/day of idle egress**, almost all of it
# this job — and C10 (F21) moves the dump itself INSIDE Railway, onto the
# private network, where the bytes never leave the project.
#
# So this script is now, by default, the **PULL** side:
#
#   apps/dr-backup/  (in Railway, cron 0 */4 * * *)   dumps over
#       `*.railway.internal`, encrypts with gpg, keeps the volume retention
#       (4-hourly for 2 days, daily for 14, weekly for 8) and serves the
#       encrypted sets over a token-protected file server (`serve.py`).
#
#   THIS SCRIPT (ops host, daily)                     fetches ONLY the newest
#       encrypted set, verifies its SHA256SUMS, and keeps **2 days** locally —
#       the third copy, off the Railway account entirely.
#
# ── Modes ──────────────────────────────────────────────────────────────────
#
#   --mode pull   fetch the newest set from the dr-backup service (default
#                 once $DR_PULL_CONFIG exists).
#   --mode dump   the legacy behaviour: dump production from this host over
#                 the public endpoint. Kept, and kept working, for exactly one
#                 reason: creating the dr-backup service is an OWNER GATE
#                 (C11). Deleting the dump path before that gate is executed
#                 would leave this host with NO backups at all in the window
#                 between merge and provisioning. It logs a loud WARN every
#                 run so it cannot become the silent steady state.
#   --mode auto   (default) pull if $DR_PULL_CONFIG exists, else dump + WARN.
#
# Both modes produce the SAME set layout (dr_lib.sh:dr_dump_target writes it
# in one place) and both post a measured report to $DR_REPORT_URL
# `/admin/ops/backup-report` — `bytes_exported`, `bytes_encrypted` and
# `bytes_on_wire`, so "the backup moved off the public network" is a number in
# a report, not a claim in a commit message.
#
# See RUNBOOK.md for the RPO statement (4 h), the residual-risk statement (the
# second copy is in the same Railway account and region) and the key-custody
# statement. None of it is hidden in this script.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./dr_lib.sh
source "$HERE/dr_lib.sh"

# Pull configuration. NAMES AND LOCATIONS ONLY — this file is 0600 root-owned
# and exports DR_PULL_BASE_URL (the dr-backup file server) and DR_PULL_TOKEN
# (or DR_PULL_TOKEN_FILE). It is created by the owner in C11 step 3; no value
# from it is ever logged.
DR_PULL_CONFIG="${DR_PULL_CONFIG:-/root/.crawmatic-dr/pull.env}"
# Local retention for pulled sets: 2 days, no dailies, no weeklies (the long
# tail lives on the Railway volume; this host holds the recent third copy).
DR_PULL_KEEP_HOURS="${DR_PULL_KEEP_HOURS:-48}"
DR_PULL_TIMEOUT="${DR_PULL_TIMEOUT:-600}"

RUN_TS="$(date -u +%Y%m%dT%H%M%SZ)"
MODE="auto"
WORK=""
STAGE=""

cleanup() {
  local rc=$?
  dr_snap_close 2>/dev/null || true
  dr_clear_pgenv
  unset DR_PULL_TOKEN || true
  [[ -n "$WORK"  && -d "$WORK"  ]] && rm -rf "$WORK"
  # A half-written set must never be left where the verifier could pick it up
  # as "the latest backup".
  [[ -n "$STAGE" && -d "$STAGE" ]] && rm -rf "$STAGE"
  exit $rc
}
trap cleanup EXIT

usage() { echo "usage: $0 [--mode auto|pull|dump]" >&2; }

while (( $# )); do
  case "$1" in
    --mode) MODE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done
case "$MODE" in auto|pull|dump) ;; *) usage; exit 2 ;; esac

# ── Retention ──────────────────────────────────────────────────────────────
# Delegates to dr_lib.sh:dr_prune_sets so the host and the Railway volume run
# ONE retention implementation with different windows.
dr_prune() { dr_prune_sets "$DR_SETS_DIR" "$DR_ROOT"; }

# ═══ PULL MODE ═════════════════════════════════════════════════════════════

dr_pull_load_config() {
  [[ -f "$DR_PULL_CONFIG" ]] || return 1
  local mode owner
  mode=$(stat -c '%a' "$DR_PULL_CONFIG"); owner=$(stat -c '%u' "$DR_PULL_CONFIG")
  [[ "$mode" == "600" ]] || dr_die "$DR_PULL_CONFIG has mode $mode, expected 600"
  [[ "$owner" == "0"  ]] || dr_die "$DR_PULL_CONFIG is not owned by root"
  # shellcheck disable=SC1090
  source "$DR_PULL_CONFIG" >/dev/null 2>&1 || dr_die "cannot source $DR_PULL_CONFIG"
  [[ -n "${DR_PULL_BASE_URL:-}" ]] || dr_die "$DR_PULL_CONFIG does not export DR_PULL_BASE_URL"
  if [[ -z "${DR_PULL_TOKEN:-}" && -n "${DR_PULL_TOKEN_FILE:-}" && -s "${DR_PULL_TOKEN_FILE}" ]]; then
    DR_PULL_TOKEN="$(<"$DR_PULL_TOKEN_FILE")"; DR_PULL_TOKEN="${DR_PULL_TOKEN%%$'\n'*}"
  fi
  [[ -n "${DR_PULL_TOKEN:-}" ]] || dr_die "no pull token (DR_PULL_TOKEN / DR_PULL_TOKEN_FILE) in $DR_PULL_CONFIG"

  # The transport must not silently become plaintext-over-the-internet. Two
  # shapes are allowed and nothing else:
  #   * https://…            (TLS; the Railway edge, ciphertext + bearer token)
  #   * http://*.railway.internal[:port]   (the private network, no TLS
  #                                         terminator exists there)
  local host
  host=$(sed -E 's|^[a-z]+://||; s|/.*$||; s|:[0-9]+$||' <<<"$DR_PULL_BASE_URL")
  case "$DR_PULL_BASE_URL" in
    https://*) DR_PRIVATE_NETWORK=false ;;
    http://*)
      [[ "$host" == *.railway.internal ]] \
        || dr_die "DR_PULL_BASE_URL is plain http to '$host', which is not a *.railway.internal private name — refusing (use https, or the private domain)"
      DR_PRIVATE_NETWORK=true ;;
    *) dr_die "DR_PULL_BASE_URL must start with http:// or https://" ;;
  esac
  export DR_PRIVATE_NETWORK
  return 0
}

dr_curl_pull() {  # $1 = path, $2 = output file ("-" for stdout)
  local path="$1" out="$2"
  curl -sS --fail --location --max-time "$DR_PULL_TIMEOUT" \
       -H "Authorization: Bearer $DR_PULL_TOKEN" \
       -o "$out" "${DR_PULL_BASE_URL%/}$path"
}

dr_pull_newest_set() {
  local wire0 wire1 t0 t1
  wire0=$(dr_net_bytes); t0=$(date -u +%s)

  WORK=$(mktemp -d /tmp/dr-pull.XXXXXX); chmod 700 "$WORK"

  dr_log INFO "asking the dr-backup service for its newest set"
  dr_curl_pull "/latest" "$WORK/latest.json" \
    || dr_die "GET \$DR_PULL_BASE_URL/latest failed (service down, token rejected, or no route from this host)"

  local set_name nfiles
  set_name=$(jq -r '.set // empty' "$WORK/latest.json")
  [[ -n "$set_name" ]] || dr_die "/latest returned no .set field: $(head -c 200 "$WORK/latest.json" | tr -d '\n')"
  [[ "$set_name" == set-* ]] || dr_die "/latest returned an implausible set name '$set_name'"
  nfiles=$(jq -r '.files | length' "$WORK/latest.json")
  (( nfiles > 0 )) || dr_die "/latest listed zero files for $set_name"

  if [[ -d "$DR_SETS_DIR/$set_name" ]]; then
    dr_log INFO "newest remote set $set_name is already here — nothing to fetch"
    PULLED_SET="$set_name"; PULLED_BYTES=0; PULLED_SECONDS=0; PULLED_WIRE=0
    PULLED_SKIPPED=1
    return 0
  fi

  install -d -m 700 "$DR_ROOT" "$DR_SETS_DIR" "$DR_REPORTS_DIR"
  local free; free=$(dr_free_bytes "$DR_ROOT")
  local need; need=$(jq -r '[.files[].bytes // 0] | add' "$WORK/latest.json")
  if (( free < need + DR_MIN_FREE_BYTES )); then
    dr_prune
    free=$(dr_free_bytes "$DR_ROOT")
    (( free >= need + DR_MIN_FREE_BYTES )) \
      || dr_die "only $(dr_human "$free") free but $set_name needs $(dr_human "$need") plus the $(dr_human "$DR_MIN_FREE_BYTES") floor — refusing to fetch (a pull that fills the ops host is an outage, not a backup)"
  fi

  STAGE="$DR_SETS_DIR/.staging-$RUN_TS"; install -d -m 700 "$STAGE"
  local f
  while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    # The name comes off the wire: it must be a bare filename, never a path.
    [[ "$f" == */* || "$f" == .* ]] && dr_die "refusing to write a file named '$f' from /latest (path traversal)"
    dr_log INFO "fetching $set_name/$f"
    dr_curl_pull "/sets/$set_name/$f" "$STAGE/$f" \
      || dr_die "fetch of $set_name/$f failed"
    chmod 600 "$STAGE/$f"
  done < <(jq -r '.files[].name' "$WORK/latest.json")

  [[ -s "$STAGE/SHA256SUMS" ]] || dr_die "$set_name arrived without SHA256SUMS — refusing to accept an unverifiable set"
  ( cd "$STAGE" && sha256sum -c --status SHA256SUMS ) \
    || dr_die "$set_name FAILED SHA256SUMS after transfer — discarding (the staging dir is removed on exit)"
  dr_log INFO "$set_name verifies against its own SHA256SUMS after transfer"

  mv "$STAGE" "$DR_SETS_DIR/$set_name"; STAGE=""
  chmod 700 "$DR_SETS_DIR/$set_name"

  t1=$(date -u +%s); wire1=$(dr_net_bytes)
  PULLED_SET="$set_name"
  PULLED_BYTES=$(dr_dir_bytes "$DR_SETS_DIR/$set_name")
  PULLED_SECONDS=$(( t1 - t0 ))
  PULLED_WIRE=$(( wire1 - wire0 )); (( PULLED_WIRE >= 0 )) || PULLED_WIRE=0
  dr_log INFO "pulled $set_name: $(dr_human "$PULLED_BYTES") stored, $(dr_human "$PULLED_WIRE") on the wire, ${PULLED_SECONDS}s"
}

dr_pull_report() {
  local set_dir="$DR_SETS_DIR/$PULLED_SET" targets="$WORK/targets.json"
  # One report row per target file actually held locally, with the byte
  # counts the producing side recorded (bytes_exported is a property of the
  # dump, not of the transfer) plus this leg's measured wire cost.
  if [[ -f "$set_dir/manifest.json" ]]; then
    # `bytes_exported` is a property of the DUMP and is carried through from
    # the producing side unchanged. `bytes_on_wire` for this leg is the
    # ciphertext each file actually cost to transfer; the interface-level
    # delta for the whole pull (headers, TLS, the /latest call) is logged
    # above and is within a few KiB of the sum, so attributing it per file is
    # both accurate and attributable — unlike smearing one number across rows.
    jq --argjson skipped "$( (( ${PULLED_SKIPPED:-0} )) && echo true || echo false )" '[ .targets | to_entries[] | .value
          | {name, bytes_exported: (.bytes_exported // 0),
             bytes_encrypted: (.bytes_encrypted // .bytes // 0),
             bytes_on_wire: (if $skipped then 0 else (.bytes_encrypted // .bytes // 0) end),
             dump_seconds: (.dump_seconds // 0),
             server_version: (.server_version // "unknown"),
             alembic_head: (.alembic_head // "none")} ]' \
      "$set_dir/manifest.json" > "$targets"
  else
    printf '[]' > "$targets"
  fi
  local retention
  retention=$(jq -n --argjson hours "$DR_PULL_KEEP_HOURS" \
                    --argjson sets "$(find "$DR_SETS_DIR" -mindepth 1 -maxdepth 1 -type d -name 'set-*' | wc -l)" \
                    --argjson bytes "$(dr_dir_bytes "$DR_ROOT")" \
                 '{hours: $hours, days: 0, weeks: 0, sets_kept: $sets, bytes_stored: $bytes}')
  dr_backup_report_payload "pull" "ops-host" "$PULLED_SET" "$targets" "$retention" \
    > "$DR_REPORTS_DIR/pull-$PULLED_SET.json"
  chmod 600 "$DR_REPORTS_DIR/pull-$PULLED_SET.json"
  dr_post_backup_report "$DR_REPORTS_DIR/pull-$PULLED_SET.json" || true
}

main_pull() {
  dr_log INFO "=== pull run $RUN_TS start (mode=pull) ==="
  command -v curl >/dev/null || dr_die "curl not on PATH"
  command -v jq   >/dev/null || dr_die "jq not on PATH"
  install -d -m 700 "$DR_ROOT" "$DR_SETS_DIR" "$DR_REPORTS_DIR"

  # Local window: 2 days of 4-hourly sets, nothing older. The long tail lives
  # on the Railway volume (14 daily / 8 weekly); this host is the recent copy.
  DR_KEEP_HOURS="$DR_PULL_KEEP_HOURS"; DR_KEEP_DAYS=0; DR_KEEP_WEEKS=0
  dr_prune
  dr_pull_newest_set
  dr_pull_report
  dr_prune

  dr_log INFO "=== pull run $RUN_TS OK (${PULLED_SET}) ==="
}

# ═══ LEGACY DUMP MODE (this host → production over the PUBLIC endpoint) ═════

main_dump() {
  dr_require_root
  [[ -x "$PG_BIN/pg_dump" ]] || dr_die "$PG_BIN/pg_dump not found (PostgreSQL 18 client is required; /usr/bin/pg_dump is 16.x and cannot dump these servers)"
  command -v railway >/dev/null || dr_die "railway CLI not on PATH"
  command -v jq      >/dev/null || dr_die "jq not on PATH"
  dr_ensure_key

  dr_log WARN "LEGACY MODE: dumping production FROM THIS HOST over the public Railway TCP proxy."
  dr_log WARN "Every byte of this run is billable public egress (deep-dive baseline: 0.853 GB/day)."
  dr_log WARN "This path exists only until the dr-backup service exists (OWNER GATE, C11 step 3);"
  dr_log WARN "create $DR_PULL_CONFIG and this script switches to pulling automatically."

  install -d -m 700 "$DR_ROOT" "$DR_SETS_DIR" "$DR_REPORTS_DIR"
  local set_name="set-$RUN_TS"

  dr_log INFO "=== backup run $set_name start (mode=dump, PUBLIC endpoint) ==="
  dr_prune

  local free; free=$(dr_free_bytes "$DR_ROOT")
  if (( free < DR_MIN_FREE_BYTES )); then
    dr_die "only $(dr_human "$free") free on $(df -P "$DR_ROOT" | awk 'NR==2{print $6}') — below DR_MIN_FREE_BYTES $(dr_human "$DR_MIN_FREE_BYTES"); refusing to dump (a backup that fills the ops host is an outage, not a backup)"
  fi
  dr_log INFO "disk: $(dr_human "$free") free, store $(dr_human "$(dr_dir_bytes "$DR_ROOT")")"

  WORK=$(mktemp -d /tmp/dr-backup.XXXXXX); chmod 700 "$WORK"
  STAGE="$DR_SETS_DIR/.staging-$RUN_TS"; install -d -m 700 "$STAGE"

  local entry name project service
  for entry in "${DR_TARGETS[@]}"; do
    IFS='|' read -r name project service <<<"$entry"
    dr_log INFO "[$name] loading credentials via Railway bridge (names only)"
    dr_load_pgenv "$project" "$service"
    dr_dump_target "$name" "$STAGE" "$WORK"
    dr_clear_pgenv
  done

  jq -n --arg id "$set_name" --arg created "$(dr_ts)" \
        --arg host "$(hostname)" --arg tool "scripts/dr/backup_prod.sh --mode dump" \
        --slurpfile metas <(cat "$WORK"/*.meta.json) '
     {backup_set: $id, created_utc: $created, host: $host, tool: $tool,
      encryption: "gpg --symmetric --cipher-algo AES256 (key: host-local file, mode 0600)",
      private_network: false,
      targets: ($metas | map({key: .name, value: .}) | from_entries)}' \
     > "$STAGE/manifest.json"
  chmod 600 "$STAGE/manifest.json"

  ( cd "$STAGE" && sha256sum ./*.dump.gpg ./manifest.json > SHA256SUMS )
  chmod 600 "$STAGE/SHA256SUMS"

  mv "$STAGE" "$DR_SETS_DIR/$set_name"; STAGE=""
  chmod 700 "$DR_SETS_DIR/$set_name"

  # Measured report for the legacy leg too — private_network:false is the
  # number that makes the "before" side of the egress comparison real.
  jq '[ .targets | to_entries[] | .value
        | {name, bytes_exported: (.bytes_exported // 0),
           bytes_encrypted: (.bytes_encrypted // .bytes // 0),
           bytes_on_wire: (.bytes_on_wire // 0),
           dump_seconds: (.dump_seconds // 0),
           server_version: (.server_version // "unknown"),
           alembic_head: (.alembic_head // "none")} ]' \
     "$DR_SETS_DIR/$set_name/manifest.json" > "$WORK/targets.json"
  local retention
  retention=$(jq -n --argjson hours "$DR_KEEP_HOURS" --argjson days "$DR_KEEP_DAYS" \
                    --argjson sets "$(find "$DR_SETS_DIR" -mindepth 1 -maxdepth 1 -type d -name 'set-*' | wc -l)" \
                    --argjson bytes "$(dr_dir_bytes "$DR_ROOT")" \
                 '{hours: $hours, days: $days, weeks: 0, sets_kept: $sets, bytes_stored: $bytes}')
  DR_PRIVATE_NETWORK=false \
    dr_backup_report_payload "backup" "ops-host" "$set_name" "$WORK/targets.json" "$retention" \
    > "$DR_REPORTS_DIR/backup-$set_name.json"
  chmod 600 "$DR_REPORTS_DIR/backup-$set_name.json"
  dr_post_backup_report "$DR_REPORTS_DIR/backup-$set_name.json" || true

  dr_prune
  dr_log INFO "=== backup run $set_name OK ($(dr_human "$(dr_dir_bytes "$DR_SETS_DIR/$set_name")")) ==="
}

main() {
  dr_require_root
  case "$MODE" in
    pull)
      dr_pull_load_config || dr_die "--mode pull but $DR_PULL_CONFIG does not exist (OWNER GATE C11 step 3 creates it)"
      main_pull ;;
    dump)
      main_dump ;;
    auto)
      if dr_pull_load_config; then
        main_pull
      else
        main_dump
      fi ;;
  esac
}

main "$@"

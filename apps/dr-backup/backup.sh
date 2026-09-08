#!/usr/bin/env bash
# dr-backup — the Crawmatic backup, running INSIDE Railway on the private
# network (C10 / F21).
#
# WHY THIS SERVICE EXISTS
# -----------------------
# Until now `scripts/dr/backup_prod.sh` ran on the ops host and pulled two full
# logical dumps out of production every four hours across Railway's PUBLIC TCP
# proxy. The 2026-09 deep dive measured the bill for that: **0.853 GB/day of
# idle egress**, essentially all of it this backup. Dumping from a service that
# sits in the same project means the bytes cross `*.railway.internal` and never
# leave the private network — the same backup, the same encryption, without the
# egress.
#
# WHAT IT DOES, EVERY RUN (Railway cron `0 */4 * * *`)
# ---------------------------------------------------
#   1. Refuses to start unless every target's PGHOST is a `*.railway.internal`
#      private name. A public hostname here would silently reintroduce the
#      exact cost this service exists to remove, so it is a hard failure, not
#      a warning. (`tests/unit/test_dr_backup_script_lint.py` enforces the same
#      rule statically, so the check cannot be edited out unnoticed.)
#   2. Dumps each target with `pg_dump -Fc` inside a shared REPEATABLE READ
#      snapshot, straight into `gpg --symmetric` — no plaintext dump ever
#      touches the volume — recording `bytes_exported` (plaintext, measured in
#      the pipe) and `bytes_on_wire` (this network namespace's own counters).
#      Shared with the host implementation: `dr_lib.sh:dr_dump_target`.
#   3. Captures the SIDECARS a database dump does not contain and a restore
#      drill needs: the netledger durable buffer, the B1 result spool, a
#      listing of the C5 evidence store, and a config snapshot (variable NAMES
#      plus a non-secret allowlist of values — never a secret value).
#   4. Applies the volume retention: 4-hourly sets for 2 days, daily for 14,
#      weekly for 8.
#   5. Posts the measured report to `/admin/ops/backup-report`.
#
# WHAT IT NEVER DOES
# ------------------
# Never writes plaintext production data to the volume; never puts a
# credential on argv (PG* only, `dr_lib.sh` design rule 1); never logs a
# secret value (`dr_scrub` runs over every line); never deletes a set outside
# the retention windows above; never reaches the public internet except for
# the report POST, which is itself private when $DR_REPORT_URL is an internal
# name.
#
# The passphrase arrives as the Railway variable **$DR_GPG_PASSPHRASE** (name
# only — the value is set by the owner in C11 step 3 and lives in their
# password manager). It is written to a 0600 file inside the container's own
# tmpfs for the lifetime of the run because `gpg --passphrase-file` is the only
# form that keeps it off argv, and removed in the exit trap.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Container-side configuration ───────────────────────────────────────────
#
# dr_lib.sh's own `${X:-default}` values are shaped for the OPS HOST (a 1 GiB
# store, a 2 GiB free-space floor, /usr/lib/postgresql/18/bin,
# /srv/crawmatic/backups/dr). Once it is sourced those variables are SET, so a
# later `${X:-container-default}` here would silently keep the host's number —
# which is how the smoke run ended up enforcing the host's 1 GiB budget on an
# 8 GiB volume. So the environment's own values are captured FIRST, dr_lib.sh
# is sourced, and the container defaults are then applied unconditionally
# unless the environment (Railway variables / Dockerfile ENV) supplied one.
for _v in DR_ROOT PG_BIN DR_KEEP_HOURS DR_KEEP_DAYS DR_KEEP_WEEKS \
          DR_MIN_KEEP DR_MAX_BYTES DR_MIN_FREE_BYTES; do
  declare "_ENV_$_v=${!_v-}"
done
unset _v

# shellcheck source=../../scripts/dr/dr_lib.sh
source "$APP_DIR/dr_lib.sh"

DR_ROOT="${_ENV_DR_ROOT:-/backups}"
DR_SETS_DIR="$DR_ROOT/sets"
DR_REPORTS_DIR="$DR_ROOT/reports"
PG_BIN="${_ENV_PG_BIN:-/usr/local/bin}"

# Volume retention (C10 acceptance): 4-hourly for 2 days, daily for 14,
# weekly for 8.
DR_KEEP_HOURS="${_ENV_DR_KEEP_HOURS:-48}"
DR_KEEP_DAYS="${_ENV_DR_KEEP_DAYS:-14}"
DR_KEEP_WEEKS="${_ENV_DR_KEEP_WEEKS:-8}"
DR_MIN_KEEP="${_ENV_DR_MIN_KEEP:-3}"
# The volume is sized for this, not for the ops host's 1 GiB: 12 sets/day × 2
# days + 14 + 8 ≈ 46 sets ≈ 1.5 GB at today's 33 MB/set. 8 GiB leaves room for
# growth and still bounds a runaway.
DR_MAX_BYTES="${_ENV_DR_MAX_BYTES:-8589934592}"
DR_MIN_FREE_BYTES="${_ENV_DR_MIN_FREE_BYTES:-536870912}"

# Targets: a space-separated list of names; each name NAME contributes
# NAME_PGHOST / NAME_PGPORT / NAME_PGUSER / NAME_PGPASSWORD / NAME_PGDATABASE,
# set in Railway as variable REFERENCES to each database service's private
# fields (see RUNBOOK §Owner gate). Names only ever appear here.
DR_BACKUP_TARGETS="${DR_BACKUP_TARGETS:-engine saas}"

# Sidecar sources (present only when the volume carrying them is mounted on
# this service — see RUNBOOK §Sidecars).
NETLEDGER_BUFFER_PATH="${NETLEDGER_BUFFER_PATH:-}"
SCRAPE_RESULT_SPOOL_PATH="${SCRAPE_RESULT_SPOOL_PATH:-}"
OFFER_EVIDENCE_STORE_DIR="${OFFER_EVIDENCE_STORE_DIR:-}"

# Config-snapshot value allowlist: variables whose VALUES are safe to record.
# Everything else contributes its NAME only. Deliberately an allowlist, not a
# secret-name denylist — a denylist fails open on the next variable somebody
# adds.
DR_CONFIG_VALUE_ALLOWLIST="${DR_CONFIG_VALUE_ALLOWLIST:-RAILWAY_ENVIRONMENT_NAME RAILWAY_SERVICE_NAME RAILWAY_PROJECT_NAME RAILWAY_REGION DR_BACKUP_TARGETS DR_KEEP_HOURS DR_KEEP_DAYS DR_KEEP_WEEKS DR_MIN_KEEP DR_MAX_BYTES DR_ROOT DR_REPORT_PATH PG_BIN TZ}"

RUN_TS="$(date -u +%Y%m%dT%H%M%SZ)"
SET_NAME="set-$RUN_TS"
WORK=""
STAGE=""
KEYTMP=""

cleanup() {
  local rc=$?
  dr_snap_close 2>/dev/null || true
  dr_clear_pgenv
  [[ -n "$KEYTMP" && -f "$KEYTMP" ]] && rm -f "$KEYTMP"
  [[ -n "$WORK"   && -d "$WORK"   ]] && rm -rf "$WORK"
  [[ -n "$STAGE"  && -d "$STAGE"  ]] && rm -rf "$STAGE"
  exit $rc
}
trap cleanup EXIT

# ── Passphrase ─────────────────────────────────────────────────────────────
dr_materialize_key() {
  if [[ -s "${DR_KEY_FILE:-}" ]]; then
    dr_log INFO "using the mounted key file at \$DR_KEY_FILE"
    return 0
  fi
  [[ -n "${DR_GPG_PASSPHRASE:-}" ]] \
    || dr_die "neither \$DR_KEY_FILE nor \$DR_GPG_PASSPHRASE is set — refusing to write an unencrypted backup"
  KEYTMP="$(mktemp /tmp/dr-key.XXXXXX)"
  chmod 600 "$KEYTMP"
  printf '%s' "$DR_GPG_PASSPHRASE" > "$KEYTMP"
  unset DR_GPG_PASSPHRASE
  DR_KEY_FILE="$KEYTMP"
  dr_log INFO "passphrase materialised from \$DR_GPG_PASSPHRASE into a 0600 file for this run only"
}

# ── Per-target credentials, from this service's own variables ──────────────
dr_load_target_env() {  # $1 = target name
  local name="$1" prefix
  prefix=$(printf '%s' "$name" | tr '[:lower:]-' '[:upper:]_')
  local host port user pass db
  host="$(eval "printf '%s' \"\${${prefix}_PGHOST:-}\"")"
  port="$(eval "printf '%s' \"\${${prefix}_PGPORT:-5432}\"")"
  user="$(eval "printf '%s' \"\${${prefix}_PGUSER:-}\"")"
  pass="$(eval "printf '%s' \"\${${prefix}_PGPASSWORD:-}\"")"
  db="$(  eval "printf '%s' \"\${${prefix}_PGDATABASE:-}\"")"

  [[ -n "$host" && -n "$user" && -n "$pass" && -n "$db" ]] \
    || dr_die "[$name] incomplete variable set (names only: ${prefix}_PGHOST/${prefix}_PGPORT/${prefix}_PGUSER/${prefix}_PGPASSWORD/${prefix}_PGDATABASE)"

  # THE point of this service. A public hostname here means the dump would
  # leave the private network and be billed as egress again.
  [[ "$host" == *.railway.internal ]] \
    || dr_die "[$name] ${prefix}_PGHOST is not a *.railway.internal private name — refusing to dump over a public endpoint (that is the entire reason this service exists)"

  export PGHOST="$host" PGPORT="$port" PGUSER="$user" PGPASSWORD="$pass" PGDATABASE="$db"
  export PGCONNECT_TIMEOUT=20
  dr_log INFO "[$name] connecting over the private network (\$${prefix}_PGHOST, *.railway.internal)"
}

# ── Sidecars ───────────────────────────────────────────────────────────────
#
# A logical dump of Postgres is not the whole recovery point. Three things
# live outside it and a restore that ignores them silently loses work that was
# already paid for:
#
#   netledger buffer  (app_shared.netledger.buffer)  — sub-resource and close
#                     events not yet flushed into the ledger: money already
#                     spent that is not in any table yet.
#   result spool      (scrape_core.result_spool, B1) — scrape results written
#                     durably before persistence: pages already fetched.
#   evidence store    (C5, OFFER_EVIDENCE_STORE_DIR)  — the blobs observations
#                     reference by hash. The BLOBS are not copied here (they
#                     are large and live on their own volume); the LISTING is,
#                     so a restore can prove exactly which hashes existed and
#                     which are missing rather than discovering it later.
#
# Each is recorded as present-or-absent WITH A REASON. An absent sidecar is a
# stated fact in the manifest, never a silent pass in the drill.
dr_capture_sidecars() {  # $1 = stage dir, $2 = work dir
  local stage="$1" work="$2"
  local sc="$work/sidecars"; mkdir -p "$sc"
  local entries="$work/sidecar_entries.json"; : > "$entries"

  _sqlite_snapshot() {  # $1 = src, $2 = dst, $3 = label
    python3 - "$1" "$2" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
# .backup() is the only consistent way to copy a LIVE sqlite/WAL database:
# copying the file while a writer holds the WAL yields a torn snapshot.
with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as s, sqlite3.connect(dst) as d:
    s.backup(d)
PY
  }

  _record() {  # $1 kind, $2 present(true/false), $3 detail json
    jq -n --arg kind "$1" --argjson present "$2" --argjson detail "$3" \
       '{kind: $kind, present: $present} + $detail' >> "$entries"
  }

  # netledger buffer + result spool: consistent SQLite snapshots.
  local pair kind path
  for pair in "netledger_buffer|$NETLEDGER_BUFFER_PATH" "result_spool|$SCRAPE_RESULT_SPOOL_PATH"; do
    kind="${pair%%|*}"; path="${pair#*|}"
    if [[ -z "$path" ]]; then
      _record "$kind" false '{"reason":"path variable unset on this service"}'
      dr_log WARN "sidecar $kind: path variable unset — recorded absent (OWNER GATE: mount the spool volume on dr-backup)"
    elif [[ ! -s "$path" ]]; then
      _record "$kind" false '{"reason":"path set but no file at it (queue never created, or volume not mounted)"}'
      dr_log WARN "sidecar $kind: nothing at the configured path — recorded absent"
    else
      _sqlite_snapshot "$path" "$sc/$kind.sqlite3" "$kind"
      _record "$kind" true "$(jq -n --argjson bytes "$(stat -c '%s' "$sc/$kind.sqlite3")" \
                                    --arg sha "$(sha256sum "$sc/$kind.sqlite3" | cut -d' ' -f1)" \
                                    '{file: "'"$kind"'.sqlite3", bytes: $bytes, sha256: $sha}')"
      dr_log INFO "sidecar $kind captured ($(dr_human "$(stat -c '%s' "$sc/$kind.sqlite3")"))"
    fi
  done

  # evidence store: the LISTING, not the blobs.
  if [[ -z "$OFFER_EVIDENCE_STORE_DIR" || ! -d "$OFFER_EVIDENCE_STORE_DIR" ]]; then
    _record "evidence_listing" false '{"reason":"OFFER_EVIDENCE_STORE_DIR unset or not mounted on this service"}'
    dr_log WARN "sidecar evidence_listing: store dir not available — recorded absent"
  else
    python3 - "$OFFER_EVIDENCE_STORE_DIR" "$sc/evidence_listing.txt" <<'PY'
import os, sys
root, out = sys.argv[1], sys.argv[2]
rows = []
for dirpath, _dirs, files in os.walk(root):
    for f in files:
        p = os.path.join(dirpath, f)
        try:
            rows.append((os.path.relpath(p, root), os.path.getsize(p)))
        except OSError:
            continue
rows.sort()
with open(out, "w", encoding="utf-8") as fh:
    for rel, size in rows:
        fh.write(f"{rel} {size}\n")
PY
    local n bytes
    n=$(wc -l < "$sc/evidence_listing.txt" | tr -d ' ')
    bytes=$(awk '{s += $2} END {printf "%d", s + 0}' "$sc/evidence_listing.txt")
    _record "evidence_listing" true \
      "$(jq -n --argjson files "$n" --argjson blob_bytes "$bytes" \
              --arg sha "$(sha256sum "$sc/evidence_listing.txt" | cut -d' ' -f1)" \
              '{file: "evidence_listing.txt", files: $files, blob_bytes: $blob_bytes, sha256: $sha}')"
    dr_log INFO "sidecar evidence_listing captured ($n blob(s), $(dr_human "$bytes"))"
  fi

  # One encrypted bundle, same key as the dumps.
  if [[ -n "$(ls -A "$sc" 2>/dev/null)" ]]; then
    tar -C "$sc" -czf - . | dr_gpg_encrypt_stdin "$stage/sidecars.tar.gz.gpg"
    chmod 600 "$stage/sidecars.tar.gz.gpg"
    dr_log INFO "sidecars encrypted into sidecars.tar.gz.gpg ($(dr_human "$(stat -c '%s' "$stage/sidecars.tar.gz.gpg")"))"
  else
    dr_log WARN "no sidecar captured this run — sidecars.tar.gz.gpg not written"
  fi

  jq -s '.' "$entries" > "$work/sidecars.json"
}

# ── Config snapshot ────────────────────────────────────────────────────────
# NAMES of every variable this service sees, plus VALUES for the non-secret
# allowlist only. This is what a restore compares against: "the service that
# produced this set was configured like THIS".
dr_config_snapshot() {  # $1 = work dir -> $1/config_snapshot.json
  local work="$1" names allow
  names=$(compgen -e | sort | jq -R . | jq -s .)
  allow=$(
    for v in $DR_CONFIG_VALUE_ALLOWLIST; do
      printf '%s\t%s\n' "$v" "$(eval "printf '%s' \"\${$v:-}\"")"
    done | jq -R 'split("\t") | {key: .[0], value: .[1]}' | jq -s 'from_entries'
  )
  jq -n --argjson names "$names" --argjson values "$allow" \
        --arg captured "$(dr_ts)" \
        --arg allowlist "$DR_CONFIG_VALUE_ALLOWLIST" '
     {captured_utc: $captured,
      variable_names: $names,
      variable_count: ($names | length),
      values: $values,
      value_allowlist: ($allowlist | split(" ")),
      note: "NAMES of every variable, VALUES for the allowlist only — no secret value is ever recorded here"}' \
    > "$work/config_snapshot.json"
}

# ── main ───────────────────────────────────────────────────────────────────
main() {
  [[ -x "$PG_BIN/pg_dump" ]] || dr_die "$PG_BIN/pg_dump not found in this image"
  command -v gpg     >/dev/null || dr_die "gpg not in this image"
  command -v jq      >/dev/null || dr_die "jq not in this image"
  command -v python3 >/dev/null || dr_die "python3 not in this image"

  install -d -m 700 "$DR_ROOT" "$DR_SETS_DIR" "$DR_REPORTS_DIR"
  dr_materialize_key
  dr_ensure_key

  dr_log INFO "=== dr-backup run $SET_NAME start (private network) ==="
  dr_prune_sets "$DR_SETS_DIR" "$DR_ROOT"

  local free; free=$(dr_free_bytes "$DR_ROOT")
  (( free >= DR_MIN_FREE_BYTES )) \
    || dr_die "only $(dr_human "$free") free on the backup volume — below DR_MIN_FREE_BYTES $(dr_human "$DR_MIN_FREE_BYTES"); refusing to dump"
  dr_log INFO "volume: $(dr_human "$free") free, store $(dr_human "$(dr_dir_bytes "$DR_ROOT")")"

  WORK=$(mktemp -d /tmp/dr-backup.XXXXXX); chmod 700 "$WORK"
  STAGE="$DR_SETS_DIR/.staging-$RUN_TS"; install -d -m 700 "$STAGE"

  local name
  for name in $DR_BACKUP_TARGETS; do
    dr_load_target_env "$name"
    dr_dump_target "$name" "$STAGE" "$WORK"
    dr_clear_pgenv
  done

  dr_capture_sidecars "$STAGE" "$WORK"
  dr_config_snapshot "$WORK"

  jq -n --arg id "$SET_NAME" --arg created "$(dr_ts)" \
        --arg host "${RAILWAY_SERVICE_NAME:-dr-backup}" \
        --arg tool "apps/dr-backup/backup.sh" \
        --slurpfile metas <(cat "$WORK"/*.meta.json) \
        --slurpfile sidecars "$WORK/sidecars.json" \
        --slurpfile config "$WORK/config_snapshot.json" '
     {backup_set: $id, created_utc: $created, host: $host, tool: $tool,
      encryption: "gpg --symmetric --cipher-algo AES256 (key: $DR_GPG_PASSPHRASE, owner-held)",
      private_network: true,
      targets: ($metas | map({key: .name, value: .}) | from_entries),
      sidecars: ($sidecars[0] // []),
      sidecar_file: "sidecars.tar.gz.gpg",
      config_snapshot: ($config[0] // {})}' \
     > "$STAGE/manifest.json"
  chmod 600 "$STAGE/manifest.json"

  ( cd "$STAGE" && sha256sum ./*.gpg ./manifest.json > SHA256SUMS )
  chmod 600 "$STAGE/SHA256SUMS"

  mv "$STAGE" "$DR_SETS_DIR/$SET_NAME"; STAGE=""
  chmod 700 "$DR_SETS_DIR/$SET_NAME"

  # ── the measured report ──────────────────────────────────────────────────
  jq '[ .targets | to_entries[] | .value
        | {name, bytes_exported: (.bytes_exported // 0),
           bytes_encrypted: (.bytes_encrypted // .bytes // 0),
           bytes_on_wire: (.bytes_on_wire // 0),
           dump_seconds: (.dump_seconds // 0),
           server_version: (.server_version // "unknown"),
           alembic_head: (.alembic_head // "none")} ]' \
     "$DR_SETS_DIR/$SET_NAME/manifest.json" > "$WORK/targets.json"
  local retention
  retention=$(jq -n --argjson hours "$DR_KEEP_HOURS" --argjson days "$DR_KEEP_DAYS" \
                    --argjson weeks "$DR_KEEP_WEEKS" \
                    --argjson sets "$(find "$DR_SETS_DIR" -mindepth 1 -maxdepth 1 -type d -name 'set-*' | wc -l)" \
                    --argjson bytes "$(dr_dir_bytes "$DR_ROOT")" \
                 '{hours: $hours, days: $days, weeks: $weeks, sets_kept: $sets, bytes_stored: $bytes}')
  DR_PRIVATE_NETWORK=true \
    dr_backup_report_payload "backup" "dr-backup" "$SET_NAME" "$WORK/targets.json" "$retention" \
    > "$DR_REPORTS_DIR/backup-$SET_NAME.json"
  chmod 600 "$DR_REPORTS_DIR/backup-$SET_NAME.json"
  dr_post_backup_report "$DR_REPORTS_DIR/backup-$SET_NAME.json" || true

  dr_prune_sets "$DR_SETS_DIR" "$DR_ROOT"
  dr_log INFO "=== dr-backup run $SET_NAME OK ($(dr_human "$(dr_dir_bytes "$DR_SETS_DIR/$SET_NAME")")) ==="
}

main "$@"

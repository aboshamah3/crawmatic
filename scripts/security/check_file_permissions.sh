#!/usr/bin/env bash
# Credential-file gate. Two modes, because the two useful questions cannot be
# asked in the same place:
#
#   --mode tracked   (CI)   Is a credential-shaped file COMMITTED to git?
#                           This is the only version of the question CI can
#                           answer honestly: `actions/checkout` rewrites every
#                           file to 0644, so a permission check on a CI
#                           checkout would flag the entire tree and prove
#                           nothing about the machine that holds the secrets.
#
#   --mode host      (cron) Is a credential-shaped file on THIS host readable
#                           by group or other? This is the `.cf-key.txt`
#                           class: /srv/crawmatic/.cf-key.txt was found
#                           world-readable (0644) during the 2026-08-25 secret
#                           residue scan (A3) and flagged for the owner.
#
# Exit 0 = clean, 1 = findings, 2 = usage error.
#
# Allowlist: a file of glob patterns (one per line, `#` comments) whose
# matches are reported as ALLOWED rather than failing. Default
# `<root>/.perm-allowlist`. Allowlisting is for files that are provably not
# credentials (fixtures, examples, generated docs) — never for "we know about
# that one".
set -uo pipefail

MODE=""
ROOT=""
ALLOWLIST=""
VERBOSE=0

usage() {
  cat >&2 <<'EOF'
usage: check_file_permissions.sh --mode tracked|host [--root DIR] [--allowlist FILE] [-v]
  --mode tracked   fail if a credential-shaped file is tracked by git under --root
  --mode host      fail if a credential-shaped file under --root is group/other readable
EOF
  exit 2
}

while (( $# )); do
  case "$1" in
    --mode)      MODE="$2"; shift 2 ;;
    --root)      ROOT="$2"; shift 2 ;;
    --allowlist) ALLOWLIST="$2"; shift 2 ;;
    -v|--verbose) VERBOSE=1; shift ;;
    -h|--help)   usage ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done

[[ "$MODE" == "tracked" || "$MODE" == "host" ]] || usage
ROOT="${ROOT:-$(pwd)}"
[[ -d "$ROOT" ]] || { echo "no such directory: $ROOT" >&2; exit 2; }
ROOT="$(cd "$ROOT" && pwd)"
ALLOWLIST="${ALLOWLIST:-$ROOT/.perm-allowlist}"

# ── Patterns ───────────────────────────────────────────────────────────────
# Tier 1: the name alone is enough. Flagged whatever the extension.
STRONG=(
  '.env' '.env.*' '*.pem' '*.key' '*.p12' '*.pfx' '*.p8' '*.jks' '*.keystore'
  'id_rsa*' 'id_dsa*' 'id_ecdsa*' 'id_ed25519*' '.pgpass' '.netrc' '_netrc'
  '.npmrc' '.htpasswd' '*.kdbx' 'service-account*.json' 'serviceaccount*.json'
)
# Tier 2: the name only hints, so source and documentation files are excluded —
# `webhookSecret.ts` is code, `.cf-key.txt` is a credential.
HINT=( '*key*' '*secret*' '*credential*' '*token*' '*password*' '*passwd*' )
CODE_EXT='ts|tsx|js|jsx|mjs|cjs|py|php|go|rs|java|rb|kt|kts|swift|c|h|hpp|cpp|cs|sql|html|htm|css|scss|less|vue|svelte|md|rst|po|pot|snap|map|lock|dart'
# Binary/media formats a credential is never stored in. A screenshot named
# `…-key-reveal.png` is a screenshot; policing its mode teaches operators to
# ignore this gate, which is worse than not having it.
MEDIA_EXT='png|jpe?g|gif|svg|webp|avif|ico|bmp|pdf|zip|gz|tgz|bz2|xz|tar|mp4|mov|webm|mp3|wav|woff2?|ttf|otf|eot|so|dylib|dll|exe|whl|jar'
# Never a real credential by naming convention.
BENIGN_SUFFIX='\.(example|sample|template|dist|default)$|\.example\.|\.sample\.'
# Directories with no business being scanned.
PRUNE='node_modules|\.git|\.wasp|vendor|dist/|build/|__pycache__|\.venv|\.mypy_cache|\.pytest_cache|graphify-out|/\.claude/'

matches_any() {  # $1 = basename, $2.. = globs
  local base="$1"; shift
  local g
  for g in "$@"; do
    # shellcheck disable=SC2053
    [[ "$base" == $g ]] && return 0
  done
  return 1
}

is_credential_shaped() {  # $1 = path
  local path="$1" base ext
  base="$(basename "$path")"
  [[ "$base" =~ $BENIGN_SUFFIX ]] && return 1
  matches_any "$base" "${STRONG[@]}" && return 0
  ext="${base##*.}"
  [[ "$base" == "$ext" ]] && ext=""
  [[ -n "$ext" && "$ext" =~ ^($CODE_EXT|$MEDIA_EXT)$ ]] && return 1
  local lower="${base,,}"
  matches_any "$lower" "${HINT[@]}" && return 0
  return 1
}

load_allowlist() {
  ALLOW=()
  [[ -f "$ALLOWLIST" ]] || return 0
  local line
  while IFS= read -r line; do
    line="${line%%#*}"; line="${line#"${line%%[![:space:]]*}"}"; line="${line%"${line##*[![:space:]]}"}"
    [[ -n "$line" ]] && ALLOW+=("$line")
  done < "$ALLOWLIST"
}

is_allowed() {  # $1 = path relative to ROOT
  local rel="$1" p
  for p in "${ALLOW[@]+"${ALLOW[@]}"}"; do
    # shellcheck disable=SC2053
    [[ "$rel" == $p ]] && return 0
  done
  return 1
}

FINDINGS=0
ALLOWED=0
SCANNED=0

report() {  # $1 = rel path, $2 = detail
  local rel="$1" detail="$2"
  if is_allowed "$rel"; then
    ALLOWED=$((ALLOWED + 1))
    (( VERBOSE )) && echo "ALLOW  $rel  ($detail)"
    return
  fi
  FINDINGS=$((FINDINGS + 1))
  echo "FAIL   $rel  ($detail)"
}

load_allowlist

if [[ "$MODE" == "tracked" ]]; then
  git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1 || { echo "not a git repository: $ROOT" >&2; exit 2; }
  while IFS= read -r rel; do
    [[ -n "$rel" ]] || continue
    [[ "$rel" =~ $PRUNE ]] && continue
    SCANNED=$((SCANNED + 1))
    is_credential_shaped "$rel" && report "$rel" "credential-shaped filename is TRACKED in git"
  done < <(git -C "$ROOT" ls-files -z | tr '\0' '\n')
  echo "checked $SCANNED tracked file(s) under $ROOT: $FINDINGS finding(s), $ALLOWED allowlisted"
else
  # The name filter and the permission filter are pushed into `find` itself:
  # a bash loop over every file in /srv/crawmatic takes minutes, and a daily
  # gate nobody waits for is a daily gate nobody runs.
  find_args=( "$ROOT" -xdev
    '(' -type d '(' -name node_modules -o -name .git -o -name .wasp -o -name vendor -o -name .claude
        -o -name dist -o -name build -o -name __pycache__ -o -name '.venv*'
        -o -name graphify-out -o -name .mypy_cache -o -name .pytest_cache ')' -prune ')'
    -o '(' -type f -perm /066 '(' )
  first=1
  for g in "${STRONG[@]}" "${HINT[@]}"; do
    (( first )) || find_args+=( -o )
    first=0
    find_args+=( -iname "$g" )
  done
  find_args+=( ')' -print ')' )

  while IFS= read -r path; do
    [[ -n "$path" ]] || continue
    [[ "$path" =~ $PRUNE ]] && continue
    local_rel="${path#"$ROOT"/}"
    SCANNED=$((SCANNED + 1))
    if is_credential_shaped "$path"; then
      mode="$(stat -c '%a' "$path" 2>/dev/null)" || continue
      # Group- or other-readable (or writable) is the failure condition:
      # the low three octal digits masked with 066.
      perm="${mode: -3}"
      if (( 8#$perm & 8#066 )); then
        report "$local_rel" "mode $mode, owner $(stat -c '%U:%G' "$path") — readable beyond its owner"
      fi
    fi
  done < <(find "${find_args[@]}" 2>/dev/null)
  echo "checked $SCANNED credential-shaped, group/other-readable candidate(s) under $ROOT: $FINDINGS finding(s), $ALLOWED allowlisted"
fi

if (( FINDINGS )); then
  echo
  echo "FILE-PERMISSION GATE FAILED: $FINDINGS credential-shaped file(s) above."
  echo "Fix the file, or — only if it provably holds no credential — add its path to $ALLOWLIST."
  exit 1
fi
echo "FILE-PERMISSION GATE PASSED"
exit 0

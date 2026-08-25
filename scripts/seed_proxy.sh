#!/usr/bin/env bash
# seed_proxy.sh — one-shot production seed of the DataImpulse proxy
# (SPEC-10 access stack). Run by hand from the repo root, like
# scripts/seed_bootstrap.py:
#
#   bash scripts/seed_proxy.sh
#
# Reads PROXY_LOGIN / PROXY_PASSWORD / PROXY_HOST / PROXY_PORT /
# PROXY_COUNTRY from .env (or the environment). Encrypts the password
# with the *production* ENCRYPTION_KEYS via `railway run` (the key is
# never printed), then upserts through `railway connect postgres`:
#
#   * global ProxyProvider  "dataimpulse-residential" (workspace_id NULL)
#   * global AccessPolicy   "global_default" (DIRECT_THEN_PROXY,
#     proxy-on-retry, browser fallback, rotate-per-request)
#
# Idempotent: re-running refreshes the provider credentials in place
# and never duplicates either row (partial unique indexes on the
# NULL-workspace names back this).
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

: "${PROXY_LOGIN:?PROXY_LOGIN is required (set it in .env)}"
: "${PROXY_PASSWORD:?PROXY_PASSWORD is required (set it in .env)}"
PROXY_HOST="${PROXY_HOST:-gw.dataimpulse.com}"
PROXY_PORT="${PROXY_PORT:-823}"
PROXY_COUNTRY="${PROXY_COUNTRY:-SA}"
PROVIDER_NAME="${PROVIDER_NAME:-dataimpulse-residential}"
# DataImpulse geo-targeting lives in the username: login__cr.<cc>.
# The spider sends `ProxyProvider.username` verbatim in
# Proxy-Authorization, so the suffix must be baked in here.
LOWER_CC="$(printf '%s' "$PROXY_COUNTRY" | tr '[:upper:]' '[:lower:]')"
PROXY_USERNAME="${PROXY_LOGIN}__cr.${LOWER_CC}"
MONTHLY_BUDGET="${MONTHLY_BUDGET:-60000}"   # request quota (~2k/day headroom)

echo "==> encrypting password with production ENCRYPTION_KEYS (via railway run)"
CIPHER="$(PROXY_PASSWORD="$PROXY_PASSWORD" railway run --service api -- \
  uv run python scripts/encrypt_proxy_password.py | tr -d '[:space:]')"
KEY_VERSION="${CIPHER%%|*}"
TOKEN="${CIPHER#*|}"
if [[ -z "$KEY_VERSION" || -z "$TOKEN" || "$CIPHER" != *"|"* ]]; then
  echo "encryption step failed: got '$CIPHER'" >&2
  exit 1
fi
echo "==> encrypted with key version ${KEY_VERSION}"

echo "==> upserting proxy_providers + access_policies in production"
railway connect postgres <<SQL
\\set ON_ERROR_STOP on
BEGIN;

INSERT INTO proxy_providers (
    id, workspace_id, name, type, base_url, username,
    password_encrypted, password_key_version, country_code, status,
    monthly_budget_limit, created_at, updated_at
)
SELECT gen_random_uuid(), NULL, '${PROVIDER_NAME}', 'RESIDENTIAL',
       'http://${PROXY_HOST}:${PROXY_PORT}', '${PROXY_USERNAME}',
       '${TOKEN}', ${KEY_VERSION}, '${PROXY_COUNTRY}', 'ACTIVE',
       ${MONTHLY_BUDGET}, now(), now()
WHERE NOT EXISTS (
    SELECT 1 FROM proxy_providers
    WHERE workspace_id IS NULL AND name = '${PROVIDER_NAME}'
);

UPDATE proxy_providers SET
    base_url = 'http://${PROXY_HOST}:${PROXY_PORT}',
    username = '${PROXY_USERNAME}',
    password_encrypted = '${TOKEN}',
    password_key_version = ${KEY_VERSION},
    country_code = '${PROXY_COUNTRY}',
    status = 'ACTIVE',
    monthly_budget_limit = ${MONTHLY_BUDGET},
    updated_at = now()
WHERE workspace_id IS NULL AND name = '${PROVIDER_NAME}';

INSERT INTO access_policies (
    id, workspace_id, name, strategy, provider_id, country_code,
    use_proxy_on_first_attempt, use_proxy_on_retry,
    allow_browser_fallback, max_retries, rotate_per_request,
    sticky_session, session_ttl_minutes, max_requests_per_minute,
    max_requests_per_hour, max_requests_per_day, timeout_ms,
    created_at, updated_at
)
SELECT gen_random_uuid(), NULL, 'global_default', 'DIRECT_THEN_PROXY',
       p.id, '${PROXY_COUNTRY}', false, true, true, 2, true, false,
       NULL, NULL, NULL, NULL, 30000, now(), now()
FROM proxy_providers p
WHERE p.workspace_id IS NULL AND p.name = '${PROVIDER_NAME}'
  AND NOT EXISTS (
      SELECT 1 FROM access_policies
      WHERE workspace_id IS NULL AND name = 'global_default'
  );

UPDATE access_policies SET
    provider_id = (SELECT id FROM proxy_providers
                   WHERE workspace_id IS NULL AND name = '${PROVIDER_NAME}'),
    updated_at = now()
WHERE workspace_id IS NULL AND name = 'global_default';

COMMIT;

SELECT name, type, base_url, username, country_code, status,
       monthly_budget_limit
FROM proxy_providers WHERE workspace_id IS NULL;
SELECT name, strategy, use_proxy_on_retry, allow_browser_fallback,
       max_retries, rotate_per_request
FROM access_policies WHERE workspace_id IS NULL;
SQL

echo "==> done"

# Runbook: reissuing connector keys after a `CONNECTOR_SCOPES` widening

> Closes review finding **R03** (2026-09-09), acceptance criterion 3:
> *"A DELIBERATE migration/reissue strategy for existing keys exists and is
> documented."*
>
> Source of truth for the scope set itself:
> [`apps/api/app/routers/admin.py`](../../apps/api/app/routers/admin.py)
> (`CONNECTOR_SCOPES`). Proof of the grant and its workspace bound:
> `tests/unit/test_connector_key_live_checks.py`.

**Production database access is READ-ONLY for diagnosis.** Every write step
below goes through the admin HTTP surface, never through SQL.

---

## 0. Why this runbook exists

`create_connector_key` writes `scopes=list(CONNECTOR_SCOPES)` **onto the
`api_keys` row** at mint time. It does not store a reference to the list, and
authentication reads the row, not the constant. That is deliberate — it means a
widening of `CONNECTOR_SCOPES` can never silently grow the privileges of a
credential that is already sitting on a merchant's WordPress host.

The cost of that safety is this runbook: after R03 added `jobs:read` and
`jobs:write`, **every connector key minted before that deploy still carries the
old, seven-scope set**, and the plugin's "refresh prices now" button keeps
answering `403` on those stores until the key is replaced. Nothing self-heals
in the engine; the engine has no way to reach into a WordPress install and swap
a secret. The reissue is driven from the SaaS side, and the trigger is either a
merchant action or an operator sweep.

There is no in-place scope upgrade, and one must not be added. Mutating
`api_keys.scopes` for a live key would (a) widen a credential whose holder never
re-consented, (b) leave no audit row saying when the grant changed, and (c) make
the "widening the list does not widen issued keys" invariant — which
`test_minted_keys_carry_their_scopes_on_the_row_not_by_reference` pins — false.
**Mint a new key and revoke the old one. Always.**

---

## 1. Who gets a working key, and how {#who-gets-a-working-key}

Three paths exist, in increasing operator effort. All three end in the same
engine calls: `POST /v1/admin/workspaces/{workspace_id}/connector-keys` (mint)
followed by `POST /v1/admin/api-keys/{key_id}/revoke` (retire the old one).

### 1.1 Merchant re-pairs the store (default, no operator involvement)

The plugin already tells the merchant to do exactly this. On a `403` from
either live-check call, `CW_Sync::classify_live_check_error()` latches a
`crawmatic_live_check_scope` block and surfaces:

> *"Your Crawmatic connector key predates live-check support, so Crawmatic
> refused this request (403). Re-pair this store — or re-issue its key from the
> Crawmatic notice — to get a key that can run live checks."*

Re-pairing runs the SaaS pairing flow, which mints a fresh connector key with
the current `CONNECTOR_SCOPES` and persists it encrypted against the project.
The plugin's latch clears when a *different* engine credential is stored.

This path is self-service and needs nothing from us. It is also the slowest:
a store only discovers the problem when a merchant presses the button.

### 1.2 Operator-driven reissue for one store

Use when a merchant reports the 403 and will not re-pair, or when support wants
to fix it ahead of the complaint.

```bash
# 1. Confirm the key is actually stale — the list endpoint returns scopes.
curl -sS -H "Authorization: Bearer $SAAS_SERVICE_TOKEN" \
  "$ENGINE_BASE/v1/admin/workspaces/$WORKSPACE_ID/api-keys" \
  | jq '[.items[] | select(.name | startswith("connector:")) | {id, key_prefix, status, scopes}]'
```

A stale key is an `ACTIVE` row whose `scopes` array lacks `jobs:read`/`jobs:write`.

Then have the **SaaS** re-issue (it is the only component that can encrypt and
store the new plaintext against the project, and the plaintext is returned
exactly once and never persisted by the engine). Do not call the engine mint
endpoint by hand for a live store: a key the SaaS did not record is a key the
plugin will never receive and the revoke path can never find.

The SaaS-side entry point is the connector-key issuance registry
(`app/src/connect/connectorKeyIssuance.ts`) driven through the credentials
route; forcing the plugin's credential refresh causes the SaaS to serve the
project's stored key, so **the stored key must be cleared/rotated first** —
serving the same stale key back is what `maybe_self_heal()` already tried and
what put the merchant in front of this runbook.

### 1.3 Fleet sweep (only if the 403 rate is material)

Enumerate every workspace's connector keys through
`GET /v1/admin/workspaces/{workspace_id}/api-keys` (it reports `scopes`,
`status` and `key_prefix`, never the hash or plaintext), bucket the `ACTIVE`
`connector:*` rows by whether `jobs:write` is present, and hand the stale list
to the SaaS to re-issue in batches. Revoke each old key only **after** the SaaS
confirms the replacement is persisted — revoking first takes the store offline
for catalog sync, not just for live checks.

There is no engine-side bulk script for this on purpose: the engine cannot
persist the new plaintext anywhere useful, so a bulk mint driven from the engine
would produce a pile of keys nobody holds.

---

## 2. Rollback

Reissue is additive until the revoke. If a reissued store breaks, the old key is
still `ACTIVE` (unless already revoked) and the SaaS can serve it again. Once
the old key is revoked it is gone — `revoked_at` is set once and the plaintext
was never stored — and the only recovery is another mint.

---

## 3. Verification

A store is fixed when the plugin's live check completes end to end:
`POST /v1/variants/{id}/rescrape` → `202`, then `GET /v1/jobs/{job_id}` →
`200`. Both are proved against the current scope set by
`tests/unit/test_connector_key_live_checks.py`, which also proves the negative
half — a connector key minted for workspace A gets `404` (not `403`) on
workspace B's variant and job rows, so the widened grant stays bounded to the
one workspace the key was minted for.

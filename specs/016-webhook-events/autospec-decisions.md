# Autospec Decisions — SPEC-16 Webhook Events

All questions auto-answered from the master doc (`/srv/crawmatic/PROJECT_SPEC.md`) unless noted.

## specify

- [specify] Q: Is automatic webhook delivery in scope for v1? → A: No — poll-only; delivery/dispatch/retries/signing explicitly out of scope (source: doc §16 Acceptance "No automatic delivery is required yet", §22 webhook_events "Automatic delivery later")
- [specify] Q: How is webhook_events retention/partitioning handled? → A: Born monthly-partitioned by created_at (PK includes created_at); 90-day retention via partition drop by existing SPEC-15 maintenance job; register table into existing registry, no new job (source: doc §29 Partitioning and Retention, §22)
- [specify] Q: What URL validation applies to webhook endpoints? → A: Same SSRF-safe save-time validation as competitor match URLs — http/https only, public host only, reject private/loopback/link-local/unique-local/metadata/internal-hostname/userinfo, validate resolved IP; reuse existing validator (source: doc §11 URL safety validation)
- [specify] Q: What triggers event creation? → A: Domain state changes — alert-state transitions (SPEC-09), scrape job status changes (SPEC-08), strategy changes (SPEC-12); on dedicated Celery `webhook_events` queue, decoupled from source op (source: doc §16 Covers, §26 Celery Queues)
- [specify] Q: What auth scopes gate the API? → A: webhooks:read (list/get), webhooks:write (create/update/delete endpoints) — must be registered in scope catalog (source: doc §API keys scope list)
- [specify] Q: Are events endpoint-filtered at creation in v1? → A: No — events created workspace-wide; endpoint event_types records subscription intent only (no delivery to gate) (source: doc §16 Covers + §22, informed default given no-delivery scope)

## clarify

No critical ambiguities requiring the user. Coverage scan found three Partial areas, all
deferred to plan (grounded in existing codebase enums), none user-answerable better than the code:

- [clarify] Q: Exact webhook_events.status enum values? → A: Deferred to plan; reasonable default a not-delivered state (e.g. PENDING/CREATED) since v1 has no delivery (source: doc §22 lists `status` only; FR-010/FR-011 fix semantics)
- [clarify] Q: Concrete event_type taxonomy + which transitions emit? → A: Deferred to plan; derive from existing enums — alert transitions (SPEC-09 alert_type/state), scrape job status changes (SPEC-08), strategy changes (SPEC-12) (source: doc §16 Covers, §26; grounded in codebase, not user-decidable)
- [clarify] Q: Pagination style? → A: Keyset/cursor over (created_at, id) for stable cross-partition ordering, matching existing list endpoints (source: spec Assumptions; platform convention)

## checklist

- [checklist] Focus derivation → security + API/data-integrity (SSRF, RLS isolation, scopes, pagination, partitioning/retention, decoupled creation, no-delivery boundary). Source: spec domain (auth/data-integration backend, no UI).
- Generated checklists/security-api.md (38 items). All 38 pass against spec.md + plan.md; 0 unresolved. requirements.md (specify) also fully passing. No artifact fixes required — spec already covers every requirement-quality dimension.

## analyze

Report: 0 CRITICAL, 1 HIGH, 1 MEDIUM, 2 LOW. 100% FR→task coverage (19/19). Not blocked. Remediated all:
- [analyze] I1 (HIGH) SSRF DNS contradiction: FR-002 + edge case + SC-002 over-specified save-time "validate against resolved IP", but the mandated reuse validator `validate_competitor_url` does NO DNS resolution (by design — master doc §11 puts DNS re-resolution at fetch/delivery time). → Fixed spec: save-time = string/literal + known-internal-hostname check; DNS re-resolution deferred to delivery-time dispatcher (out of v1). Aligns with doc §11 two-phase model + competitor-URL precedent. Updated CHK002 to match. (source: doc §11; url_safety.py:115 "No DNS resolution")
- [analyze] I2 (MEDIUM) T009 told implementer to mirror WORKSPACE_OWNED_MODELS into check_workspace_scoping.py, but that script imports the set (no hardcoded list). → Reworded T009: edit repository.py only, run guard to verify.
- [analyze] I3 (LOW) `has_secret` mislabeled as reused function → clarified as derived response boolean in ground rules.
- [analyze] I4 (LOW) stale ~line anchors in T033/T034/T035 → added locate-by-function-name caveats.

### analyze re-run (post-remediation)
- I1–I4 all verified RESOLVED. New finding N1 surfaced:
- [analyze] N1 (HIGH) strategy seam mis-anchored: T035 assumed `apply_promotion`/`apply_rediscovery` run in `flush_stats` returning `promoted`, but both actually fire inside `app_shared/strategy/flush.py::flush_profile` (which returns only `keys_flushed:int`), and `apply_rediscovery` ALSO fires in `tasks_strategy.py::light_recheck` — so the original T035 was unimplementable and would miss the light_recheck DEGRADED path (violating FR-008/SC-003). → Re-anchored to both real sites: (a) flush_profile surfaces genuine transitions (widen `-> int` return) → flush_stats enqueues post-commit; (b) light_recheck enqueues post-commit per `triggered`. Updated T035/T036/T040 + contracts/events.md §3 + plan.md (seam count 4 sites, tasks_strategy.py note). Verified against live code (flush.py:271/306, promotion.py:148, rediscovery.py:435→bool, light_recheck:665/675). Final focused re-run: 0 CRITICAL / 0 HIGH remaining.

## implement (6 phases, one sonnet subagent each)

- P1 Setup (T001-T006): 2 StrEnums + task-name constant; verified scopes/registry/config invariants untouched. 43 tests.
- P2 Foundational (T007-T013): models/webhooks.py (WebhookEndpoint plain + WebhookEvent born-partitioned composite PK), migration 03dec3037c8f (down_revision 4a1dca402f78, both tables + child partitions + RLS), WORKSPACE_OWNED_MODELS registration. Single head green.
- P3 US1 poll API (T014-T020): GET /v1/webhook-events (keyset pagination reused, event_type filter, 422 INVALID_CURSOR) + GET /{id} (404). 1722 unit passed.
- P4 US2 endpoint CRUD (T021-T028): POST/GET/GET{id}/PATCH/DELETE /v1/webhook-endpoints; reused validate_competitor_url (UNSAFE_URL 422) + Fernet encrypt_secret; WebhookEndpointResponse exposes has_secret only (guard test). 1741 unit passed.
- P5 US3 event creation (T029-T037): payload builders (<8KiB guard), create_webhook_event task on new webhook_events queue, 4 enqueue seams. N1 strategy fix implemented: flush_profile widened to FlushResult(keys_flushed, transitions); flush_stats + light_recheck both enqueue POST-commit by-name (_enqueue_strategy_transition, try/except fire-and-forget), covering the previously-missed light_recheck DEGRADED path. 1767 unit passed.
- P6 Polish (T038-T044): ruff (env-limited note), scoping guard OK, import-boundary + partition-registry (len==4) + retention (webhook_events:90) + single-head guards green. FULL SUITE: 1770 passed / 293 skipped / 0 failed / 0 errors. Integration webhook/live cases skip cleanly (no live Postgres/Redis/Celery in build env).
- Deferred live verifications: T037 webhook event-creation-at-seams live; T020/T028 poll pagination-across-partitions + RLS cross-workspace denial + migration round-trip + partition create/drop (all skipif-guarded, require live stack).

## converge

CONVERGED (1 cycle) — all 19 FR + 7 SC + US1/US2/US3 acceptance scenarios verified against built code; 0 gaps (missing 0 / partial 0 / contradicts 0 / unrequested 0). tasks.md unchanged (no convergence phase appended). N1 strategy fix confirmed firing from BOTH flush_stats/flush_profile (promotion→ACTIVE, rediscovery→DEGRADED) and light_recheck (DEGRADED) post-commit. Retention reuses SPEC-15 machinery, no new scheduler. Final suite: 1770 passed / 293 skipped / 0 failed / 0 errors. Only live-env verification deferred (skipif-guarded).

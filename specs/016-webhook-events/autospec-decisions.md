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

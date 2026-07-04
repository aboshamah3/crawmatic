# Autospec decisions — SPEC-10 (Access Policies, Proxies, Request Attempts)

Log of every question auto-answered from the master doc (`/srv/crawmatic/PROJECT_SPEC.md`)
during the autospec pipeline, and every default chosen where the doc was silent.

## specify

- [specify] Q: Feature short name / directory → A: `010-access-policies-proxies` (source: doc §35 item 10 title; sequential numbering per .specify/init-options.json)
- [specify] Q: How to slice user stories → A: P1 access-config CRUD (policies + proxies, encrypted, isolated), P2 resolution + proxy assignment engine, P3 partitioned attempt logging (source: doc §35 acceptance criteria order + §22 models)
- [specify] Q: Encryption approach for proxy credentials → A: application-level symmetric encryption at rest with key version for rotation (source: doc §22 `password_encrypted`, §33 Security & Secrets; default: key-version detail where doc silent)
- [specify] Q: Budget enforcement mechanism → A: Redis usage counters incremented per proxied request, reset monthly; never count request_attempts rows; fallback or LIMIT_REACHED on exhaustion (source: doc §22 config guardrails line 1580)
- [specify] Q: Scope of rate limiting here vs distributed limiter → A: enforce only the policy's own per-min/hour/day ceilings + domain cooldown/concurrency; cluster-wide limiter deferred to spec 011 (source: doc §35 item 11 = separate increment)
- [specify] Q: Scope of browser fallback → A: express via policy flag/strategy only; actual browser rendering is spec 014 (source: doc §35 item 14; default for boundary)
- [specify] Q: request_attempts partitioning → A: monthly-partitioned from birth by created_at, PK includes partition key, soft references only (source: doc §22 lines 1241, 1880-1887, principle 25)

## clarify

No operator interruption required — all clarifications resolved from the master doc.

- [clarify] Q: allowed access_method values → A: DIRECT_HTTP, DIRECT_HTTP_RETRY, PROXY_HTTP, PLAYWRIGHT_PROXY; no external scraping APIs (source: doc §11)
- [clarify] Q: strategy→attempt-sequence mapping → A: Direct → Direct retry(backoff) → Proxy HTTP → Playwright-via-proxy → fail; learned domains start from preferred method (source: doc §11)
- [clarify] Q: proxy credential encryption + rotation → A: Fernet symmetric, key in env var, key_version column; decrypt-old/re-encrypt/retire rotation (source: doc §33)
- [clarify] Q: proxy_providers.status enum → A: ACTIVE/DISABLED default (source: doc silent; low-impact default, confirmable in planning)

## checklist

Generated 2 domain checklists (security.md 15 items, data-integrity.md 17 items) plus
requirements.md (16 items) — all 48 items pass after one remediation.

- [checklist] Q: which tables are dual-scope vs tenant-only? → A: proxy_providers + access_policies dual-scope (nullable workspace_id, global read-only default); domain_access_rules + request_attempts tenant-only (non-null workspace_id) (source: doc §22 column nullability + plan research D2). Remediation: rewrote FR-006 to state the two isolation shapes explicitly (was uniformly "dual-scope", which contradicted §22 for domain_access_rules/request_attempts).

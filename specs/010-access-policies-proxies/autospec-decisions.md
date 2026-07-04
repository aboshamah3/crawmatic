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

## analyze

0 CRITICAL. Triaged 1 HIGH + 2 MEDIUM + 3 LOW; remediated all in-artifact (no user input).

- [analyze] G1 (HIGH): rate-ceiling/cooldown module (T024) built + tested but not wired into the fetch path → FR-011 unenforced at runtime. Remediation: wired check_rate_ceilings + check_domain_cooldown into the spider request seam before every dispatch (T026 + spider-integration contract §2), reporting RATE_LIMITED; added RATE_LIMITED + LIMIT_REACHED assertions to T032.
- [analyze] I1 (MEDIUM): FR-011 said MUST enforce per-domain concurrency, but plan defers it to SPEC-11. Remediation: reworded FR-011 to enforce ceilings+cooldown here, mark max_concurrent_requests as intent-only (SPEC-11).
- [analyze] A1 (MEDIUM): workspace-default policy selection undefined. Remediation: pinned reserved-name convention — workspace default = policy named `default`, fallback to `global_default`; neither → NONE_RESOLVED (skip). Updated FR-007, T025, policy-resolution contract (backed by existing partial-unique (workspace_id,name)).
- [analyze] A2 (LOW): RESIDENTIAL_ONLY didn't filter by ProxyType. Remediation: assign_proxy takes `strategy`, restricts to ProxyType.RESIDENTIAL for RESIDENTIAL_ONLY; updated FR-009, T022/T028, access-engine contract.
- [analyze] U1 (LOW): block_detection_rules stored-but-unused. Remediation: FR-004 notes it is config-only this increment (consumer deferred).
- [analyze] U2 (LOW): budget-exhaustion had no integration assertion. Remediation: added LIMIT_REACHED case to T032.

Re-ran analyze: all 6 resolved, 0 CRITICAL/0 HIGH. One new MEDIUM surfaced:

- [analyze re-run] C1 (MEDIUM): domain-rule `max_requests_per_minute` stored but not enforced. Remediation (enforce, not defer): domain-rule per-minute overrides the policy per-minute ceiling for that domain; updated FR-011 + T026.

## implement + converge

Implemented in 6 sonnet subagents (one per phase), 39 tasks; committed per phase.
- [implement] Q: now-required ENCRYPTION_KEYS broke pre-existing Settings-constructing tests → A: added the key to affected env fixtures + updated scope-vocabulary test, per the SPEC-08 precedent (source: repo convention)
- [implement] Q: request_attempts new table? → A: no — reused SPEC-07's existing partitioned table; US3 = wiring finish only (source: plan D1)
- [converge] CONVERGED — 0 findings (0 CRITICAL/HIGH/MEDIUM/LOW); tasks.md unchanged. Live-stack verifications deferred (no container engine), authored + skip-clean per SPEC-05→09 pattern.

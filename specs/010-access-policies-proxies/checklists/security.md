# Security Requirements Quality Checklist: Access Policies, Proxies & Request Attempts

**Purpose**: Validate that the security & secrets requirements (credential encryption, plaintext
non-leakage, SSRF URL validation, proxy-budget enforcement) are complete, clear, and consistent
before implementation.
**Created**: 2026-07-04
**Feature**: [spec.md](../spec.md)

## Credential Protection

- [x] CHK001 Is the encryption mechanism for proxy credentials specified with a concrete algorithm and key source? [Clarity, Spec §Clarifications, FR-003]
- [x] CHK002 Is the requirement that plaintext passwords are never returned via any API response explicit? [Completeness, Spec §FR-003]
- [x] CHK003 Are key-rotation requirements (decrypt old version, re-encrypt, retire) defined and measurable? [Completeness, Spec §FR-003]
- [x] CHK004 Is a `key_version` (or equivalent versioning) requirement defined so rotation is verifiable per record? [Clarity, Spec §FR-003]
- [x] CHK005 Is the failure behavior specified when the encryption key is missing/unreadable (operational error, never plaintext leakage)? [Edge Case, Spec §Edge Cases]

## SSRF / URL Safety

- [x] CHK006 Are the URL-safety deny rules (scheme, private ranges, loopback, link-local, unique-local, metadata, internal hostnames, userinfo) enumerated? [Completeness, Spec §FR-005]
- [x] CHK007 Is validation required at BOTH save time and fetch time, including DNS re-resolution and per-redirect-hop re-validation? [Coverage, Spec §FR-005]
- [x] CHK008 Is the URL-safety requirement applied to proxy `base_url` specifically (not only competitor URLs)? [Consistency, Spec §FR-005]
- [x] CHK009 Is the rejection outcome for an unsafe proxy `base_url` at save time defined (validation error)? [Clarity, Spec §US1 AC5]

## Proxy Budget & Rate Enforcement

- [x] CHK010 Is the monthly proxy-budget enforcement mechanism specified as Redis counters (never a request_attempts row scan)? [Clarity, Spec §FR-010]
- [x] CHK011 Is the behavior on budget exhaustion defined (fallback per strategy or fail LIMIT_REACHED)? [Completeness, Spec §FR-010]
- [x] CHK012 Are the policy rate ceilings (per-minute/hour/day) and per-domain cooldown/concurrency enforcement requirements defined with the reported error (RATE_LIMITED)? [Completeness, Spec §FR-011]
- [x] CHK013 Is the boundary with the deferred cluster-wide rate limiter (spec 011) explicitly excluded from scope? [Scope, Spec §Assumptions]

## Workspace Isolation (security dimension)

- [x] CHK014 Are cross-workspace read/write denial and no-context-zero-rows requirements stated for all access-config and attempt data? [Coverage, Spec §FR-006, SC-005]
- [x] CHK015 Is the dual-scope (global-readable default vs tenant-owned) vs tenant-only distinction unambiguous per table? [Consistency, Spec §FR-006, plan.md]

## Notes

- Check items off as completed: `[x]`
- Items test requirement quality, not implementation.

# Contract: Access-attempt engine (`app_shared.access.engine`)

Pure, stdlib-only. Turns a resolved policy + attempt history into the next transport decision
(US2 behavioral core, FR-008/009). Exhaustively unit-testable — the SC-001 guarantee that a
scrape follows the configured sequence in 100% of runs lives here, not in the spider.

## API

```python
@dataclass(frozen=True)
class AttemptPlan:
    access_method: AccessMethod         # DIRECT_HTTP | DIRECT_HTTP_RETRY | PROXY_HTTP | PLAYWRIGHT_PROXY
    use_proxy: bool

STOP = _Stop()   # falsy sentinel: no further attempt; caller finalizes the outcome

def next_attempt(
    strategy: AccessStrategy, *,
    attempt_number: int,                # 1-based
    max_retries: int,
    use_proxy_on_first_attempt: bool,
    use_proxy_on_retry: bool,
    allow_browser_fallback: bool,
    preferred_method: AccessMethod | None = None,   # learned domain start (SPEC-12); None = unknown
    proxy_budget_exhausted: bool = False,
) -> AttemptPlan | _Stop:
    ...

@dataclass(frozen=True)
class ProxyAssignment:
    provider_id: uuid.UUID
    country: str | None
    sticky_key: str | None              # set when sticky_session (session reuse token)

def assign_proxy(
    *, policy_provider_id, policy_country, domain_rule_country,
    visible_providers,                  # {id: (status, country)} own+global
    attempt_number, rotate_per_request, sticky_session, session_seed,
) -> ProxyAssignment | None:
    """Pick provider+country per policy/domain-rule, honoring rotation vs sticky. Returns
    None when the referenced provider is DISABLED/absent (degrade gracefully — caller falls
    back per strategy or fails PROXY_FAILED). rotate_per_request -> new pick each attempt;
    sticky_session -> stable sticky_key derived from session_seed within session TTL."""
```

## Behavior matrix (encoded & tested)

| strategy | attempt 1 | retry (≤ max_retries) | terminal fallback |
|----------|-----------|-----------------------|-------------------|
| `DIRECT_ONLY` | DIRECT_HTTP (no proxy) | DIRECT_HTTP_RETRY (no proxy) | STOP — never a proxy (FR-008 sc.2) |
| `DIRECT_THEN_PROXY` | DIRECT_HTTP (proxy iff `use_proxy_on_first_attempt`) | PROXY_HTTP iff `use_proxy_on_retry` else DIRECT_HTTP_RETRY (FR-008 sc.1) | PLAYWRIGHT_PROXY iff `allow_browser_fallback` else STOP |
| `PROXY_FIRST` | PROXY_HTTP | PROXY_HTTP | PLAYWRIGHT_PROXY iff allowed else STOP |
| `RESIDENTIAL_ONLY` | PROXY_HTTP (residential provider) | PROXY_HTTP | STOP (no direct, no browser unless allowed) |
| `BROWSER_FALLBACK` | DIRECT_HTTP | PROXY_HTTP (when allowed) | PLAYWRIGHT_PROXY |

Cross-cutting rules:
- **Unknown domain default chain** (no `preferred_method`): `DIRECT_HTTP → DIRECT_HTTP_RETRY →
  PROXY_HTTP → PLAYWRIGHT_PROXY → STOP` (§11), gated by the strategy flags above.
- `preferred_method` set → first attempt starts from it (learned domain), remaining chain
  follows (SPEC-12 forward-compat; learning itself is out of scope).
- `max_retries == 0` → `attempt_number == 1` returns the first plan, `attempt_number == 2`
  returns `STOP` (exactly one attempt — Edge Case).
- `proxy_budget_exhausted` → any proxy step is skipped: fall through to the next non-proxy
  step per strategy, or `STOP` (caller maps to `LIMIT_REACHED`, FR-010).
- `PLAYWRIGHT_PROXY` is only ever **returned as intent** — the spider does not render it here
  (SPEC-14); an environment without the browser service treats it as STOP+needs-tuning.

## Acceptance (unit, exhaustive)

- Every (strategy × attempt_number ∈ 1..max_retries+2 × flag combination) yields the matrix
  above; `DIRECT_ONLY` never emits `use_proxy=True` for any input (SC-001).
- `max_retries=0` → one plan then STOP.
- `proxy_budget_exhausted` reroutes/stops correctly for each strategy.
- `assign_proxy` returns `None` when the provider is DISABLED/missing; honors sticky (stable
  key across attempts) vs rotate (differs across attempts).

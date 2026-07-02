# Contract: Scope vocabulary (`app_shared/security/scopes.py`)

The API-key capability vocabulary + membership check (FR-013, §22). Framework-agnostic.

## Exposed symbols

```python
class Scope(StrEnum):
    PRODUCTS_READ = "products:read";     PRODUCTS_WRITE = "products:write"
    VARIANTS_READ = "variants:read";     VARIANTS_WRITE = "variants:write"
    COMPETITORS_READ = "competitors:read"; COMPETITORS_WRITE = "competitors:write"
    MATCHES_READ = "matches:read";       MATCHES_WRITE = "matches:write"
    JOBS_RUN = "jobs:run";               JOBS_READ = "jobs:read"
    RESULTS_READ = "results:read";       ALERTS_READ = "alerts:read"
    WEBHOOKS_READ = "webhooks:read";     WEBHOOKS_WRITE = "webhooks:write"

def validate_scopes(values: Iterable[str]) -> list[str]: ...   # raises ValueError on unknown
def has_scopes(granted: Iterable[str], required: Iterable[str]) -> bool: ...
```

## Guarantees

- `validate_scopes` coerces/validates each value against `Scope`; an unknown scope raises `ValueError` (API-key create → `422`).
- `has_scopes(granted, required)` returns `True` iff every `required` scope is in `granted`. An API-key request needing a scope the key lacks is refused (`403`, FR-013/SC-004).
- These are string-backed values (`StrEnum`), stored in `api_keys.scopes` (JSONB) as plain strings.

## Tests (unit, no DB)

- Full vocabulary present and matches §22.
- `validate_scopes(["products:read"])` ok; `validate_scopes(["bogus:read"])` raises.
- `has_scopes(["a","b"], ["a"]) is True`; `has_scopes(["a"], ["a","b"]) is False`.

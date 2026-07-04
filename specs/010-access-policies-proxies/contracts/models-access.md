# Contract: ORM models & enums (`app_shared.models.access`, `app_shared.enums`)

Declares three ORM classes + three enums. Module declares **ORM shape only** — RLS is emitted
in the creating migration (see `migration-access.md`), matching every prior model module.

## Enums (append to `app_shared/enums.py`)

```python
class AccessStrategy(StrEnum):
    DIRECT_ONLY = "DIRECT_ONLY"
    DIRECT_THEN_PROXY = "DIRECT_THEN_PROXY"
    PROXY_FIRST = "PROXY_FIRST"
    RESIDENTIAL_ONLY = "RESIDENTIAL_ONLY"
    BROWSER_FALLBACK = "BROWSER_FALLBACK"

class ProxyType(StrEnum):
    DATACENTER = "DATACENTER"
    RESIDENTIAL = "RESIDENTIAL"
    MOBILE = "MOBILE"

class ProxyProviderStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
```

`AccessMethod` and `ScrapeErrorCode` are **reused unchanged** (already carry every member this
spec needs).

## Models (`app_shared/models/access.py`)

- `ProxyProvider(Base, TimestampMixin)` — dual-scope. Nullable indexed `workspace_id` (not
  `WorkspaceScopedBase`). `__table_args__`: the two partial-unique `Index`es (per-tenant +
  global), the `ForeignKeyConstraint` on `workspace_id`. Columns per data-model.md;
  `password_encrypted: Text`, `password_key_version: Integer` both nullable; `type`/`status`
  via `enum_column`.
- `AccessPolicy(Base, TimestampMixin)` — dual-scope. Same nullable-workspace pattern;
  `strategy` via `enum_column`; booleans with server-agnostic Python defaults; nullable
  ceiling/session columns; `provider_id` plain nullable `Uuid` (no FK).
- `DomainAccessRule(Base, WorkspaceScopedBase, TimestampMixin)` — tenant-only. `competitor_id`
  indexed `Uuid` (no FK); `access_policy_id` `Uuid` (no FK); `block_detection_rules: JSONB`
  nullable; `enabled: Boolean` default `True`; the composite lookup index + the domain/pattern
  uniqueness (via a partial or `COALESCE(url_pattern,'')` expression index).

## Acceptance

- All enum-like columns render as `VARCHAR` (no Postgres ENUM), verified by a DDL/compile
  assertion like the SPEC-06/09 model tests.
- `mapper_configured` naive-datetime guard passes (all timestamps `TZDateTime`).
- `ProxyProvider`/`AccessPolicy` are **absent** from `WORKSPACE_OWNED_MODELS`;
  `DomainAccessRule` is **present** (guarded by `scripts/check_workspace_scoping.py`).
- Constraint names are deterministic (naming convention) and < 63 bytes.

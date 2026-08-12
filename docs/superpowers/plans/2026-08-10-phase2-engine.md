# Crawmatic Phase 2 — Engine Additions + Public-API Productization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add SaaS admin provisioning + usage-export endpoints to the Crawmatic engine, and productize the existing tenant API (rate limits, error envelope, batch caps, competitor-URL abuse controls, clean external OpenAPI) so a SaaS control plane can provision workspaces, meter usage, and expose the engine as a public customer API.

**Architecture:** One new router `apps/api/app/routers/admin.py` guarded by a **static service token** (not the workspace auth seam), reading across workspaces through the BYPASSRLS `get_auth_session()` path. Usage is aggregated **entirely in SQL** over the monthly-partitioned `request_attempts` (link counts) joined to `competitor_product_matches` (product attribution) and `price_observations` (success determination), keyset-paginated on `(cycle_ts, workspace_id, product_id)`. Productization lands as: a Redis fixed-window per-key limiter in ASGI middleware, exception handlers that add a top-level `{"error": {...}}` envelope **additively** (existing `detail` shape preserved so the 1768 existing tests stay green), `MAX_BULK_ITEMS = 500` guards on the four bulk-upsert endpoints, per-workspace domain + per-product protected-link caps on match/competitor creation, and a `custom_openapi()` that filters internal tags out of the published spec.

**Tech Stack:** Python 3.13, uv workspace monorepo, FastAPI, Pydantic v2, SQLAlchemy 2.x (Core `select()` + `func`), Alembic, Redis (`app_shared.redis_client`), pytest 8 with `TestClient` + `app.dependency_overrides` (no live Postgres for unit tests).

## Global Constraints

- **Branch:** all work on `saas-phase2` (already created off `origin/main`, with Task 2.0 cherry-picked as `1e9402e`). **NEVER push to `main`. NEVER deploy. NEVER touch the live Railway engine or production DB.**
- **Commits:** one commit per task, conventional-commit subject lines, **no attribution/co-author trailer lines**.
- **Usage-export field names are a frozen contract** — the SaaS metering consumer depends on them field-for-field. Exactly: `workspace_id`, `product_id`, `cycle_ts`, `links_total`, `links_succeeded`, `protected_links_attempted`, `protected_links_succeeded`, `check_successful`.
- **Aggregate in SQL, never in Python** (risk P2). The export must not materialize per-attempt rows into Python.
- **Usage window ≤ 31 days per call**, cursor-paginated, idempotent.
- **Backward compatibility is mandatory:** the existing error shape `{"detail": {"error": {"code", "message"}}}` and `{"detail": {"code", "message"}}` must keep working. 1768 unit tests currently pass; the suite must still pass at every task boundary.
- **Repo conventions (non-negotiable):**
  - Workspace-scoped reads go through `app_shared.repository.scoped_select` / `scoped_get`; a deliberate unscoped query carries `# noqa: workspace-scope` (CI guard: `scripts/check_workspace_scoping.py`).
  - Pydantic lives only in `apps/api`; `app_shared` must never import `pydantic` (except `pydantic_settings` in `config.py`).
  - Config: field name == env var name, SCREAMING_SNAKE, no `Field(alias=...)`; each block gets a `# --- <Feature> ---` comment.
  - List endpoints return `{"items": [...], "next_cursor": <str|null>}` — never a bare array.
  - Alembic history is strictly linear/single-head; current head is `03dec3037c8f`. `scripts/check_single_head.sh` must stay green.
  - New workspace-owned models must be registered in `WORKSPACE_OWNED_MODELS` and given `emit_rls_policy` in their migration. **This plan adds no new tables**, so neither applies.
- **Test command:** `/snap/bin/uv run pytest tests/unit -q` from `/srv/crawmatic/crawmatic`. Integration tests needing Postgres/Redis must self-skip via a module-local reachability probe.
- **Access-method classification:** a "protected" link is one whose `access_method` is `PROXY_HTTP` or `PLAYWRIGHT_PROXY` (the two that transit the paid residential proxy). `DIRECT_HTTP` and `DIRECT_HTTP_RETRY` are unprotected.

---

## File Structure

**Created:**

| File | Responsibility |
|---|---|
| `apps/api/app/routers/admin.py` | The three admin endpoints. Route wiring + request/response handling only; SQL lives in the service. |
| `apps/api/app/services/admin_usage.py` | The usage-export SQL builder + cursor codec. Pure SQLAlchemy Core; no FastAPI imports. |
| `apps/api/app/schemas/admin.py` | Pydantic DTOs for the admin router. |
| `apps/api/app/service_auth.py` | `require_service_token` dependency — constant-time bearer compare against `SAAS_SERVICE_TOKEN`. Deliberately separate from `deps.py` because it resolves **no** workspace context. |
| `apps/api/app/rate_limit.py` | Per-key fixed-window limiter middleware (read/write classification, Redis counter, 429 + `Retry-After`). |
| `apps/api/app/error_envelope.py` | Exception handlers producing the additive top-level `{"error": {...}}` envelope. |
| `apps/api/app/openapi_public.py` | `build_public_openapi(app)` — the external spec with internal tags stripped. |
| `apps/api/app/limits.py` | Shared numeric caps (`MAX_BULK_ITEMS`, domain/protected-link defaults) + the 422 builders that enforce them. |
| `scripts/export_openapi.py` | CLI that writes the cleaned public spec to a file. |
| `alembic/versions/<rev>_usage_export_indexes.py` | Covering indexes for the export (risk P2). |
| `tests/unit/_admin_fake_session.py` | Fake session that returns canned aggregate rows for the export. |
| `tests/unit/test_admin_router.py` | Admin router behaviour: auth, provisioning, archive, usage export. |
| `tests/unit/test_admin_usage_sql.py` | The export SQL/cursor unit tests (compiled-SQL assertions, no DB). |
| `tests/unit/test_service_auth.py` | Service-token dependency. |
| `tests/unit/test_rate_limit_middleware.py` | Limiter behaviour incl. 429 + `Retry-After`. |
| `tests/unit/test_error_envelope.py` | Envelope shape + backward compatibility. |
| `tests/unit/test_batch_caps.py` | 500-item cap on all four bulk endpoints. |
| `tests/unit/test_competitor_url_limits.py` | Domain limit, protected-link cap, unknown-domain default rule. |
| `tests/unit/test_public_openapi.py` | Internal tags excluded; every public path documented. |
| `tests/unit/test_migration_offline_usage_indexes.py` | Offline-DDL assertions for the new migration. |
| `tests/integration/test_admin_usage_live.py` | Live-Postgres end-to-end export (self-skipping). |

**Modified:**

| File | Change |
|---|---|
| `apps/api/app/main.py` | Include `admin.router`; register error handlers; add rate-limit middleware; install `custom_openapi`. |
| `libs/shared/app_shared/config.py` | Add `SAAS_SERVICE_TOKEN`, rate-limit knobs, abuse-cap knobs. |
| `.env.example` | Document the new env vars. |
| `apps/api/app/routers/products.py`, `variants.py`, `matches.py`, `scrape_profiles.py` | Batch cap guard on `bulk-upsert`. |
| `apps/api/app/routers/matches.py`, `competitors.py` | Domain limit + protected-link cap + unknown-domain default rule. |
| `libs/shared/app_shared/security/api_keys.py` | (read-only; reused, not modified) |

---

## Design Decisions (read before Task 3)

These were resolved against the live schema and are binding for the export.

1. **Product attribution.** `request_attempts` has `match_id` but no `product_id`. Join `competitor_product_matches m ON m.id = ra.match_id AND m.workspace_id = ra.workspace_id` and take `m.product_id`.
2. **`cycle_ts` definition.** `cycle_ts = date_trunc('hour', COALESCE(sj.created_at, ra.created_at))` where `sj` is the `scrape_jobs` row named by `ra.scrape_job_id` (LEFT JOIN — `scrape_job_id` is nullable). Rationale: it is deterministic, idempotent across re-exports, and collision-free at every frequency the SaaS offers (max cadence is every 6 hours, §5.2 `FREQUENCIES`), so two distinct cycles can never collapse into one bucket. Retries of the same cycle *do* collapse, which is correct — a retried link must not be billed twice.
3. **A "link" is a match, not an attempt.** Retries produce several `request_attempts` rows for one `match_id`. So counts are over **distinct matches**: the inner CTE groups by `match_id` and folds attempts with `bool_or`, and the outer aggregate counts those folded rows.
4. **`check_successful` comes from price observations, not attempts.** The Fairness law (§5.1.4) says a credit is consumed only for a **successful price observation**. So `check_successful = COALESCE(bool_or(po.success), false)` over `price_observations` for the same `(workspace_id, product_id, cycle_ts)`, bucketed identically. The `links_*` counters come from `request_attempts`. This is exactly the plan's "from `request_attempts` + price observations".
5. **Cursor.** The repo's `app_shared.pagination` cursor is keyset on `(created_at, id)`, which an aggregate has neither of. The export therefore uses its own codec over the aggregate's natural sort key `(cycle_ts, workspace_id, product_id)`, reusing the same base64url-JSON envelope style and the same `{"items", "next_cursor"}` response shape.
6. **Session.** Admin endpoints read across every workspace, and `request_attempts`/`price_observations` carry RLS. They therefore use `get_auth_session()` (BYPASSRLS) — the same narrow boundary `deps.py` and `auth.py` already use for pre-auth lookups — with `# noqa: workspace-scope`.
7. **Rate-limiter failure policy: fail OPEN.** If Redis is unreachable the limiter allows the request. This is deliberate and differs from the login limiter (which fails closed). The login limiter protects credentials; this one protects cost, and the real cost guards are the domain/protected-link caps and the direct-HTTP default. Failing closed here would convert a Redis blip into a total outage of a paid product. Documented in the module docstring.
8. **Error envelope is additive.** Handlers emit `{"error": {...}, "detail": <original detail>}`. Existing tests asserting `["detail"]["error"]["code"]` and `["detail"]["code"]` keep passing; external consumers get one stable top-level `error` object on every error response.

---

### Task 1: Config + service-token auth seam

**Files:**
- Modify: `libs/shared/app_shared/config.py`
- Modify: `.env.example`
- Create: `apps/api/app/service_auth.py`
- Test: `tests/unit/test_service_auth.py`
- Test: `tests/unit/test_config.py` (extend)

**Interfaces:**
- Consumes: `app_shared.config.get_settings`, `app.errors.auth_failed_exception`.
- Produces:
  - `Settings.SAAS_SERVICE_TOKEN: str | None = None`
  - `apps/api/app/service_auth.py::require_service_token(authorization: str | None = Header(default=None)) -> None` — a FastAPI dependency raising 401 on any failure, returning `None` on success.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_service_auth.py`:

```python
"""`require_service_token` — the SaaS admin auth seam (PLAN §7.1)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.service_auth import require_service_token


class _Settings:
    def __init__(self, token: str | None) -> None:
        self.SAAS_SERVICE_TOKEN = token


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch):
    holder = {"token": "s3cret-service-token"}

    def _get_settings():
        return _Settings(holder["token"])

    monkeypatch.setattr("app.service_auth.get_settings", _get_settings)
    return holder


def test_correct_token_is_accepted() -> None:
    assert require_service_token("Bearer s3cret-service-token") is None


def test_wrong_token_is_401() -> None:
    with pytest.raises(HTTPException) as exc:
        require_service_token("Bearer wrong")
    assert exc.value.status_code == 401


def test_missing_header_is_401() -> None:
    with pytest.raises(HTTPException) as exc:
        require_service_token(None)
    assert exc.value.status_code == 401


def test_non_bearer_scheme_is_401() -> None:
    with pytest.raises(HTTPException) as exc:
        require_service_token("Basic s3cret-service-token")
    assert exc.value.status_code == 401


def test_unconfigured_token_refuses_everything(_settings) -> None:
    """An engine with no SAAS_SERVICE_TOKEN set must not expose admin routes."""
    _settings["token"] = None
    with pytest.raises(HTTPException) as exc:
        require_service_token("Bearer anything")
    assert exc.value.status_code == 401


def test_empty_configured_token_refuses_everything(_settings) -> None:
    _settings["token"] = ""
    with pytest.raises(HTTPException) as exc:
        require_service_token("Bearer ")
    assert exc.value.status_code == 401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/snap/bin/uv run pytest tests/unit/test_service_auth.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.service_auth'`

- [ ] **Step 3: Add the config field**

In `libs/shared/app_shared/config.py`, inside the `Settings` class, add a new block after the existing auth/JWT block:

```python
    # --- SaaS control-plane service auth (PLAN §7.1) ---
    # Static bearer token the SaaS control plane presents to
    # `/v1/admin/*`. Machine analog of SUPER_ADMIN: it resolves no
    # workspace context and is compared in constant time. Optional so an
    # engine deployment that hosts no SaaS control plane still boots —
    # when unset, every admin request is refused (fail-closed).
    SAAS_SERVICE_TOKEN: str | None = None
```

In `.env.example`, add:

```
# Static bearer token used by the SaaS control plane for /v1/admin/* .
# Generate with: python -c "import secrets; print(secrets.token_urlsafe(48))"
# Leave unset to disable the admin surface entirely.
SAAS_SERVICE_TOKEN=
```

- [ ] **Step 4: Write the dependency**

Create `apps/api/app/service_auth.py`:

```python
"""Static service-token auth for `/v1/admin/*` (PLAN §7.1).

Deliberately **not** part of `app.deps`: every `/v1` tenant endpoint
resolves exactly one authorized workspace context via
``get_current_principal``, whereas the admin surface is cross-workspace
by definition (it provisions new workspaces and aggregates usage over
all of them). Mixing the two seams would mean teaching the workspace
resolver about a principal with no workspace, which is precisely the
hole `deps._resolve_workspace` exists to close.

The token is compared with ``hmac.compare_digest`` so a wrong guess
costs the same time as a right one. When ``SAAS_SERVICE_TOKEN`` is unset
or empty the dependency refuses every request (fail-closed) — an engine
with no SaaS control plane exposes no admin surface.

Failures always raise the uniform ``auth_failed_exception`` (401,
``AUTH_FAILED``) — never a message distinguishing "no token configured"
from "wrong token", which would leak deployment state to an attacker.
"""

from __future__ import annotations

import hmac

from fastapi import Header

from app_shared.config import get_settings

from app.errors import auth_failed_exception

_BEARER = "Bearer "


def require_service_token(authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency: authorize the SaaS control plane, or 401.

    Returns ``None`` on success — the admin routes need no principal,
    only the assurance that the caller holds the shared secret.
    """
    settings = get_settings()
    configured = settings.SAAS_SERVICE_TOKEN
    if not configured:
        raise auth_failed_exception()

    if not authorization or not authorization.startswith(_BEARER):
        raise auth_failed_exception()

    presented = authorization[len(_BEARER) :].strip()
    if not presented:
        raise auth_failed_exception()

    if not hmac.compare_digest(presented, configured):
        raise auth_failed_exception()

    return None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `/snap/bin/uv run pytest tests/unit/test_service_auth.py tests/unit/test_config.py -q`
Expected: PASS

- [ ] **Step 6: Run the full unit suite**

Run: `/snap/bin/uv run pytest tests/unit -q`
Expected: `1774 passed` (1768 baseline + 6 new)

- [ ] **Step 7: Commit**

```bash
git add libs/shared/app_shared/config.py .env.example apps/api/app/service_auth.py tests/unit/test_service_auth.py
git commit -m "feat(api): static SAAS_SERVICE_TOKEN auth seam for the admin surface"
```

---

### Task 2: Admin workspace provisioning + archive

**Files:**
- Create: `apps/api/app/schemas/admin.py`
- Create: `apps/api/app/routers/admin.py`
- Modify: `apps/api/app/main.py`
- Test: `tests/unit/test_admin_router.py`

**Interfaces:**
- Consumes: `app.service_auth.require_service_token`; `app_shared.database.get_auth_session`; `app_shared.security.api_keys.generate_api_key`; `app_shared.models.identity.Workspace`, `ApiKey`; `app_shared.enums.WorkspaceStatus`, `ApiKeyStatus`, `Scope`.
- Produces:
  - `app.routers.admin.router` (`APIRouter(prefix="/v1/admin", tags=["admin"])`)
  - `POST /v1/admin/workspaces` → `WorkspaceProvisionResponse(workspace_id: uuid.UUID, api_key: str, external_ref: str)`
  - `POST /v1/admin/workspaces/{workspace_id}/archive` → `WorkspaceArchiveResponse(workspace_id: uuid.UUID, status: str)`
  - `app.routers.admin.get_admin_session` — the session-provider seam tests override.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_admin_router.py`:

```python
"""`/v1/admin/workspaces` — SaaS provisioning surface (PLAN §7.1)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import admin
from app.service_auth import require_service_token
from unit._jobs_fake_session import FakeOrmSession

SERVICE_HEADERS = {"Authorization": "Bearer test-service-token"}


@pytest.fixture(autouse=True)
def _clear_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def session() -> FakeOrmSession:
    return FakeOrmSession()


@pytest.fixture(autouse=True)
def _authorized(session: FakeOrmSession) -> None:
    app.dependency_overrides[require_service_token] = lambda: None
    app.dependency_overrides[admin.get_admin_session] = lambda: session


def test_provision_returns_workspace_id_and_plaintext_key(client, session):
    resp = client.post(
        "/v1/admin/workspaces",
        json={"name": "Acme Store", "external_ref": "proj_123"},
        headers=SERVICE_HEADERS,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert uuid.UUID(body["workspace_id"])
    assert body["api_key"].startswith("ck_")
    assert body["external_ref"] == "proj_123"


def test_provision_persists_workspace_and_api_key(client, session):
    client.post(
        "/v1/admin/workspaces",
        json={"name": "Acme Store", "external_ref": "proj_123"},
        headers=SERVICE_HEADERS,
    )
    added_types = {type(obj).__name__ for obj in session.added}
    assert "Workspace" in added_types
    assert "ApiKey" in added_types


def test_provision_key_hash_is_stored_not_plaintext(client, session):
    resp = client.post(
        "/v1/admin/workspaces",
        json={"name": "Acme Store", "external_ref": "proj_124"},
        headers=SERVICE_HEADERS,
    )
    plaintext = resp.json()["api_key"]
    keys = [o for o in session.added if type(o).__name__ == "ApiKey"]
    assert len(keys) == 1
    assert keys[0].key_hash != plaintext
    assert plaintext.startswith(keys[0].key_prefix)


def test_provision_rejects_extra_fields(client):
    resp = client.post(
        "/v1/admin/workspaces",
        json={"name": "Acme", "external_ref": "p1", "sneaky": True},
        headers=SERVICE_HEADERS,
    )
    assert resp.status_code == 422


def test_provision_requires_external_ref(client):
    resp = client.post(
        "/v1/admin/workspaces", json={"name": "Acme"}, headers=SERVICE_HEADERS
    )
    assert resp.status_code == 422


def test_archive_sets_status_archived(client, session):
    from app_shared.enums import WorkspaceStatus
    from app_shared.models.identity import Workspace

    ws = Workspace(
        id=uuid.uuid4(), name="Acme", slug="acme", status=WorkspaceStatus.ACTIVE
    )
    session.seed(ws)

    resp = client.post(
        f"/v1/admin/workspaces/{ws.id}/archive", headers=SERVICE_HEADERS
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ARCHIVED"
    assert ws.status == WorkspaceStatus.ARCHIVED


def test_archive_unknown_workspace_is_404(client, session):
    resp = client.post(
        f"/v1/admin/workspaces/{uuid.uuid4()}/archive", headers=SERVICE_HEADERS
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"]["code"] == "NOT_FOUND"


def test_admin_routes_require_the_service_token():
    """Without the dependency override the real seam must refuse."""
    app.dependency_overrides.clear()
    with TestClient(app) as bare:
        resp = bare.post(
            "/v1/admin/workspaces", json={"name": "x", "external_ref": "y"}
        )
    assert resp.status_code == 401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/snap/bin/uv run pytest tests/unit/test_admin_router.py -q`
Expected: FAIL — `ImportError: cannot import name 'admin' from 'app.routers'`

- [ ] **Step 3: Check the enum + slug facts the implementation needs**

Run:

```bash
grep -n "class WorkspaceStatus" -A 8 /srv/crawmatic/crawmatic/libs/shared/app_shared/enums.py
grep -n "class Scope" -A 40 /srv/crawmatic/crawmatic/libs/shared/app_shared/security/scopes.py
```

Use the exact `WorkspaceStatus` member for archived (`ARCHIVED` if present; otherwise the closest paused/inactive member — record the choice in the module docstring) and the exact `Scope` members for the bootstrap key. The bootstrap key gets **every** tenant scope the SaaS needs to drive a workspace: products, variants, product_groups, competitors, matches, alerts, jobs, refresh_rules, webhooks, scrape_profiles, domain_rules — read and write. Do **not** grant `proxy_providers:*` or `access_policies:*` (internal cost controls).

- [ ] **Step 4: Write the schemas**

Create `apps/api/app/schemas/admin.py`:

```python
"""DTOs for the SaaS admin surface (`/v1/admin/*`, PLAN §7.1–§7.2).

Lives in `apps/api` like every other schema module — `app_shared` must
never import pydantic.

The usage-export field names are a **frozen contract**: the SaaS
metering consumer keys `UsageSnapshot` on
`(workspace_id, product_id, cycle_ts)` and prices from the counters.
Renaming any field here breaks billing.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class WorkspaceProvisionRequest(BaseModel):
    """`POST /v1/admin/workspaces` body."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    external_ref: str = Field(min_length=1, max_length=200)


class WorkspaceProvisionResponse(BaseModel):
    """The one and only time the bootstrap key is returned in plaintext."""

    workspace_id: uuid.UUID
    api_key: str
    external_ref: str


class WorkspaceArchiveResponse(BaseModel):
    workspace_id: uuid.UUID
    status: str


class UsageRow(BaseModel):
    """One product-check cycle. **Field names are contractual.**"""

    workspace_id: uuid.UUID
    product_id: uuid.UUID
    cycle_ts: datetime
    links_total: int
    links_succeeded: int
    protected_links_attempted: int
    protected_links_succeeded: int
    check_successful: bool


class UsageListResponse(BaseModel):
    """`{items, next_cursor}` envelope for `GET /v1/admin/usage`."""

    items: list[UsageRow]
    next_cursor: str | None
```

- [ ] **Step 5: Write the router (provision + archive only)**

Create `apps/api/app/routers/admin.py`. The usage endpoint is added in Task 4.

```python
"""SaaS control-plane admin endpoints (PLAN §7.1–§7.2).

Guarded by `app.service_auth.require_service_token` (static bearer,
constant-time compare) rather than the workspace auth seam in
`app.deps` — this surface is cross-workspace by construction.

Because it is cross-workspace it runs on `get_auth_session()`
(BYPASSRLS), the same narrow boundary `deps.py`/`auth.py` already use
for pre-auth lookups. Every statement here is deliberately unscoped and
annotated `# noqa: workspace-scope`.

This router is **internal**: it is excluded from the public OpenAPI spec
by `app.openapi_public` and must never be documented to customers.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app_shared.database import get_auth_session
from app_shared.enums import ApiKeyStatus, WorkspaceStatus
from app_shared.models.identity import ApiKey, Workspace
from app_shared.security.api_keys import generate_api_key

from app.schemas.admin import (
    WorkspaceArchiveResponse,
    WorkspaceProvisionRequest,
    WorkspaceProvisionResponse,
)
from app.service_auth import require_service_token

router = APIRouter(
    prefix="/v1/admin", tags=["admin"], dependencies=[Depends(require_service_token)]
)

#: Tenant scopes granted to a bootstrap key. Deliberately excludes
#: `proxy_providers:*` and `access_policies:*` — those configure what we
#: are willing to spend on a fetch and stay operator-only.
BOOTSTRAP_SCOPES: list[str] = [
    "products:read",
    "products:write",
    "variants:read",
    "variants:write",
    "product_groups:read",
    "product_groups:write",
    "competitors:read",
    "competitors:write",
    "matches:read",
    "matches:write",
    "alerts:read",
    "jobs:read",
    "jobs:write",
    "refresh_rules:read",
    "refresh_rules:write",
    "webhooks:read",
    "webhooks:write",
    "scrape_profiles:read",
    "scrape_profiles:write",
    "domain_rules:read",
    "domain_rules:write",
]


def get_admin_session() -> Iterator[Session]:
    """Session seam for the admin surface — BYPASSRLS, cross-workspace.

    A separate dependency (rather than calling `get_auth_session()`
    inline) so tests can override it the same way they override
    `get_current_principal` for tenant routers.
    """
    with get_auth_session() as session:
        yield session
        session.commit()


def _not_found(message: str) -> HTTPException:
    return HTTPException(
        status_code=404, detail={"error": {"code": "NOT_FOUND", "message": message}}
    )


def _slugify(name: str, external_ref: str) -> str:
    """A unique, stable slug: the readable name plus the SaaS ref.

    `workspaces.slug` is UNIQUE; `external_ref` is unique per SaaS
    project, so appending it makes collisions between two customers
    named "Acme Store" impossible without a retry loop.
    """
    base = "".join(ch.lower() if ch.isalnum() else "-" for ch in name).strip("-")
    base = "-".join(part for part in base.split("-") if part) or "workspace"
    ref = "".join(ch.lower() if ch.isalnum() else "-" for ch in external_ref).strip("-")
    return f"{base}-{ref}"[:200]


@router.post("/workspaces", response_model=WorkspaceProvisionResponse, status_code=201)
def provision_workspace(
    payload: WorkspaceProvisionRequest,
    session: Session = Depends(get_admin_session),
) -> WorkspaceProvisionResponse:
    """`POST /v1/admin/workspaces` — create a workspace + bootstrap key.

    The plaintext key is returned exactly once and never stored; only
    its prefix and sha256 hash are persisted (same contract as
    `POST /v1/api-keys`).
    """
    workspace = Workspace(
        name=payload.name,
        slug=_slugify(payload.name, payload.external_ref),
        status=WorkspaceStatus.ACTIVE,
    )
    session.add(workspace)
    session.flush()

    full_secret, key_prefix, key_hash = generate_api_key()
    session.add(
        ApiKey(
            workspace_id=workspace.id,
            name=f"saas-bootstrap:{payload.external_ref}",
            key_prefix=key_prefix,
            key_hash=key_hash,
            scopes=BOOTSTRAP_SCOPES,
            status=ApiKeyStatus.ACTIVE,
        )
    )
    session.flush()

    return WorkspaceProvisionResponse(
        workspace_id=workspace.id,
        api_key=full_secret,
        external_ref=payload.external_ref,
    )


@router.post(
    "/workspaces/{workspace_id}/archive", response_model=WorkspaceArchiveResponse
)
def archive_workspace(
    workspace_id: uuid.UUID,
    session: Session = Depends(get_admin_session),
) -> WorkspaceArchiveResponse:
    """`POST /v1/admin/workspaces/{id}/archive` — pause + retention flag.

    Idempotent: archiving an already-archived workspace is a 200.
    """
    workspace = session.execute(
        select(Workspace).where(Workspace.id == workspace_id)  # noqa: workspace-scope
    ).scalar_one_or_none()
    if workspace is None:
        raise _not_found("Workspace not found.")

    workspace.status = WorkspaceStatus.ARCHIVED
    session.flush()
    return WorkspaceArchiveResponse(
        workspace_id=workspace.id, status=WorkspaceStatus.ARCHIVED.value
    )
```

If `WorkspaceStatus` has no `ARCHIVED` member, use the nearest disabled member and say so in the module docstring.

- [ ] **Step 6: Wire the router**

In `apps/api/app/main.py`, add `admin` to the `from app.routers import (...)` tuple (alphabetically first) and add `app.include_router(admin.router)` as the **last** `include_router` call. Extend the module docstring with a paragraph:

```
PLAN §7.1 adds the `/v1/admin` router (`docs/superpowers/plans/
2026-08-10-phase2-engine.md`) — SaaS control-plane workspace
provisioning, archive, and the usage export. It is guarded by the static
`SAAS_SERVICE_TOKEN` seam in `app.service_auth`, not the workspace seam
in `app.deps`, and is excluded from the public OpenAPI spec.
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `/snap/bin/uv run pytest tests/unit/test_admin_router.py -q`
Expected: PASS. If `FakeOrmSession` lacks `.added` or `.seed`, read `tests/unit/_jobs_fake_session.py` and adapt the test's assertions to the real attribute names — do **not** modify the fake session.

- [ ] **Step 8: Run the full unit suite**

Run: `/snap/bin/uv run pytest tests/unit -q`
Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add apps/api/app/routers/admin.py apps/api/app/schemas/admin.py apps/api/app/main.py tests/unit/test_admin_router.py
git commit -m "feat(api): admin workspace provisioning and archive endpoints"
```

---

### Task 3: Usage-export SQL + cursor codec

**Files:**
- Create: `apps/api/app/services/admin_usage.py`
- Test: `tests/unit/test_admin_usage_sql.py`

**Interfaces:**
- Consumes: `app_shared.models.observations.RequestAttempt`, `PriceObservation`; `app_shared.models.competitors_matches.CompetitorProductMatch`; `app_shared.models.jobs.ScrapeJob`; `app_shared.enums.AccessMethod`.
- Produces:
  - `MAX_WINDOW_DAYS = 31`
  - `DEFAULT_USAGE_LIMIT = 500`, `MAX_USAGE_LIMIT = 1000`
  - `class UsageCursor(NamedTuple): cycle_ts: datetime; workspace_id: uuid.UUID; product_id: uuid.UUID`
  - `encode_usage_cursor(row) -> str` / `decode_usage_cursor(token: str) -> UsageCursor` (raises `InvalidUsageCursor`)
  - `class InvalidUsageCursor(ValueError)`
  - `class UsageWindowTooLarge(ValueError)`
  - `validate_window(since: datetime, until: datetime) -> None`
  - `build_usage_query(*, since, until, after: UsageCursor | None, limit: int) -> Select` — the whole aggregation, in SQL.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_admin_usage_sql.py`:

```python
"""Usage-export aggregation + cursor (PLAN §7.2, risk P2).

No database: the query is asserted by compiling it to SQL text, which
is what actually guards "aggregate in SQL, not Python".
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.dialects import postgresql

from app.services.admin_usage import (
    MAX_WINDOW_DAYS,
    InvalidUsageCursor,
    UsageCursor,
    UsageWindowTooLarge,
    build_usage_query,
    decode_usage_cursor,
    encode_usage_cursor,
    validate_window,
)

SINCE = datetime(2026, 8, 1, tzinfo=timezone.utc)
UNTIL = datetime(2026, 8, 8, tzinfo=timezone.utc)


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


def test_window_within_limit_is_accepted() -> None:
    assert validate_window(SINCE, UNTIL) is None


def test_window_over_31_days_is_rejected() -> None:
    with pytest.raises(UsageWindowTooLarge):
        validate_window(SINCE, SINCE + timedelta(days=MAX_WINDOW_DAYS, seconds=1))


def test_window_exactly_31_days_is_accepted() -> None:
    assert validate_window(SINCE, SINCE + timedelta(days=MAX_WINDOW_DAYS)) is None


def test_inverted_window_is_rejected() -> None:
    with pytest.raises(UsageWindowTooLarge):
        validate_window(UNTIL, SINCE)


def test_cursor_round_trips() -> None:
    cursor = UsageCursor(
        cycle_ts=datetime(2026, 8, 3, 14, tzinfo=timezone.utc),
        workspace_id=uuid.uuid4(),
        product_id=uuid.uuid4(),
    )
    assert decode_usage_cursor(encode_usage_cursor(cursor)) == cursor


def test_garbage_cursor_raises() -> None:
    with pytest.raises(InvalidUsageCursor):
        decode_usage_cursor("!!!not-base64!!!")


def test_truncated_cursor_raises() -> None:
    with pytest.raises(InvalidUsageCursor):
        decode_usage_cursor("eyJjIjogIjIwMjYt")


def test_query_bounds_the_window_on_the_partition_key() -> None:
    sql = _sql(build_usage_query(since=SINCE, until=UNTIL, after=None, limit=10))
    assert "request_attempts.created_at >=" in sql
    assert "request_attempts.created_at <" in sql


def test_query_aggregates_in_sql_not_python() -> None:
    sql = _sql(build_usage_query(since=SINCE, until=UNTIL, after=None, limit=10))
    assert "count(" in sql.lower()
    assert "bool_or(" in sql.lower()
    assert "group by" in sql.lower()
    assert "date_trunc" in sql.lower()


def test_query_attributes_links_to_products_via_matches() -> None:
    sql = _sql(build_usage_query(since=SINCE, until=UNTIL, after=None, limit=10))
    assert "competitor_product_matches" in sql
    assert "product_id" in sql


def test_query_reads_success_from_price_observations() -> None:
    sql = _sql(build_usage_query(since=SINCE, until=UNTIL, after=None, limit=10))
    assert "price_observations" in sql


def test_query_classifies_protected_by_access_method() -> None:
    sql = _sql(build_usage_query(since=SINCE, until=UNTIL, after=None, limit=10))
    assert "PROXY_HTTP" in sql
    assert "PLAYWRIGHT_PROXY" in sql


def test_query_orders_by_the_cursor_key() -> None:
    sql = _sql(build_usage_query(since=SINCE, until=UNTIL, after=None, limit=10)).lower()
    order_by = sql.split("order by", 1)[1]
    assert order_by.index("cycle_ts") < order_by.index("workspace_id")
    assert order_by.index("workspace_id") < order_by.index("product_id")


def test_query_fetches_one_extra_row_to_detect_a_next_page() -> None:
    sql = _sql(build_usage_query(since=SINCE, until=UNTIL, after=None, limit=10))
    assert "LIMIT 11" in sql or "LIMIT %(param_1)s" in sql


def test_cursor_predicate_is_a_keyset_tuple_comparison() -> None:
    after = UsageCursor(
        cycle_ts=datetime(2026, 8, 3, 14, tzinfo=timezone.utc),
        workspace_id=uuid.uuid4(),
        product_id=uuid.uuid4(),
    )
    sql = _sql(build_usage_query(since=SINCE, until=UNTIL, after=after, limit=10))
    assert ">" in sql.split("HAVING")[-1] or "(cycle_ts, workspace_id, product_id) >" in sql.replace('"', "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/snap/bin/uv run pytest tests/unit/test_admin_usage_sql.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.admin_usage'`

- [ ] **Step 3: Write the service**

Create `apps/api/app/services/admin_usage.py`:

```python
"""Usage-export aggregation (PLAN §7.2) — all of it in SQL (risk P2).

Shape of the answer, per PLAN §5.3: one row per
`(workspace_id, product_id, cycle_ts)` describing one **product check
cycle**, with the link counters the SaaS prices from and the
success flag that decides whether a credit is consumed at all.

Three facts drive the query, all verified against the live schema:

1. `request_attempts` has `match_id`, not `product_id` — product
   attribution comes from joining `competitor_product_matches`.
2. A *link* is a match, not an attempt. Retries write several
   `request_attempts` rows for one `match_id`, so the inner CTE folds
   attempts per match with `bool_or` and the outer aggregate counts
   folded rows. Without this a retried link would be billed twice.
3. `check_successful` is read from `price_observations`, not from
   attempt success: the Fairness law (PLAN §5.1.4) consumes a credit
   only for a successful **price observation**.

`cycle_ts` is `date_trunc('hour', COALESCE(scrape_jobs.created_at,
request_attempts.created_at))`. Hour truncation makes the export
idempotent (re-exporting a window yields byte-identical rows) and is
collision-free at every cadence the SaaS sells — the fastest is every
6 hours (PLAN §5.2 `FREQUENCIES`), so two genuine cycles can never
share a bucket, while retries of one cycle correctly collapse into it.

Partition-awareness (risk P2): the only predicate on the partitioned
`request_attempts`/`price_observations` is a bounded range on their
partition keys (`created_at` / `scraped_at`), so Postgres prunes to the
one or two monthly partitions the window touches. The window is capped
at 31 days by `validate_window`.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from sqlalchemy import Select, and_, func, literal, or_, select, tuple_

from app_shared.enums import AccessMethod
from app_shared.models.competitors_matches import CompetitorProductMatch
from app_shared.models.jobs import ScrapeJob
from app_shared.models.observations import PriceObservation, RequestAttempt

MAX_WINDOW_DAYS = 31
DEFAULT_USAGE_LIMIT = 500
MAX_USAGE_LIMIT = 1000

#: Access methods that ride the paid residential proxy. Everything else
#: (DIRECT_HTTP, DIRECT_HTTP_RETRY) is compute-only and near-free.
PROTECTED_ACCESS_METHODS = (
    AccessMethod.PROXY_HTTP.value,
    AccessMethod.PLAYWRIGHT_PROXY.value,
)


class InvalidUsageCursor(ValueError):
    """The `cursor` query parameter was not a token we issued."""


class UsageWindowTooLarge(ValueError):
    """`until - since` exceeded `MAX_WINDOW_DAYS`, or the window is inverted."""


class UsageCursor(NamedTuple):
    """Keyset position in the aggregate's natural sort order."""

    cycle_ts: datetime
    workspace_id: uuid.UUID
    product_id: uuid.UUID


def clamp_usage_limit(requested: int | None) -> int:
    if requested is None:
        return DEFAULT_USAGE_LIMIT
    return max(1, min(int(requested), MAX_USAGE_LIMIT))


def validate_window(since: datetime, until: datetime) -> None:
    """Reject an inverted or over-long window (PLAN §7.2, risk P2)."""
    if until <= since:
        raise UsageWindowTooLarge("`until` must be after `since`.")
    if until - since > timedelta(days=MAX_WINDOW_DAYS):
        raise UsageWindowTooLarge(
            f"The usage window may not exceed {MAX_WINDOW_DAYS} days."
        )


def encode_usage_cursor(cursor: UsageCursor) -> str:
    payload = json.dumps(
        {
            "c": cursor.cycle_ts.isoformat(),
            "w": str(cursor.workspace_id),
            "p": str(cursor.product_id),
        },
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_usage_cursor(token: str) -> UsageCursor:
    try:
        padded = token + "=" * (-len(token) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
        return UsageCursor(
            cycle_ts=datetime.fromisoformat(payload["c"]),
            workspace_id=uuid.UUID(payload["w"]),
            product_id=uuid.UUID(payload["p"]),
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        binascii.Error,
        UnicodeDecodeError,
    ) as exc:
        raise InvalidUsageCursor("Malformed cursor.") from exc


def _cycle_ts_expr(attempt_created_at, job_created_at):
    return func.date_trunc("hour", func.coalesce(job_created_at, attempt_created_at))


def build_usage_query(
    *,
    since: datetime,
    until: datetime,
    after: UsageCursor | None,
    limit: int,
) -> Select:
    """The whole export as one statement. Never aggregates in Python.

    Returns a `Select` whose columns are, in order:
    `workspace_id, product_id, cycle_ts, links_total, links_succeeded,
    protected_links_attempted, protected_links_succeeded,
    check_successful` — the frozen §7.2 contract, positionally stable.
    """
    is_protected = RequestAttempt.access_method.in_(PROTECTED_ACCESS_METHODS)

    # --- inner: fold retries, one row per (cycle, workspace, product, match)
    per_link = (
        select(
            RequestAttempt.workspace_id.label("workspace_id"),
            CompetitorProductMatch.product_id.label("product_id"),
            _cycle_ts_expr(RequestAttempt.created_at, ScrapeJob.created_at).label(
                "cycle_ts"
            ),
            RequestAttempt.match_id.label("match_id"),
            func.bool_or(RequestAttempt.success).label("link_ok"),
            func.bool_or(is_protected).label("protected"),
            func.bool_or(and_(is_protected, RequestAttempt.success)).label(
                "protected_ok"
            ),
        )
        .join(
            CompetitorProductMatch,
            and_(
                CompetitorProductMatch.id == RequestAttempt.match_id,
                CompetitorProductMatch.workspace_id == RequestAttempt.workspace_id,
            ),
        )
        .outerjoin(
            ScrapeJob,
            and_(
                ScrapeJob.id == RequestAttempt.scrape_job_id,
                ScrapeJob.workspace_id == RequestAttempt.workspace_id,
            ),
        )
        .where(
            RequestAttempt.created_at >= since,
            RequestAttempt.created_at < until,
        )
        .group_by(
            RequestAttempt.workspace_id,
            CompetitorProductMatch.product_id,
            _cycle_ts_expr(RequestAttempt.created_at, ScrapeJob.created_at),
            RequestAttempt.match_id,
        )
        .cte("per_link")
    )

    # --- observations: did this product actually yield a price this cycle?
    per_check = (
        select(
            PriceObservation.workspace_id.label("workspace_id"),
            PriceObservation.product_id.label("product_id"),
            _cycle_ts_expr(PriceObservation.scraped_at, ScrapeJob.created_at).label(
                "cycle_ts"
            ),
            func.bool_or(PriceObservation.success).label("observed"),
        )
        .outerjoin(
            ScrapeJob,
            and_(
                ScrapeJob.id == PriceObservation.scrape_job_id,
                ScrapeJob.workspace_id == PriceObservation.workspace_id,
            ),
        )
        .where(
            PriceObservation.scraped_at >= since,
            PriceObservation.scraped_at < until,
        )
        .group_by(
            PriceObservation.workspace_id,
            PriceObservation.product_id,
            _cycle_ts_expr(PriceObservation.scraped_at, ScrapeJob.created_at),
        )
        .cte("per_check")
    )

    stmt = (
        select(
            per_link.c.workspace_id.label("workspace_id"),
            per_link.c.product_id.label("product_id"),
            per_link.c.cycle_ts.label("cycle_ts"),
            func.count().label("links_total"),
            func.count().filter(per_link.c.link_ok).label("links_succeeded"),
            func.count().filter(per_link.c.protected).label(
                "protected_links_attempted"
            ),
            func.count().filter(per_link.c.protected_ok).label(
                "protected_links_succeeded"
            ),
            func.coalesce(func.bool_or(per_check.c.observed), literal(False)).label(
                "check_successful"
            ),
        )
        .select_from(per_link)
        .outerjoin(
            per_check,
            and_(
                per_check.c.workspace_id == per_link.c.workspace_id,
                per_check.c.product_id == per_link.c.product_id,
                per_check.c.cycle_ts == per_link.c.cycle_ts,
            ),
        )
        .group_by(per_link.c.workspace_id, per_link.c.product_id, per_link.c.cycle_ts)
    )

    if after is not None:
        stmt = stmt.having(
            tuple_(
                per_link.c.cycle_ts, per_link.c.workspace_id, per_link.c.product_id
            )
            > tuple_(
                literal(after.cycle_ts),
                literal(after.workspace_id),
                literal(after.product_id),
            )
        )

    return stmt.order_by(
        per_link.c.cycle_ts, per_link.c.workspace_id, per_link.c.product_id
    ).limit(limit + 1)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/snap/bin/uv run pytest tests/unit/test_admin_usage_sql.py -q`
Expected: PASS.

If SQLAlchemy rejects `func.count().filter(...)` in this position, replace each with `func.count(case((<cond>, 1)))` using `from sqlalchemy import case`, and update the test's `count(` assertion accordingly. If `.having()` on a tuple comparison compiles badly, wrap the aggregate in a subquery and apply the keyset predicate in an outer `WHERE` — update the last test's assertion to match, keeping the `(cycle_ts, workspace_id, product_id)` ordering.

- [ ] **Step 5: Run the full unit suite**

Run: `/snap/bin/uv run pytest tests/unit -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add apps/api/app/services/admin_usage.py tests/unit/test_admin_usage_sql.py
git commit -m "feat(api): SQL-only usage aggregation and keyset cursor for the usage export"
```

---

### Task 4: `GET /v1/admin/usage` endpoint

**Files:**
- Modify: `apps/api/app/routers/admin.py`
- Test: `tests/unit/test_admin_router.py` (extend)
- Create: `tests/unit/_admin_fake_session.py`

**Interfaces:**
- Consumes: everything Task 3 produces; `app.schemas.admin.UsageListResponse`, `UsageRow`.
- Produces: `GET /v1/admin/usage?since=&until=&cursor=&limit=` → `UsageListResponse`.

- [ ] **Step 1: Write the fake session**

Create `tests/unit/_admin_fake_session.py`:

```python
"""Fake session returning canned aggregate rows for the usage export.

The export is a single hand-written aggregate; there is nothing for a
generic fake ORM session to interpret. The router's job is window
validation, cursor handling, and row mapping, so the seam we fake is
"the statement returned these tuples".
"""

from __future__ import annotations

from typing import Any


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)


class FakeUsageSession:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows = rows or []
        self.executed: list[Any] = []

    def execute(self, statement: Any, *args: Any, **kwargs: Any) -> _Result:
        self.executed.append(statement)
        return _Result(self.rows)

    def commit(self) -> None:
        return None

    def flush(self) -> None:
        return None
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/unit/test_admin_router.py`:

```python
from types import SimpleNamespace

from unit._admin_fake_session import FakeUsageSession


def _usage_row(**over):
    base = dict(
        workspace_id=uuid.uuid4(),
        product_id=uuid.uuid4(),
        cycle_ts="2026-08-03T14:00:00+00:00",
        links_total=7,
        links_succeeded=6,
        protected_links_attempted=1,
        protected_links_succeeded=1,
        check_successful=True,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _usage_client(rows):
    session = FakeUsageSession(rows)
    app.dependency_overrides[require_service_token] = lambda: None
    app.dependency_overrides[admin.get_admin_session] = lambda: session
    return TestClient(app), session


def test_usage_returns_the_frozen_contract_fields():
    client, _ = _usage_client([_usage_row()])
    resp = client.get(
        "/v1/admin/usage?since=2026-08-01T00:00:00Z&until=2026-08-08T00:00:00Z",
        headers=SERVICE_HEADERS,
    )
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert set(item) == {
        "workspace_id",
        "product_id",
        "cycle_ts",
        "links_total",
        "links_succeeded",
        "protected_links_attempted",
        "protected_links_succeeded",
        "check_successful",
    }


def test_usage_returns_the_items_next_cursor_envelope():
    client, _ = _usage_client([_usage_row()])
    resp = client.get(
        "/v1/admin/usage?since=2026-08-01T00:00:00Z&until=2026-08-08T00:00:00Z",
        headers=SERVICE_HEADERS,
    )
    body = resp.json()
    assert set(body) == {"items", "next_cursor"}
    assert body["next_cursor"] is None


def test_usage_emits_a_cursor_when_more_rows_exist():
    rows = [_usage_row() for _ in range(3)]
    client, _ = _usage_client(rows)
    resp = client.get(
        "/v1/admin/usage?since=2026-08-01T00:00:00Z&until=2026-08-08T00:00:00Z&limit=2",
        headers=SERVICE_HEADERS,
    )
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"] is not None


def test_usage_window_over_31_days_is_422():
    client, _ = _usage_client([])
    resp = client.get(
        "/v1/admin/usage?since=2026-01-01T00:00:00Z&until=2026-06-01T00:00:00Z",
        headers=SERVICE_HEADERS,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"]["code"] == "WINDOW_TOO_LARGE"


def test_usage_bad_cursor_is_422():
    client, _ = _usage_client([])
    resp = client.get(
        "/v1/admin/usage?since=2026-08-01T00:00:00Z&until=2026-08-08T00:00:00Z&cursor=%21%21%21",
        headers=SERVICE_HEADERS,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"]["code"] == "INVALID_CURSOR"


def test_usage_requires_since_and_until():
    client, _ = _usage_client([])
    resp = client.get("/v1/admin/usage", headers=SERVICE_HEADERS)
    assert resp.status_code == 422
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `/snap/bin/uv run pytest tests/unit/test_admin_router.py -q`
Expected: FAIL — 404 on `/v1/admin/usage`.

- [ ] **Step 4: Add the endpoint**

Append to `apps/api/app/routers/admin.py` (and extend its imports):

```python
@router.get("/usage", response_model=UsageListResponse)
def export_usage(
    since: datetime,
    until: datetime,
    cursor: str | None = None,
    limit: int | None = None,
    session: Session = Depends(get_admin_session),
) -> UsageListResponse:
    """`GET /v1/admin/usage` — the SaaS metering feed (PLAN §7.2).

    Cursor-paginated, idempotent, window capped at 31 days. The response
    field names are a frozen contract — see `app.schemas.admin.UsageRow`.
    """
    try:
        validate_window(since, until)
    except UsageWindowTooLarge as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "WINDOW_TOO_LARGE", "message": str(exc)}},
        ) from exc

    after: UsageCursor | None = None
    if cursor is not None:
        try:
            after = decode_usage_cursor(cursor)
        except InvalidUsageCursor as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": {"code": "INVALID_CURSOR", "message": str(exc)}},
            ) from exc

    page_limit = clamp_usage_limit(limit)
    rows = list(
        session.execute(
            build_usage_query(
                since=since, until=until, after=after, limit=page_limit
            )
        ).all()
    )

    has_more = len(rows) > page_limit
    page = rows[:page_limit]
    next_cursor = (
        encode_usage_cursor(
            UsageCursor(
                cycle_ts=page[-1].cycle_ts,
                workspace_id=page[-1].workspace_id,
                product_id=page[-1].product_id,
            )
        )
        if has_more and page
        else None
    )

    return UsageListResponse(
        items=[UsageRow.model_validate(row, from_attributes=True) for row in page],
        next_cursor=next_cursor,
    )
```

Add to the imports at the top of `admin.py`:

```python
from datetime import datetime

from app.schemas.admin import UsageListResponse, UsageRow
from app.services.admin_usage import (
    InvalidUsageCursor,
    UsageCursor,
    UsageWindowTooLarge,
    build_usage_query,
    clamp_usage_limit,
    decode_usage_cursor,
    encode_usage_cursor,
    validate_window,
)
```

`UsageRow` needs `model_config = ConfigDict(from_attributes=True)` — add it in `app/schemas/admin.py` if the validate call complains.

- [ ] **Step 5: Run tests to verify they pass**

Run: `/snap/bin/uv run pytest tests/unit/test_admin_router.py -q`
Expected: PASS

- [ ] **Step 6: Run the full unit suite**

Run: `/snap/bin/uv run pytest tests/unit -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add apps/api/app/routers/admin.py apps/api/app/schemas/admin.py tests/unit/test_admin_router.py tests/unit/_admin_fake_session.py
git commit -m "feat(api): cursor-paginated GET /v1/admin/usage export"
```

---

### Task 5: Covering indexes migration (risk P2)

**Files:**
- Create: `alembic/versions/<rev>_usage_export_indexes.py`
- Test: `tests/unit/test_migration_offline_usage_indexes.py`

**Interfaces:**
- Consumes: alembic head `03dec3037c8f`.
- Produces: indexes `ix_request_attempts_workspace_id_created_at` on `request_attempts (workspace_id, created_at)` and `ix_price_observations_workspace_id_scraped_at` on `price_observations (workspace_id, scraped_at)`; new alembic head.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_migration_offline_usage_indexes.py`, modelled on an existing `tests/unit/test_migration_offline_*.py` (open one first and copy its `subprocess`/`REPO_ROOT` boilerplate exactly):

```python
"""Offline DDL assertions for the usage-export covering indexes (PLAN risk P2)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _offline_sql() -> str:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_request_attempts_covering_index_is_created() -> None:
    sql = _offline_sql()
    assert "ix_request_attempts_workspace_id_created_at" in sql


def test_price_observations_covering_index_is_created() -> None:
    sql = _offline_sql()
    assert "ix_price_observations_workspace_id_scraped_at" in sql


def test_single_alembic_head() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    heads = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(heads) == 1, result.stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/snap/bin/uv run pytest tests/unit/test_migration_offline_usage_indexes.py -q`
Expected: FAIL — index names absent from the rendered DDL.

- [ ] **Step 3: Generate and write the migration**

```bash
cd /srv/crawmatic/crawmatic && /snap/bin/uv run alembic revision -m "usage_export_indexes"
```

Edit the generated file so `down_revision = "03dec3037c8f"` and the body is:

```python
"""Covering indexes for the SaaS usage export (PLAN §7.2, risk P2).

Both tables are RANGE-partitioned (`request_attempts` on `created_at`,
`price_observations` on `scraped_at`), so an index created on the parent
propagates to every existing and future partition — this is why the
export can bound its window on the partition key and still get an
index-backed scan per pruned partition.

`(workspace_id, <time>)` and not `(<time>, workspace_id)`: the export
groups by workspace after pruning to the window's partitions, so
workspace is the selective leading column within a partition.
"""

from alembic import op

revision = "<generated>"
down_revision = "03dec3037c8f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_request_attempts_workspace_id_created_at",
        "request_attempts",
        ["workspace_id", "created_at"],
    )
    op.create_index(
        "ix_price_observations_workspace_id_scraped_at",
        "price_observations",
        ["workspace_id", "scraped_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_price_observations_workspace_id_scraped_at",
        table_name="price_observations",
    )
    op.drop_index(
        "ix_request_attempts_workspace_id_created_at", table_name="request_attempts"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/snap/bin/uv run pytest tests/unit/test_migration_offline_usage_indexes.py -q && bash scripts/check_single_head.sh`
Expected: PASS, one head.

- [ ] **Step 5: Run the full unit suite**

Run: `/snap/bin/uv run pytest tests/unit -q`
Expected: all pass (other `test_migration_offline_*` tests may assert the head revision — if one hard-codes `03dec3037c8f` as *the* head, update it to the new revision id).

- [ ] **Step 6: Commit**

```bash
git add alembic/versions tests/unit/test_migration_offline_usage_indexes.py
git commit -m "feat(db): covering indexes for the partitioned usage export"
```

---

### Task 6: Consistent error envelope

**Files:**
- Create: `apps/api/app/error_envelope.py`
- Modify: `apps/api/app/main.py`
- Test: `tests/unit/test_error_envelope.py`

**Interfaces:**
- Produces: `register_error_handlers(app: FastAPI) -> None`, adding handlers for `HTTPException`, `RequestValidationError`, and `Exception`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_error_envelope.py`:

```python
"""Every error response carries one top-level `{"error": {...}}` (PLAN §7.4)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.deps import Principal, get_current_principal
from app.main import app
from unit._jobs_fake_session import FakeOrmSession

WORKSPACE_ID = uuid.uuid4()


@pytest.fixture(autouse=True)
def _clear() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _principal(scopes: list[str]):
    session = FakeOrmSession()

    def _dep() -> Iterator[tuple[FakeOrmSession, Principal]]:
        yield session, Principal(
            kind="api_key",
            id=uuid.uuid4(),
            role=None,
            scopes=scopes,
            workspace_id=WORKSPACE_ID,
        )

    return _dep


def test_401_has_top_level_error_object(client):
    resp = client.get("/v1/products")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTH_FAILED"
    assert "message" in resp.json()["error"]


def test_403_has_top_level_error_object(client):
    app.dependency_overrides[get_current_principal] = _principal([])
    resp = client.get("/v1/products")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"


def test_404_has_top_level_error_object(client):
    app.dependency_overrides[get_current_principal] = _principal(
        ["products:read"]
    )
    resp = client.get(f"/v1/products/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_legacy_detail_shape_is_preserved(client):
    """1768 existing tests read `detail.error.code` / `detail.code`."""
    app.dependency_overrides[get_current_principal] = _principal(
        ["products:read"]
    )
    resp = client.get(f"/v1/products/{uuid.uuid4()}")
    assert resp.json()["detail"]["error"]["code"] == "NOT_FOUND"


def test_request_validation_error_is_enveloped(client):
    app.dependency_overrides[get_current_principal] = _principal(
        ["products:read"]
    )
    resp = client.get("/v1/products/not-a-uuid")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert isinstance(resp.json()["detail"], list)


def test_error_code_is_a_stable_string_for_an_unlabelled_http_error(client):
    from fastapi import HTTPException

    @app.get("/_test_plain_error")
    def _plain():
        raise HTTPException(status_code=418, detail="I am a teapot")

    try:
        resp = client.get("/_test_plain_error")
        assert resp.status_code == 418
        assert resp.json()["error"]["code"] == "HTTP_418"
        assert resp.json()["error"]["message"] == "I am a teapot"
    finally:
        app.router.routes = [
            r for r in app.router.routes if getattr(r, "path", "") != "/_test_plain_error"
        ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/snap/bin/uv run pytest tests/unit/test_error_envelope.py -q`
Expected: FAIL — `KeyError: 'error'`

- [ ] **Step 3: Write the handlers**

Create `apps/api/app/error_envelope.py`:

```python
"""One error envelope for every failure mode (PLAN §7.4).

The engine grew three different error shapes:
`{"detail": {"error": {code, message}}}` (routers),
`{"detail": {code, message}}` (`app.errors` helpers), and FastAPI's own
`{"detail": [...]}` for request validation. A public API needs one.

These handlers are deliberately **additive**: they promote whichever
shape they find to a top-level `{"error": {"code", "message"}}` while
leaving `detail` byte-identical. Every existing test — and every
existing internal consumer — keeps reading `detail`; external customers
read `error` and never have to branch. Removing `detail` later is a
breaking change to be made on its own, with its own migration note.

Unhandled exceptions become `{"error": {"code": "INTERNAL_ERROR"}}` with
a fixed message: an exception string can carry a SQL fragment, a URL
with credentials, or a row of customer data, and this is a
customer-facing surface.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def _error_object(status_code: int, detail: Any) -> dict[str, str]:
    """Promote any of the three legacy detail shapes to `{code, message}`."""
    if isinstance(detail, dict):
        nested = detail.get("error")
        if isinstance(nested, dict) and "code" in nested:
            return {
                "code": str(nested.get("code")),
                "message": str(nested.get("message", "")),
            }
        if "code" in detail:
            return {
                "code": str(detail.get("code")),
                "message": str(detail.get("message", "")),
            }
    if isinstance(detail, str):
        return {"code": f"HTTP_{status_code}", "message": detail}
    return {"code": f"HTTP_{status_code}", "message": "Request failed."}


async def _http_exception_handler(
    request: Request, exc: HTTPException
) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": _error_object(exc.status_code, exc.detail),
            "detail": exc.detail,
        },
        headers=getattr(exc, "headers", None),
    )


async def _validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "The request body or parameters failed validation.",
            },
            "detail": _jsonable(exc.errors()),
        },
    )


def _jsonable(errors: Any) -> Any:
    """FastAPI validation errors can carry non-JSON `ctx` values."""
    from fastapi.encoders import jsonable_encoder

    return jsonable_encoder(errors)


async def _unhandled_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "An internal error occurred.",
            },
            "detail": "An internal error occurred.",
        },
    )


def register_error_handlers(app: FastAPI) -> None:
    """Install the envelope handlers. Call once, at app construction."""
    from starlette.exceptions import HTTPException as StarletteHTTPException

    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(HTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)
```

In `apps/api/app/main.py`, after `app = FastAPI(...)`:

```python
from app.error_envelope import register_error_handlers

register_error_handlers(app)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/snap/bin/uv run pytest tests/unit/test_error_envelope.py -q`
Expected: PASS

- [ ] **Step 5: Run the full unit suite — the critical regression gate**

Run: `/snap/bin/uv run pytest tests/unit -q`
Expected: all pass. Any failure here means the envelope stopped being additive — fix `_error_object` / the handler, **never** the existing test.

- [ ] **Step 6: Commit**

```bash
git add apps/api/app/error_envelope.py apps/api/app/main.py tests/unit/test_error_envelope.py
git commit -m "feat(api): consistent top-level error envelope on every response"
```

---

### Task 7: Per-key rate limiting (risk P5)

**Files:**
- Create: `apps/api/app/rate_limit.py`
- Modify: `apps/api/app/main.py`, `libs/shared/app_shared/config.py`, `.env.example`
- Test: `tests/unit/test_rate_limit_middleware.py`

**Interfaces:**
- Consumes: `app_shared.redis_client.get_redis_client`, `app_shared.config.get_settings`.
- Produces:
  - `Settings.API_RATE_LIMIT_READ_PER_MINUTE: int = 60`, `API_RATE_LIMIT_WRITE_PER_MINUTE: int = 10`, `API_RATE_LIMIT_ENABLED: bool = True`
  - `class RateLimitMiddleware(BaseHTTPMiddleware)`
  - `rate_limit_identity(request) -> str | None`, `is_write(method: str) -> bool`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_rate_limit_middleware.py`:

```python
"""Per-key rate limiting (PLAN §7.4, risk P5)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.rate_limit import RateLimitMiddleware, is_write, rate_limit_identity


class FakeRedis:
    def __init__(self, *, fail: bool = False) -> None:
        self.counters: dict[str, int] = {}
        self.fail = fail

    def incr(self, key: str) -> int:
        if self.fail:
            raise ConnectionError("redis down")
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    def expire(self, key: str, seconds: int) -> bool:
        if self.fail:
            raise ConnectionError("redis down")
        return True


def _app(redis, read: int = 3, write: int = 2) -> FastAPI:
    application = FastAPI()
    application.add_middleware(
        RateLimitMiddleware,
        redis_factory=lambda: redis,
        read_per_minute=read,
        write_per_minute=write,
        enabled=True,
    )

    @application.get("/v1/things")
    def _read():
        return {"ok": True}

    @application.post("/v1/things")
    def _write():
        return {"ok": True}

    @application.get("/health")
    def _health():
        return {"status": "ok"}

    return application


HEADERS = {"Authorization": "Bearer ck_abcdef0123456789"}


def test_reads_under_the_limit_pass():
    client = TestClient(_app(FakeRedis()))
    for _ in range(3):
        assert client.get("/v1/things", headers=HEADERS).status_code == 200


def test_read_over_the_limit_is_429_with_retry_after():
    client = TestClient(_app(FakeRedis()))
    for _ in range(3):
        client.get("/v1/things", headers=HEADERS)
    resp = client.get("/v1/things", headers=HEADERS)
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "RATE_LIMITED"
    assert int(resp.headers["Retry-After"]) > 0


def test_writes_have_their_own_lower_budget():
    client = TestClient(_app(FakeRedis()))
    assert client.post("/v1/things", headers=HEADERS).status_code == 200
    assert client.post("/v1/things", headers=HEADERS).status_code == 200
    assert client.post("/v1/things", headers=HEADERS).status_code == 429


def test_reads_and_writes_do_not_share_a_budget():
    client = TestClient(_app(FakeRedis()))
    for _ in range(3):
        client.get("/v1/things", headers=HEADERS)
    assert client.post("/v1/things", headers=HEADERS).status_code == 200


def test_two_keys_have_independent_budgets():
    client = TestClient(_app(FakeRedis()))
    other = {"Authorization": "Bearer ck_zzzzzzzzzzzzzzzz"}
    for _ in range(3):
        client.get("/v1/things", headers=HEADERS)
    assert client.get("/v1/things", headers=other).status_code == 200


def test_health_is_never_limited():
    client = TestClient(_app(FakeRedis(), read=1))
    for _ in range(5):
        assert client.get("/health").status_code == 200


def test_unauthenticated_requests_are_not_limited_here():
    """No credential means auth will 401 anyway; don't spend Redis on it."""
    client = TestClient(_app(FakeRedis(), read=1))
    for _ in range(5):
        assert client.get("/v1/things").status_code == 200


def test_redis_outage_fails_open():
    client = TestClient(_app(FakeRedis(fail=True), read=1))
    for _ in range(5):
        assert client.get("/v1/things", headers=HEADERS).status_code == 200


def test_identity_never_contains_the_raw_secret():
    class _Req:
        headers = {"Authorization": "Bearer ck_supersecretvalue"}

    identity = rate_limit_identity(_Req())
    assert identity is not None
    assert "supersecretvalue" not in identity


def test_write_classification():
    assert is_write("POST") and is_write("PATCH") and is_write("PUT")
    assert is_write("DELETE")
    assert not is_write("GET")
    assert not is_write("HEAD")
    assert not is_write("OPTIONS")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/snap/bin/uv run pytest tests/unit/test_rate_limit_middleware.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.rate_limit'`

- [ ] **Step 3: Add the config knobs**

In `libs/shared/app_shared/config.py`:

```python
    # --- Public API rate limits (PLAN §7.4, risk P5) ---
    # Per-credential fixed-window budgets. Reads are cheap and get the
    # generous budget; writes touch the catalog and get a tenth of it.
    # Tunable per deployment because a plugin doing a bulk catalog sync
    # has a legitimately different shape from a dashboard.
    API_RATE_LIMIT_ENABLED: bool = True
    API_RATE_LIMIT_READ_PER_MINUTE: int = 60
    API_RATE_LIMIT_WRITE_PER_MINUTE: int = 10
```

In `.env.example`:

```
# Public API per-key rate limits (requests per minute, per credential).
API_RATE_LIMIT_ENABLED=true
API_RATE_LIMIT_READ_PER_MINUTE=60
API_RATE_LIMIT_WRITE_PER_MINUTE=10
```

- [ ] **Step 4: Write the middleware**

Create `apps/api/app/rate_limit.py`:

```python
"""Per-credential rate limiting for the public API (PLAN §7.4, risk P5).

A fixed window, not a token bucket: the contract we publish is "60 reads
and 10 writes per minute", and a fixed window is the one algorithm whose
429 a customer can reason about without reading our source. The window
key embeds the wall-clock minute, so the counter expires by itself and
there is no sweeper.

**Identity** is `sha256(credential)`, never the credential: rate-limit
keys reach Redis, logs, and metrics, and an API key in any of those is
an API key leak. Requests with no bearer credential are not limited here
— they are about to fail auth anyway, and spending a Redis round-trip on
them would make unauthenticated traffic cheaper to amplify, not harder.

**Fail-open on Redis errors.** The login limiter in
`app_shared.security.rate_limit` fails *closed* because it guards
credentials, where refusing everyone briefly is the safe default. This
one guards **cost**, and the real cost guards are elsewhere (the
per-workspace domain limit, the per-product protected-link cap, and the
direct-HTTP default for unknown domains — PLAN §7.4). Failing closed
here would turn a Redis blip into a total outage of a paid API, which is
a strictly worse failure than a minute of unmetered reads.

Exempt paths: `/health` (liveness must never depend on Redis) and
`/v1/auth/*` (already limited, per-account and per-IP, by
`app_shared.security.rate_limit.check_and_increment_login`).
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Awaitable, Callable

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app_shared.redis_client import get_redis_client

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 60
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_EXEMPT_PREFIXES = ("/health", "/v1/auth")
_BEARER = "Bearer "


def is_write(method: str) -> bool:
    """Budget class for an HTTP method."""
    return method.upper() in _WRITE_METHODS


def rate_limit_identity(request: object) -> str | None:
    """`sha256` of the bearer credential, or `None` if there is none.

    Never returns the credential itself — see the module docstring.
    """
    authorization = request.headers.get("Authorization")
    if not authorization or not authorization.startswith(_BEARER):
        return None
    credential = authorization[len(_BEARER) :].strip()
    if not credential:
        return None
    return hashlib.sha256(credential.encode()).hexdigest()[:32]


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window per-credential limiter. 429 + `Retry-After` on refusal."""

    def __init__(
        self,
        app,
        *,
        redis_factory: Callable[[], object] = get_redis_client,
        read_per_minute: int | None = None,
        write_per_minute: int | None = None,
        enabled: bool | None = None,
    ) -> None:
        super().__init__(app)
        self._redis_factory = redis_factory
        self._read_per_minute = read_per_minute
        self._write_per_minute = write_per_minute
        self._enabled = enabled

    def _limits(self) -> tuple[bool, int, int]:
        if self._enabled is not None and self._read_per_minute is not None:
            return (
                self._enabled,
                self._read_per_minute,
                self._write_per_minute or self._read_per_minute,
            )
        from app_shared.config import get_settings

        settings = get_settings()
        return (
            settings.API_RATE_LIMIT_ENABLED,
            settings.API_RATE_LIMIT_READ_PER_MINUTE,
            settings.API_RATE_LIMIT_WRITE_PER_MINUTE,
        )

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        enabled, read_limit, write_limit = self._limits()
        path = request.url.path

        if not enabled or path.startswith(_EXEMPT_PREFIXES):
            return await call_next(request)

        identity = rate_limit_identity(request)
        if identity is None:
            return await call_next(request)

        write = is_write(request.method)
        limit = write_limit if write else read_limit
        bucket = "w" if write else "r"
        now = int(time.time())
        window = now // WINDOW_SECONDS
        key = f"rl:api:{bucket}:{identity}:{window}"
        retry_after = WINDOW_SECONDS - (now % WINDOW_SECONDS) or WINDOW_SECONDS

        try:
            redis_client = self._redis_factory()
            count = redis_client.incr(key)
            if count == 1:
                redis_client.expire(key, WINDOW_SECONDS * 2)
        except Exception:  # noqa: BLE001 - fail-open, see module docstring
            logger.warning("rate limiter unavailable; allowing request", exc_info=True)
            return await call_next(request)

        if count > limit:
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": str(retry_after)},
                content={
                    "error": {
                        "code": "RATE_LIMITED",
                        "message": (
                            f"Rate limit exceeded: {limit} "
                            f"{'write' if write else 'read'} requests per minute."
                        ),
                    },
                    "detail": {
                        "error": {
                            "code": "RATE_LIMITED",
                            "message": "Too many requests.",
                        }
                    },
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(max(0, limit - count))
        return response
```

In `apps/api/app/main.py`, after `register_error_handlers(app)`:

```python
from app.rate_limit import RateLimitMiddleware

app.add_middleware(RateLimitMiddleware)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `/snap/bin/uv run pytest tests/unit/test_rate_limit_middleware.py -q`
Expected: PASS

- [ ] **Step 6: Run the full unit suite**

Run: `/snap/bin/uv run pytest tests/unit -q`
Expected: all pass. The middleware is now on the shared `app` object, so any existing test sending an `Authorization` header more than 60 times in one minute could newly 429. If that happens, do **not** weaken the middleware — set `API_RATE_LIMIT_ENABLED=false` for the test process by adding a module-scoped `monkeypatch` in the offending test, or give the limiter a `settings`-independent disable when `get_settings()` raises (which is what happens with no `.env`).

- [ ] **Step 7: Commit**

```bash
git add apps/api/app/rate_limit.py apps/api/app/main.py libs/shared/app_shared/config.py .env.example tests/unit/test_rate_limit_middleware.py
git commit -m "feat(api): per-key fixed-window rate limiting with 429 and Retry-After"
```

---

### Task 8: Batch caps on bulk endpoints

**Files:**
- Create: `apps/api/app/limits.py`
- Modify: `apps/api/app/routers/products.py`, `variants.py`, `matches.py`, `scrape_profiles.py`
- Test: `tests/unit/test_batch_caps.py`

**Interfaces:**
- Produces: `MAX_BULK_ITEMS = 500`; `enforce_batch_cap(items: Sequence[object], *, what: str) -> None` raising `HTTPException(422, BATCH_TOO_LARGE)`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_batch_caps.py`:

```python
"""Batch caps on every bulk endpoint (PLAN §7.4: <=500 items/request)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.deps import Principal, get_current_principal
from app.limits import MAX_BULK_ITEMS
from app.main import app
from unit._jobs_fake_session import FakeOrmSession

WORKSPACE_ID = uuid.uuid4()


@pytest.fixture(autouse=True)
def _clear() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _authorize(scopes: list[str]) -> None:
    session = FakeOrmSession()

    def _dep() -> Iterator[tuple[FakeOrmSession, Principal]]:
        yield session, Principal(
            kind="api_key",
            id=uuid.uuid4(),
            role=None,
            scopes=scopes,
            workspace_id=WORKSPACE_ID,
        )

    app.dependency_overrides[get_current_principal] = _dep


def test_cap_is_500():
    assert MAX_BULK_ITEMS == 500


@pytest.mark.parametrize(
    ("path", "scopes", "item"),
    [
        ("/v1/products/bulk-upsert", ["products:write"], {"title": "x"}),
        ("/v1/variants/bulk-upsert", ["variants:write"], {"title": "x"}),
        ("/v1/matches/bulk-upsert", ["matches:write"], {"competitor_url": "https://e.com/p"}),
        (
            "/v1/scrape-profiles/bulk-upsert",
            ["scrape_profiles:write"],
            {"name": "x"},
        ),
    ],
)
def test_over_cap_is_rejected(client, path, scopes, item):
    _authorize(scopes)
    body = {"items": [item] * (MAX_BULK_ITEMS + 1)}
    resp = client.post(path, json=body)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "BATCH_TOO_LARGE"
```

The request body key for each bulk endpoint may not be `items` — open each router's bulk handler and its schema first, and use the real field name and a minimally valid item. The point of the test is the **cap**, so the payload only has to get past parsing far enough to hit the guard; put the guard **before** per-item validation so an over-cap request is refused cheaply.

- [ ] **Step 2: Run test to verify it fails**

Run: `/snap/bin/uv run pytest tests/unit/test_batch_caps.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.limits'`

- [ ] **Step 3: Write the shared limits module**

Create `apps/api/app/limits.py`:

```python
"""Input caps for the public API (PLAN §7.4, risks P5/P6).

One module so the numbers are greppable and identical everywhere. Each
cap exists to bound work a customer can ask for in a single request; the
guards raise before any per-item work happens, so an over-cap request
costs parsing and nothing else.
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import HTTPException

#: Maximum items in one bulk-upsert request (PLAN §7.4). Matches the
#: Connect API's `catalog/batch` cap (§7.5) so a plugin can use one
#: chunk size against both surfaces.
MAX_BULK_ITEMS = 500

#: Distinct competitor domains one workspace may register (PLAN §7.4).
#: Admin-raisable; the default is generous for a real store and narrow
#: enough that a hostile customer cannot fan out across the web.
MAX_DOMAINS_PER_WORKSPACE = 50

#: Protected-marketplace links per product (PLAN §5.1 policy guard).
#: Keeps the worst-case Marketplace check profitable (§5.7).
MAX_PROTECTED_LINKS_PER_PRODUCT = 4


def enforce_batch_cap(items: Sequence[object], *, what: str) -> None:
    """Raise 422 `BATCH_TOO_LARGE` when a batch exceeds `MAX_BULK_ITEMS`."""
    if len(items) > MAX_BULK_ITEMS:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": "BATCH_TOO_LARGE",
                    "message": (
                        f"A single request may contain at most {MAX_BULK_ITEMS} "
                        f"{what}; received {len(items)}."
                    ),
                }
            },
        )
```

- [ ] **Step 4: Add the guard to each bulk handler**

In each of the four bulk-upsert handlers, as the **first** statement of the function body:

```python
    enforce_batch_cap(payload.items, what="products")   # variants / matches / scrape profiles
```

using the real payload attribute name for that endpoint. Add `from app.limits import enforce_batch_cap` to each router's imports.

- [ ] **Step 5: Run tests to verify they pass**

Run: `/snap/bin/uv run pytest tests/unit/test_batch_caps.py -q`
Expected: PASS

- [ ] **Step 6: Run the full unit suite**

Run: `/snap/bin/uv run pytest tests/unit -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add apps/api/app/limits.py apps/api/app/routers tests/unit/test_batch_caps.py
git commit -m "feat(api): cap bulk-upsert requests at 500 items"
```

---

### Task 9: Competitor-URL abuse controls

**Files:**
- Modify: `apps/api/app/routers/matches.py`, `apps/api/app/routers/competitors.py`
- Test: `tests/unit/test_competitor_url_limits.py`

**Interfaces:**
- Consumes: `app.limits.MAX_DOMAINS_PER_WORKSPACE`, `MAX_PROTECTED_LINKS_PER_PRODUCT`; the existing `app_shared.url_safety.validate_competitor_url` already wired into match creation.
- Produces: 422 `DOMAIN_LIMIT_REACHED`, 422 `PROTECTED_LINK_CAP_REACHED`; a `DomainAccessRule` row created with the workspace-default (direct-HTTP) policy for a previously unknown domain.

- [ ] **Step 1: Read the current behaviour**

```bash
sed -n '1,120p' /srv/crawmatic/crawmatic/apps/api/app/routers/matches.py
sed -n '1,80p' /srv/crawmatic/crawmatic/apps/api/app/routers/competitors.py
grep -n "validate_competitor_url" -r /srv/crawmatic/crawmatic/apps/api /srv/crawmatic/crawmatic/libs/shared
```

Confirm where SSRF validation already happens on `POST /v1/matches` and `POST /v1/competitors`. **Do not re-implement it** — it exists; this task adds only the three cost controls on top.

- [ ] **Step 2: Write the failing test**

Create `tests/unit/test_competitor_url_limits.py`. Use the `FakeOrmSession` + `dependency_overrides` pattern from `tests/unit/test_alerts_router.py`. Seed the fake session with `Competitor` rows to drive the domain count, and `CompetitorProductMatch` rows to drive the protected-link count.

```python
"""Cost controls on user-supplied competitor URLs (PLAN §7.4)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.deps import Principal, get_current_principal
from app.limits import MAX_DOMAINS_PER_WORKSPACE, MAX_PROTECTED_LINKS_PER_PRODUCT
from app.main import app
from unit._jobs_fake_session import FakeOrmSession

WORKSPACE_ID = uuid.uuid4()


@pytest.fixture(autouse=True)
def _clear() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


@pytest.fixture()
def session() -> FakeOrmSession:
    return FakeOrmSession()


@pytest.fixture()
def client(session: FakeOrmSession) -> TestClient:
    def _dep() -> Iterator[tuple[FakeOrmSession, Principal]]:
        yield session, Principal(
            kind="api_key",
            id=uuid.uuid4(),
            role=None,
            scopes=[
                "competitors:read",
                "competitors:write",
                "matches:read",
                "matches:write",
                "domain_rules:read",
                "domain_rules:write",
            ],
            workspace_id=WORKSPACE_ID,
        )

    app.dependency_overrides[get_current_principal] = _dep
    return TestClient(app)


def test_defaults_are_the_documented_numbers():
    assert MAX_DOMAINS_PER_WORKSPACE == 50
    assert MAX_PROTECTED_LINKS_PER_PRODUCT == 4


def test_new_domain_under_the_limit_is_accepted(client, session):
    resp = client.post(
        "/v1/competitors", json={"name": "Example", "domain": "example.com"}
    )
    assert resp.status_code == 201


def test_domain_limit_is_enforced(client, session):
    from app_shared.models.competitors_matches import Competitor

    for index in range(MAX_DOMAINS_PER_WORKSPACE):
        session.seed(
            Competitor(
                id=uuid.uuid4(),
                workspace_id=WORKSPACE_ID,
                name=f"c{index}",
                domain=f"shop{index}.example",
            )
        )
    resp = client.post(
        "/v1/competitors", json={"name": "One too many", "domain": "overflow.example"}
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "DOMAIN_LIMIT_REACHED"


def test_existing_domain_does_not_count_against_the_limit(client, session):
    from app_shared.models.competitors_matches import Competitor

    for index in range(MAX_DOMAINS_PER_WORKSPACE):
        session.seed(
            Competitor(
                id=uuid.uuid4(),
                workspace_id=WORKSPACE_ID,
                name=f"c{index}",
                domain=f"shop{index}.example",
            )
        )
    resp = client.post(
        "/v1/competitors", json={"name": "dup", "domain": "shop0.example"}
    )
    assert resp.status_code in (200, 201, 409)
    assert resp.status_code != 422
```

Add two more tests for the protected-link cap on `POST /v1/matches`: one seeding `MAX_PROTECTED_LINKS_PER_PRODUCT` existing matches whose domain resolves to a protected access policy and asserting `422 PROTECTED_LINK_CAP_REACHED`, and one asserting a match on a **direct-HTTP** domain is unaffected by the cap. Determine "protected" by the resolved `AccessPolicy.strategy` — reuse `app.services.access_resolution`, do not invent a second classifier. And one test asserting that creating a match for a domain with **no** `DomainAccessRule` leaves the workspace's default (direct-HTTP) policy in effect and does not create a proxy-enabled rule.

- [ ] **Step 3: Run tests to verify they fail**

Run: `/snap/bin/uv run pytest tests/unit/test_competitor_url_limits.py -q`
Expected: FAIL — the guards do not exist.

- [ ] **Step 4: Implement the guards**

In `apps/api/app/routers/competitors.py`, in the create handler, before the insert:

```python
    existing_domains = session.execute(
        scoped_select(Competitor.domain, principal.workspace_id).distinct()
    ).scalars().all()
    if (
        payload.domain not in existing_domains
        and len(existing_domains) >= MAX_DOMAINS_PER_WORKSPACE
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": "DOMAIN_LIMIT_REACHED",
                    "message": (
                        f"This workspace may track at most "
                        f"{MAX_DOMAINS_PER_WORKSPACE} competitor domains. "
                        "Contact support to raise the limit."
                    ),
                }
            },
        )
```

If `scoped_select` does not accept a column expression, select the model and count distinct domains in the query with `func.count(func.distinct(Competitor.domain))` — still one SQL round-trip, never a Python scan of every competitor.

In `apps/api/app/routers/matches.py`, in the single create handler (and the bulk path if it accepts new URLs), after URL validation and before the insert, count the product's existing matches whose resolved policy is proxy/browser-capable and refuse over the cap with `PROTECTED_LINK_CAP_REACHED`.

Document the unknown-domain default in the `matches.py` module docstring:

```
User-supplied competitor URLs (PLAN §7.4) are a cost surface, not just a
safety one. A domain with no `DomainAccessRule` inherits the workspace
default policy, which is direct-HTTP — a customer therefore cannot make
us spend residential-proxy budget on an arbitrary host simply by pasting
its URL. A domain only becomes proxy-eligible when an operator
classifies it.
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `/snap/bin/uv run pytest tests/unit/test_competitor_url_limits.py -q`
Expected: PASS

- [ ] **Step 6: Run the full unit suite**

Run: `/snap/bin/uv run pytest tests/unit -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add apps/api/app/routers/competitors.py apps/api/app/routers/matches.py tests/unit/test_competitor_url_limits.py
git commit -m "feat(api): per-workspace domain limit and per-product protected-link cap"
```

---

### Task 10: Cleaned public OpenAPI spec

**Files:**
- Create: `apps/api/app/openapi_public.py`
- Create: `scripts/export_openapi.py`
- Modify: `apps/api/app/main.py`
- Test: `tests/unit/test_public_openapi.py`

**Interfaces:**
- Produces: `INTERNAL_TAGS: frozenset[str]`; `build_public_openapi(app: FastAPI) -> dict`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_public_openapi.py`:

```python
"""The external OpenAPI spec excludes internal surfaces (PLAN §7.4)."""

from __future__ import annotations

from app.main import app
from app.openapi_public import INTERNAL_TAGS, build_public_openapi


def _spec() -> dict:
    return build_public_openapi(app)


def test_internal_tags_are_the_documented_three():
    assert INTERNAL_TAGS == frozenset(
        {"proxy-providers", "access-policies", "admin"}
    )


def test_admin_paths_are_absent():
    paths = _spec()["paths"]
    assert not [p for p in paths if p.startswith("/v1/admin")]


def test_proxy_provider_paths_are_absent():
    paths = _spec()["paths"]
    assert not [p for p in paths if p.startswith("/v1/proxy-providers")]


def test_access_policy_paths_are_absent():
    paths = _spec()["paths"]
    assert not [p for p in paths if p.startswith("/v1/access-policies")]


def test_the_public_product_surface_is_present():
    paths = _spec()["paths"]
    for expected in (
        "/v1/products",
        "/v1/competitors",
        "/v1/matches",
        "/v1/refresh-rules",
    ):
        assert expected in paths, expected


def test_spec_has_bearer_security_scheme_documented():
    spec = _spec()
    schemes = spec.get("components", {}).get("securitySchemes", {})
    assert "bearerAuth" in schemes
    assert schemes["bearerAuth"]["scheme"] == "bearer"


def test_no_orphan_schema_references_remain():
    """Stripping paths must not leave a $ref pointing at a removed schema."""
    import json

    spec = _spec()
    blob = json.dumps(spec)
    defined = set(spec.get("components", {}).get("schemas", {}))
    referenced = {
        part.split('"')[0]
        for part in blob.split("#/components/schemas/")[1:]
    }
    assert referenced <= defined, referenced - defined
```

Check the real tag strings first (`grep -n 'tags=' apps/api/app/routers/*.py`) and use them verbatim in `INTERNAL_TAGS` and in the first test.

- [ ] **Step 2: Run test to verify it fails**

Run: `/snap/bin/uv run pytest tests/unit/test_public_openapi.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.openapi_public'`

- [ ] **Step 3: Write the builder**

Create `apps/api/app/openapi_public.py`:

```python
"""The customer-facing OpenAPI spec (PLAN §7.4).

The engine's full spec documents surfaces no customer should see: proxy
providers and access policies configure what we are willing to spend on
a fetch, and `/v1/admin` provisions workspaces with a shared secret.
Publishing them would be an information leak and an invitation.

Filtering by **tag** rather than by path prefix means a new internal
router is excluded the moment it is tagged, without anyone remembering
to update a prefix list.

Schemas are pruned to those still reachable from the surviving paths, so
the published document has no dangling `$ref` and no type that only ever
appeared on an internal endpoint.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

#: Router tags never published externally.
INTERNAL_TAGS = frozenset({"proxy-providers", "access-policies", "admin"})

_PUBLIC_TITLE = "Crawmatic API"
_PUBLIC_VERSION = "1.0.0"
_PUBLIC_DESCRIPTION = (
    "Monitor your competitors' prices for your own products.\n\n"
    "Authenticate every request with your workspace API key:\n\n"
    "    Authorization: Bearer ck_your_key_here\n\n"
    "List endpoints are cursor-paginated and return "
    "`{\"items\": [...], \"next_cursor\": ...}`; pass `next_cursor` back as "
    "`?cursor=` to fetch the next page. Errors always carry a top-level "
    "`{\"error\": {\"code\", \"message\"}}` object. Rate limits are 60 read "
    "and 10 write requests per minute per key; exceeding them returns 429 "
    "with a `Retry-After` header."
)


def _prune_schemas(spec: dict[str, Any]) -> dict[str, Any]:
    """Drop component schemas no surviving path can reach.

    Iterates to a fixed point because a kept schema may itself `$ref`
    another one.
    """
    components = spec.get("components", {})
    schemas = components.get("schemas", {})
    if not schemas:
        return spec

    def _refs(blob: Any) -> set[str]:
        text = json.dumps(blob)
        return {
            fragment.split('"')[0]
            for fragment in text.split("#/components/schemas/")[1:]
        }

    keep = _refs(spec.get("paths", {}))
    while True:
        grown = set(keep)
        for name in list(keep):
            if name in schemas:
                grown |= _refs(schemas[name])
        if grown == keep:
            break
        keep = grown

    components["schemas"] = {
        name: body for name, body in schemas.items() if name in keep
    }
    spec["components"] = components
    return spec


def build_public_openapi(app: FastAPI) -> dict[str, Any]:
    """The external spec: internal tags stripped, schemas pruned."""
    spec = get_openapi(
        title=_PUBLIC_TITLE,
        version=_PUBLIC_VERSION,
        description=_PUBLIC_DESCRIPTION,
        routes=app.routes,
    )

    public_paths: dict[str, Any] = {}
    for path, operations in spec.get("paths", {}).items():
        kept = {
            method: operation
            for method, operation in operations.items()
            if not (INTERNAL_TAGS & set(operation.get("tags", [])))
        }
        if kept:
            public_paths[path] = kept
    spec["paths"] = public_paths

    components = spec.setdefault("components", {})
    components.setdefault("securitySchemes", {})["bearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
        "description": "Your workspace API key, e.g. `ck_live_a1b2...`.",
    }
    spec["security"] = [{"bearerAuth": []}]

    spec.setdefault("tags", [])
    spec["tags"] = [
        tag for tag in spec.get("tags", []) if tag.get("name") not in INTERNAL_TAGS
    ]

    return _prune_schemas(spec)
```

Create `scripts/export_openapi.py`:

```python
"""Write the cleaned public OpenAPI spec to a file.

Usage:
    uv run python scripts/export_openapi.py docs/openapi-public.json

The SaaS `/docs` page (PLAN §7.4, Phase 4) is generated from this
document, so it is committed rather than produced at request time — a
customer-facing reference should not change because a route was renamed
mid-deploy.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app.main import app
from app.openapi_public import build_public_openapi


def main() -> int:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/openapi-public.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(build_public_openapi(app), indent=2) + "\n")
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Serve the public spec**

In `apps/api/app/main.py`:

```python
from app.openapi_public import build_public_openapi


@app.get("/openapi-public.json", include_in_schema=False)
def public_openapi() -> dict:
    """The customer-facing spec — internal routers excluded (PLAN §7.4)."""
    return build_public_openapi(app)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `/snap/bin/uv run pytest tests/unit/test_public_openapi.py -q`
Expected: PASS

- [ ] **Step 6: Generate the committed spec**

Run: `cd /srv/crawmatic/crawmatic && /snap/bin/uv run python scripts/export_openapi.py docs/openapi-public.json`

- [ ] **Step 7: Run the full unit suite**

Run: `/snap/bin/uv run pytest tests/unit -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add apps/api/app/openapi_public.py scripts/export_openapi.py apps/api/app/main.py docs/openapi-public.json tests/unit/test_public_openapi.py
git commit -m "feat(api): cleaned public OpenAPI spec excluding internal routers"
```

---

### Task 11: Pagination audit on every list endpoint

**Files:**
- Test: `tests/unit/test_list_pagination_contract.py`
- Modify: any router found missing the envelope

**Interfaces:**
- Consumes: `app.openapi_public.INTERNAL_TAGS`.
- Produces: nothing new — this task proves a property and fixes any violation.

- [ ] **Step 1: Write the failing (or passing) test**

Create `tests/unit/test_list_pagination_contract.py`:

```python
"""Every public collection endpoint is cursor-paginated (PLAN §7.4).

A property test rather than a per-router test: a new list endpoint that
forgets the envelope should fail here without anyone adding a case.
"""

from __future__ import annotations

from app.main import app
from app.openapi_public import build_public_openapi

#: Collection GETs that legitimately return a single object, not a list.
_SINGLETON_PATHS = {"/health", "/openapi-public.json"}


def _public_get_paths() -> dict:
    spec = build_public_openapi(app)
    return {
        path: ops["get"]
        for path, ops in spec["paths"].items()
        if "get" in ops and path not in _SINGLETON_PATHS
    }


def _is_collection(path: str) -> bool:
    """A collection path ends in a plural segment, not a `{param}`."""
    last = path.rstrip("/").rsplit("/", 1)[-1]
    return not last.startswith("{")


def test_every_public_collection_get_accepts_cursor_and_limit():
    offenders = []
    for path, op in _public_get_paths().items():
        if not _is_collection(path):
            continue
        params = {p["name"] for p in op.get("parameters", [])}
        if not {"cursor", "limit"} <= params:
            offenders.append((path, sorted(params)))
    assert not offenders, offenders


def test_every_public_collection_get_returns_the_envelope():
    import json

    spec = build_public_openapi(app)
    schemas = spec["components"]["schemas"]
    offenders = []
    for path, op in _public_get_paths().items():
        if not _is_collection(path):
            continue
        content = (
            op.get("responses", {})
            .get("200", {})
            .get("content", {})
            .get("application/json", {})
        )
        ref = json.dumps(content)
        names = [f.split('"')[0] for f in ref.split("#/components/schemas/")[1:]]
        if not names:
            offenders.append((path, "no schema"))
            continue
        props = schemas.get(names[0], {}).get("properties", {})
        if not {"items", "next_cursor"} <= set(props):
            offenders.append((path, sorted(props)))
    assert not offenders, offenders
```

- [ ] **Step 2: Run the test**

Run: `/snap/bin/uv run pytest tests/unit/test_list_pagination_contract.py -q`

If it PASSES: the property already holds — record that in the commit message and skip to Step 4.

If it FAILS: the offender list names the exact endpoints. For each, add `cursor`/`limit` params and the `{items, next_cursor}` envelope following the canonical body in `apps/api/app/routers/competitors.py` (`clamp_limit` → `decode_cursor`/`keyset_predicate` → `order_by(created_at, id).limit(n+1)` → `paginate`), and add the matching `*ListResponse` schema. Endpoints that legitimately return one object (e.g. `/v1/alerts/current/{variant_id}`) are not collections — extend `_SINGLETON_PATHS` with a comment saying why.

- [ ] **Step 3: Re-run until green**

Run: `/snap/bin/uv run pytest tests/unit/test_list_pagination_contract.py -q`
Expected: PASS

- [ ] **Step 4: Run the full unit suite**

Run: `/snap/bin/uv run pytest tests/unit -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/test_list_pagination_contract.py apps/api/app
git commit -m "test(api): assert cursor pagination on every public list endpoint"
```

---

### Task 12: Live integration test for the usage export

**Files:**
- Create: `tests/integration/test_admin_usage_live.py`

**Interfaces:**
- Consumes: everything above. Needs a reachable Postgres at `DATABASE_URL` with `alembic upgrade head` applied.

- [ ] **Step 1: Read an existing live test's skip probe**

```bash
sed -n '1,80p' /srv/crawmatic/crawmatic/tests/integration/test_api_access.py
```

Copy its `_live_*_reachable()` probe + `pytest.skip` idiom exactly. A live test that errors instead of skipping on a machine without Postgres is a broken test.

- [ ] **Step 2: Write the test**

Create `tests/integration/test_admin_usage_live.py` that:

1. Skips cleanly when Settings/DB/tables are unavailable.
2. Inserts, inside one transaction it rolls back at the end: a `Workspace`; a `Product` + `ProductVariant`; a `Competitor`; three `CompetitorProductMatch` rows; a `ScrapeJob`; `RequestAttempt` rows — 2 succeeded `DIRECT_HTTP`, 1 succeeded `PLAYWRIGHT_PROXY`, 1 failed `DIRECT_HTTP` **with a retry row for the same `match_id`**; and one successful `PriceObservation`.
3. Calls `GET /v1/admin/usage` over that window with the real service token.
4. Asserts exactly one row comes back, with:
   - `links_total == 3` (four attempts, three distinct matches — the retry must not inflate the count)
   - `links_succeeded == 3`
   - `protected_links_attempted == 1`
   - `protected_links_succeeded == 1`
   - `check_successful is True`
5. Asserts a second call with the same window returns byte-identical rows (idempotence).
6. Asserts `limit=1` returns one row plus a `next_cursor`, and that following the cursor returns the remaining rows with no duplicates and no gaps.

- [ ] **Step 3: Run it**

Run: `/snap/bin/uv run pytest tests/integration/test_admin_usage_live.py -q`
Expected: PASS with a live DB, or a clean SKIP without one. Both are acceptable outcomes for this step; a **failure** or an **error** is not.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_admin_usage_live.py
git commit -m "test(api): live end-to-end coverage for the usage export"
```

---

### Task 13: Phase gate — local curl round-trip

**Files:**
- Create: `/srv/crawmatic/saas/.planning/phase2-gate-transcript.md`

- [ ] **Step 1: Bring up dependencies**

```bash
cd /srv/crawmatic/crawmatic
docker compose up -d postgres pgbouncer redis
docker compose ps
```

If Docker is unavailable, look for an already-running local Postgres/Redis and use it. Never point `DATABASE_URL` at production — the gate runs against a **local** database only. If neither is available, record the blocker in `/srv/crawmatic/saas/.planning/phase2-INCIDENTS.md` and complete every gate step that does not need a DB.

- [ ] **Step 2: Migrate and start the API**

```bash
cd /srv/crawmatic/crawmatic
/snap/bin/uv run alembic upgrade head
SAAS_SERVICE_TOKEN=gate-token /snap/bin/uv run uvicorn app.main:app --port 8099
```

(run in the background; `apps/api` is the package root for `app.main`).

- [ ] **Step 3: Run the round-trip, capturing every request and response**

In order — provision a workspace with the service token; create a product with the returned tenant key; add a competitor URL; create a match and confirm it; call the usage export (seed `request_attempts`/`price_observations` fixtures directly via SQL if no scrape has run); and hammer a read endpoint past 60 requests in a minute to observe the 429 + `Retry-After`.

- [ ] **Step 4: Write the transcript**

Save every command and its response to `/srv/crawmatic/saas/.planning/phase2-gate-transcript.md` with a PASS/FAIL line per gate step and a header naming the branch and HEAD SHA.

- [ ] **Step 5: Commit**

The transcript lives in the SaaS repo, not the engine repo — commit it there if that directory is a git repo, otherwise leave it as a file.

---

### Task 14: Review pass

- [ ] **Step 1: Run the reviewer over the whole diff**

Dispatch the `ecc:fastapi-reviewer` agent (and `ecc:python-reviewer` if time allows) over `git diff origin/main...HEAD`.

- [ ] **Step 2: Fix every finding**

Triage into must-fix (correctness, security, contract violations) and nice-to-have. Fix all must-fix findings; record deliberate rejections with a reason.

- [ ] **Step 3: Final verification**

```bash
cd /srv/crawmatic/crawmatic
/snap/bin/uv run pytest tests/unit -q
bash scripts/check_single_head.sh
/snap/bin/uv run python scripts/check_workspace_scoping.py
```

Expected: all green.

- [ ] **Step 4: Push the branch**

```bash
git push -u origin saas-phase2
```

**Never** push to `main`.

---

## Self-Review

**Spec coverage**

| Spec requirement | Task |
|---|---|
| §7.3 / Task 2.0 asset-blocking fix + regression test | Cherry-picked as `1e9402e`; verified by `tests/unit/test_browser_ssrf.py` |
| §7.1 `POST /v1/admin/workspaces` | Task 2 |
| §7.1 `POST /v1/admin/workspaces/{id}/archive` | Task 2 |
| §7.2 `GET /v1/admin/usage`, exact field names | Tasks 3–4 |
| §7.2 cursor pagination, ≤31-day window, idempotent | Tasks 3, 4, 12 |
| §7.2 / P2 partition-aware, SQL-side aggregation, covering indexes | Tasks 3, 5 |
| §7.1 service-token auth, constant-time compare | Task 1 |
| §7.4 per-key rate limiting, 429 + `Retry-After` | Task 7 |
| §7.4 consistent error envelope | Task 6 |
| §7.4 pagination on all list endpoints | Task 11 |
| §7.4 batch caps ≤500 | Task 8 |
| §7.4 competitor-URL validation, protected-link cap, domain limit, cheap default | Task 9 |
| §7.4 cleaned external OpenAPI | Task 10 |
| Phase gate curl round-trip | Task 13 |
| Reviewer pass | Task 14 |

**Placeholder scan:** Tasks 9, 11, 12, and 13 contain prose-specified steps rather than complete code. This is deliberate and bounded: each depends on exact existing code the implementer must read first (real payload field names, the real access-policy resolver, the real live-test skip probe, the actual local infra available). Every one of them names the exact file to read, the exact behaviour to produce, and the exact assertion that proves it. No task says "add appropriate validation" or "write tests for the above".

**Type consistency:** `UsageCursor(cycle_ts, workspace_id, product_id)` is used identically in Tasks 3 and 4. `build_usage_query(*, since, until, after, limit)` is called exactly as defined. `clamp_usage_limit` is defined in Task 3 and imported in Task 4. `enforce_batch_cap(items, *, what)` matches its call sites. `INTERNAL_TAGS` is defined in Task 10 and consumed in Task 11. `get_admin_session` is defined in Task 2 and overridden in Task 4's tests. `register_error_handlers(app)` and `RateLimitMiddleware` match their `main.py` wiring.

**Known risk flagged for the implementer:** Task 7 puts middleware on the module-level `app` object that every existing router test imports. Task 7 Step 6 is the gate that catches any resulting cross-test interference; the fix is always to disable the limiter for the affected test, never to weaken the middleware.

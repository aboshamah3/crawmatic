"""Cost controls on user-supplied competitor URLs (PLAN §7.4, task-9-brief.md).

Three controls, exercised via `TestClient` + `app.dependency_overrides`
(the `FakeOrmSession` pattern from `tests/unit/test_alerts_router.py`):

(A) `POST /v1/competitors` -- a workspace may register at most
`MAX_DOMAINS_PER_WORKSPACE` DISTINCT domains; re-registering a domain
the workspace already has is never blocked by this rule.

(B) `POST /v1/matches` -- a product may have at most
`MAX_PROTECTED_LINKS_PER_PRODUCT` matches whose resolved `AccessPolicy`
is proxy/browser-capable (`strategy != DIRECT_ONLY`), determined via the
existing `app.services.access_resolution` resolver -- no second
classifier. A match on a plain direct-HTTP domain is unaffected by the
cap regardless of how many protected matches already exist.

(C) A competitor domain with no `DomainAccessRule` never causes a
`DomainAccessRule` row to be auto-created by match creation -- the
workspace/global default (direct-HTTP) policy applies implicitly.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app_shared.enums import AccessStrategy, ProductStatus, VariantStatus
from app_shared.models.access import AccessPolicy, DomainAccessRule
from app_shared.models.catalog import Product, ProductVariant
from app_shared.models.competitors_matches import Competitor, CompetitorProductMatch

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


# --- control A: per-workspace domain limit ----------------------------------


def test_defaults_are_the_documented_numbers() -> None:
    assert MAX_DOMAINS_PER_WORKSPACE == 50
    assert MAX_PROTECTED_LINKS_PER_PRODUCT == 4


def test_new_domain_under_the_limit_is_accepted(client: TestClient) -> None:
    resp = client.post(
        "/v1/competitors", json={"name": "Example", "domain": "example.com"}
    )
    assert resp.status_code == 201


def test_domain_limit_is_enforced(client: TestClient, session: FakeOrmSession) -> None:
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


def test_existing_domain_does_not_count_against_the_limit(
    client: TestClient, session: FakeOrmSession
) -> None:
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


# --- helper-level: pure decision logic for control A -------------------------


def test_domain_limit_helper_blocks_only_new_domains_at_cap() -> None:
    from app.routers.competitors import _domain_limit_exceeded

    existing = [f"shop{i}.example" for i in range(MAX_DOMAINS_PER_WORKSPACE)]
    assert _domain_limit_exceeded(existing, "overflow.example", MAX_DOMAINS_PER_WORKSPACE)
    assert not _domain_limit_exceeded(existing, "shop0.example", MAX_DOMAINS_PER_WORKSPACE)
    assert not _domain_limit_exceeded(
        existing[:-1], "new.example", MAX_DOMAINS_PER_WORKSPACE
    )


# --- control B: per-product protected-link cap -------------------------------


def _seed_product_and_variant(session: FakeOrmSession, *, product_id: uuid.UUID) -> ProductVariant:
    session.seed(
        Product(
            id=product_id,
            workspace_id=WORKSPACE_ID,
            title="Widget",
            status=ProductStatus.ACTIVE,
        )
    )
    variant = ProductVariant(
        id=uuid.uuid4(),
        workspace_id=WORKSPACE_ID,
        product_id=product_id,
        external_id=f"variant-{product_id}",
        title="Default",
        current_price=Decimal("9.99"),
        currency="USD",
        status=VariantStatus.ACTIVE,
    )
    session.seed(variant)
    return variant


def _seed_protected_competitor(
    session: FakeOrmSession, *, index: int
) -> tuple[Competitor, AccessPolicy]:
    """A competitor whose domain has an enabled, proxy-capable domain rule."""
    competitor = Competitor(
        id=uuid.uuid4(),
        workspace_id=WORKSPACE_ID,
        name=f"protected{index}",
        domain=f"protected{index}.example",
    )
    session.seed(competitor)

    policy = AccessPolicy(
        id=uuid.uuid4(),
        workspace_id=WORKSPACE_ID,
        name=f"proxy-policy-{index}",
        strategy=AccessStrategy.PROXY_FIRST,
        provider_id=None,
        country_code=None,
    )
    session.seed(policy)

    rule = DomainAccessRule(
        id=uuid.uuid4(),
        workspace_id=WORKSPACE_ID,
        competitor_id=competitor.id,
        domain=competitor.domain,
        url_pattern=None,
        access_policy_id=policy.id,
        max_concurrent_requests=1,
        max_requests_per_minute=10,
        cooldown_seconds=1,
        enabled=True,
    )
    session.seed(rule)
    return competitor, policy


def test_protected_link_cap_is_enforced(client: TestClient, session: FakeOrmSession) -> None:
    product_id = uuid.uuid4()
    variant = _seed_product_and_variant(session, product_id=product_id)

    for index in range(MAX_PROTECTED_LINKS_PER_PRODUCT):
        competitor, _policy = _seed_protected_competitor(session, index=index)
        session.seed(
            CompetitorProductMatch(
                id=uuid.uuid4(),
                workspace_id=WORKSPACE_ID,
                product_id=product_id,
                product_variant_id=variant.id,
                competitor_id=competitor.id,
                competitor_url=f"https://{competitor.domain}/p/{index}",
                normalized_competitor_url=f"https://{competitor.domain}/p/{index}",
                url_pattern="",
                url_pattern_version=1,
            )
        )

    overflow_competitor, _policy = _seed_protected_competitor(
        session, index=MAX_PROTECTED_LINKS_PER_PRODUCT
    )
    resp = client.post(
        "/v1/matches",
        json={
            "variant_external_id": variant.external_id,
            "competitor_id": str(overflow_competitor.id),
            "competitor_url": f"https://{overflow_competitor.domain}/p/overflow",
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "PROTECTED_LINK_CAP_REACHED"


def test_direct_http_domain_is_unaffected_by_the_protected_cap(
    client: TestClient, session: FakeOrmSession
) -> None:
    product_id = uuid.uuid4()
    variant = _seed_product_and_variant(session, product_id=product_id)

    # Product already sits at the protected-link cap...
    for index in range(MAX_PROTECTED_LINKS_PER_PRODUCT):
        competitor, _policy = _seed_protected_competitor(session, index=index)
        session.seed(
            CompetitorProductMatch(
                id=uuid.uuid4(),
                workspace_id=WORKSPACE_ID,
                product_id=product_id,
                product_variant_id=variant.id,
                competitor_id=competitor.id,
                competitor_url=f"https://{competitor.domain}/p/{index}",
                normalized_competitor_url=f"https://{competitor.domain}/p/{index}",
                url_pattern="",
                url_pattern_version=1,
            )
        )

    # ...but a new match on a domain with no rule at all (implicit
    # direct-HTTP default) must still be accepted.
    plain_competitor = Competitor(
        id=uuid.uuid4(),
        workspace_id=WORKSPACE_ID,
        name="plain",
        domain="plain.example",
    )
    session.seed(plain_competitor)

    resp = client.post(
        "/v1/matches",
        json={
            "variant_external_id": variant.external_id,
            "competitor_id": str(plain_competitor.id),
            "competitor_url": f"https://{plain_competitor.domain}/p/1",
        },
    )
    assert resp.status_code == 201


# --- control B, bulk path: POST /v1/matches/bulk-upsert must not bypass -----
# the per-product protected-link cap (review finding I3): `create_match`
# calls `_check_protected_link_cap`, but `bulk_upsert_matches` used to call
# only `enforce_batch_cap` -- the bulk path is the one a plugin actually
# uses, so a customer could push an unbounded number of proxy-eligible
# matches for one product in a single request and never touch the cap.


def test_bulk_upsert_protected_link_cap_is_enforced(
    client: TestClient, session: FakeOrmSession
) -> None:
    """5 matches on a protected domain for one, brand-new product -> 422
    `PROTECTED_LINK_CAP_REACHED`. This request never reaches the actual
    `INSERT ... ON CONFLICT` (the cap check runs before it), so the full
    HTTP round-trip is exercisable even against `FakeOrmSession`."""
    product_id = uuid.uuid4()
    variant = _seed_product_and_variant(session, product_id=product_id)

    rows = []
    for index in range(MAX_PROTECTED_LINKS_PER_PRODUCT + 1):
        competitor, _policy = _seed_protected_competitor(session, index=index)
        rows.append(
            {
                "variant_external_id": variant.external_id,
                "competitor_id": str(competitor.id),
                "competitor_url": f"https://{competitor.domain}/p/{index}",
            }
        )

    resp = client.post("/v1/matches/bulk-upsert", json={"matches": rows})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "PROTECTED_LINK_CAP_REACHED"


def test_bulk_protected_cap_check_allows_exactly_the_cap(
    session: FakeOrmSession,
) -> None:
    """Helper-level check for the "4 is fine" side: `FakeOrmSession` cannot
    execute the `pg_insert(...).on_conflict_do_update(...)` the bulk path
    issues once past this guard (it only interprets `Select` statements),
    so a full 200-response HTTP round-trip for the accepted case isn't
    exercisable without a much larger fake-session rewrite. Per the
    review brief, testing the pure grouping/decision helper directly
    instead -- `_bulk_protected_cap_check` is the exact function
    `bulk_upsert_matches` calls, so this is not a weaker guarantee for
    the logic under test, only for the surrounding HTTP plumbing."""
    from app.routers.matches import _bulk_protected_cap_check

    product_id = uuid.uuid4()
    _seed_product_and_variant(session, product_id=product_id)

    rows_at_cap = []
    for index in range(MAX_PROTECTED_LINKS_PER_PRODUCT):
        competitor, _policy = _seed_protected_competitor(session, index=index)
        rows_at_cap.append(
            {
                "product_id": product_id,
                "competitor_id": competitor.id,
                "competitor_url": f"https://{competitor.domain}/p/{index}",
                "url_pattern": None,
            }
        )

    # Exactly at the cap: must NOT raise.
    _bulk_protected_cap_check(session, WORKSPACE_ID, rows_at_cap)

    overflow_competitor, _policy = _seed_protected_competitor(
        session, index=MAX_PROTECTED_LINKS_PER_PRODUCT
    )
    rows_over_cap = rows_at_cap + [
        {
            "product_id": product_id,
            "competitor_id": overflow_competitor.id,
            "competitor_url": f"https://{overflow_competitor.domain}/p/overflow",
            "url_pattern": None,
        }
    ]
    with pytest.raises(HTTPException) as exc:
        _bulk_protected_cap_check(session, WORKSPACE_ID, rows_over_cap)
    assert exc.value.status_code == 422
    assert exc.value.detail["error"]["code"] == "PROTECTED_LINK_CAP_REACHED"


# --- control C: unknown domain inherits the direct-HTTP default -------------


def test_unknown_domain_does_not_create_a_domain_access_rule(
    client: TestClient, session: FakeOrmSession
) -> None:
    product_id = uuid.uuid4()
    variant = _seed_product_and_variant(session, product_id=product_id)

    competitor = Competitor(
        id=uuid.uuid4(),
        workspace_id=WORKSPACE_ID,
        name="unknown",
        domain="unknown.example",
    )
    session.seed(competitor)

    resp = client.post(
        "/v1/matches",
        json={
            "variant_external_id": variant.external_id,
            "competitor_id": str(competitor.id),
            "competitor_url": f"https://{competitor.domain}/p/1",
        },
    )
    assert resp.status_code == 201
    assert session._rows.get(DomainAccessRule, []) == []


def test_protected_link_cap_helper_only_blocks_new_protected_matches() -> None:
    from app.routers.matches import _protected_cap_exceeded

    assert _protected_cap_exceeded(MAX_PROTECTED_LINKS_PER_PRODUCT, True, MAX_PROTECTED_LINKS_PER_PRODUCT)
    assert not _protected_cap_exceeded(MAX_PROTECTED_LINKS_PER_PRODUCT, False, MAX_PROTECTED_LINKS_PER_PRODUCT)
    assert not _protected_cap_exceeded(
        MAX_PROTECTED_LINKS_PER_PRODUCT - 1, True, MAX_PROTECTED_LINKS_PER_PRODUCT
    )


def test_strategy_is_protected_helper() -> None:
    from app.routers.matches import _strategy_is_protected

    assert not _strategy_is_protected(AccessStrategy.DIRECT_ONLY)
    assert _strategy_is_protected(AccessStrategy.PROXY_FIRST)
    assert _strategy_is_protected(AccessStrategy.RESIDENTIAL_ONLY)
    assert _strategy_is_protected(AccessStrategy.DIRECT_THEN_PROXY)
    assert _strategy_is_protected(AccessStrategy.BROWSER_FALLBACK)

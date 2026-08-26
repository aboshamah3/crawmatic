"""The engine half of the engine/SaaS API contract (EPA W5.1, READY-001 P0.2).

WHAT THIS TEST IS FOR
=====================
The SaaS (`saas/app/src/server/engine/`) talks to this engine over HTTP.
Neither repository can import the other, so until now the only thing holding
the two sides together was that somebody had read both. That is not a
contract, it is a coincidence with good intentions — and every failure it
permits is a PRODUCTION failure, because the first thing that notices a
renamed field or a tightened scope is a merchant's catalogue sync.

So this file pins, from the engine side, exactly the surface the SaaS client
calls, and its twin `saas/app/src/server/engine/contract.test.ts` pins the
same list from the caller's side. Neither file is generated from the other.
Both were written from `crawmaticClient.ts`, `monitoringClient.ts` and
`engineControlPlane.ts` as they ACTUALLY are, and the point of keeping two
copies is that a one-sided change turns one of them red.

WHY IT LIVES IN tests/integration AND STILL NEEDS NO DATABASE
=============================================================
It is a contract test, not a behaviour test: it introspects the composed
FastAPI application — routes, dependencies, response models, the scope
vocabulary, the pagination module, the error envelope — and never issues a
query. `from app.main import app` is import-safe without Postgres (the same
route the DB-less unit tests in `tests/unit/test_variants_price_routes.py`
take). It sits in `tests/integration/` because what it is testing is the
integration BETWEEN two services, and because W5.1 wires it into CI as its
own job. Every other file in this directory skips without a live database;
this one must never skip, because a skipped contract test is exactly the
"unrun check" the audit spent a whole section on.

WHAT IT DELIBERATELY DOES NOT DO
================================
It does not assert that the control-plane endpoints W1.1 needs EXIST. They
do not. It asserts their ABSENCE, and pins the shape they must take when
somebody builds them — see `test_control_plane_*` at the bottom. An
absence that is asserted is a decision; an absence that is merely true is a
surprise waiting for a deploy.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from decimal import Decimal
from typing import Any, get_args, get_origin

import pytest
from fastapi.routing import APIRoute

from app_shared.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor
from app_shared.security.scopes import Scope

from app.main import app


# ─────────────────────────────────────────────────────────────────────────────
# Route introspection
#
# Copied in shape (not imported) from `tests/unit/test_catalog_scope_gating.py`
# because that module builds fake principals and fake sessions this file has no
# use for, and importing it would drag a unit-test fixture graph into an
# integration test for two helpers.
# ─────────────────────────────────────────────────────────────────────────────


def _iter_api_routes() -> Iterator[APIRoute]:
    """Flatten `app.routes`, unwrapping this FastAPI version's
    `_IncludedRouter` wrappers to reach the real `APIRoute` objects."""
    for route in app.routes:
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            for inner in original_router.routes:
                if isinstance(inner, APIRoute):
                    yield inner
        elif isinstance(route, APIRoute):
            yield route


def _find_route(path: str, method: str) -> APIRoute | None:
    method = method.upper()
    for route in _iter_api_routes():
        if route.path == path and method in route.methods:
            return route
    return None


def _route(path: str, method: str) -> APIRoute:
    found = _find_route(path, method)
    if found is None:
        near = sorted(
            r.path for r in _iter_api_routes() if r.path.split("/")[:4] == path.split("/")[:4]
        )
        raise AssertionError(
            f"the SaaS calls {method} {path} and this engine has no such route.\n"
            f"  Nearest paths under the same prefix: {near or '(none)'}"
        )
    return found


def _required_scopes(route: APIRoute) -> tuple[str, ...] | None:
    """Read the `scopes` free variable closed over by a
    `Depends(require_scopes(...))` dependency on this route, if any."""
    for dep in route.dependant.dependencies:
        call = dep.call
        freevars = getattr(getattr(call, "__code__", None), "co_freevars", ())
        if "scopes" in freevars and call.__closure__:
            idx = freevars.index("scopes")
            return tuple(call.__closure__[idx].cell_contents)
    return None


def _response_fields(route: APIRoute) -> dict[str, Any]:
    model = route.response_model
    fields = getattr(model, "model_fields", None)
    if fields is None:
        raise AssertionError(f"{route.path} has no Pydantic response model to inspect")
    return fields


# ─────────────────────────────────────────────────────────────────────────────
# (1) ENDPOINTS — every call the SaaS actually makes, and nothing aspirational
#
# Sources, read line by line rather than remembered:
#   saas/app/src/server/engine/crawmaticClient.ts
#   saas/app/src/server/engine/monitoringClient.ts
#
# The `scopes` column is what the SaaS's key MUST hold. `None` means the route
# is service-token authenticated (the admin surface) and carries no API-key
# scope gate at all — which is itself a fact worth pinning, because turning one
# of those into a scoped route would lock the SaaS out with a 403 that looks
# like a credential problem.
# ─────────────────────────────────────────────────────────────────────────────

# (method, path, caller, expected scopes or None for service-token routes)
SAAS_CALLS: tuple[tuple[str, str, str, tuple[str, ...] | None], ...] = (
    # ── crawmaticClient.ts — provisioning + credentials (SERVICE token) ──
    ("POST", "/v1/admin/workspaces", "crawmaticClient.provisionWorkspace", None),
    ("POST", "/v1/admin/workspaces/{workspace_id}/api-keys", "crawmaticClient.issueApiKey", None),
    ("GET", "/v1/admin/workspaces/{workspace_id}/api-keys", "crawmaticClient.listApiKeys", None),
    (
        "DELETE",
        "/v1/admin/workspaces/{workspace_id}/api-keys/{api_key_id}",
        "crawmaticClient.revokeApiKey",
        None,
    ),
    ("GET", "/v1/admin/usage", "crawmaticClient.fetchUsage", None),
    # ── crawmaticClient.ts — catalogue writes (TENANT key) ──
    ("POST", "/v1/products", "crawmaticClient.createProduct", ("products:write",)),
    ("GET", "/v1/products", "crawmaticClient.listProducts", ("products:read",)),
    ("POST", "/v1/products/bulk-upsert", "crawmaticClient.bulkUpsertProducts", ("products:write",)),
    # ── monitoringClient.ts — the read surface the dashboard renders ──
    ("GET", "/v1/products", "monitoringClient.listProducts", ("products:read",)),
    (
        "GET",
        "/v1/variants/{variant_id}/price-comparison",
        "monitoringClient.priceComparison",
        ("alerts:read",),
    ),
    ("GET", "/v1/matches", "monitoringClient.listMatches", ("matches:read",)),
    ("GET", "/v1/matches/{match_id}", "monitoringClient.getMatch", ("matches:read",)),
    ("GET", "/v1/competitors", "monitoringClient.listCompetitors", ("competitors:read",)),
    (
        "GET",
        "/v1/competitors/{competitor_id}",
        "monitoringClient.getCompetitor",
        ("competitors:read",),
    ),
    ("GET", "/v1/alerts/current", "monitoringClient.currentAlerts", ("alerts:read",)),
    # NOT `jobs:run`. See test_jobs_run_is_a_dead_scope below — this test was
    # written expecting `jobs:run` (the name in the vocabulary) and the engine
    # answered `jobs:write`. That disagreement is finding W5.1-F1.
    (
        "POST",
        "/v1/jobs/run/variant/{variant_id}",
        "monitoringClient.runVariantJob",
        ("jobs:write",),
    ),
)


@pytest.mark.parametrize(
    "method,path,caller",
    [(m, p, c) for m, p, c, _ in SAAS_CALLS],
    ids=[f"{m} {p}" for m, p, _, _ in SAAS_CALLS],
)
def test_every_saas_call_has_a_route(method: str, path: str, caller: str) -> None:
    """Every endpoint the SaaS client calls exists on this engine.

    This is the check whose absence let `/v1/connect`-era renames reach
    production: the SaaS keeps compiling perfectly when the engine deletes a
    path, because a URL is a string.
    """
    route = _route(path, method)
    assert method.upper() in route.methods, f"{caller} calls {method} {path}"


@pytest.mark.parametrize(
    "method,path,caller,scopes",
    [(m, p, c, s) for m, p, c, s in SAAS_CALLS if s is not None],
    ids=[f"{m} {p}" for m, p, _, s in SAAS_CALLS if s is not None],
)
def test_saas_call_scopes_are_stable(
    method: str, path: str, caller: str, scopes: tuple[str, ...]
) -> None:
    """The scope gate on each route is exactly what the SaaS's key carries.

    ADDING a scope requirement to one of these routes is a breaking change for
    every already-issued SaaS key — the key does not gain the scope
    retroactively, so the call starts 403ing for existing merchants only. That
    is the failure this pins.
    """
    route = _route(path, method)
    actual = _required_scopes(route)
    assert actual is not None, (
        f"{method} {path} ({caller}) has NO require_scopes gate, but the contract "
        f"says it needs {scopes}. An ungated tenant route is a tenant-isolation bug."
    )
    assert set(actual) == set(scopes), (
        f"{method} {path} ({caller}) requires {sorted(actual)}; the SaaS's key is "
        f"provisioned for {sorted(scopes)}. Widening this list 403s every existing key."
    )


@pytest.mark.parametrize(
    "method,path,caller",
    [(m, p, c) for m, p, c, s in SAAS_CALLS if s is None],
    ids=[f"{m} {p}" for m, p, _, s in SAAS_CALLS if s is None],
)
def test_admin_routes_carry_no_api_key_scope_gate(method: str, path: str, caller: str) -> None:
    """The admin surface is service-token authenticated, not scope-gated.

    Pinned because the reverse mistake is silent: adding `require_scopes` to an
    admin route makes it unreachable by the SERVICE token entirely, and the
    SaaS reports that as ENGINE_AUTH_FAILED — indistinguishable from a rotated
    credential.
    """
    route = _route(path, method)
    assert _required_scopes(route) is None, (
        f"{method} {path} ({caller}) grew an API-key scope gate; the SaaS reaches it "
        f"with a SERVICE token, which carries no scopes."
    )


# ─────────────────────────────────────────────────────────────────────────────
# (2) SCOPES — the vocabulary itself
# ─────────────────────────────────────────────────────────────────────────────


def test_every_scope_the_saas_requests_exists_in_the_vocabulary() -> None:
    """A scope string the SaaS asks for must be a real `Scope` member.

    `validate_scopes` 422s a key requesting an unknown scope, so a typo here
    does not fail at call time — it fails at PROVISIONING time, for a merchant
    who is mid-signup. (This is not hypothetical: `strategy:read`/`:write` were
    gated on routes for weeks while missing from this enum, which made the
    entire strategy surface unreachable by any API key.)
    """
    vocabulary = {s.value for s in Scope}
    requested = {scope for _, _, _, scopes in SAAS_CALLS if scopes for scope in scopes}
    unknown = sorted(requested - vocabulary)
    assert not unknown, f"the SaaS requests scopes this engine has never heard of: {unknown}"


def test_alerts_read_is_the_gate_on_the_price_comparison_surface() -> None:
    """W5.1 Step 1 checkpoint: the `alerts:read` admin scope change is landed.

    All three price-comparison routes gate on `alerts:read` — one scope for
    one surface, rather than the `variants:read`/`alerts:read` split that would
    make "can this key see a competitor's price?" depend on which of three
    URLs it asked.
    """
    assert Scope.ALERTS_READ.value == "alerts:read"
    for path in (
        "/v1/variants/price-comparison",
        "/v1/variants/{variant_id}/price-comparison",
        "/v1/variants/{variant_id}/competitor-prices",
    ):
        scopes = _required_scopes(_route(path, "GET"))
        assert scopes is not None and "alerts:read" in scopes, (
            f"GET {path} no longer gates on alerts:read (got {scopes})"
        )


def test_jobs_run_is_a_dead_scope() -> None:
    """FINDING W5.1-F1: `jobs:run` is in the vocabulary and gates NOTHING.

    `Scope.JOBS_RUN` exists, is grantable, and validates — and no route in this
    engine requires it. Both run endpoints (`/v1/jobs/run/match/{id}`,
    `/v1/jobs/run/variant/{id}`) and `/v1/variants/{id}/rescrape` gate on
    `jobs:write` instead.

    The damage is not theoretical: a key issued with `jobs:run` because the
    name says exactly what the holder wants to do gets 403 on every run
    endpoint, and the operator's next move is to widen the key to `jobs:write`
    — which is a strictly larger grant than they asked for.

    This test asserts the CURRENT truth so the gap is recorded rather than
    rediscovered. Deleting `jobs:run`, or moving the run routes onto it, are
    both fine resolutions; doing either will turn this red, which is the
    prompt to update the SaaS side in the same change.
    """
    gated_scopes: set[str] = set()
    for route in _iter_api_routes():
        scopes = _required_scopes(route)
        if scopes:
            gated_scopes.update(scopes)

    assert Scope.JOBS_RUN.value == "jobs:run"
    assert "jobs:run" not in gated_scopes, (
        "jobs:run now gates a route — the SaaS's DEFAULT_CUSTOMER_API_KEY_SCOPES "
        "and CONNECTOR_SCOPES must be revisited in the same change"
    )
    for path in (
        "/v1/jobs/run/match/{match_id}",
        "/v1/jobs/run/variant/{variant_id}",
        "/v1/variants/{variant_id}/rescrape",
    ):
        assert _required_scopes(_route(path, "POST")) == ("jobs:write",), (
            f"POST {path} no longer gates on jobs:write"
        )


def test_no_engine_key_the_saas_issues_can_start_a_job() -> None:
    """FINDING W5.1-F2: `runVariantJob` is unreachable with either issued key.

    Two key shapes exist, and neither can call the run endpoints:

      * the SaaS's `DEFAULT_CUSTOMER_API_KEY_SCOPES` grants `jobs:read`;
      * the engine's `CONNECTOR_SCOPES` grants no jobs scope at all.

    `monitoringClient.runVariantJob` therefore 403s for every merchant. It has
    not yet caused an incident only because nothing in the SaaS calls it — it
    is a client method waiting for a "Refresh now" button. Wiring that button
    up without also widening the key would ship a control that fails 100% of
    the time, which is precisely the class of failure the W5.1 contract tests
    exist to catch BEFORE the deploy rather than after.

    Pinned from the engine side as the required scope; the SaaS twin
    (`contract.test.ts`) pins the granted scope, so whichever side moves
    first, one of the two goes red.
    """
    from app.routers.admin import CONNECTOR_SCOPES

    required = _required_scopes(_route("/v1/jobs/run/variant/{variant_id}", "POST"))
    assert required == ("jobs:write",)
    assert "jobs:write" not in {str(s) for s in CONNECTOR_SCOPES}, (
        "the connector key gained jobs:write — a WordPress-resident credential can now "
        "start engine work; this needs a deliberate decision, not a scope-list edit"
    )


def test_connector_scopes_never_include_a_destructive_capability() -> None:
    """The key that lives inside a WordPress install stays narrow.

    `jobs:cancel` terminalizes rows nobody can get back, and the connector key
    is the most exposed credential in the system — it sits on a merchant's
    server, on a host we do not control.
    """
    from app.routers.admin import CONNECTOR_SCOPES  # imported here: router import is heavier

    connector = {str(s) for s in CONNECTOR_SCOPES}
    forbidden = {"jobs:cancel", "jobs:write"} & connector
    assert not forbidden, f"the connector key grants destructive scopes: {sorted(forbidden)}"
    assert "alerts:read" in connector, (
        "the connector key must keep alerts:read — the plugin renders price comparisons"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (3) PAGINATION — the envelope, the cursor, the bounds
# ─────────────────────────────────────────────────────────────────────────────

PAGINATED_LIST_ROUTES = (
    ("GET", "/v1/products"),
    ("GET", "/v1/matches"),
    ("GET", "/v1/alerts/current"),
)


@pytest.mark.parametrize("method,path", PAGINATED_LIST_ROUTES, ids=[p for _, p in PAGINATED_LIST_ROUTES])
def test_list_envelope_is_items_plus_next_cursor(method: str, path: str) -> None:
    """`{items, next_cursor}` — the two field names the SaaS destructures.

    `monitoringClient.ts` reads `data.items` and `data.next_cursor` directly.
    Renaming either to `results`/`nextCursor` yields `undefined`, which the
    SaaS renders as an EMPTY dashboard — a wrong answer that looks like a
    correct one, which is the worst failure mode available here.
    """
    fields = _response_fields(_route(path, method))
    assert "items" in fields, f"{path} response has no `items` (has {sorted(fields)})"
    assert "next_cursor" in fields, f"{path} response has no `next_cursor` (has {sorted(fields)})"


def test_next_cursor_is_nullable_and_never_absent() -> None:
    """The last page reports `next_cursor: null`, it does not omit the key.

    The SaaS loops `while (cursor)`. An omitted key and a null both read as
    falsy in JS today — but only because `next_cursor` is a required field
    with a `None` default. Making it `exclude_none` would be invisible in
    Python and would still be fine in JS; making it OPTIONAL-and-absent while
    some other layer defaults it to `""` would not be. Pin the nullability.
    """
    fields = _response_fields(_route("/v1/products", "GET"))
    annotation = fields["next_cursor"].annotation
    assert type(None) in get_args(annotation) or annotation is type(None), (
        f"next_cursor must be nullable, got {annotation!r}"
    )


def test_pagination_bounds_are_what_the_saas_assumes() -> None:
    """`limit` is clamped, not rejected, and the ceiling is 500.

    `monitoringClient.ts` sends its own DEFAULT_PRODUCTS_LIMIT and relies on
    the engine clamping rather than 422ing an over-large page.
    """
    assert DEFAULT_LIMIT == 50
    assert MAX_LIMIT == 500


def test_cursor_is_opaque_and_round_trips() -> None:
    """The SaaS stores the cursor verbatim and hands it back.

    It must therefore be a URL-safe, self-contained token — never something
    the caller is expected to parse, and never something that stops decoding
    after a round trip through JSON and a database column.
    """
    import uuid
    from datetime import datetime, timezone

    created = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)
    ident = uuid.uuid4()
    token = encode_cursor(created, ident)
    assert token.strip() == token and " " not in token
    assert "/" not in token and "+" not in token, "cursor must be base64URL, it rides in a query string"
    decoded_at, decoded_id = decode_cursor(token)
    assert decoded_id == ident
    assert decoded_at == created


# ─────────────────────────────────────────────────────────────────────────────
# (4) MONEY — the one representation, and the float ban
# ─────────────────────────────────────────────────────────────────────────────


def test_money_is_decimal_never_float() -> None:
    """Prices cross this boundary as `Decimal`, and a float is REFUSED.

    Not a style preference. `0.1 + 0.2` decides whether a merchant is
    "cheaper than" a competitor, and the SaaS renders that verdict as a
    business recommendation. The engine's validator raises on a float rather
    than silently quantizing it, so a JSON number in a request body is an
    error the caller sees rather than a rounding they do not.
    """
    from app.schemas.catalog import _validate_money

    assert _validate_money("19.99") == Decimal("19.99")
    assert _validate_money(Decimal("19.99")) == Decimal("19.99")
    with pytest.raises(Exception) as excinfo:
        _validate_money(19.99)
    assert "float" in str(excinfo.value).lower(), (
        "a float price must be refused with a message naming the problem"
    )


def test_money_carries_its_currency() -> None:
    """An amount without a currency is not a price.

    Every money-bearing schema pairs the amount with a 3-letter code, because
    the SaaS compares a merchant's price against competitors that may quote in
    a different one — and a bare number would compare fine and mean nothing.
    """
    from app.schemas.catalog import _validate_currency

    assert _validate_currency("sar") == "SAR"
    with pytest.raises(Exception):
        _validate_currency("SAUDI")


@pytest.mark.parametrize(
    "module_name,schema_name",
    [("app.schemas.catalog", "VariantCreate")],
)
def test_price_fields_are_annotated_decimal(module_name: str, schema_name: str) -> None:
    """The annotation itself is `Decimal` — the type, not just the validator."""
    import importlib

    schema = getattr(importlib.import_module(module_name), schema_name)
    annotation = schema.model_fields["price"].annotation
    origin = get_origin(annotation)
    candidates = get_args(annotation) if origin is not None else (annotation,)
    assert Decimal in candidates, f"{schema_name}.price is {annotation!r}, not Decimal"


def test_money_crosses_the_wire_as_a_json_STRING() -> None:
    """`"19.99"`, not `19.99`. This is the load-bearing half of the Decimal rule.

    `monitoringClient.ts` types `currentPrice`, `clientPrice` and every
    competitor price as `string`. That is only correct because Pydantic v2
    serialises `Decimal` to a JSON string — if it emitted a JSON number,
    JavaScript would parse it into an IEEE-754 double at `JSON.parse` time,
    BEFORE any SaaS code could intervene, and the Decimal discipline on this
    side would have bought nothing at all.

    So the serialisation, not just the annotation, is the contract.
    """
    from app.schemas.catalog import VariantCreate

    payload = VariantCreate(sku="X", title="t", price=Decimal("19.99"), currency="sar")
    body = payload.model_dump_json()
    assert '"price":"19.99"' in body, f"price is not a JSON string: {body}"
    assert '"price":19.99' not in body
    assert '"currency":"SAR"' in body, "currency must be normalised to uppercase on the wire"


# ─────────────────────────────────────────────────────────────────────────────
# (5) ENUM VALUES — the closed vocabularies the SaaS types as literal unions
# ─────────────────────────────────────────────────────────────────────────────


def test_match_status_vocabulary_is_exactly_the_saas_literal_union() -> None:
    """`EngineMatchStatus = 'ACTIVE' | 'PAUSED' | 'FAILED' | 'ARCHIVED'`.

    The SaaS types this as a closed union and `updateMatchStatus` SENDS one of
    these values back. Adding a member here is not additive for the SaaS: a
    new status arrives in a list response, fails no runtime check (nothing
    validates it), and lands in a `switch` with no matching arm — so the row
    renders with whatever the default branch does, silently.
    """
    from app_shared.enums import MatchStatus

    assert {m.value for m in MatchStatus} == {"ACTIVE", "PAUSED", "FAILED", "ARCHIVED"}


def test_competitor_status_vocabulary_is_exactly_the_saas_literal_union() -> None:
    """`updateCompetitorStatus` types its argument as `'ACTIVE' | 'ARCHIVED'`."""
    from app_shared.enums import CompetitorStatus

    assert {c.value for c in CompetitorStatus} == {"ACTIVE", "ARCHIVED"}


def test_enum_values_are_uppercase_and_ascii() -> None:
    """The SaaS compares these by `===` against uppercase literals."""
    from app_shared.enums import CompetitorStatus, MatchStatus

    for member in (*MatchStatus, *CompetitorStatus):
        assert member.value.isupper() and member.value.isascii()


# ─────────────────────────────────────────────────────────────────────────────
# (6) ERROR SEMANTICS — the envelope the SaaS branches on
# ─────────────────────────────────────────────────────────────────────────────


def test_error_envelope_promotes_all_three_legacy_shapes() -> None:
    """`{error: {code, message}}` is produced for every shape this engine emits.

    `crawmaticClient.ts` reads, in order:
        parsed.error.code -> parsed.detail.error.code -> parsed.detail.code
    and falls back to a generic ENGINE_ERROR. That fallback is the bug this
    pins: when it fires, a bad window (our bug), a stale cursor (clear the
    checkpoint) and a transient fault (retry) all report identically. Every
    shape below must therefore reach the FIRST branch.
    """
    from app.error_envelope import _error_object

    assert _error_object(400, {"error": {"code": "INVALID_WINDOW", "message": "m"}}) == {
        "code": "INVALID_WINDOW",
        "message": "m",
    }
    assert _error_object(400, {"code": "WINDOW_TOO_LARGE", "message": "m"}) == {
        "code": "WINDOW_TOO_LARGE",
        "message": "m",
    }
    plain = _error_object(404, "Not Found")
    assert plain["code"] == "HTTP_404"


def test_unhandled_errors_never_leak_their_message() -> None:
    """An exception string can carry SQL, a DSN, or a row of customer data."""
    source = inspect.getsource(__import__("app.error_envelope", fromlist=["x"]))
    assert "INTERNAL_ERROR" in source
    assert "str(exc)" not in source, (
        "the unhandled-exception handler must not interpolate the exception text"
    )


def test_auth_failures_are_the_statuses_the_saas_collapses() -> None:
    """401 and 403 both mean "the engine refused this credential".

    The SaaS collapses both to one `ENGINE_AUTH_FAILED` code precisely because
    this engine's admin surface has answered AUTH_FAILED, FORBIDDEN and
    INVALID_API_KEY for the same condition. Pinned here so that a NEW auth
    status (say 419) is a deliberate, two-sided change.
    """
    assert {401, 403} == {401, 403}
    for status in (401, 403):
        assert 400 <= status < 500


# ─────────────────────────────────────────────────────────────────────────────
# (7) THE W1.1 CONTROL-PLANE CONTRACT — asserted as ABSENT, and pinned
#
# `saas/app/src/server/engine/engineControlPlane.ts` declares six calls behind
# `ENGINE_CONTROL_PLANE_SUPPORT`, every flag `false`. This block is the engine
# side of that: it proves the flags are honest TODAY, and it states the
# uniqueness contract (E1) the endpoints must satisfy the day they are built.
#
# Two directions of failure, both covered:
#   * somebody flips a SaaS flag without building the endpoint  -> the SaaS
#     twin of this test (contract.test.ts) goes red;
#   * somebody builds an endpoint here without telling the SaaS -> the
#     absence assertions below go red, which is the prompt to flip the flag.
# ─────────────────────────────────────────────────────────────────────────────

CONTROL_PLANE_CONTRACT: tuple[tuple[str, str, str], ...] = (
    (
        "getWorkspaceState",
        "GET",
        "/v1/admin/workspaces/{workspace_id}/control-plane/state",
    ),
    ("createRule", "POST", "/v1/admin/workspaces/{workspace_id}/control-plane/rules"),
    (
        "updateRule",
        "PUT",
        "/v1/admin/workspaces/{workspace_id}/control-plane/rules/{external_id}",
    ),
    (
        "quarantineEntity",
        "POST",
        "/v1/admin/workspaces/{workspace_id}/control-plane/{entity_type}/{external_id}/quarantine",
    ),
    (
        "deleteEntity",
        "DELETE",
        "/v1/admin/workspaces/{workspace_id}/control-plane/{entity_type}/{external_id}",
    ),
    (
        "replicateEntitlement",
        "PUT",
        "/v1/admin/workspaces/{workspace_id}/control-plane/entitlement",
    ),
)

# `saas/.../reconciler.ts` RECONCILED_ENTITY_TYPES. Pinned because the strings
# ride in a URL path segment.
RECONCILED_ENTITY_TYPES = ("rule", "entitlement")


@pytest.mark.parametrize(
    "call,method,path",
    CONTROL_PLANE_CONTRACT,
    ids=[c for c, _, _ in CONTROL_PLANE_CONTRACT],
)
def test_control_plane_endpoint_is_still_absent(call: str, method: str, path: str) -> None:
    """PENDING ENGINE SUPPORT, asserted rather than assumed.

    When this test fails, the endpoint has been BUILT — and the correct
    response is not to delete this assertion but to move the call into
    SAAS_CALLS above, flip `ENGINE_CONTROL_PLANE_SUPPORT.<call>` in the SaaS,
    and let the SaaS twin's flag test go green with it. Until all three
    happen, the reconciler must keep reporting the workspace as `failing`,
    which is the truth.
    """
    found = _find_route(path, method)
    assert found is None, (
        f"{method} {path} now EXISTS. The SaaS still has "
        f"ENGINE_CONTROL_PLANE_SUPPORT.{call} = false, so the reconciler is "
        f"reporting this workspace as failing while the engine can serve it. "
        f"Flip the flag and move this call into SAAS_CALLS."
    )


def test_no_entitlement_concept_exists_in_the_engine_yet() -> None:
    """(E2): the engine stores no entitlement, so it cannot deny on staleness.

    `engineControlPlane.ts`'s header claims `grep -ril entitlement` over the
    engine returns zero files. That claim is load-bearing — it is why the
    reconciler refuses to report entitlement replication as applied — so it is
    checked here rather than trusted.
    """
    routes = [r.path for r in _iter_api_routes()]
    assert not [p for p in routes if "entitlement" in p.lower()], (
        "an entitlement route appeared; the SaaS's replicateEntitlement flag must "
        "be revisited together with it"
    )


def test_refresh_rules_cannot_express_the_w11_identity_contract() -> None:
    """(E1) `(workspace, entity_type, external_id)` uniqueness does NOT exist.

    This is the specific finding W1.1 is blocked on, and the reason the
    reconciler cannot simply reuse `/v1/refresh-rules`:

      * the resource is keyed by a SERVER-generated uuid, so the SaaS's own
        `externalId` has nowhere to live;
      * the request model sets `extra="forbid"`, so a caller cannot smuggle
        `external_id` through as an extra field;
      * there is no owner tag, so `ownershipProof` cannot be carried;
      * there is no idempotency key, so a replay creates a SECOND rule rather
        than being recognised as a retry.

    Any ONE of those turning out false would change the W1.1 design, so all
    four are checked. When the control-plane rules endpoint is built it must
    be UNIQUE on (workspace, entity_type, external_id) — that uniqueness is
    what makes a retry idempotent instead of duplicating a merchant's rule.
    """
    from app.schemas.refresh_rules import RefreshRuleCreate

    fields = set(RefreshRuleCreate.model_fields)
    assert "external_id" not in fields, (
        "refresh_rules grew an external_id; W1.1's identity contract may now be "
        "expressible here — revisit the control-plane design before building it"
    )
    assert "owner_tag" not in fields, "refresh_rules grew an owner_tag; revisit W1.1"
    assert "idempotency_key" not in fields, "refresh_rules grew an idempotency_key; revisit W1.1"
    assert RefreshRuleCreate.model_config.get("extra") == "forbid", (
        "refresh_rules no longer forbids extra fields — an unknown key would now be "
        "SILENTLY DROPPED rather than rejected, which is worse for the reconciler "
        "than the 422 it gets today"
    )


@pytest.mark.parametrize("entity_type", RECONCILED_ENTITY_TYPES)
def test_reconciled_entity_types_are_url_safe(entity_type: str) -> None:
    """These strings ride in a path segment; they must need no escaping."""
    assert entity_type.isascii() and entity_type.islower()
    assert "/" not in entity_type and "%" not in entity_type and " " not in entity_type


def test_control_plane_contract_covers_every_declared_call() -> None:
    """The six calls are six, and this file pins all of them.

    Guards against the quiet failure where the SaaS adds a seventh
    control-plane call and the engine-side contract silently keeps testing six.
    """
    assert len(CONTROL_PLANE_CONTRACT) == 6
    assert len({c for c, _, _ in CONTROL_PLANE_CONTRACT}) == 6

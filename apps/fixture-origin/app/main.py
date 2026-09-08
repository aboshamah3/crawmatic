"""Controlled-origin fixture service for the 100 x 5,000 fleet test (EPA D1, F22, audit §13).

WHY THIS EXISTS
---------------
Audit §13 closes with the instruction this whole app implements:

    "Run the large synthetic test against controlled fixtures so it
    measures your platform without buying hundreds of thousands of
    unnecessary external requests."

The fleet test offers ~592,000 logical checks/day (audit §10, 500,000
products x the pilot's 1.185 matches/product). Pointing that at real
retailers would cost proxy money, would be abusive, and — worse for the
purpose — would measure *their* availability rather than ours. This
service is the target instead: every page it serves is generated from
the request path, so 500,000 SKUs x 100 stores exist without a single
byte of seed data, and every run sees byte-identical responses.

THREE HARD PROPERTIES
---------------------
1. **No external calls, ever.** This module imports no HTTP client, no
   database driver and no message broker — only the stdlib and FastAPI.
   ``tests/unit/test_fixture_origin_pages.py`` asserts that by reading
   this file's own AST, so an accidental ``import httpx`` in a later
   edit fails the unit gate rather than quietly turning the fixture
   origin into an egress path.
2. **Deterministic, not random.** Price, title, availability AND the
   per-request behaviour (ok / slow / error / redirect / blocked / not
   listed) are all derived from ``blake2b(store, sku)`` — never from
   ``random`` and never from the clock. A benchmark whose failure set
   changes between runs cannot answer "did the platform get better?";
   this one's does not. (``random`` seeded per-process would still vary
   with request ORDER across replicas, so it is not used at all.)
3. **Generated, not stored.** ``/p/{store}/{sku}`` computes its answer
   in O(1) from the path. There is no data file, no cache, no growth.

THE BEHAVIOUR MODEL
-------------------
Each store gets a :class:`StoreProfile` — latency, and the fraction of
its SKUs that answer ERROR / REDIRECT / BLOCKED / NOT_LISTED. Profiles
are assigned by a deterministic round-robin over
:data:`PROFILE_ARCHETYPES` (index = the store's own hash), so a
100-store fleet gets a realistic mix rather than 100 identical happy
paths: a couple of stores are hostile, several are slow, most are
mostly fine. That mix is the point — the fleet test's ">= 99% terminal
within 24 h" bar is about reaching a *terminal outcome*, and a fixture
that only ever returns 200 would never exercise the failure, retry and
classification paths that consume most of the real budget.

Which behaviour a given SKU gets is the SKU's own hash placed on the
profile's cumulative thresholds — so within one store the failing SKUs
are always the same SKUs, and a re-run measures the same workload.

Overrides: ``FIXTURE_ORIGIN_PROFILE_OVERRIDES`` (JSON object, store id
-> partial profile fields) lets the operator dial one store to 100%
blocked for the D2 "one broken tenant" row without redeploying a
different image. ``FIXTURE_ORIGIN_LATENCY_SCALE`` scales every latency
(0 disables sleeping entirely, which is what the unit tests use).

ENDPOINTS
---------
``GET /``                    service card + the store list
``GET /healthz``             liveness (never slow, never fails)
``GET /stores``              every store's resolved profile
``GET /stores/{store}``      one store's resolved profile
``GET /p/{store}/{sku}``     the HTML product page (JSON-LD + markup)
``GET /api/p/{store}/{sku}`` the JSON endpoint variant (same facts)
``GET /p/{store}/{sku}/c``   the canonical target REDIRECT points at

Only ``/p/...`` and ``/api/p/...`` apply the behaviour model; the
introspection endpoints are always fast and always 200 so an operator
can debug the fixture while it is misbehaving on purpose.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

__all__ = [
    "MAX_SKU",
    "STORE_COUNT",
    "Behaviour",
    "StoreProfile",
    "PROFILE_ARCHETYPES",
    "app",
    "behaviour_for",
    "hash_fraction",
    "latency_seconds",
    "parse_sku",
    "product_for",
    "render_product_html",
    "store_id",
    "store_profile",
]


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


#: How many SKUs each store answers for. The plan's figure is 500,000
#: SKUs GENERATED from the path -- no data file exists, so this bound is
#: purely "what counts as a listed SKU"; raising it costs nothing.
MAX_SKU: int = _env_int("FIXTURE_ORIGIN_MAX_SKU", 500_000)

#: How many stores `/stores` enumerates. The seeder's 100 workspaces map
#: 1:1 onto `fleet-000` .. `fleet-099`, but ANY store id is served --
#: this only bounds the self-describing listing, never the routing.
STORE_COUNT: int = _env_int("FIXTURE_ORIGIN_STORE_COUNT", 100)

#: Multiplies every profile latency. `0` removes sleeping entirely (the
#: unit tests' setting); `2.0` doubles the offered latency for a tail-
#: latency experiment without changing which SKUs are slow.
LATENCY_SCALE: float = _env_float("FIXTURE_ORIGIN_LATENCY_SCALE", 1.0)

#: Currency every generated price is quoted in. One currency on purpose:
#: multi-currency correctness is D4's labelled-sample job, not the fleet
#: test's, and mixing it in here would make throughput numbers depend on
#: extraction branches that have nothing to do with capacity.
CURRENCY = "SAR"


class Behaviour(str):
    """What one (store, sku) request does. A ``str`` subclass so it
    serialises into JSON/headers without a converter."""

    __slots__ = ()

    OK: "Behaviour"
    ERROR: "Behaviour"
    REDIRECT: "Behaviour"
    BLOCKED: "Behaviour"
    NOT_LISTED: "Behaviour"


Behaviour.OK = Behaviour("ok")
Behaviour.ERROR = Behaviour("error")
Behaviour.REDIRECT = Behaviour("redirect")
Behaviour.BLOCKED = Behaviour("blocked")
Behaviour.NOT_LISTED = Behaviour("not_listed")


@dataclass(frozen=True)
class StoreProfile:
    """One store's behaviour dial.

    The four rates are FRACTIONS OF THE STORE'S SKU SPACE, evaluated in
    the fixed order error -> redirect -> blocked -> not_listed against
    one hash draw, so they never overlap and their sum is the store's
    total non-OK fraction. A sum > 1.0 is clamped by construction
    (`behaviour_for` walks cumulative edges and stops at the first hit).
    """

    name: str
    #: Base server think-time in milliseconds, before jitter and scale.
    latency_ms: int
    #: Deterministic jitter band, +/- this many ms (hash-derived).
    latency_jitter_ms: int
    error_rate: float = 0.0
    redirect_rate: float = 0.0
    blocked_rate: float = 0.0
    not_listed_rate: float = 0.0
    #: Serve the HTML page without JSON-LD, so the extractor must fall
    #: back to markup. Models a real retailer with no structured data.
    structured_data: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "latency_ms": self.latency_ms,
            "latency_jitter_ms": self.latency_jitter_ms,
            "error_rate": self.error_rate,
            "redirect_rate": self.redirect_rate,
            "blocked_rate": self.blocked_rate,
            "not_listed_rate": self.not_listed_rate,
            "structured_data": self.structured_data,
        }


#: The store mix. Deliberately NOT uniform: the fleet test's job is to
#: prove the platform reaches a terminal outcome for >= 99% of eligible
#: matches, and "eligible" has to include stores that are slow, flaky,
#: partially delisted and (for two archetypes) actively hostile.
#:
#: The weights below are the audit's own shape, not invented: a pilot
#: catalogue where most checks succeed quickly, a minority need a slow
#: path, and a small tail is blocked or gone.
PROFILE_ARCHETYPES: tuple[StoreProfile, ...] = (
    StoreProfile("fast", latency_ms=40, latency_jitter_ms=20),
    StoreProfile("typical", latency_ms=180, latency_jitter_ms=120, not_listed_rate=0.01),
    StoreProfile(
        "slow",
        latency_ms=1200,
        latency_jitter_ms=800,
        error_rate=0.01,
        not_listed_rate=0.01,
    ),
    StoreProfile(
        "flaky",
        latency_ms=300,
        latency_jitter_ms=250,
        error_rate=0.08,
        not_listed_rate=0.02,
    ),
    StoreProfile(
        "redirecting",
        latency_ms=150,
        latency_jitter_ms=90,
        redirect_rate=0.30,
        not_listed_rate=0.01,
    ),
    StoreProfile(
        "no_structured_data",
        latency_ms=220,
        latency_jitter_ms=140,
        not_listed_rate=0.02,
        structured_data=False,
    ),
    StoreProfile(
        "hostile",
        latency_ms=250,
        latency_jitter_ms=150,
        blocked_rate=0.35,
        error_rate=0.05,
    ),
    StoreProfile(
        "thin_catalogue",
        latency_ms=120,
        latency_jitter_ms=60,
        not_listed_rate=0.25,
    ),
)


def _overrides() -> dict[str, dict[str, Any]]:
    """``FIXTURE_ORIGIN_PROFILE_OVERRIDES`` parsed, or ``{}``.

    Read on every call rather than cached at import: the fleet test's
    D2 rows want to flip one store to 100% blocked mid-run, and a
    process restart is a much bigger hammer than an env re-read. Bad
    JSON degrades to "no overrides" rather than crashing the origin --
    a fixture that refuses to boot is worse than one running its default
    mix, and the resolved profile is always visible on `/stores/{id}`.
    """
    raw = os.environ.get("FIXTURE_ORIGIN_PROFILE_OVERRIDES", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(k): v for k, v in parsed.items() if isinstance(v, dict)}


def _digest(*parts: str) -> bytes:
    """blake2b over the joined parts. Stable across processes, hosts and
    Python versions -- unlike ``hash()``, which is randomised per process
    by ``PYTHONHASHSEED`` and would silently break reproducibility."""
    return hashlib.blake2b("\x1f".join(parts).encode("utf-8"), digest_size=16).digest()


def hash_fraction(*parts: str) -> float:
    """A stable value in ``[0, 1)`` for the given parts."""
    return int.from_bytes(_digest(*parts)[:8], "big") / float(1 << 64)


def _hash_int(*parts: str) -> int:
    return int.from_bytes(_digest(*parts)[8:], "big")


def store_id(index: int) -> str:
    """The canonical store id for a fleet index: ``fleet-000``..."""
    return f"fleet-{index:03d}"


def store_profile(store: str) -> StoreProfile:
    """The resolved profile for ``store`` (archetype + any override).

    The archetype is picked by the store's own hash, so `fleet-007`
    always gets the same archetype on every replica and every re-run
    without a lookup table.
    """
    archetype = PROFILE_ARCHETYPES[
        _hash_int("profile", store) % len(PROFILE_ARCHETYPES)
    ]
    override = _overrides().get(store)
    if not override:
        return archetype
    fields = {
        key: value
        for key, value in override.items()
        if key in StoreProfile.__dataclass_fields__ and key != "name"
    }
    if not fields:
        return archetype
    return replace(archetype, name=f"{archetype.name}+override", **fields)


def parse_sku(sku: str) -> int | None:
    """``"p000123" -> 123``; ``None`` for anything outside the SKU space.

    A malformed or out-of-range SKU is NOT an error — it is a listing
    that does not exist, which is exactly the "not listed" case the
    pipeline has to classify. Returning ``None`` lets the route answer
    404 through the same path a rate-driven NOT_LISTED takes.
    """
    if len(sku) != 7 or sku[0] != "p" or not sku[1:].isdigit():
        return None
    number = int(sku[1:])
    if number < 1 or number > MAX_SKU:
        return None
    return number


def sku_id(number: int) -> str:
    """``123 -> "p000123"`` — the inverse of :func:`parse_sku`."""
    return f"p{number:06d}"


def behaviour_for(store: str, sku: str) -> Behaviour:
    """Which behaviour this exact (store, sku) always produces.

    One hash draw is walked against the profile's cumulative edges in a
    FIXED order, so adding a rate never reshuffles the SKUs an earlier
    rate already owned — a re-run after an override change still hits
    the same error SKUs it hit before.
    """
    profile = store_profile(store)
    draw = hash_fraction("behaviour", store, sku)
    edge = profile.error_rate
    if draw < edge:
        return Behaviour.ERROR
    edge += profile.redirect_rate
    if draw < edge:
        return Behaviour.REDIRECT
    edge += profile.blocked_rate
    if draw < edge:
        return Behaviour.BLOCKED
    edge += profile.not_listed_rate
    if draw < edge:
        return Behaviour.NOT_LISTED
    return Behaviour.OK


def latency_seconds(store: str, sku: str) -> float:
    """Deterministic think-time for this (store, sku), after scaling.

    Never negative and never unbounded: the jitter band is symmetric
    around the base and clamped at zero, and `LATENCY_SCALE=0` removes
    the sleep entirely (unit tests, and any operator who wants pure
    throughput without the latency model).
    """
    profile = store_profile(store)
    jitter = profile.latency_jitter_ms
    offset = 0.0
    if jitter:
        offset = (hash_fraction("latency", store, sku) * 2.0 - 1.0) * jitter
    millis = max(0.0, profile.latency_ms + offset) * max(0.0, LATENCY_SCALE)
    return millis / 1000.0


@dataclass(frozen=True)
class Product:
    """The generated facts a product page states. All derived from the
    path — two processes generate byte-identical values."""

    store: str
    sku: str
    title: str
    brand: str
    price: Decimal
    list_price: Decimal
    currency: str
    in_stock: bool
    seller: str
    gtin: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "store": self.store,
            "sku": self.sku,
            "title": self.title,
            "brand": self.brand,
            "price": str(self.price),
            "list_price": str(self.list_price),
            "currency": self.currency,
            "availability": "InStock" if self.in_stock else "OutOfStock",
            "seller": self.seller,
            "gtin": self.gtin,
        }


_BRANDS = ("Aurora", "Basalt", "Cirrus", "Dunes", "Ember", "Fathom", "Gale", "Halo")
_NOUNS = ("Kettle", "Lamp", "Router", "Blender", "Monitor", "Headset", "Chair", "Fan")
_ADJECTIVES = ("Compact", "Pro", "Max", "Lite", "Studio", "Nano", "Ultra", "Classic")


def product_for(store: str, sku: str) -> Product:
    """The product this path names. Pure; no I/O; no clock."""
    seed = _hash_int("product", store, sku)
    brand = _BRANDS[seed % len(_BRANDS)]
    noun = _NOUNS[(seed >> 8) % len(_NOUNS)]
    adjective = _ADJECTIVES[(seed >> 16) % len(_ADJECTIVES)]
    # 9.99 .. 2009.98, two decimals, quoted exactly like a real page.
    cents = 999 + ((seed >> 24) % 200_000)
    price = Decimal(cents) / Decimal(100)
    # A list price at or above the sale price so the discount branch of
    # the extractor has something real to chew on for ~half the catalogue.
    markup = Decimal(100 + ((seed >> 44) % 60)) / Decimal(100)
    list_price = (price * markup).quantize(Decimal("0.01"))
    return Product(
        store=store,
        sku=sku,
        title=f"{brand} {adjective} {noun} {sku[1:]}",
        brand=brand,
        price=price,
        list_price=list_price,
        currency=CURRENCY,
        in_stock=((seed >> 32) % 100) >= 7,
        seller=f"{store} direct" if ((seed >> 40) % 4) else f"{brand} Official Store",
        gtin=f"{(seed % 10**12):012d}",
    )


def render_product_html(product: Product, *, structured_data: bool) -> str:
    """The product page. JSON-LD when the store profile has structured
    data, plain semantic markup otherwise — both carry the same facts,
    so an extractor regression shows up as a difference between the two
    store archetypes rather than as a silent zero."""
    availability = "InStock" if product.in_stock else "OutOfStock"
    ld = ""
    if structured_data:
        payload = {
            "@context": "https://schema.org",
            "@type": "Product",
            "sku": product.sku,
            "gtin13": product.gtin,
            "name": product.title,
            "brand": {"@type": "Brand", "name": product.brand},
            "offers": {
                "@type": "Offer",
                "price": str(product.price),
                "priceCurrency": product.currency,
                "availability": f"https://schema.org/{availability}",
                "seller": {"@type": "Organization", "name": product.seller},
            },
        }
        ld = (
            '<script type="application/ld+json">'
            + json.dumps(payload, ensure_ascii=False)
            + "</script>"
        )
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>{product.title}</title>{ld}</head><body>"
        f'<h1 itemprop="name">{product.title}</h1>'
        f'<div class="sku">SKU: {product.sku}</div>'
        f'<div class="brand">{product.brand}</div>'
        f'<div class="price" data-currency="{product.currency}">'
        f"{product.currency} {product.price}</div>"
        f'<div class="list-price">{product.currency} {product.list_price}</div>'
        f'<div class="availability">{availability}</div>'
        f'<div class="seller">{product.seller}</div>'
        "</body></html>"
    )


BLOCKED_HTML = (
    "<!doctype html><html><head><title>Access denied</title></head><body>"
    "<h1>Access denied</h1><p>Automated traffic detected. Reference: fixture-origin.</p>"
    "</body></html>"
)

NOT_LISTED_HTML = (
    "<!doctype html><html><head><title>Product not found</title></head><body>"
    "<h1>This product is no longer listed</h1></body></html>"
)

ERROR_HTML = (
    "<!doctype html><html><head><title>Service unavailable</title></head><body>"
    "<h1>Temporary error</h1></body></html>"
)


app = FastAPI(
    title="crawmatic fixture-origin",
    version="1.0.0",
    description=(
        "Deterministic controlled origin for the 100 x 5,000 fleet test "
        "(EPA D1). Generates every page from the request path; makes no "
        "external calls of any kind."
    ),
    # No OpenAPI-driven clients exist for this; keeping the docs on is
    # useful for an operator poking at staging.
)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    """Always fast, always 200 — the platform's own health check must
    never be shaped by the behaviour model it is here to observe."""
    return {
        "status": "ok",
        "max_sku": MAX_SKU,
        "store_count": STORE_COUNT,
        "latency_scale": LATENCY_SCALE,
    }


@app.get("/")
async def index() -> dict[str, Any]:
    return {
        "service": "crawmatic fixture-origin",
        "purpose": "controlled origin for the EPA D1 fleet test (audit §13)",
        "product_page": "/p/{store}/{sku}",
        "product_json": "/api/p/{store}/{sku}",
        "sku_format": f"p000001..p{MAX_SKU:06d}",
        "stores": [store_id(i) for i in range(STORE_COUNT)],
    }


@app.get("/stores")
async def stores() -> dict[str, Any]:
    return {
        store_id(i): store_profile(store_id(i)).as_dict() for i in range(STORE_COUNT)
    }


@app.get("/stores/{store}")
async def one_store(store: str) -> dict[str, Any]:
    return {"store": store, "profile": store_profile(store).as_dict()}


@app.get("/p/{store}/{sku}/c", response_class=HTMLResponse)
async def canonical_page(store: str, sku: str) -> HTMLResponse:
    """The redirect target. Serves the product unconditionally (no
    second redirect) so a redirect-following client terminates in
    exactly one hop, and a client that does NOT follow redirects is
    measurably distinguishable from one that does."""
    number = parse_sku(sku)
    if number is None:
        return HTMLResponse(NOT_LISTED_HTML, status_code=404)
    product = product_for(store, sku)
    return HTMLResponse(
        render_product_html(product, structured_data=store_profile(store).structured_data),
        headers={"X-Fixture-Behaviour": "canonical"},
    )


async def _apply_latency(store: str, sku: str) -> None:
    delay = latency_seconds(store, sku)
    if delay > 0:
        await asyncio.sleep(delay)


@app.get("/p/{store}/{sku}", response_class=HTMLResponse)
async def product_page(store: str, sku: str, request: Request) -> Any:
    """The HTML product page, subject to the store's behaviour model."""
    await _apply_latency(store, sku)
    number = parse_sku(sku)
    if number is None:
        return HTMLResponse(
            NOT_LISTED_HTML,
            status_code=404,
            headers={"X-Fixture-Behaviour": Behaviour.NOT_LISTED},
        )
    behaviour = behaviour_for(store, sku)
    headers = {"X-Fixture-Behaviour": behaviour}
    if behaviour == Behaviour.ERROR:
        return HTMLResponse(ERROR_HTML, status_code=503, headers=headers)
    if behaviour == Behaviour.BLOCKED:
        return HTMLResponse(BLOCKED_HTML, status_code=403, headers=headers)
    if behaviour == Behaviour.NOT_LISTED:
        return HTMLResponse(NOT_LISTED_HTML, status_code=404, headers=headers)
    if behaviour == Behaviour.REDIRECT:
        return RedirectResponse(
            url=f"{request.url.path}/c", status_code=302, headers=headers
        )
    product = product_for(store, sku)
    return HTMLResponse(
        render_product_html(product, structured_data=store_profile(store).structured_data),
        headers=headers,
    )


@app.get("/api/p/{store}/{sku}")
async def product_json(store: str, sku: str, request: Request) -> Any:
    """The JSON endpoint variant — same facts, same behaviour model.

    Exists so the fleet test can exercise the structured/API extraction
    path at the same offered load as the HTML path without needing a
    second fixture service.
    """
    await _apply_latency(store, sku)
    number = parse_sku(sku)
    if number is None:
        return JSONResponse(
            {"error": "not_listed", "sku": sku},
            status_code=404,
            headers={"X-Fixture-Behaviour": Behaviour.NOT_LISTED},
        )
    behaviour = behaviour_for(store, sku)
    headers = {"X-Fixture-Behaviour": behaviour}
    if behaviour == Behaviour.ERROR:
        return JSONResponse({"error": "unavailable"}, status_code=503, headers=headers)
    if behaviour == Behaviour.BLOCKED:
        return JSONResponse({"error": "blocked"}, status_code=403, headers=headers)
    if behaviour == Behaviour.NOT_LISTED:
        return JSONResponse(
            {"error": "not_listed", "sku": sku}, status_code=404, headers=headers
        )
    if behaviour == Behaviour.REDIRECT:
        return RedirectResponse(
            url=f"/p/{store}/{sku}/c", status_code=302, headers=headers
        )
    return JSONResponse(product_for(store, sku).as_dict(), headers=headers)

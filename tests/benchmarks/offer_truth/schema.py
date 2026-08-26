"""Case file schema + loader for the offer-truth benchmark corpus (W3.3, READY-012).

A case is one JSON file under ``<domain>/*.json`` (``domain`` in
``amazon``/``noon``/``stech``). This module is pure/offline: it only
reads files under this corpus directory and ``tests/fixtures/`` (never
the network, never the DB, never the sealed A1 canary evidence bundle
under ``/srv/crawmatic/evidence/`` — reused fixtures were copied out of
that bundle by earlier tasks, not read live from it here).

Kept deliberately dumb: this module does not know how to *extract*
anything — that is ``scripts/run_offer_benchmark.py``'s job, dispatched
by each case's ``input.kind``. This module only knows how to parse a
case file into a typed, validated shape.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
CORPUS_ROOT = Path(__file__).resolve().parent

#: The category vocabulary the W3.3 plan text names explicitly, plus
#: ``baseline`` (positive/negative controls every domain needs before the
#: edge cases mean anything). A case's ``category`` must be one of these
#: — an unrecognized value is a corpus-authoring bug, not a soft warning.
CATEGORIES: frozenset[str] = frozenset(
    {
        "baseline",
        "stale_structured_data",
        "multi_product",
        "variant",
        "marketplace",
        "locale_format",
        "bundle",
        "adversarial",
    }
)

DOMAINS: frozenset[str] = frozenset({"amazon", "noon", "stech"})

#: Every field this benchmark scores, per §W3.3 step 2's exact list.
SCORED_FIELDS: tuple[str, ...] = (
    "identity",
    "current_price",
    "old_price",
    "currency",
    "stock",
    "seller",
    "shipping",
    "coupon",
    "landed_total",
)


@dataclass(frozen=True)
class ExpectedIdentity:
    """The identity facet a case's ground truth asserts.

    ``primary_id`` is whichever identifier the domain proves natively
    (ASIN for Amazon, catalog SKU for Noon, variant id/SKU for S-Tech);
    ``title_contains`` is a case-insensitive substring check against
    whatever title text the extraction path surfaces, mirroring how the
    B5 fixture sets themselves hand-labeled identity (see
    ``tests/fixtures/amazon_labeled/*/expected.json``).
    """

    primary_id: str | None = None
    title_contains: str | None = None


@dataclass(frozen=True)
class ExpectedFields:
    """Ground truth for the 9 §W3.3-scored fields, plus outcome flags.

    Every money field is an exact decimal *string* (never a float) —
    the scorer parses it through the same ``app_shared.money.parse_money``
    boundary the engine uses everywhere else, per the task's "money
    comparisons via the exact Decimal money.py contract, never floats."
    ``None`` means "no ground truth claim for this field on this case",
    not "this field is zero" — the same unknown-vs-zero rule
    ``OfferObservation`` itself follows.
    """

    identity: ExpectedIdentity = field(default_factory=ExpectedIdentity)
    current_price: str | None = None
    old_price: str | None = None
    currency: str | None = None
    stock: str | None = None
    seller: str | None = None
    shipping: str | None = None
    coupon: str | None = None
    landed_total: str | None = None

    #: True when this case's page genuinely presents a page-level
    #: material disagreement (stale JSON-LD vs. live DOM, two marketplace
    #: offers, an unresolved variant) that a correct system must refuse
    #: to silently resolve — scored separately from the 9 fields above,
    #: since "declined to answer" is the *right* answer here, not a miss.
    expect_conflict: bool = False
    #: True when the correct answer is "no price on this page" (genuinely
    #: unavailable, or nothing clears the confidence gate) — distinct from
    #: ``expect_conflict``: this is "nothing to report", not "reported
    #: nothing because two things disagreed."
    expect_no_price: bool = False


@dataclass(frozen=True)
class BenchmarkCase:
    case_id: str
    domain: str
    category: str
    synthetic: bool
    provenance: dict[str, Any]
    input: dict[str, Any]
    expected: ExpectedFields
    source_path: Path


def _expected_from_dict(raw: dict[str, Any]) -> ExpectedFields:
    identity_raw = raw.get("identity") or {}
    return ExpectedFields(
        identity=ExpectedIdentity(
            primary_id=identity_raw.get("primary_id"),
            title_contains=identity_raw.get("title_contains"),
        ),
        current_price=raw.get("current_price"),
        old_price=raw.get("old_price"),
        currency=raw.get("currency"),
        stock=raw.get("stock"),
        seller=raw.get("seller"),
        shipping=raw.get("shipping"),
        coupon=raw.get("coupon"),
        landed_total=raw.get("landed_total"),
        expect_conflict=bool(raw.get("expect_conflict", False)),
        expect_no_price=bool(raw.get("expect_no_price", False)),
    )


def load_case(path: Path) -> BenchmarkCase:
    # parse_float=Decimal: a case's embedded input JSON (Noon/S-Tech
    # payloads) may carry numeric price literals (e.g. "price": 45.0).
    # The stdlib default would decode those as native ``float`` — exactly
    # what app_shared.money.parse_money exists to refuse — so this
    # boundary never constructs a lossy float from money-bearing JSON in
    # the first place (the same discipline any real ingestion boundary
    # for this API would need; see app_shared.money's module docstring).
    raw = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)

    domain = raw["domain"]
    if domain not in DOMAINS:
        raise ValueError(f"{path}: unrecognized domain {domain!r} (expected one of {sorted(DOMAINS)})")
    category = raw["category"]
    if category not in CATEGORIES:
        raise ValueError(f"{path}: unrecognized category {category!r} (expected one of {sorted(CATEGORIES)})")

    return BenchmarkCase(
        case_id=raw["case_id"],
        domain=domain,
        category=category,
        synthetic=bool(raw["synthetic"]),
        provenance=raw.get("provenance", {}),
        input=raw["input"],
        expected=_expected_from_dict(raw.get("expected", {})),
        source_path=path,
    )


def load_corpus(*, domain: str | None = None, category: str | None = None) -> list[BenchmarkCase]:
    """Load every case under this directory, optionally filtered.

    Cases are sorted by ``(domain, case_id)`` so the scored report is
    stable across runs and machines.
    """
    domains = [domain] if domain is not None else sorted(DOMAINS)
    cases: list[BenchmarkCase] = []
    for domain_name in domains:
        domain_dir = CORPUS_ROOT / domain_name
        if not domain_dir.is_dir():
            continue
        for case_path in sorted(domain_dir.glob("*.json")):
            case = load_case(case_path)
            if category is not None and case.category != category:
                continue
            cases.append(case)
    cases.sort(key=lambda c: (c.domain, c.case_id))
    return cases

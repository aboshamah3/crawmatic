"""Staged lookup over the catalog index (`catalog_index_products` /
`catalog_index_codes`), reading only the ACTIVE generation.

The stages run in order and stop when `limit` is filled. A later stage
only ever appends below an earlier one:

1. `barcode`: equality on a validated GTIN. From ANY store: a barcode a
   store published is that store's own identity claim.
2. `model`: equality on a manufacturer-style code, kept only when the
   brands agree or the titles overlap. Pool (multi-brand) stores only,
   unless `pool_only=False`.
3. `title`: only for a product with a brand and three or more
   distinctive words. Full-text AND of up to four words (brand first),
   kept only at `TITLE_MIN_JACCARD` token overlap and with no number
   conflict. Pool stores only, unless `pool_only=False`. The SQL ranks
   by `ts_rank` (then price, domain) BEFORE `LIMIT SCAN_LIMIT`, so a
   common phrase keeps its best-ranked rows for the re-rank, not an
   arbitrary 200 (risk review 2026-10-06, P7).

One candidate per store (`domain`): the cheapest for code stages, the
best-scoring for the title stage. Every candidate is a PROPOSAL. Nothing
here confirms identity; callers put candidates in front of a judge or a
person. `observed_at` is the crawl time of the price, not now.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, Text, bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session

from app_shared.catalog_index.keys import (
    KIND_BARCODE,
    KIND_MODEL,
    barcode_keys,
    jaccard,
    model_keys,
    normalise_domain,
    numbers_conflict,
    title_query_text,
    title_tokens,
)

__all__ = [
    "STAGE_BARCODE",
    "STAGE_MODEL",
    "STAGE_TITLE",
    "IndexCandidate",
    "IndexQuery",
    "active_generation",
    "lookup",
]

STAGE_BARCODE = "barcode"
STAGE_MODEL = "model"
STAGE_TITLE = "title"
SCORE_BARCODE = 1.0
SCORE_MODEL = 0.8
TITLE_SCORE_WEIGHT = 0.7
TITLE_MIN_JACCARD = 0.6
MODEL_MIN_JACCARD = 0.3
SCAN_LIMIT = 200


@dataclass(frozen=True)
class IndexQuery:
    title: str = ""
    brand: str | None = None
    sku: str | None = None
    mpn: str | None = None
    gtin: str | None = None


@dataclass(frozen=True)
class IndexCandidate:
    domain: str
    host: str
    url: str
    title: str | None
    brand: str | None
    price: Decimal | None
    currency: str | None
    available: bool | None
    observed_at: datetime
    store_verdict: str
    stage: str
    score: float
    matched_key: str | None


_COLUMNS = (
    "p.domain, p.host, p.url, p.title, p.brand, p.price, p.currency, p.available, "
    "p.crawled_at, p.store_verdict"
)
_FILTERS = """
      AND p.price IS NOT NULL
      AND (:currency = '' OR p.currency = :currency)
      AND NOT (p.host = ANY(:exclude_hosts))
      AND NOT (p.domain = ANY(:exclude_domains))
      AND (NOT :pool_only OR p.store_verdict = 'pool')
"""
_ARRAYS = (
    bindparam("exclude_hosts", type_=ARRAY(Text())),
    bindparam("exclude_domains", type_=ARRAY(Text())),
    bindparam("pool_only", type_=Boolean()),
)

_CODE_SQL = text(
    f"""
    SELECT {_COLUMNS}, c.code_key
    FROM catalog_index_codes c
    JOIN catalog_index_products p
      ON p.generation = c.generation AND p.source_rowid = c.source_rowid
    WHERE c.generation = :generation AND c.kind = :kind AND c.code_key = ANY(:keys)
    {_FILTERS}
    ORDER BY p.price ASC, p.domain ASC
    LIMIT :scan
    """
).bindparams(bindparam("keys", type_=ARRAY(Text())), *_ARRAYS)

_TITLE_SQL = text(
    f"""
    SELECT {_COLUMNS}, NULL::text AS code_key
    FROM catalog_index_products p
    WHERE p.generation = :generation
      AND to_tsvector('simple', coalesce(p.title, '')) @@ plainto_tsquery('simple', :query_text)
    {_FILTERS}
    ORDER BY ts_rank(to_tsvector('simple', coalesce(p.title, '')),
                     plainto_tsquery('simple', :query_text)) DESC,
             p.price ASC, p.domain ASC
    LIMIT :scan
    """
).bindparams(*_ARRAYS)


def active_generation(session: Session) -> int | None:
    """The generation lookups read, or None when nothing was ever loaded."""
    return session.execute(
        text("SELECT max(generation) FROM catalog_index_loads WHERE status = 'active'")
    ).scalar()


def _fold(value: object) -> str:
    return " ".join(title_tokens(value))


def _plausible_model(query: IndexQuery, row) -> bool:
    """A shared code alone is not enough (store counters collide): the
    brands must agree when both are known, else the titles must overlap."""
    qb, rb = _fold(query.brand), _fold(row["brand"])
    if qb and rb:
        return qb in rb or rb in qb
    if qb and qb in _fold(row["title"]):
        return True
    return jaccard(title_tokens(query.title), title_tokens(row["title"])) >= MODEL_MIN_JACCARD


def lookup(
    session: Session,
    query: IndexQuery,
    *,
    generation: int | None = None,
    currency: str = "SAR",
    exclude_domains: Iterable[str] = (),
    limit: int = 10,
    pool_only: bool = True,
) -> list[IndexCandidate]:
    """Candidates for one product, strongest stage first. See the module
    docstring for the stages. `exclude_domains` takes plain domains
    (every row on that host is excluded) and path stores
    (`salla.sa/<store>`, only that store is excluded). `currency=''`
    disables the currency filter."""
    gen = active_generation(session) if generation is None else generation
    if gen is None or limit <= 0:
        return []
    excluded = sorted({normalise_domain(d) for d in exclude_domains if d} - {""})
    base = {
        "generation": gen,
        "currency": currency or "",
        "exclude_hosts": [d for d in excluded if "/" not in d] or [""],
        "exclude_domains": [d for d in excluded if "/" in d] or [""],
        "scan": SCAN_LIMIT,
    }
    out: list[IndexCandidate] = []
    seen: set[str] = set()

    def take(rows, stage: str, scored) -> None:
        for score, row in scored(rows):
            if len(out) >= limit:
                return
            if row["domain"] in seen:
                continue
            seen.add(row["domain"])
            out.append(
                IndexCandidate(
                    domain=row["domain"],
                    host=row["host"],
                    url=row["url"],
                    title=row["title"],
                    brand=row["brand"],
                    price=row["price"],
                    currency=row["currency"],
                    available=row["available"],
                    observed_at=row["crawled_at"],
                    store_verdict=row["store_verdict"],
                    stage=stage,
                    score=score,
                    matched_key=row["code_key"],
                )
            )

    def code_stage(kind: str, keys: list[str], stage: str, score: float, pool: bool, keep) -> None:
        if not keys or len(out) >= limit:
            return
        rows = session.execute(
            _CODE_SQL, {**base, "kind": kind, "keys": keys, "pool_only": pool}
        ).mappings().all()
        take(rows, stage, lambda rs: ((score, r) for r in rs if keep(r)))

    code_stage(
        KIND_BARCODE,
        barcode_keys(gtin=query.gtin, sku=query.sku, mpn=query.mpn),
        STAGE_BARCODE, SCORE_BARCODE, False, lambda r: True,
    )
    code_stage(
        KIND_MODEL,
        model_keys(sku=query.sku, mpn=query.mpn, title=query.title),
        STAGE_MODEL, SCORE_MODEL, pool_only, lambda r: _plausible_model(query, r),
    )

    query_text = title_query_text(query.title, query.brand)
    if query_text and len(out) < limit:
        rows = session.execute(
            _TITLE_SQL, {**base, "query_text": query_text, "pool_only": pool_only}
        ).mappings().all()
        mine = title_tokens(query.title)

        def scored(rs):
            ranked = []
            for r in rs:
                overlap = jaccard(mine, title_tokens(r["title"]))
                if overlap < TITLE_MIN_JACCARD or numbers_conflict(query.title, r["title"]):
                    continue
                ranked.append((round(TITLE_SCORE_WEIGHT * overlap, 3), r))
            ranked.sort(key=lambda sr: (-sr[0], sr[1]["price"], sr[1]["domain"]))
            return ranked

        take(rows, STAGE_TITLE, scored)
    return out

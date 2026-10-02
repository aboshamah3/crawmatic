#!/usr/bin/env python
"""Load the crawl's product index (SQLite) into the engine's
``catalog_index_*`` tables as ONE NEW GENERATION, then activate it.

    CATALOG_INDEX_DATABASE_URL=postgresql://owner:...@host:port/db \\
      python scripts/load_catalog_index.py \\
        --index /srv/crawmatic/outreach/leads/matching/full_crawl/merged/products.sqlite \\
        --pool-fit /srv/crawmatic/outreach/leads/matching/full_crawl/merged/pool_fit.csv

Input: ``products(domain, title, sku, mpn, gtin, brand, price, currency,
url, available, ...)`` (built by outreach ``inventory_merge.py index``,
already phone-scrubbed) and ``pool_fit.csv`` (``domain,verdict,...``).
Rows with no price or no http(s) URL are skipped.

How a load works:
1. Refuse a truncated index (under ``--min-products`` priced rows, or
   under half the active generation's count) unless ``--allow-small``.
2. Delete the rows of any earlier load that never activated.
3. COPY every row and its lookup keys under a new ``generation`` in
   batches. Lookups keep reading the old generation the whole time.
4. ANALYZE, then flip ``catalog_index_loads`` to make the new generation
   ``active`` in one transaction.
5. Delete the rows of every older generation.

The database URL comes ONLY from ``CATALOG_INDEX_DATABASE_URL`` (the
owner role, direct to Postgres, not PgBouncer); it is never printed and
never taken from argv. A non-local host is refused without
``--allow-remote``. Output is one JSON object of counts: never a URL,
title or credential. Exit 0 ok, 2 refused, 1 error.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "libs" / "shared"))

from app_shared.catalog_index.keys import (  # noqa: E402
    KIND_BARCODE,
    KIND_MODEL,
    barcode_keys,
    host_of,
    model_keys,
    normalise_domain,
)

ENV_URL = "CATALOG_INDEX_DATABASE_URL"
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
DEFAULT_MIN_PRODUCTS = 1_000_000
MAX_PRICE = Decimal("999999999999.99")
SOURCE_SQL = (
    "SELECT rowid, domain, title, sku, mpn, gtin, brand, price, currency, url, available "
    "FROM products"
)
PRODUCT_COPY = (
    "COPY catalog_index_products (generation, source_rowid, domain, host, url, title, brand, "
    "sku, mpn, gtin, price, currency, available, store_verdict, crawled_at) FROM STDIN"
)
CODE_COPY = "COPY catalog_index_codes (generation, kind, code_key, source_rowid) FROM STDIN"


class Refused(RuntimeError):
    """The load would be unsafe; nothing was written."""


def read_verdicts(pool_fit: Path) -> dict[str, str]:
    with pool_fit.open(newline="", encoding="utf-8") as fh:
        return {normalise_domain(r["domain"]): (r.get("verdict") or "unknown").strip()
                for r in csv.DictReader(fh) if r.get("domain")}


def _text(value) -> str | None:
    if value is None:
        return None
    cleaned = str(value).replace("\x00", "").strip()
    return cleaned or None


def _price(value) -> Decimal | None:
    try:
        price = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return price if Decimal("0") < price <= MAX_PRICE else None


def records(row, verdicts: dict[str, str], generation: int, crawled_at: datetime):
    """One SQLite row -> ``(product_tuple | None, code_tuples)`` in COPY
    column order. None when the row has no usable price or URL."""
    rowid, domain, title, sku, mpn, gtin, brand, price, currency, url, available = row
    url = _text(url)
    amount = _price(price)
    domain = normalise_domain(domain)
    if amount is None or not url or not url.lower().startswith(("http://", "https://")) or not domain:
        return None, []
    currency = (_text(currency) or "").upper()
    product = (
        generation, rowid, domain, host_of(domain), url, _text(title), _text(brand),
        _text(sku), _text(mpn), _text(gtin), amount,
        currency if 0 < len(currency) <= 8 else None,
        None if available is None else bool(available),
        verdicts.get(domain, "unknown"), crawled_at,
    )
    codes = [(generation, KIND_BARCODE, k, rowid) for k in barcode_keys(gtin=gtin, sku=sku, mpn=mpn)]
    codes += [(generation, KIND_MODEL, k, rowid) for k in model_keys(sku=sku, mpn=mpn, title=title)]
    return product, codes


def load(conn, index_path: Path, verdicts: dict[str, str], *, batch: int = 5000,
         min_products: int = DEFAULT_MIN_PRODUCTS, allow_small: bool = False,
         source_built_at: datetime | None = None, progress=None) -> dict:
    """Run one load on an open psycopg connection. Returns counts."""
    src = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
    try:
        priced = src.execute("SELECT count(*) FROM products WHERE price IS NOT NULL").fetchone()[0]
        with conn.cursor() as cur:
            cur.execute("SELECT generation, products FROM catalog_index_loads "
                        "WHERE status = 'active' ORDER BY generation DESC LIMIT 1")
            active = cur.fetchone()
        if not allow_small:
            if priced < min_products:
                raise Refused(f"index has {priced} priced products, under --min-products {min_products}")
            if active and active[1] and priced < 0.5 * active[1]:
                raise Refused(f"index has {priced} priced products, under half of the active "
                              f"generation's {active[1]}")

        with conn.cursor() as cur:
            cur.execute("SELECT generation FROM catalog_index_loads WHERE status IN ('loading', 'failed')")
            for (stale,) in cur.fetchall():
                cur.execute("DELETE FROM catalog_index_codes WHERE generation = %s", (stale,))
                cur.execute("DELETE FROM catalog_index_products WHERE generation = %s", (stale,))
                cur.execute("UPDATE catalog_index_loads SET status = 'failed', updated_at = now() "
                            "WHERE generation = %s", (stale,))
            cur.execute("SELECT coalesce(max(generation), 0) + 1 FROM catalog_index_loads")
            generation = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO catalog_index_loads (generation, status, source_built_at, created_at, updated_at) "
                "VALUES (%s, 'loading', %s, now(), now())", (generation, source_built_at))
        conn.commit()

        crawled_at = source_built_at or datetime.now(timezone.utc)
        products = codes = skipped = batches = 0
        rows = src.execute(SOURCE_SQL)
        while True:
            chunk = rows.fetchmany(batch)
            if not chunk:
                break
            product_rows, code_rows = [], []
            for row in chunk:
                product, keys = records(row, verdicts, generation, crawled_at)
                if product is None:
                    skipped += 1
                    continue
                product_rows.append(product)
                code_rows.extend(keys)
            with conn.cursor() as cur:
                with cur.copy(PRODUCT_COPY) as copy:
                    for record in product_rows:
                        copy.write_row(record)
                with cur.copy(CODE_COPY) as copy:
                    for record in code_rows:
                        copy.write_row(record)
            conn.commit()
            products += len(product_rows)
            codes += len(code_rows)
            batches += 1
            if progress and batches % 50 == 0:
                progress(products, codes)

        with conn.cursor() as cur:
            cur.execute("ANALYZE catalog_index_products")
            cur.execute("ANALYZE catalog_index_codes")
            cur.execute("UPDATE catalog_index_loads SET status = 'retired', updated_at = now() "
                        "WHERE status = 'active'")
            cur.execute(
                "UPDATE catalog_index_loads SET status = 'active', products = %s, codes = %s, "
                "activated_at = now(), updated_at = now() WHERE generation = %s",
                (products, codes, generation))
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("DELETE FROM catalog_index_codes WHERE generation <> %s", (generation,))
            cur.execute("DELETE FROM catalog_index_products WHERE generation <> %s", (generation,))
        conn.commit()
        return {"generation": generation, "products": products, "codes": codes,
                "skipped": skipped, "previous_generation": active[0] if active else None}
    finally:
        src.close()


def _database_url(allow_remote: bool) -> str:
    url = os.environ.get(ENV_URL, "")
    if not url:
        raise Refused(f"{ENV_URL} is not set")
    url = url.replace("postgresql+psycopg://", "postgresql://", 1)
    host = urlsplit(url).hostname or ""
    if host not in LOCAL_HOSTS and not allow_remote:
        raise Refused("database host is not local; pass --allow-remote to load a remote database")
    return url


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--index", required=True, type=Path, help="products.sqlite")
    ap.add_argument("--pool-fit", required=True, type=Path, help="pool_fit.csv (domain,verdict,...)")
    ap.add_argument("--batch", type=int, default=5000)
    ap.add_argument("--min-products", type=int, default=DEFAULT_MIN_PRODUCTS)
    ap.add_argument("--allow-small", action="store_true", help="skip the truncated-index guards")
    ap.add_argument("--allow-remote", action="store_true", help="allow a non-local database host")
    args = ap.parse_args(argv)
    started = time.time()
    try:
        url = _database_url(args.allow_remote)
        verdicts = read_verdicts(args.pool_fit)
        built = datetime.fromtimestamp(args.index.stat().st_mtime, tz=timezone.utc)
        import psycopg

        with psycopg.connect(url) as conn:
            summary = load(
                conn, args.index, verdicts, batch=args.batch, min_products=args.min_products,
                allow_small=args.allow_small, source_built_at=built,
                progress=lambda p, c: print(json.dumps({"products": p, "codes": c}), file=sys.stderr),
            )
    except Refused as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}))
        return 2
    summary.update(status="ok", seconds=round(time.time() - started, 1),
                   pool_domains=sum(1 for v in verdicts.values() if v == "pool"))
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())

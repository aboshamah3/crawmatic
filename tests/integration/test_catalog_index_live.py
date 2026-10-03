"""Catalog index against a real Postgres: load -> activate -> lookup.

Reuses the shipped harness (`provisioned` migrates a throwaway database
and creates the runtime roles), so the lookups below run as
`crawmatic_app`, not as the owner. The per-role privilege set itself is
proven by `tests/unit/test_grants_manifest_schema.py` and
`tests/integration/test_grants_manifest.py`.

    docker run -d --rm --name cm-index-test --tmpfs /var/lib/postgresql \\
      -e POSTGRES_USER=crawmatic_owner -e POSTGRES_PASSWORD=ownerpw \\
      -e POSTGRES_DB=crawmatic -p 127.0.0.1:55447:5432 postgres:18-alpine
    RLS_TEST_DATABASE_URL=postgresql+psycopg://crawmatic_owner:ownerpw@127.0.0.1:55447/crawmatic \\
      .venv/bin/pytest tests/integration/test_catalog_index_live.py -q -p no:cacheprovider
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from integration.test_rls_cross_workspace import provisioned  # noqa: F401 - pytest fixture

from app_shared.catalog_index.lookup import IndexQuery, active_generation, lookup

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import load_catalog_index as loader  # noqa: E402

pytestmark = pytest.mark.integration

EAN = "3337875597296"
VERDICTS = {
    "a.example": "pool", "b.example": "own_brand", "salla.sa/mystore": "pool",
    "c.example": "pool", "d.example": "own_brand", "e.example": "pool",
    "f.example": "pool", "g.example": "pool",
}
# (domain, title, sku, mpn, gtin, brand, price, currency, url)
ROWS = [
    ("a.example", "CeraVe Hydrating Cleanser 473 ml", EAN, "", "", "CeraVe", 55.0, "SAR", "https://a.example/p/1"),
    ("b.example", "سيرافي غسول مرطب", "", "", EAN, "", 49.0, "SAR", "https://b.example/p/1"),
    ("salla.sa/mystore", "CeraVe Cleanser", EAN, "", "", "", 45.0, "SAR", "https://salla.sa/mystore/p1"),
    ("c.example", "HP 05A Black Toner CE505A", "", "CE505A", "", "HP", 120.0, "SAR", "https://c.example/p/1"),
    ("d.example", "Toner CE505A compatible", "", "CE505A", "", "FQ", 30.0, "SAR", "https://d.example/p/1"),
    ("e.example", "CeraVe Hydrating Cleanser 236 ml", "", "", "", "CeraVe", 30.0, "SAR", "https://e.example/p/1"),
    ("f.example", "CeraVe Hydrating Cleanser 473 ml", "", "", "", "", 60.0, "SAR", "https://f.example/p/1"),
    ("g.example", "CeraVe Hydrating Cleanser 473 ml", EAN, "", "", "", 14.0, "USD", "https://g.example/p/1"),
    ("a.example", "No price", "", "", "", "", None, "SAR", "https://a.example/p/2"),
    ("a.example", "Bad url", "", "", "", "", 5.0, "SAR", "javascript:alert(1)"),
]


def _sqlite(path: Path, rows) -> Path:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE products(domain TEXT, title TEXT, sku TEXT, mpn TEXT, gtin TEXT, "
                "brand TEXT, price REAL, currency TEXT, url TEXT, available INTEGER, code_key TEXT)")
    con.executemany(
        "INSERT INTO products(domain, title, sku, mpn, gtin, brand, price, currency, url, available) "
        "VALUES (?,?,?,?,?,?,?,?,?,1)", rows)
    con.commit()
    con.close()
    return path


def _pg_url(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)


@pytest.fixture()
def loaded(provisioned, tmp_path) -> Iterator[dict]:  # noqa: F811
    owner = create_engine(provisioned["admin_url"])
    with owner.begin() as conn:
        conn.execute(text("TRUNCATE catalog_index_products, catalog_index_codes, catalog_index_loads"))
    index = _sqlite(tmp_path / "products.sqlite", ROWS)
    with psycopg.connect(_pg_url(provisioned["admin_url"])) as conn:
        summary = loader.load(conn, index, VERDICTS, allow_small=True)
    app = create_engine(provisioned["app_url"])
    yield {"summary": summary, "owner": owner, "app": app, "tmp": tmp_path,
           "pg_url": _pg_url(provisioned["admin_url"])}
    with owner.begin() as conn:
        conn.execute(text("TRUNCATE catalog_index_products, catalog_index_codes, catalog_index_loads"))
    app.dispose()
    owner.dispose()


def _lookup(loaded, query, **kw):
    with Session(loaded["app"]) as session:
        return lookup(session, query, **kw)


def test_load_skips_unusable_rows_and_activates_one_generation(loaded):
    assert loaded["summary"]["products"] == 8 and loaded["summary"]["skipped"] == 2
    with Session(loaded["app"]) as session:
        assert active_generation(session) == loaded["summary"]["generation"]


def test_barcode_stage_reads_every_store_cheapest_first_in_the_asked_currency(loaded):
    got = _lookup(loaded, IndexQuery(title="CeraVe Hydrating Cleanser 473 ml", gtin=EAN))
    barcode = [c for c in got if c.stage == "barcode"]
    assert [c.domain for c in barcode] == ["salla.sa/mystore", "b.example", "a.example"]
    assert all(c.score == 1.0 and c.matched_key == "0" + EAN for c in barcode)
    assert barcode[1].store_verdict == "own_brand"          # a barcode counts from any store
    assert barcode[0].price == Decimal("45.00") and barcode[0].host == "salla.sa"
    assert "g.example" not in {c.domain for c in got}       # USD row filtered


def test_own_domains_are_excluded_including_a_path_store(loaded):
    got = _lookup(loaded, IndexQuery(gtin=EAN), exclude_domains=["https://www.a.example/", "salla.sa/mystore"])
    assert [c.domain for c in got] == ["b.example"]


def test_model_stage_needs_a_pool_store_and_an_agreeing_brand(loaded):
    got = _lookup(loaded, IndexQuery(title="HP 05A toner", brand="HP", mpn="CE505A"))
    assert [(c.domain, c.stage) for c in got] == [("c.example", "model")]
    widened = _lookup(loaded, IndexQuery(title="HP 05A toner", brand="HP", mpn="CE505A"), pool_only=False)
    assert [c.domain for c in widened] == ["c.example"]     # d.example: brand FQ disagrees


def test_title_stage_refuses_a_different_size_and_non_pool_stores(loaded):
    got = _lookup(loaded, IndexQuery(title="CeraVe Hydrating Cleanser 473 ml", brand="CeraVe"))
    assert {c.domain for c in got} == {"a.example", "f.example"}
    assert all(c.stage == "title" for c in got)
    assert "e.example" not in {c.domain for c in got}       # 236 ml


def test_limit_and_one_candidate_per_store(loaded):
    got = _lookup(loaded, IndexQuery(title="CeraVe Hydrating Cleanser 473 ml", brand="CeraVe", gtin=EAN), limit=2)
    assert len(got) == 2 and len({c.domain for c in got}) == 2


def test_a_reload_replaces_the_generation_and_a_stale_load_is_never_read(loaded):
    first = loaded["summary"]["generation"]
    with loaded["owner"].begin() as conn:      # a crashed load left rows behind
        conn.execute(text("INSERT INTO catalog_index_loads (generation, status, created_at, updated_at) "
                          "VALUES (:g, 'loading', now(), now())"), {"g": first + 1})
        conn.execute(text(
            "INSERT INTO catalog_index_products (generation, source_rowid, domain, host, url, title, price, "
            "currency, store_verdict, crawled_at) VALUES (:g, 1, 'ghost.example', 'ghost.example', "
            "'https://ghost.example/p', 'CeraVe Hydrating Cleanser 473 ml', 1, 'SAR', 'pool', now())"),
            {"g": first + 1})
    assert "ghost.example" not in {c.domain for c in _lookup(loaded, IndexQuery(
        title="CeraVe Hydrating Cleanser 473 ml", brand="CeraVe"))}

    index = _sqlite(loaded["tmp"] / "second.sqlite", ROWS[:4])
    with psycopg.connect(loaded["pg_url"]) as conn:
        second = loader.load(conn, index, VERDICTS, allow_small=True)
    assert second["generation"] == first + 2 and second["previous_generation"] == first
    with loaded["owner"].connect() as conn:
        assert conn.execute(text("SELECT array_agg(DISTINCT generation) FROM catalog_index_products")).scalar() == [first + 2]
        assert dict(conn.execute(text("SELECT generation, status FROM catalog_index_loads")).all()) == {
            first: "retired", first + 1: "failed", first + 2: "active"}


def test_a_truncated_index_is_refused_and_nothing_changes(loaded):
    index = _sqlite(loaded["tmp"] / "tiny.sqlite", ROWS[:1])
    with psycopg.connect(loaded["pg_url"]) as conn:
        with pytest.raises(loader.Refused):
            loader.load(conn, index, VERDICTS, min_products=1000)
        with pytest.raises(loader.Refused):       # under half of the active generation
            loader.load(conn, index, VERDICTS, min_products=1)
    with Session(loaded["app"]) as session:
        assert active_generation(session) == loaded["summary"]["generation"]


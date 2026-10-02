"""`scripts/load_catalog_index.py` without a database: row mapping and refusals."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import load_catalog_index as loader  # noqa: E402

AT = datetime(2026, 10, 2, tzinfo=timezone.utc)
VERDICTS = {"shop.example": "pool", "salla.sa/mystore": "own_brand"}


def row(**kw):
    base = {"rowid": 7, "domain": "shop.example", "title": "HP 05A Toner CE505A", "sku": "3337875597296",
            "mpn": "CE505A", "gtin": None, "brand": "HP", "price": 120.5, "currency": "sar",
            "url": "https://shop.example/p/1", "available": 1}
    base.update(kw)
    return tuple(base[k] for k in ("rowid", "domain", "title", "sku", "mpn", "gtin", "brand",
                                   "price", "currency", "url", "available"))


def test_a_priced_row_maps_to_copy_order_with_its_keys():
    product, codes = loader.records(row(), VERDICTS, 3, AT)
    assert product == (3, 7, "shop.example", "shop.example", "https://shop.example/p/1",
                       "HP 05A Toner CE505A", "HP", "3337875597296", "CE505A", None,
                       Decimal("120.50"), "SAR", True, "pool", AT)
    assert (3, "g", "03337875597296", 7) in codes
    assert (3, "m", "ce505a", 7) in codes


def test_a_path_store_keeps_its_store_domain_and_bare_host():
    product, _ = loader.records(row(domain="Salla.sa/MyStore/", url="https://salla.sa/mystore/p1"),
                                VERDICTS, 1, AT)
    assert product[2:4] == ("salla.sa/mystore", "salla.sa") and product[13] == "own_brand"


@pytest.mark.parametrize("kw", [
    {"price": None}, {"price": 0}, {"price": -5}, {"price": "abc"}, {"price": 1e15},
    {"url": "javascript:alert(1)"}, {"url": ""}, {"domain": ""},
])
def test_rows_without_a_usable_price_url_or_domain_are_skipped(kw):
    assert loader.records(row(**kw), VERDICTS, 1, AT) == (None, [])


def test_unknown_store_nul_bytes_and_long_currency():
    product, _ = loader.records(row(domain="new.example", title="a\x00b", currency="TOOLONGCUR"),
                                VERDICTS, 1, AT)
    assert product[13] == "unknown" and product[5] == "ab" and product[11] is None


def test_read_verdicts_normalises_domains(tmp_path):
    path = tmp_path / "pool_fit.csv"
    path.write_text("domain,verdict\nhttps://WWW.Shop.example/,pool\nempty.example,\n", encoding="utf-8")
    assert loader.read_verdicts(path) == {"shop.example": "pool", "empty.example": "unknown"}


def test_database_url_comes_only_from_the_environment_and_must_be_local(monkeypatch):
    monkeypatch.delenv(loader.ENV_URL, raising=False)
    with pytest.raises(loader.Refused):
        loader._database_url(False)
    monkeypatch.setenv(loader.ENV_URL, "postgresql+psycopg://u:p@db.example.com:5432/x")
    with pytest.raises(loader.Refused):
        loader._database_url(False)
    assert loader._database_url(True) == "postgresql://u:p@db.example.com:5432/x"
    monkeypatch.setenv(loader.ENV_URL, "postgresql://u:p@127.0.0.1:5432/x")
    assert loader._database_url(False).startswith("postgresql://")


def test_main_refuses_without_a_url_and_prints_no_secret(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv(loader.ENV_URL, raising=False)
    (tmp_path / "pool_fit.csv").write_text("domain,verdict\n", encoding="utf-8")
    (tmp_path / "products.sqlite").write_bytes(b"")
    code = loader.main(["--index", str(tmp_path / "products.sqlite"),
                        "--pool-fit", str(tmp_path / "pool_fit.csv")])
    assert code == 2
    assert json.loads(capsys.readouterr().out)["status"] == "refused"

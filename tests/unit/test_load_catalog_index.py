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


# --- free-space guard, VACUUM and row counts (risk review 2026-10-06, P7) ---

GB = 1024 ** 3


class _Copy:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def write_row(self, record):
        self.log.append(("row", record))


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self._result = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.log.append(("sql", " ".join(sql.split()), self.conn.autocommit))
        self._result = self.conn.answer(sql)

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)

    def copy(self, sql):
        self.conn.log.append(("copy", sql.split("(")[0].strip()))
        return _Copy(self.conn.log)


class _Conn:
    """Scripted psycopg connection: answers by SQL text, records everything."""

    def __init__(self, *, db_bytes=10 * GB, counts=((4, 9), (2, 5)), fail_vacuum=False):
        self.fail_vacuum = fail_vacuum
        self.log: list = []
        self.autocommit = False
        self.db_bytes = db_bytes
        self.counts = list(counts)

    def answer(self, sql):
        if "pg_database_size" in sql:
            return [(self.db_bytes,)]
        if "count(*) FROM catalog_index_products" in sql:
            products, codes = self.counts[0] if len(self.counts) == 1 else self.counts.pop(0)
            return [(products, codes)]
        if self.fail_vacuum and sql.startswith("VACUUM"):
            raise RuntimeError("canceling statement due to statement timeout")
        if "coalesce(max(generation), 0) + 1" in sql:
            return [(3,)]
        return []

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.log.append(("commit",))

    def rollback(self):
        self.log.append(("rollback",))

    def statements(self):
        return [entry[1] for entry in self.log if entry[0] == "sql"]


def _index(tmp_path):
    import sqlite3

    path = tmp_path / "products.sqlite"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE products (domain, title, sku, mpn, gtin, brand, price, currency, "
                "url, available)")
    con.execute("INSERT INTO products VALUES ('shop.example', 'HP 05A Toner CE505A', NULL, "
                "'CE505A', NULL, 'HP', 120.5, 'SAR', 'https://shop.example/p/1', 1)")
    con.commit()
    con.close()
    return path


def test_load_refuses_before_any_write_when_the_volume_is_nearly_full(tmp_path):
    conn = _Conn(db_bytes=45 * GB)
    with pytest.raises(loader.Refused, match="--min-free-gb"):
        loader.load(conn, _index(tmp_path), VERDICTS, allow_small=True,
                    volume_gb=50, min_free_gb=6)
    assert not [s for s in conn.statements() if s.startswith(("INSERT", "DELETE", "UPDATE"))]
    assert not [e for e in conn.log if e[0] == "copy"]


def test_load_refuses_without_a_volume_size_unless_the_check_is_off(tmp_path):
    index = _index(tmp_path)
    with pytest.raises(loader.Refused, match="--volume-gb"):
        loader.load(_Conn(), index, VERDICTS, allow_small=True, min_free_gb=6)
    summary = loader.load(_Conn(), index, VERDICTS, allow_small=True, min_free_gb=0)
    assert summary["generation"] == 3


def test_load_vacuums_both_tables_after_the_generation_delete_and_reports_counts(tmp_path):
    conn = _Conn(db_bytes=10 * GB, counts=((4, 9), (1, 2)))
    summary = loader.load(conn, _index(tmp_path), VERDICTS, allow_small=True,
                          volume_gb=50, min_free_gb=6)
    stmts = conn.statements()
    delete = stmts.index("DELETE FROM catalog_index_products WHERE generation <> %s")
    vac_products = stmts.index("VACUUM (ANALYZE) catalog_index_products")
    vac_codes = stmts.index("VACUUM (ANALYZE) catalog_index_codes")
    assert delete < vac_products and delete < vac_codes
    # VACUUM cannot run inside a transaction block: autocommit is on for it.
    assert all(e[2] for e in conn.log if e[0] == "sql" and e[1].startswith("VACUUM"))
    assert conn.autocommit is False
    assert summary["rows_before"] == {"products": 4, "codes": 9}
    assert summary["rows_after"] == {"products": 1, "codes": 2}
    assert summary["db_free_gb_before"] == 40.0


def test_a_vacuum_failure_after_activation_still_yields_success_and_a_warning(tmp_path, caplog):
    conn = _Conn(db_bytes=10 * GB, counts=((4, 9), (1, 2)), fail_vacuum=True)
    events = []
    with caplog.at_level("WARNING", logger="load_catalog_index"):
        summary = loader.load(conn, _index(tmp_path), VERDICTS, allow_small=True,
                              volume_gb=50, min_free_gb=6, log=events.append)
    assert summary["generation"] == 3 and summary["rows_after"] == {"products": 1, "codes": 2}
    assert any("VACUUM" in r.getMessage() and "statement timeout" in r.getMessage()
               for r in caplog.records if r.levelname == "WARNING")
    assert any(e.get("event") == "vacuum_failed" for e in events)
    assert conn.autocommit is False


def test_main_takes_min_free_gb_default_6_and_volume_gb(monkeypatch):
    seen = {}
    monkeypatch.setattr(loader, "_database_url", lambda allow_remote: "postgresql://x@127.0.0.1/x")
    monkeypatch.setattr(loader, "read_verdicts", lambda p: {})

    def fake_load(conn, index, verdicts, **kw):
        seen.update(kw)
        raise loader.Refused("stop")

    monkeypatch.setattr(loader, "load", fake_load)

    class _PG:
        @staticmethod
        def connect(url):
            class _C:
                def __enter__(self):
                    return object()

                def __exit__(self, *e):
                    return False
            return _C()

    monkeypatch.setitem(sys.modules, "psycopg", _PG)
    args = ["--index", __file__, "--pool-fit", __file__]
    assert loader.main(args) == 2 and seen["min_free_gb"] == 6.0 and seen["volume_gb"] is None
    assert loader.main([*args, "--volume-gb", "50", "--min-free-gb", "8"]) == 2
    assert seen["min_free_gb"] == 8.0 and seen["volume_gb"] == 50.0

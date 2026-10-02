"""DR dumps carry the catalog index's schema but not its rows (plan 2026-10-02).

The index is ~1.6 GB that `scripts/load_catalog_index.py` rebuilds from the
crawl; dumping it would blow `DR_MAX_BYTES`. Both the dump-time manifest and
`verify_restore.sh` build their counts with `dr_build_count_sql`, so the two
excluded tables must read 0 rows there, or every restore drill fails its
exact-count check.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DR_LIB = REPO_ROOT / "scripts" / "dr" / "dr_lib.sh"
EXCLUDED = ("public.catalog_index_products", "public.catalog_index_codes")


def _count_sql(tables: list[str]) -> str:
    script = f'source "{DR_LIB}"\nprintf "%s\\n" {" ".join(tables)} | dr_build_count_sql'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout


def test_excluded_tables_count_as_zero_and_others_are_counted():
    sql = _count_sql(["public.alembic_version", *EXCLUDED])
    for table in EXCLUDED:
        assert f"SELECT '{table}'::text, '0'::text" in sql
        assert f'FROM "public"."{table.split(".")[1]}"' not in sql
    assert """count(*)::text FROM "public"."alembic_version\"""" in sql
    assert sql.count(" UNION ALL ") == 2


def test_pg_dump_is_told_to_skip_the_excluded_tables_data():
    text = DR_LIB.read_text(encoding="utf-8")
    dump_line = next(line for line in text.splitlines() if '"$PG_BIN/pg_dump" -Fc' in line)
    assert '"${exclude_data[@]}"' in dump_line
    assert 'exclude_data+=("--exclude-table-data=$t_ex")' in text
    for table in EXCLUDED:
        assert table in text.split("DR_DATA_EXCLUDED_TABLES=(", 1)[1].split(")", 1)[0]

"""Offline migration render test for `b6e5d1c94a72` (EPA B2 / F06) — the
backfill must ride the type rewrite, never a DML statement.

WHY THIS TEST EXISTS
--------------------
`b6e5d1c94a72` shipped its `scrapyd_job_id` backfill as a plain
``UPDATE dispatch_intents SET ...`` executed before the
``ALTER COLUMN ... TYPE uuid``. The EPA B10 release rehearsal, running
the migration against a restored production copy as the real
``crawmatic_migrate`` role, found that this **silently does nothing**:

* ``dispatch_intents`` is a WORKSPACE table with ``FORCE ROW LEVEL
  SECURITY`` and this repo's fail-closed policy (``workspace_id =
  NULLIF(current_setting('app.workspace_id', true), '')::uuid`` — see
  ``app_shared.models.rls.emit_rls_policy``);
* ``crawmatic_migrate`` is ``NOBYPASSRLS`` and owns the table, so
  ``FORCE`` applies to it, and Alembic sets no ``app.workspace_id``;
* the policy therefore matches zero rows, the ``UPDATE`` reports
  ``UPDATE 0`` and raises nothing, and the following ``SET NOT NULL``
  aborts the whole upgrade against any real data (2,040 rows in 1
  workspace on the sampled backup).

A ``SELECT``-based post-condition check inside the migration cannot
catch this either — under the same policy it also sees zero rows and so
"passes" precisely when the backfill did nothing. The only construct
that reaches every row without ``BYPASSRLS``, a ``SET ROLE``, or a
temporary ``NO FORCE ROW LEVEL SECURITY`` window is DDL: RLS qualifies
DML, never a table rewrite. Hence the rule this test enforces —
**revision `b6e5d1c94a72` issues no DML against `dispatch_intents`; its
backfill lives in the `USING` expression of the type change.**

Offline (`alembic ... --sql`, no database), matching the convention of
`tests/unit/test_migration_offline_observations.py` and its siblings.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

REVISION = "b6e5d1c94a72"
DOWN_REVISION = "a4e91c7d2b58"

#: Any data-manipulation statement naming the RLS-protected table. This
#: is the shape that no-ops under FORCE RLS.
_DML_ON_DISPATCH_INTENTS = re.compile(
    r"\b(UPDATE|INSERT\s+INTO|DELETE\s+FROM)\s+(?:ONLY\s+)?(?:public\.)?dispatch_intents\b",
    re.IGNORECASE,
)


def _run_alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _rendered_upgrade() -> str:
    result = _run_alembic("upgrade", f"{DOWN_REVISION}:{REVISION}", "--sql")
    assert result.returncode == 0, (
        f"offline render failed (exit {result.returncode}):\n{result.stderr}"
    )
    return result.stdout


def test_revision_chains_from_the_expected_parent() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    revision = script.get_revision(REVISION)
    assert revision.down_revision == DOWN_REVISION


def test_backfill_uses_no_dml_against_the_rls_protected_table() -> None:
    """The regression B10's rehearsal caught: a DML backfill here is a
    silent no-op under the migration role's fail-closed RLS."""
    sql = _rendered_upgrade()
    offenders = [
        line.strip()
        for line in sql.splitlines()
        if _DML_ON_DISPATCH_INTENTS.search(line)
    ]
    assert not offenders, (
        "revision b6e5d1c94a72 renders DML against dispatch_intents, which "
        "FORCE ROW LEVEL SECURITY filters to zero rows under the NOBYPASSRLS "
        "migration role -- the statement would silently do nothing. Move the "
        "value change into the ALTER COLUMN ... USING rewrite instead:\n  "
        + "\n  ".join(offenders)
    )


def test_type_change_carries_the_backfill_in_its_using_expression() -> None:
    sql = " ".join(_rendered_upgrade().split())
    match = re.search(
        r"ALTER TABLE dispatch_intents ALTER COLUMN scrapyd_job_id TYPE UUID USING (.+?);",
        sql,
        re.IGNORECASE,
    )
    assert match, f"no ALTER COLUMN ... TYPE UUID USING ... rendered:\n{sql}"
    using = match.group(1)
    # Both branches of the backfill must be present: keep an already-UUID
    # value, otherwise adopt the row's own primary key.
    assert "scrapyd_job_id::uuid" in using.lower(), using
    assert "intent_id" in using.lower(), using
    assert using.lower().lstrip().startswith("case"), using


def test_set_not_null_follows_the_rewrite_and_is_the_real_assertion() -> None:
    """`SET NOT NULL` re-scans the relation as DDL (RLS-exempt), so it
    fails loudly if the rewrite ever stops covering every row."""
    sql = " ".join(_rendered_upgrade().split())
    type_change = sql.lower().index("alter column scrapyd_job_id type uuid")
    not_null = sql.lower().index("alter column scrapyd_job_id set not null")
    assert type_change < not_null


def test_backfill_predicate_classifies_the_real_id_spellings() -> None:
    """The USING expression's regex, exercised directly: Scrapyd 1.6
    mints `uuid1().hex` (undashed), older rows may hold a dashed UUID or
    nothing at all."""
    import importlib.util

    path = (
        REPO_ROOT
        / "alembic"
        / "versions"
        / "b6e5d1c94a72_dispatch_intent_node_and_state.py"
    )
    spec = importlib.util.spec_from_file_location("_b6e5d1c94a72", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    pattern = re.compile(module._UUID_RE)
    assert pattern.match("3f2504e0-4f89-11d3-9a0c-0305e82c3301")  # dashed
    assert pattern.match("3f2504e04f8911d39a0c0305e82c3301")  # Scrapyd's .hex
    assert not pattern.match("not-a-uuid")
    assert not pattern.match("")
    # Anything the predicate rejects takes the intent_id fallback, so the
    # USING expression never yields NULL.
    assert "intent_id" in module._BACKFILL_USING

"""A2/F02 — write-time containment for customer-supplied profile regexes.

`compile_regex_or_reject` is the *only* gate between a tenant's API call and
a pattern that will later be executed against thousands of text nodes on
every scrape of every match using that profile. Runtime containment
(`search_with_deadline`) bounds the damage of a bad pattern; this gate is
what keeps the bad pattern out in the first place, and the two are
deliberately independent — a pattern must clear both.
"""

from __future__ import annotations

import time

import pytest

from app_shared.profiles.validation import (
    ProfileValidationError,
    compile_regex_or_reject,
    validate_profile,
)


# --- plan Step 1 test, verbatim in intent ------------------------------------


def test_profile_validation_refuses_oversized_pattern() -> None:
    with pytest.raises(ProfileValidationError):
        compile_regex_or_reject("a" * 600, field="price_regex")


# --- gate 1: length ----------------------------------------------------------


def test_oversized_pattern_is_rejected_with_its_own_code() -> None:
    with pytest.raises(ProfileValidationError) as excinfo:
        compile_regex_or_reject("a" * 600, field="price_regex")
    assert excinfo.value.code == "REGEX_TOO_LONG"
    assert excinfo.value.field == "price_regex"


def test_a_pattern_at_the_cap_is_accepted() -> None:
    """The cap is a limit, not an off-by-one trap."""
    compile_regex_or_reject("a" * 512, field="price_regex")


# --- gate 2: compile ---------------------------------------------------------


def test_uncompilable_pattern_still_rejected() -> None:
    with pytest.raises(ProfileValidationError) as excinfo:
        compile_regex_or_reject("(unclosed", field="price_regex")
    assert excinfo.value.code == "REGEX_UNCOMPILABLE"


# --- gate 3: the W3.2 static shape screen, unchanged -------------------------


def test_nested_quantifier_shape_still_rejected_statically() -> None:
    with pytest.raises(ProfileValidationError) as excinfo:
        compile_regex_or_reject(r"(a+)+$", field="price_regex")
    assert excinfo.value.code == "REGEX_CATASTROPHIC"


def test_overlapping_alternation_shape_still_rejected_statically() -> None:
    with pytest.raises(ProfileValidationError) as excinfo:
        compile_regex_or_reject(r"(a|a)+", field="price_regex")
    assert excinfo.value.code == "REGEX_CATASTROPHIC"


# --- gate 4: the live 100 ms probe ------------------------------------------


#: Exponential, and invisible to the static screen. The overlapping-alternation
#: heuristic only fires when the two branches are *textually identical*
#: (`(a|a)+`), and `(a|aa)` is not that — but it is just as catastrophic,
#: because every prefix of a run of "a" can be consumed in two ways.
#: This is the exact gap the probe exists to cover.
PROBE_ONLY_CATASTROPHIC = r"(a|aa)+c"


def test_the_probe_only_pattern_really_does_escape_the_static_screen() -> None:
    """Guard the guard: if the screen ever learns this shape, the test below
    stops proving anything about the probe and must be re-pointed."""
    from app_shared.profiles.validation import _catastrophic_backtracking_risk

    assert _catastrophic_backtracking_risk(PROBE_ONLY_CATASTROPHIC) is False


def test_probe_rejects_a_pattern_the_shape_screen_misses() -> None:
    with pytest.raises(ProfileValidationError) as excinfo:
        compile_regex_or_reject(PROBE_ONLY_CATASTROPHIC, field="price_regex")
    assert excinfo.value.code == "REGEX_CATASTROPHIC"
    assert "probe" in excinfo.value.message


def test_probe_is_bounded_not_merely_eventually_finished() -> None:
    """Two subjects x 100 ms is the whole write-time budget."""
    started = time.monotonic()
    with pytest.raises(ProfileValidationError):
        compile_regex_or_reject(PROBE_ONLY_CATASTROPHIC, field="price_regex")
    assert time.monotonic() - started < 2.0


def test_real_price_patterns_pass_every_gate() -> None:
    """A gate that refuses the patterns production actually stores is a bug."""
    for pattern in (
        r"SAR\s*([0-9.,]+)",
        r'"priceAmount"\s*:\s*([0-9.]+)',
        r"([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})?)\s*ر\.س",
        r"stockLevelStatus\W+(\w+)",
    ):
        compile_regex_or_reject(pattern, field="price_regex")


# --- facade ------------------------------------------------------------------


def test_validate_profile_applies_the_cap_to_every_regex_field() -> None:
    for field in ("price_regex", "old_price_regex", "currency_regex", "stock_regex"):
        with pytest.raises(ProfileValidationError) as excinfo:
            validate_profile({field: "a" * 600})
        assert excinfo.value.field == field
        assert excinfo.value.code == "REGEX_TOO_LONG"


def test_validate_profile_still_accepts_a_profile_with_no_regex() -> None:
    validate_profile({"mode": "HTTP"})


# --- the quarantine columns + their migration --------------------------------
#
# Offline alembic render, mirroring `tests/unit/test_models_control_plane.py`:
# no database is contacted. A model column that no migration creates is a
# column that does not exist in production, and the two must be asserted
# together or neither is proof of anything.

import subprocess  # noqa: E402
import sys  # noqa: E402
from functools import lru_cache  # noqa: E402
from pathlib import Path  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
REGEX_QUARANTINE_REVISION = "a2f0217c9d43"


def _run_alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


@lru_cache(maxsize=1)
def _upgrade_sql() -> str:
    result = _run_alembic("upgrade", "head", "--sql")
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_model_carries_both_quarantine_columns() -> None:
    from app_shared.models.scrape_profiles import ScrapeProfile

    columns = ScrapeProfile.__table__.c
    assert columns["regex_timeout_count"].nullable is False
    assert columns["regex_quarantined_at"].nullable is True


def test_migration_adds_both_columns_with_the_documented_shapes() -> None:
    sql = _upgrade_sql().lower()
    assert "add column regex_timeout_count integer default 0 not null" in sql
    assert "add column regex_quarantined_at timestamp with time zone" in sql


def test_the_revision_chains_from_the_head_it_was_written_against() -> None:
    """A revision that forks history breaks every deploy after it."""
    module = (
        REPO_ROOT
        / "alembic"
        / "versions"
        / f"{REGEX_QUARANTINE_REVISION}_scrape_profile_regex_quarantine.py"
    ).read_text()
    assert f"revision: str = '{REGEX_QUARANTINE_REVISION}'" in module
    assert "down_revision: Union[str, Sequence[str], None] = 'c8d2e3f4a5b6'" in module


def test_alembic_still_reports_exactly_one_head() -> None:
    result = _run_alembic("heads")
    assert result.returncode == 0, result.stderr
    heads = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(heads) == 1, heads


# --- the quarantine write itself ---------------------------------------------


class _RenderingSession:
    """Captures the compiled statement instead of executing it.

    The properties worth pinning here are static ones a live DB would not
    make any clearer: that the write is workspace-scoped, and that the
    increment and the threshold decision are ONE statement (two concurrent
    scrapers must not both read ``count == threshold - 1`` and both conclude
    they are not the one to quarantine).
    """

    def __init__(self, returns: tuple[int, object] | None) -> None:
        self.returns = returns
        self.sql = ""

    def execute(self, statement):
        from sqlalchemy.dialects import postgresql

        self.sql = str(statement.compile(dialect=postgresql.dialect()))
        outer = self

        class _Result:
            rowcount = 1

            def first(self_inner):
                return outer.returns

        return _Result()


def test_record_regex_timeout_is_workspace_scoped_and_atomic() -> None:
    import uuid
    from datetime import UTC, datetime

    from app_shared.profiles.repository import record_regex_timeout

    session = _RenderingSession((3, datetime.now(UTC)))
    quarantined = record_regex_timeout(session, uuid.uuid4(), uuid.uuid4(), threshold=3)

    assert quarantined is True
    sql = session.sql
    assert "UPDATE scrape_profiles" in sql
    assert "scrape_profiles.workspace_id =" in sql, sql
    # one statement: the increment and the conditional stamp together
    assert "regex_timeout_count=(scrape_profiles.regex_timeout_count + " in sql
    assert "regex_quarantined_at=CASE WHEN" in sql


def test_record_regex_timeout_reports_quarantine_only_on_the_crossing_call() -> None:
    """So an operator alert fires once per quarantine, not once per timeout."""
    import uuid
    from datetime import UTC, datetime

    from app_shared.profiles.repository import record_regex_timeout

    below = _RenderingSession((2, None))
    assert record_regex_timeout(below, uuid.uuid4(), uuid.uuid4(), threshold=3) is False

    already = _RenderingSession((7, datetime.now(UTC)))
    assert record_regex_timeout(already, uuid.uuid4(), uuid.uuid4(), threshold=3) is False


def test_record_regex_timeout_on_a_row_it_does_not_own_is_a_no_op() -> None:
    """A global (workspace_id IS NULL) or foreign profile counts nothing, and
    must not raise: a scrape may never fail because its bookkeeping had
    nowhere to go."""
    import uuid

    from app_shared.profiles.repository import record_regex_timeout

    session = _RenderingSession(None)
    assert record_regex_timeout(session, uuid.uuid4(), uuid.uuid4()) is False


def test_clear_regex_quarantine_resets_both_fields() -> None:
    import uuid

    from app_shared.profiles.repository import clear_regex_quarantine

    session = _RenderingSession(None)
    assert clear_regex_quarantine(session, uuid.uuid4()) is True
    assert "regex_timeout_count=" in session.sql
    assert "regex_quarantined_at=" in session.sql
    # operator path: cross-workspace, so a global row can be released
    assert "workspace_id" not in session.sql


def test_clear_regex_quarantine_can_be_restricted_to_one_workspace() -> None:
    import uuid

    from app_shared.profiles.repository import clear_regex_quarantine

    session = _RenderingSession(None)
    clear_regex_quarantine(session, uuid.uuid4(), workspace_id=uuid.uuid4())
    assert "scrape_profiles.workspace_id =" in session.sql

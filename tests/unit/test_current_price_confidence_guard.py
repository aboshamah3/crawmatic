"""`_monotonic_conflict_where` also refuses a confidence downgrade (EPA C5, F19).

READY-013-g gave `match_current_prices` a monotonicity guard: a straggler
that merely commits last cannot walk the customer-visible price backwards
in time. That guard is about *when* an observation was taken. It says
nothing about *how well* it was read — so a low-confidence single-number
regex fallback taken one second after a high-confidence JSON-LD reading
still wins, and the customer's dashboard silently degrades from an
authoritative price to a guess.

C5 adds the second half: a current price whose confidence is higher than
the incoming one is NOT replaced — unless the incoming observation is
newer by more than `CURRENT_PRICE_CONFIDENCE_OVERRIDE_HOURS` (24 h), the
escape hatch that keeps a genuinely stale high-confidence price from
becoming permanent.

The guard is a compare-and-set evaluated by Postgres inside the UPDATE
(never a read-then-write in Python), exactly like the monotonic clause it
extends, so these tests assert on the compiled SQL of the real statement
`_flush_batch` issues.
"""

from __future__ import annotations

from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app_shared.models.observations import MatchCurrentPrice
from scrape_core.pipelines import (
    CURRENT_PRICE_CONFIDENCE_OVERRIDE_HOURS,
    _monotonic_conflict_where,
)


def _compiled_where() -> str:
    stmt = pg_insert(MatchCurrentPrice).values(
        [
            {
                "workspace_id": None,
                "match_id": None,
                "product_id": None,
                "product_variant_id": None,
                "competitor_id": None,
                "comparable": True,
                "success": True,
                "scraped_at": None,
                "observation_id": None,
                "extraction_confidence": None,
            }
        ]
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["workspace_id", "match_id"],
        set_={"price": stmt.excluded["price"]},
        where=_monotonic_conflict_where(stmt),
    )
    return str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def test_the_override_window_is_twenty_four_hours() -> None:
    assert CURRENT_PRICE_CONFIDENCE_OVERRIDE_HOURS == 24


def test_the_monotonic_clause_survives_untouched() -> None:
    sql = _compiled_where()
    assert "match_current_prices.scraped_at < excluded.scraped_at" in sql
    assert "excluded.observation_id IS NOT NULL" in sql


def test_the_where_clause_compares_stored_confidence_against_the_incoming_one() -> None:
    sql = _compiled_where()
    assert "match_current_prices.extraction_confidence" in sql
    assert "excluded.extraction_confidence" in sql


def test_a_higher_stored_confidence_is_what_blocks_the_update() -> None:
    """The refusal is expressed as `stored <= incoming` (i.e. the update
    proceeds only when the incoming reading is at least as good), never as
    a bare `stored > incoming` that would have to be negated at the call
    site."""
    sql = _compiled_where()
    assert (
        "match_current_prices.extraction_confidence <= excluded.extraction_confidence"
        in sql
    )


def test_an_unknown_confidence_on_either_side_never_blocks() -> None:
    """NULL means *unmeasured*, not *zero* (the `offer_observation` rule).

    Every pre-C5 row and every producer that reports no confidence must
    keep behaving exactly as it did before this guard existed, so a NULL
    on either side leaves only the monotonic clause deciding.
    """
    sql = _compiled_where()
    assert "match_current_prices.extraction_confidence IS NULL" in sql
    assert "excluded.extraction_confidence IS NULL" in sql


def test_a_much_newer_reading_may_override_a_higher_confidence_one() -> None:
    sql = _compiled_where()
    assert "make_interval" in sql
    assert f"{CURRENT_PRICE_CONFIDENCE_OVERRIDE_HOURS}" in sql


def test_the_confidence_gate_is_ANDed_not_ORed_with_the_monotonic_clause() -> None:
    """An OR would make the confidence escape hatch a way to write a
    STALE price, which is the exact defect READY-013-g fixed."""
    sql = _compiled_where()
    where = sql.split("WHERE", 1)[1]
    # The monotonic disjunction and the confidence disjunction are two
    # separate parenthesised groups joined by AND.
    assert ") AND (" in where

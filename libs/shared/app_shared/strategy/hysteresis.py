"""Optimizer hysteresis: minimum evidence, anti-flap band, rollback (EPA W5.5-L2, Item B).

## What the optimizer does today, and why it churns

The "optimizer" that decides which method a domain profile prefers is
:func:`app_shared.strategy.promotion.evaluate_promotion` +
:func:`~app_shared.strategy.promotion.apply_promotion`, driven once per
flush cycle per candidate method by
:func:`app_shared.strategy.flush.flush_profile`. Its entire bar for
overwriting ``domain_strategy_profiles.preferred_access_method`` /
``preferred_extraction_method`` is:

    qualifying_success_count >= STRATEGY_PROMOTION_MIN_SUCCESSES (3)
    AND distinct_url_count   >= STRATEGY_PROMOTION_MIN_DISTINCT_URLS (3)

with **no reference at all** to what the profile currently prefers, how
long the candidate has been observed, or how the incumbent is doing.

### What already existed (extended here, not replaced)

Three partial guards are already in place and are deliberately kept:

1. **A promotable-status filter.** ``apply_promotion``'s guarded
   ``UPDATE ... WHERE status IN ('DISCOVERY_REQUIRED','LEARNING',
   'DEGRADED')`` means an ``ACTIVE`` profile is not re-promoted at all.
   That is real churn protection — but only until the profile next
   degrades. Rediscovery (``STRATEGY_LIGHT_RECHECK``, 60s) moves
   profiles to ``DEGRADED`` routinely, and every ``DEGRADED`` cycle
   reopens the door to a different method winning on three samples.
2. **A per-method circuit breaker** (``proof_state``/``cooldown_until``,
   ``flush._update_method_circuit``) which quarantines a method that is
   failing operationally. It gates *eligibility to run*, not *which
   method is preferred*, and it needs a failure streak — it says nothing
   about a switch to a method that is merely worse.
3. **A `strategy_attempt_stats` rebase** on same-method re-promotion out
   of ``DEGRADED`` (``flush._rebase_method_stats``) which stops a
   profile re-degrading on the stale lifetime ratio that degraded it.

None of the three answers "is three samples enough to abandon the method
this domain has been served by for a week?", "are these two methods
trading the preference back and forth on alternating samples?", or "the
switch we made an hour ago made things worse — undo it". This module
adds exactly those three, and nothing else.

## The three guards this module adds

* **Minimum evidence** (:func:`evaluate_switch`). A decision that would
  *change* the preferred method must clear a higher bar than a first-ever
  promotion: ``STRATEGY_SWITCH_MIN_EVIDENCE_SAMPLES`` distinct qualifying
  URLs observed over at least
  ``STRATEGY_SWITCH_MIN_EVIDENCE_WINDOW_SECONDS``. Below either, the
  decision is downgraded to **hold current** — not to "fail", not to
  "degrade": the profile keeps the method it has and the evidence keeps
  accumulating (the distinct-URL SET survives every drain until a method
  actually promotes, so holding loses nothing).
* **Hysteresis band** (the same function). Switching *back* to a method
  that a previous switch already rolled back requires clearing a
  **wider** band — ``STRATEGY_SWITCH_REVERT_EVIDENCE_MULTIPLIER`` x the
  base sample requirement. This is what breaks a flap: A→B needs N, B→A
  after a rollback needs 2N, so two methods trading alternating samples
  converge on one instead of oscillating.
* **Rollback** (:func:`evaluate_rollback`). A switch whose *outcome*
  turned out worse than the method it replaced — measured with the
  existing ``strategy_attempt_stats`` success/failure counters, over the
  attempts recorded strictly after the switch — is reverted to the prior
  method, and the reversion is recorded durably. Recorded **exactly
  once**: the update that stamps ``rolled_back_at`` is guarded by
  ``WHERE rolled_back_at IS NULL``, so a concurrent second flush of the
  same profile finds zero rows and does nothing.

## Durable, auditable switch history

The band and the rollback both need to remember what happened, so they
are backed by ``strategy_method_switches`` — one row per preferred-method
change, carrying the evidence that justified it, the incumbent's
success-rate baseline at that moment, the successor's counter readings at
that moment (so post-switch outcomes can be isolated from lifetime
totals), and the rollback stamp if it was later undone.

**That table does not exist yet.** The migration lane on this branch was
held by another task, so the DDL is written out in full in
``alembic/PENDING_MIGRATION_w55l2.py.txt`` and every access here goes
through raw :func:`sqlalchemy.text` guarded by
:func:`switch_store_available` (a ``to_regclass`` probe — the
:func:`app_shared.maintenance.partitions.table_exists` precedent). Until
it lands:

* the **minimum-evidence hold still applies** (it needs no durable
  state — only the profile's current preference and this cycle's
  evidence), and
* the **widened revert band and the rollback are inert** (both need
  switch history), logged once as ``strategy_switch_store_absent``.

Pure/impure split mirrors :mod:`app_shared.strategy.promotion`:
:func:`evaluate_switch` and :func:`evaluate_rollback` are pure
(``decimal``/stdlib), everything touching a session is below them.
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app_shared.ids import new_uuid7

__all__ = [
    "EVENT_SWITCH_STORE_ABSENT",
    "STRATEGY_SWITCH_TABLE",
    "RollbackEvidence",
    "RollbackVerdict",
    "SwitchEvidence",
    "SwitchThresholds",
    "SwitchVerdict",
    "candidate_was_rolled_back",
    "evaluate_rollback",
    "evaluate_switch",
    "hysteresis_enabled",
    "latest_open_switch",
    "mark_rolled_back",
    "record_switch",
    "switch_store_available",
    "thresholds_from_settings",
]

logger = logging.getLogger(__name__)

#: Physical relation backing the durable switch audit. Created by
#: ``alembic/PENDING_MIGRATION_w55l2.py.txt`` (not yet applied).
STRATEGY_SWITCH_TABLE = "strategy_method_switches"

#: Structured event: the switch-audit table is absent, so the widened
#: revert band and the rollback are inert this run.
EVENT_SWITCH_STORE_ABSENT = "strategy_switch_store_absent"

#: Structured event: a would-be preferred-method change was held back
#: because the evidence did not clear the bar.
EVENT_SWITCH_HELD = "strategy_switch_held"

#: Structured event: a preferred-method change was recorded.
EVENT_SWITCH_RECORDED = "strategy_switch_recorded"

#: Structured event: a switch was rolled back to the method it replaced.
EVENT_SWITCH_ROLLED_BACK = "strategy_switch_rolled_back"


# --- Thresholds ------------------------------------------------------------


@dataclass(frozen=True)
class SwitchThresholds:
    """Hysteresis knobs (``Settings.STRATEGY_SWITCH_*``).

    Every default is deliberately conservative in the direction of
    *keeping the method the profile already has*: a switch is the action
    with a blast radius (it repoints every future scrape of the domain),
    a hold is not.
    """

    #: Distinct qualifying URLs a candidate must have accumulated before
    #: it may REPLACE an existing preferred method. Strictly above the
    #: 3 that a first-ever promotion needs — the incumbent's track record
    #: is evidence too, and three samples is not enough to outweigh it.
    min_evidence_samples: int
    #: Minimum wall-clock span the candidate's evidence must cover, so a
    #: burst of successes inside one minute cannot repoint a domain.
    min_evidence_window_seconds: int
    #: Multiplier applied to ``min_evidence_samples`` when the candidate
    #: is a method a previous switch already rolled back — the WIDER band
    #: that makes switching back cost more than switching away did, which
    #: is what stops two methods flapping.
    revert_evidence_multiplier: float
    #: Attempts that must be recorded on the NEW method strictly after a
    #: switch before its outcome is judged at all.
    rollback_min_attempts: int
    #: How far below the replaced method's success rate the new method
    #: must fall before the switch is treated as a degradation. A margin,
    #: not equality, so ordinary noise never triggers a revert.
    rollback_degradation_margin: Decimal


def thresholds_from_settings(settings: Any) -> SwitchThresholds:
    """Build :class:`SwitchThresholds` from a ``Settings``-shaped object.

    ``getattr`` with the shipped default for every knob, so a stub
    settings object in a unit test (the
    ``tests/unit/test_promotion_degraded_repromotion.py``
    ``_StubFlushSettings`` shape) does not have to enumerate them.
    """
    return SwitchThresholds(
        min_evidence_samples=int(
            getattr(settings, "STRATEGY_SWITCH_MIN_EVIDENCE_SAMPLES", 6)
        ),
        min_evidence_window_seconds=int(
            getattr(settings, "STRATEGY_SWITCH_MIN_EVIDENCE_WINDOW_SECONDS", 1800)
        ),
        revert_evidence_multiplier=float(
            getattr(settings, "STRATEGY_SWITCH_REVERT_EVIDENCE_MULTIPLIER", 2.0)
        ),
        rollback_min_attempts=int(
            getattr(settings, "STRATEGY_SWITCH_ROLLBACK_MIN_ATTEMPTS", 10)
        ),
        rollback_degradation_margin=Decimal(
            str(getattr(settings, "STRATEGY_SWITCH_ROLLBACK_DEGRADATION_MARGIN", 0.20))
        ),
    )


def hysteresis_enabled(settings: Any) -> bool:
    """Master switch (``Settings.STRATEGY_SWITCH_HYSTERESIS_ENABLED``).

    ``False`` restores byte-for-byte the pre-W5.5-L2 optimizer behaviour
    — no hold, no band, no rollback — without reverting the migration.
    """
    return bool(getattr(settings, "STRATEGY_SWITCH_HYSTERESIS_ENABLED", True))


# --- Pure: minimum evidence + hysteresis band ------------------------------


@dataclass(frozen=True)
class SwitchEvidence:
    """Everything :func:`evaluate_switch` reasons over, already gathered."""

    #: The method the profile prefers right now (``None`` = never set).
    incumbent_method: str | None
    #: The method the promotion evaluator wants to install.
    candidate_method: str
    #: CUMULATIVE qualifying evidence for the candidate — the distinct
    #: qualifying-URL count, which survives every ``stats_buffer.drain``
    #: until the method actually promotes and is therefore the honest
    #: running total (a single cycle's drained delta is not).
    sample_count: int
    #: Wall-clock span the candidate's evidence covers, or ``None`` when
    #: the caller genuinely cannot measure it (no ``created_at`` on the
    #: stats row). ``None`` SKIPS the window check rather than failing it
    #: — an unmeasurable window is not evidence of a too-short one.
    window_seconds: float | None
    #: Whether a previous switch TO this candidate was rolled back. Widens
    #: the band (see ``revert_evidence_multiplier``).
    candidate_was_rolled_back: bool = False


@dataclass(frozen=True)
class SwitchVerdict:
    """Outcome of :func:`evaluate_switch`."""

    allow: bool
    required_samples: int
    reason: str


def evaluate_switch(
    evidence: SwitchEvidence, thresholds: SwitchThresholds
) -> SwitchVerdict:
    """Decide whether a preferred-method CHANGE has earned the right to happen.

    Pure and total. Three allow-outright cases, then the bar:

    * **No incumbent** — nothing to churn; installing the first method a
      profile ever had is not a switch (US1 AS1 is unaffected).
    * **Same method** — a re-affirmation (including the same-method
      re-promotion out of ``DEGRADED`` that
      ``promotion.apply_promotion`` exists to allow). Never held: holding
      it would re-open the fqtoners.com dead-end this codebase already
      fixed once.
    * Otherwise a genuine A→B change, which must clear
      ``min_evidence_samples`` (x ``revert_evidence_multiplier`` when B is
      a method a previous switch already rolled back) AND, when the
      window is measurable, ``min_evidence_window_seconds``.

    A held decision is "keep what you have", never "fail" — the evidence
    is not discarded, it keeps accumulating until it clears the bar.
    """
    if evidence.incumbent_method is None:
        return SwitchVerdict(
            allow=True,
            required_samples=0,
            reason="no incumbent method — first assignment is not a switch",
        )

    if evidence.candidate_method == evidence.incumbent_method:
        return SwitchVerdict(
            allow=True,
            required_samples=0,
            reason="same method — re-affirmation, not a switch",
        )

    required = max(1, int(thresholds.min_evidence_samples))
    if evidence.candidate_was_rolled_back:
        required = max(
            required + 1,
            int(math.ceil(required * max(1.0, thresholds.revert_evidence_multiplier))),
        )

    if evidence.sample_count < required:
        return SwitchVerdict(
            allow=False,
            required_samples=required,
            reason=(
                f"sample_count={evidence.sample_count} < required={required}"
                + (
                    " (widened revert band — this method was rolled back before)"
                    if evidence.candidate_was_rolled_back
                    else ""
                )
            ),
        )

    if (
        evidence.window_seconds is not None
        and evidence.window_seconds < thresholds.min_evidence_window_seconds
    ):
        return SwitchVerdict(
            allow=False,
            required_samples=required,
            reason=(
                f"evidence_window_seconds={int(evidence.window_seconds)} < "
                f"min_evidence_window_seconds={thresholds.min_evidence_window_seconds}"
            ),
        )

    return SwitchVerdict(
        allow=True,
        required_samples=required,
        reason=(
            f"sample_count={evidence.sample_count} >= required={required} AND "
            f"evidence window satisfied"
        ),
    )


# --- Pure: rollback --------------------------------------------------------


@dataclass(frozen=True)
class RollbackEvidence:
    """Everything :func:`evaluate_rollback` reasons over, already gathered."""

    #: The method the switch replaced — the target to roll back TO.
    from_method: str | None
    #: The method the switch installed.
    to_method: str
    #: ``from_method``'s ``strategy_attempt_stats.success_rate`` at the
    #: moment of the switch — the bar the switch promised to beat.
    baseline_success_rate: Decimal | None
    #: Attempts recorded on ``to_method`` STRICTLY AFTER the switch
    #: (current ``attempt_count`` minus the reading taken at switch time),
    #: so a long lifetime history cannot drown out post-switch outcomes.
    post_switch_attempts: int
    #: Successes recorded on ``to_method`` strictly after the switch.
    post_switch_successes: int
    #: Whether this switch already has a ``rolled_back_at`` stamp.
    already_rolled_back: bool


@dataclass(frozen=True)
class RollbackVerdict:
    """Outcome of :func:`evaluate_rollback`."""

    rollback: bool
    reason: str
    post_switch_success_rate: Decimal | None = None


def evaluate_rollback(
    evidence: RollbackEvidence, thresholds: SwitchThresholds
) -> RollbackVerdict:
    """Decide whether a switch degraded outcomes enough to be reverted.

    Pure and total. Rolls back only when ALL of:

    * the switch has not already been rolled back (so a rollback happens
      **exactly once** per switch — the durable stamp is the other half
      of that guarantee);
    * there is a prior method to roll back to;
    * at least ``rollback_min_attempts`` attempts were recorded on the new
      method *after* the switch (never judge on one sample);
    * both the baseline and the post-switch rate are actually known; and
    * the post-switch success rate is at least
      ``rollback_degradation_margin`` BELOW the replaced method's rate at
      switch time.

    "Not worse enough" is not a rollback. Equality is not a rollback.
    Only a clear, measured degradation is.
    """
    if evidence.already_rolled_back:
        return RollbackVerdict(rollback=False, reason="switch already rolled back once")

    if evidence.from_method is None:
        return RollbackVerdict(
            rollback=False, reason="no prior method recorded — nothing to roll back to"
        )

    if evidence.post_switch_attempts < max(1, thresholds.rollback_min_attempts):
        return RollbackVerdict(
            rollback=False,
            reason=(
                f"post_switch_attempts={evidence.post_switch_attempts} < "
                f"rollback_min_attempts={thresholds.rollback_min_attempts}"
            ),
        )

    if evidence.baseline_success_rate is None:
        return RollbackVerdict(
            rollback=False, reason="no baseline success rate recorded for the switch"
        )

    post_rate = Decimal(evidence.post_switch_successes) / Decimal(
        evidence.post_switch_attempts
    )
    floor = evidence.baseline_success_rate - thresholds.rollback_degradation_margin

    if post_rate >= floor:
        return RollbackVerdict(
            rollback=False,
            reason=(
                f"post_switch_success_rate={post_rate} >= "
                f"baseline={evidence.baseline_success_rate} - "
                f"margin={thresholds.rollback_degradation_margin}"
            ),
            post_switch_success_rate=post_rate,
        )

    return RollbackVerdict(
        rollback=True,
        reason=(
            f"post_switch_success_rate={post_rate} < baseline="
            f"{evidence.baseline_success_rate} - margin="
            f"{thresholds.rollback_degradation_margin} over "
            f"{evidence.post_switch_attempts} post-switch attempts"
        ),
        post_switch_success_rate=post_rate,
    )


# --- Durable store (capability-gated raw SQL) ------------------------------


def _exists_stmt():
    """Build the (unexecuted) ``to_regclass`` existence probe."""
    return text("SELECT to_regclass(:qualified_name) IS NOT NULL").bindparams(
        qualified_name=f"public.{STRATEGY_SWITCH_TABLE}"
    )


#: Attribute the per-session capability answer is memoised on. A flush
#: cycle asks "does the audit table exist?" once per candidate method per
#: profile; without a memo that is a dozen extra `to_regclass` round-trips
#: per profile per 60s tick, for an answer that cannot change inside one
#: task's session. Scoped to the session object (not a module global) so
#: it dies with the session and can never leak a stale answer across a
#: migration into a later task.
_STORE_MEMO_ATTR = "_w55l2_switch_store_available"


def switch_store_available(session: Session) -> bool:
    """Return ``True`` iff ``strategy_method_switches`` exists.

    Fails CLOSED to "unavailable" on any error or on a session object
    without ``.execute`` (the unit-test fakes in
    ``tests/unit/test_promotion_degraded_repromotion.py`` and
    ``tests/unit/test_rediscovery_runaway.py`` are exactly that). Falling
    back to "unavailable" is the safe direction: it disables the two
    guards that need history and leaves current behaviour otherwise
    intact, rather than raising inside a flush that must not fail on a
    telemetry concern.

    Memoised per session (see :data:`_STORE_MEMO_ATTR`).
    """
    cached = getattr(session, _STORE_MEMO_ATTR, None)
    if cached is not None:
        return bool(cached)

    execute = getattr(session, "execute", None)
    if execute is None:
        return False
    try:
        available = bool(execute(_exists_stmt()).scalar())
    except Exception:  # noqa: BLE001 - capability probe, degrade not raise
        logger.debug(
            "app_shared.strategy.hysteresis: switch-store probe failed; "
            "treating the durable switch audit as unavailable",
            exc_info=True,
        )
        return False

    try:
        setattr(session, _STORE_MEMO_ATTR, available)
    except Exception:  # noqa: BLE001 - a fake/slotted session; memo is optional
        pass
    return available


def _record_stmt(values: dict[str, Any]):
    return text(
        f"""
        INSERT INTO {STRATEGY_SWITCH_TABLE} (
            id, workspace_id, domain_strategy_profile_id, method_type,
            from_method, to_method, switched_at,
            evidence_samples, evidence_window_seconds,
            baseline_success_rate, switch_attempt_count, switch_success_count,
            rolled_back_at, rollback_reason, created_at, updated_at
        ) VALUES (
            :id, :workspace_id, :domain_strategy_profile_id, :method_type,
            :from_method, :to_method, :switched_at,
            :evidence_samples, :evidence_window_seconds,
            :baseline_success_rate, :switch_attempt_count, :switch_success_count,
            NULL, NULL, :switched_at, :switched_at
        )
        """
    ).bindparams(**values)


def record_switch(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    profile_id: uuid.UUID,
    method_type: str,
    from_method: str | None,
    to_method: str,
    evidence_samples: int,
    evidence_window_seconds: int | None,
    baseline_success_rate: Decimal | None,
    switch_attempt_count: int,
    switch_success_count: int,
    now: datetime,
) -> uuid.UUID | None:
    """Record one preferred-method change, returning its id (or ``None``).

    Written in the CALLER's transaction — the same one that carries the
    profile UPDATE — so the audit row either commits with the switch it
    describes or disappears with it. ``switch_attempt_count`` /
    ``switch_success_count`` are ``to_method``'s counter readings at this
    instant; subtracting them later is what isolates post-switch outcomes
    from a lifetime total.

    ``None`` (and no statement) when the durable store is absent.
    """
    if not switch_store_available(session):
        return None

    switch_id = new_uuid7()
    session.execute(
        _record_stmt(
            {
                "id": switch_id,
                "workspace_id": workspace_id,
                "domain_strategy_profile_id": profile_id,
                "method_type": method_type,
                "from_method": from_method,
                "to_method": to_method,
                "switched_at": now,
                "evidence_samples": int(evidence_samples),
                "evidence_window_seconds": (
                    None if evidence_window_seconds is None else int(evidence_window_seconds)
                ),
                "baseline_success_rate": baseline_success_rate,
                "switch_attempt_count": int(switch_attempt_count),
                "switch_success_count": int(switch_success_count),
            }
        )
    )
    logger.info(
        "%s profile_id=%s method_type=%s from=%s to=%s evidence_samples=%s",
        EVENT_SWITCH_RECORDED,
        profile_id,
        method_type,
        from_method,
        to_method,
        evidence_samples,
    )
    return switch_id


def _latest_open_stmt(profile_id: uuid.UUID, method_type: str):
    return text(
        f"""
        SELECT id, from_method, to_method, switched_at,
               baseline_success_rate, switch_attempt_count, switch_success_count,
               rolled_back_at
        FROM {STRATEGY_SWITCH_TABLE}
        WHERE domain_strategy_profile_id = :profile_id
          AND method_type = :method_type
          AND rolled_back_at IS NULL
        ORDER BY switched_at DESC
        LIMIT 1
        """
    ).bindparams(profile_id=profile_id, method_type=method_type)


def latest_open_switch(
    session: Session, profile_id: uuid.UUID, method_type: str
) -> Any | None:
    """The most recent not-yet-rolled-back switch for this (profile, type).

    ``None`` when the store is absent or there is no such switch.
    """
    if not switch_store_available(session):
        return None
    return session.execute(_latest_open_stmt(profile_id, method_type)).first()


def _was_rolled_back_stmt(profile_id: uuid.UUID, method_type: str, method_name: str):
    return text(
        f"""
        SELECT 1
        FROM {STRATEGY_SWITCH_TABLE}
        WHERE domain_strategy_profile_id = :profile_id
          AND method_type = :method_type
          AND to_method = :method_name
          AND rolled_back_at IS NOT NULL
        LIMIT 1
        """
    ).bindparams(
        profile_id=profile_id, method_type=method_type, method_name=method_name
    )


def candidate_was_rolled_back(
    session: Session, profile_id: uuid.UUID, method_type: str, method_name: str
) -> bool:
    """Whether a previous switch TO ``method_name`` was rolled back.

    The input to the WIDER band in :func:`evaluate_switch`. ``False``
    (band not widened) when the store is absent — the base bar still
    applies, only the anti-flap widening is inert.
    """
    if not switch_store_available(session):
        return False
    return session.execute(
        _was_rolled_back_stmt(profile_id, method_type, method_name)
    ).first() is not None


def _mark_rolled_back_stmt(switch_id: uuid.UUID, reason: str, now: datetime):
    return text(
        f"""
        UPDATE {STRATEGY_SWITCH_TABLE}
        SET rolled_back_at = :now, rollback_reason = :reason, updated_at = :now
        WHERE id = :switch_id AND rolled_back_at IS NULL
        """
    ).bindparams(switch_id=switch_id, reason=reason, now=now)


def mark_rolled_back(
    session: Session, switch_id: uuid.UUID, *, reason: str, now: datetime
) -> bool:
    """Stamp one switch as rolled back; ``True`` iff THIS call did it.

    ``WHERE rolled_back_at IS NULL`` is the exactly-once guard: two
    workers flushing the same profile concurrently both evaluate the same
    degradation, but only one ``UPDATE`` matches a row. The loser gets
    ``False`` and must not also revert the profile.
    """
    result = session.execute(_mark_rolled_back_stmt(switch_id, reason, now))
    claimed = bool(result.rowcount and result.rowcount > 0)
    if claimed:
        logger.warning(
            "%s switch_id=%s reason=%s", EVENT_SWITCH_ROLLED_BACK, switch_id, reason
        )
    return claimed

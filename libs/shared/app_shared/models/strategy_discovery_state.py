"""``strategy_discovery_state`` ORM model -- the durable cursor behind the
fleet-wide, chunked ``STRATEGY_DISCOVERY_SCAN`` sweep (EPA B4, F09,
"resumable long maintenance").

## The gap this closes

Every existing discovery trigger (`app_shared.strategy.resolution`'s AUTO
enqueue, `app_shared.strategy.rediscovery`'s guarded apply, the operator
`POST /v1/strategy/discovery-runs`) enqueues `STRATEGY_DISCOVERY_RUN` for
ONE already-known `(competitor, domain, url_pattern)` key the instant it
is discovered. None of them is a *fleet-wide* sweep that walks every
`domain_strategy_profiles` row still stuck at `DISCOVERY_REQUIRED` --
which is exactly the state a restored/imported dataset, or a profile
whose AUTO-trigger enqueue was lost before the outbox pattern existed
here, can be left in indefinitely with no self-healing path back to
`ACTIVE`/`LEARNING`.

`app.workers.tasks_strategy.strategy_discovery_scan` is that sweep. Like
`STRATEGY_PATTERN_BACKFILL`'s bounded batch (`_PATTERN_BACKFILL_BATCH_SIZE`
per invocation, no cursor needed there because *every* invocation rescans
from the same predicate), this one is bounded too
(`Settings.STRATEGY_DISCOVERY_MAX_DOMAINS_PER_RUN`) -- but unlike that
sibling, one full pass over every `DISCOVERY_REQUIRED` profile in a large
fleet can span many invocations, and each `STRATEGY_DISCOVERY_RUN` this
scan fans out is a paid `PROXY_HTTP` probe on its worst-case leg, so
losing track of "how far did the last pass get" (a task time limit, a
worker restart) must never mean either abandoning the rest of the fleet
or re-enqueueing a profile the previous invocation already forwarded.

## Shape: global, no RLS, one row per cursor key

Deliberately **no** ``workspace_id`` and **no** RLS -- the same shape and
rationale as ``maintenance_cadences``/``rollup_watermarks``: this cursor
tracks progress through a cross-tenant scan (every workspace's
`DISCOVERY_REQUIRED` profiles, ordered by id), not any one tenant's data.
Not workspace-owned: **must not** be added to
``app_shared.repository.WORKSPACE_OWNED_MODELS``.

Like ``RollupWatermark``, this table's natural key (``key``) is its own
primary key -- ``Base``'s inherited UUIDv7 ``id`` is suppressed (``id =
None``) rather than kept as a second, physically-nonexistent PK member.
Unlike ``RollupWatermark`` (whose runtime reads/writes are raw
``sqlalchemy.text`` behind a ``to_regclass`` probe, because its migration
was deferred to a later serialized slot), this table's migration lands in
the SAME change as the runtime code that uses it
(``app.workers.tasks_strategy``), so the ORM model is used directly --
no capability probe needed.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Integer, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app_shared.models.base import Base, TZDateTime, TimestampMixin

#: Single fleet-wide cursor key -- the scan has no per-workspace/per-shard
#: split today, so one row is the whole store. A stable string: renaming
#: it restarts the sweep from the beginning exactly once.
DISCOVERY_SCAN_STATE_KEY = "global"


class StrategyDiscoveryState(Base, TimestampMixin):
    """``strategy_discovery_state`` -- one durable cursor per scan ``key``.

    Global (no ``workspace_id``, no RLS) -- see the module docstring.
    """

    __tablename__ = "strategy_discovery_state"

    # This table's natural key IS its identity (the `RollupWatermark`
    # precedent) -- suppress Base's inherited UUIDv7 `id`.
    id = None  # type: ignore[assignment]

    #: Stable cursor identity (see :data:`DISCOVERY_SCAN_STATE_KEY`).
    key: Mapped[str] = mapped_column(Text(), primary_key=True)
    #: The last `domain_strategy_profiles.id` forwarded in the
    #: in-progress pass, ordered by id -- the next chunk resumes strictly
    #: after this. ``NULL`` means "start of a fresh pass" (either never
    #: run, or the previous pass completed and reset it).
    cursor_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    #: When the cursor last advanced (``NULL`` = never advanced).
    last_advanced_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    #: Monotonic count of chunks processed, for "is this sweep alive".
    advance_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)


__all__ = ["DISCOVERY_SCAN_STATE_KEY", "StrategyDiscoveryState"]

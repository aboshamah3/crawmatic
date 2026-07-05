"""Domain Strategy Optimizer — pure learning-logic package (SPEC-12).

Sibling of ``app_shared/access/``: SQLAlchemy / injected ``redis.Redis`` client /
stdlib only. No Scrapy/Twisted/FastAPI/``apps.*`` imports (Constitution I,
enforced by ``tests/unit/test_strategy_import_boundary.py`` + the repo-wide
``tests/unit/test_import_boundaries.py``).

Public surface re-exported here (T042) so callers import from
``app_shared.strategy`` rather than reaching into submodules.
"""

from __future__ import annotations

from app_shared.strategy.flush import flush_profile
from app_shared.strategy.promotion import (
    MethodStats,
    PromotionDecision,
    PromotionThresholds,
    apply_promotion,
    evaluate_promotion,
)
from app_shared.strategy.rediscovery import (
    CombinedStats,
    RecentAttemptSignal,
    RecentSignals,
    RediscoveryDecision,
    RediscoveryThresholds,
    apply_rediscovery,
    build_recent_signals,
    evaluate_rediscovery,
)
from app_shared.strategy.repository import (
    get_discovery_run,
    get_profile,
    list_discovery_runs_select,
    list_profiles_select,
    resolve_profile,
    stats_for_profile,
)
from app_shared.strategy.resolution import (
    StrategyStart,
    resolve_or_create_strategy_profile,
    resolve_strategy_start,
)
from app_shared.strategy.seed import (
    DiscoverySeedConfidences,
    seed_from_discovery,
    validate_sample_size,
)
from app_shared.strategy.stats_buffer import (
    DrainedDelta,
    PendingDelta,
    drain,
    read_pending,
    record_attempt,
)

__all__ = [
    # promotion
    "MethodStats",
    "PromotionDecision",
    "PromotionThresholds",
    "evaluate_promotion",
    "apply_promotion",
    # rediscovery
    "CombinedStats",
    "RecentAttemptSignal",
    "RecentSignals",
    "RediscoveryDecision",
    "RediscoveryThresholds",
    "evaluate_rediscovery",
    "build_recent_signals",
    "apply_rediscovery",
    # resolution
    "StrategyStart",
    "resolve_strategy_start",
    "resolve_or_create_strategy_profile",
    # seed
    "DiscoverySeedConfidences",
    "seed_from_discovery",
    "validate_sample_size",
    # stats buffer
    "PendingDelta",
    "DrainedDelta",
    "record_attempt",
    "read_pending",
    "drain",
    # flush
    "flush_profile",
    # repository
    "resolve_profile",
    "get_profile",
    "list_profiles_select",
    "get_discovery_run",
    "list_discovery_runs_select",
    "stats_for_profile",
]

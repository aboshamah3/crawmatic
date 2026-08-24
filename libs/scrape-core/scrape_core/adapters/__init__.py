"""Runnable request/response adapters shared by both scraper modes."""

from scrape_core.adapters.registry import DEFAULT_ADAPTER_REGISTRY, AdapterRegistry, get_adapter
from scrape_core.adapters.result import (
    AdapterContext,
    AdapterOutcome,
    AdapterRequest,
    AdapterResponse,
    AdapterResult,
    IdentityEvidence,
    IdentityStatus,
)

__all__ = [
    "AdapterContext",
    "AdapterOutcome",
    "AdapterRegistry",
    "AdapterRequest",
    "AdapterResponse",
    "AdapterResult",
    "DEFAULT_ADAPTER_REGISTRY",
    "IdentityEvidence",
    "IdentityStatus",
    "get_adapter",
]

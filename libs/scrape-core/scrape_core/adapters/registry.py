"""Adapter registry: the only adapter-key dispatch point spiders need."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from app_shared.enums import AdapterKey

from scrape_core.adapters.catalog import PublicCatalogJsonAdapter
from scrape_core.adapters.html import HtmlPipelineAdapter, RenderedHtmlAdapter
from scrape_core.adapters.repair import ExactIdUrlRepairAdapter
from scrape_core.adapters.result import AdapterContext, AdapterRequest, AdapterResponse, AdapterResult
from scrape_core.adapters.shopify import ShopifyProductJsonAdapter

__all__ = ["Adapter", "AdapterRegistry", "DEFAULT_ADAPTER_REGISTRY", "get_adapter"]


class Adapter(Protocol):
    def build_request(self, context: AdapterContext) -> AdapterRequest: ...

    def adapt(self, response: AdapterResponse, context: AdapterContext, **kwargs: Any) -> AdapterResult: ...


class AdapterRegistry:
    def __init__(self) -> None:
        self._factories: dict[AdapterKey, Callable[[], Adapter]] = {}

    def register(self, key: AdapterKey, factory: Callable[[], Adapter]) -> None:
        self._factories[key] = factory

    def get(self, key: AdapterKey | str) -> Adapter:
        try:
            normalized = key if isinstance(key, AdapterKey) else AdapterKey(key)
            factory = self._factories[normalized]
        except (ValueError, KeyError) as exc:
            raise KeyError(f"no runnable adapter registered for {key!r}") from exc
        return factory()


DEFAULT_ADAPTER_REGISTRY = AdapterRegistry()
for _key in (
    AdapterKey.DEFAULT_HTTP,
    AdapterKey.JSONLD_FIRST,
    AdapterKey.SELECTOR_ONLY,
    AdapterKey.REGEX_ONLY,
):
    DEFAULT_ADAPTER_REGISTRY.register(_key, HtmlPipelineAdapter)
DEFAULT_ADAPTER_REGISTRY.register(AdapterKey.PLAYWRIGHT_RENDERED, RenderedHtmlAdapter)
DEFAULT_ADAPTER_REGISTRY.register(AdapterKey.SHOPIFY_PRODUCT_JSON, ShopifyProductJsonAdapter)
DEFAULT_ADAPTER_REGISTRY.register(AdapterKey.PUBLIC_CATALOG_JSON, PublicCatalogJsonAdapter)
DEFAULT_ADAPTER_REGISTRY.register(AdapterKey.EXACT_ID_URL_REPAIR, ExactIdUrlRepairAdapter)


def get_adapter(key: AdapterKey | str) -> Adapter:
    return DEFAULT_ADAPTER_REGISTRY.get(key)

"""Adapters that pass normal or rendered HTML through the proven chain."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from scrape_core.adapters.config import resolve_adapter_config
from scrape_core.adapters.identity import validate_final_url
from scrape_core.adapters.result import (
    AdapterContext,
    AdapterOutcome,
    AdapterRequest,
    AdapterResponse,
    AdapterResult,
    IdentityStatus,
)
from scrape_core.extraction.pipeline import extract

__all__ = ["HtmlPipelineAdapter", "RenderedHtmlAdapter"]


class HtmlPipelineAdapter:
    def build_request(self, context: AdapterContext) -> AdapterRequest:
        config = resolve_adapter_config(context)
        return AdapterRequest(
            context.target_url,
            headers=config.get("headers", {}),
            follow_redirects=config.get("follow_redirects", True) is not False,
        )

    def adapt(
        self,
        response: AdapterResponse,
        context: AdapterContext,
        *,
        preferred_method: Any = None,
    ) -> AdapterResult:
        config = resolve_adapter_config(context)
        rules = config.get("identity_rules", {})
        rules = rules if isinstance(rules, Mapping) else {}
        identity = validate_final_url(
            response.requested_url or context.target_url,
            response.final_url,
            expected_identifier=context.identifier,
            rules=rules,
        )
        if not identity.allows_extraction:
            outcome = (
                AdapterOutcome.NOT_LISTED
                if identity.status == IdentityStatus.NOT_LISTED
                else AdapterOutcome.IDENTITY_MISMATCH
            )
            return AdapterResult(outcome, response.final_url, identity, message=identity.reason)

        body = response.body.decode("utf-8", errors="replace") if isinstance(response.body, bytes) else response.body
        # EPA C5 (F19): `url`/`profile_version` are correlation for the
        # ranker's SHADOW record only -- neither affects extraction, and
        # the returned candidate is byte-for-byte what it was before.
        # Passed here because this adapter is the live path's single
        # entry into the chain: without them every shadow event on real
        # traffic would land with a NULL domain, and the C11 gate is
        # decided per domain against the C4 labeled sets.
        candidate = extract(
            body,
            context.profile,
            preferred_method=preferred_method,
            url=response.final_url or response.requested_url or context.target_url,
            profile_version=getattr(context.profile, "version", None),
        )
        return AdapterResult(
            AdapterOutcome.FOUND if candidate else AdapterOutcome.NOT_FOUND,
            response.final_url,
            identity,
            candidate=candidate,
            canonical_url=response.final_url,
        )


class RenderedHtmlAdapter(HtmlPipelineAdapter):
    """Marker adapter: extraction remains the complete HTML chain."""


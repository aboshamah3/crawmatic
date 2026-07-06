"""Live browser-spider proxied-context test (SPEC-14 T035, US4 AS3, SC-006,
`contracts/browser-safety.md` "Proxy") — DEFERRED.

Seeds a workspace-default ``AccessPolicy`` (``PROXY_FIRST``) referencing a
``ProxyProvider`` with a plaintext password, so ``_prepare_dispatch``
assigns a proxy for the target's single attempt -- exercising
``_browser_request_for``'s proxied-context branch (T032):
``meta["playwright_context"] = f"proxy:{provider_id}"`` +
``meta["playwright_context_kwargs"] = {"proxy": {...}}``, never
``meta["proxy"]`` (the HTTP-transport-specific key this project's
``DOWNLOADER_MIDDLEWARES`` never reads, since
``HttpProxyMiddleware`` isn't registered here).

Asserts: the persisted ``request_attempts`` row carries
``access_method=PLAYWRIGHT_PROXY`` plus the real ``proxy_provider_id``/
``proxy_country`` (reused SPEC-10 audit, unchanged for the browser
transport), and the proxy's plaintext password appears in **no** line of
the subprocess's captured stdout/stderr (FR-011, "password never
logged"). The seeded proxy provider intentionally points at an address
nothing listens on in this environment -- the fetch itself is expected to
fail (`PROXY_FAILED`/`PLAYWRIGHT_FAILED`, never a silent direct fetch);
the assertions here are about the *attempt's own recorded fields* and the
password's absence from logs, not a successful price extraction (which
``test_browser_spider_render_live.py`` already covers for the unproxied
path).

Needs a reachable Postgres (SPEC-10 migration applied) + Redis AND an
installed Chromium binary (``playwright install``) -- this no-container-
engine build environment has neither, so this SKIPS cleanly here (never
faked).

Author now; leave unchecked (DEFERRED — needs a Postgres+Redis host with
an installed Chromium binary).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from ._browser_spider_live_support import (
    live_browser_stack_reachable,
    run_generic_browser_price_spider_subprocess,
)
from ._scrapyd_spider_live_support import (
    SeededWorkspace,
    cleanup_seeded_workspace,
    seed_competitor,
    seed_match,
    seed_workspace_with_variant,
    serve_fixture_pages,
)

_REQUIRED_TABLES = frozenset(
    {
        "price_observations",
        "request_attempts",
        "match_current_prices",
        "competitor_product_matches",
        "competitors",
        "product_variants",
        "products",
        "workspaces",
        "proxy_providers",
        "access_policies",
    }
)

pytestmark = pytest.mark.skipif(
    not live_browser_stack_reachable(_REQUIRED_TABLES),
    reason=(
        "Needs reachable DATABASE_URL + REDIS_URL with the SPEC-10 access-policy "
        "migration applied, AND an installed Playwright Chromium binary -- "
        "not available in this environment."
    ),
)

_SUBPROCESS_TIMEOUT_SECONDS = 30.0
_FIXTURE_HTML = "<html><body><div id='price'>$9.99</div></body></html>"
# Nothing listens here -- the proxied fetch is expected to fail; this test
# proves the *attempt's own recorded fields* + no-password-leak, not a
# successful extraction through a real proxy.
_UNREACHABLE_PROXY_PORT = 1


def _create_proxy_provider(workspace_id: uuid.UUID, *, plaintext_password: str) -> uuid.UUID:
    from app_shared.database import get_session
    from app_shared.enums import ProxyType
    from app_shared.models.access import ProxyProvider
    from app_shared.security.encryption import encrypt_secret

    unique = uuid.uuid4().hex[:8]
    encrypted = encrypt_secret(plaintext_password)
    with get_session() as session:
        provider = ProxyProvider(
            workspace_id=workspace_id,
            name=f"browser-proxy-{unique}",
            base_url=f"http://127.0.0.1:{_UNREACHABLE_PROXY_PORT}",
            type=ProxyType.DATACENTER,
            username="browseruser",
            password_encrypted=encrypted.ciphertext,
            password_key_version=encrypted.key_version,
            country_code="US",
        )
        session.add(provider)
        session.commit()
        session.refresh(provider)
        return provider.id


def _create_access_policy(workspace_id: uuid.UUID, *, provider_id: uuid.UUID) -> uuid.UUID:
    from app_shared.database import get_session
    from app_shared.enums import AccessStrategy
    from app_shared.models.access import AccessPolicy

    with get_session() as session:
        policy = AccessPolicy(
            workspace_id=workspace_id,
            name="default",  # WORKSPACE_DEFAULT_POLICY_NAME -- resolves for every domain, no rule needed
            strategy=AccessStrategy.PROXY_FIRST,
            provider_id=provider_id,
            max_retries=0,
            use_proxy_on_first_attempt=True,
            use_proxy_on_retry=True,
            allow_browser_fallback=False,
        )
        session.add(policy)
        session.commit()
        session.refresh(policy)
        return policy.id


def _cleanup_access_rows(workspace_id: uuid.UUID) -> None:
    from sqlalchemy import text

    from app_shared.database import get_session

    with get_session() as session:
        session.execute(text("DELETE FROM access_policies WHERE workspace_id = :ws"), {"ws": workspace_id})
        session.execute(text("DELETE FROM proxy_providers WHERE workspace_id = :ws"), {"ws": workspace_id})
        session.commit()


@pytest.fixture()
def seeded() -> Iterator[SeededWorkspace]:
    workspace = seed_workspace_with_variant("spec14-browser-proxy")
    try:
        yield workspace
    finally:
        _cleanup_access_rows(workspace.workspace_id)
        cleanup_seeded_workspace(workspace)


def test_assigned_proxy_records_playwright_proxy_attempt_with_no_password_in_logs(
    seeded: SeededWorkspace,
) -> None:
    plaintext_password = f"s3cr3t-{uuid.uuid4().hex}"
    provider_id = _create_proxy_provider(seeded.workspace_id, plaintext_password=plaintext_password)
    _create_access_policy(seeded.workspace_id, provider_id=provider_id)

    server, thread, port = serve_fixture_pages({"/product": _FIXTURE_HTML})
    try:
        competitor_id = seed_competitor(seeded, "browser-proxy-competitor")
        match_url = f"http://127.0.0.1:{port}/product"
        match_id = seed_match(seeded, competitor_id, match_url)
        scrape_job_id = uuid.uuid4()

        result = run_generic_browser_price_spider_subprocess(
            workspace_id=seeded.workspace_id,
            scrape_job_id=scrape_job_id,
            match_ids=[match_id],
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        )
        assert result.returncode == 0, result.stderr

        # FR-011 / SC-006: the decrypted password never appears in any
        # captured log line, whether the fetch through the (unreachable)
        # proxy ultimately succeeds or fails.
        assert plaintext_password not in result.stdout
        assert plaintext_password not in result.stderr

        from sqlalchemy import text

        from app_shared.database import get_session

        with get_session() as session:
            attempts = session.execute(
                text(
                    "SELECT access_method, proxy_provider_id, proxy_country, success, error_code "
                    "FROM request_attempts WHERE workspace_id = :ws AND match_id = :match"
                ),
                {"ws": seeded.workspace_id, "match": match_id},
            ).fetchall()
        assert len(attempts) == 1, attempts
        attempt = attempts[0]
        assert attempt.access_method == "PLAYWRIGHT_PROXY"
        assert attempt.proxy_provider_id == provider_id
        assert attempt.proxy_country == "US"
        # The proxy is deliberately unreachable in this environment -- the
        # attempt is expected to fail (never a silent direct fetch), but
        # it must still carry the real proxy audit fields above.
        if not attempt.success:
            assert attempt.error_code in ("PROXY_FAILED", "PLAYWRIGHT_FAILED", "TIMEOUT")
    finally:
        server.shutdown()
        server.server_close()

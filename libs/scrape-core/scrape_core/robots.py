"""`RobotsPolicyMiddleware` — per-request robots handling (`contracts/robots-middleware.md`, research D7).

Resolves `robots_policy` (`app_shared.enums.RobotsPolicy`) **per
request** from the competitor config the spider loaded — attached to
`request.meta["robots_policy"]` — never Scrapy's process-global
`ROBOTSTXT_OBEY` (which stays `False` in `price_monitor/settings.py`).
A request with no explicit policy defaults to the conservative
`RESPECT` (matching `Competitor.robots_policy`'s own default).

| `robots_policy`         | Behavior                                              |
|--------------------------|--------------------------------------------------------|
| `RESPECT`                | fetch/parse robots.txt; a disallowed path is skipped/recorded (`BLOCKED`) |
| `IGNORE_AFTER_APPROVAL`  | fetch regardless (no robots.txt lookup at all)          |
| `REVIEW_REQUIRED`        | not yet approved -> always skip/record (conservative)   |

Reactor safety: the two "no IO" policies (`IGNORE_AFTER_APPROVAL`,
`REVIEW_REQUIRED`) are decided synchronously in `process_request` (no
blocking call is ever made). `RESPECT` may need to fetch robots.txt (a
cache miss) — that work is offloaded through
`scrape_core.db.run_in_thread` (the same seam `pipelines.py` uses for
DB IO), so `process_request` never blocks the reactor thread itself.
The actual decision logic lives in `_decide_respect`, a small pure/
synchronous method callable directly (bypassing the Twisted seam) — the
seam a unit test exercises without needing a running reactor.

The robots fetcher is injectable (`robots_fetcher: Callable[[str], str
| None]`) so fixture tests supply a canned robots.txt body with no
network call (FR-021, contracts/robots-middleware.md "Testability").
"""

from __future__ import annotations

import http.client
import inspect
import ipaddress
import logging
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from scrapy.exceptions import IgnoreRequest

from app_shared.enums import RobotsPolicy
from app_shared.url_safety import UnsafeUrlError, UnsafeUrlReason

from scrape_core.db import run_in_thread
from scrape_core.errors import ROBOTS_BLOCKED_ERROR_CODE
from scrape_core.safety.fetch import Resolver, system_resolver, validate_resolved_target

__all__ = [
    "RobotsPolicyMiddleware",
    "RobotsBlockedError",
    "UnsafeTargetError",
    "default_robots_fetcher",
]

logger = logging.getLogger(__name__)

RobotsFetcher = Callable[..., "str | None"]


@dataclass(frozen=True)
class _CachedRobots:
    parser: RobotFileParser | None
    fetched_at: float


class RobotsBlockedError(IgnoreRequest):
    """Raised when the per-request `robots_policy` blocks a target (`BLOCKED`)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.error_code = ROBOTS_BLOCKED_ERROR_CODE


#: Hard cap on how much of a robots.txt body is read (Google's own limit
#: is 500 KiB; anything past this is ignored, never buffered).
_ROBOTS_MAX_BYTES = 512 * 1024
_ROBOTS_TIMEOUT_SECONDS = 5.0

#: Module-level seam so tests can inject a fake DNS answer while still
#: exercising the real `validate_resolved_target` guard.
_system_resolver: Resolver = system_resolver


class UnsafeTargetError(UnsafeUrlError):
    """The robots.txt host failed the fetch-time SSRF guard."""

    def __init__(
        self,
        message: str,
        reason: UnsafeUrlReason = UnsafeUrlReason.PRIVATE_OR_INTERNAL_IP,
    ) -> None:
        super().__init__(reason, message)


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def _resolve_validated_ip(host: str, port: int) -> str:
    """Resolve `host` once, validate every answer, return the IP to dial.

    Reuses (never re-implements) `scrape_core.safety.fetch.
    validate_resolved_target` -- the same guard behind the spider
    middleware, the browser guard and the strategy discovery probe. The
    resolver is wrapped so the addresses the guard approved are exactly
    the addresses we connect to: no second lookup, no rebinding window.
    Raises `UnsafeUrlError` (incl. `UnsafeTargetError`) or `OSError`.
    """
    answers: list[str] = []

    def capturing_resolver(name: str) -> list[str]:
        resolved = list(_system_resolver(name))
        answers.extend(resolved)
        return resolved

    validate_resolved_target(
        f"http://{_url_host(host)}:{port}/robots.txt", resolver=capturing_resolver
    )
    if not answers:
        raise UnsafeTargetError(f"no validated address for {host!r}")
    return answers[0]


def _resolve_loopback_ip_for_test(host: str) -> str:
    """Test-only resolution: loopback answers only, everything else refused."""
    resolved = list(_system_resolver(host))
    if not resolved or not all(ipaddress.ip_address(ip).is_loopback for ip in resolved):
        raise UnsafeTargetError(f"test mode only allows loopback, got {host!r}")
    return resolved[0]


def _open_connection(
    scheme: str, ip: str, port: int, host: str
) -> http.client.HTTPConnection:
    """Connect to the already-validated `ip`; TLS SNI + cert check use `host`."""
    if scheme == "https":
        context = ssl.create_default_context()
        raw = socket.create_connection((ip, port), timeout=_ROBOTS_TIMEOUT_SECONDS)
        try:
            tls = context.wrap_socket(raw, server_hostname=host)
        except BaseException:
            raw.close()
            raise
        conn = http.client.HTTPSConnection(
            ip, port, context=context, timeout=_ROBOTS_TIMEOUT_SECONDS
        )
        conn.sock = tls
        return conn
    conn = http.client.HTTPConnection(ip, port, timeout=_ROBOTS_TIMEOUT_SECONDS)
    conn.sock = socket.create_connection((ip, port), timeout=_ROBOTS_TIMEOUT_SECONDS)
    return conn


def default_robots_fetcher(
    robots_url: str,
    user_agent: str = "price_monitor",
    *,
    _allow_loopback_for_test: bool = False,
) -> str | None:
    """Best-effort robots.txt fetch for real (non-fixture) runs, SSRF-guarded.

    Must only ever be invoked off the reactor thread (`process_request`
    below always calls this via `run_in_thread`). Returns `None` (fail
    open -- no robots.txt means "allow", the conventional absence
    semantics) on any fetch error, any non-2xx status, any redirect
    (3xx is never followed: a redirect is the classic SSRF pivot), or a
    host the SSRF guard refuses. The connection is made to the validated
    IP with `Host:` (and TLS SNI/verification) set to the hostname, and
    at most `_ROBOTS_MAX_BYTES` of body are read.

    `_allow_loopback_for_test` is keyword-only and test-only: it admits
    loopback answers (and only loopback) so a local fixture server can be
    reached. Production callers never pass it.
    """
    conn: http.client.HTTPConnection | None = None
    try:
        parts = urlsplit(robots_url)
        scheme = (parts.scheme or "").lower()
        host = parts.hostname
        if scheme not in ("http", "https") or not host:
            return None
        default_port = 443 if scheme == "https" else 80
        port = parts.port or default_port

        if _allow_loopback_for_test:
            ip = _resolve_loopback_ip_for_test(host)
        else:
            ip = _resolve_validated_ip(host, port)

        host_header = _url_host(host) if port == default_port else f"{_url_host(host)}:{port}"
        path = parts.path or "/robots.txt"
        if parts.query:
            path = f"{path}?{parts.query}"

        conn = _open_connection(scheme, ip, port, host)
        conn.request(
            "GET",
            path,
            headers={"Host": host_header, "User-Agent": user_agent, "Accept": "text/plain, */*"},
        )
        response = conn.getresponse()
        if not 200 <= response.status < 300:
            return None
        body = response.read(_ROBOTS_MAX_BYTES)
        return body.decode("utf-8", errors="replace")
    except UnsafeUrlError as exc:
        logger.warning("robots: fetch refused by SSRF guard url=%s reason=%s", robots_url, exc.reason)
        return None
    except Exception:  # noqa: BLE001 - best-effort only, never raise from here
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


class RobotsPolicyMiddleware:
    """Scrapy downloader middleware: per-request `robots_policy` enforcement."""

    def __init__(
        self,
        robots_fetcher: RobotsFetcher | None = None,
        user_agent: str = "price_monitor",
        cache_ttl_seconds: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._robots_fetcher: RobotsFetcher = robots_fetcher or default_robots_fetcher
        self._user_agent = user_agent
        self._cache_ttl_seconds = max(0.0, float(cache_ttl_seconds))
        self._clock = clock
        self._cache: dict[tuple[str, str], _CachedRobots] = {}

    @classmethod
    def from_crawler(cls, crawler: Any) -> "RobotsPolicyMiddleware":
        user_agent = crawler.settings.get("USER_AGENT") or "price_monitor"
        ttl = crawler.settings.getfloat("ROBOTS_CACHE_TTL_SECONDS", 900.0)
        return cls(user_agent=user_agent, cache_ttl_seconds=ttl)

    def process_request(self, request: Any, spider: Any) -> Any:
        policy = request.meta.get("robots_policy", RobotsPolicy.RESPECT)

        if policy == RobotsPolicy.IGNORE_AFTER_APPROVAL:
            # No IO at all -- synchronous, reactor-safe by construction.
            return None

        if policy == RobotsPolicy.REVIEW_REQUIRED:
            # No IO -- conservative "not yet approved" skip, synchronous.
            raise RobotsBlockedError(
                f"robots_policy=REVIEW_REQUIRED (not yet approved to fetch): {request.url}"
            )

        # RESPECT: may require a robots.txt fetch (cache miss) -> offload
        # off the reactor thread through the established run_in_thread seam.
        # A repaired/canonical product path can be supplied by the strategy
        # adapter.  Robots is evaluated against that exact path instead of a
        # stale tracking/slug URL, while remaining completely domain-generic.
        canonical_url = request.meta.get("robots_canonical_url") or request.meta.get(
            "canonical_url"
        )
        return run_in_thread(
            self._decide_respect,
            request.url,
            self._user_agent,
            canonical_url,
        )

    def _decide_respect(
        self,
        url: str,
        user_agent: str,
        canonical_url: str | None = None,
    ) -> None:
        """Pure decision core for `RESPECT` -- cache-aware, synchronous.

        Safe to call directly (bypassing `run_in_thread`) in unit tests:
        with an injected fixture `robots_fetcher` there is no real IO, so
        no reactor is needed to exercise this logic.
        """
        evaluation_url = canonical_url or url
        parsed = urlsplit(evaluation_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        cache_key = (origin, user_agent)
        now = self._clock()

        cached = self._cache.get(cache_key)
        if cached is None or now - cached.fetched_at >= self._cache_ttl_seconds:
            body = self._fetch_robots(f"{origin}/robots.txt", user_agent)
            cached = _CachedRobots(parser=self._parse(body), fetched_at=now)
            self._cache[cache_key] = cached

        parser = cached.parser
        if parser is not None and not parser.can_fetch(user_agent, evaluation_url):
            raise RobotsBlockedError(
                "robots_policy=RESPECT: disallowed by robots.txt: "
                f"{evaluation_url}"
            )
        return None

    def _fetch_robots(self, robots_url: str, user_agent: str) -> str | None:
        """Invoke new two-argument fetchers while preserving fixture/custom fetchers.

        Older deployments commonly inject ``Callable[[str], str | None]``.
        Signature inspection keeps those working without catching a TypeError
        raised *inside* a fetcher and accidentally issuing the request twice.
        """
        try:
            parameters = inspect.signature(self._robots_fetcher).parameters.values()
        except (TypeError, ValueError):
            parameters = ()
        accepts_two = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in parameters) or sum(
            p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            for p in parameters
        ) >= 2
        if accepts_two:
            return self._robots_fetcher(robots_url, user_agent)
        return self._robots_fetcher(robots_url)

    @staticmethod
    def _parse(body: str | None) -> RobotFileParser | None:
        if body is None:
            # No robots.txt (fetch failed / 404) -- conventional semantics:
            # absence means "allow everything".
            return None
        parser = RobotFileParser()
        parser.parse(body.splitlines())
        return parser

"""A Chrome-impersonating (TLS-fingerprint) download handler.

**Why this exists.** amazon.sa and noon.com reject the *Scrapy/Twisted
HTTP client itself* -- not our headers, not our proxy IPs. One cause,
two symptoms: amazon answers with a ~5.3 KB CAPTCHA interstitial at HTTP
200, noon black-holes the connection until the timeout fires. Proven
2026-08-04 with a controlled matrix: byte-identical headers, the same
DataImpulse SA proxy, the same deterministic session IDs and the same
minute -- curl scored 26/26 priced while Scrapy was blocked 3 of 8, and
ASIN ``B071GLC1DR`` on the *identical* sessid returned 1.1 MB with a
real price to curl and 5,273 bytes of "automated access" to Scrapy.
``curl --http1.1`` still passed 4/4, so it is neither HTTP/2-vs-1.1 nor
header content -- what differs is the TLS/ALPN handshake fingerprint.

So this handler routes *only* the affected domains through ``curl_cffi``
with ``impersonate=<profile>``, which replays a real Chrome handshake.
Every other request is delegated verbatim to the wrapped
``HTTP11DownloadHandler``, so the healthy sites keep their exact current
code path (precedent for swapping handlers: the browser node's
``DOWNLOAD_HANDLERS``).

Deliberate decisions, recorded so they are not re-litigated:

* **Scrapy's headers are not forwarded.** ``impersonate=`` supplies the
  browser's own header set *in browser order*, which is itself part of
  the fingerprint; injecting our dict weakens it. (The ``Sec-Fetch-*``
  work was a dead end for exactly this reason -- headers were never the
  lever.) Only ``Proxy-Authorization`` is consumed, and it is turned
  into proxy-URL credentials rather than sent as a header.
* **``curl_cffi`` is synchronous**, so the call is offloaded through
  :func:`scrape_core.db.run_in_thread` (``deferToThread``) -- the one
  sanctioned blocking seam. The reactor is never blocked.
* **Redirects are not followed by curl.** ``allow_redirects=False``
  hands every 3xx back to Scrapy's ``RedirectMiddleware`` so the SSRF
  guard re-validates each hop, which the settings module requires.
* **``Content-Encoding``/``Content-Length`` are stripped** from the
  response: curl_cffi returns an already-decompressed body, and leaving
  those headers makes ``HttpCompressionMiddleware`` decode it a second
  time (and mis-state the length).
* **Exception class names encode their classification.**
  ``scrape_core.errors.classify_exception`` is class-*name*-based, so
  the wrappers below are named ``...TimeoutError`` / ``...ProxyError``
  / ``...DNSError`` to land on TIMEOUT / PROXY_FAILED / DNS_ERROR in
  the existing retry ladder.

``BlockDetectionMiddleware`` still sits above this handler, so amazon's
200-with-interstitial detection keeps working unchanged.
"""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from scrapy.core.downloader.handlers.http11 import HTTP11DownloadHandler
from scrapy.http import Request, Response
from scrapy.responsetypes import responsetypes

from scrape_core.db import as_awaitable, run_in_thread

__all__ = [
    "ImpersonateDNSError",
    "ImpersonateProxyError",
    "ImpersonateTimeoutError",
    "ImpersonateTransportError",
    "ImpersonatingDownloadHandler",
    "should_impersonate",
]


class ImpersonateTransportError(Exception):
    """A ``curl_cffi`` transport failure with no more specific class.

    Classifies as ``UNKNOWN_ERROR`` -- the subclasses below exist purely
    so the *name* carries the classification (see module docstring).
    """


class ImpersonateTimeoutError(ImpersonateTransportError):
    """Name contains "timeout" -> ``ScrapeErrorCode.TIMEOUT``."""


class ImpersonateProxyError(ImpersonateTransportError):
    """Name contains "proxy" -> ``ScrapeErrorCode.PROXY_FAILED``."""


class ImpersonateDNSError(ImpersonateTransportError):
    """Name contains "dns" -> ``ScrapeErrorCode.DNS_ERROR``."""


# libcurl error numbers we can classify precisely. Anything else falls
# back to the message-substring check in `_wrap_error`, then to the
# generic transport error.
_CURL_TIMEOUT_CODES = frozenset({28})  # OPERATION_TIMEDOUT
_CURL_PROXY_CODES = frozenset({5, 97})  # COULDNT_RESOLVE_PROXY, PROXY
_CURL_DNS_CODES = frozenset({6})  # COULDNT_RESOLVE_HOST


def _parse_domains(raw: str) -> tuple[str, ...]:
    """Split a comma-separated domain list into lowercase entries."""
    return tuple(part.strip().lower().lstrip(".") for part in raw.split(",") if part.strip())


def _host_matches(host: str, domains: tuple[str, ...]) -> bool:
    """True if ``host`` is one of ``domains`` or a subdomain of one.

    Suffix matching is dot-anchored so ``notamazon.sa`` never matches
    ``amazon.sa``.
    """
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in domains)


def should_impersonate(request: Request, domains: tuple[str, ...]) -> bool:
    """Decide whether ``request`` takes the impersonating path.

    ``request.meta["impersonate"]`` wins when present (either direction,
    so a spider can force it on for a one-off host or force it off for a
    single request); otherwise the URL host is matched against the
    configured domain list. Domain matching is what lets amazon/noon be
    switched on with **no spider change at all**.
    """
    override = request.meta.get("impersonate")
    if override is not None:
        return bool(override)
    return _host_matches(urlsplit(request.url).hostname or "", domains)


def _proxy_url(request: Request) -> str | None:
    """Rebuild a credentialed proxy URL for curl_cffi, or ``None``.

    The spider sets ``meta["proxy"]`` without userinfo (by design, so
    the credentials never ride in the URL through Scrapy) and puts the
    upstream Basic credentials in a ``Proxy-Authorization`` header
    instead. curl_cffi wants them in the URL, so the header is decoded
    back here -- in memory, and never logged.
    """
    proxy = request.meta.get("proxy")
    if not proxy:
        return None
    if isinstance(proxy, bytes):
        proxy = proxy.decode("ascii")

    auth = request.headers.get(b"Proxy-Authorization")
    if not auth:
        return proxy
    try:
        scheme, _, token = auth.decode("ascii").partition(" ")
        if scheme.lower() != "basic":
            return proxy
        userinfo = base64.b64decode(token).decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        # A credential we can't parse is not worth crashing (or logging)
        # over -- fall back to the bare proxy and let the upstream 407.
        return proxy

    parts = urlsplit(proxy)
    if not parts.hostname:
        return proxy
    netloc = f"{userinfo}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme or "http", netloc, parts.path, parts.query, parts.fragment))


def _wrap_error(exc: BaseException) -> Exception:
    """Map a ``curl_cffi`` error onto a name-classified wrapper."""
    code = getattr(exc, "code", None)
    code = int(code) if isinstance(code, int) else None
    text = str(exc).lower()

    if code in _CURL_TIMEOUT_CODES or "timed out" in text or "timeout" in text:
        return ImpersonateTimeoutError(str(exc))
    if code in _CURL_PROXY_CODES or "proxy" in text:
        return ImpersonateProxyError(str(exc))
    if code in _CURL_DNS_CODES or "resolve host" in text:
        return ImpersonateDNSError(str(exc))
    return ImpersonateTransportError(str(exc))


def _response_headers(raw: Any) -> dict[str, list[bytes]]:
    """Normalize curl_cffi's headers, dropping the two unsafe ones.

    ``Content-Encoding`` and ``Content-Length`` describe the body *as it
    was on the wire*; curl_cffi hands back a decoded body, so keeping
    them would make ``HttpCompressionMiddleware`` decode it again.
    """
    items = raw.multi_items() if hasattr(raw, "multi_items") else list(raw.items())
    headers: dict[str, list[bytes]] = {}
    for name, value in items:
        if name.lower() in ("content-encoding", "content-length"):
            continue
        if isinstance(value, str):
            value = value.encode("latin-1", "replace")
        headers.setdefault(name, []).append(value)
    return headers


class ImpersonatingDownloadHandler:
    """Registered for both ``http`` and ``https``; delegates by default.

    Only requests selected by :func:`should_impersonate` go through
    ``curl_cffi``; everything else is handed to a real
    ``HTTP11DownloadHandler`` instance, so the 8 healthy competitor
    sites are byte-for-byte unaffected by this handler existing.
    """

    # Scrapy instantiates a handler on first use for its scheme rather
    # than at crawler start. Declared explicitly (it is the default, but
    # omitting it is deprecated since 2.16) so the wrapped
    # HTTP11DownloadHandler is never built for a crawl that makes no
    # HTTP request at all.
    lazy = True

    def __init__(self, settings: Any, crawler: Any = None) -> None:
        self._fallback = HTTP11DownloadHandler.from_crawler(crawler)
        self._domains = _parse_domains(settings.get("SCRAPE_IMPERSONATE_DOMAINS", ""))
        self._profile = settings.get("SCRAPE_IMPERSONATE_PROFILE") or "chrome131"
        self._default_timeout = settings.getfloat("DOWNLOAD_TIMEOUT", 180.0)

    @classmethod
    def from_crawler(cls, crawler: Any) -> "ImpersonatingDownloadHandler":
        return cls(crawler.settings, crawler)

    async def download_request(self, request: Request) -> Response:
        """Scrapy 2.16's coroutine handler API (``request`` only).

        Declaring this ``async def`` is what keeps Scrapy on the modern
        call path. A plain ``def`` returning a ``Deferred`` is the
        deprecated one, and Scrapy invokes *that* as
        ``download_request(request, spider)`` -- which no longer matches
        the wrapped ``HTTP11DownloadHandler``'s own signature, so every
        delegated (non-impersonated) request would fail with a
        ``TypeError``.
        """
        if not should_impersonate(request, self._domains):
            return await self._fallback.download_request(request)
        # curl_cffi is synchronous: never call it on the reactor thread.
        # `as_awaitable` bridges the Deferred to whichever machinery is
        # driving this coroutine (Twisted's own on the HTTP node,
        # asyncio on the browser node) -- see scrape_core.db.
        return await as_awaitable(run_in_thread(self._fetch, request))

    def _fetch(self, request: Request) -> Response:
        """Perform one impersonated fetch. Runs in a thread-pool thread."""
        try:
            from curl_cffi import requests as curl_requests
        except ImportError as exc:  # pragma: no cover - deploy-time only
            # Deliberately raised here rather than at handler
            # construction: a missing optional dependency must break
            # only amazon/noon, never the eight healthy sites.
            raise ImpersonateTransportError(
                "curl_cffi is not installed; the impersonating transport is unavailable"
            ) from exc

        proxy = _proxy_url(request)
        timeout = request.meta.get("download_timeout") or self._default_timeout
        body = request.body or None

        try:
            resp = curl_requests.request(
                request.method,
                request.url,
                data=body,
                # Scrapy's headers are intentionally NOT forwarded --
                # `impersonate` owns the header set and its order.
                impersonate=self._profile,
                proxies={"http": proxy, "https": proxy} if proxy else None,
                timeout=timeout,
                # Scrapy's RedirectMiddleware must see every hop so the
                # SSRF guard re-validates it.
                allow_redirects=False,
                verify=True,
            )
        except Exception as exc:
            raise _wrap_error(exc) from exc

        headers = _response_headers(resp.headers)
        content = resp.content or b""
        response_cls = responsetypes.from_args(headers=headers, url=resp.url, body=content)
        return response_cls(
            url=resp.url,
            status=resp.status_code,
            headers=headers,
            body=content,
            request=request,
        )

    async def close(self) -> None:
        await self._fallback.close()

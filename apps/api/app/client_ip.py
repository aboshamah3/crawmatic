"""Where the API believes a request came from (EPA W5.5-L1 §10, item 1).

`X-Forwarded-For` is written by whoever sends the request. A limiter keyed on
a caller-controlled string is not a limiter: an attacker sets a different
value per attempt and every attempt lands in a fresh bucket. The header is
still the only way to see past a load balancer, so the rule is not "trust it"
or "ignore it" — it is **trust exactly as many hops as we actually have**.

    XFF:  <spoofable> , <spoofable> , ... , <appended by proxy N> , ... , <appended by proxy 1>
                                                                          ^-- rightmost

Each trusted proxy appends the address it received the connection from. So
with `TRUSTED_PROXY_HOPS = n`, the real client is the n-th entry counted from
the RIGHT — everything to the left of it was supplied by the caller and is
worth nothing. Reading the LEFTMOST entry, which is the common shape and the
one this codebase had, reads whatever the attacker typed.

**What this deployment actually looks like.** `apps/api/Dockerfile` runs
`uvicorn` with no `--proxy-headers` and no `--forwarded-allow-ips`, so
`request.client.host` is the TCP peer — Railway's edge, never the customer.
Before this module, `POST /v1/auth/login`'s per-source rate limit therefore
keyed every request on the internet to ONE bucket: the per-source half of
`check_and_increment_login` was, in production, a single global counter that
one busy tenant could exhaust for everyone. That is the bug this fixes, and
it is why the default is 1 rather than 0.

**Fallbacks are the socket, never a guess.** If the header is absent, or has
fewer entries than there are trusted hops (a request that did NOT come
through the expected chain — a direct hit on the container, a probe, a
misconfigured hop count), the socket address is used. Never the leftmost
entry, and never `"unknown"` while a real socket address exists: a shorter
chain than expected means the header is not evidence, so we fall back to the
one thing the caller cannot write.
"""

from __future__ import annotations

from typing import Any

#: How many proxies sit in front of this process and append to
#: `X-Forwarded-For`. Railway's edge is exactly one.
#:
#: PENDING OWNER REVIEW: 1 is the value that matches the deployment described
#: above. Set it to 0 for any deployment reached directly (no proxy at all),
#: which makes `X-Forwarded-For` ignored entirely. Setting it HIGHER than the
#: real chain length is the dangerous direction — it starts reading entries
#: the caller wrote — which is why the too-short-chain case below falls back
#: to the socket instead of taking what it can get.
TRUSTED_PROXY_HOPS = 1

# TODO(config): promote to `app_shared.config.Settings` as
# `TRUSTED_PROXY_HOPS`. A module constant only because `config.py` was held by
# a concurrent worker when this landed (EPA W5.5-L1).

_UNKNOWN = "unknown"


def _forwarded_entries(headers: Any) -> list[str]:
    """Every `X-Forwarded-For` entry, left to right, blanks dropped.

    **`getlist`, not `get`.** Starlette's `Headers.get` returns only the
    FIRST matching header line, and RFC 7230 lets a list-valued header be
    split across several lines. A caller who sends their own
    `X-Forwarded-For` line ahead of the one a proxy adds would otherwise
    have the whole chain read from *their* line — the exact bypass the hop
    count exists to close. Every line is concatenated in order, which is the
    same chain the header would have carried folded onto one line.
    """
    if headers is None:
        return []
    getlist = getattr(headers, "getlist", None)
    if callable(getlist):
        lines = list(getlist("x-forwarded-for"))
    else:  # pragma: no cover - plain-mapping headers in a caller's test double
        raw = headers.get("X-Forwarded-For")
        lines = [raw] if raw else []
    entries: list[str] = []
    for line in lines:
        entries.extend(part.strip() for part in str(line).split(",") if part.strip())
    return entries


def client_ip(request: Any, *, trusted_proxy_hops: int | None = None) -> str:
    """The address to attribute this request to, honouring the proxy chain.

    Returns the socket peer whenever the forwarded chain is absent or shorter
    than the trusted hop count — see the module docstring. Never raises: a
    request object without `.client` or without headers still yields a
    usable, non-empty string.
    """
    hops = TRUSTED_PROXY_HOPS if trusted_proxy_hops is None else trusted_proxy_hops

    client = getattr(request, "client", None)
    socket_host = getattr(client, "host", None) if client is not None else None
    fallback = socket_host or _UNKNOWN

    if hops <= 0:
        # No proxy is trusted, so the header is caller-controlled noise.
        return fallback

    entries = _forwarded_entries(getattr(request, "headers", None))
    if len(entries) < hops:
        # Fewer hops than expected: this request did not traverse the chain we
        # configured, so its header is not evidence of anything.
        return fallback

    return entries[-hops]

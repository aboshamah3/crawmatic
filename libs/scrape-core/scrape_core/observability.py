"""Structured JSON log helper (SPEC-11 US4, `contracts/observability.md`,
Constitution §31 "Structured JSON logs" / "per-domain rate-limit hits,
... requeue/overflow counts, and dedup skips").

No new logging framework or third-party dependency is introduced --
this wraps the stdlib ``logging`` module every other module in this
codebase already uses (mirrors the plain ``logger.warning(...)``/
``logger.error(...)`` convention in
``libs/shared/app_shared/limiter/{bucket,locks}.py`` and
``scrape_core.pipelines``), formatting the message body as one JSON
object instead of a ``%s``-interpolated string so the six
`contracts/observability.md` events (``rate_limit.hit``,
``rate_limit.requeue``, ``rate_limit.overflow``, ``semaphore.denied``,
``dedup.skip``, ``dedup.release``) are grep/parse-friendly per-line
JSON, namespaced by whichever of ``workspace_id``/``domain``/
``access_method`` the event carries. No external monitoring dependency
is required for MVP (Constitution §31) -- these are counted/alerted on
by parsing the emitted JSON log lines, not a separate metrics client.

:func:`log_event` never raises: a field that ``json.dumps`` cannot
serialize natively (``uuid.UUID``, a ``StrEnum`` member not already a
plain ``str`` instance, etc.) falls back to ``str()`` via ``default``,
and a wholly unexpected serialization failure still emits a plain
(non-JSON) fallback line rather than losing the log entirely or
raising into the caller (the reactor thread, in every real call site).
"""

from __future__ import annotations

import json
import logging
from typing import Any

__all__ = ["log_event"]


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    """Emit one structured JSON log line: ``{"event": event, **fields}``.

    Pure in-memory string formatting + a single ``logging.Logger.info``
    call -- exactly as blocking (i.e. not at all, from the reactor's
    perspective) as the existing ``logger.warning``/``logger.error``
    calls already made from async spider methods and Twisted callbacks
    elsewhere in this feature; stdlib ``logging`` handler I/O is the
    same either way, JSON or plain string.
    """
    try:
        payload = json.dumps({"event": event, **fields}, default=str)
    except Exception:  # noqa: BLE001 - never let logging crash the caller
        payload = f'{{"event": "{event}", "fields": "{fields!s}"}}'
    logger.info(payload)

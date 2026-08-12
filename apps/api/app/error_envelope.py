"""One error envelope for every failure mode (PLAN §7.4).

The engine grew three different error shapes:
`{"detail": {"error": {code, message}}}` (routers),
`{"detail": {code, message}}` (`app.errors` helpers), and FastAPI's own
`{"detail": [...]}` for request validation. A public API needs one.

These handlers are deliberately **additive**: they promote whichever
shape they find to a top-level `{"error": {"code", "message"}}` while
leaving `detail` byte-identical. Every existing test — and every
existing internal consumer — keeps reading `detail`; external customers
read `error` and never have to branch. Removing `detail` later is a
breaking change to be made on its own, with its own migration note.

Unhandled exceptions become `{"error": {"code": "INTERNAL_ERROR"}}` with
a fixed message: an exception string can carry a SQL fragment, a URL
with credentials, or a row of customer data, and this is a
customer-facing surface.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def _error_object(status_code: int, detail: Any) -> dict[str, str]:
    """Promote any of the three legacy detail shapes to `{code, message}`."""
    if isinstance(detail, dict):
        nested = detail.get("error")
        if isinstance(nested, dict) and "code" in nested:
            return {
                "code": str(nested.get("code")),
                "message": str(nested.get("message", "")),
            }
        if "code" in detail:
            return {
                "code": str(detail.get("code")),
                "message": str(detail.get("message", "")),
            }
    if isinstance(detail, str):
        return {"code": f"HTTP_{status_code}", "message": detail}
    return {"code": f"HTTP_{status_code}", "message": "Request failed."}


async def _http_exception_handler(
    request: Request, exc: HTTPException
) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": _error_object(exc.status_code, exc.detail),
            "detail": exc.detail,
        },
        headers=getattr(exc, "headers", None),
    )


async def _validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "The request body or parameters failed validation.",
            },
            "detail": _jsonable(exc.errors()),
        },
    )


def _jsonable(errors: Any) -> Any:
    """FastAPI validation errors can carry non-JSON `ctx` values."""
    from fastapi.encoders import jsonable_encoder

    return jsonable_encoder(errors)


async def _unhandled_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "An internal error occurred.",
            },
            "detail": "An internal error occurred.",
        },
    )


def register_error_handlers(app: FastAPI) -> None:
    """Install the envelope handlers. Call once, at app construction."""
    from starlette.exceptions import HTTPException as StarletteHTTPException

    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(HTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)

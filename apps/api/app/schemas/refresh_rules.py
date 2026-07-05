"""Refresh rule API DTOs (`contracts/refresh-rules-api.md`) — SPEC-13 US1.

Pydantic v2 request/response models for the `/v1/refresh-rules` router
(``apps/api/app/routers/refresh_rules.py``). Kept in ``apps/api`` (never
``app_shared``) so the framework-agnostic core never depends on
Pydantic — same discipline as ``app.schemas.competitors``/``matches``.

Cross-field validation (research R9) lives in two plain, framework-agnostic
functions below (:func:`validate_cadence` / :func:`validate_scope_target`),
mirroring the ``app_shared.profiles.validation.ProfileValidationError``
precedent rather than relying on FastAPI's own (differently-shaped) request
-validation error envelope: both raise :class:`RefreshRuleValidationError`
(a `ValueError` subclass carrying a `.code`), so a direct-construction unit
test (`tests/unit/test_refresh_rules_validation.py`, no DB/FastAPI) and the
CRUD router can both recover the exact `INVALID_CADENCE`/`INVALID_CRON`/
`SCOPE_TARGET_MISMATCH` code for the `{"error":{"code","message"}}` envelope.
`RefreshRuleCreate` also wires them into a `model_validator(mode="after")` so
constructing the model directly enforces the same invariants; the router
additionally re-runs them explicitly on `PATCH` (merged view) and
`POST` (parsed payload) so the structured envelope is always used, never
FastAPI's default `RequestValidationError` shape.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app_shared.enums import ScrapeScope
from app_shared.scheduling.cadence import validate_cron

# Reused, not rebuilt (contracts/refresh-rules-api.md) — the DELETE
# response shape is identical to the SPEC-04 catalog / SPEC-05 competitor
# delete outcome.
from app.schemas.catalog import DeleteOutcome  # noqa: F401 - re-exported for routers


class RefreshRuleValidationError(ValueError):
    """Cross-field refresh-rule validation failure carrying a structured error code.

    Mirrors :class:`app_shared.profiles.validation.ProfileValidationError` —
    a plain `ValueError` subclass with a `.code` attribute so callers (unit
    tests, the CRUD router) can recover the exact contract error code
    (`INVALID_CADENCE`/`INVALID_CRON`/`SCOPE_TARGET_MISMATCH`) without
    depending on FastAPI/Pydantic's own error shape.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# Which single target-id field each non-WORKSPACE scope requires
# (data-model.md `ck_refresh_rules_scope_target`; research R6/R9).
_SCOPE_TARGET_FIELDS: dict[ScrapeScope, str] = {
    ScrapeScope.COMPETITOR: "competitor_id",
    ScrapeScope.PRODUCT: "product_id",
    ScrapeScope.VARIANT: "product_variant_id",
    ScrapeScope.PRODUCT_GROUP: "product_group_id",
    ScrapeScope.MATCH: "match_id",
}


def validate_cadence(cron_expression: str | None, interval_minutes: int | None) -> None:
    """Enforce "exactly one of cron_expression/interval_minutes" + cron parseability.

    Raises :class:`RefreshRuleValidationError` with code `INVALID_CADENCE`
    (neither/both supplied) or `INVALID_CRON` (an unparseable cron string) —
    mirrors the DB CHECK `ck_refresh_rules_exactly_one_cadence` (research R9).
    """
    if (cron_expression is None) == (interval_minutes is None):
        raise RefreshRuleValidationError(
            "INVALID_CADENCE",
            "exactly one of cron_expression/interval_minutes must be supplied",
        )
    if cron_expression is not None:
        try:
            validate_cron(cron_expression)
        except ValueError as exc:
            raise RefreshRuleValidationError("INVALID_CRON", str(exc)) from exc


def validate_scope_target(
    scope: ScrapeScope,
    *,
    product_id: uuid.UUID | None,
    product_variant_id: uuid.UUID | None,
    product_group_id: uuid.UUID | None,
    competitor_id: uuid.UUID | None,
    match_id: uuid.UUID | None,
) -> None:
    """Enforce the scope<->target-id matrix (data-model.md `ck_refresh_rules_scope_target`).

    WORKSPACE ⇒ every target id must be `None`; every other scope ⇒ exactly
    its own target id is non-`None` and the remaining four are `None`.
    Raises :class:`RefreshRuleValidationError` with code
    `SCOPE_TARGET_MISMATCH` on any violation.
    """
    targets = {
        "product_id": product_id,
        "product_variant_id": product_variant_id,
        "product_group_id": product_group_id,
        "competitor_id": competitor_id,
        "match_id": match_id,
    }

    if scope == ScrapeScope.WORKSPACE:
        if any(value is not None for value in targets.values()):
            raise RefreshRuleValidationError(
                "SCOPE_TARGET_MISMATCH",
                "scope=WORKSPACE requires every target id to be null",
            )
        return

    required_field = _SCOPE_TARGET_FIELDS[scope]
    for field_name, value in targets.items():
        if field_name == required_field:
            if value is None:
                raise RefreshRuleValidationError(
                    "SCOPE_TARGET_MISMATCH",
                    f"scope={scope.value} requires {required_field} to be set",
                )
        elif value is not None:
            raise RefreshRuleValidationError(
                "SCOPE_TARGET_MISMATCH",
                f"scope={scope.value} forbids {field_name} to be set",
            )


class RefreshRuleCreate(BaseModel):
    """`POST /v1/refresh-rules` request body.

    `name`/`scope` are required; exactly one of `cron_expression`/
    `interval_minutes` must be supplied (`INVALID_CADENCE`/`INVALID_CRON`),
    and the scope<->target-id matrix must hold (`SCOPE_TARGET_MISMATCH`) —
    both re-checked by the router after confirming the supplied target id
    resolves in-workspace (contract "the supplied target id must resolve
    in-workspace via `scoped_get`").
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    scope: ScrapeScope
    product_id: uuid.UUID | None = None
    product_variant_id: uuid.UUID | None = None
    product_group_id: uuid.UUID | None = None
    competitor_id: uuid.UUID | None = None
    match_id: uuid.UUID | None = None
    cron_expression: str | None = None
    interval_minutes: int | None = Field(default=None, ge=1)
    priority: int = 0
    enabled: bool = True

    @model_validator(mode="after")
    def _check_cadence_and_scope(self) -> "RefreshRuleCreate":
        validate_cadence(self.cron_expression, self.interval_minutes)
        validate_scope_target(
            self.scope,
            product_id=self.product_id,
            product_variant_id=self.product_variant_id,
            product_group_id=self.product_group_id,
            competitor_id=self.competitor_id,
            match_id=self.match_id,
        )
        return self


class RefreshRuleUpdate(BaseModel):
    """`PATCH /v1/refresh-rules/{id}` — every field optional (partial update).

    An empty body (no fields set) is rejected by the router with
    `422 EMPTY_UPDATE` (`strategy.py` precedent) before this schema's
    values are ever applied. The router re-validates cadence/scope on the
    **merged** (existing row + this patch) view — not this schema alone,
    since e.g. patching only `name` must not require re-supplying cadence.
    Includes `enabled` — enable/disable is a PATCH field, not a separate
    action subresource (contract).
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    scope: ScrapeScope | None = None
    product_id: uuid.UUID | None = None
    product_variant_id: uuid.UUID | None = None
    product_group_id: uuid.UUID | None = None
    competitor_id: uuid.UUID | None = None
    match_id: uuid.UUID | None = None
    cron_expression: str | None = None
    interval_minutes: int | None = Field(default=None, ge=1)
    priority: int | None = None
    enabled: bool | None = None


class RefreshRuleResponse(BaseModel):
    """A `refresh_rules` row as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    scope: ScrapeScope
    product_id: uuid.UUID | None
    product_variant_id: uuid.UUID | None
    product_group_id: uuid.UUID | None
    competitor_id: uuid.UUID | None
    match_id: uuid.UUID | None
    cron_expression: str | None
    interval_minutes: int | None
    priority: int
    enabled: bool
    next_run_at: datetime | None
    last_run_at: datetime | None
    locked_at: datetime | None
    created_at: datetime
    updated_at: datetime


class RefreshRuleListResponse(BaseModel):
    """`{items, next_cursor}` envelope for `GET /v1/refresh-rules`."""

    items: list[RefreshRuleResponse]
    next_cursor: str | None

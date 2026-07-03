"""Workspace-scoped repository helpers (FR-018, Principle II).

The **sanctioned** way to query workspace-owned models
(:class:`~app_shared.models.identity.User`,
:class:`~app_shared.models.identity.ApiKey`). Framework-agnostic
(SQLAlchemy only — no FastAPI). This is the module
``scripts/check_workspace_scoping.py`` path-allowlists, since it
legitimately constructs scoped selects generically without a literal
``workspace_id`` predicate visible at every call site.

Application-layer defense-in-depth: combined with DB-level RLS
(:func:`app_shared.models.rls.emit_rls_policy` +
:func:`app_shared.database.set_workspace_context`), this is the
two-layer isolation model (app filter + DB RLS) — either layer alone
would fail closed to zero rows on a bug in the other, never leak rows.
"""

from __future__ import annotations

import uuid
from typing import Any, TypeVar

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app_shared.models.base import Base
from app_shared.models.catalog import Product, ProductGroup, ProductGroupItem, ProductVariant
from app_shared.models.competitors_matches import Competitor, CompetitorProductMatch
from app_shared.models.identity import ApiKey, User
from app_shared.models.observations import MatchCurrentPrice, PriceObservation, RequestAttempt

# Widened (SPEC-04 research D9) from a closed TypeVar over the two
# SPEC-03 models to any Base subclass — the four catalog models
# (Product/ProductVariant/ProductGroup/ProductGroupItem) and the two
# SPEC-05 competitor/match models reuse these helpers unchanged.
ModelT = TypeVar("ModelT", bound=Base)

# Single source of truth for "which ORM models are workspace-owned" — the
# CI guard (scripts/check_workspace_scoping.py) imports this exact set so
# the guarded set and the runtime set never drift (FR-018/FR-020).
WORKSPACE_OWNED_MODELS: frozenset[type] = frozenset(
    {
        User,
        ApiKey,
        Product,
        ProductVariant,
        ProductGroup,
        ProductGroupItem,
        Competitor,
        CompetitorProductMatch,
        PriceObservation,
        RequestAttempt,
        MatchCurrentPrice,
    }
)


def assert_workspace_owned_query_is_scoped(model: type, workspace_id: Any) -> None:
    """Raise ``ValueError`` if ``model`` is workspace-owned and ``workspace_id`` is missing.

    Called by :func:`scoped_select`/:func:`scoped_get` before constructing
    a query, so a caller can never accidentally obtain an unscoped
    ``Select``/fetch for a workspace-owned model through these helpers.
    """
    if model in WORKSPACE_OWNED_MODELS and not workspace_id:
        raise ValueError(
            f"{model.__name__} is workspace-owned (in WORKSPACE_OWNED_MODELS); "
            "a non-empty workspace_id is required to query it via the "
            "app_shared.repository helpers."
        )


def scoped_select(model: type[ModelT], workspace_id: uuid.UUID | str) -> Select[Any]:
    """Return ``select(model).where(model.workspace_id == workspace_id)``.

    A ``workspace_id`` predicate is **always** present in the returned
    query — this is the sanctioned alternative to a bare ``select(User)``/
    ``select(ApiKey)`` (FR-018).
    """
    assert_workspace_owned_query_is_scoped(model, workspace_id)
    return select(model).where(model.workspace_id == workspace_id)


def scoped_get(
    session: Session,
    model: type[ModelT],
    id_: uuid.UUID | str,
    workspace_id: uuid.UUID | str,
) -> ModelT | None:
    """Fetch a single row by BOTH ``id`` and ``workspace_id`` — never ``session.get(model, id_)`` alone.

    Raises ``ValueError`` (via :func:`assert_workspace_owned_query_is_scoped`)
    if ``workspace_id`` is missing/None/empty for a workspace-owned model.
    """
    assert_workspace_owned_query_is_scoped(model, workspace_id)
    stmt = select(model).where(model.id == id_, model.workspace_id == workspace_id)
    return session.execute(stmt).scalar_one_or_none()

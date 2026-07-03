"""Workspace-scoped repository helper tests (SPEC-03 T041, FR-018).

`app_shared.repository` — the sanctioned way to query workspace-owned
models. Pure SQLAlchemy expression-compilation assertions (`scoped_select`)
plus a raises-on-missing-workspace_id check (`scoped_get`) — no database
connection required.
"""

from __future__ import annotations

import uuid

import pytest

from app_shared.models.catalog import Product, ProductGroup, ProductGroupItem, ProductVariant
from app_shared.models.competitors_matches import Competitor, CompetitorProductMatch
from app_shared.models.identity import ApiKey, User
from app_shared.models.observations import MatchCurrentPrice, PriceObservation, RequestAttempt
from app_shared.repository import (
    WORKSPACE_OWNED_MODELS,
    assert_workspace_owned_query_is_scoped,
    scoped_get,
    scoped_select,
)


def test_workspace_owned_models_is_exactly_user_and_api_key() -> None:
    # SPEC-04 (T006) widened this set with the four catalog models; SPEC-05
    # (T005) widened it further with Competitor/CompetitorProductMatch;
    # SPEC-07 (T009) widens it again with the three observation/current-
    # price models — updated here alongside app_shared.repository so the
    # suite stays in sync with the runtime set.
    assert WORKSPACE_OWNED_MODELS == frozenset(
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


def test_scoped_select_renders_a_workspace_id_where_clause() -> None:
    workspace_id = uuid.uuid4()
    stmt = scoped_select(User, workspace_id)
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": False}))
    assert "WHERE" in compiled
    assert "workspace_id" in compiled


def test_scoped_select_on_api_key_also_scopes() -> None:
    workspace_id = uuid.uuid4()
    stmt = scoped_select(ApiKey, workspace_id)
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": False}))
    assert "WHERE" in compiled
    assert "workspace_id" in compiled


@pytest.mark.parametrize("bad_workspace_id", [None, "", ])
def test_scoped_select_raises_when_workspace_id_missing(bad_workspace_id) -> None:
    with pytest.raises(ValueError):
        scoped_select(User, bad_workspace_id)


@pytest.mark.parametrize("bad_workspace_id", [None, ""])
def test_scoped_get_raises_when_workspace_id_missing(bad_workspace_id) -> None:
    with pytest.raises(ValueError):
        scoped_get(session=None, model=User, id_=uuid.uuid4(), workspace_id=bad_workspace_id)


def test_assert_workspace_owned_query_is_scoped_passes_with_a_workspace_id() -> None:
    # Should not raise.
    assert_workspace_owned_query_is_scoped(User, uuid.uuid4())
    assert_workspace_owned_query_is_scoped(ApiKey, uuid.uuid4())


def test_assert_workspace_owned_query_is_scoped_ignores_non_owned_models() -> None:
    """A model NOT in WORKSPACE_OWNED_MODELS (e.g. Workspace itself) never raises."""
    from app_shared.models.identity import Workspace

    # Should not raise even with no workspace_id — Workspace is the tenant root.
    assert_workspace_owned_query_is_scoped(Workspace, None)

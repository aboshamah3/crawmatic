"""String-backed, application-validated enumerations.

Per ``contracts/enums.md`` / data-model.md "Entity: Core enum support"
(§22): enum-like values are stored as plain string columns and validated
in the application — **never** a Postgres-native ``ENUM`` type, and (per
the [analyze A2] decision) never SQLAlchemy's ``Enum`` type either, so
rejection of out-of-set values is deterministically an application-layer
concern rather than a DB `CHECK` constraint.

``enum_column`` renders to a plain ``String`` column at the DDL level
(same mechanism the ``Money`` type in ``app_shared.money`` uses for
``NUMERIC``): a ``TypeDecorator`` whose ``impl`` is ``sqlalchemy.String``
does the coerce/validate work in ``process_bind_param`` /
``process_result_value``, but Postgres sees (and Alembic renders) an
ordinary ``VARCHAR`` column.
"""

from __future__ import annotations

import enum
from typing import Any

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

DEFAULT_ENUM_COLUMN_LENGTH = 32


class StrEnum(str, enum.Enum):
    """Base class for string-backed, application-validated enumerations.

    Members compare/hash/serialize as their string ``value``
    (inherits ``str``), so ``RecordStatus.ACTIVE == "active"`` and
    ``str(RecordStatus.ACTIVE) == "active"``.
    """

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)


class RecordStatus(StrEnum):
    """Minimal core enum used as a shared building block (and by the demo table)."""

    ACTIVE = "active"
    ARCHIVED = "archived"


class WorkspaceStatus(StrEnum):
    """Lifecycle status of a ``workspaces`` row (SPEC-03 FR-022)."""

    ACTIVE = "active"
    SUSPENDED = "suspended"


class UserRole(StrEnum):
    """Authorization role of a ``users`` row (SPEC-03 FR-003, §33)."""

    SUPER_ADMIN = "super_admin"
    WORKSPACE_ADMIN = "workspace_admin"
    READ_ONLY = "read_only"


class UserStatus(StrEnum):
    """Lifecycle status of a ``users`` row (SPEC-03 FR-022)."""

    ACTIVE = "active"
    SUSPENDED = "suspended"


class ApiKeyStatus(StrEnum):
    """Lifecycle status of an ``api_keys`` row (SPEC-03 FR-014)."""

    ACTIVE = "active"
    REVOKED = "revoked"


class ProductStatus(StrEnum):
    """Lifecycle status of a ``products`` row (SPEC-04 FR-017)."""

    ACTIVE = "active"
    ARCHIVED = "archived"


class VariantStatus(StrEnum):
    """Lifecycle status of a ``product_variants`` row (SPEC-04 FR-017)."""

    ACTIVE = "active"
    ARCHIVED = "archived"


class GroupStatus(StrEnum):
    """Lifecycle status of a ``product_groups`` row (SPEC-04 FR-017)."""

    ACTIVE = "active"
    ARCHIVED = "archived"


class LegalStatus(StrEnum):
    """Legal review status of a ``competitors`` row (SPEC-05 §22, Principle VI).

    Competitors default to ``REVIEW_REQUIRED`` per Constitution Principle VI.
    """

    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    APPROVED = "APPROVED"
    DISABLED = "DISABLED"


class RobotsPolicy(StrEnum):
    """robots.txt handling policy of a ``competitors`` row (SPEC-05 §22)."""

    RESPECT = "RESPECT"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    IGNORE_AFTER_APPROVAL = "IGNORE_AFTER_APPROVAL"


class CompetitorStatus(StrEnum):
    """Lifecycle status of a ``competitors`` row (SPEC-05 FR-016)."""

    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class MatchPriority(StrEnum):
    """Scrape priority of a ``competitor_product_matches`` row (SPEC-05 §22)."""

    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class MatchStatus(StrEnum):
    """Lifecycle status of a ``competitor_product_matches`` row (SPEC-05 FR-016)."""

    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    FAILED = "FAILED"
    ARCHIVED = "ARCHIVED"


class HealthStatus(StrEnum):
    """Scrape health status of a ``competitor_product_matches`` row (SPEC-05 FR-017)."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILING = "FAILING"
    UNKNOWN = "UNKNOWN"


class ScrapeProfileMode(StrEnum):
    """Extraction transport mode of a ``scrape_profiles`` row (SPEC-06 §22, FR-001)."""

    HTTP = "HTTP"
    BROWSER = "BROWSER"
    CUSTOM = "CUSTOM"


class AdapterKey(StrEnum):
    """Extraction adapter of a ``scrape_profiles`` row (SPEC-06 §22, FR-001)."""

    DEFAULT_HTTP = "default_http"
    JSONLD_FIRST = "jsonld_first"
    SELECTOR_ONLY = "selector_only"
    REGEX_ONLY = "regex_only"
    SHOPIFY_PRODUCT_JSON = "shopify_product_json"
    WOOCOMMERCE_STORE_API = "woocommerce_store_api"
    PLAYWRIGHT_RENDERED = "playwright_rendered"
    CUSTOM_ADAPTER = "custom_adapter"


class VariantStrategy(StrEnum):
    """Variant-resolution strategy of a ``scrape_profiles`` row (SPEC-06 §22, FR-001)."""

    PAGE_SINGLE_PRICE = "PAGE_SINGLE_PRICE"
    URL_HAS_VARIANT_SELECTED = "URL_HAS_VARIANT_SELECTED"
    HTML_VARIANT_TABLE = "HTML_VARIANT_TABLE"
    EMBEDDED_JSON_VARIANTS = "EMBEDDED_JSON_VARIANTS"
    SELECT_VARIANT_WITH_PLAYWRIGHT = "SELECT_VARIANT_WITH_PLAYWRIGHT"
    CUSTOM_VARIANT_ADAPTER = "CUSTOM_VARIANT_ADAPTER"


class _AppValidatedEnumString(TypeDecorator[Any]):
    """Plain ``String`` column with application-side enum validation.

    Never a Postgres-native ``ENUM`` and never ``sqlalchemy.Enum`` —
    the DDL rendered by ``impl`` is an ordinary ``VARCHAR(length)``.
    Membership is coerced/validated against ``enum_type`` at bind time
    (write) and result time (read); an out-of-set value raises
    ``ValueError`` rather than silently passing through or being
    enforced by a DB-level `CHECK`.
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_type: type[StrEnum], *args: Any, **kwargs: Any) -> None:
        self._enum_type = enum_type
        super().__init__(*args, **kwargs)

    def _coerce(self, value: Any) -> StrEnum:
        if isinstance(value, self._enum_type):
            return value
        try:
            return self._enum_type(value)
        except ValueError as exc:
            valid = ", ".join(member.value for member in self._enum_type)
            raise ValueError(
                f"{value!r} is not a valid {self._enum_type.__name__} value "
                f"(expected one of: {valid})"
            ) from exc

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        return self._coerce(value).value

    def process_result_value(self, value: Any, dialect: Any) -> StrEnum | None:
        if value is None:
            return None
        return self._coerce(value)


def enum_column(
    enum_type: type[StrEnum], *, length: int = DEFAULT_ENUM_COLUMN_LENGTH, **kw: Any
) -> Mapped[Any]:
    """Column factory mapping ``enum_type`` to a plain, app-validated ``String`` column.

    ``length`` sizes the underlying ``VARCHAR``; any remaining keyword
    arguments (``nullable``, ``default``, ``index``, ...) pass straight
    through to ``mapped_column``.
    """
    return mapped_column(_AppValidatedEnumString(enum_type, length=length), **kw)

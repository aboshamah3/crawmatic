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


class AccessMethod(StrEnum):
    """Transport method used for a fetch attempt (SPEC-07 §11/§22).

    This slice (``generic_price_spider``) writes only ``DIRECT_HTTP`` —
    the other members are forward-compat for proxy/browser transports
    added by later specs.
    """

    DIRECT_HTTP = "DIRECT_HTTP"
    DIRECT_HTTP_RETRY = "DIRECT_HTTP_RETRY"
    PROXY_HTTP = "PROXY_HTTP"
    PLAYWRIGHT_PROXY = "PLAYWRIGHT_PROXY"


class StockStatus(StrEnum):
    """Stock/availability signal extracted alongside a price (SPEC-07 §22)."""

    IN_STOCK = "IN_STOCK"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    UNKNOWN = "UNKNOWN"


class ExtractionMethod(StrEnum):
    """Strategy that produced a price candidate (SPEC-07 §22, contracts/extraction.md).

    This slice writes ``JSON_LD``/``CSS``/``REGEX``/``SINGLE_NUMBER``; the
    remaining members are forward-compat for later extraction strategies
    (platform JSON APIs, embedded JSON blobs, XPath, Playwright-rendered
    pages) so the column never needs a widening migration.
    """

    JSON_LD = "JSON_LD"
    CSS = "CSS"
    REGEX = "REGEX"
    SINGLE_NUMBER = "SINGLE_NUMBER"
    PLATFORM_JSON = "PLATFORM_JSON"
    EMBEDDED_JSON = "EMBEDDED_JSON"
    XPATH = "XPATH"
    PLAYWRIGHT = "PLAYWRIGHT"


class ScrapeErrorCode(StrEnum):
    """Structured error-code vocabulary (Constitution §34, contracts/errors.md).

    Shared by ``price_observations``/``request_attempts``/
    ``match_current_prices.error_code`` and by the scraping-side
    fetch-failure classification helpers, so debugging, the later
    strategy optimizer, access-policy tuning, and client reporting
    share one language. This slice (SPEC-07) emits the first eleven
    members; the rest are forward-compat placeholders for proxies,
    Playwright, the strategy optimizer, and rate-limiting/legal-review
    features owned by later specs — declared now so those specs never
    need a widening migration on this column.
    """

    # --- Emitted by this slice (contracts/errors.md) ---
    HTTP_403 = "HTTP_403"
    HTTP_404 = "HTTP_404"
    HTTP_429 = "HTTP_429"
    TIMEOUT = "TIMEOUT"
    DNS_ERROR = "DNS_ERROR"
    PRICE_NOT_FOUND = "PRICE_NOT_FOUND"
    INVALID_PRICE_FORMAT = "INVALID_PRICE_FORMAT"
    LOW_CONFIDENCE_PRICE = "LOW_CONFIDENCE_PRICE"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    BLOCKED = "BLOCKED"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"

    # --- Forward-compat (not exercised by this slice) ---
    VARIANT_NOT_FOUND = "VARIANT_NOT_FOUND"
    CURRENCY_NOT_FOUND = "CURRENCY_NOT_FOUND"
    STOCK_NOT_FOUND = "STOCK_NOT_FOUND"
    PROXY_FAILED = "PROXY_FAILED"
    PLAYWRIGHT_FAILED = "PLAYWRIGHT_FAILED"
    SELECTOR_BROKEN = "SELECTOR_BROKEN"
    STRATEGY_DEGRADED = "STRATEGY_DEGRADED"
    RATE_LIMITED = "RATE_LIMITED"
    LOCKED_ALREADY_RUNNING = "LOCKED_ALREADY_RUNNING"
    LIMIT_REACHED = "LIMIT_REACHED"
    LEGAL_REVIEW_REQUIRED = "LEGAL_REVIEW_REQUIRED"


class ScrapeScope(StrEnum):
    """Refresh/job scope of a ``scrape_jobs`` row (SPEC-08 §22 "Refresh scopes").

    Shared with ``refresh_rules`` in a later spec. This spec's endpoints
    (run-match / run-variant) produce only ``MATCH`` and ``VARIANT``; the
    remaining members are forward-compat for later scope-run endpoints.
    """

    WORKSPACE = "WORKSPACE"
    COMPETITOR = "COMPETITOR"
    PRODUCT = "PRODUCT"
    VARIANT = "VARIANT"
    PRODUCT_GROUP = "PRODUCT_GROUP"
    MATCH = "MATCH"


class ScrapeJobType(StrEnum):
    """Trigger-type provenance of a ``scrape_jobs`` row (SPEC-08 §22).

    Direct API runs (this spec) record ``MANUAL``; the remaining members
    are forward-compat for the scheduler/retry/discovery features owned
    by later specs.
    """

    MANUAL = "MANUAL"
    SCHEDULED = "SCHEDULED"
    API_TRIGGERED = "API_TRIGGERED"
    RETRY_FAILED = "RETRY_FAILED"
    DISCOVERY = "DISCOVERY"


class ScrapeJobStatus(StrEnum):
    """Lifecycle status of a ``scrape_jobs`` row (SPEC-08 §22, D6).

    ``PENDING`` at creation -> ``RUNNING`` (dispatch begins) -> a
    deterministic terminal status (``COMPLETED``/``PARTIAL_FAILED``/
    ``FAILED``). ``CANCELLED`` is a vocabulary member but not produced by
    this spec's endpoints.
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL_FAILED = "PARTIAL_FAILED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ScrapeJobSource(StrEnum):
    """Trigger-source provenance of a ``scrape_jobs`` row (SPEC-08 §22).

    Direct API runs (this spec) record ``API``; the remaining members are
    forward-compat for the scheduler/internal/plugin triggers owned by
    later specs.
    """

    API = "API"
    SCHEDULER = "SCHEDULER"
    INTERNAL = "INTERNAL"
    PLUGIN = "PLUGIN"


class ScrapeTargetStatus(StrEnum):
    """Lifecycle status of a ``scrape_job_targets`` row (SPEC-08 §22).

    ``PENDING`` at creation -> ``STARTED`` -> a terminal status
    (``COMPLETED``/``FAILED``/``SKIPPED``). ``mark_target``
    (``app_shared.jobs.targets``) is the single writer of these
    transitions.

    ``DEFERRED`` (SPEC-11 FR-018, data-model.md §2.1) is a distinct,
    **non-terminal** overflow outcome: an in-spider requeue-cap
    overflow hands the target back to Celery ``scrape_dispatch`` for
    later re-dispatch (``DEFERRED -> STARTED`` on re-pickup), so it is
    deliberately excluded from any terminal-status set.
    """

    PENDING = "PENDING"
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    DEFERRED = "DEFERRED"


class AlertType(StrEnum):
    """Deterministic alert classification of a variant's price position (SPEC-09 §23, FR-004).

    Produced by the pure ``app_shared.alerts.engine`` ordered decision
    tree; ``NO_COMPETITOR_DATA`` when there is no comparable competitor
    price, ``RISK``/``HIGH_PRICE`` when the client price is above the
    highest/cheapest competitor, ``CHANCE_TO_INCREASE_PRICE`` when the
    client price is more than 5% below the competitor average,
    ``NORMAL`` within the 1%-5% band, ``CLOSE_TO_COMPETITORS`` under 1%.
    """

    NO_COMPETITOR_DATA = "NO_COMPETITOR_DATA"
    RISK = "RISK"
    HIGH_PRICE = "HIGH_PRICE"
    CHANCE_TO_INCREASE_PRICE = "CHANCE_TO_INCREASE_PRICE"
    NORMAL = "NORMAL"
    CLOSE_TO_COMPETITORS = "CLOSE_TO_COMPETITORS"


class AlertSeverity(StrEnum):
    """Severity derived solely from ``AlertType`` via the fixed map (SPEC-09 FR-011)."""

    NONE = "NONE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class AlertStatus(StrEnum):
    """Lifecycle status of a ``variant_alert_states`` row (SPEC-09 §22).

    ``ACTIVE`` while the current type is non-``NORMAL``; ``RESOLVED``
    (with ``resolved_at`` stamped) once the type returns to ``NORMAL``.
    """

    ACTIVE = "ACTIVE"
    RESOLVED = "RESOLVED"


class AlertEventType(StrEnum):
    """Ordered alert-transition classification of a ``price_alert_events`` row (SPEC-09 D5).

    ``UNCHANGED`` is part of the vocabulary but is never persisted — a
    row is written only on a type/severity change (CREATED/UPDATED/
    RESOLVED/REOPENED).
    """

    CREATED = "CREATED"
    UPDATED = "UPDATED"
    RESOLVED = "RESOLVED"
    REOPENED = "REOPENED"
    UNCHANGED = "UNCHANGED"


class AccessStrategy(StrEnum):
    """Named access strategy of an ``access_policies`` row (SPEC-10 §22, FR-001).

    Consulted by the pure ``app_shared.access.engine`` to decide the next
    ``AccessMethod`` for a fetch attempt: ``DIRECT_ONLY`` never proxies;
    ``DIRECT_THEN_PROXY`` retries via proxy after a failed direct attempt;
    ``PROXY_FIRST``/``RESIDENTIAL_ONLY`` proxy from the first attempt (the
    latter restricted to ``ProxyType.RESIDENTIAL`` providers);
    ``BROWSER_FALLBACK`` signals ``PLAYWRIGHT_PROXY`` intent only (SPEC-14
    executes it).
    """

    DIRECT_ONLY = "DIRECT_ONLY"
    DIRECT_THEN_PROXY = "DIRECT_THEN_PROXY"
    PROXY_FIRST = "PROXY_FIRST"
    RESIDENTIAL_ONLY = "RESIDENTIAL_ONLY"
    BROWSER_FALLBACK = "BROWSER_FALLBACK"


class ProxyType(StrEnum):
    """Exit-node class of a ``proxy_providers`` row (SPEC-10 §22, FR-002)."""

    DATACENTER = "DATACENTER"
    RESIDENTIAL = "RESIDENTIAL"
    MOBILE = "MOBILE"


class ProxyProviderStatus(StrEnum):
    """Lifecycle status of a ``proxy_providers`` row (SPEC-10 §22, FR-002).

    A ``DISABLED`` provider is excluded from ``assign_proxy`` candidate
    selection — callers degrade per strategy rather than crash.
    """

    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class StrategyStatus(StrEnum):
    """Lifecycle status of a ``domain_strategy_profiles`` row (SPEC-12 §22, FR-007).

    ``DISCOVERY_REQUIRED`` (new key, no learned start yet) → ``LEARNING``
    (discovery seeded a winner or a promotion attempt is in progress) →
    ``ACTIVE`` (3-confirmation promotion rule satisfied) → ``DEGRADED``
    (rediscovery triggered, FR-020) → back to ``LEARNING``/``ACTIVE`` via
    a fresh discovery run. ``DISABLED`` is operator-set only: the learned
    preference is never applied and there is no automatic transition out
    (FR-014).
    """

    DISCOVERY_REQUIRED = "DISCOVERY_REQUIRED"
    LEARNING = "LEARNING"
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    DISABLED = "DISABLED"


class MethodType(StrEnum):
    """Disambiguates the ``method_name`` vocabulary on a ``strategy_attempt_stats``
    row / a ``domain_strategy_profiles`` preferred-method pair (SPEC-12 §22, FR-008).

    ``method_name`` values are the **reused** ``AccessMethod`` members
    when ``method_type=ACCESS`` and the **reused** ``ExtractionMethod``
    members when ``method_type=EXTRACTION`` — this enum is not itself a
    method vocabulary, just the discriminator (research D1).
    """

    ACCESS = "ACCESS"
    EXTRACTION = "EXTRACTION"


def validate_method_name(method_type: "MethodType", method_name: str) -> str:
    """Validate ``method_name`` against the vocabulary ``method_type`` selects (D1).

    ``strategy_attempt_stats.method_name`` (and the profile's
    ``preferred_access_method``/``preferred_extraction_method``) is a
    plain ``Text``/``String`` column, never ``enum_column`` — one
    column can't natively carry two disjoint enum types at once. This
    is the application-layer gate: an ``AccessMethod`` value is only
    valid when ``method_type=ACCESS``; an ``ExtractionMethod`` value
    only when ``method_type=EXTRACTION``. Returns the validated
    ``.value`` string; raises ``ValueError`` on a value that's well-
    formed for the *other* type, or not a member of either.
    """
    vocabulary: type[StrEnum] = AccessMethod if method_type == MethodType.ACCESS else ExtractionMethod
    try:
        return vocabulary(method_name).value
    except ValueError as exc:
        valid = ", ".join(member.value for member in vocabulary)
        raise ValueError(
            f"{method_name!r} is not a valid method_name for method_type="
            f"{method_type.value} (expected one of: {valid})"
        ) from exc


class DiscoveryRunStatus(StrEnum):
    """Lifecycle status of a ``strategy_discovery_runs`` row (SPEC-12 US3 AS1/AS4).

    ``PENDING`` (enqueued, not yet picked up) → ``RUNNING`` (sample is
    being probed) → ``COMPLETED`` (a winning access + extraction method
    pair was found, ``winning_*``/``completed_at`` set) or ``NO_WINNER``
    (no combination cleared the promotion-quality bar, ``completed_at``
    set, ``winning_*`` stay ``NULL``) or ``FAILED`` (an out-of-bounds
    ``sample_size`` or an unexpected error aborted the run before/
    during probing).
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    NO_WINNER = "NO_WINNER"
    FAILED = "FAILED"


class WebhookEventStatus(StrEnum):
    """Delivery status of a ``webhook_events`` row (SPEC-16 §22, FR-011).

    v1 only ever writes ``PENDING`` (recorded, not delivered;
    ``delivered_at`` stays null). ``DELIVERED``/``FAILED`` are reserved
    for the future delivery feature and are unused in v1.
    """

    PENDING = "PENDING"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"


class WebhookEventType(StrEnum):
    """Stable event-type taxonomy for ``webhook_events.event_type`` (SPEC-16 §22, FR-008).

    Maps existing source-domain enum transitions to a stable dotted
    string vocabulary: price alerts (SPEC-09 ``AlertEventType``), scrape
    job terminal statuses (SPEC-08 ``ScrapeJobStatus``), and domain
    strategy status changes (SPEC-12 ``StrategyStatus`` ACTIVE/DEGRADED).
    Stored in a ``String(64)`` column (free string, producer-validated
    by this enum); endpoint ``event_types`` subscriptions remain a free
    JSONB list of strings, forward-compatible with unknown types.
    """

    PRICE_ALERT_CREATED = "price.alert.created"
    PRICE_ALERT_UPDATED = "price.alert.updated"
    PRICE_ALERT_RESOLVED = "price.alert.resolved"
    PRICE_ALERT_REOPENED = "price.alert.reopened"
    SCRAPE_JOB_COMPLETED = "scrape.job.completed"
    SCRAPE_JOB_PARTIAL = "scrape.job.partial_failed"
    SCRAPE_JOB_FAILED = "scrape.job.failed"
    DOMAIN_STRATEGY_UPDATED = "domain.strategy.updated"


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

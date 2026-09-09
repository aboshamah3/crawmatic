"""The controlled-origin fixture service (EPA D1, F22, audit §13).

`apps/fixture-origin` is deliberately NOT a uv-workspace member (root
`pyproject.toml` excludes it, the `apps/dr-backup` precedent) so it
cannot import `app_shared` or share a dependency set with the engine it
is used to measure. It is therefore loaded here the way this suite
already loads other non-importable files — `importlib.util
.spec_from_file_location`, as in `test_migrate_stech_identifiers.py`.

The properties under test are the ones the fleet test's validity rests
on:

* **no external calls** — asserted structurally, from the module's own
  AST, so a later `import httpx` fails the unit gate instead of quietly
  turning the fixture origin into an egress path;
* **deterministic** — the same path always produces the same price and
  the same behaviour, because a benchmark whose failure set moves
  between runs cannot answer "did we get better?";
* **generated from the path** — 500,000 SKUs exist with no data file;
* the behaviour model itself (ok / slow / error / redirect / blocked /
  not listed) actually reaches the wire with the right status codes.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "apps" / "fixture-origin" / "app" / "main.py"


def _load(name: str = "_fixture_origin_main"):
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def origin():
    module = _load()
    # Never sleep in the unit gate: the latency model is asserted
    # through `latency_seconds` directly, and a 1.2 s "slow" store would
    # otherwise add minutes to the suite.
    module.LATENCY_SCALE = 0.0
    return module


@pytest.fixture(scope="module")
def client(origin):
    from fastapi.testclient import TestClient

    with TestClient(origin.app) as test_client:
        yield test_client


def _find_sku(origin, store: str, behaviour: str, limit: int = 20_000) -> str:
    """First SKU in ``store`` with the wanted behaviour."""
    for number in range(1, limit):
        sku = origin.sku_id(number)
        if origin.behaviour_for(store, sku) == behaviour:
            return sku
    raise AssertionError(f"no {behaviour} sku found in {store} within {limit}")


def _store_with(origin, **wanted) -> str:
    """First fleet store whose resolved profile matches ``wanted``."""
    for index in range(500):
        store = origin.store_id(index)
        profile = origin.store_profile(store)
        if all(getattr(profile, key) == value for key, value in wanted.items()):
            return store
    raise AssertionError(f"no store matches {wanted}")


# --------------------------------------------------------------------------
# Structural: no external calls, no data files
# --------------------------------------------------------------------------

#: Any of these imported by the fixture origin means it can reach
#: something. It must be able to reach nothing.
FORBIDDEN_IMPORTS = {
    "httpx",
    "requests",
    "urllib.request",
    "aiohttp",
    "socket",
    "psycopg",
    "sqlalchemy",
    "redis",
    "celery",
    "app_shared",
    "boto3",
    "smtplib",
    "ftplib",
    "http.client",
    "subprocess",
}


def test_makes_no_external_calls() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    offenders = {
        name
        for name in imported
        if name in FORBIDDEN_IMPORTS or name.split(".")[0] in FORBIDDEN_IMPORTS
    }
    assert offenders == set(), (
        f"fixture-origin must make no external calls; found imports {sorted(offenders)}"
    )


def test_reads_no_data_files() -> None:
    """Every SKU is generated. `open()`/`Path.read_*` would mean a
    fixture whose content depends on what was deployed alongside it."""
    source = MODULE_PATH.read_text(encoding="utf-8")
    for forbidden in ("open(", "read_text", "read_bytes", "pathlib"):
        assert forbidden not in source, f"fixture-origin must not use {forbidden!r}"


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


def test_product_is_deterministic_across_module_loads(origin) -> None:
    """A second, independent import produces byte-identical facts —
    i.e. determinism survives a process restart and a second replica,
    which `hash()` (PYTHONHASHSEED-randomised) would not."""
    other = _load("_fixture_origin_main_second")
    for sku in ("p000001", "p012345", "p499999"):
        assert origin.product_for("fleet-007", sku).as_dict() == (
            other.product_for("fleet-007", sku).as_dict()
        )
        assert origin.behaviour_for("fleet-007", sku) == other.behaviour_for(
            "fleet-007", sku
        )


def test_behaviour_is_stable_for_the_same_path(origin) -> None:
    store = _store_with(origin, name="flaky")
    sku = _find_sku(origin, store, origin.Behaviour.ERROR)
    assert [origin.behaviour_for(store, sku) for _ in range(50)] == [
        origin.Behaviour.ERROR
    ] * 50


def test_hash_fraction_is_in_range_and_spread(origin) -> None:
    values = [origin.hash_fraction("x", str(n)) for n in range(500)]
    assert all(0.0 <= value < 1.0 for value in values)
    assert len(set(values)) == len(values)
    # Not clustered in one decile — a broken derivation would be.
    assert 0.35 < sum(values) / len(values) < 0.65


# --------------------------------------------------------------------------
# The SKU space: 500,000 SKUs, generated from the path
# --------------------------------------------------------------------------


def test_sku_space_is_five_hundred_thousand(origin) -> None:
    assert origin.MAX_SKU == 500_000
    assert origin.parse_sku("p000001") == 1
    assert origin.parse_sku("p500000") == 500_000
    assert origin.parse_sku("p500001") is None
    assert origin.parse_sku("p000000") is None
    assert origin.parse_sku("500001") is None
    assert origin.parse_sku("pABCDEF") is None
    assert origin.parse_sku("p00001") is None  # wrong width


def test_sku_id_round_trips(origin) -> None:
    for number in (1, 42, 123_456, 500_000):
        assert origin.parse_sku(origin.sku_id(number)) == number


# --------------------------------------------------------------------------
# The behaviour model
# --------------------------------------------------------------------------


def test_every_store_archetype_is_reachable(origin) -> None:
    """A 100-store fleet must actually get a mix, not 100 happy paths."""
    names = {
        origin.store_profile(origin.store_id(index)).name for index in range(100)
    }
    assert names == {profile.name for profile in origin.PROFILE_ARCHETYPES}


def test_behaviour_rates_track_the_profile(origin) -> None:
    store = _store_with(origin, name="hostile")
    profile = origin.store_profile(store)
    sample = [origin.behaviour_for(store, origin.sku_id(n)) for n in range(1, 5001)]
    blocked = sample.count(origin.Behaviour.BLOCKED) / len(sample)
    errored = sample.count(origin.Behaviour.ERROR) / len(sample)
    assert abs(blocked - profile.blocked_rate) < 0.03
    assert abs(errored - profile.error_rate) < 0.02


def test_fast_store_is_always_ok(origin) -> None:
    store = _store_with(origin, name="fast")
    assert all(
        origin.behaviour_for(store, origin.sku_id(n)) == origin.Behaviour.OK
        for n in range(1, 2001)
    )


def test_latency_is_deterministic_bounded_and_scalable(origin) -> None:
    store = _store_with(origin, name="slow")
    profile = origin.store_profile(store)
    origin.LATENCY_SCALE = 1.0
    try:
        values = [origin.latency_seconds(store, origin.sku_id(n)) for n in range(1, 400)]
        assert all(value >= 0.0 for value in values)
        upper = (profile.latency_ms + profile.latency_jitter_ms) / 1000.0
        assert max(values) <= upper + 1e-9
        assert origin.latency_seconds(store, "p000001") == values[0]
        origin.LATENCY_SCALE = 0.0
        assert origin.latency_seconds(store, "p000001") == 0.0
    finally:
        origin.LATENCY_SCALE = 0.0


def test_profile_overrides_apply(origin, monkeypatch) -> None:
    store = _store_with(origin, name="fast")
    monkeypatch.setenv(
        "FIXTURE_ORIGIN_PROFILE_OVERRIDES",
        json.dumps({store: {"blocked_rate": 1.0}}),
    )
    profile = origin.store_profile(store)
    assert profile.blocked_rate == 1.0
    assert profile.name.endswith("+override")
    assert origin.behaviour_for(store, "p000001") == origin.Behaviour.BLOCKED


def test_bad_override_json_degrades_to_the_archetype(origin, monkeypatch) -> None:
    store = _store_with(origin, name="fast")
    monkeypatch.setenv("FIXTURE_ORIGIN_PROFILE_OVERRIDES", "{not json")
    assert origin.store_profile(store).blocked_rate == 0.0


# --------------------------------------------------------------------------
# HTTP surface
# --------------------------------------------------------------------------


def test_healthz_is_always_ok(client) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_index_lists_the_fleet_stores(client, origin) -> None:
    payload = client.get("/").json()
    assert payload["stores"][0] == "fleet-000"
    assert len(payload["stores"]) == origin.STORE_COUNT


def test_product_page_carries_price_and_structured_data(client, origin) -> None:
    store = _store_with(origin, name="fast")
    response = client.get(f"/p/{store}/p000123")
    assert response.status_code == 200
    assert response.headers["X-Fixture-Behaviour"] == "ok"
    body = response.text
    product = origin.product_for(store, "p000123")
    assert str(product.price) in body
    assert "application/ld+json" in body
    payload = json.loads(body.split('ld+json">', 1)[1].split("</script>", 1)[0])
    assert payload["offers"]["price"] == str(product.price)
    assert payload["offers"]["priceCurrency"] == product.currency


def test_store_without_structured_data_still_states_the_price(client, origin) -> None:
    store = _store_with(origin, name="no_structured_data")
    sku = _find_sku(origin, store, origin.Behaviour.OK)
    response = client.get(f"/p/{store}/{sku}")
    assert response.status_code == 200
    assert "ld+json" not in response.text
    assert str(origin.product_for(store, sku).price) in response.text


def test_json_endpoint_states_the_same_facts(client, origin) -> None:
    store = _store_with(origin, name="fast")
    payload = client.get(f"/api/p/{store}/p000123").json()
    assert payload == origin.product_for(store, "p000123").as_dict()


@pytest.mark.parametrize(
    "behaviour, status",
    [("error", 503), ("blocked", 403), ("not_listed", 404)],
)
def test_failure_behaviours_reach_the_wire(client, origin, behaviour, status) -> None:
    store = _store_with(origin, name="hostile" if behaviour != "not_listed" else "thin_catalogue")
    sku = _find_sku(origin, store, behaviour)
    response = client.get(f"/p/{store}/{sku}")
    assert response.status_code == status
    assert response.headers["X-Fixture-Behaviour"] == behaviour
    json_response = client.get(f"/api/p/{store}/{sku}")
    assert json_response.status_code == status


def test_redirect_lands_on_the_canonical_page_in_one_hop(client, origin) -> None:
    store = _store_with(origin, name="redirecting")
    sku = _find_sku(origin, store, origin.Behaviour.REDIRECT)
    response = client.get(f"/p/{store}/{sku}", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == f"/p/{store}/{sku}/c"
    followed = client.get(f"/p/{store}/{sku}", follow_redirects=True)
    assert followed.status_code == 200
    assert followed.headers["X-Fixture-Behaviour"] == "canonical"
    assert str(origin.product_for(store, sku).price) in followed.text


def test_out_of_range_sku_is_not_listed_not_an_error(client, origin) -> None:
    store = _store_with(origin, name="fast")
    response = client.get(f"/p/{store}/p999999")
    assert response.status_code == 404
    assert response.headers["X-Fixture-Behaviour"] == "not_listed"


def test_store_profile_endpoint_is_never_shaped_by_the_behaviour_model(
    client, origin
) -> None:
    store = _store_with(origin, name="hostile")
    response = client.get(f"/stores/{store}")
    assert response.status_code == 200
    assert response.json()["profile"]["blocked_rate"] > 0

"""The unit suite's environment isolation itself (`tests/conftest.py`,
plan task 0.4 / production-readiness audit §2).

A guard that silently stops working is worse than no guard, and this one is
invisible by construction: it only shows up when a test that shouldn't reach
a real dependency does. These tests assert the guard is actually in force in
whatever session they are running in.
"""

from __future__ import annotations

import os
import socket
from urllib.parse import urlsplit

import pytest

from conftest import INVALID_DEPENDENCY_ENVIRONMENT  # the root tests/conftest.py


@pytest.mark.parametrize("name", sorted(INVALID_DEPENDENCY_ENVIRONMENT))
def test_dependency_urls_point_at_the_dead_port(name: str) -> None:
    assert os.environ[name] == INVALID_DEPENDENCY_ENVIRONMENT[name]
    assert "127.0.0.1:1" in os.environ[name]


def test_the_patched_values_are_syntactically_valid_urls() -> None:
    """"Invalid, not missing" is the design point: every patched value is a
    well-formed URL, so code that merely READS configuration is unaffected and
    only code that CONNECTS fails. A missing variable would instead blow up at
    `Settings()` construction and hide the difference between "this test needs
    config" and "this test needs a live server".

    (`Settings()` itself is not constructed here: it has required fields —
    `ENCRYPTION_KEYS` among them — that the isolated environment deliberately
    does not supply, and a pydantic `ValidationError` echoes the whole input
    dict into the test log, which is the last place a credential should land.)
    """
    for name, value in INVALID_DEPENDENCY_ENVIRONMENT.items():
        parts = urlsplit(value.replace("+psycopg", ""))
        assert parts.scheme, name
        assert parts.hostname == "127.0.0.1", name
        assert parts.port == 1, name


def test_connecting_to_the_patched_database_is_refused_immediately() -> None:
    """The property the whole fixture exists for: reaching out fails fast and
    loudly instead of quietly finding a developer's real Postgres."""
    url = urlsplit(INVALID_DEPENDENCY_ENVIRONMENT["DATABASE_URL"].replace("+psycopg", ""))
    assert url.hostname == "127.0.0.1" and url.port == 1

    with pytest.raises(OSError):  # ConnectionRefusedError is an OSError subclass
        with socket.create_connection((url.hostname, url.port), timeout=2):
            pass


@pytest.mark.integration
def test_integration_marked_tests_keep_the_ambient_environment() -> None:
    """The other half of the policy: a test that declares it needs the live
    stack must NOT be handed the dead-port values, or `pytest tests/integration`
    would fail for a reason that has nothing to do with the code.

    Deselected from the unit gate by its own marker (`-m "not integration"`),
    which is exactly the arrangement it is asserting.
    """
    for name, invalid in INVALID_DEPENDENCY_ENVIRONMENT.items():
        assert os.environ.get(name) != invalid, name

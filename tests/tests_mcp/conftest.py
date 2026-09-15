"""Fixtures and shared helpers for the MCP server generator's tests.

Reuses `tests_cli/conftest.py`'s synthetic `WidgetsAPI`/`GadgetsAPI`/`CliTestClient` fixture set (and its
`module_scoped()`/`make_httpx_response()`/`make_rest_response()` helpers) rather than rebuilding the same
parameter-kind matrix. What's genuinely new here is async transport mocking - every existing CLI test
dispatches synchronously, while the MCP server always constructs its client with `async_mode=True`.
"""

from __future__ import annotations

import importlib.util
from typing import Any

import pytest
from httpx2 import AsyncClient
from pytest_mock import MockerFixture


@pytest.fixture
def async_request_mock(mocker: MockerFixture) -> Any:
    """Patch `httpx2.AsyncClient.request` so a client constructed with `async_mode=True` never hits the
    network, returning the mock so a test can set `.return_value`/`.side_effect` before constructing the
    client.

    Mirrors `tests/conftest.py`'s `api_client_factory(async_mode=True)` pattern, which can't be reused
    directly here since it's module-scoped and pinned to its own internal `_APIClient`, not this package's
    synthetic clients (`CliTestClient`, `DummyJSONClient`, ...).

    :param mocker: pytest-mock fixture
    """
    return mocker.patch.object(AsyncClient, "request")


def patch_mcp_installed(mocker: MockerFixture, *, installed: bool) -> None:
    """Patch `importlib.util.find_spec` so the `mcp` extra's installed state is deterministic for the test.

    Delegates to the real `find_spec` for every other module name, mirroring `tests_cli/conftest.py`'s
    `patch_argcomplete_installed()`.

    :param mocker: pytest-mock fixture
    :param installed: Whether `mcp` should appear installed to `_entrypoint.py`'s check
    """
    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "mcp":
            return object() if installed else None
        return real_find_spec(name, *args, **kwargs)

    mocker.patch("api_client_core.mcp._entrypoint.importlib.util.find_spec", side_effect=fake_find_spec)

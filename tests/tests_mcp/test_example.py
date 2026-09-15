"""Tests for the `examples/dummyjson` runnable example, through `api_client_core.mcp`.

Guards against the example rotting out of sync with the MCP server, the same way
`tests_cli/test_example.py` already does for the CLI generator. `examples/` is intentionally excluded
from the strict `mypy src` gate.

Guarded by `pytest.importorskip("mcp")`: an end-to-end example naturally exercises the full stack
(catalog -> schema -> tools -> dispatch), which needs the SDK.
"""

from __future__ import annotations

from typing import Any

import pytest

mcp = pytest.importorskip("mcp")

from api_client_core.mcp._constants import Mode  # noqa: E402
from api_client_core.mcp.catalog import ServerOptions, build_catalog  # noqa: E402
from api_client_core.mcp.server import _construct_client, _dispatch_tool_call  # noqa: E402
from api_client_core.mcp.tools import build_dynamic_tools, build_static_tools  # noqa: E402
from examples.dummyjson.client import DummyJSONClient  # noqa: E402

from ..tests_cli.conftest import make_httpx_response  # noqa: E402


class TestDummyJSONExampleModes:
    """Tests that the example client resolves to the expected exposure mode and tool set"""

    def test_default_auto_mode_resolves_to_dynamic(self) -> None:
        """Test that the full, unfiltered example client (73 endpoints) resolves to dynamic mode under
        the default threshold, and publishes exactly the four fixed meta-tools
        """
        catalog = build_catalog(DummyJSONClient, ServerOptions())
        assert catalog.mode is Mode.DYNAMIC
        assert len(catalog.entries) == 73
        tools = build_dynamic_tools(catalog, ServerOptions())
        assert {t.name for t in tools} == {"list_resources", "search_endpoints", "describe_endpoint", "call_endpoint"}

    def test_forced_static_mode_publishes_one_tool_per_endpoint(self) -> None:
        """Test that --mode static on the same, otherwise-unfiltered client publishes one tool per
        endpoint regardless of the default threshold
        """
        options = ServerOptions(mode=Mode.STATIC)
        catalog = build_catalog(DummyJSONClient, options)
        assert catalog.mode is Mode.STATIC
        assert len(catalog.entries) == 73
        tools = build_static_tools(catalog, options)
        assert len(tools) == 73
        assert "products__get_product" in {t.name for t in tools}

    def test_resource_filter_narrows_below_threshold_and_flips_to_static(self) -> None:
        """Test that --resource products applies BEFORE the auto-mode threshold check: the filtered
        10-endpoint client resolves to static mode even though the unfiltered 73-endpoint one resolves
        to dynamic
        """
        catalog = build_catalog(DummyJSONClient, ServerOptions(resources=("products",)))
        assert catalog.mode is Mode.STATIC
        assert len(catalog.entries) == 10
        assert all(e.resource == "products" for e in catalog.entries)


class TestDummyJSONExampleDispatch:
    """Tests that the example client dispatches through `api_client_core.mcp` against a mocked httpx2"""

    async def test_search_endpoints_finds_product_endpoints(self, async_request_mock: Any) -> None:
        """Test that search_endpoints(query="product") finds the expected product endpoint_ids"""
        options = ServerOptions()
        client = await _construct_client(DummyJSONClient, options)
        catalog = build_catalog(DummyJSONClient, options)

        result = await _dispatch_tool_call(client, catalog, "search_endpoints", {"query": "product"}, options)

        assert result.is_error is False
        endpoint_ids = {r["endpoint_id"] for r in result.structured_content["results"]}
        assert "products__get_product" in endpoint_ids
        assert "products__create_product" in endpoint_ids
        await client.aclose()

    async def test_describe_then_call_endpoint_round_trips(self, async_request_mock: Any, mocker: Any) -> None:
        """Test that describe_endpoint's input_schema matches what call_endpoint actually accepts,
        and that call_endpoint reaches the expected real HTTP call - the same round trip
        `tests_cli/test_example.py`'s `test_get_product_reaches_the_expected_url` proves for the CLI.
        """
        async_request_mock.return_value = make_httpx_response(
            mocker, 200, json_body={"id": 1, "title": "t", "price": 9.99}
        )
        options = ServerOptions()
        client = await _construct_client(DummyJSONClient, options)
        catalog = build_catalog(DummyJSONClient, options)

        described = await _dispatch_tool_call(
            client, catalog, "describe_endpoint", {"endpoint_id": "products__get_product"}, options
        )
        assert described.is_error is False
        assert "product_id" in described.structured_content["input_schema"]["properties"]

        called = await _dispatch_tool_call(
            client,
            catalog,
            "call_endpoint",
            {"endpoint_id": "products__get_product", "arguments": {"product_id": 1}},
            options,
        )
        assert called.is_error is False
        assert async_request_mock.call_args.args == ("GET", "/products/1")
        await client.aclose()

    async def test_static_mode_call_endpoint_sends_the_aliased_query_param(
        self, async_request_mock: Any, mocker: Any
    ) -> None:
        """Test that a static-mode tool call sends the aliased `sortBy` query param, not `sort_by` - the
        same aliasing `tests_cli/test_example.py`'s `test_list_products_sends_aliased_query_param` proves
        for the CLI.
        """
        async_request_mock.return_value = make_httpx_response(mocker, 200, json_body={"products": []})
        options = ServerOptions(mode=Mode.STATIC)
        client = await _construct_client(DummyJSONClient, options)
        catalog = build_catalog(DummyJSONClient, options)

        result = await _dispatch_tool_call(client, catalog, "products__list_products", {"sort_by": "price"}, options)

        assert result.is_error is False
        assert async_request_mock.call_args.kwargs.get("params") == {"sortBy": "price"}
        await client.aclose()

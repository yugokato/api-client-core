"""End-to-end tests driving a real `mcp.ClientSession` against a real `mcp.server.Server`.

Every other module in `tests_mcp/` calls into `api_client_core.mcp.server`/`.runner` directly, below the
SDK's own protocol layer (`_dispatch_tool_call()`, `dispatch_endpoint_call()`, ...) - useful for testing this
package's own logic in isolation, but it never exercises the SDK boundary itself: the initialize
handshake, `Server.get_capabilities()`'s derivation from which `on_*` handlers were actually wired, the
real `tools/list`/`tools/call` JSON-RPC round trip (including the SDK's own field-name serialization,
e.g. `input_schema` -> `inputSchema`), or the client-side `outputSchema` validation of `structuredContent`
`ClientSession.call_tool()` performs on every non-error result. This module closes that gap, connecting a
real `ClientSession` to a real `Server` over the SDK's in-memory transport
(`mcp.shared.memory.create_client_server_memory_streams`), so a change to any of the above would actually
be caught here rather than only by a hand-written assertion on the closures `_build_server()` builds.

Guarded by `pytest.importorskip("mcp")`, like every other module here that touches the SDK's types.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import anyio
import pytest
from pytest_mock import MockerFixture

mcp = pytest.importorskip("mcp")

from mcp import ClientSession  # noqa: E402
from mcp.shared.memory import create_client_server_memory_streams  # noqa: E402

from api_client_core.mcp._constants import Mode  # noqa: E402
from api_client_core.mcp.catalog import ServerOptions, build_catalog  # noqa: E402
from api_client_core.mcp.server import _build_server, _construct_client  # noqa: E402

from ..tests_cli.conftest import CliTestClient, make_httpx_response  # noqa: E402

_BASE_URL = "https://example.com/api"


@asynccontextmanager
async def _live_session(options: ServerOptions) -> AsyncIterator[ClientSession]:
    """Build a client/server pair exactly as `prepare()` would, wire them over the SDK's in-memory
    transport, and yield an already-initialized `ClientSession` for the duration of the block.

    The server task is cancelled once the caller's block exits, rather than relied on to exit on its own:
    `ClientSession`'s own teardown doesn't close the shared memory streams it was handed (it doesn't own
    them), so the server's `run()` would otherwise never see its read stream close.

    :param options: Resolved server options, threaded to both catalog build and client construction
    """
    client = await _construct_client(CliTestClient, options)
    catalog = build_catalog(CliTestClient, options)
    server = _build_server(client, catalog, options)
    try:
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            client_read, client_write = client_streams
            server_read, server_write = server_streams
            async with anyio.create_task_group() as tg:
                tg.start_soon(server.run, server_read, server_write, server.create_initialization_options())
                async with ClientSession(client_read, client_write) as session:
                    await session.initialize()
                    yield session
                tg.cancel_scope.cancel()
    finally:
        await client.aclose()


class TestStaticModeSession:
    """Tests for a live session against a static-mode server (CliTestClient, under the default threshold)"""

    async def test_list_tools_matches_the_catalog(self) -> None:
        """Test that tools/list returns one real SDK Tool per catalog entry, capability-advertised and
        all, not just what build_static_tools() itself claims to build
        """
        options = ServerOptions(base_url=_BASE_URL)
        catalog = build_catalog(CliTestClient, options)
        assert catalog.mode is Mode.STATIC
        async with _live_session(options) as session:
            result = await session.list_tools()
            assert {t.name for t in result.tools} == {e.tool_name for e in catalog.entries}

    async def test_call_tool_success_round_trips_through_output_schema_validation(
        self, async_request_mock: Any, mocker: MockerFixture
    ) -> None:
        """Test that a successful call_tool() round-trips through the SDK's client-side outputSchema
        validation without raising - the exact contract that would break silently if _RESULT_OUTPUT_SCHEMA
        (tools.py) and the envelope dispatch_endpoint_call() actually returns (runner.py) ever drifted apart
        """
        async_request_mock.return_value = make_httpx_response(mocker, 200, json_body={"id": 1, "name": "Foo"})
        async with _live_session(ServerOptions(base_url=_BASE_URL)) as session:
            result = await session.call_tool("widgets__get_widget", {"widget_id": 1})
            assert result.is_error is False
            assert result.structured_content["body"] == {"id": 1, "name": "Foo"}

    async def test_call_tool_with_call_wrappers_round_trips_a_single_envelope(
        self, async_request_mock: Any, mocker: MockerFixture
    ) -> None:
        """Test that a call carrying a single-response `call_wrappers` object still produces the
        {status_code, headers, body} envelope and passes the SDK's client-side outputSchema validation
        """
        async_request_mock.return_value = make_httpx_response(mocker, 200, json_body={"id": 1})
        async with _live_session(ServerOptions(base_url=_BASE_URL)) as session:
            result = await session.call_tool(
                "widgets__get_widget", {"widget_id": 1, "call_wrappers": {"with_retry": {}}}
            )
            assert result.is_error is False
            assert result.structured_content["body"] == {"id": 1}

    async def test_call_tool_with_repeat_round_trips_a_results_list(
        self, async_request_mock: Any, mocker: MockerFixture
    ) -> None:
        """Test that a `with_repeat` list result ({"results": [...]}) passes the SDK's outputSchema
        validation - the `anyOf` branch of _RESULT_OUTPUT_SCHEMA that a top-level union would break
        """
        async_request_mock.return_value = make_httpx_response(mocker, 200, json_body={"ok": True})
        async with _live_session(ServerOptions(base_url=_BASE_URL)) as session:
            result = await session.call_tool("widgets__list_widgets", {"call_wrappers": {"with_repeat": {"num": 2}}})
            assert result.is_error is False
            assert len(result.structured_content["results"]) == 2

    async def test_call_tool_failure_is_reported_as_an_error_result(
        self, async_request_mock: Any, mocker: MockerFixture
    ) -> None:
        """Test that a non-2xx response reaches the client as isError=True, not a raised protocol error -
        and that an error result is exempt from outputSchema validation (it carries no structuredContent),
        matching the SDK's documented behavior
        """
        async_request_mock.return_value = make_httpx_response(mocker, 404, json_body={"error": "not found"})
        async with _live_session(ServerOptions(base_url=_BASE_URL)) as session:
            result = await session.call_tool("widgets__get_widget", {"widget_id": 999})
            assert result.is_error is True

    async def test_capabilities_advertise_tools_only(self) -> None:
        """Test that the live initialize handshake reports only the tools capability, matching
        _build_server()'s on_list_tools/on_call_tool-only handler set
        """
        async with _live_session(ServerOptions(base_url=_BASE_URL)) as session:
            assert session.server_capabilities is not None
            assert session.server_capabilities.tools is not None
            assert session.server_capabilities.resources is None


class TestDynamicModeSession:
    """Tests for a live session against a dynamic-mode server (CliTestClient, forced below its own
    default threshold)
    """

    _OPTIONS = ServerOptions(threshold=1, base_url=_BASE_URL)

    async def test_list_tools_returns_exactly_the_four_meta_tools(self) -> None:
        """Test that tools/list publishes exactly the four fixed dynamic-mode meta-tools, regardless of
        how many endpoints the underlying client actually has
        """
        catalog = build_catalog(CliTestClient, self._OPTIONS)
        assert catalog.mode is Mode.DYNAMIC
        async with _live_session(self._OPTIONS) as session:
            result = await session.list_tools()
            assert {t.name for t in result.tools} == {
                "list_resources",
                "search_endpoints",
                "describe_endpoint",
                "call_endpoint",
            }

    async def test_search_then_call_endpoint_round_trips(self, async_request_mock: Any, mocker: MockerFixture) -> None:
        """Test the full search_endpoints -> call_endpoint workflow over a live session, including the
        SDK's outputSchema validation of call_endpoint's structuredContent
        """
        async_request_mock.return_value = make_httpx_response(mocker, 200, json_body={"id": 1, "name": "Foo"})
        async with _live_session(self._OPTIONS) as session:
            search_result = await session.call_tool("search_endpoints", {"query": "get_widget"})
            assert search_result.is_error is False
            endpoint_ids = {r["endpoint_id"] for r in search_result.structured_content["results"]}
            assert "widgets__get_widget" in endpoint_ids

            call_result = await session.call_tool(
                "call_endpoint", {"endpoint_id": "widgets__get_widget", "arguments": {"widget_id": 1}}
            )
            assert call_result.is_error is False
            assert call_result.structured_content["body"] == {"id": 1, "name": "Foo"}

"""Unit tests for `api_client_core.mcp.server`

Guarded by `pytest.importorskip("mcp")`: this module (unlike catalog.py/schema.py/runner.py) imports the
optional `mcp` SDK, so its tests only run when the `mcp` extra is installed.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from pytest_mock import MockerFixture

mcp = pytest.importorskip("mcp")

from api_client_core import APIClient  # noqa: E402
from api_client_core._common.console import reserve_stdout  # noqa: E402
from api_client_core.mcp import server  # noqa: E402
from api_client_core.mcp._constants import Mode  # noqa: E402
from api_client_core.mcp.catalog import ServerOptions, build_catalog  # noqa: E402
from api_client_core.mcp.server import (  # noqa: E402
    _apply_headers,
    _build_server,
    _construct_client,
    _dispatch_tool_call,
    _format_exception,
    prepare,
    serve,
    serve_connection,
)

from ..tests_cli.conftest import CliTestClient, WidgetsAPI, make_httpx_response, module_scoped  # noqa: E402

_BASE_URL = "https://example.com/api"


class TestConstructClient:
    """Tests for `_construct_client()`'s async-mode enforcement and MCP-specific defaults"""

    async def test_constructs_in_async_mode(self) -> None:
        """Test that the client is always constructed with async_mode=True"""
        client = await _construct_client(CliTestClient, ServerOptions(base_url=_BASE_URL))
        assert client.async_mode is True

    async def test_disables_request_logging_by_default(self) -> None:
        """Test that log_requests is forced off unconditionally, unlike the CLI's -q-keyed version of
        this same switch
        """
        client = await _construct_client(CliTestClient, ServerOptions(base_url=_BASE_URL))
        assert client.rest_client.log_requests is False

    async def test_log_requests_option_keeps_logging_on(self) -> None:
        """Test that --log-requests (options.log_requests=True) leaves request logging untouched"""
        client = await _construct_client(CliTestClient, ServerOptions(log_requests=True, base_url=_BASE_URL))
        assert client.rest_client.log_requests is True

    async def test_base_url_option_is_forwarded(self) -> None:
        """Test that --base-url overrides the client's default base URL"""
        client = await _construct_client(CliTestClient, ServerOptions(base_url="https://override.example.com"))
        assert str(client.base_url).rstrip("/") == "https://override.example.com"

    async def test_client_whose_constructor_rejects_the_async_mode_kwarg_raises_actionably(self) -> None:
        """Test that a client whose __init__ doesn't even accept async_mode raises a RuntimeError
        naming the actual requirement, not a bare TypeError
        """

        @module_scoped
        class _NoKwargClient(APIClient):
            app_name = "no-async-kwarg-test"

            def __init__(self) -> None:
                super().__init__(async_mode=False, base_url="https://example.com")

        with pytest.raises(RuntimeError, match="async_mode=True"):
            await _construct_client(_NoKwargClient, ServerOptions())

    async def test_client_that_silently_stays_sync_raises_actionably(self, mocker: MockerFixture) -> None:
        """Test that a client accepting (and ignoring) async_mode=True, but still constructing in sync
        mode, is caught by the defensive post-construction check rather than silently proceeding, and
        that the still-sync client is closed with close(), not the aclose() a genuine async-mode client
        would need
        """

        @module_scoped
        class _IgnoresAsyncModeClient(APIClient):
            app_name = "ignores-async-mode-test"

            def __init__(self, **kwargs: Any) -> None:
                # Bypasses the real constructor's own guard against this, simulating a client that lands
                # in sync mode despite being asked for async.
                self.async_mode = False
                self._base_url = "https://example.com"
                self.rest_client = mocker.Mock()

        with pytest.raises(RuntimeError, match="did not construct in async mode"):
            await _construct_client(_IgnoresAsyncModeClient, ServerOptions())

    async def test_type_error_from_inside_a_compatible_constructors_own_body_propagates_unmodified(self) -> None:
        """Test that a TypeError raised from inside a constructor that DOES accept async_mode propagates
        with its own real message, rather than being misreported as the unrelated
        "must accept async_mode=True" complaint - that check is only for a constructor that can't even
        accept the kwarg in the first place
        """

        @module_scoped
        class _BrokenInitClient(APIClient):
            app_name = "broken-init-test"

            def __init__(self, **kwargs: Any) -> None:
                super().__init__(base_url="https://example.com", **kwargs)
                raise TypeError("something unrelated went wrong in here")

        with pytest.raises(TypeError, match="something unrelated went wrong in here"):
            await _construct_client(_BrokenInitClient, ServerOptions())

    async def test_a_failure_after_construction_closes_the_client(self, mocker: MockerFixture) -> None:
        """Test that a client which fails the post-construction async-mode check is actually closed, not
        just discarded, before the failure propagates
        """

        @module_scoped
        class _IgnoresAsyncModeClient(APIClient):
            app_name = "closes-on-failure-test"

            def __init__(self, **kwargs: Any) -> None:
                self.async_mode = False
                self._base_url = "https://example.com"
                self.rest_client = mocker.Mock()

        close_spy = mocker.spy(APIClient, "close")
        with pytest.raises(RuntimeError, match="did not construct in async mode"):
            await _construct_client(_IgnoresAsyncModeClient, ServerOptions())
        close_spy.assert_called_once()

    async def test_a_failure_applying_headers_closes_the_client(self, mocker: MockerFixture) -> None:
        """Test that a client is closed if _apply_headers() raises, rather than leaking the connection
        pool of an otherwise fully async-mode client
        """
        mocker.patch("api_client_core.mcp.server._apply_headers", side_effect=RuntimeError("bad header"))
        aclose_spy = mocker.spy(APIClient, "aclose")
        with pytest.raises(RuntimeError, match="bad header"):
            await _construct_client(CliTestClient, ServerOptions(base_url=_BASE_URL, headers=(("X", "y"),)))
        aclose_spy.assert_called_once()


class TestApplyHeaders:
    """Tests for `_apply_headers()`'s post-construction header application"""

    async def test_no_headers_is_a_no_op(self) -> None:
        """Test that an empty headers tuple doesn't touch the client at all"""
        client = await _construct_client(CliTestClient, ServerOptions(base_url=_BASE_URL))
        _apply_headers(client, ())  # must not raise

    async def test_headers_are_applied_to_the_underlying_client(self) -> None:
        """Test that given headers reach the underlying httpx2 client's headers"""
        client = await _construct_client(CliTestClient, ServerOptions(base_url=_BASE_URL))
        _apply_headers(client, (("X-Custom", "value"),))
        assert client.rest_client.client.headers["X-Custom"] == "value"

    async def test_authorization_header_clears_existing_auth(self) -> None:
        """Test that an explicit Authorization header clears any auth the client installed for itself,
        so it isn't silently overridden by that auth on every request
        """
        client = await _construct_client(CliTestClient, ServerOptions(base_url=_BASE_URL))
        client.rest_client.auth = lambda request: request  # a minimal valid httpx2 auth callable
        _apply_headers(client, (("Authorization", "Bearer xyz"),))
        assert client.rest_client.auth is None


class TestFormatException:
    """Tests for `_format_exception()`"""

    def test_delegates_to_format_error_message(self, mocker: MockerFixture) -> None:
        """Test that `_format_exception()` is a pure delegation to `format_error_message()`, which owns
        the actual type-based formatting rules (see `tests_common/test_console.py::TestWriteError`)
        """
        spy = mocker.spy(server, "format_error_message")
        exc = ValueError("bad value")
        assert _format_exception(exc) == "ValueError: bad value"
        spy.assert_called_once_with(exc)


class TestDispatchToolCall:
    """Tests for `_dispatch_tool_call()`'s routing and its unhandled-exception catch-all"""

    @pytest.fixture
    async def client(self, async_request_mock: Any) -> Any:
        return await _construct_client(CliTestClient, ServerOptions(base_url=_BASE_URL))

    async def test_static_mode_dispatches_by_tool_name(self, client: Any, async_request_mock: Any, mocker: Any) -> None:
        """Test that static mode looks a tool name up in the catalog and dispatches it"""
        async_request_mock.return_value = make_httpx_response(mocker, 200, json_body={"id": 1})
        catalog = build_catalog(CliTestClient, ServerOptions())
        result = await _dispatch_tool_call(client, catalog, "widgets__get_widget", {"widget_id": 1}, ServerOptions())
        assert result.is_error is False

    async def test_static_mode_unknown_tool_name_is_an_error(self, client: Any) -> None:
        """Test that an unrecognized tool name in static mode returns isError rather than raising"""
        catalog = build_catalog(CliTestClient, ServerOptions())
        result = await _dispatch_tool_call(client, catalog, "bogus__tool", {}, ServerOptions())
        assert result.is_error is True

    async def test_static_mode_unknown_argument_hint_names_only_the_input_schema(self, client: Any) -> None:
        """Test that dispatch passes the catalog's resolved mode through to dispatch_endpoint_call(), so
        a static-mode unknown-argument error only points at this tool's own input schema, not also at
        describe_endpoint, which doesn't exist as a tool under this mode
        """
        catalog = build_catalog(CliTestClient, ServerOptions())
        result = await _dispatch_tool_call(client, catalog, "widgets__get_widget", {"bogus": "x"}, ServerOptions())
        assert result.is_error is True
        text = result.content[0].text
        assert "input schema" in text
        assert "describe_endpoint" not in text

    async def test_dynamic_mode_dispatches_the_four_meta_tools(self, client: Any) -> None:
        """Test that dynamic mode routes each of the four fixed tool names to a working handler.

        `_dispatch_tool_call()` returns the SDK's own `types.CallToolResult`, not the internal
        `ToolResult` (`dispatch_endpoint_call()`'s own return type) - `structured_content`/`content[0].text`,
        not `structured`/`content` directly.
        """
        options = ServerOptions(threshold=1)
        catalog = build_catalog(CliTestClient, options)
        result = await _dispatch_tool_call(client, catalog, "list_resources", {}, options)
        assert result.is_error is False
        assert result.structured_content is not None and "resources" in result.structured_content

    async def test_dynamic_mode_calling_a_valid_endpoint_id_as_a_tool_name_hints_at_call_endpoint(
        self, client: Any
    ) -> None:
        """Test that calling a real endpoint_id directly (as a static-mode tool name would be called),
        while actually in dynamic mode, points at call_endpoint rather than a bare "unknown tool" - the
        most likely way a model reaches this branch, having just gotten that same id from
        search_endpoints
        """
        options = ServerOptions(threshold=1)
        catalog = build_catalog(CliTestClient, options)
        result = await _dispatch_tool_call(client, catalog, "widgets__get_widget", {}, options)
        assert result.is_error is True
        assert "call_endpoint" in result.content[0].text
        assert "widgets__get_widget" in result.content[0].text

    async def test_dynamic_mode_unknown_endpoint_id_hints_at_search_endpoints(self, client: Any) -> None:
        """Test that an endpoint_id that resolves to nothing points at search_endpoints, for both
        describe_endpoint and call_endpoint
        """
        options = ServerOptions(threshold=1)
        catalog = build_catalog(CliTestClient, options)
        for tool_name, arguments in (
            ("describe_endpoint", {"endpoint_id": "bogus"}),
            ("call_endpoint", {"endpoint_id": "bogus", "arguments": {}}),
        ):
            result = await _dispatch_tool_call(client, catalog, tool_name, arguments, options)
            assert result.is_error is True
            assert "search_endpoints" in result.content[0].text

    async def test_dynamic_mode_rejects_an_unknown_top_level_key(self, client: Any) -> None:
        """Test that a call_endpoint call whose caller forgot to nest its own arguments under
        "arguments" - sending {"endpoint_id": ..., "widget_id": 1} instead - is rejected naming the
        actual mistake, rather than the low-level SDK's own unenforced additionalProperties: false
        silently dropping "widget_id" and surfacing a confusing "missing required argument" instead
        """
        options = ServerOptions(threshold=1)
        catalog = build_catalog(CliTestClient, options)
        result = await _dispatch_tool_call(
            client, catalog, "call_endpoint", {"endpoint_id": "widgets__get_widget", "widget_id": 1}, options
        )
        assert result.is_error is True
        assert "widget_id" in result.content[0].text

    async def test_dynamic_mode_target_endpoint_unknown_argument_hint_names_only_describe_endpoint(
        self, client: Any
    ) -> None:
        """Test that dispatch passes the catalog's resolved mode through to dispatch_endpoint_call(), so
        a dynamic-mode unknown-argument error for the *target endpoint's* own schema (nested correctly
        under "arguments", unlike the top-level-key mistake above) only points at describe_endpoint, not
        also at a static-mode tool's own input schema that doesn't exist under this mode
        """
        options = ServerOptions(threshold=1)
        catalog = build_catalog(CliTestClient, options)
        result = await _dispatch_tool_call(
            client,
            catalog,
            "call_endpoint",
            {"endpoint_id": "widgets__get_widget", "arguments": {"bogus": "x"}},
            options,
        )
        assert result.is_error is True
        text = result.content[0].text
        assert "describe_endpoint" in text
        assert "this tool's own input schema" not in text

    async def test_dynamic_mode_search_endpoints_rejects_an_unknown_top_level_key(self, client: Any) -> None:
        """Test that search_endpoints rejects an unrecognized top-level key (e.g. "q" instead of
        "query") rather than silently ignoring it and returning an unfiltered page
        """
        options = ServerOptions(threshold=1)
        catalog = build_catalog(CliTestClient, options)
        result = await _dispatch_tool_call(client, catalog, "search_endpoints", {"q": "widget"}, options)
        assert result.is_error is True
        assert "q" in result.content[0].text

    async def test_binary_body_result_serializes_without_crashing_the_connection(
        self, client: Any, async_request_mock: Any, mocker: Any
    ) -> None:
        """Test that a binary (non-JSON, non-UTF-8) response body reaches a serializable
        CallToolResult rather than raw bytes in structuredContent, which the SDK's
        model_dump(mode="json") can't encode. This used to fail only once the SDK serialized the
        already-returned result, past _dispatch_tool_call()'s catch-all, crashing the whole
        connection instead of surfacing as one failed tool call.
        """
        response = make_httpx_response(mocker, 200, content=b"\x89PNG\r\n\x1a\n\xff\xfe")
        async_request_mock.return_value = response
        catalog = build_catalog(CliTestClient, ServerOptions())
        result = await _dispatch_tool_call(client, catalog, "widgets__get_widget", {"widget_id": 1}, ServerOptions())
        assert result.is_error is False
        result.model_dump(mode="json", by_alias=True)  # must not raise

    async def test_unanticipated_exception_becomes_an_error_result_not_a_raise(self, client: Any, mocker: Any) -> None:
        """Test that an exception _dispatch_tool_call() can't anticipate (unlike a bad argument or a
        non-2xx response, both already handled inside dispatch_endpoint_call()) is caught here and turned
        into an isError result rather than propagating - an exception escaping this function would become
        a JSON-RPC protocol error in most MCP hosts, not a recoverable failure
        """
        catalog = build_catalog(CliTestClient, ServerOptions())
        mocker.patch(
            "api_client_core.mcp.server.dispatch_endpoint_call", side_effect=TimeoutError("the network timed out")
        )
        result = await _dispatch_tool_call(client, catalog, "widgets__get_widget", {"widget_id": 1}, ServerOptions())
        assert result.is_error is True
        text = result.content[0].text
        assert "TimeoutError" in text
        assert "the network timed out" in text


class TestPrepare:
    """Tests for `prepare()`'s catalog/client/server construction, the startup phase `_entrypoint.py`'s
    `_serve()` catches broadly as a usage error, separately from actually serving the connection
    """

    async def test_returns_a_constructed_client_and_a_wired_server(self, async_request_mock: Any) -> None:
        """Test that prepare() returns the constructed, async-mode client together with a Server ready
        to serve it
        """
        client, mcp_server = await prepare(CliTestClient, ServerOptions(base_url=_BASE_URL))
        try:
            assert client.async_mode is True
            assert isinstance(mcp_server, mcp.server.Server)
        finally:
            await client.aclose()

    async def test_a_construction_failure_propagates(self) -> None:
        """Test that a failure constructing the client propagates out of prepare() unmodified, for
        _entrypoint.py's _serve() to catch and report as a usage error
        """

        @module_scoped
        class _NoKwargClient(APIClient):
            app_name = "prepare-no-async-kwarg-test"

            def __init__(self) -> None:
                super().__init__(async_mode=False, base_url="https://example.com")

            @property
            def widgets(self) -> WidgetsAPI:
                return WidgetsAPI(self)

        with pytest.raises(RuntimeError, match="async_mode=True"):
            await prepare(_NoKwargClient, ServerOptions())

    async def test_a_failure_building_the_server_closes_the_already_constructed_client(
        self, async_request_mock: Any, mocker: MockerFixture
    ) -> None:
        """Test that a failure in _build_server(), after the client already exists, closes that client
        before propagating, rather than leaking its connection pool
        """
        mocker.patch("api_client_core.mcp.server._build_server", side_effect=RuntimeError("bad tool set"))
        aclose_spy = mocker.spy(APIClient, "aclose")
        with pytest.raises(RuntimeError, match="bad tool set"):
            await prepare(CliTestClient, ServerOptions(base_url=_BASE_URL))
        aclose_spy.assert_called_once()


class TestServeConnection:
    """Tests for `serve_connection()`'s stdio serve loop and client teardown"""

    async def test_closes_the_client_even_when_serving_fails(
        self, async_request_mock: Any, mocker: MockerFixture
    ) -> None:
        """Test that the client is closed in a finally, even when the serve loop itself raises - the
        failure still propagates, since a live-connection failure is a genuine crash, not a usage error
        """
        client, mcp_server = await prepare(CliTestClient, ServerOptions(base_url=_BASE_URL))
        mocker.patch("api_client_core.mcp.server.stdio_server", side_effect=RuntimeError("transport broke"))
        aclose_spy = mocker.spy(client, "aclose")
        with pytest.raises(RuntimeError, match="transport broke"):
            await serve_connection(client, mcp_server)
        aclose_spy.assert_called_once()


class TestBuildServer:
    """Tests for `_build_server()`'s handler wiring and capability/instructions derivation"""

    async def test_server_name_includes_the_app_name(self) -> None:
        """Test that the server's name is derived from the served app's app_name"""
        client = await _construct_client(CliTestClient, ServerOptions(base_url=_BASE_URL))
        catalog = build_catalog(CliTestClient, ServerOptions())
        server = _build_server(client, catalog, ServerOptions())
        assert catalog.app_name in server.name

    async def test_capabilities_advertise_tools_only(self) -> None:
        """Test that the server advertises the tools capability, and nothing else - no
        resources/prompts/logging/completions
        """
        client = await _construct_client(CliTestClient, ServerOptions(base_url=_BASE_URL))
        catalog = build_catalog(CliTestClient, ServerOptions())
        server = _build_server(client, catalog, ServerOptions())
        capabilities = server.get_capabilities()
        assert capabilities.tools is not None
        assert capabilities.resources is None
        assert capabilities.prompts is None
        assert capabilities.logging is None

    async def test_instructions_name_static_mode(self) -> None:
        """Test that the instructions text distinguishes static mode from dynamic mode"""
        client = await _construct_client(CliTestClient, ServerOptions(base_url=_BASE_URL))
        catalog = build_catalog(CliTestClient, ServerOptions())
        assert catalog.mode is Mode.STATIC
        server = _build_server(client, catalog, ServerOptions())
        assert server.instructions is not None
        assert "individual tools" in server.instructions

    async def test_instructions_name_dynamic_mode_and_the_workflow(self) -> None:
        """Test that dynamic mode's instructions name the search -> describe -> call workflow"""
        client = await _construct_client(CliTestClient, ServerOptions(threshold=1, base_url=_BASE_URL))
        catalog = build_catalog(CliTestClient, ServerOptions(threshold=1))
        assert catalog.mode is Mode.DYNAMIC
        server = _build_server(client, catalog, ServerOptions(threshold=1))
        assert server.instructions is not None
        assert "search_endpoints" in server.instructions
        assert "describe_endpoint" in server.instructions
        assert "call_endpoint" in server.instructions


class TestServeStdoutOrdering:
    """Tests for the ordering `serve()` actually depends on for stdio safety: on stdio transport,
    stdout IS the JSON-RPC channel, and the stdlib-level `reserve_stdout()`/`stdio_server()`'s
    stronger, OS-file-descriptor-level claim must never both be active at once - the former must be
    gone by the time the latter is entered, since `stdio_server()`, given no explicit streams, needs
    to see the genuine, already-restored stdout to claim it itself.
    """

    async def test_pre_serve_reservation_is_exited_before_stdio_server_is_entered(
        self, mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Test that `serve()`'s `reserve_stdout()` block covers catalog build and client
        construction (a stray print() there is redirected to stderr), and is fully exited - `sys.stdout`
        back to the real stream - before `stdio_server()` is ever entered.
        """
        real_stdout = sys.stdout
        seen_stdout_at_stdio_server: list[Any] = []

        def fake_build_catalog(client_class: Any, options: Any) -> Any:
            print("printed during pre-serve catalog build")  # noqa: T201 - the exact hazard under test
            return build_catalog(client_class, options)

        @asynccontextmanager
        async def fake_stdio_server() -> AsyncIterator[tuple[Any, Any]]:
            seen_stdout_at_stdio_server.append(sys.stdout)
            yield (mocker.AsyncMock(), mocker.AsyncMock())

        mocker.patch("api_client_core.mcp.server.build_catalog", side_effect=fake_build_catalog)
        mocker.patch("api_client_core.mcp.server.stdio_server", fake_stdio_server)
        mocker.patch("api_client_core.mcp.server.Server.run", new=mocker.AsyncMock())

        await serve(CliTestClient, ServerOptions(base_url=_BASE_URL))

        assert seen_stdout_at_stdio_server == [real_stdout]
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "printed during pre-serve catalog build" in captured.err

    async def test_print_inside_a_tool_handler_lands_on_stderr_under_an_explicit_reservation(
        self, capsys: pytest.CaptureFixture[str], async_request_mock: Any, mocker: Any
    ) -> None:
        """Test that `reserve_stdout()`/`real_stdout()` - the same primitive `serve()` uses for its own
        pre-serve phase - redirects a stray print() to stderr when a caller holds it explicitly around
        dispatch, e.g. to exercise dispatch logic in isolation, as this test does. During a real serve()
        run this reservation is not what's active at dispatch time - see the ordering test above for
        what actually is.
        """
        async_request_mock.return_value = make_httpx_response(mocker, 200, json_body={"id": 1})
        client = await _construct_client(CliTestClient, ServerOptions(base_url=_BASE_URL))
        catalog = build_catalog(CliTestClient, ServerOptions())

        with reserve_stdout():
            print("this must not corrupt the JSON-RPC stream")  # noqa: T201 - the exact hazard under test
            result = await _dispatch_tool_call(
                client, catalog, "widgets__get_widget", {"widget_id": 1}, ServerOptions()
            )

        captured = capsys.readouterr()
        assert captured.out == ""
        assert "this must not corrupt the JSON-RPC stream" in captured.err
        assert result.is_error is False

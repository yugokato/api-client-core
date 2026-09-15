"""Server lifecycle layer: client construction, the stdio stdout-protection ordering, and MCP handler
wiring.

The only module besides `tools.py` that imports the `mcp` SDK.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from typing import Any

from common_libs.logging import get_logger
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .. import __version__
from .._common.console import format_error_message, reserve_stdout
from ..core.base import APIClient
from ._constants import Mode
from .catalog import EndpointCatalog, ServerOptions, build_catalog
from .dynamic_tools import dispatch_dynamic_tool, error_result
from .runner import ToolResult, dispatch_endpoint_call
from .tools import build_dynamic_tools, build_static_tools
from .wrappers import split_call_wrappers

logger = get_logger(__name__)


async def serve(client_class: type[APIClient], options: ServerOptions) -> int:
    """Build the catalog, construct the client, and serve MCP requests over stdio until the connection
    closes.

    A thin convenience combining `prepare()` and `serve_connection()`, for a caller that has no need to
    treat the two differently - a caller that does (distinguishing a startup failure from a live one)
    calls them separately instead.

    :param client_class: Concrete `APIClient` subclass to serve
    :param options: Resolved server options
    """
    client, mcp_server = await prepare(client_class, options)
    return await serve_connection(client, mcp_server)


async def prepare(client_class: type[APIClient], options: ServerOptions) -> tuple[APIClient, Server[Any]]:
    """Build the catalog, construct the client, and wire the MCP server around them - every step that
    can fail as a usage error, before the stdio transport ever claims the real stdout.

    A failure once the client is constructed closes it before propagating, so a startup failure never
    leaks the underlying HTTP client's connection pool. Runs entirely inside a stdout reservation, exited
    before this returns and well before `serve_connection()` ever enters the stdio transport.

    :param client_class: Concrete `APIClient` subclass to serve
    :param options: Resolved server options
    """
    with reserve_stdout():
        catalog = build_catalog(client_class, options)
        client = await _construct_client(client_class, options)
        try:
            mcp_server = _build_server(client, catalog, options)
        except Exception:
            await client.aclose()
            raise
    return client, mcp_server


async def serve_connection(client: APIClient, server: Server[Any]) -> int:
    """Serve MCP requests over stdio on an already-built client/server pair, until the connection closes.

    Must only be entered once the stdout reservation from `prepare()` has already been exited: the stdio
    transport, given no explicit streams, claims the process's real stdin/stdout itself at the OS
    file-descriptor level, so it must see the genuine, already-restored stdout rather than one still
    redirected by an outer reservation.

    :param client: Constructed, async-mode API client to dispatch tool calls against
    :param server: Already-wired MCP server, from `prepare()`
    """
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        await client.aclose()
    return 0


async def _construct_client(client_class: type[APIClient], options: ServerOptions) -> APIClient:
    """Construct `client_class` in async mode and apply the MCP-specific defaults.

    `async_mode=True` isn't a preference: the client raises `RuntimeError` for `async_mode=False` inside
    a running event loop, and this server always runs inside one. Whether the constructor even accepts
    `async_mode` is checked up front, so an incompatible client gets an actionable error naming the
    requirement rather than a confusing `TypeError`.

    :param client_class: Concrete `APIClient` subclass to construct
    :param options: Resolved server options
    """
    if not _accepts_kwarg(client_class.__init__, "async_mode"):
        raise RuntimeError(
            f"{client_class.__name__} must accept async_mode=True - the MCP server always runs inside an event loop."
        )
    kwargs: dict[str, Any] = {"async_mode": True}
    if options.base_url:
        kwargs["base_url"] = options.base_url
    client = client_class(**kwargs)
    if not client.async_mode:
        await _close_client(client)
        raise RuntimeError(f"{client_class.__name__} did not construct in async mode.")

    try:
        _apply_headers(client, options.headers)
    except Exception:
        await _close_client(client)
        raise
    if not options.log_requests:
        # An MCP host typically surfaces server stderr in a log pane, where a full request/response
        # block per tool call is just noise. --log-requests opts back in.
        client.rest_client.log_requests = False
    return client


async def _close_client(client: APIClient) -> None:
    """Close `client`, using whichever of `close()`/`aclose()` actually matches the mode it landed in.

    A client that failed the async-mode check is still in sync mode at that point, and calling the wrong
    one raises `TypeError`.

    :param client: The client to close
    """
    if client.async_mode:
        await client.aclose()
    else:
        client.close()


def _apply_headers(client: APIClient, headers: tuple[tuple[str, str], ...]) -> None:
    """Apply `-H`/`--header` values to an already-constructed client's underlying httpx2 client.

    An explicit header named `Authorization` (case-insensitively) first clears any auth the client
    installed for itself, since that auth is otherwise applied on every request and would silently
    override an explicit header given here. Applied post-construction, so a client that issues its
    request from `__init__` (a login call, say) predates this switch.

    :param client: Instantiated, async-mode API client to apply the headers to
    :param headers: `(name, value)` pairs, in the order given
    """
    if not headers:
        return
    if any(name.lower() == "authorization" for name, _ in headers):
        client.rest_client.auth = None
    client.rest_client.client.headers.update(headers)


def _build_server(client: APIClient, catalog: EndpointCatalog, options: ServerOptions) -> Server[Any]:
    """Wire an `mcp.server.Server` around a built catalog and a constructed client.

    Handlers are supplied as constructor keyword arguments, not registered via decorator methods on an
    already-built instance. The server auto-derives which capabilities to advertise from which handlers
    were actually passed here.

    :param client: Constructed, async-mode API client to dispatch tool calls against
    :param catalog: The resolved endpoint catalog
    :param options: Resolved server options
    """
    if catalog.mode is Mode.STATIC:
        tool_list = build_static_tools(catalog, options)
    else:
        tool_list = build_dynamic_tools(catalog, options)

    async def on_list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tool_list)

    async def on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        return await _dispatch_tool_call(client, catalog, params.name, params.arguments or {}, options)

    return Server(
        name=f"api-client:{catalog.app_name}",
        version=__version__,
        instructions=_instructions(catalog),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


def _accepts_kwarg(func: Callable[..., Any], name: str) -> bool:
    """Return whether `func`'s signature would accept `name` as a keyword argument, either by
    declaring it explicitly or by accepting arbitrary ones via `**kwargs`.

    :param func: Callable whose signature to inspect
    :param name: Keyword argument name to check for
    """
    params = inspect.signature(func).parameters
    if name in params and params[name].kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    ):
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


async def _dispatch_tool_call(
    client: APIClient, catalog: EndpointCatalog, name: str, arguments: dict[str, Any], options: ServerOptions
) -> types.CallToolResult:
    """Route one `call_tool` request to the right handler and shape its result, catching any exception
    the dispatched handler didn't anticipate itself.

    An exception escaping this function would become a JSON-RPC protocol error in most MCP hosts, not a
    recoverable failure a model can act on, so every path through it returns a `CallToolResult`, never
    raises.

    :param client: Constructed, async-mode API client
    :param catalog: The resolved endpoint catalog
    :param name: The tool name from the request
    :param arguments: The tool call's raw JSON arguments
    :param options: Resolved server options
    """
    try:
        if catalog.mode is Mode.STATIC:
            entry = catalog.by_name.get(name)
            if entry is None:
                result = error_result(f"Unknown tool: {name}")
            else:
                # The reserved call_wrappers key rides inside a static tool's own arguments; dynamic
                # mode reads it as its own top-level property on call_endpoint instead.
                endpoint_args, call_wrappers = split_call_wrappers(arguments)
                result = await dispatch_endpoint_call(
                    client, entry, endpoint_args, options, mode=catalog.mode, call_wrappers=call_wrappers
                )
        else:
            result = await dispatch_dynamic_tool(client, catalog, name, arguments, options)
    except Exception as e:
        logger.exception(f"Unhandled error dispatching tool {name!r}")
        result = error_result(_format_exception(e))
    return _to_call_tool_result(result)


def _instructions(catalog: EndpointCatalog) -> str:
    """Build the server's `instructions` text: the highest-leverage field for teaching a model the
    dynamic-mode workflow without spending a tool call on it.

    :param catalog: The resolved endpoint catalog
    """
    lines = [f"This server exposes {catalog.app_name}'s API, generated directly from its endpoint definitions."]
    if catalog.mode is Mode.STATIC:
        lines.append(f"{len(catalog.entries)} endpoint(s) are published as individual tools.")
    else:
        lines.append(
            f"{len(catalog.entries)} endpoint(s) are available through four tools instead of one each: call "
            "search_endpoints to find an endpoint_id, describe_endpoint for its full input schema, then "
            "call_endpoint to dispatch it. list_resources gives a starting overview."
        )
    lines.append(
        "Any call accepts an optional call_wrappers object (retry, rate limiting, expected-status "
        "assertions, repeat, concurrency, stats). with_repeat/with_concurrency return a list of responses."
    )
    return "\n".join(lines)


def _to_call_tool_result(result: ToolResult) -> types.CallToolResult:
    """Convert a `ToolResult` into the SDK's own `CallToolResult`.

    `structured` is dropped, falling back to text-only content, if it somehow isn't JSON-safe: the SDK
    would otherwise raise well after dispatch has already returned, taking down the whole connection
    instead of surfacing as one failed tool call.

    :param result: The dispatched call's result
    """
    structured = result.structured
    if structured is not None:
        try:
            json.dumps(structured)
        except (TypeError, ValueError):
            logger.warning("Dropping non-JSON-safe structuredContent from a tool result")
            structured = None
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=result.content)],
        structured_content=structured,
        is_error=result.is_error,
    )


def _format_exception(exc: BaseException) -> str:
    """Format a caught exception through the shared `format_error_message()`: a bare message for
    `LookupError`/`RuntimeError` (this package's self-descriptive usage-failure types), `Type: message`
    for anything else.

    :param exc: The exception to format
    """
    return format_error_message(exc)

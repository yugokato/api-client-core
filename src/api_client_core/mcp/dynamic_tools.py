"""Dynamic-mode dispatch layer: routes one `call_tool` request to the right fixed meta-tool handler
(`list_resources`/`search_endpoints`/`describe_endpoint`/`call_endpoint`) and builds ToolResult` for each one.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from api_client_core.core.endpoints.utils.docstring import get_first_doc_line

from ._constants import (
    CALL_WRAPPERS_KEY,
    SEARCH_ENDPOINTS_DEFAULT_LIMIT,
    SEARCH_ENDPOINTS_MAX_LIMIT,
    SEARCH_ENDPOINTS_MIN_LIMIT,
    SEARCH_ENDPOINTS_MIN_OFFSET,
    DynamicTool,
)
from .catalog import CatalogEntry, EndpointCatalog, ServerOptions
from .runner import ToolResult, dispatch_endpoint_call
from .schema import annotations_for, build_input_schema, tool_description, tool_title
from .wrappers import call_wrappers_schema

if TYPE_CHECKING:
    from ..core.base import APIClient

# Each fixed meta-tool's published input_schema, as a plain SDK-free dict.
DYNAMIC_TOOL_SCHEMAS: dict[DynamicTool, dict[str, Any]] = {
    DynamicTool.LIST_RESOURCES: {"type": "object", "properties": {}, "additionalProperties": False},
    DynamicTool.SEARCH_ENDPOINTS: {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Case-insensitive keyword to match"},
            "resource": {"type": "string", "description": "Restrict to one resource group"},
            "method": {"type": "string", "description": "Restrict to one HTTP method"},
            "limit": {
                "type": "integer",
                "default": SEARCH_ENDPOINTS_DEFAULT_LIMIT,
                "minimum": SEARCH_ENDPOINTS_MIN_LIMIT,
                "maximum": SEARCH_ENDPOINTS_MAX_LIMIT,
            },
            "offset": {
                "type": "integer",
                "default": SEARCH_ENDPOINTS_MIN_OFFSET,
                "minimum": SEARCH_ENDPOINTS_MIN_OFFSET,
            },
        },
        "additionalProperties": False,
    },
    DynamicTool.DESCRIBE_ENDPOINT: {
        "type": "object",
        "properties": {"endpoint_id": {"type": "string"}},
        "required": ["endpoint_id"],
        "additionalProperties": False,
    },
    DynamicTool.CALL_ENDPOINT: {
        "type": "object",
        "properties": {
            "endpoint_id": {"type": "string"},
            "arguments": {
                "type": "object",
                "default": {},
                "description": "This endpoint's arguments, per describe_endpoint's input_schema",
            },
            CALL_WRAPPERS_KEY: call_wrappers_schema(),
        },
        "required": ["endpoint_id"],
        "additionalProperties": False,
    },
}

# Each fixed meta-tool's accepted top-level argument keys, derived from DYNAMIC_TOOL_SCHEMAS above.
_DYNAMIC_TOOL_KEYS: dict[DynamicTool, frozenset[str]] = {
    tool: frozenset(schema.get("properties", ())) for tool, schema in DYNAMIC_TOOL_SCHEMAS.items()
}


async def dispatch_dynamic_tool(
    client: APIClient, catalog: EndpointCatalog, name: str, arguments: dict[str, Any], options: ServerOptions
) -> ToolResult:
    """Route one dynamic-mode meta-tool call to its handler.

    :param client: Constructed, async-mode API client
    :param catalog: The resolved endpoint catalog
    :param name: The tool name from the request
    :param arguments: The tool call's raw JSON arguments
    :param options: Resolved server options
    """
    try:
        tool = DynamicTool(name)
    except ValueError:
        tool = None
    if tool is None:
        if name in catalog.by_name:
            # Likely a model treating an endpoint_id from search_endpoints as a static-mode tool name.
            return error_result(
                f"This server is in dynamic mode. Call call_endpoint with endpoint_id={name!r} instead of "
                f"calling {name!r} as a tool name directly."
            )
        return error_result(f"Unknown tool: {name}")

    error = _unknown_keys_error(name, arguments, _DYNAMIC_TOOL_KEYS[tool])
    if error is not None:
        return error

    if tool is DynamicTool.LIST_RESOURCES:
        return _json_result(_resource_summary_payload(catalog))
    if tool is DynamicTool.SEARCH_ENDPOINTS:
        return _search_endpoints_result(catalog, arguments)

    # describe_endpoint and call_endpoint share the same endpoint_id lookup.
    entry = _entry_for(catalog, arguments.get("endpoint_id"))
    if entry is None:
        return error_result(_unknown_endpoint_id_error(arguments.get("endpoint_id")))
    if tool is DynamicTool.DESCRIBE_ENDPOINT:
        return _json_result(_describe_endpoint_payload(catalog, entry, options))

    # DynamicTool.CALL_ENDPOINT: the only member left, once every other branch above has returned.
    call_arguments = arguments.get("arguments")
    if call_arguments is None:
        call_arguments = {}
    elif not isinstance(call_arguments, dict):
        return error_result("'arguments' must be an object")
    return await dispatch_endpoint_call(
        client, entry, call_arguments, options, mode=catalog.mode, call_wrappers=arguments.get(CALL_WRAPPERS_KEY)
    )


def error_result(message: str) -> ToolResult:
    """Wrap a one-line message as a failed `ToolResult`.

    :param message: Human/model-readable failure detail
    """
    return ToolResult(content=message, structured=None, is_error=True)


def _unknown_endpoint_id_error(endpoint_id: Any) -> str:
    """Build the error message for an `endpoint_id` that doesn't resolve to any catalog entry, pointing
    at `search_endpoints` rather than leaving a model to guess where a valid one comes from.

    :param endpoint_id: The raw `endpoint_id` argument that failed to resolve
    """
    return f"Unknown endpoint_id: {endpoint_id!r}. Call search_endpoints to find a valid one."


def _unknown_keys_error(tool_name: str, arguments: dict[str, Any], allowed: frozenset[str]) -> ToolResult | None:
    """Return an `isError` `ToolResult` naming any top-level key `arguments` carries that `tool_name`
    doesn't accept, or `None` if every key given is allowed.

    This is the only place an unrecognized top-level key is caught, since the schema itself isn't
    enforced for us - most likely a model forgetting to nest `call_endpoint`'s arguments under
    `"arguments"` and sending them at the top level instead.

    :param tool_name: The dynamic-mode meta-tool name the call targets
    :param arguments: The tool call's raw JSON arguments
    :param allowed: The set of top-level keys this tool's schema declares
    """
    unknown = sorted(set(arguments) - allowed)
    if not unknown:
        return None
    return error_result(f"Unknown argument(s) for {tool_name}: {', '.join(unknown)}.")


def _search_endpoints_result(catalog: EndpointCatalog, arguments: dict[str, Any]) -> ToolResult:
    """Handle `search_endpoints`: filter, rank, and page the catalog.

    `limit`/`offset` are clamped to their published bounds here rather than merely relied on to be
    honored, since an out-of-range value reaching here directly would otherwise return the entire catalog
    or a tail slice.

    :param catalog: The resolved endpoint catalog
    :param arguments: The tool call's raw JSON arguments
    """
    raw_limit = arguments.get("limit")
    raw_offset = arguments.get("offset")
    try:
        limit = SEARCH_ENDPOINTS_DEFAULT_LIMIT if raw_limit is None else int(raw_limit)
        offset = SEARCH_ENDPOINTS_MIN_OFFSET if raw_offset is None else int(raw_offset)
    except (TypeError, ValueError):
        return error_result("'limit' and 'offset' must be integers")
    limit = min(max(limit, SEARCH_ENDPOINTS_MIN_LIMIT), SEARCH_ENDPOINTS_MAX_LIMIT)
    offset = max(offset, SEARCH_ENDPOINTS_MIN_OFFSET)

    candidates = list(catalog.entries)
    resource = arguments.get("resource")
    if resource:
        candidates = [e for e in candidates if e.resource == resource]
    method = arguments.get("method")
    if method:
        candidates = [e for e in candidates if e.endpoint.method == str(method).lower()]
    query = arguments.get("query")
    if query:
        candidates = _ranked_search(candidates, str(query))

    total = len(candidates)
    page = candidates[offset : offset + limit]
    results = [_endpoint_summary(e) for e in page]
    return _json_result({"total": total, "offset": offset, "limit": limit, "results": results})


def _ranked_search(entries: list[CatalogEntry], query: str) -> list[CatalogEntry]:
    """Rank entries by exact endpoint_id match, then id-substring, then path-substring, then
    summary-substring, dropping anything matching none of those. Ties break by `(resource, tool_name)`.

    :param entries: Candidate entries, already filtered by resource/method
    :param query: Case-insensitive search term
    """
    needle = query.lower()

    def rank(entry: CatalogEntry) -> int:
        name = entry.tool_name.lower()
        if name == needle:
            return 0
        if needle in name:
            return 1
        if needle in entry.endpoint.path.lower():
            return 2
        if needle in tool_title(entry.endpoint).lower():
            return 3
        return 4

    scored = [(rank(e), e) for e in entries]
    matched = [(score, e) for score, e in scored if score < 4]
    matched.sort(key=lambda pair: (pair[0], pair[1].resource, pair[1].tool_name))
    return [e for _, e in matched]


def _resource_summary_payload(catalog: EndpointCatalog) -> dict[str, Any]:
    """Build `list_resources`' payload: one entry per resource group, with its own endpoint count.

    :param catalog: The resolved endpoint catalog
    """
    resources = [
        {
            "resource": name,
            "endpoint_count": len(entries),
            "summary": get_first_doc_line(entries[0].api_class.__doc__),
            "methods": sorted({e.endpoint.method for e in entries}),
        }
        for name, entries in sorted(catalog.resources.items())
    ]
    return {"resources": resources}


def _endpoint_summary(entry: CatalogEntry) -> dict[str, Any]:
    """Build one endpoint's one-line summary, used by `search_endpoints`' paged results.

    :param entry: The catalog entry to summarize
    """
    return {
        "endpoint_id": entry.tool_name,
        "resource": entry.resource,
        "method": entry.endpoint.method,
        "path": entry.endpoint.path,
        "summary": tool_title(entry.endpoint),
        "deprecated": entry.endpoint.is_deprecated,
    }


def _describe_endpoint_payload(catalog: EndpointCatalog, entry: CatalogEntry, options: ServerOptions) -> dict[str, Any]:
    """Build one endpoint's full metadata payload, returned by `describe_endpoint`.

    Carries one `input_schema` rather than also a separate `parameters` block, since every property
    already states its own required/deprecated/description/location inline.

    :param catalog: The resolved endpoint catalog, holding the per-server schema cache
    :param entry: The catalog entry to describe
    :param options: Resolved server options
    """
    endpoint = entry.endpoint
    return {
        "endpoint_id": entry.tool_name,
        "resource": entry.resource,
        "method": endpoint.method,
        "path": endpoint.path,
        "summary": tool_title(endpoint),
        "description": tool_description(endpoint),
        "deprecated": endpoint.is_deprecated,
        "public": endpoint.is_public,
        "documented": endpoint.is_documented,
        "input_schema": _cached_input_schema(catalog, entry, options),
        "annotations": annotations_for(endpoint.method),
    }


def _cached_input_schema(catalog: EndpointCatalog, entry: CatalogEntry, options: ServerOptions) -> dict[str, Any]:
    """Return `entry`'s input schema, building it once per server and reusing it after.

    The first build logs any skipped or unmapped parameter; a cache hit skips both the rebuild and the
    duplicate diagnostic. The cached dict is treated as read-only (serialized, never mutated).

    :param catalog: The resolved endpoint catalog, holding the per-server schema cache
    :param entry: The catalog entry whose schema to return
    :param options: Resolved server options
    """
    cached = catalog.input_schema_cache.get(entry.tool_name)
    if cached is None:
        cached = build_input_schema(entry.endpoint, options, warn=True)
        catalog.input_schema_cache[entry.tool_name] = cached
    return cached


def _entry_for(catalog: EndpointCatalog, endpoint_id: Any) -> CatalogEntry | None:
    """Look up a catalog entry by `endpoint_id`, tolerating a missing or non-string value.

    :param catalog: The resolved endpoint catalog
    :param endpoint_id: The raw `endpoint_id` argument
    """
    if not isinstance(endpoint_id, str):
        return None
    return catalog.by_name.get(endpoint_id)


def _json_result(payload: dict[str, Any]) -> ToolResult:
    """Wrap a plain payload dict as a successful `ToolResult`.

    `default=str` covers a value that isn't itself JSON-safe (e.g. an `Enum` member or `bytes`), so one
    such value degrades gracefully instead of raising for the whole payload. `structured` is derived from
    the same rendered text via a JSON round trip, so it's guaranteed to match `content` and be JSON-safe.

    :param payload: The payload to wrap, not required to already be JSON-safe
    """
    content = json.dumps(payload, indent=2, default=str)
    return ToolResult(content=content, structured=json.loads(content), is_error=False)

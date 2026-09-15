"""Tool-definition layer: `EndpointCatalog` -> `mcp.types.Tool` objects, including the four fixed
dynamic-mode meta-tools.

The only module besides `server.py` that imports the `mcp` SDK's types, so an SDK API change can only
ever break these two files.
"""

from __future__ import annotations

from typing import Any

from common_libs.logging import get_logger
from mcp import types

from ._constants import DESTRUCTIVE_METHODS, IDEMPOTENT_METHODS, READ_ONLY_METHODS, DynamicTool, ResponseFormat
from .catalog import EndpointCatalog, ServerOptions
from .dynamic_tools import DYNAMIC_TOOL_SCHEMAS
from .schema import annotations_for, build_input_schema, tool_description, tool_title

logger = get_logger(__name__)

# Admits both result shapes - the single {status_code, headers, body} envelope, and a repeat/concurrency
# {results: [...]} list - as an anyOf over required, since the MCP contract requires type: object at the
# root. results is left unconstrained, like body, so a truncation marker validates in its place.
_RESULT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status_code": {"type": "integer"},
        "headers": {"type": "object", "additionalProperties": {"type": "string"}},
        "body": {},
        "results": {},
        "stats": {"type": "array"},
    },
    "anyOf": [
        {"required": ["status_code", "headers", "body"]},
        {"required": ["results"]},
    ],
}

# The three read-only meta-tools never reach an external HTTP API - they read the in-memory catalog -
# so open_world_hint is False here, unlike annotations_for()'s always-True (every endpoint it describes does reach one).
_READ_ONLY_META_TOOL_ANNOTATIONS = types.ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


def build_static_tools(catalog: EndpointCatalog, options: ServerOptions) -> list[types.Tool]:
    """Build one `types.Tool` per catalog entry, for static mode.

    An entry that fails to build is skipped with a warning rather than aborting the whole server, since
    an MCP host runs this unattended. Raises `RuntimeError` only if every entry fails.

    :param catalog: The resolved endpoint catalog
    :param options: Resolved server options
    """
    output_schema = _output_schema_for(options)
    tools = []
    for entry in catalog.entries:
        endpoint = entry.endpoint
        try:
            tools.append(
                types.Tool(
                    name=entry.tool_name,
                    title=tool_title(endpoint),
                    description=tool_description(endpoint),
                    input_schema=build_input_schema(endpoint, options, warn=True, with_call_wrappers=True),
                    output_schema=output_schema,
                    annotations=types.ToolAnnotations(**annotations_for(endpoint.method)),
                )
            )
        except Exception as e:
            logger.warning(f"Skipping {endpoint} ({entry.tool_name!r}): unable to build its tool: {e}")
    if not tools:
        raise RuntimeError(f"None of {len(catalog.entries)} endpoint(s) could be built as MCP tools.")
    return tools


def build_dynamic_tools(catalog: EndpointCatalog, options: ServerOptions) -> list[types.Tool]:
    """Build the four fixed meta-tools, for dynamic mode.

    :param catalog: The resolved endpoint catalog, used only to compute `call_endpoint`'s annotations
                    from the actual filtered method set
    :param options: Resolved server options
    """
    return [
        types.Tool(
            name=DynamicTool.LIST_RESOURCES.value,
            title="List resources",
            description=(
                "List every resource group this server exposes, with how many endpoints each has. Start here "
                "to get oriented."
            ),
            input_schema=DYNAMIC_TOOL_SCHEMAS[DynamicTool.LIST_RESOURCES],
            annotations=_READ_ONLY_META_TOOL_ANNOTATIONS,
        ),
        types.Tool(
            name=DynamicTool.SEARCH_ENDPOINTS.value,
            title="Search endpoints",
            description=(
                "Search this server's endpoint catalog by keyword, resource, or HTTP method. Returns a page of "
                "matching endpoint_ids with a one-line summary each - pass one to describe_endpoint for its full "
                "schema, or straight to call_endpoint to dispatch it."
            ),
            input_schema=DYNAMIC_TOOL_SCHEMAS[DynamicTool.SEARCH_ENDPOINTS],
            annotations=_READ_ONLY_META_TOOL_ANNOTATIONS,
        ),
        types.Tool(
            name=DynamicTool.DESCRIBE_ENDPOINT.value,
            title="Describe endpoint",
            description="Get one endpoint's full metadata and input schema, by endpoint_id.",
            input_schema=DYNAMIC_TOOL_SCHEMAS[DynamicTool.DESCRIBE_ENDPOINT],
            annotations=_READ_ONLY_META_TOOL_ANNOTATIONS,
        ),
        types.Tool(
            name=DynamicTool.CALL_ENDPOINT.value,
            title="Call endpoint",
            description="Dispatch a real API call to one endpoint, by endpoint_id, with its arguments.",
            input_schema=DYNAMIC_TOOL_SCHEMAS[DynamicTool.CALL_ENDPOINT],
            output_schema=_output_schema_for(options),
            annotations=_call_endpoint_annotations(catalog),
        ),
    ]


def _call_endpoint_annotations(catalog: EndpointCatalog) -> types.ToolAnnotations:
    """Compute `call_endpoint`'s annotations from the actual filtered endpoint set, so e.g.
    `--read-only` makes it honestly advertise itself as read-only too, rather than a hard-coded worst
    case.

    :param catalog: The resolved endpoint catalog
    """
    methods = {entry.endpoint.method for entry in catalog.entries}
    return types.ToolAnnotations(
        read_only_hint=methods.issubset(READ_ONLY_METHODS),
        destructive_hint=bool(methods & set(DESTRUCTIVE_METHODS)),
        idempotent_hint=methods.issubset(IDEMPOTENT_METHODS),
        open_world_hint=True,
    )


def _output_schema_for(options: ServerOptions) -> dict[str, Any] | None:
    """Return the `outputSchema` to publish for a tool call result, or `None` when the response shape
    isn't uniform enough to declare one.

    Only `full` (the default `--response-format`) has one fixed shape across every tool - `json` returns
    each endpoint's own arbitrary body shape, and `raw` returns undecoded text, neither of which is
    worth generalizing into a schema.

    :param options: Resolved server options
    """
    if options.response_format is ResponseFormat.FULL:
        return _RESULT_OUTPUT_SCHEMA
    return None

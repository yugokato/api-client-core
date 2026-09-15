"""Vocabulary shared across the MCP server generator.

Stays stdlib-only, since it's imported at module scope on the fast `--help`/`--version` path, before the
`mcp` SDK or the user's project is known to be importable. Must never import from another `mcp/` module or
any other `api_client_core.*` module.
"""

from __future__ import annotations

from enum import StrEnum

PROG = "api-client-mcp"


# Below this many endpoints (after filtering), auto mode exposes one tool per endpoint (static). At or
# above it, auto mode exposes the four fixed dynamic-mode meta-tools instead.
DEFAULT_THRESHOLD = 50
# Joins a resource attribute name and an endpoint function name into one tool name, e.g.
# "products__get_product". Also serves as the `endpoint_id` token in dynamic mode.
TOOL_NAME_SEP = "__"
# A tool name derived past this length is truncated and given a deterministic hash suffix instead.
MAX_TOOL_NAME_LEN = 64
# Default cap on a base64-encoded File parameter's decoded size, in bytes.
DEFAULT_MAX_FILE_BYTES = 10 * 1024 * 1024
# Fixed cap on a dispatched call's rendered response body, in bytes, with no CLI override: an oversized
# body is replaced with a truncation marker rather than sent whole in one JSON-RPC frame.
MAX_RESULT_BYTES = 256 * 1024
# Length of the text preview kept in a truncation marker, in characters.
TRUNCATION_PREVIEW_CHARS = 2000
# Per-value cap applied to a header when truncating body alone wasn't enough to bring a result
# under MAX_RESULT_BYTES (only reachable via --all-headers against a response carrying an
# unusually large header block). Combined with the small, fixed allowlist this falls back to, the
# result stays well under the cap regardless of how large a single header value was.
MAX_HEADER_VALUE_CHARS = 500
# The reserved tool-argument key carrying a call's with_xxx() wrappers.
CALL_WRAPPERS_KEY = "call_wrappers"
# search_endpoints' limit/offset bounds, enforced at dispatch time since the published schema alone
# isn't validated for us.
SEARCH_ENDPOINTS_DEFAULT_LIMIT = 20
SEARCH_ENDPOINTS_MIN_LIMIT = 1
SEARCH_ENDPOINTS_MAX_LIMIT = 100
SEARCH_ENDPOINTS_MIN_OFFSET = 0
# HTTP methods an endpoint's MCP tool annotations are derived from.
READ_ONLY_METHODS = ("get", "head", "options", "trace")
IDEMPOTENT_METHODS = (*READ_ONLY_METHODS, "put", "delete")
DESTRUCTIVE_METHODS = ("put", "patch", "delete")
# Response headers kept in a tool call's result envelope by default: an allowlist, not a denylist, since most response
# headers are operational noise a model can't act on and a few (e.g. Set-Cookie, Authorization) must never reach it.
# Lower-case, matched case-insensitively.
INCLUDED_HEADERS: frozenset[str] = frozenset(
    {
        "content-type",
        "content-length",
        "location",
        "retry-after",
        "etag",
        "last-modified",
        "www-authenticate",
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "x-ratelimit-reset",
        "ratelimit-limit",
        "ratelimit-remaining",
        "ratelimit-reset",
        "x-request-id",
    }
)


class Mode(StrEnum):
    """Accepted `--mode` values, controlling how endpoints are exposed as MCP tools."""

    STATIC = "static"
    DYNAMIC = "dynamic"
    AUTO = "auto"


class DynamicTool(StrEnum):
    """The four fixed meta-tools published in dynamic mode."""

    LIST_RESOURCES = "list_resources"
    SEARCH_ENDPOINTS = "search_endpoints"
    DESCRIBE_ENDPOINT = "describe_endpoint"
    CALL_ENDPOINT = "call_endpoint"


class ResponseFormat(StrEnum):
    """Accepted `--response-format` values, controlling how a tool call's result content is rendered."""

    FULL = "full"
    JSON = "json"
    RAW = "raw"


class Flag(StrEnum):
    """Every long option string the MCP server registers for itself.

    `-h`/`--help` is omitted: argparse's `ArgumentParser` registers it itself (this file never sets
    `add_help=False`), so there's no `add_argument()` call here that would need the constant.
    """

    VERSION = "--version"
    MODE = "--mode"
    THRESHOLD = "--threshold"
    RESOURCE = "--resource"
    INCLUDE = "--include"
    EXCLUDE = "--exclude"
    METHOD = "--method"
    READ_ONLY = "--read-only"
    TOOL_PREFIX = "--tool-prefix"
    ALLOW_FILE_PATHS = "--allow-file-paths"
    MAX_FILE_BYTES = "--max-file-bytes"
    ALL_HEADERS = "--all-headers"
    RESPONSE_FORMAT = "--response-format"
    BASE_URL = "--base-url"
    HEADER = "--header"
    LOG_LEVEL = "--log-level"
    LOG_REQUESTS = "--log-requests"

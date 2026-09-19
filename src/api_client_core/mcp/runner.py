"""Dispatch layer: an MCP tool call's raw JSON arguments -> an awaited endpoint call -> a result envelope.

Never raises for an anticipated failure (a bad argument, a non-2xx response, or a `raise_on_error`
client's `HTTPStatusError`) - each becomes a `ToolResult` with `is_error=True`. An unexpected exception is
left to propagate rather than be silently turned into a misleading result.
"""

from __future__ import annotations

import base64
import binascii
import inspect
import json
import mimetypes
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import UnionType
from typing import TYPE_CHECKING, Any, Union, get_args, get_origin

from common_libs.clients.rest_client.utils import format_request_failure, get_request_from_exception, mask_sensitive_url
from httpx2 import HTTPStatusError, RequestError

from .._common.wrappers import apply_wrappers
from ..core.endpoints.stats import EndpointStat, Stats, StatsCollector
from ..core.endpoints.utils import param_type as param_type_util
from ..core.endpoints.utils.endpoint_call import normalize_call_args
from ..core.types import File, RestResponse
from ._constants import (
    CALL_WRAPPERS_KEY,
    INCLUDED_HEADERS,
    MAX_HEADER_VALUE_CHARS,
    MAX_RESULT_BYTES,
    TRUNCATION_PREVIEW_CHARS,
    Mode,
    ResponseFormat,
)
from .errors import ToolArgumentError
from .schema import iter_param_fields
from .wrappers import parse_call_wrappers

if TYPE_CHECKING:
    from ..core.base import APIClient
    from ..core.endpoints import Endpoint
    from .catalog import CatalogEntry, ServerOptions


@dataclass(frozen=True)
class ToolResult:
    """The result of dispatching one tool call, ready for the server layer to hand to the SDK.

    :param content: Human/model-readable text content - the JSON envelope, a decoded body, raw response
                    text, or a one-line failure detail, depending on `--response-format`
    :param structured: The same content as a JSON-safe value, for `structuredContent`, or `None` for a
                       `raw`-format result (which has no single well-defined JSON shape) or a failure
    :param is_error: Whether this result represents a failure (non-2xx response, or a bad argument)
    """

    content: str
    structured: Any
    is_error: bool


async def dispatch_endpoint_call(
    client: APIClient,
    entry: CatalogEntry,
    arguments: dict[str, Any],
    options: ServerOptions,
    *,
    mode: Mode | None = None,
    call_wrappers: Any = None,
) -> ToolResult:
    """Dispatch one MCP tool call: coerce its arguments, call the endpoint, and shape the result.

    :param client: Constructed, async-mode API client to dispatch against
    :param entry: The catalog entry (endpoint + owning API class) this call targets
    :param arguments: The tool call's raw JSON arguments (without the reserved `call_wrappers` key)
    :param options: Resolved server options
    :param mode: The server's resolved exposure mode, if known to the caller - lets an unknown-argument
                error name the one tool actually available for that mode (`describe_endpoint` in dynamic
                mode, this tool's own input schema in static mode) instead of hedging between both
    :param call_wrappers: The tool call's raw `call_wrappers` object, if any
    """
    endpoint = entry.endpoint
    try:
        call_wrapper_plan = parse_call_wrappers(call_wrappers)
        call_kwargs = _coerce_arguments(endpoint, arguments, options, mode=mode)
        call_args, call_kwargs = normalize_call_args(endpoint.introspection.original_func, (), call_kwargs)
    except (ToolArgumentError, TypeError) as e:
        return ToolResult(content=str(e), structured=None, is_error=True)

    # Always None, deferring to the client's log_requests default: the framework only forwards quiet
    # to the original function when it isn't None, so an explicit value here would break a handwritten
    # endpoint without its **kwargs.
    ctrl_kwargs = {"quiet": None, "with_hooks": True, "raw_options": None}
    try:
        if call_wrapper_plan.chain:
            # Wrappers require the bound EndpointFunc, not the plain Endpoint facade, to compose.
            call: Any | None = apply_wrappers(endpoint.bind(client), call_wrapper_plan.chain)
        else:
            call = None
    except (TypeError, ValueError) as e:
        # A wrapper's required option was missing, or another misconfiguration.
        return ToolResult(content=str(e), structured=None, is_error=True)

    async def invoke() -> Any:
        if call is not None:
            return await call(*call_args, **call_kwargs, **ctrl_kwargs)
        return await endpoint(client, *call_args, **call_kwargs, **ctrl_kwargs)

    # with_stats isn't folded into the chain, since its only effect is a printed table the model never
    # sees. `collector` is read after the await unwinds, so it stays bound even if the call raises.
    collector: StatsCollector | None = None
    try:
        if call_wrapper_plan.collect_stats:
            with Stats.collect("mcp-call") as collector:
                response = await invoke()
        else:
            response = await invoke()
    except HTTPStatusError as e:
        # A raise_on_error client raises instead of returning the failed response.
        response = RestResponse(_response=e.response)
    except RequestError as e:
        # No response was produced to shape, so report a one-line isError result naming the request.
        return _error_result(_connection_failure_detail(e), collector)
    except AssertionError as e:
        # A failed with_expected_status()/with_max_response_time() assertion - an anticipated outcome.
        return _error_result(f"Call wrapper assertion failed: {e}", collector)

    return _shape_result(
        response, options, expected_statuses=call_wrapper_plan.expected_statuses, stats=_stats_payload(collector)
    )


def _error_result(detail: str, collector: StatsCollector | None) -> ToolResult:
    """Build a one-line `isError` `ToolResult`, folding in a `stats` block when stats were collected.

    :param detail: The one-line failure detail
    :param collector: The scoped stats collector, or `None` when `with_stats` was not requested
    """
    stats = _stats_payload(collector)
    if stats is None:
        return ToolResult(content=detail, structured=None, is_error=True)
    payload = {"error": detail, "stats": stats}
    return ToolResult(content=json.dumps(payload, default=str, indent=2), structured=payload, is_error=True)


def _stats_payload(collector: StatsCollector | None) -> list[dict[str, Any]] | None:
    """Serialize a scoped stats collector's records into computed response-time percentiles (not raw
    accumulators), or `None` when `with_stats` was not requested.

    :param collector: The scoped stats collector, or `None`
    """
    if collector is None:
        return None
    return [_endpoint_stat_dict(stat) for stat in collector.all()]


def _endpoint_stat_dict(stat: EndpointStat) -> dict[str, Any]:
    """Serialize one `EndpointStat` to a model-readable dict: counts plus computed response-time stats.

    :param stat: The record to serialize
    """
    return {
        "endpoint": stat.endpoint,
        "app_name": stat.app_name,
        "num_calls": stat.num_calls,
        "num_1xx": stat.num_1xx,
        "num_2xx": stat.num_2xx,
        "num_3xx": stat.num_3xx,
        "num_4xx": stat.num_4xx,
        "num_5xx": stat.num_5xx,
        "num_unknown_status": stat.num_unknown_status,
        "num_errors": stat.num_errors,
        "avg_response_time": stat.avg_response_time,
        "min_response_time": stat.min_response_time,
        "max_response_time": stat.max_response_time,
        "p50_response_time": stat.p50_response_time,
        "p95_response_time": stat.p95_response_time,
        "p99_response_time": stat.p99_response_time,
    }


def _connection_failure_detail(exc: RequestError) -> str:
    """Format a connection-level failure (one that produced no response) as a one-line detail.

    Names the method and URL when the failed request is recoverable from the exception, so a model sees
    what the call was reaching for rather than a bare transport-error class.

    :param exc: The transport error raised in place of a response
    """
    detail = f"{type(exc).__name__}: {exc}"
    request = get_request_from_exception(exc)
    if request is None:
        return detail
    return f"{request.method.upper()} {mask_sensitive_url(str(request.url))} failed - {detail}"


def _coerce_arguments(
    endpoint: Endpoint[Any], arguments: dict[str, Any], options: ServerOptions, *, mode: Mode | None = None
) -> dict[str, Any]:
    """Convert a tool call's raw JSON arguments into endpoint call kwargs.

    An unknown argument name or a missing required parameter raises `ToolArgumentError` up front, naming
    the offending keys - except `call_wrappers` nested in here by mistake (dynamic mode's `call_endpoint`
    takes it as a top-level sibling of `arguments`, not inside `arguments` itself), which gets a dedicated
    hint naming the actual mistake instead of being reported as just another unknown key. Every other
    value is forwarded largely as-is, converting only `Enum` (from its member name) and `File` (from its
    base64/path form) - deliberately as lenient as a direct Python call, since this client is also meant
    for negative-path API testing.

    :param endpoint: Endpoint the call targets
    :param arguments: The tool call's raw JSON arguments
    :param options: Resolved server options
    :param mode: The server's resolved exposure mode, if known - lets an unknown-argument error name the
                 right tool to check instead of hedging between both
    """
    specs = {param.name: param for param in iter_param_fields(endpoint)}

    unknown = sorted(set(arguments) - set(specs))
    if unknown:
        if mode is Mode.DYNAMIC and CALL_WRAPPERS_KEY in unknown:
            raise ToolArgumentError(
                f"{CALL_WRAPPERS_KEY!r} belongs beside 'arguments' on call_endpoint, not nested inside it: "
                f"call call_endpoint({{endpoint_id, arguments, {CALL_WRAPPERS_KEY}}})."
            )
        if mode is Mode.DYNAMIC:
            hint = "Call describe_endpoint for its full input schema."
        elif mode is Mode.STATIC:
            hint = "Check this tool's input schema for the accepted parameters."
        else:
            hint = (
                "Call describe_endpoint (dynamic mode) or check this tool's own input schema (static mode) for "
                "the accepted parameters."
            )
        raise ToolArgumentError(f"Unknown argument(s) for {endpoint}: {', '.join(unknown)}. {hint}")
    missing = sorted(spec.name for spec in specs.values() if spec.required and spec.name not in arguments)
    if missing:
        raise ToolArgumentError(f"Missing required argument(s) for {endpoint}: {', '.join(missing)}")

    return {name: _coerce_value(value, specs[name].annotation, options) for name, value in arguments.items()}


def _coerce_value(value: Any, annotation: Any, options: ServerOptions) -> Any:
    """Convert one argument value according to its resolved parameter type.

    An `Enum` value is looked up by member name and resolved to that member's wire value. A `File` value
    is converted from its base64/path form. A list, dict, or fixed-length tuple's elements are each
    converted recursively the same way. A genuine multi-member union is tried member by member, trying
    `Enum`/`File` first since they're the only members whose coercion can actually reject a value - trying
    another member first would always "succeed" and leave a same-shaped `Enum`/`File` value unconverted.

    :param value: The raw JSON value for one argument
    :param annotation: The parameter's resolved type annotation
    :param options: Resolved server options
    """
    if value is None:
        return None
    base, _ = param_type_util.unwrap_annotation(annotation)
    if get_origin(base) in (Union, UnionType):
        members = get_args(base)
        ordered = sorted(members, key=lambda m: 0 if _can_reject_value(m) else 1)
        for member in ordered:
            with suppress(ToolArgumentError):
                return _coerce_value(value, member, options)
        return value
    if inspect.isclass(base) and issubclass(base, Enum) and isinstance(value, str):
        try:
            member = base[value]
        except KeyError:
            raise ToolArgumentError(
                f"Invalid value {value!r} (choose from {', '.join(m.name for m in base)})"
            ) from None
        return member.value if isinstance(member.value, str | int | float) else member
    if param_type_util.is_type_of(base, File):
        return _coerce_file(value, options)

    elem_type = param_type_util.get_sequence_elem_type(base)
    if elem_type is not None and elem_type is not inspect.Parameter.empty and isinstance(value, list):
        return [_coerce_value(v, elem_type, options) for v in value]
    if isinstance(value, dict) and get_origin(base) is dict:
        args = get_args(base)
        if len(args) == 2:
            return {k: _coerce_value(v, args[1], options) for k, v in value.items()}
    if isinstance(value, list) and get_origin(base) is tuple:
        # A fixed-length, heterogeneous tuple[X, Y, ...], matched elementwise against its declared types.
        # A length mismatch is left uncoerced rather than truncated or zero-filled, since nothing else
        # enforces the schema's minItems/maxItems here.
        args = get_args(base)
        if args and not (len(args) == 2 and args[1] is Ellipsis) and len(value) == len(args):
            return [_coerce_value(v, t, options) for v, t in zip(value, args, strict=True)]
    return value


def _can_reject_value(member: Any) -> bool:
    """Return whether this type's coercion can actually reject a value: only `Enum` (an unrecognized
    member name) or `File` (a malformed object) ever raise `ToolArgumentError`.

    :param member: One member of a genuine multi-member union, not yet unwrapped
    """
    base, _ = param_type_util.unwrap_annotation(member)
    return (inspect.isclass(base) and issubclass(base, Enum)) or param_type_util.is_type_of(base, File)


def _coerce_file(value: Any, options: ServerOptions) -> File:
    """Convert one `File` argument's JSON form - inline base64, or a local path when
    `--allow-file-paths` is set - into a `File`.

    :param value: The raw `{"filename", "content_base64", "content_type"}` or `{"path"}` object
    :param options: Resolved server options
    """
    if not isinstance(value, dict):
        raise ToolArgumentError(f"Expected a file object, got {type(value).__name__}")
    if "path" in value:
        if not options.allow_file_paths:
            raise ToolArgumentError(
                "This server doesn't accept local file paths. Send inline base64 content instead "
                '(\'{"filename": ..., "content_base64": ...}\').'
            )
        return _file_from_path(_string_field(value, "path"), options)
    if "content_base64" not in value or "filename" not in value:
        raise ToolArgumentError("A file object needs 'filename' and 'content_base64' (or, if enabled, 'path')")
    filename = _string_field(value, "filename")
    content_base64 = _string_field(value, "content_base64")
    content_type = _string_field(value, "content_type") if value.get("content_type") is not None else None
    try:
        # Whitespace (e.g. from a line-wrapped literal) is stripped before decoding, since validate=True
        # would otherwise reject it as invalid content.
        content = base64.b64decode("".join(content_base64.split()), validate=True)
    except binascii.Error as e:
        raise ToolArgumentError(f"Invalid base64 content: {e}") from None
    _check_file_size(len(content), options)
    content_type = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    # mypy misreads File as abstract because of its Protocol base. @dataclass supplies a concrete
    # __init__ at runtime.
    return File(filename, content, content_type)  # type: ignore[abstract]


def _file_from_path(path_str: str, options: ServerOptions) -> File:
    """Read a local file path into a `File`. Only reachable when `--allow-file-paths` is set.

    The size is checked against the file's stat size before reading it into memory, so an oversized
    file is rejected without first buffering it.

    :param path_str: Filesystem path to read
    :param options: Resolved server options
    """
    path = Path(path_str)
    if not path.is_file():
        raise ToolArgumentError(f"No such file: {path_str!r}")
    _check_file_size(path.stat().st_size, options)
    content = path.read_bytes()
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    # mypy misreads File as abstract because of its Protocol base. @dataclass supplies a concrete
    # __init__ at runtime.
    return File(path.name, content, content_type)  # type: ignore[abstract]


def _string_field(value: dict[str, Any], key: str) -> str:
    """Return `value[key]` as a string, raising `ToolArgumentError` naming `key` if it isn't one.

    :param value: The raw file object
    :param key: The field name to validate
    """
    field = value[key]
    if not isinstance(field, str):
        raise ToolArgumentError(f"A file object's {key!r} must be a string, got {type(field).__name__}")
    return field


def _check_file_size(size: int, options: ServerOptions) -> None:
    """Raise `ToolArgumentError` if `size` exceeds `--max-file-bytes`.

    :param size: The file content's size, in bytes - decoded content's own `len()`, or a not-yet-read
                local file's own `stat().st_size`
    :param options: Resolved server options
    """
    if size > options.max_file_bytes:
        raise ToolArgumentError(f"File content exceeds the {options.max_file_bytes}-byte limit (--max-file-bytes)")


def _shape_result(
    response: RestResponse | list[Any],
    options: ServerOptions,
    *,
    expected_statuses: tuple[int, ...] = (),
    stats: list[dict[str, Any]] | None = None,
) -> ToolResult:
    """Shape a dispatched call's result into a `ToolResult`.

    `response` is one response for a plain call, or a list of responses/exceptions from a repeat or
    concurrency group. The body is always already fully read, since streaming isn't exposed over MCP.

    :param response: The dispatched call's return value
    :param options: Resolved server options
    :param expected_statuses: Status codes declared via `with_expected_status()`, treated as OK
    :param stats: Serialized stats records to fold into a full-format or failure envelope, or `None`
    """
    if isinstance(response, list):
        return _shape_list_result(response, options, expected_statuses, stats)
    return _shape_single_result(response, options, expected_statuses, stats)


def _shape_single_result(
    response: RestResponse,
    options: ServerOptions,
    expected_statuses: tuple[int, ...],
    stats: list[dict[str, Any]] | None,
) -> ToolResult:
    """Shape one `RestResponse` into a `ToolResult`, honoring `--response-format` and any expected status.

    :param response: The response returned by the dispatched call
    :param options: Resolved server options
    :param expected_statuses: Status codes to treat as OK even when not 2xx
    :param stats: Serialized `with_stats()` records, or `None`
    """
    extra = _stats_extra(stats)
    if not _response_ok(response, expected_statuses):
        # Include the full body, not just a one-line summary: it's the model's only channel, and often
        # what lets it correct its arguments and retry. structuredContent is always the same envelope,
        # not just when with_stats was requested: the MCP SDK only validates it against a tool's
        # output_schema on a successful result, so there's no schema-safety reason to withhold it here.
        detail = _render_full_payload(response, options, extra=extra)
        return ToolResult(
            content=f"{format_request_failure(response)}\n\n{detail}", structured=json.loads(detail), is_error=True
        )

    if options.response_format is ResponseFormat.RAW:
        return ToolResult(content=_bound_text(response._response.text), structured=None, is_error=False)
    if options.response_format is ResponseFormat.JSON:
        content = _render_bounded_body(_json_safe_body(response.response))
        body = json.loads(content)
        # structuredContent must be a JSON object per the MCP spec, so a body that decoded to something
        # else (a list, a string, a bare number, ...) is shown only as text content.
        structured = body if isinstance(body, dict) else None
        return ToolResult(content=content, structured=structured, is_error=False)

    content = _render_full_payload(response, options, extra=extra)
    # Round-tripped through JSON so structuredContent matches content exactly and is always JSON-safe.
    return ToolResult(content=content, structured=json.loads(content), is_error=False)


def _shape_list_result(
    items: list[Any],
    options: ServerOptions,
    expected_statuses: tuple[int, ...],
    stats: list[dict[str, Any]] | None,
) -> ToolResult:
    """Shape a repeat/concurrency list result into one `ToolResult`.

    Each item is a response or a captured exception. The result is `is_error` if any item failed, and the
    per-item array is wrapped as `{"results": [...]}` since `structuredContent` must be an object.

    :param items: The list returned by the multi-call wrapper
    :param options: Resolved server options
    :param expected_statuses: Status codes to treat as OK even when not 2xx
    :param stats: Serialized stats records, or `None`
    """
    ok_flags = [_item_ok(item, expected_statuses) for item in items]
    is_error = not all(ok_flags)

    if options.response_format is ResponseFormat.RAW:
        content = "\n\n".join(_item_raw(item) for item in items)
        return ToolResult(content=_bound_text(content), structured=None, is_error=is_error)

    if options.response_format is ResponseFormat.JSON:
        content = _render_list_payload({"results": [_item_body(item) for item in items]})
        return ToolResult(content=content, structured=json.loads(content), is_error=is_error)

    payload: dict[str, Any] = {"results": [_item_full(item, options) for item in items]}
    if stats is not None:
        payload["stats"] = stats
    content = _render_list_payload(payload)
    return ToolResult(
        content=f"{_list_summary(items, ok_flags)}\n\n{content}", structured=json.loads(content), is_error=is_error
    )


def _response_ok(response: RestResponse, expected_statuses: tuple[int, ...]) -> bool:
    """Whether a response counts as success: a 2xx, or a status the caller declared via
    `with_expected_status()`.

    :param response: The response to check
    :param expected_statuses: Status codes to treat as OK even when not 2xx
    """
    return response.ok or response.status_code in expected_statuses


def _item_ok(item: Any, expected_statuses: tuple[int, ...]) -> bool:
    """Whether one list item counts as success. A captured exception never does.

    :param item: A `RestResponse` or a captured `BaseException`
    :param expected_statuses: Status codes to treat as OK even when not 2xx
    """
    return isinstance(item, RestResponse) and _response_ok(item, expected_statuses)


def _item_full(item: Any, options: ServerOptions) -> dict[str, Any]:
    """One list item as a `{status_code, headers, body}` envelope, or `{"error": <detail>}` for a
    captured exception.

    :param item: A `RestResponse` or a captured `BaseException`
    :param options: Resolved server options
    """
    if isinstance(item, BaseException):
        return {"error": _exception_detail(item)}
    return {
        "status_code": item.status_code,
        "headers": _filtered_headers(item, options),
        "body": _json_safe_body(item.response),
    }


def _item_body(item: Any) -> Any:
    """One list item's decoded body, or `{"error": <detail>}` for a captured exception.

    :param item: A `RestResponse` or a captured `BaseException`
    """
    if isinstance(item, BaseException):
        return {"error": _exception_detail(item)}
    return _json_safe_body(item.response)


def _item_raw(item: Any) -> str:
    """One list item's undecoded text, or a one-line detail for a captured exception.

    :param item: A `RestResponse` or a captured `BaseException`
    """
    if isinstance(item, BaseException):
        return _exception_detail(item)
    return item._response.text


def _exception_detail(exc: BaseException) -> str:
    """A one-line detail for a captured exception. An `HTTPStatusError` renders through the shared
    request-failure formatter, so it reads like any other failed response rather than httpx2's own
    two-line message.

    :param exc: The captured exception
    """
    if isinstance(exc, HTTPStatusError):
        return format_request_failure(RestResponse(_response=exc.response))
    return f"{type(exc).__name__}: {exc}"


def _list_summary(items: list[Any], ok_flags: list[bool]) -> str:
    """A one-line summary of a multi-call result, deduplicating identical failure details.

    :param items: The list items
    :param ok_flags: Per-item success flags, aligned with `items`
    """
    total = len(items)
    failed = [item for item, ok in zip(items, ok_flags, strict=True) if not ok]
    if not failed:
        return f"All {total} call(s) succeeded."
    details = list(dict.fromkeys(_failure_brief(item) for item in failed))
    return f"{len(failed)} of {total} call(s) failed: {'; '.join(details)}"


def _failure_brief(item: Any) -> str:
    """A compact, request-id-free failure label for `_list_summary()`'s deduplication.

    :param item: A failed `RestResponse` or a captured `BaseException`
    """
    if isinstance(item, BaseException):
        return _exception_detail(item)
    return f"HTTP {item.status_code}"


def _stats_extra(stats: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """Wrap serialized stats records as an envelope `extra` mapping, or `None` when absent.

    :param stats: Serialized `with_stats()` records, or `None`
    """
    return {"stats": stats} if stats is not None else None


def _filtered_headers(response: RestResponse, options: ServerOptions) -> dict[str, str]:
    """A response's headers, limited to the default allowlist unless `--all-headers` is set.

    :param response: The response whose headers to filter
    :param options: Resolved server options
    """
    headers = dict(response._response.headers)
    if options.all_headers:
        return headers
    return {k: v for k, v in headers.items() if k.lower() in INCLUDED_HEADERS}


def _bounded_headers(headers: dict[str, str]) -> dict[str, str]:
    """Bound an oversized headers dict so the rendered envelope still fits the result size cap even
    when truncating `body` alone wasn't enough - reachable only via `--all-headers` against a
    response carrying an unusually large header block.

    Falls back to the same fixed allowlist `_filtered_headers()` applies by default, then truncates
    each surviving value to `MAX_HEADER_VALUE_CHARS`, so the result is bounded by the allowlist's
    own small, fixed entry count regardless of how many headers (or how large a single one) the
    response actually carried. A marker key notes the drop, since a model has no other signal that
    `--all-headers` was overridden here.

    :param headers: The (already `--all-headers`-inclusive) headers dict that left the envelope over
                    the cap even after `body` was replaced with a truncation marker
    """
    bounded = {
        k: v if len(v) <= MAX_HEADER_VALUE_CHARS else f"{v[:MAX_HEADER_VALUE_CHARS]}...(truncated)"
        for k, v in headers.items()
        if k.lower() in INCLUDED_HEADERS
    }
    bounded["x-mcp-headers-truncated"] = "true"
    return bounded


def _render_list_payload(payload: dict[str, Any]) -> str:
    """Render a `{"results": [...], ...}` list envelope as indented JSON, replacing `results` with a
    truncation marker if the whole rendered text would exceed the result size cap.

    :param payload: The list envelope, with a `results` key
    """
    content = json.dumps(payload, default=str, indent=2)
    size = len(content.encode())
    if size <= MAX_RESULT_BYTES:
        return content
    payload["results"] = _truncation_marker(content, size)
    return json.dumps(payload, default=str, indent=2)


def _render_full_payload(response: RestResponse, options: ServerOptions, *, extra: dict[str, Any] | None = None) -> str:
    """Render one response's `{status_code, headers, body}` envelope as indented JSON, truncating
    `body` - and, if that alone isn't enough, `headers` too - if the envelope would exceed the
    result size cap.

    :param response: The response to envelope, never a stream response
    :param options: Resolved server options
    :param extra: Extra top-level keys (e.g. a `stats` block) to merge into the envelope
    """
    payload: dict[str, Any] = {
        "status_code": response.status_code,
        "headers": _filtered_headers(response, options),
        "body": _json_safe_body(response.response),
    }
    if extra:
        payload.update(extra)
    content = json.dumps(payload, default=str, indent=2)
    size = len(content.encode())
    if size <= MAX_RESULT_BYTES:
        return content

    body_preview = json.dumps(payload["body"], default=str, indent=2)
    payload["body"] = _truncation_marker(content, size, preview_source=body_preview)
    content = json.dumps(payload, default=str, indent=2)
    if len(content.encode()) <= MAX_RESULT_BYTES:
        return content

    # Only reachable when headers alone (typically via --all-headers) are what pushed the envelope
    # over the cap, since body was already replaced with a small, fixed-size marker above.
    payload["headers"] = _bounded_headers(payload["headers"])
    return json.dumps(payload, default=str, indent=2)


def _json_safe_body(body: Any) -> Any:
    """Return a response body as a JSON-safe value.

    A JSON-decoded body passes through unchanged. Raw `bytes` (a binary body that didn't decode as JSON
    or valid UTF-8 text) is base64-encoded instead, with an `encoding` marker distinguishing it from an
    ordinary JSON-decoded object.

    :param body: The response's already-decoded body
    """
    if isinstance(body, bytes):
        return {"content_base64": base64.b64encode(body).decode("ascii"), "encoding": "base64"}
    return body


def _render_bounded_body(body: Any) -> str:
    """Render `body` as indented JSON, replacing it with a truncation marker if the rendered text would
    exceed the result size cap.

    :param body: A response body already reduced to a JSON-safe value
    """
    content = json.dumps(body, default=str, indent=2)
    size = len(content.encode())
    if size <= MAX_RESULT_BYTES:
        return content
    return json.dumps(_truncation_marker(content, size), default=str, indent=2)


def _truncation_marker(rendered: str, size: int, *, preview_source: str | None = None) -> dict[str, Any]:
    """Build the marker replacing an oversized body: always a JSON object, so it's always eligible for
    `structuredContent` regardless of the original body's shape.

    :param rendered: The JSON text the caller already produced for the oversized payload
    :param size: The actual rendered size that triggered truncation, in bytes
    :param preview_source: Text to slice the preview from instead of `rendered`, when the two differ (a
                caller whose text leads with other keys before the one being replaced)
    """
    source = rendered if preview_source is None else preview_source
    return {"truncated": True, "bytes": size, "preview": source[:TRUNCATION_PREVIEW_CHARS]}


def _bound_text(text: str) -> str:
    """Truncate `text` with a trailing marker if it exceeds the result size cap, otherwise return it
    unchanged.

    :param text: A `raw`-format response's undecoded text
    """
    size = len(text.encode())
    if size <= MAX_RESULT_BYTES:
        return text
    return (
        f"{text[:TRUNCATION_PREVIEW_CHARS]}...\n\n"
        f"[truncated: showing the first {TRUNCATION_PREVIEW_CHARS} characters of {size} bytes]"
    )

"""JSON Schema layer: `Endpoint` -> MCP tool input schema, description, and safety annotations.

`param_type_util.unwrap_annotation()` preserves `T | None` nullability as a JSON Schema `anyOf ... null`
branch, since an explicit `null` can be a meaningful wire value some endpoints require.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Iterator
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin
from uuid import UUID

from common_libs.logging import get_logger

from ..core.endpoints import Endpoint, EndpointParam
from ..core.endpoints.utils import param_type as param_type_util
from ..core.endpoints.utils.endpoint_model import get_reserved_param_names
from ..core.types import File
from ._constants import CALL_WRAPPERS_KEY, DESTRUCTIVE_METHODS, IDEMPOTENT_METHODS, READ_ONLY_METHODS
from .catalog import ServerOptions
from .wrappers import call_wrappers_schema

logger = get_logger(__name__)

_SCALAR_JSON_TYPES: dict[type, str] = {str: "string", int: "integer", float: "number"}
_STRINGLY_TYPES_FORMAT: dict[type, str] = {datetime: "date-time", date: "date", time: "time", UUID: "uuid"}
_LITERAL_JSON_TYPES: dict[type, str] = {bool: "boolean", int: "integer", float: "number", str: "string"}
# `call_wrappers` joins the control kwargs as a reserved name: an endpoint parameter that resolves to it
# is dropped from the schema rather than colliding with the reserved per-call `call_wrappers` property.
_RESERVED_PARAM_NAMES: frozenset[str] = frozenset(get_reserved_param_names()) | {CALL_WRAPPERS_KEY}
# Sentinel distinguishing "this default can't be rendered as JSON" from an actual JSON `null` default.
_NO_DEFAULT = object()


def iter_param_fields(endpoint: Endpoint[Any], *, warn: bool = False) -> Iterator[EndpointParam]:
    """Yield an `EndpointParam` for each of an endpoint's usable parameters, building no schema at all.

    A parameter resolving to a reserved control-kwarg name (`quiet`/`with_hooks`/`raw_options`) is skipped
    outright, since passing one through would collide with the keyword the dispatcher already supplies it
    under. Split out from schema construction so argument coercion, on every tool dispatch, never pays
    for building a JSON Schema fragment per parameter just to throw it away.

    :param endpoint: Endpoint whose parameters to walk
    :param warn: Log a diagnostic for each skipped reserved-name parameter. Only a schema-publication pass
                should set this, not one that merely reads parameters back for argument coercion
    """
    for param in endpoint.introspection.iter_params():
        if param.name in _RESERVED_PARAM_NAMES:
            if warn:
                logger.debug(
                    f"{endpoint.api_class.__name__}.{endpoint.func_name}: Parameter {param.name!r} is reserved "
                    f"for MCP dispatch and is never reachable through this tool. Start the server with "
                    f"--log-level DEBUG to see this again."
                )
            continue
        yield param


def build_input_schema(
    endpoint: Endpoint[Any], options: ServerOptions, *, warn: bool = False, with_call_wrappers: bool = False
) -> dict[str, Any]:
    """Build the full JSON Schema object for one endpoint's parameters.

    Ready to publish as an MCP tool's `inputSchema` in static mode, or return from `describe_endpoint` in
    dynamic mode. Static mode additionally nests the reserved `call_wrappers` property
    (`with_call_wrappers=True`). A field whose type can't be mapped at all still gets a property, falling
    back to an unconstrained (`{}`) schema, so one unrecognized annotation degrades that single parameter
    rather than the whole tool.

    :param endpoint: Endpoint whose parameters to build a schema for
    :param options: Resolved server options, used for File handling
    :param warn: Log a diagnostic for each skipped or unmapped parameter
    :param with_call_wrappers: Nest the reserved `call_wrappers` property (static mode only)
    """
    param_docs = endpoint.introspection.param_docs
    properties: dict[str, Any] = {}
    required: list[str] = []
    for param in iter_param_fields(endpoint, warn=warn):
        try:
            prop_schema = _schema_for_annotation(param.annotation, options)
        except Exception as e:
            if warn:
                logger.warning(
                    f"{endpoint.api_class.__name__}.{endpoint.func_name}: Unable to determine a JSON schema for "
                    f"parameter {param.name!r} (annotation: {param.annotation!r}): {type(e).__name__}: {e}. "
                    f"Falling back to an unconstrained schema for this parameter."
                )
            prop_schema = {}
        if param.default is None and not _is_nullable_schema(prop_schema):
            # The common `def f(self, name: str = None)` pattern: the annotation itself isn't nullable,
            # but the default already is, so the schema is widened to match.
            prop_schema = _merge_null(prop_schema)

        description = param_docs.get(param.name)
        if param.deprecated:
            description = f"(deprecated) {description}" if description else "(deprecated)"
        if not param.required and param.has_default:
            default = _format_default(param.default)
            if default is not _NO_DEFAULT:
                prop_schema = {**prop_schema, "default": default}
        if param.deprecated:
            prop_schema = {**prop_schema, "deprecated": True}
        if description:
            prop_schema = {**prop_schema, "description": description}
        prop_schema = {**prop_schema, "x-location": param.location}

        properties[param.name] = prop_schema
        if param.required:
            required.append(param.name)

    if with_call_wrappers:
        properties[CALL_WRAPPERS_KEY] = call_wrappers_schema()

    schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


def tool_description(endpoint: Endpoint[Any]) -> str:
    """Build an MCP tool's description from an endpoint's docstring prose, plus the real HTTP call and
    any deprecation/undocumented note.

    Shows the full prose rather than just its first line, since an MCP client has no `--help` width
    budget to economize for.

    :param endpoint: Endpoint to describe
    """
    prose = endpoint.introspection.description
    text = f"{prose}\n\nHTTP: {endpoint}" if prose else str(endpoint)
    if endpoint.is_deprecated:
        text += f"\n\nDEPRECATED: {endpoint} is deprecated."
    if not endpoint.is_documented:
        text += "\n\nThis endpoint is marked as undocumented."
    return text


def tool_title(endpoint: Endpoint[Any]) -> str:
    """Build an MCP tool's short title from an endpoint's docstring summary line.

    :param endpoint: Endpoint to title
    """
    return endpoint.introspection.summary or str(endpoint)


def annotations_for(method: str) -> dict[str, bool]:
    """Return the four MCP tool annotation hints for one HTTP method, every field set explicitly.

    Never left to an SDK default: an unset `destructiveHint` defaults to `True` in the MCP spec, so an
    unset POST would over-claim destruction. Keyed by their MCP wire names (`readOnlyHint`, not
    `read_only_hint`), since one caller embeds this dict verbatim rather than through an SDK model.

    :param method: Lowercase HTTP method
    """
    return {
        "readOnlyHint": method in READ_ONLY_METHODS,
        "destructiveHint": method in DESTRUCTIVE_METHODS,
        "idempotentHint": method in IDEMPOTENT_METHODS,
        "openWorldHint": True,
    }


def _schema_for_annotation(annotation: Any, options: ServerOptions) -> dict[str, Any]:
    """Map a resolved parameter type annotation to a JSON Schema fragment.

    :param annotation: Resolved type annotation of one endpoint parameter field
    :param options: Resolved server options (threaded through for File handling)
    """
    base, nullable = param_type_util.unwrap_annotation(annotation)
    core = _leaf_schema(base, options)
    return _merge_null(core) if nullable else core


def _merge_null(schema: dict[str, Any]) -> dict[str, Any]:
    """Widen `schema` to also accept `null`, folding it into an existing `anyOf` rather than nesting a
    second one inside it.

    :param schema: A JSON Schema fragment not already nullable (see `_is_nullable_schema()`)
    """
    if "anyOf" in schema:
        return {"anyOf": [*schema["anyOf"], {"type": "null"}]}
    return {"anyOf": [schema, {"type": "null"}]}


def _is_nullable_schema(schema: dict[str, Any]) -> bool:
    """Return whether `schema` already accepts `null`, either directly or as one of its `anyOf` members.

    :param schema: A JSON Schema fragment, as built by `_schema_for_annotation()`
    """
    if schema.get("type") == "null":
        return True
    any_of = schema.get("anyOf")
    if not isinstance(any_of, list):
        return False
    return any(isinstance(member, dict) and member.get("type") == "null" for member in any_of)


def _leaf_schema(base: Any, options: ServerOptions) -> dict[str, Any]:
    """Map an already-unwrapped (`Annotated[]`/nullable-union-stripped) type to a JSON Schema fragment.

    An annotation with no mapping at all (a bare `dict`, an unresolved forward reference, ...) returns
    `{}` - JSON Schema's "anything goes" - rather than raising, so one unmapped parameter degrades
    gracefully instead of taking out the whole tool.

    :param base: Already-unwrapped annotation
    :param options: Resolved server options
    """
    if base is inspect.Parameter.empty:
        return {}
    if base is bool:
        return {"type": "boolean"}

    origin = get_origin(base)
    if origin in (Union, UnionType):
        # A genuine multi-member union (None already excluded by unwrap_annotation()).
        return {"anyOf": [_schema_for_annotation(m, options) for m in get_args(base)]}
    if origin is Literal:
        return _literal_schema(get_args(base))
    if inspect.isclass(base) and issubclass(base, Enum):
        return {"type": "string", "enum": [member.name for member in base]}
    if param_type_util.is_type_of(base, File):
        return _file_schema(options)

    fixed_tuple = _fixed_tuple_schema(base, options)
    if fixed_tuple is not None:
        return fixed_tuple
    elem_type = param_type_util.get_sequence_elem_type(base)
    if elem_type is not None:
        if elem_type is inspect.Parameter.empty:
            return {"type": "array"}
        return {"type": "array", "items": _schema_for_annotation(elem_type, options)}

    if base in _SCALAR_JSON_TYPES:
        return {"type": _SCALAR_JSON_TYPES[base]}
    if base in _STRINGLY_TYPES_FORMAT:
        return {"type": "string", "format": _STRINGLY_TYPES_FORMAT[base]}
    if base is Decimal:
        return {"type": "string"}
    if inspect.isclass(base) and issubclass(base, str):
        return {"type": "string"}

    if origin is dict:
        args = get_args(base)
        if len(args) == 2:
            _, value_type = args
            return {"type": "object", "additionalProperties": _schema_for_annotation(value_type, options)}
        return {"type": "object"}
    if base is dict:
        return {"type": "object"}
    return {}


def _literal_schema(choices: tuple[Any, ...]) -> dict[str, Any]:
    """Map a `Literal[...]`'s choices to a JSON Schema `enum`, with a shared `type` when every choice is
    the same JSON-representable scalar type.

    :param choices: `Literal[...]`'s allowed values
    """
    if not choices:
        return {"type": "string"}
    types_seen = {type(c) for c in choices}
    schema: dict[str, Any] = {"enum": list(choices)}
    if len(types_seen) == 1:
        json_type = _LITERAL_JSON_TYPES.get(next(iter(types_seen)))
        if json_type:
            schema = {"type": json_type, **schema}
    return schema


def _fixed_tuple_schema(base: Any, options: ServerOptions) -> dict[str, Any] | None:
    """Map a fixed-length, heterogeneous `tuple[X, Y, ...]` to a JSON Schema array with `prefixItems`.
    Returns `None` for anything else, including a variable-length `tuple[X, ...]`.

    :param base: Already-unwrapped annotation
    :param options: Resolved server options
    """
    if get_origin(base) is not tuple:
        return None
    args = get_args(base)
    if not args or (len(args) == 2 and args[1] is Ellipsis):
        return None
    items = [_schema_for_annotation(arg, options) for arg in args]
    return {"type": "array", "prefixItems": items, "minItems": len(items), "maxItems": len(items)}


def _file_schema(options: ServerOptions) -> dict[str, Any]:
    """Build the JSON Schema for a `File`-typed parameter: inline base64 content always accepted, a
    local filesystem path only when `--allow-file-paths` is given.

    The path form is omitted from the schema entirely by default, rather than merely rejected at
    dispatch time, so a model is never invited to try a form the server will refuse.

    :param options: Resolved server options
    """
    base64_form = {
        "type": "object",
        "properties": {
            "filename": {"type": "string"},
            "content_base64": {"type": "string", "contentEncoding": "base64"},
            "content_type": {"type": "string"},
        },
        "required": ["filename", "content_base64"],
        "additionalProperties": False,
    }
    if not options.allow_file_paths:
        return base64_form
    path_form = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    }
    return {"anyOf": [base64_form, path_form]}


def _format_default(default: Any) -> Any:
    """Render a parameter's default value as a JSON-safe value for the schema's `"default"` key.

    An `Enum` member is rendered as its name, matching what an Enum-typed property accepts on the
    wire. A "stringly" type (`datetime`/`date`/`time`/`UUID`/`Decimal`) is rendered via `str()`. Anything
    else that doesn't survive a JSON round-trip returns `_NO_DEFAULT`, signaling the caller to omit the
    key entirely rather than show a wrong or malformed value.

    :param default: The field's default value
    """
    if isinstance(default, Enum):
        return default.name
    if isinstance(default, datetime | date | time | UUID | Decimal):
        return str(default)
    try:
        json.dumps(default)
    except TypeError:
        return _NO_DEFAULT
    return default

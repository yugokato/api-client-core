"""The MCP projection of the shared call-wrapper registry.

Turns the shared `WRAPPERS` registry into the JSON Schema fragment published under a tool's
`call_wrappers` property, and parses a tool call's `call_wrappers` object into a `CallWrapperPlan` the
runner folds onto the bound endpoint func.

Several behaviors differ from the framework defaults on purpose, since MCP is machine-driven:

- `with_repeat`/`with_concurrency` default `return_exceptions=True` here, so an N-call group always runs
  every call and returns a list the runner can shape, instead of the first failure aborting the group.
- `with_lock`'s `lock_name` is restricted to a plain token, since it becomes a filesystem path component.
- A required scalar option (e.g. `with_rate_limit`'s `max_requests`) is rejected here, at parse time,
  when absent, rather than left to fail deep inside the framework's own call once the chain is built.
- An explicit JSON `null` (a whole wrapper's options object, or one option's own value) is treated
  the same as an omitted key, since a model has no other way to say "leave this at its default".
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .._common.wrappers import WITH_CONCURRENCY, WITH_LOCK, WITH_REPEAT, WITH_STATS, WRAPPERS, WrapperSpec
from .._common.wrappers import expected_statuses as _chain_expected_statuses
from ._constants import CALL_WRAPPERS_KEY
from .errors import ToolArgumentError

_JSON_TYPES: Mapping[type, str] = {int: "integer", float: "number", bool: "boolean", str: "string"}
# with_lock's lock_name becomes a filesystem path component: unlike every other wrapper option, a
# model-supplied value here reaches the filesystem, so it's restricted to a plain token.
_LOCK_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,255}$")


@dataclass(frozen=True)
class CallWrapperPlan:
    """A parsed `call_wrappers` object, ready for the runner.

    :param chain: `(wrapper_name, options)` pairs in canonical fold order, `with_stats` removed
    :param collect_stats: Whether `with_stats` was requested (handled by the runner's own stats scope,
                          not folded into `chain`)
    :param expected_statuses: Every code across all `with_expected_status` links, for `is_error` resolution
    """

    chain: tuple[tuple[str, dict[str, Any]], ...]
    collect_stats: bool
    expected_statuses: tuple[int, ...]


_EMPTY_PLAN = CallWrapperPlan(chain=(), collect_stats=False, expected_statuses=())


def call_wrappers_schema() -> dict[str, Any]:
    """Build the JSON Schema object for a tool's `call_wrappers` property, one sub-object per wrapper."""
    return {
        "type": "object",
        "description": (
            "Optional call wrappers, each mirroring the framework's with_xxx() chainable wrapper. "
            "Wrappers are applied in a fixed order regardless of key order. with_repeat and "
            "with_concurrency are mutually exclusive and each return a list of responses."
        ),
        "properties": {name: _wrapper_schema(spec) for name, spec in WRAPPERS.items()},
        "additionalProperties": False,
    }


def parse_call_wrappers(value: Any) -> CallWrapperPlan:
    """Parse and validate a tool call's `call_wrappers` object into a `CallWrapperPlan`.

    Does the validation the schema itself isn't enforced for: an unknown wrapper or option name, a wrong
    JSON type, a value below a registry minimum, a missing required option, an out-of-pattern `with_lock`
    name, both terminal wrappers at once. Every failure raises `ToolArgumentError`.

    :param value: The raw `call_wrappers` value from the tool call (`None` or absent yields an empty plan)
    """
    if value is None:
        return _EMPTY_PLAN
    if not isinstance(value, dict):
        raise ToolArgumentError(f"{CALL_WRAPPERS_KEY!r} must be an object mapping wrapper names to their options")

    unknown = sorted(set(value) - set(WRAPPERS))
    if unknown:
        raise ToolArgumentError(f"Unknown call wrapper(s): {', '.join(unknown)}. Valid wrappers: {', '.join(WRAPPERS)}")
    if WITH_REPEAT in value and WITH_CONCURRENCY in value:
        raise ToolArgumentError(f"{WITH_REPEAT!r} and {WITH_CONCURRENCY!r} are mutually exclusive")

    resolved = {name: _resolve_options(WRAPPERS[name], value[name]) for name in WRAPPERS if name in value}

    collect_stats = resolved.pop(WITH_STATS, None) is not None
    for terminal_wrapper in (WITH_REPEAT, WITH_CONCURRENCY):
        if terminal_wrapper in resolved:
            resolved[terminal_wrapper].setdefault("return_exceptions", True)

    chain = tuple(resolved.items())
    return CallWrapperPlan(chain=chain, collect_stats=collect_stats, expected_statuses=_chain_expected_statuses(chain))


def split_call_wrappers(arguments: Mapping[str, Any]) -> tuple[dict[str, Any], Any]:
    """Split the reserved `call_wrappers` key off a raw tool-call arguments object.

    :param arguments: The tool call's raw arguments
    """
    rest = {k: v for k, v in arguments.items() if k != CALL_WRAPPERS_KEY}
    return rest, arguments.get(CALL_WRAPPERS_KEY)


def _wrapper_schema(spec: WrapperSpec) -> dict[str, Any]:
    """Build one wrapper's object schema, one property per accepted option.

    :param spec: The wrapper to describe
    """
    schema: dict[str, Any] = {
        "type": "object",
        "description": spec.summary,
        "properties": {opt: _option_schema(spec, opt, opt_type) for opt, opt_type in spec.options.items()},
        "additionalProperties": False,
    }
    required = spec.array_options | spec.required
    if required:
        schema["required"] = sorted(required)
    return schema


def _option_schema(spec: WrapperSpec, opt: str, opt_type: type) -> dict[str, Any]:
    """Build one option's schema fragment.

    :param spec: The wrapper the option belongs to
    :param opt: The option name
    :param opt_type: The option's scalar type
    """
    scalar: dict[str, Any] = {"type": _JSON_TYPES[opt_type]}
    if opt in spec.multi:
        return {"anyOf": [dict(scalar), {"type": "array", "items": dict(scalar), "minItems": 1}]}
    if opt in spec.array_options:
        return {"type": "array", "items": dict(scalar), "minItems": 1}
    if spec.name == WITH_LOCK and opt == "lock_name":
        return {**scalar, "pattern": _LOCK_NAME_RE.pattern}
    fragment = dict(scalar)
    if opt in spec.minimums:
        fragment["minimum"] = spec.minimums[opt]
    return fragment


def _resolve_options(spec: WrapperSpec, raw: Any) -> dict[str, Any]:
    """Validate and normalize one wrapper's given options object.

    An explicit JSON `null`, for the whole options object or for one option's own value, is treated
    the same as omitting it, since a model has no way to distinguish the two intents when leaving
    an optional value unused.

    :param spec: The wrapper the options are for
    :param raw: The raw options value from the `call_wrappers` object
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ToolArgumentError(f"{spec.name!r} options must be an object, got {type(raw).__name__}")
    unknown = sorted(set(raw) - set(spec.options))
    if unknown:
        valid = ", ".join(spec.options) or "(none)"
        raise ToolArgumentError(f"Unknown option(s) for {spec.name!r}: {', '.join(unknown)}. Valid options: {valid}")
    resolved = {opt: _coerce_option(spec, opt, val) for opt, val in raw.items() if val is not None}
    for opt in spec.array_options | spec.required:
        if opt not in resolved:
            raise ToolArgumentError(f"{spec.name!r} requires {opt!r}")
    return resolved


def _coerce_option(spec: WrapperSpec, opt: str, val: Any) -> Any:
    """Validate one option value against its declared shape and bounds, returning it unchanged.

    :param spec: The wrapper the option belongs to
    :param opt: The option name
    :param val: The raw JSON value
    """
    opt_type = spec.options[opt]
    if opt in spec.multi:
        items = val if isinstance(val, list) else [val]
        if not items:
            raise ToolArgumentError(f"{spec.name!r} {opt!r} must not be an empty array")
        for item in items:
            _check_scalar(spec, opt, opt_type, item)
        return val
    if opt in spec.array_options:
        if not isinstance(val, list) or not val:
            raise ToolArgumentError(f"{spec.name!r} {opt!r} must be a non-empty array")
        for item in val:
            _check_scalar(spec, opt, opt_type, item)
        return val
    if spec.name == WITH_LOCK and opt == "lock_name":
        _check_scalar(spec, opt, opt_type, val)
        if not _LOCK_NAME_RE.match(val):
            raise ToolArgumentError(f"{spec.name!r} {opt!r} must match {_LOCK_NAME_RE.pattern!r}")
        return val
    _check_scalar(spec, opt, opt_type, val)
    if opt in spec.minimums and val < spec.minimums[opt]:
        raise ToolArgumentError(f"{spec.name!r} {opt!r} must be >= {spec.minimums[opt]}, got {val}")
    if opt == "interval" and val <= 0:
        raise ToolArgumentError(f"{spec.name!r} {opt!r} must be > 0, got {val}")
    return val


def _check_scalar(spec: WrapperSpec, opt: str, opt_type: type, val: Any) -> None:
    """Raise `ToolArgumentError` unless `val` is a JSON value of `opt_type`.

    `bool` is checked before `int` since Python's `True`/`False` are `int` instances. A `float`-typed
    option also accepts a JSON integer (`5` for `retry_after`), the way the framework's signatures do.

    :param spec: The wrapper the option belongs to, for the error message
    :param opt: The option name, for the error message
    :param opt_type: The expected scalar type
    :param val: The value to check
    """
    if opt_type is bool:
        ok = isinstance(val, bool)
    elif opt_type is int:
        ok = isinstance(val, int) and not isinstance(val, bool)
    elif opt_type is float:
        ok = isinstance(val, (int, float)) and not isinstance(val, bool)
    else:
        ok = isinstance(val, str)
    if not ok:
        raise ToolArgumentError(f"{spec.name!r} {opt!r} must be a {opt_type.__name__}, got {type(val).__name__}")

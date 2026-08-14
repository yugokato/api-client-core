from __future__ import annotations

import argparse
import inspect
import json
import mimetypes
import re
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import MISSING, Field
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum, IntEnum, StrEnum
from pathlib import Path
from types import NoneType, UnionType
from typing import Annotated, Any, Literal, NamedTuple, Union, get_args, get_origin
from uuid import UUID

from common_libs.ansi_colors import ColorCodes
from common_libs.logging import get_logger
from common_libs.utils import dedup

from api_client_core.endpoints import Endpoint
from api_client_core.endpoints.utils import endpoint_call as endpoint_call_util
from api_client_core.endpoints.utils import param_type as param_type_util
from api_client_core.endpoints.utils.endpoint_model import get_reserved_param_names
from api_client_core.types import Alias, File, Unset

from ._constants import ELLIPSIS, LOG_LEVELS, NOT_PROVIDED, RESERVED_CLI_FLAGS, WRAPPER_CHAIN_DEST, Flag
from .parser import ArgumentParser, set_full_metavar
from .utils import color_output

logger = get_logger(__name__)

_PARAMS_GROUP_TITLE = "request parameters"
_LOCATION_COLUMN_WIDTH = 8
# Indent applied to a parameter's own `:param` description, shown on its own line beneath the marker row, so it reads as
# a nested detail rather than a continuation of that row. This also marks it, to _HelpFormatter, as a detail worth
# keeping (clamped to one line) under -h, rather than the unindented detail paragraph of the summary\ndetail...
# convention other flags use, which -h drops outright. _HelpFormatter._split_lines() preserves the indent itself on a
# wrapped continuation line too.
_DESCRIPTION_INDENT = "  "
# Metavar for a repeatable flag whose element type has no metavar of its own (e.g. a JSON list), naming the
# single value each repetition takes rather than the parameter's own, often-plural name.
_LIST_METAVAR = "VALUE"
# Suffix marking a repeatable flag's own value-type column (e.g. "str[]"), so it reads as a collection rather
# than a single value of that type.
_LIST_TYPE_SUFFIX = "[]"
# The two `nargs` spellings a repeatable flag can carry: "*" as originally built, "+" once a required list is
# upgraded to reject zero values (see the required-param branch below).
_REPEATABLE_NARGS = ("*", "+")
# Matches one `:param <name>: <description>` docstring line, a common reST/Sphinx convention, once the
# line's own leading indentation has been stripped.
_PARAM_DOC_RE = re.compile(r"^:param\s+(\S+):\s*(.*)$")


class _TypeName(StrEnum):
    """CLI value-type names shown in a flag's own type column."""

    STR = "str"
    BOOL = "bool"
    PATH = "path"
    JSON = "json"


_SCALAR_TYPES = (str, int, float)  # each type's own `__name__` is the CLI value type displayed for it
# Types with no unambiguous CLI-token parser but an unambiguous plain-string wire form, sent as-is rather than falling
# back to JSON. A str subclass is handled separately, since it can't be listed here by identity.
_STRINGLY_TYPES = (datetime, date, time, UUID, Decimal)
# Container origins treated as a homogeneous, repeatable collection of one element type (nargs="*" on the CLI). A
# variable-length tuple[X, ...] is checked separately, since bare `tuple` also covers a fixed-length, heterogeneous
# tuple like tuple[str, int], which isn't this shape.
_SEQUENCE_ORIGINS = (list, set, frozenset, Sequence)
# Same collections, unparameterized (e.g. bare `list` rather than `list[X]`). get_origin() returns None for
# these, so they need their own identity check rather than joining _SEQUENCE_ORIGINS. Includes bare `tuple`,
# which _SEQUENCE_ORIGINS excludes since a *subscripted* tuple may be fixed-length and heterogeneous - a bare
# `tuple` carries no such shape and is unambiguously repeatable.
_BARE_SEQUENCE_TYPES = (*_SEQUENCE_ORIGINS, tuple)
_MAX_CHOICE_GROUP_WIDTH = 30  # width of a rendered "{a,b,c}" Literal/Enum choice group before it elides to ELLIPSIS
# Width of a union's own "a|b|c" combined type-column name before it elides. Wider than _MAX_CHOICE_GROUP_WIDTH since a
# union combines multiple members rather than one group's own choices, and reusing the same cap would elide an ordinary,
# short union like "{ACTIVE,INACTIVE}|str". A repeatable flag's own displayed type (_ArgSpec.display_type) renders up
# to len(_LIST_TYPE_SUFFIX) wider than this cap, since the "[]" suffix is appended after eliding.
_MAX_UNION_TYPE_WIDTH = 30


class _Rank(IntEnum):
    """Union-ordering rank, most restrictive first: a Literal/Enum's own choices narrow the value further
    than any scalar type, str (and any type sharing its rank) never fails to convert so it always sorts
    last.
    """

    LITERAL = 0
    FILE = 1
    BOOL = 2
    INT = 3
    FLOAT = 4
    STR = 5


_RANK_BY_SCALAR_TYPE = {int: _Rank.INT, float: _Rank.FLOAT, str: _Rank.STR}
# Attribute set on an argparse Action whose value(s) may be a filesystem path, for shell path completion.
_ACCEPTS_FILE_PATH_ATTR = "accepts_file_path"
# Attribute set on an argparse Action whose value is JSON-typed (parsed by _parse_json), so its value may
# alternatively be a `@<path>` file indirection, for shell path completion once `@` itself is typed.
_ACCEPTS_JSON_FILE_ATTR = "accepts_json_file"
# argparse dests already claimed elsewhere: the subparser dests, the parser-of-record dest (set at every
# level so runner.py can show the deepest-reached parser's own help when a command is left incomplete), and
# the wrapper-chain dest. A parameter whose resolved name matches one of these would silently overwrite
# instead of raising.
_RESERVED_DESTS = frozenset({"_command", "_endpoint", "_parser", "_resource", WRAPPER_CHAIN_DEST})
# Control kwarg names the CLI runner passes alongside the collected call kwargs. Unlike a plain flag/seen_flags
# collision, a parameter with one of these names can't be worked around by renaming its own flag: it still has to key
# the call by the parameter's real name, which the runner then passes a second time as a control kwarg, raising `got
# multiple values for keyword argument` no matter what CLI flag led to it. This includes `quiet`, even though it's also
# caught earlier as a plain `--quiet` flag collision: aliasing that flag away would otherwise leave this deeper,
# unfixable collision undetected.
_RESERVED_CALL_KWARGS = frozenset(get_reserved_param_names())
# Upper bound on how many trailing underscores _next_free_alias() will try before giving up. Generous: an
# endpoint would need this many literal flag collisions in a row for it to ever matter.
_MAX_ALIAS_SUFFIX_LEN = 8

_JSON_STDIN_TOKEN = "-"  # value reading a JSON-typed flag's whole value from stdin
_JSON_FILE_PREFIX = "@"  # value prefix reading a JSON-typed flag's whole value from the named file
# Tokens that open a JSON container or string, so a value starting with one is unambiguously meant as JSON.
_JSON_OPENERS = (_JSON_FILE_PREFIX, "{", "[", '"')
# Every type json.loads() can return, named the way JSON itself names them, for reporting the shape a
# whole-value `-`/`@<path>` indirection actually read. Keyed by exact type rather than isinstance(), since
# isinstance(True, int) is True and would misreport a boolean document as a number.
_JSON_TYPE_NAMES = {
    dict: "an object",
    list: "an array",
    str: "a string",
    int: "a number",
    float: "a number",
    bool: "a boolean",
    NoneType: "null",
}


def add_endpoint_arguments(parser: argparse.ArgumentParser, endpoint: Endpoint[Any]) -> None:
    """Add one CLI flag per endpoint parameter to a leaf subparser, in its own `endpoint parameters` group.

    A parameter whose derived flag collides with a reserved CLI flag or another parameter's own flag is exposed
    under a trailing-underscore alias (`--flag_`, `--flag__`, ...) instead, so it stays reachable rather than
    silently dropped. A parameter whose type can't be mapped to a CLI flag at all still gets one, falling back
    to a JSON-parsed flag with a blank type column, so one unrecognized annotation degrades that single
    parameter rather than the whole command. A parameter with no annotation at all gets the same blank-column
    shape deliberately, since it never claimed a type to show.

    A parameter documented with its own `:param <name>: ...` line in the endpoint function's docstring shows
    that description on its own line beneath the existing location/type/marker line: in full under `--help`,
    clamped to that one line under `-h`.

    :param parser: Leaf subparser for a single endpoint command
    :param endpoint: Endpoint whose parameter model drives the generated arguments
    """
    group = parser.add_argument_group(_PARAMS_GROUP_TITLE)
    sig = endpoint_call_util.get_params_signature(endpoint.original_func)
    resolved = list(_resolve_params(endpoint, sig, warn=True))
    _, param_docs = split_param_docs(endpoint.original_func.__doc__)
    # Widest displayed value-type name among this endpoint's parameters, so every row's marker column aligns
    # regardless of how long the preceding type name is. 0 when no parameter has one, in which case the column
    # is omitted entirely rather than padded to nothing.
    type_width = max((len(spec.display_type) for *_, spec in resolved if spec.display_type), default=0)
    for field, param_name, dest, flag, spec in resolved:
        sig_param = sig.parameters.get(param_name)
        required = sig_param is not None and sig_param.default is inspect.Parameter.empty

        arg_kwargs = spec.kwargs
        arg_kwargs["dest"] = dest
        original_flag = _flag_for(param_name)
        renamed_from = original_flag if flag != original_flag else None
        try:
            arg_kwargs["help"] = _help_text(
                endpoint,
                field,
                required=required,
                spec=spec,
                type_width=type_width,
                renamed_from=renamed_from,
                description=param_docs.get(param_name),
            )
        except Exception as e:
            # A pathological __repr__ on a default value shouldn't take out the whole command: fall back to the bare
            # location marker for this one parameter's help.
            logger.warning(
                f"{endpoint.api_class.__name__}.{endpoint.func_name}: Failed to build help text for parameter "
                f"{param_name!r}: {type(e).__name__}: {e}"
            )
            arg_kwargs["help"] = f"[{_param_location(endpoint, field)}]"
        if arg_kwargs.get("nargs") in _REPEATABLE_NARGS and "metavar" not in arg_kwargs:
            # Names the single value each repetition takes, rather than argparse's own dest-derived metavar,
            # which is usually the parameter's own plural name (e.g. "products") and reads as if the whole
            # collection were expected per value.
            arg_kwargs["metavar"] = _LIST_METAVAR
        if required:
            arg_kwargs["required"] = True
            if arg_kwargs.get("nargs") == "*":
                # A required list must accept at least one value: nargs="*" would otherwise also accept zero.
                arg_kwargs["nargs"] = "+"
        else:
            arg_kwargs["default"] = NOT_PROVIDED

        try:
            action = group.add_argument(flag, **arg_kwargs)
        except Exception as e:
            # An unexpected argparse-level failure (e.g. a still-unforeseen action/flag interaction) shouldn't take out
            # the whole command either: skip just this one parameter.
            logger.warning(
                f"{endpoint.api_class.__name__}.{endpoint.func_name}: Unable to add option {flag!r} for "
                f"parameter {param_name!r}: {type(e).__name__}: {e}"
            )
            continue
        if spec.accepts_file_path:
            setattr(action, _ACCEPTS_FILE_PATH_ATTR, True)
        elif spec.is_json:
            setattr(action, _ACCEPTS_JSON_FILE_ATTR, True)
        if spec.full_metavar is not None:
            set_full_metavar(action, spec.full_metavar)


def collect_call_kwargs(endpoint: Endpoint[Any], namespace: argparse.Namespace) -> dict[str, Any]:
    """Build endpoint call kwargs from a parsed namespace, omitting flags the caller didn't provide.

    Keys are the original signature parameter names, not the model field names, so the endpoint call actually
    binds each value to its target parameter. Read back from `dest`, not `param_name`: the two differ for a
    parameter whose own flag was renamed to a trailing-underscore alias, since its value is parsed into that
    alias's own dest, not one named after the parameter itself. A parameter skipped or never registered as a
    flag is skipped here identically, via the `getattr()` default below, since no namespace attribute was
    ever added for it.

    :param endpoint: Endpoint the parsed namespace was built for
    :param namespace: Namespace produced by parsing the endpoint's registered arguments
    """
    sig = endpoint_call_util.get_params_signature(endpoint.original_func)
    call_kwargs: dict[str, Any] = {}
    for _f, param_name, dest, _flag, _spec in _resolve_params(endpoint, sig):
        value = getattr(namespace, dest, NOT_PROVIDED)
        if value is NOT_PROVIDED:
            continue
        if isinstance(value, Path):
            value = _to_file(value)
        elif isinstance(value, list):
            value = [_to_file(v) if isinstance(v, Path) else v for v in value]
        call_kwargs[param_name] = value
    return call_kwargs


def normalize_call_args(
    func: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Move keyword values that target positional-only parameters into the positional arguments.

    :param func: Original API function
    :param args: Positional arguments from the endpoint call
    :param kwargs: Keyword arguments from the endpoint call
    """
    sig = endpoint_call_util.get_params_signature(func)
    positional_only = [p for p in sig.parameters.values() if p.kind is inspect.Parameter.POSITIONAL_ONLY]
    unfilled = positional_only[len(args) :]
    last_named = max((i for i, p in enumerate(unfilled) if p.name in kwargs), default=-1)
    if last_named < 0:
        return args, kwargs

    new_args = list(args)
    kwargs = dict(kwargs)
    for param in unfilled[: last_named + 1]:
        if param.name in kwargs:
            new_args.append(kwargs.pop(param.name))
        elif param.default is inspect.Parameter.empty:
            raise TypeError(
                f"{func.__name__}() cannot accept {unfilled[last_named].name!r} as a keyword argument without a "
                f"value for the preceding positional-only parameter {param.name!r}. Give {param.name!r} a value, "
                f"or pass the positional-only parameters positionally."
            )
        else:
            new_args.append(param.default)
    return tuple(new_args), kwargs


def peek_log_level(argv: list[str]) -> str | None:
    """Peek `--log-level` out of `argv` without consuming it.

    Used to configure logging ahead of discovery, before the real parser, which also defines this flag, is
    even built. `argv` is later parsed again in full by the real parser, unmodified, so this peek only needs
    a best-effort read rather than full validation.

    Deliberately doesn't constrain the value to its real choices: an invalid value is checked, and nulled
    out, here rather than raising, so a bad `--log-level` doesn't block discovery from running at all -
    parsing fails properly later, once the real parser sees it. Case-insensitive, matching the real flag's
    own `type=str.upper`.

    :param argv: Argument list to peek the flag out of, forwarded to the real parser unmodified
    """
    parser = ArgumentParser(add_help=False, exit_on_error=False)
    parser.add_argument(Flag.LOG_LEVEL, default=None)
    try:
        args, _ = parser.parse_known_args(argv)
    except (argparse.ArgumentError, SystemExit):
        return None
    log_level = args.log_level.upper() if args.log_level else None
    return log_level if log_level in LOG_LEVELS else None


class _ArgSpec(NamedTuple):
    """`argparse.add_argument()` keyword arguments for one endpoint parameter, with its CLI value type.

    :param kwargs: Keyword arguments to pass to `argparse.add_argument()`
    :param value_type: CLI element/atom value type, or `None` when the flag takes no value or its accepted
                       type cannot be named. `display_type` is the type actually shown in help, since a
                       repeatable flag shows this with a trailing `[]`
    :param accepts_file_path: Whether the flag's value(s) may be a filesystem path, for shell path completion
    :param full_metavar: A Literal/Enum choice group's full, unelided form, shown in place of `kwargs`'s own
                         (possibly elided) `metavar` under `--help`, or `None` when `kwargs` sets no `metavar`
    """

    kwargs: dict[str, Any]
    value_type: str | None
    accepts_file_path: bool = False
    full_metavar: str | None = None

    @property
    def display_type(self) -> str | None:
        """CLI value type to display, `value_type` suffixed with `[]` when the flag is repeatable.

        Keyed off `kwargs["nargs"]` rather than appended at each list-producing call site, so "repeatable ⇔
        `[]`" holds by construction instead of needing to be kept in sync by hand. Checks both `nargs`
        spellings a repeatable flag can carry (`_REPEATABLE_NARGS`): the type-column width is computed before
        a required list's own `nargs` is upgraded from `"*"` to `"+"`, so both must read as repeatable for
        the width and the rendered value to stay in agreement.
        """
        if not self.value_type:
            return self.value_type
        if self.kwargs.get("nargs") in _REPEATABLE_NARGS:
            return f"{self.value_type}{_LIST_TYPE_SUFFIX}"
        return self.value_type

    @property
    def is_json(self) -> bool:
        """Whether the flag's value is JSON-parsed, so it also accepts a `-`/`@<path>` indirection.

        For a repeatable flag (`_JsonListAction`), the indirection supplies the flag's whole value rather
        than one element of it.
        """
        if self.kwargs.get("type") in (_parse_json, _parse_json_or_str):
            return True
        return isinstance(self.kwargs.get("action"), type) and issubclass(self.kwargs["action"], _JsonListAction)


def _resolve_params(
    endpoint: Endpoint[Any], sig: inspect.Signature, *, warn: bool = False
) -> Iterator[tuple[Field[Any], str, str, str, _ArgSpec]]:
    """Yield each usable model field's `(field, param_name, dest, flag, spec)`.

    A field whose derived flag collides with a reserved CLI flag or one already yielded for an earlier field
    of the same endpoint is renamed to a trailing-underscore alias (`--flag_`, trying one more `_` each time
    that still collides, up to `_MAX_ALIAS_SUFFIX_LEN`) with a matching `param_name_` dest, rather than
    dropped, so it stays reachable from the CLI. `dest` differs from `param_name` only for such an aliased
    field: it is what the parser action is actually registered under, while `param_name` is what
    `collect_call_kwargs()` must still key the real function call by. A `bool` field registers both
    `--flag`/`--no-flag` (or their aliased form), so both forms are checked before the field is accepted.

    A field is dropped outright, with no alias attempted, when its resolved parameter name itself collides
    with a reserved argparse dest or control kwarg - renaming the flag can't fix this, since the value would
    still reach the endpoint call a second time under the very keyword name it's dispatched under - when it
    resolves to the bare `--` option-terminator token, or on the rare case where even its own alias still
    collides.

    A field whose type can't be mapped at all still gets a flag, falling back to a JSON-parsed one with no
    displayed value type, rather than dropping the entire command over one parameter. A field with no
    annotation at all reaches the same blank-column shape by its own dedicated route in `_arg_spec()`, since
    it never claimed a type to show, but still accepts a plain string when the value isn't valid JSON.

    Shared by two callers so both skip and rename identically: neither adds the flag to the parser under a
    dest the other wouldn't also read a value back from. Only the parser-building call passes `warn=True`,
    so a skipped, aliased, or fallen-back field is logged once per parser build rather than on every
    dispatched call.

    :param endpoint: Endpoint whose parameter model to resolve
    :param sig: Original endpoint function's signature, used to resolve each field back to its parameter name
    :param warn: Log a diagnostic for each skipped, aliased, or fallen-back field. Only the parser-building
                pass should set this
    """
    seen_flags: set[str] = set()
    seen_dests: set[str] = set()
    for name, field in endpoint.model.__dataclass_fields__.items():
        param_name = _resolve_signature_name(name, field.type, sig)
        flag = _flag_for(param_name)
        try:
            spec = _arg_spec(field.type, flag)
        except Exception as e:
            if warn:
                logger.warning(
                    f"{endpoint.api_class.__name__}.{endpoint.func_name}: Unable to determine a CLI type for "
                    f"parameter {param_name!r} (annotation: {field.type!r}): {type(e).__name__}: {e}. Falling back "
                    f"to a JSON-typed flag for this parameter."
                )
            spec = _ArgSpec({"type": _parse_json}, None)

        if flag == "--" or param_name in _RESERVED_DESTS or param_name in _RESERVED_CALL_KWARGS:
            if warn:
                logger.debug(
                    f"{endpoint.api_class.__name__}.{endpoint.func_name}: Unable to map the parameter "
                    f"{param_name!r} to an option: its name is invalid or already reserved for CLI dispatch/call "
                    f"control, and renaming its flag can't work around that. Re-run with --log-level DEBUG on "
                    f"any command to see this again."
                )
            continue

        is_bool_pair = spec.kwargs.get("action") is argparse.BooleanOptionalAction
        dest = param_name
        registered_flags: tuple[str, ...] = (flag, f"--no-{flag.removeprefix('--')}") if is_bool_pair else (flag,)
        if any(rf in RESERVED_CLI_FLAGS or rf in seen_flags for rf in registered_flags):
            original_flag = flag
            alias = _next_free_alias(original_flag, param_name, is_bool_pair, seen_flags, seen_dests)
            if alias is None:
                if warn:
                    logger.debug(
                        f"{endpoint.api_class.__name__}.{endpoint.func_name}: Unable to map the parameter "
                        f"{param_name!r} to an option: {original_flag!r} is already reserved for CLI "
                        f"dispatch/call control or used by another parameter, and every trailing-underscore "
                        f"fallback tried is too."
                    )
                continue
            flag, dest, registered_flags = alias
            if warn:
                logger.debug(
                    f"{endpoint.api_class.__name__}.{endpoint.func_name}: Parameter {param_name!r} exposed as "
                    f"{flag!r} instead of {original_flag!r}, which is already reserved for CLI dispatch/call "
                    f"control or used by another parameter."
                )

        seen_flags.update(registered_flags)
        seen_dests.add(dest)
        yield field, param_name, dest, flag, spec


def _next_free_alias(
    flag: str, param_name: str, is_bool_pair: bool, seen_flags: set[str], seen_dests: set[str]
) -> tuple[str, str, tuple[str, ...]] | None:
    """Find the first non-colliding `--<flag>_`, `--<flag>__`, ... alias for a flag that collides with a
    reserved CLI flag or an earlier parameter's own flag, mirroring the trailing-underscore convention
    Python itself uses to dodge a keyword clash (e.g. `class_`). Returns `(flag, dest, registered_flags)`.

    Tried up to `_MAX_ALIAS_SUFFIX_LEN` trailing underscores, a generous bound given the loop can only ever
    need as many attempts as there are already-registered flags/dests to dodge (bounded by one endpoint's
    own parameter count) before landing on a free one. Returns `None` if every attempt up to that bound
    still collides.

    :param flag: The parameter's own naturally-derived flag, already known to collide
    :param param_name: The parameter's original signature name, used to derive the alias's own dest
    :param is_bool_pair: Whether `flag` registers a `--flag`/`--no-flag` pair rather than a single flag
    :param seen_flags: Every flag (and `--no-` negation) already registered for an earlier field of the
                       same endpoint
    :param seen_dests: Every dest already registered for an earlier field of the same endpoint
    """
    suffix = "_"
    while len(suffix) <= _MAX_ALIAS_SUFFIX_LEN:
        alias_flag = f"{flag}{suffix}"
        alias_dest = f"{param_name}{suffix}"
        alias_registered_flags = (
            (alias_flag, f"--no-{alias_flag.removeprefix('--')}") if is_bool_pair else (alias_flag,)
        )
        conflict = (
            alias_dest in seen_dests
            or alias_dest in _RESERVED_DESTS
            or alias_dest in _RESERVED_CALL_KWARGS
            or any(rf in RESERVED_CLI_FLAGS or rf in seen_flags for rf in alias_registered_flags)
        )
        if not conflict:
            return alias_flag, alias_dest, alias_registered_flags
        suffix += "_"
    return None


def _resolve_signature_name(field_name: str, field_type: Any, sig: inspect.Signature) -> str:
    """Resolve a model field back to the original signature parameter name it was derived from.

    A model field is renamed away from its signature name when it collides with a reserved name or a path
    parameter of the same name, in which case its `Alias` metadata holds the original name. Driving the CLI
    flag and endpoint call by the renamed name would either miss the target parameter entirely or have its
    value silently absorbed into `**kwargs`.

    :param field_name: Model field name
    :param field_type: Model field's resolved type annotation
    :param sig: Original endpoint function's signature
    """
    if field_name in sig.parameters:
        return field_name
    annotated = param_type_util.get_annotated_type(field_type, metadata_filter=Alias)
    candidates = annotated if isinstance(annotated, list | tuple) else ([annotated] if annotated else [])
    for candidate in candidates:
        for meta in candidate.__metadata__:
            if isinstance(meta, Alias) and meta.value in sig.parameters:
                return meta.value
    return field_name


def _flag_for(param_name: str) -> str:
    """Derive a parameter's CLI flag from its original signature name.

    Strips leading/trailing underscores (e.g. `from_`, `_internal`) and collapses any internal underscore run
    to a single `-`, so an unusual name still produces a well-formed flag (e.g. `_internal` -> `--internal`,
    `a__b` -> `--a-b`).

    :param param_name: Original signature parameter name
    """
    return f"--{re.sub('_+', '-', param_name.strip('_'))}"


def _arg_spec(annotation: Any, flag: str) -> _ArgSpec:
    """Map a resolved parameter type annotation to `argparse.add_argument` keyword arguments and a CLI element/
    atom value type (`_ArgSpec.value_type`; see `_ArgSpec.display_type` for the type as actually shown in help).

    A parameter with no annotation at all gets a JSON-parsed flag like any other unmapped type, but with no
    displayed value type, since it never claimed one: `str`/`int`/`bool`/... would misrepresent a type the
    author never declared, and `json` would overclaim a specific format they didn't ask for either.

    :param annotation: Resolved type annotation of one endpoint parameter field
    :param flag: The parameter's own derived CLI flag, needed to special-case a `bool` whose flag already
                 starts with `--no-`
    """
    base = _effective_type(annotation)
    if base is inspect.Parameter.empty:
        return _ArgSpec({"type": _parse_json_or_str}, None)
    if base is bool:
        if flag.startswith("--no-"):
            # BooleanOptionalAction would pair this with a nonsensical "--no-no-..." negation on Python 3.11-3.13, and
            # Python 3.14 rejects registering a `--no-`-prefixed option under it outright (ValueError: invalid option
            # name ... for BooleanOptionalAction). A `no_x`-named parameter already reads as a negation, so a single
            # flag (present -> True) is both correct and simpler.
            return _ArgSpec({"action": "store_true"}, None)
        return _ArgSpec({"action": argparse.BooleanOptionalAction}, None)

    origin = get_origin(base)
    if origin in (Union, UnionType):
        # A genuine multi-type union. A single-non-None Optional was already unwrapped to its bare type above.
        members = tuple(m for m in get_args(base) if m is not NoneType)
        scalar_or_list_spec = _scalar_or_list_spec(members)
        if scalar_or_list_spec is not None:
            return scalar_or_list_spec
        union_spec = _union_value_spec(members)
        if union_spec is not None:
            return _ArgSpec({"type": union_spec.converter}, union_spec.value_type, union_spec.accepts_file_path)
        return _ArgSpec({"type": _parse_json}, _TypeName.JSON)
    elem_type = _sequence_elem_type(base)
    if elem_type is not None:
        # Recurses through the same single-token mapping a scalar of this type would use, so e.g.
        # list[SomeEnum]/list[Literal[...]] keep their own choices validation instead of degrading to str, and
        # list[dict[str, Any]] gets JSON-parsed per element instead of being sent as literal strings. Applies equally to
        # tuple[X, ...], set[X], frozenset[X], and Sequence[X]: the request-building layer applies no runtime validation
        # against the declared container type, so handing it the plain list the CLI naturally collects works the same
        # way list[X] already does.
        elem_spec = _value_spec(elem_type)
        if elem_spec is None:
            # An unparameterized collection (elem_type is the "no type declared" sentinel) gets the lenient,
            # JSON-or-string element parse; a declared-but-unmapped element type (e.g. list[dict[str, Any]])
            # keeps the strict, JSON-only one.
            action = _LenientJsonListAction if elem_type is inspect.Parameter.empty else _JsonListAction
            return _ArgSpec({"nargs": "*", "action": action}, _TypeName.JSON)
        return _ArgSpec(
            {"nargs": "*", "type": elem_spec.converter, **elem_spec.extra},
            elem_spec.value_type,
            elem_spec.accepts_file_path,
            elem_spec.full_metavar,
        )

    value_spec = _value_spec(base)
    if value_spec is not None:
        return _ArgSpec(
            {"type": value_spec.converter, **value_spec.extra},
            value_spec.value_type,
            value_spec.accepts_file_path,
            value_spec.full_metavar,
        )
    return _ArgSpec({"type": _parse_json}, _TypeName.JSON)


class _ValueSpec(NamedTuple):
    """A single-token CLI mapping for one annotation. May describe a whole parameter's own annotation, or just one
    member of a larger union.

    :param converter: `argparse` `type=` converter for one CLI token
    :param value_type: CLI value type name to display when this spec stands on its own (e.g. `str`/`int`/`bool`)
    :param rank: Restrictiveness used to order a union's members, most restrictive first
    :param extra: Additional `argparse.add_argument()` keyword arguments (`choices`/`metavar`), if any
    :param accepts_file_path: Whether a value may be a filesystem path, for shell path completion
    :param names: Atomic display name(s) contributed to a union's own combined `value_type` when this spec is one of
                  its members. A `Literal`/`Enum` contributes its own choice group (e.g. `{ACTIVE,INACTIVE}`) rather
                  than its plain `value_type`, so the choices stay visible in the union's combined name.
                  An already-chained spec contributes its own deduped names, flattening nested unions into one topmost
                  display
    :param full_metavar: A Literal/Enum choice group's full, unelided form, shown in place of `extra`'s own (possibly
                         elided) `metavar` under `--help`, or `None` when `extra` sets no `metavar`
    """

    converter: Callable[[str], Any]
    value_type: str
    rank: int
    extra: dict[str, Any]
    accepts_file_path: bool
    names: tuple[str, ...]
    full_metavar: str | None = None


def _value_spec(annotation: Any, *, strict: bool = False) -> _ValueSpec | None:
    """Map an annotation with a single-token CLI form (`str`/`int`/`float`/`bool`/`File`/`Enum`/`Literal`) to an
    `argparse` converter, a displayable CLI value type, and its union-ordering rank.

    Returns `None` for an annotation with no such form (e.g. `dict`, a bare `list`, an unresolved forward reference),
    so the caller falls back to JSON. Used both for a whole parameter's own top-level annotation and, recursively,
    for one member of a larger union - `strict` only matters for the latter.

    :param annotation: Type annotation to map. May be one member of a larger union rather than a parameter's own
                       top-level annotation
    :param strict: For a `Literal`, raise instead of returning the token unchanged when nothing matches.
                   Irrelevant to every other annotation shape, which either always raises on a bad token
                   (`Enum`, `bool`, `int`, `float`, `File`) or never does (`str`, and the stringly types)
    """
    if param_type_util.is_type_of(annotation, File):
        metavar = _TypeName.PATH.upper()
        return _ValueSpec(_existing_file, _TypeName.PATH, _Rank.FILE, {"metavar": metavar}, True, (_TypeName.PATH,))

    base = _effective_type(annotation)
    if base is bool:
        return _ValueSpec(_parse_bool, _TypeName.BOOL, _Rank.BOOL, {}, False, (_TypeName.BOOL,))

    origin = get_origin(base)
    if origin is Literal:
        choices = get_args(base)
        if not choices:
            return _ValueSpec(str, _TypeName.STR, _Rank.STR, {}, False, (_TypeName.STR,))
        converter = _choice_converter(choices, strict=strict)
        group = _format_choice_group(choices)
        extra: dict[str, Any] = {} if strict else {"choices": list(choices), "metavar": group}
        full_metavar = None if strict else _format_choice_group_full(choices)
        return _ValueSpec(converter, _literal_value_type(choices), _Rank.LITERAL, extra, False, (group,), full_metavar)
    if inspect.isclass(base) and issubclass(base, Enum):
        enum_members = list(base)
        group = _format_choice_group(enum_members)
        return _ValueSpec(
            _enum_converter(base),
            _TypeName.STR,
            _Rank.LITERAL,
            {"metavar": group},
            False,
            (group,),
            _format_choice_group_full(enum_members),
        )
    if origin in (Union, UnionType):
        members = tuple(m for m in get_args(base) if m is not NoneType)
        return _union_value_spec(members)
    if base in _SCALAR_TYPES:
        return _ValueSpec(base, base.__name__, _RANK_BY_SCALAR_TYPE[base], {}, False, (base.__name__,))
    if base in _STRINGLY_TYPES or (inspect.isclass(base) and issubclass(base, str)):
        return _ValueSpec(str, _TypeName.STR, _Rank.STR, {}, False, (_TypeName.STR,))
    return None


def _union_value_spec(members: tuple[Any, ...]) -> _ValueSpec | None:
    """Map a union's non-`None` members to one chained `_ValueSpec`, when every member itself has a single-token CLI
    form.

    Tries each member's own converter in rank order (most restrictive first) until one succeeds, so e.g. `Status | str`
    converts `"ACTIVE"` to the enum member rather than leaving it the bare string, and `int | str` converts `"42"` to
    `42` rather than leaving it `"42"`. Each member is resolved strictly, so a `Literal`/`Enum` member raises on a
    non-matching token instead of falling through as an unmatched raw string, letting the chain try its next, less
    restrictive member.

    :param members: Union members, with `NoneType` already excluded
    """
    specs: list[_ValueSpec] = []
    for member in members:
        spec = _value_spec(member, strict=True)
        if spec is None:
            return None
        specs.append(spec)

    ordered = tuple(sorted(specs, key=lambda s: s.rank))
    names = dedup(*(name for s in ordered for name in s.names))
    return _ValueSpec(
        _chained_converter(ordered, names),
        _elide_joined(names, sep="|", max_width=_MAX_UNION_TYPE_WIDTH),
        ordered[0].rank,
        {},
        any(s.accepts_file_path for s in ordered),
        names,
    )


def _scalar_or_list_spec(members: tuple[Any, ...]) -> _ArgSpec | None:
    """Map a union of exactly `S | list[X]` (`S` may itself be a union of several non-list members, and may
    share a type with `X`, or differ from it) to an `_ArgSpec`, when both sides have a single-token CLI form.
    `list[X]` may equally be `tuple[X, ...]`, `set[X]`, `frozenset[X]`, or `Sequence[X]`.

    `nargs="*"` accepts any number of tokens, and conversion is deferred until their count is known: a single token
    converts with `S`'s own type and is stored bare, two or more each convert with `X`'s.

    Each member's own effective type is checked individually, so a per-member `Annotated` wrapping (rather than an
    outer one) is still recognized instead of falling through to the union's own JSON fallback.

    :param members: Union members, with `NoneType` already excluded
    """
    list_indices = [i for i, m in enumerate(members) if _sequence_elem_type(_effective_type(m)) is not None]
    if len(list_indices) != 1:
        return None
    (list_index,) = list_indices
    elem_type = _sequence_elem_type(_effective_type(members[list_index]))
    scalar_members = tuple(m for i, m in enumerate(members) if i != list_index)
    if not scalar_members:
        return None

    # A lone scalar member keeps its own spec, rather than being wrapped in a single-member union chain, so a bad
    # single-token value still fails with that type's own clean "invalid <name> value: ..." message instead of a chain's
    # "expected one of <name>: ..." wording meant for an actual multi-member choice.
    scalar_spec = (
        _value_spec(scalar_members[0], strict=True) if len(scalar_members) == 1 else _union_value_spec(scalar_members)
    )
    if scalar_spec is None:
        return None
    elem_spec = _value_spec(elem_type, strict=True)
    if elem_spec is None:
        return None

    names = dedup(*scalar_spec.names, *elem_spec.names)
    action = _scalar_or_list_action(scalar_spec, elem_spec)
    accepts_file_path = scalar_spec.accepts_file_path or elem_spec.accepts_file_path
    return _ArgSpec(
        {"nargs": "*", "action": action},
        _elide_joined(names, sep="|", max_width=_MAX_UNION_TYPE_WIDTH),
        accepts_file_path,
    )


def _literal_value_type(choices: tuple[Any, ...]) -> str:
    """Name the CLI value type for a `Literal[...]`'s choices: the shared type's own name when every choice
    is a `str`/`int`/`float`/`bool` of the same type, else `str`, still safely representable as a single CLI
    token, just not nameable as one Python type (e.g. a mixed `Literal["a", 1]`).

    :param choices: `Literal[...]`'s allowed values
    """
    types = {type(c) for c in choices}
    if len(types) == 1:
        (choice_type,) = types
        if choice_type is bool:
            return _TypeName.BOOL
        if choice_type in _SCALAR_TYPES:
            return choice_type.__name__
    return _TypeName.STR


_stdin_consumed = [False]  # single-element list (not bool) so `read_stdin_text()` mutates it without `global`


def reset_stdin_state() -> None:
    """Reset the "stdin already read for a `-` value" tracking ahead of parsing one command.

    `read_stdin_text()`'s own tracking is a plain module-level flag, which by itself would persist for the
    whole process rather than just the one command being parsed. That distinction is invisible to the real
    `api-client` process, which parses exactly one command per process lifetime, but not to a caller that
    dispatches more than one command in the same process (e.g. an embedding harness, or this package's own
    test suite). `runner.py`'s `run()` calls this once, immediately before `parser.parse_args()`, so each
    dispatched command gets its own fresh "one `-` allowed" budget regardless of what an earlier command in
    the same process already consumed. The budget is shared across every flag that accepts a `-` indirection
    (a JSON-typed value, and `-H`/`--header`), not tracked per flag, since stdin itself is a single, shared
    stream no two flags in the same command can each get their own read of.
    """
    _stdin_consumed[0] = False


def _is_json_indirection(value: str) -> bool:
    """Whether a `--flag` value is a `-`/`@<path>` indirection reading its JSON from stdin or a file, rather
    than being the JSON document itself.

    :param value: Raw CLI value to check
    """
    return value == _JSON_STDIN_TOKEN or value.startswith(_JSON_FILE_PREFIX)


def _parse_json(value: str) -> Any:
    """Parse a `--flag` value as JSON, for a parameter whose type has no more specific mapping.

    A value of `-` reads the JSON from stdin instead of the token itself, and a value starting with `@`
    reads it from the named file (`@<path>`), so a real payload doesn't have to be inlined on the command
    line. Used as the `type=` callable so a malformed value, an unreadable file, or a second `-` is rejected
    by `argparse` itself during parsing, rather than raising once parsing has already completed.

    :param value: Raw CLI value to parse as JSON, or a `-`/`@<path>` indirection to read it from
    """
    if value == _JSON_STDIN_TOKEN:
        source = read_stdin_text()
    elif value.startswith(_JSON_FILE_PREFIX):
        source = read_file_text(value[1:])
    else:
        source = value
    try:
        return json.loads(source)
    except json.JSONDecodeError:
        raise argparse.ArgumentTypeError(f"invalid JSON: {source!r}") from None


def _parse_json_or_str(value: str) -> Any:
    """Parse a `--flag` value as JSON, keeping it a plain string when it isn't valid JSON.

    Used for a parameter with no annotation at all. Its flag advertises no CLI value type, so a bare `hello`
    has to stay a string rather than being rejected as malformed JSON.

    A value that opens a JSON container or string, or that is a `-`/`@<path>` indirection, is still parsed
    strictly: each is an unambiguous request for a JSON document, so a decode failure there is a typo worth
    reporting rather than a plain string worth keeping. Silently sending `{"a": 1` (a missing brace) as a
    string would surface only as a confusing server-side error, far from its cause.

    :param value: Raw CLI value to parse as JSON, or a `-`/`@<path>` indirection to read it from
    """
    if value == _JSON_STDIN_TOKEN or value.startswith(_JSON_OPENERS):
        return _parse_json(value)
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def read_stdin_text() -> str:
    """Read a flag's whole value from stdin, for a `-` given as its CLI value.

    Shared by every flag that accepts a `-`/`@<path>` indirection (a JSON-typed value, and `-H`/`--header`),
    so a value that happens to hold sensitive data (a bearer token, a password) never has to be typed in
    argv, where it would otherwise be visible in shell history and to other processes on the same machine.

    Raises once stdin has already been consumed by an earlier `-` in the same command: stdin is a stream,
    not a re-readable resource, so a second `-` would otherwise silently read empty content rather than
    failing loudly. Also raises up front when stdin is a terminal rather than a pipe/file/redirect, since
    `sys.stdin.read()` would otherwise block forever waiting for input that was never meant to come from a
    human typing at the prompt, with no indication of what it's waiting for.
    """
    if _stdin_consumed[0]:
        raise argparse.ArgumentTypeError("'-' (stdin) can only be used for one parameter per command")
    if sys.stdin.isatty():
        raise argparse.ArgumentTypeError(
            "'-' reads from stdin, but stdin is a terminal. Pipe or redirect input instead"
        )
    _stdin_consumed[0] = True
    return sys.stdin.read()


def read_file_text(path_str: str) -> str:
    """Read a flag's whole value from a file, for an `@<path>` given as its CLI value.

    Shared by every flag that accepts a `-`/`@<path>` indirection (a JSON-typed value, and `-H`/`--header`),
    so a value that happens to hold sensitive data (a bearer token, a password) never has to be typed in
    argv, where it would otherwise be visible in shell history and to other processes on the same machine.

    :param path_str: The file path given after `@`
    """
    try:
        return Path(path_str).read_text(encoding="utf-8")
    except OSError as e:
        raise argparse.ArgumentTypeError(f"cannot read '{path_str}': {e.strerror or e}") from None
    except UnicodeDecodeError:
        raise argparse.ArgumentTypeError(f"cannot read '{path_str}': not valid UTF-8 text") from None


def _existing_file(value: str) -> Path:
    """Parse a `--flag` value as a `Path` to an existing, readable file.

    Used as the `type=` callable for a `File` field so a missing or non-file path is rejected by `argparse`
    itself during parsing, rather than raising once parsing has already completed and the value is read.

    :param value: Raw CLI value for a `File` flag
    """
    path = Path(value)
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"no such file: {value!r}")
    return path


def _to_file(path: Path) -> File:
    """Convert a `Path` parsed from a `File`-typed CLI flag (scalar or list element) into a `File`.

    :param path: Existing file path to convert
    """
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    # mypy misreads File as abstract because of its DataclassModel Protocol base. @dataclass supplies a concrete
    # __init__ at runtime.
    return File(path.name, path.read_bytes(), content_type)  # type: ignore[abstract]


def _effective_type(annotation: Any) -> Any:
    """Strip `Annotated[]` metadata and `Optional`/`T | None` wrapping down to the base type.

    :param annotation: Type annotation to unwrap
    """
    origin = get_origin(annotation)
    if origin is Annotated:
        return _effective_type(get_args(annotation)[0])
    if origin in (Union, UnionType):
        non_none = [a for a in get_args(annotation) if a is not NoneType]
        if len(non_none) == 1:
            return _effective_type(non_none[0])
    return annotation


def _sequence_elem_type(base: Any) -> Any | None:
    """Return the declared element type `X` for a homogeneous, repeatable-collection annotation - `list[X]`,
    `tuple[X, ...]`, `set[X]`, `frozenset[X]`, or `collections.abc.Sequence[X]` - or `None` for anything else,
    including a fixed-length, heterogeneous tuple (e.g. `tuple[str, int]`), which has no single element type.

    An unparameterized collection (bare `list`, `tuple`, `set`, `frozenset`, or `Sequence`) is repeatable
    too, but declares no element type: it returns `inspect.Parameter.empty`, the same sentinel `_arg_spec()`
    already uses for "no type was declared here", so the caller falls back to a JSON-or-string element parse
    rather than either a scalar `str` guess or a single JSON-document flag.

    :param base: Already-unwrapped annotation to inspect
    """
    origin = get_origin(base)
    if origin in _SEQUENCE_ORIGINS:
        (elem_type,) = get_args(base) or (inspect.Parameter.empty,)
        return elem_type
    if origin is tuple:
        args = get_args(base)
        if len(args) == 2 and args[1] is Ellipsis:
            return args[0]
        return None
    if base in _BARE_SEQUENCE_TYPES:
        return inspect.Parameter.empty
    return None


def _enum_converter(cls: type[Enum]) -> Callable[[str], Enum]:
    """Build an `argparse` `type=` callable that converts a member-name string to an enum member.

    Raising `argparse.ArgumentTypeError` (rather than a bare `ValueError`) here, instead of via `choices=` on the
    action itself, lets the message list the accepted names (matching a `Literal` flag's own `choose from ...`
    wording) without argparse falling back to each choice's raw `repr()` (e.g. `<Status.ACTIVE: 'active'>`) the
    way `choices=list(cls)` would produce.

    :param cls: Enum class the flag's value should be converted to
    """

    def convert(value: str) -> Enum:
        try:
            return cls[value]
        except KeyError:
            raise argparse.ArgumentTypeError(
                f"invalid choice: {value!r} (choose from {', '.join(m.name for m in cls)})"
            ) from None

    convert.__name__ = cls.__name__
    return convert


def _parse_bool(value: str) -> bool:
    """Parse a `--flag` value as a `bool`, for a `bool` appearing inside a `list[...]`/union rather than as a
    standalone flag (a standalone `bool` field instead uses `argparse.BooleanOptionalAction`, which takes no
    value at all).

    Accepts `true`/`false` case-insensitively, matching JSON's own boolean spelling, rather than e.g. `1`/`0`,
    which would make a `bool` sharing a union or list with `int` ambiguous.

    :param value: Raw CLI token to parse as a boolean
    """
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    raise argparse.ArgumentTypeError(f"invalid bool value: {value!r}")


def choice_token(choice: Any) -> str:
    """Render one `Literal`/`Enum` choice value as the CLI token that represents it.

    An `Enum` member renders as its own `.name` (e.g. `"ACTIVE"`), a `bool` as `"true"`/`"false"`, and
    anything else via plain `str()`.

    :param choice: One value to render as a CLI token
    """
    if isinstance(choice, Enum):
        return choice.name
    if isinstance(choice, bool):
        return str(choice).lower()
    return str(choice)


def _format_choice_group(choices: Sequence[Any]) -> str:
    """Render a `Literal`/`Enum`'s choices as one brace-delimited group for the CLI value-type column, eliding
    past `_MAX_CHOICE_GROUP_WIDTH`. This is the action's own `metavar`, shown in the compact usage line and,
    under the condensed `-h` help, next to the flag itself; `--help` shows `_format_choice_group_full()`'s
    unelided form there instead.

    :param choices: `Literal[...]`'s allowed values, or an `Enum` class's members
    """
    return _elide_joined(
        [choice_token(c) for c in choices], sep=",", prefix="{", suffix="}", max_width=_MAX_CHOICE_GROUP_WIDTH
    )


def _format_choice_group_full(choices: Sequence[Any]) -> str:
    """Render a `Literal`/`Enum`'s choices as one brace-delimited group with every choice listed, no matter how
    wide, for display next to the flag under the full `--help`.

    :param choices: `Literal[...]`'s allowed values, or an `Enum` class's members
    """
    return "{" + ",".join(choice_token(c) for c in choices) + "}"


def _elide_joined(tokens: Sequence[str], *, sep: str, prefix: str = "", suffix: str = "", max_width: int) -> str:
    """Join `tokens` with `sep` (wrapped in `prefix`/`suffix`) for a CLI value-type column, eliding to a
    trailing `…` marker once the full join would exceed `max_width`.

    Renders every token when the full join fits. Otherwise keeps as many leading whole tokens as fit alongside
    the marker, rather than truncating mid-token or silently dropping the overflow count. Always keeps at
    least one token, even if that token alone already exceeds the width cap, in which case the marker is
    omitted since nothing was actually left out.

    :param tokens: Already-rendered tokens to join, in display order
    :param sep: Separator placed between tokens
    :param prefix: Text placed before the first token
    :param suffix: Text placed after the last token (before the marker, if any)
    :param max_width: Width cap, including `prefix`/`suffix`, before eliding
    """
    full = prefix + sep.join(tokens) + suffix
    if len(full) <= max_width:
        return full

    kept: list[str] = []
    for token in tokens:
        candidate = prefix + sep.join([*kept, token]) + f"{sep}{ELLIPSIS}{suffix}"
        if kept and len(candidate) > max_width:
            break
        kept.append(token)
    if len(kept) == len(tokens):
        return prefix + sep.join(kept) + suffix
    return prefix + sep.join(kept) + f"{sep}{ELLIPSIS}{suffix}"


def _choice_converter(choices: tuple[Any, ...], *, strict: bool = False) -> Callable[[str], Any]:
    """Build an `argparse` `type=` converter that matches a CLI token against a `Literal[...]`'s choice
    values.

    Non-strict (the default, for a standalone `Literal` flag) returns the token unchanged when nothing
    matches, so argparse's own `choices=` check produces the clean `invalid choice: ...` message. Strict (for
    a member of a scalar union) raises instead, so the union's own chained converter can fall through to try
    its next member.

    :param choices: `Literal[...]`'s allowed values
    :param strict: Raise instead of returning the token unchanged when nothing matches
    """
    token_map = {choice_token(c): c for c in choices}

    def convert(value: str) -> Any:
        if value in token_map:
            return token_map[value]
        if strict:
            raise ValueError(value)
        return value

    convert.__name__ = "|".join(token_map)
    return convert


def _chained_converter(specs: tuple[_ValueSpec, ...], names: tuple[str, ...]) -> Callable[[str], Any]:
    """Build an `argparse` `type=` converter for a union that tries each member's own value spec in turn,
    returning the first successful conversion.

    :param specs: Member value specs to try, already ordered most restrictive first
    :param names: The same members' own combined display names, already deduped by the caller
    """
    label = "|".join(names)

    def convert(value: str) -> Any:
        for spec in specs:
            try:
                return spec.converter(value)
            except (TypeError, ValueError, argparse.ArgumentTypeError):
                continue
        raise argparse.ArgumentTypeError(f"expected one of {label}: {value!r}")

    convert.__name__ = label
    return convert


def _scalar_or_list_action(scalar_spec: _ValueSpec, elem_spec: _ValueSpec) -> type[argparse.Action]:
    """Build an `argparse` `action=` class for a union of a scalar `S` with `list[X]` (`S` and `X` may be the
    same type, or different).

    Defers conversion until the number of given tokens is known: exactly one token converts with `S`'s own spec and is
    stored bare, two or more each convert with `X`'s and are stored as a `list`.

    :param scalar_spec: Value spec to apply when exactly one token is given
    :param elem_spec: Value spec to apply to each token when two or more are given
    """

    class _Action(argparse.Action):
        def __call__(
            self,
            parser: argparse.ArgumentParser,
            namespace: argparse.Namespace,
            values: str | Sequence[Any] | None,
            option_string: str | None = None,
        ) -> None:
            if not isinstance(values, list):
                # nargs="*" always calls back with a list, even for zero or one value.
                raise TypeError(f"expected a list of values for {option_string or self.dest!r}, got {values!r}")
            spec = scalar_spec if len(values) == 1 else elem_spec
            converted = []
            for value in values:
                try:
                    converted.append(spec.converter(value))
                except argparse.ArgumentTypeError as e:
                    raise argparse.ArgumentError(self, str(e)) from None
                except (TypeError, ValueError):
                    # Mirrors argparse's own ArgumentParser._get_value(), which formats a bare `type=` converter's
                    # TypeError/ValueError this same way, so a converter failure here reads identically to one from a
                    # plain `type=` callable instead of leaking e.g. int()'s raw "invalid literal for int() with base
                    # 10: ..." message.
                    name = getattr(spec.converter, "__name__", repr(spec.converter))
                    raise argparse.ArgumentError(self, f"invalid {name} value: {value!r}") from None
            setattr(namespace, self.dest, converted[0] if len(converted) == 1 else converted)

    return _Action


class _JsonListAction(argparse.Action):
    """Store a repeatable JSON-typed flag's values, treating a sole `-`/`@<path>` indirection as the whole
    parameter value rather than one element of it.

    Two or more given values are each parsed individually by `converter` and stored as a `list`, exactly
    like a plain `type=converter` flag would. A single indirection value is instead deserialized and stored
    as-is: the document it reads is already the whole value (e.g. a file holding a JSON array for a
    `list[X]` parameter), so wrapping it in another list would nest it incorrectly. An indirection given
    alongside any other value is rejected, since the two forms would otherwise disagree about whether that
    other value belongs inside the indirection's own document or next to it.

    An indirection's document must itself be a JSON array, since it stands for the whole collection.
    Anything else is rejected rather than wrapped into a single-element list: silently wrapping would make
    `-`/`@<path>` mean two different things depending on the document's own shape, the exact ambiguity the
    "cannot be combined with other values" rule above already exists to rule out. This check only looks at
    the document's own top-level shape, not its elements: e.g. a mistyped `"id": "1"` inside one of those
    elements is still forwarded unvalidated, same as an inline value's element would be.

    Used for a `list[X]` whose element type `X` has no more specific mapping (e.g. `list[dict[str, Any]]`),
    strictly JSON-parsing each element. `_LenientJsonListAction` overrides `converter` for an unparameterized
    collection (e.g. bare `list`), whose element type was never declared.
    """

    converter: Callable[[str], Any] = staticmethod(_parse_json)

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[Any] | None,
        option_string: str | None = None,
    ) -> None:
        if not isinstance(values, list):
            # nargs="*" always calls back with a list, even for zero or one value.
            raise TypeError(f"expected a list of values for {option_string or self.dest!r}, got {values!r}")
        indirections = [v for v in values if _is_json_indirection(v)]
        if indirections and len(values) > 1:
            raise argparse.ArgumentError(
                self, f"{indirections[0]!r} provides the entire option value and cannot be combined with other values"
            )
        parsed = []
        for value in values:
            try:
                parsed.append(self.converter(value))
            except argparse.ArgumentTypeError as e:
                raise argparse.ArgumentError(self, str(e)) from None
            except (TypeError, ValueError):
                # Mirrors argparse's own ArgumentParser._get_value(), which formats a bare `type=` converter's
                # TypeError/ValueError this same way, so a converter failure here reads identically to one from
                # a plain `type=` callable.
                raise argparse.ArgumentError(self, f"invalid {_TypeName.JSON} value: {value!r}") from None
        if indirections and not isinstance(parsed[0], list):
            raise argparse.ArgumentError(
                self,
                f"JSON input from {indirections[0]!r} must be an array, not {_JSON_TYPE_NAMES[type(parsed[0])]}",
            )
        setattr(namespace, self.dest, parsed[0] if indirections else parsed)


class _LenientJsonListAction(_JsonListAction):
    """`_JsonListAction` for an unparameterized collection (e.g. bare `list`), whose element type was never
    declared: each token is JSON-parsed with a plain-string fallback (`_parse_json_or_str`) rather than
    strictly rejected as malformed JSON, the same leniency an unannotated scalar parameter already gets.
    """

    converter: Callable[[str], Any] = staticmethod(_parse_json_or_str)


def accepts_file_path(action: argparse.Action) -> bool:
    """Whether a parser action's value(s) may be a filesystem path, for shell path completion.

    True for a plain `File` flag, and for a union or scalar-or-list flag whenever any of their member specs
    is one, so e.g. `File | str` and `File | list[File]` still get real path completion.

    :param action: A parser action to check
    """
    return bool(getattr(action, _ACCEPTS_FILE_PATH_ATTR, False))


def accepts_json_file(action: argparse.Action) -> bool:
    """Whether a parser action's value is JSON-typed, so it may alternatively be given as `@<path>` (or `-`
    for stdin), for shell path completion once `@` itself is typed. For a repeatable flag, the indirection
    supplies the flag's whole value rather than one element of it.

    :param action: A parser action to check
    """
    return bool(getattr(action, _ACCEPTS_JSON_FILE_ATTR, False))


def mark_accepts_file_indirection(action: argparse.Action) -> None:
    """Mark a parser action as accepting the same `-`/`@<path>` whole-value indirection a JSON-typed flag
    does, so `accepts_json_file()` reports it for shell path completion once `@` itself is typed.

    For a flag registered outside `add_endpoint_arguments()` (e.g. `-H`/`--header`) whose value isn't itself
    JSON, but that still reads its whole value from stdin or a file the same way, via `read_stdin_text()`/
    `read_file_text()`. `add_endpoint_arguments()`'s own JSON-typed flags are marked this way as a byproduct
    of resolving their `_ArgSpec`, so they need no separate call to this.

    :param action: The action whose value accepts the indirection
    """
    setattr(action, _ACCEPTS_JSON_FILE_ATTR, True)


def split_param_docs(doc: str | None) -> tuple[str, dict[str, str]]:
    """Split an endpoint function's own docstring into its prose (everything but `:param` entries) and a
    `dict` of `:param <name>: <description>` entries keyed by parameter name.

    A description that continues onto the following non-blank line(s) not themselves starting a new
    `:field:` - this project's own convention for one too long to fit a single line - is joined back into
    one line. Runs of source lines that belong to no `:param` entry accumulate into the prose, in the order
    they appear, with consecutive blank lines collapsed to one so a `:param` block's own removal never
    leaves a stray gap behind. `cleandoc()` normalizes indentation first, so this works the same regardless
    of how deep the enclosing function body sits and matches Python's own C-level docstring cleanup (3.13+)
    on every supported version.

    :param doc: The endpoint function's own docstring, if any
    """
    if not doc or not doc.strip():
        return "", {}
    params: dict[str, list[str]] = {}
    prose: list[str] = []
    current: str | None = None
    for line in inspect.cleandoc(doc).splitlines():
        match = _PARAM_DOC_RE.match(line.strip())
        if match:
            name, text = match.groups()
            current = name
            params[current] = [text] if text else []
            continue
        if current is not None:
            stripped = line.strip()
            if stripped and not stripped.startswith(":"):
                params[current].append(stripped)
                continue
            current = None
            if not stripped:
                continue
        if line.strip() or (prose and prose[-1].strip()):
            prose.append(line)
    while prose and not prose[-1].strip():
        prose.pop()
    return "\n".join(prose), {name: " ".join(parts) for name, parts in params.items()}


def _param_location(endpoint: Endpoint[Any], field: Field[Any]) -> str:
    """Return the request location marker (`path`/`query`/`body`) for one endpoint parameter field.

    Kept separate so a bare `[location]` marker can still be shown if the rest of that parameter's help text
    fails to render, without losing the parameter's flag entirely.

    :param endpoint: Endpoint object
    :param field: Dataclass field describing the parameter
    """
    if field.metadata.get("path") is True:
        return "path"
    if (
        endpoint.method == "get"
        or endpoint.model.endpoint_func._use_query_string
        or param_type_util.is_query_param(field.type)
    ):
        return "query"
    return "body"


def _help_text(
    endpoint: Endpoint[Any],
    field: Field[Any],
    *,
    required: bool,
    spec: _ArgSpec,
    type_width: int,
    renamed_from: str | None = None,
    description: str | None = None,
) -> str:
    """Compose CLI help text for a single endpoint parameter field.

    :param endpoint: Endpoint object
    :param field: Dataclass field describing the parameter
    :param required: Whether the parameter is required
    :param spec: The field's own resolved `_ArgSpec`, for its displayed value type (`display_type`) and
                 whether it's JSON-parsed
    :param type_width: Width of the value-type column, shared by every parameter of the same endpoint so their
                       marker columns all align. `0` omits the column
    :param renamed_from: The parameter's own naturally-derived flag, if it was renamed to a trailing-underscore
                         alias because that flag collided with a reserved one or another parameter's own,
                         or `None` if it wasn't renamed
    :param description: The parameter's own `:param <name>: ...` description parsed from the endpoint
                        function's docstring, indented on its own line beneath the marker row: shown in
                        full under `--help`, clamped to that one line under `-h`, or `None` if the
                        docstring documents no such parameter
    """
    location = _param_location(endpoint, field)
    columns = _help_column(f"[{location}]", _LOCATION_COLUMN_WIDTH)
    if type_width:
        columns += _help_column(spec.display_type or "", type_width + 1, color_code=ColorCodes.DARK_GREY)
    markers = []
    if required:
        markers.append(color_output("*required", color_code=ColorCodes.RED))
    if not required and field.default not in (Unset, MISSING):
        markers.append(f"(default: {_format_default(field.default, is_json=spec.is_json)})")
    if param_type_util.is_deprecated_param(field.type):
        markers.append(color_output("(deprecated)", color_code=ColorCodes.YELLOW))
    if renamed_from is not None:
        markers.append(color_output(f"(renamed from {renamed_from}: CLI-reserved)", color_code=ColorCodes.YELLOW))
    text = (columns + " ".join(markers)).rstrip()
    return f"{text}\n{_DESCRIPTION_INDENT}{description}" if description else text


def _format_default(default: Any, *, is_json: bool) -> str:
    """Render a parameter's default value the way it would need to be typed on the CLI to reproduce it.

    An `Enum` member is rendered as its own `.name` (e.g. `ACTIVE`), matching what an `Enum`-typed flag actually
    accepts, rather than Python's own `(default: <Status.ACTIVE: 'active'>)` `repr()` spelling. A JSON-parsed flag
    needs its default spelled in JSON syntax (`null`, `true`/`false`) rather than Python's `None`/`True`/`False`
    spelling, which would be rejected as invalid JSON. For a repeatable JSON-typed flag (`_JsonListAction`), a
    `None` default is still shown as `null` for the same reason, even though omitting the flag already reaches
    that same default and a literal `null` document is rejected as not being an array.

    :param default: The field's default value
    :param is_json: Whether the field's flag is JSON-parsed
    """
    if isinstance(default, Enum):
        return default.name
    if is_json:
        try:
            return json.dumps(default)
        except (TypeError, ValueError):
            pass
    return repr(default)


def _help_column(text: str, width: int, *, color_code: str | None = None) -> str:
    """Render one fixed-width column of a parameter's help text.

    Padding is appended outside any ANSI codes, so it stays measurable and a trailing column with no markers after
    it can be stripped.

    :param text: Column text, before coloring
    :param width: Column width, including at least one trailing space
    :param color_code: ANSI color to apply to the text itself
    """
    rendered = color_output(text, color_code=color_code) if text and color_code else text
    return rendered + " " * max(width - len(text), 1)

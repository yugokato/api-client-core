from __future__ import annotations

import argparse
import importlib.util
import json
import re
from collections.abc import Sequence
from inspect import cleandoc
from typing import Any

from common_libs.ansi_colors import ColorCodes
from common_libs.logging import get_logger

from api_client_core import __version__
from api_client_core.base import APIClient
from api_client_core.endpoints import Endpoint

from ._cache import is_completion_registered
from ._completion_schema import AppSpec, CompletionTree, OptSpec, ResourceSpec
from ._constants import HELP_FLAGS, LOG_LEVELS, OUTPUT_CHOICES, PROG, Flag, Output
from ._paths import project_roots
from .discovery import (
    DiscoveryResult,
    discover_clients,
    discover_clients_with_failures,
    discover_resources,
    endpoints_for,
    format_discovery_failures,
)
from .params import (
    accepts_file_path,
    accepts_json_file,
    add_endpoint_arguments,
    choice_token,
    mark_accepts_file_indirection,
    read_file_text,
    read_stdin_text,
    split_param_docs,
)
from .parser import ArgumentParser, HelpAction, default_metavar
from .utils import box_text, color_output, get_terminal_width, indent_text
from .wrappers import add_wrapper_arguments

logger = get_logger(__name__)

_TAB_COMPLETION_TIP = "Install the 'cli-completion' extra to enable tab completion (see README)"
_TAB_COMPLETION_REGISTER_TIP = (
    f"To activate tab completion, add 'eval \"$(register-python-argcomplete --no-defaults {PROG})\"' "
    "to your shell startup file (see README)"
)
_OPTIONS_GROUP_TITLE = "options"
_HEADER_STDIN_TOKEN = "-"  # value reading -H's whole value (one or more NAME:VALUE lines) from stdin
_HEADER_FILE_PREFIX = "@"  # value prefix reading -H's whole value from the named file


def build_initial_parser() -> argparse.ArgumentParser:
    """Build the parser used to select an API client.

    This parser only exposes the available application names. A module that failed to import, or a candidate
    class that declares no `app_name`, during discovery is named as a warning, alongside the tab-completion
    tip, instead of silently leaving the app list looking complete (or empty) with no explanation.
    """
    result = discover_clients_with_failures()
    parser = ArgumentParser(prog=PROG, tips=_tab_completion_tips(), warnings=_initial_parser_warnings(result))
    # `--version` is declared for `--help` output only. dispatch() handles it directly.
    parser.add_argument(Flag.VERSION, action="version", version=f"{PROG} {__version__}")
    subparsers = parser.add_subparsers(metavar="<app-name>", help="app_name set in your API client")
    for app_name, client_class in sorted(result.clients.items(), key=lambda item: _natural_sort_key(item[0])):
        help_text = _first_doc_line(client_class.__doc__) or f"{client_class.__name__} commands"
        subparsers.add_parser(app_name, help=help_text)
    return parser


def build_client_parser(client_class: type[APIClient], *, prog: str | None = None) -> argparse.ArgumentParser:
    """Build a CLI parser for the given API client class.

    Every discovered API class becomes a kebab-case resource, every `Endpoint` on it a kebab-case subcommand,
    and every endpoint parameter a typed flag. Help text is sourced directly from each endpoint's docstring.

    A command whose arguments fail to build is skipped with a warning and removed from its resource's
    subparser, rather than the failure taking down the whole client's CLI.

    :param client_class: Concrete `APIClient` subclass to introspect
    :param prog: Program name shown in generated help. Defaults to argparse's usual inference
    """
    tips = _tab_completion_tips()
    parser = ArgumentParser(prog=prog, description=_generate_description(client_class), tips=tips)
    _add_common_arguments(parser, default=None)
    parser.set_defaults(_parser=parser)

    resources = discover_resources(client_class)
    if not resources:
        raise RuntimeError(
            f"No API classes discovered on {client_class.__name__}. A resource must be exposed as a "
            f"@cached_property/@property whose return type annotation is a BaseAPI subclass. If a resource module "
            f"failed to import instead, re-run with --log-level DEBUG to see why."
        )

    # Not required: a resource/command given with nothing after it still parses successfully, so run()
    # can show that level's own help (naming its choices) instead of argparse's bare "arguments are
    # required" error, which names what's missing but not what's available.
    resource_subparsers = parser.add_subparsers(
        prog=parser.prog, dest="_resource", required=False, metavar="<resource-group>"
    )
    for attr_name, api_class in sorted(resources.items(), key=lambda item: _natural_sort_key(_to_kebab_case(item[0]))):
        endpoints = sorted(endpoints_for(api_class), key=lambda e: _natural_sort_key(_to_kebab_case(e.func_name)))
        if not endpoints:
            logger.debug(f"Skipping resource {attr_name!r} ({api_class.__qualname__}): it exposes no endpoints")
            continue
        resource_name = _to_kebab_case(attr_name)
        if resource_name in resource_subparsers.choices:
            logger.warning(
                f"Multiple API classes resolve to resource name {resource_name!r}: ignoring {api_class.__qualname__}"
            )
            continue
        resource_parser = resource_subparsers.add_parser(
            resource_name,
            help=_first_doc_line(api_class.__doc__) or f"{api_class.__name__} commands",
            description=_generate_description(api_class),
            tips=tips,
        )
        _add_common_arguments(resource_parser, default=argparse.SUPPRESS)
        resource_parser.set_defaults(_parser=resource_parser)
        command_subparsers = resource_parser.add_subparsers(
            prog=resource_parser.prog, dest="_command", required=False, metavar="<command>"
        )
        for endpoint in endpoints:
            command_name = _to_kebab_case(endpoint.func_name)
            if command_name in command_subparsers.choices:
                logger.warning(
                    f"Multiple endpoints on {api_class.__qualname__} resolve to command name {command_name!r}: "
                    f"ignoring {endpoint.func_name!r} ({endpoint})"
                )
                continue
            try:
                prose, _ = split_param_docs(endpoint.original_func.__doc__)
                command_parser = command_subparsers.add_parser(
                    command_name,
                    help=_command_help(endpoint, prose),
                    description=_generate_description(endpoint),
                    add_help=False,
                    tips=tips,
                )
                command_parser.set_defaults(_endpoint=endpoint, _parser=command_parser)
                add_endpoint_arguments(command_parser, endpoint)
                # Snapshot the endpoint's own parameter actions before the wrapper/call-control flags are added, so the
                # compact usage= built below covers only the former.
                param_actions = list(command_parser._actions)
                add_wrapper_arguments(command_parser)
                _add_call_ctrl_arguments(command_parser)
                command_parser.usage = _compact_usage(command_parser.prog, param_actions)
            except Exception as e:
                logger.warning(
                    f"Skipping command {command_name!r} on resource {resource_name!r}: {type(e).__name__}: {e}"
                )
                _discard_subparser(command_subparsers, command_name)
        if not command_subparsers.choices:
            logger.debug(f"Discarding resource {resource_name!r}: every command failed to build")
            _discard_subparser(resource_subparsers, resource_name)

    if not resource_subparsers.choices:
        raise RuntimeError(
            f"No usable commands discovered on {client_class.__name__}: every API class exposed no endpoints, or "
            f"every endpoint's arguments failed to build. Re-run with --log-level DEBUG to see why."
        )
    return parser


def build_completion_tree() -> CompletionTree:
    """Build a JSON-serializable completion tree for every discovered API client.

    Mirrors the full command structure (app name -> resource -> command -> flags) into plain data, so a
    shell-completion request can rebuild an equivalent parser from cached JSON without importing this module
    at all.

    A client whose parser can't be built is skipped rather than raising, so one broken or ambiguous client
    doesn't break completion for the rest.
    """
    tree: CompletionTree = {}
    for app_name, client_class in discover_clients().items():
        try:
            tree[app_name] = build_completion_entry(client_class)
        except Exception as e:
            logger.debug(f"Skipping {app_name!r} ({client_class.__qualname__}) from completion: {e}")
            continue
    return tree


def build_completion_entry(client_class: type[APIClient]) -> AppSpec:
    """Build one client's own completion subtree entry.

    :param client_class: Concrete `APIClient` subclass to build the entry for
    """
    return _serialize_client_parser(build_client_parser(client_class))


def _tab_completion_tips() -> list[str]:
    """Return a tab-completion setup tip covering whichever step is still outstanding, or an empty list
    once nothing is left to set up.

    `argcomplete` not being importable at all is the more urgent gap and takes priority. Once it's
    installed, a real shell-completion request must still have been served at least once (recorded by
    `_entrypoint._complete()`'s own marker, via `is_completion_registered()`) before the remaining
    `eval "$(register-python-argcomplete ...)"` step can be assumed done - otherwise a user who installs the
    extra but hasn't added that line yet presses TAB, gets nothing, and has no on-screen hint at all.
    """
    if importlib.util.find_spec("argcomplete") is None:
        return [_TAB_COMPLETION_TIP]
    if not is_completion_registered():
        return [_TAB_COMPLETION_REGISTER_TIP]
    return []


def _initial_parser_warnings(result: DiscoveryResult) -> list[str]:
    """Compose the initial parser's warnings: the scanned directory if discovery found nothing at all, and a
    summary of any import failures or unnamed clients, in that order.

    The directory-blaming warning is suppressed whenever an unnamed client was recorded: unlike an import
    failure (which may just as well be an unrelated broken module in the wrong directory), an unnamed client
    is only ever recorded for an actual `APIClient` subclass that was found and imported, so blaming the
    directory would contradict the more specific, more actionable failure already named below it.

    :param result: The completed discovery scan to summarize
    """
    warnings = []
    if not result.clients and not result.unnamed_clients:
        root = project_roots()[0]
        warnings.append(
            f"No API clients were discovered under {root}. Run this command from inside a project that defines "
            f"your API classes."
        )
    if result.import_failures:
        warnings.append(
            f"{len(result.import_failures)} module(s) failed to import and were skipped during discovery:\n"
            f"{format_discovery_failures(result.import_failures)}"
        )
    if result.unnamed_clients:
        warnings.append(
            f"{len(result.unnamed_clients)} candidate class(es) declare no 'app_name' and were skipped during "
            f"discovery:\n{format_discovery_failures(result.unnamed_clients)}"
        )
    return warnings


def _add_base_url_argument(target: argparse._ActionsContainer, *, default: Any) -> None:
    """Add the `--base-url` flag shared by the top-level, resource, and leaf-command parsers.

    :param target: Top-level parser, resource parser, or leaf-command argument group to add the flag to
    :param default: Default applied to the flag when it isn't given at this level
    """
    target.add_argument(Flag.BASE_URL, default=default, help="Override the client's base URL")


def _add_log_level_argument(target: argparse._ActionsContainer, *, default: Any) -> None:
    """Add the `--log-level` flag shared by the top-level, resource, and leaf-command parsers.

    :param target: Top-level parser, resource parser, or leaf-command argument group to add the flag to
    :param default: Default applied to the flag when it isn't given at this level
    """
    target.add_argument(
        Flag.LOG_LEVEL,
        default=default,
        type=str.upper,
        choices=LOG_LEVELS,
        metavar="LEVEL",
        help="Set the log level (default: INFO).",
    )


def _add_common_arguments(target: argparse._ActionsContainer, *, default: Any) -> None:
    """Add `--base-url` and `--log-level` to a single container, for the top-level and resource parsers.

    Registered at both levels so either flag can be given anywhere on the command line. The leaf command
    parser adds them separately instead, alongside the rest of its own `options` group.

    `default` must be `None` for the top-level parser and `argparse.SUPPRESS` for a resource or leaf parser:
    argparse re-applies a subparser's own defaults onto the shared namespace, so a plain `None` at an inner
    level would silently overwrite a value already given further left. `SUPPRESS` leaves an unset inner flag
    untouched instead, letting an outer value stand while an inner value, when given, still wins.

    :param target: Top-level parser or resource parser to add the flags to
    :param default: Default applied to both flags when neither is given at this level
    """
    _add_base_url_argument(target, default=default)
    _add_log_level_argument(target, default=default)


def _discard_subparser(subparsers_action: argparse._SubParsersAction[Any], name: str) -> None:
    """Undo an `add_parser()` registration for a leaf command whose arguments failed to build.

    `add_parser()` registers the new subparser into `choices` and its help pseudo-action before the caller
    gets a chance to add its own flags, so a failure partway through populating those flags leaves a
    half-built, uncallable command behind unless both are undone here.

    :param subparsers_action: The `_SubParsersAction` `add_parser()` was called on
    :param name: Command name to remove
    """
    subparsers_action._name_parser_map.pop(name, None)
    subparsers_action._choices_actions[:] = [a for a in subparsers_action._choices_actions if a.dest != name]


def _add_call_ctrl_arguments(parser: ArgumentParser) -> None:
    """Add per-call control flags to a leaf (endpoint) subparser, in a single `options` group.

    The leaf subparser is built with `add_help=False` so the endpoint-parameters group renders first in
    help output. This function adds `-h`/`--help` back manually to restore it.

    :param parser: Leaf subparser for a single endpoint command
    """
    options_group = parser.add_argument_group(title=_OPTIONS_GROUP_TITLE)
    options_group.add_argument(
        *HELP_FLAGS,
        action=HelpAction,
        default=argparse.SUPPRESS,
        help="Show this help message and exit (-h: summary, --help: full details)",
    )
    _add_base_url_argument(options_group, default=argparse.SUPPRESS)
    _add_log_level_argument(options_group, default=argparse.SUPPRESS)
    options_group.add_argument(
        "-o",
        Flag.OUTPUT,
        default=Output.NONE,
        choices=OUTPUT_CHOICES,
        help="Control what is written to stdout (default: none).\nnone: nothing. json: the decoded response body. "
        "raw: the undecoded response body, as text, exactly as the server sent it. full: {status_code, "
        'headers, body} for each call. Selecting any value other than "none" automatically enables the '
        "-q/--quiet option.",
    )
    options_group.add_argument(
        "-q",
        Flag.QUIET,
        action="store_true",
        default=None,
        help="Suppress request/response logs.\nA failed response is reduced to a single error line on stderr.",
    )
    options_group.add_argument(Flag.NO_HOOKS, action="store_true", help="Skip pre/post request hooks")
    options_group.add_argument(
        Flag.RAW_OPTION,
        action="append",
        default=[],
        type=_parse_raw_option,
        metavar="KEY=VALUE",
        help="Raw httpx2 request option (repeatable).\nValue is parsed as JSON, falling back to a plain string.",
    )
    header_action = options_group.add_argument(
        "-H",
        Flag.HEADER,
        action=_HeaderAction,
        default=[],
        type=_parse_header,
        metavar="NAME:VALUE",
        help='Additional request header (repeatable).\ne.g. -H "Authorization: Bearer $TOKEN"\n'
        "-H @<path> or -H - (stdin) instead reads one or more NAME:VALUE lines, keeping a sensitive value "
        "out of shell history.",
    )
    mark_accepts_file_indirection(header_action)


def _parse_raw_option(item: str) -> tuple[str, Any]:
    """Parse one `--raw-option KEY=VALUE` flag into a `(key, value)` pair.

    Used as the argument's `type=` callable so a malformed value is rejected by `argparse` itself during parsing (a
    clean `error: ...` message and exit code 2), rather than raising once parsing has already completed.

    :param item: Raw `KEY=VALUE` string from one `--raw-option` occurrence
    """
    key, sep, value = item.partition("=")
    if not sep:
        raise argparse.ArgumentTypeError(f"--raw-option must be KEY=VALUE, got: {item!r}")
    try:
        return key, json.loads(value)
    except json.JSONDecodeError:
        return key, value


def _parse_header(item: str) -> list[tuple[str, str]]:
    """Parse one `-H`/`--header` occurrence into one or more `(name, value)` pairs.

    A plain `NAME:VALUE` token parses to a single pair, as `-H` always has. A value of `-` or `@<path>`
    instead reads its whole value from stdin or the named file, via `read_stdin_text()`/`read_file_text()`
    (the same indirection a JSON-typed endpoint parameter accepts), one header per non-blank line, each in
    `NAME:VALUE` form - so a header carrying sensitive data (e.g. a bearer token) never has to be typed into
    argv, where it would otherwise be visible in shell history and to other processes on the same machine.

    Used as the argument's `type=` callable so a malformed value, an unreadable file, or a second `-` in the
    same command is rejected by `argparse` itself during parsing, rather than raising once parsing has
    already completed. `_HeaderAction` is what then flattens every occurrence's own one-or-more pairs into a
    single accumulated list, since a plain `action="append"` would nest each occurrence's list instead.

    :param item: Raw `-H`/`--header` value: a `NAME:VALUE` token, `-` (stdin), or `@<path>` (file)
    """
    if item == _HEADER_STDIN_TOKEN:
        text = read_stdin_text()
    elif item.startswith(_HEADER_FILE_PREFIX):
        text = read_file_text(item[1:])
    else:
        return [_parse_header_line(item)]

    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise argparse.ArgumentTypeError(f"{item!r} produced no headers")
    return [_parse_header_line(line) for line in lines]


def _parse_header_line(item: str) -> tuple[str, str]:
    """Parse one `NAME:VALUE` line into a `(name, value)` pair.

    Splits on the first `:` only, so a value that itself contains a colon (e.g. `Authorization: Bearer a:b`)
    survives intact. Both sides are stripped of surrounding whitespace, matching curl's own `-H "Name: Value"`
    spelling. An empty value is allowed (some APIs use a header's mere presence as a signal), an empty name is
    not.

    :param item: One `NAME:VALUE` line, from a plain `-H` token or one line of a `-`/`@<path>` indirection
    """
    name, sep, value = item.partition(":")
    name = name.strip()
    if not sep or not name:
        raise argparse.ArgumentTypeError(f"-H/--header must be NAME:VALUE, got: {item!r}")
    return name, value.strip()


class _HeaderAction(argparse.Action):
    """Accumulate `-H`/`--header` occurrences, each already parsed by `_parse_header()` into a list of one or
    more `(name, value)` pairs (more than one only for a `-`/`@<path>` indirection). Flattens each
    occurrence's own pairs into the accumulated list, rather than argparse's own built-in `action="append"`,
    which would append each occurrence's list as one nested element instead.
    """

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        # Rebinds `self.dest` to a fresh list rather than mutating the existing one in place: that list is
        # `action.default` itself, the same object every parse of this parser starts from, so an in-place
        # `.extend()` would leak one parse's own headers into the very next one. Mirrors argparse's own
        # `_AppendAction`, which copies for the identical reason via its private `_copy_items()`.
        setattr(namespace, self.dest, [*getattr(namespace, self.dest), *values])


def _natural_sort_key(name: str) -> tuple[str | int, ...]:
    """Build a sort key that orders embedded digit runs numerically instead of lexicographically.

    :param name: String to build a natural sort key for
    """
    return tuple(int(chunk) if chunk.isdigit() else chunk for chunk in re.split(r"(\d+)", name))


def _to_kebab_case(name: str) -> str:
    """Convert a Python identifier (attribute or function name) into a lowercase, kebab-case CLI token.

    Leading underscores are left untouched, an internal underscore run becomes a single `-` (`get_user` ->
    `get-user`), and a CamelCase boundary is split the same way (`UserProfiles` -> `user-profiles`, `APIKeys`
    -> `api-keys`), so a client written with capitalized or camelCase names still gets an idiomatic CLI token.

    Two names that normalize to the same token (e.g. `Users`/`users`) collide. The caller is responsible for
    detecting and reporting that.

    :param name: Python identifier to convert
    """
    prefix_len = len(name) - len(name.lstrip("_"))
    prefix, body = name[:prefix_len], name[prefix_len:]
    body = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", body)
    body = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "-", body)
    return (prefix + body.replace("_", "-")).lower()


def _compact_usage(prog: str, param_actions: Sequence[argparse.Action]) -> str:
    """Build a leaf command's own `usage=` string, covering only its endpoint parameters plus a trailing
    `[options]` placeholder for the execution-wrapper and call-control flags added afterward.

    Without this, every leaf command's wrapper and call-control flags would dominate its usage line ahead of
    the endpoint's own parameters. Built directly from the registered actions, in registration order, rather
    than a hand-written string, so it can't drift from the real flags.

    An explicitly given `usage=` string is used completely as-is by argparse, with no line wrapping, so this
    wraps it manually instead. `prog` is embedded literally rather than as a placeholder, since the wrapping
    needs its real length up front.

    :param prog: The leaf command parser's own resolved `prog` (e.g. `api-client my-app users get-user`)
    :param param_actions: Endpoint-parameter actions, snapshotted before any wrapper/control flag is added
    """
    tokens = [prog, *(_format_action_usage(a) for a in param_actions), "[OPTIONS]"]
    text_width = max(get_terminal_width() - 2, 20)
    return _wrap_usage_tokens(tokens, text_width).replace("%", "%%")


def _wrap_usage_tokens(tokens: Sequence[str], width: int, prefix: str = "usage: ") -> str:
    """Greedily wrap already-atomic usage tokens (each one a whole `--flag VALUE`/`[--flag VALUE]` group, never
    split mid-token, `prog` first) into lines of at most `width` columns.

    Every line after the first is indented to align under `prog`, unless `prog` itself is long enough
    relative to `width` that the aligned indent wouldn't leave room for the longest remaining token, in which
    case `prog` gets its own line and the rest wrap at a minimal indent.

    :param tokens: Atomic tokens to wrap, `prog` first
    :param width: Maximum line width, including `prefix`/indent
    :param prefix: The `"usage: "` prefix that will be prepended outside this function, used only to size the
                  first line's own budget
    """
    prog, rest_tokens = tokens[0], tokens[1:]
    longest = max((len(t) for t in rest_tokens), default=0)
    prog_gets_own_line = len(prefix) + len(prog) + 1 + longest > width
    indent = " " * (len(prefix) if prog_gets_own_line else len(prefix) + len(prog) + 1)
    lines: list[str] = [prog] if prog_gets_own_line else []
    line: list[str] = [] if prog_gets_own_line else [prog]
    line_len = (len(indent) - 1) if prog_gets_own_line else (len(prefix) + len(prog))
    for token in rest_tokens:
        if line and line_len + 1 + len(token) > width:
            lines.append(" ".join(line))
            line = []
            line_len = len(indent) - 1
        line.append(token)
        line_len += len(token) + 1
    if line:
        lines.append(" ".join(line))
    first, *rest = lines
    return "\n".join([first, *(indent + r for r in rest)])


def _format_action_usage(action: argparse.Action) -> str:
    """Render one endpoint-parameter action's own usage fragment (e.g. `--flag VALUE`, or `[--flag VALUE]` when
    optional).

    Mirrors the small subset of argparse's own usage formatting actually needed here (a value-less flag, a
    single value, or a repeatable one), rather than reusing argparse's own private formatting internals,
    which aren't stable across supported Python versions.

    A value-less flag registering more than one option string (a `bool` parameter's paired `--flag`/
    `--no-flag`) renders both, joined by `" | "`; if that pair is also required, it's wrapped in parens
    instead of brackets, a deliberate divergence from argparse's own bare rendering of a required group,
    which reads ambiguously mid-line. Every other required action (a single option string, with or without a
    value) instead renders bare, with no wrapping at all, matching argparse's own convention that only an
    optional flag gets brackets.

    :param action: A single action to render
    """
    flag = action.option_strings[0]
    if action.nargs == 0:
        part = " | ".join(action.option_strings)
        if action.required and len(action.option_strings) > 1:
            return f"({part})"
    else:
        metavar = action.metavar or (default_metavar(action.dest) if action.dest else flag)
        if action.nargs == "*":
            part = f"{flag} [{metavar} ...]"
        elif action.nargs == "+":
            part = f"{flag} {metavar} [{metavar} ...]"
        else:
            part = f"{flag} {metavar}"
    return part if action.required else f"[{part}]"


def _command_help(endpoint: Endpoint[Any], doc: str | None) -> str:
    """Compose the one-line help summary shown for an endpoint's command in its resource's `--help`.

    :param endpoint: Endpoint the command was generated for
    :param doc: The endpoint function's own docstring prose (its `:param` entries already split out by
                `split_param_docs()`), if any
    """
    summary = _first_doc_line(doc) or str(endpoint)
    if endpoint.is_deprecated:
        summary += color_output(" (deprecated)", color_code=ColorCodes.YELLOW)
    return summary


def _first_doc_line(doc: str | None) -> str | None:
    """Return the first non-blank line of a docstring, or `None` if it has none.

    :param doc: Docstring to summarize
    """
    return doc.strip().splitlines()[0] if doc and doc.strip() else None


def _serialize_client_parser(parser: argparse.ArgumentParser) -> AppSpec:
    """Serialize one client's built parser into its completion subtree.

    :param parser: Parser built for one client
    """
    resource_subparsers = _get_subparsers_action(parser)
    resources = {
        resource_name: ResourceSpec(
            opts=_serialize_options(resource_parser),
            commands={
                command_name: _serialize_options(command_parser)
                for command_name, command_parser in _get_subparsers_action(resource_parser).choices.items()
            },
        )
        for resource_name, resource_parser in resource_subparsers.choices.items()
    }
    return AppSpec(opts=_serialize_options(parser), resources=resources)


def _get_subparsers_action(parser: argparse.ArgumentParser) -> argparse._SubParsersAction[argparse.ArgumentParser]:
    """Return the single `_SubParsersAction` added to a parser.

    :param parser: App, resource, or command parser to inspect
    """
    return next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))


def _serialize_options(parser: argparse.ArgumentParser) -> list[OptSpec]:
    """Serialize a parser's own optional flags into `OptSpec` dicts.

    Excludes any subparsers action and the auto-added `-h`/`--help`, since argparse re-adds `--help` on
    rebuild. `"nargs"` mirrors the real action's own arity, so the rebuilt parser accepts the same number of
    values in the same occurrence rather than always assuming exactly one (e.g. a `nargs="?"` flag doesn't
    consume the next token as its value, and a `nargs="+"` flag keeps accepting further ones instead of
    treating the second as unrelated). `"is_file"` marks a flag whose value(s) may be a filesystem path, so
    the rebuilt parser can offer real path completion only there instead of for every value flag.
    `"is_json_file"` marks a JSON-typed flag the same way, for its `@<path>` form.

    Each choice is converted to a JSON-safe form, since `choices` can legally hold a value that isn't
    JSON-serializable (e.g. an `Enum` member or `bytes`), which would otherwise make the whole completion tree
    fail to cache. `nargs` needs no such conversion: argparse itself only ever sets it to `None`, an `int`, or
    one of its own `"?"`/`"*"`/`"+"` string markers, all natively JSON-safe.

    :param parser: App-level or leaf command parser whose direct flags to serialize
    """
    specs: list[OptSpec] = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction) or not action.option_strings:
            continue
        if set(action.option_strings) & set(HELP_FLAGS):
            continue
        specs.append(
            OptSpec(
                opts=list(action.option_strings),
                choices=[_json_safe_choice(c) for c in action.choices] if action.choices else None,
                nargs=action.nargs,
                is_file=accepts_file_path(action),
                is_json_file=accepts_json_file(action),
            )
        )
    return specs


def _json_safe_choice(choice: Any) -> Any:
    """Return a JSON-serializable form of one `action.choices` value.

    A JSON-native value (`str`, `int`, or `float`) is returned unchanged. Anything else (e.g. an `Enum`
    member, `bytes`, or a `bool`) is rendered as its real CLI token instead, so completion never offers a
    value the real flag would then reject.

    :param choice: One value from an `argparse` action's `choices`
    """
    if choice is None or (isinstance(choice, str | int | float) and not isinstance(choice, bool)):
        return choice
    return choice_token(choice)


def _generate_description(obj: Endpoint[Any] | type[Any]) -> str:
    """Build a parser's own boxed `description=` text: a title line (an endpoint's own `str()`, or a
    class's name) followed by its docstring's prose, if any, indented underneath.

    An endpoint's own `:param <name>: ...` entries are left out of the box entirely, since each one is
    already shown as that parameter's own flag `help=` (see `add_endpoint_arguments()`), and showing both
    would just duplicate the same text twice under `--help`.

    :param obj: The `Endpoint` or class (a client or API class) to describe
    """
    if isinstance(obj, Endpoint):
        desc = str(obj)
        doc, _ = split_param_docs(obj.original_func.__doc__)
    else:
        desc = f"{obj.__name__}"
        doc = cleandoc(obj.__doc__) if obj.__doc__ else ""
    if doc:
        desc += f":\n{indent_text(doc)}"
    return box_text(desc)

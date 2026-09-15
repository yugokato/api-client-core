"""Console entry point for the `api-client-mcp` command.

At module scope this imports only the stdlib, so `--help`/`--version` and the "you don't have the `mcp`
extra installed" error work without ever importing the `mcp` SDK, `httpx2`, or the user's project.
Everything else is imported function-locally, past that fast path.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import signal
import sys
from typing import TYPE_CHECKING

from api_client_core.core.constants import VALID_METHODS

from .._common.console import LOG_LEVELS, STDERR_LOGGING_DELTA_CONFIG, reserve_stdout, write_error
from ._constants import DEFAULT_MAX_FILE_BYTES, DEFAULT_THRESHOLD, PROG, Flag, Mode, ResponseFormat

if TYPE_CHECKING:
    from .catalog import ServerOptions


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `api-client-mcp` console script.

    :param argv: Argument list to parse (excluding the program name). Defaults to `sys.argv[1:]`
    """
    argv = sys.argv[1:] if argv is None else argv
    try:
        try:
            return _run(argv)
        finally:
            # A pipe write is block-buffered, so a downstream reader (e.g. `--help | head`) that
            # already exited only surfaces the broken pipe here, at an explicit flush.
            sys.stdout.flush()
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # dup2'ing stdout onto os.devnull stops the interpreter re-reporting the same failure while
        # flushing stdout again at shutdown. signal.SIGPIPE doesn't exist on Windows: 13 is its
        # universal POSIX value, used as a fallback there to keep the exit code conventional.
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull_fd, sys.stdout.fileno())
        finally:
            os.close(devnull_fd)
        return 128 + getattr(signal, "SIGPIPE", 13)


def _run(argv: list[str]) -> int:
    """Parse argv, resolve the app name, and serve until the connection closes.

    The server module is imported before discovery ever runs, since discovery puts the project root on
    `sys.path` and imports every top-level project module - a project with its own top-level `mcp` module
    would otherwise shadow the real SDK for every later import in the process. Logging is bootstrapped
    both before and after discovery, since a downstream project's import-time logging setup
    (triggered by discovery) can silently reset the operator's `--log-level`. Discovery itself runs
    inside a stdout reservation that's exited well before the connection is served, so the stdio
    transport can later claim the genuine, already-restored stdout.

    :param argv: Argument list to parse
    """
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return int(e.code) if e.code is not None else 0

    if importlib.util.find_spec("mcp") is None:
        write_error("The 'mcp' extra isn't installed (see README)")
        return 2

    if not args.app_name:
        parser.print_usage(sys.stderr)
        write_error("the following arguments are required: app_name")
        return 2

    from api_client_core.logging import setup_logging

    from . import server

    setup_logging(delta_config=STDERR_LOGGING_DELTA_CONFIG, level=args.log_level)

    from api_client_core._common.discovery import ensure_project_on_sys_path, find_client

    with reserve_stdout():
        ensure_project_on_sys_path()
        try:
            client_class = find_client(args.app_name)
        except LookupError as e:
            write_error(e)
            return 2

    # Re-applied now that discovery has run, restoring --log-level in case discovery reset it (see _run()'s docstring).
    setup_logging(delta_config=STDERR_LOGGING_DELTA_CONFIG, level=args.log_level)

    options = _options_from_args(args)

    async def _serve() -> int:
        """Prepare the server, reporting a startup failure as a clean usage error, then serve the
        connection until it closes.

        Only a startup failure (a bad client constructor, an unsatisfiable filter combination) is
        reported this way, at exit 2. A failure once the connection is already live is left to
        propagate as a genuine crash instead.
        """
        try:
            client, mcp_server = await server.prepare(client_class, options)
        except Exception as e:
            write_error(e)
            return 2
        return await server.serve_connection(client, mcp_server)

    return asyncio.run(_serve())


def _build_parser() -> argparse.ArgumentParser:
    """Build the fixed argument parser for `api-client-mcp`.

    Unlike the CLI generator, this argument surface never varies per discovered client - there's no
    per-endpoint flag to generate - so a single `argparse.ArgumentParser` built up front is enough.
    """
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Serve an API client's endpoints as MCP tools, generated directly from its endpoint definitions.",
    )
    parser.add_argument("app_name", nargs="?", metavar="<app-name>", help="app_name set in your API client")
    parser.add_argument(Flag.VERSION, action="version", version=_version_string())
    parser.add_argument(
        Flag.MODE,
        choices=tuple(m.value for m in Mode),
        default=Mode.AUTO.value,
        help="static: One tool per endpoint, fixed at startup, dynamic: four meta-tools that find and dispatch "
        "endpoints at call time, auto (default): choose automatically by endpoint count",
    )
    parser.add_argument(
        Flag.THRESHOLD,
        type=_non_negative_int,
        default=DEFAULT_THRESHOLD,
        metavar="N",
        help="In auto mode, the endpoint count (after filtering) at or above which dynamic mode is chosen "
        f"instead of static (default: {DEFAULT_THRESHOLD})",
    )
    parser.add_argument(
        Flag.RESOURCE, action="append", default=[], metavar="NAME", help="Expose only this resource (repeatable)"
    )
    parser.add_argument(
        Flag.INCLUDE,
        action="append",
        default=[],
        metavar="GLOB",
        help="Expose only tool names matching this glob (repeatable)",
    )
    parser.add_argument(
        Flag.EXCLUDE,
        action="append",
        default=[],
        metavar="GLOB",
        help="Never expose tool names matching this glob (repeatable, applied last)",
    )
    parser.add_argument(
        Flag.METHOD,
        action="append",
        default=[],
        choices=VALID_METHODS,
        type=str.lower,
        metavar="METHOD",
        help="Expose only endpoints using this HTTP method (repeatable)",
    )
    parser.add_argument(
        Flag.READ_ONLY, action="store_true", help="Expose only read-only (GET/HEAD/OPTIONS/TRACE) endpoints"
    )
    parser.add_argument(Flag.TOOL_PREFIX, metavar="TOKEN", help="Extra token prepended to every tool name")
    parser.add_argument(
        Flag.ALLOW_FILE_PATHS,
        action="store_true",
        help="Also accept a local filesystem path for a File parameter: a model can then have this "
        "process read any file it can read and send its content out over HTTP",
    )
    parser.add_argument(
        Flag.MAX_FILE_BYTES,
        type=_positive_int,
        default=DEFAULT_MAX_FILE_BYTES,
        metavar="BYTES",
        help=f"Cap on a File parameter's decoded size (default: {DEFAULT_MAX_FILE_BYTES})",
    )
    parser.add_argument(
        Flag.ALL_HEADERS,
        action="store_true",
        help="Include every response header in a tool call's result envelope, instead of the default "
        "allowlist of operationally useful ones (off by default: most response headers are noise a "
        "model can't act on, and some may be sensitive)",
    )
    parser.add_argument(
        Flag.RESPONSE_FORMAT,
        choices=tuple(f.value for f in ResponseFormat),
        default=ResponseFormat.FULL.value,
        help="Shape of a successful tool call's result: the {status_code, headers, body} envelope (full, "
        "the default), the bare decoded body (json), or undecoded response text (raw)",
    )
    parser.add_argument(Flag.BASE_URL, metavar="URL", help="Override the client's default base URL")
    parser.add_argument(
        "-H",
        Flag.HEADER,
        action="append",
        type=_parse_header,
        default=[],
        metavar="NAME:VALUE",
        help="Extra request header (repeatable)",
    )
    parser.add_argument(
        Flag.LOG_LEVEL, choices=LOG_LEVELS, type=str.upper, metavar="LEVEL", help="Log level for stderr diagnostics"
    )
    parser.add_argument(
        Flag.LOG_REQUESTS,
        action="store_true",
        help="Keep per-call request/response logging on (off by default: an MCP host usually surfaces "
        "server stderr in a log pane, where this is noise)",
    )
    return parser


def _version_string() -> str:
    """Return the `--version` output.

    Called once per parser build, since argparse's `action="version"` needs the string ready at
    `add_argument()` time. Resolving the version here is cheap and never pulls in `httpx2`.
    """
    from .. import __version__

    return f"{PROG} {__version__}"


def _options_from_args(args: argparse.Namespace) -> ServerOptions:
    """Build a `ServerOptions` from the parsed namespace.

    :param args: Parsed argument namespace
    """
    from .catalog import ServerOptions

    return ServerOptions(
        mode=Mode(args.mode),
        threshold=args.threshold,
        resources=tuple(args.resource),
        include=tuple(args.include),
        exclude=tuple(args.exclude),
        methods=tuple(args.method),
        read_only=args.read_only,
        tool_prefix=args.tool_prefix,
        allow_file_paths=args.allow_file_paths,
        max_file_bytes=args.max_file_bytes,
        all_headers=args.all_headers,
        response_format=ResponseFormat(args.response_format),
        base_url=args.base_url,
        headers=tuple(args.header),
        log_requests=args.log_requests,
    )


def _parse_header(item: str) -> tuple[str, str]:
    """Parse one `-H`/`--header` value as a `NAME:VALUE` pair. Used directly as the flag's `type=`
    converter, so a malformed value is rejected by argparse itself, as a usage error, at parse time.

    :param item: Raw `-H` value
    """
    name, sep, value = item.partition(":")
    if not sep:
        raise argparse.ArgumentTypeError(f"invalid header {item!r}: expected NAME:VALUE")
    return name.strip(), value.strip()


def _non_negative_int(value: str) -> int:
    """Parse an int flag value that must not be negative. Used as `--threshold`'s `type=` converter, so
    argparse rejects a negative value as a usage error at parse time rather than letting it silently pin
    auto mode to dynamic. `0` is allowed (auto mode then always resolves to dynamic).

    :param value: Raw flag value
    """
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {parsed}")
    return parsed


def _positive_int(value: str) -> int:
    """Parse an int flag value that must be at least 1. Used as `--max-file-bytes`'s `type=` converter, so
    argparse rejects a zero or negative cap as a usage error at parse time rather than letting it reject
    every File argument later, one call at a time.

    :param value: Raw flag value
    """
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {parsed}")
    return parsed

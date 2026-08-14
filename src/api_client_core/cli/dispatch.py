from __future__ import annotations

import sys

from api_client_core import __version__
from api_client_core.logging import setup_logging

from ._constants import HELP_FLAGS, PROG, Flag
from ._stdout import cli_stdout
from .builder import build_initial_parser
from .discovery import ensure_project_on_sys_path, find_client
from .params import peek_log_level
from .runner import STDERR_LOGGING_DELTA_CONFIG, run
from .utils import write_error

_GLOBAL_VALUE_FLAGS = (Flag.BASE_URL, Flag.LOG_LEVEL)


def dispatch(argv: list[str] | None = None) -> int:
    """Resolve the leading token to a discovered `APIClient` subclass and dispatch the call.

    A `--base-url`/`--log-level` given ahead of the app name is skipped over rather than mistaken for it, so
    `api-client --log-level DEBUG my-app ...` resolves the app the same way `api-client my-app --log-level
    DEBUG ...` already does. `-h`/`--help`/`--version` given ahead of the app name (or with no app name at
    all) are honored the same way they are as the very first token, rather than being mistaken for an
    unrecognized option. Any other leading option - a genuine typo, or a flag given with no app name at all -
    is reported as a dedicated usage error naming it, instead of a confusing "invalid app name" message.

    A `--log-level` given anywhere in `argv`, including ahead of the app name, is honored on every path
    through this function, not just the one that goes on to resolve a client: an empty `argv`, a leading
    `-h`/`--help`, and an unresolved app name all still benefit from it, e.g. to see the DEBUG-level reason a
    client failed to be discovered at all.

    :param argv: Argument list including the leading app-name token. Defaults to `sys.argv[1:]`
    """
    argv = sys.argv[1:] if argv is None else argv
    log_level = peek_log_level(argv)

    if not argv:
        _bootstrap(log_level)
        build_initial_parser().print_help(sys.stderr)
        return 2

    arg0 = argv[0]
    if arg0 in HELP_FLAGS:
        _bootstrap(log_level)
        build_initial_parser().print_help()
        return 0
    if arg0 == Flag.VERSION:
        # Skips bootstrap and discovery: a version check shouldn't pay the latency of importing every project module
        # just to print a version string.
        print(f"{PROG} {__version__}", file=cli_stdout())
        return 0

    app_name, rest, leading_flag = _peek_app_name(argv)
    if app_name is None:
        if leading_flag in HELP_FLAGS:
            _bootstrap(log_level)
            build_initial_parser().print_help()
            return 0
        if leading_flag == Flag.VERSION:
            print(f"{PROG} {__version__}", file=cli_stdout())
            return 0
        _bootstrap(log_level)
        parser = build_initial_parser()
        if leading_flag is None:
            parser.print_help(sys.stderr)
        else:
            parser.print_usage(sys.stderr)
            write_error(f"unrecognized option: {leading_flag!r}")
        return 2

    _bootstrap(log_level)

    try:
        client_class = find_client(app_name)
    except LookupError as e:
        write_error(e)
        return 2

    return run(client_class, rest, prog=f"{PROG} {app_name}", log_level=log_level)


def _peek_app_name(argv: list[str]) -> tuple[str | None, list[str], str | None]:
    """Find the app name token in `argv`, skipping any `--base-url`/`--log-level` given ahead of it.

    Scans left to right, skipping a recognized global flag (and its value, if any) at each step, until it
    finds either the app name - the first token that isn't one of those flags or their value - or a leading
    option one of those flags doesn't cover. The latter may be `-h`/`--help`/`--version`, which the caller
    still recognizes and handles, a genuine unrecognized option, or a recognized global flag that's missing
    its own value (the last token in `argv`, with no `=value` and nothing following it) - the caller reports
    the latter two as distinct usage errors. A flag's value may be given as a separate following token or
    joined with `=` (e.g. `--base-url=https://x`), matching argparse's own accepted forms. `--base-url`/
    `--log-level` given *after* the app name are left untouched in `rest` for the real per-level parsers to
    see again, exactly as an app name given with no leading flags already works today.

    :param argv: Full argument list, including the app name wherever it appears
    """
    i = 0
    while i < len(argv):
        token = argv[i]
        flag, eq, _value = token.partition("=")
        if flag in _GLOBAL_VALUE_FLAGS:
            if not eq and i + 1 >= len(argv):
                # The flag is missing its own value: reported as such by the caller, rather than silently
                # falling through to the end of argv, which would otherwise be indistinguishable from no
                # leading flag having been given at all.
                return None, [], token
            i += 1 if eq else 2
            continue
        if token.startswith("-"):
            return None, [], token
        return token, argv[:i] + argv[i + 1 :], None
    return None, [], None


def _bootstrap(log_level: str | None = None) -> None:
    """Configure logging and put the project on `sys.path`, ahead of any discovery.

    :param log_level: Log level override for the `api_client_core`/`common_libs` loggers, if given
    """
    setup_logging(delta_config=STDERR_LOGGING_DELTA_CONFIG, level=log_level)
    ensure_project_on_sys_path()

# PYTHON_ARGCOMPLETE_OK
"""Console entry point for the `api-client` command.

This module exists to keep tab completion fast. Anything with real dependencies is imported function-locally,
deferred past the completion hot path.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ._cache import cache_key, load_cache, mark_completion_registered, save_cache
from ._completion_schema import CompletionTree, OptSpec
from ._constants import PROG, Flag
from ._paths import find_project_root, project_roots
from ._stdout import reserve_stdout


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `api-client` console script.

    A shell-completion request is served entirely by a stdlib+argcomplete-only fast path and never reaches
    a real run, so it never pays the cost of a real run's heavier imports.

    The real stdout is reserved for the duration of both the completion path and a real run, so the only things that can
    ever reach it are the CLI's own output (help text, `--version`, an `--output json` payload, or a completion listing)
    rather than a stray write made by downstream project code.

    :param argv: Argument list to dispatch (including the leading app name). Defaults to `sys.argv[1:]`
    """
    if os.environ.get("_ARGCOMPLETE"):
        with reserve_stdout():
            _complete()
        return 0

    try:
        try:
            with reserve_stdout():
                from api_client_core.cli.dispatch import dispatch

                return dispatch(argv)
        finally:
            # A pipe write is block-buffered, so a downstream reader (e.g. `| head`) that already exited only
            # surfaces the broken pipe here, at an explicit flush, rather than at print() time. The `finally`
            # covers `dispatch()` exiting via `SystemExit` (e.g. `--help`/a usage error below the top level)
            # as well as a normal return, since either way this is the last write to stdout in the process.
            sys.stdout.flush()
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # signal.SIGPIPE doesn't exist on Windows: 13 is its universal POSIX value, used as a fallback there
        # purely to keep the exit code conventional, since the BrokenPipeError itself is POSIX-pipe-specific.
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull_fd, sys.stdout.fileno())
        finally:
            os.close(devnull_fd)
        return 128 + getattr(signal, "SIGPIPE", 13)


def _complete() -> None:
    """Handle a shell completion request and exit.

    Serves a cached completion tree when one is fresh for the current project, rebuilding it only on a cache
    miss. A no-op if the optional `argcomplete` dependency isn't installed, or if the current directory isn't
    inside any project.

    `_ARGCOMPLETE` is set exclusively by the shell function `register-python-argcomplete` installs, so reaching this
    point at all (with `argcomplete` importable) is itself proof that `eval "$(register-python-argcomplete ...)"` is
    already active in the current shell. `mark_completion_registered()` records that once, so the tab-completion
    setup tip stops suggesting that step everywhere else.

    Setting the `_ARC_DEBUG` environment variable forces a fresh rebuild, enables debug logging, disables the
    rebuild pruning below, and re-raises any unexpected failure instead of silently giving up.
    """
    try:
        import argcomplete
    except ImportError:
        return
    mark_completion_registered()

    if find_project_root(Path.cwd()) is None:
        return

    debug = bool(os.environ.get("_ARC_DEBUG"))
    try:
        if debug:
            from api_client_core.logging import setup_logging

            setup_logging(level="DEBUG")

        key = cache_key(project_roots())
        tree = None if debug else load_cache(key)
        if tree is None:
            from api_client_core.cli.builder import build_completion_tree

            if debug:
                tree = build_completion_tree()
            else:
                # A rebuild imports every discoverable project module, so any import-time output it triggers
                # (a bare print(), a logging handler bound to sys.__stdout__/sys.__stderr__, ...) is silenced
                # here rather than left to land on the terminal mid-completion.
                with _silence_streams():
                    tree = build_completion_tree()
            save_cache(key, tree)

        prune_to = None if debug else _typed_app_and_resource()
        # Suppresses path completion for a flag with no choices. Only a file-accepting flag should complete to a path,
        # and that only takes effect if the shell was registered with --no-defaults.
        parser = _build_parser_from_tree(tree, prune_to=prune_to)
        argcomplete.autocomplete(parser, default_completer=lambda **kwargs: [])
    except KeyboardInterrupt:
        raise
    except BaseException:
        # BaseException, not Exception: a project module's own import-time SystemExit (e.g. a stray script
        # calling sys.exit()) is already recorded, never raised, by discovery.py's own _try_import(). This is
        # still the shell-completion hot path, though, so anything else unforeseen must never be allowed to
        # kill the calling shell either.
        if debug:
            raise
        return


@contextmanager
def _silence_streams() -> Iterator[None]:
    """Silence stdout and stderr for the duration of the block.

    Redirects both the underlying file descriptors and the ``sys.stdout`` / ``sys.stderr`` objects, covering normal
    Python output as well as direct writes to file descriptors 1 and 2.

    The completion protocol uses a separate file descriptor, so completion results are unaffected. Flushes the original
    standard streams before restoring the file descriptors to prevent buffered output from escaping after the block
    exits.
    """
    devnull_fd = os.open(os.devnull, os.O_WRONLY)
    saved_stdout_fd = os.dup(1)
    saved_stderr_fd = os.dup(2)
    saved_stdout = sys.stdout
    saved_stderr = sys.stderr
    devnull_stream = open(os.devnull, "w")

    try:
        os.dup2(devnull_fd, 1)
        os.dup2(devnull_fd, 2)
        sys.stdout = devnull_stream
        sys.stderr = devnull_stream

        yield
    finally:
        # Stop ordinary Python writes from going to the devnull stream.
        sys.stdout = saved_stdout
        sys.stderr = saved_stderr

        # Flush buffered writes while fd 1/2 still point at devnull.
        try:
            if sys.__stdout__ is not None:
                sys.__stdout__.flush()
            if sys.__stderr__ is not None:
                sys.__stderr__.flush()
        finally:
            # These must happen even if flushing fails.
            os.dup2(saved_stdout_fd, 1)
            os.dup2(saved_stderr_fd, 2)

            os.close(saved_stdout_fd)
            os.close(saved_stderr_fd)
            os.close(devnull_fd)
            devnull_stream.close()


def _typed_app_and_resource() -> tuple[str | None, str | None] | None:
    """Best-effort peek of the app name and resource-group name already typed ahead of the token currently
    being completed, so `_build_parser_from_tree()` can skip fully materializing a branch a completion
    request can't possibly need.

    Reads the same `COMP_LINE`/`COMP_POINT`/`_ARGCOMPLETE` env vars, and the same word-splitting
    (`argcomplete.split_line()`), that `argcomplete.autocomplete()` itself uses to resolve the words already
    typed, rather than a separately-derived guess that could disagree with it. Mirrors `dispatch.py`'s own
    `_peek_app_name()` in skipping over a `--base-url`/`--log-level` flag (and its value) given ahead of
    either name, but is deliberately reimplemented here rather than imported: `dispatch.py` pulls in this
    package's full runtime dependency chain at module scope, exactly what this module (on the shell-completion
    hot path) exists to avoid paying for on every keystroke. Stops at the first token it doesn't recognize
    (any other flag, or a positional beyond the second) rather than guessing further, so a wrong guess can
    only make the caller build *more* of the tree than strictly needed, never less.

    Returns `None` on any missing or malformed completion-request state, so the caller falls back to building
    the whole tree rather than risking an incorrect prune.
    """
    try:
        from argcomplete import split_line

        comp_line = os.environ["COMP_LINE"]
        comp_point = int(os.environ["COMP_POINT"])
        start = int(os.environ["_ARGCOMPLETE"]) - 1
        _, _, _, words, _ = split_line(comp_line, comp_point)
        argv = words[start + 1 :]  # +1 drops the program name itself, matching dispatch()'s own `argv`
    except Exception:
        return None

    found: list[str] = []
    i = 0
    while i < len(argv) and len(found) < 2:
        token = argv[i]
        flag, eq, _value = token.partition("=")
        if flag in (Flag.BASE_URL, Flag.LOG_LEVEL):
            i += 1 if eq or i + 1 >= len(argv) else 2
            continue
        if token.startswith("-"):
            break
        found.append(token)
        i += 1
    return (found[0] if found else None), (found[1] if len(found) > 1 else None)


def _build_parser_from_tree(
    tree: CompletionTree, *, prune_to: tuple[str | None, str | None] | None = None
) -> argparse.ArgumentParser:
    """Rebuild an `argparse.ArgumentParser` from a cached completion tree.

    Never re-adds `-h`/`--help`, since argparse adds it to every parser itself. A zero-arg (`"nargs": 0`) flag
    is rebuilt as `store_true` so completion doesn't consume the next token as its value; every other flag
    keeps its own real `"nargs"`, so a repeatable one (`"?"`/`"*"`/`"+"`) still behaves that way here too.

    `--version` is added directly here as a plain `store_true` stand-in, since it belongs to the top-level
    command itself rather than to any discovered client, and completion only needs the option string to
    exist.

    `prune_to`, when given, skips fully materializing a branch a completion request can't possibly need:
    every app, and every resource within the one app that matches `prune_to`, still gets its own name
    registered as a valid completion choice, but only the app matching `prune_to[0]` gets its own flags and
    resources built at all, and within it, only the resource matching `prune_to[1]` gets its own flags and
    commands built. A sibling branch that was never typed can only ever need its name listed, since
    `argcomplete` never walks into a subparser the input didn't actually select. Left `None` (the default) to
    build the whole tree unpruned, the exact-inverse shape `build_completion_tree()`'s own docstring promises.

    :param tree: Completion tree, as built fresh or loaded from cache
    :param prune_to: `(app name, resource name)` already typed ahead of the token being completed, as
                     resolved by `_typed_app_and_resource()`, or `None` to build every branch in full
    """
    from .parser import ArgumentParser

    typed_app, typed_resource = prune_to if prune_to is not None else (None, None)
    parser = ArgumentParser(prog=PROG)
    parser.add_argument(Flag.VERSION, action="store_true")
    app_subparsers = parser.add_subparsers(dest="app_name")
    for app_name, app_spec in tree.items():
        app_parser = app_subparsers.add_parser(app_name)
        if prune_to is not None and app_name != typed_app:
            continue
        _add_options(app_parser, app_spec.get("opts", []))
        resource_subparsers = app_parser.add_subparsers(dest="_resource")
        for resource_name, resource_spec in app_spec.get("resources", {}).items():
            resource_parser = resource_subparsers.add_parser(resource_name)
            if prune_to is not None and resource_name != typed_resource:
                continue
            _add_options(resource_parser, resource_spec.get("opts", []))
            command_subparsers = resource_parser.add_subparsers(dest="_command")
            for command_name, opts in resource_spec.get("commands", {}).items():
                command_parser = command_subparsers.add_parser(command_name)
                _add_options(command_parser, opts)
    return parser


def _at_file_completer(prefix: str, **kwargs: Any) -> list[str]:
    """Complete a JSON-typed flag's `@<path>` value to real filesystem paths, once `@` itself is typed.

    A no-op for a prefix that doesn't start with `@` (empty, `-`, or inline JSON), since none of those have
    a path to complete against, matching the flag's own no-completion behavior before `@` is typed.

    :param prefix: The word being completed, as given by `argcomplete`
    :param kwargs: Additional `argcomplete` completer arguments (`action`/`parser`/`parsed_args`), forwarded
                  to `FilesCompleter` unchanged
    """
    if not prefix.startswith("@"):
        return []

    from argcomplete.completers import FilesCompleter

    return [f"@{path}" for path in FilesCompleter()(prefix=prefix.removeprefix("@"), **kwargs)]


def _add_options(parser: argparse.ArgumentParser, opts: list[OptSpec]) -> None:
    """Add a parser's flags from their serialized `OptSpec` dicts.

    A zero-`"nargs"` flag is rebuilt as `store_true`, so completion doesn't consume the next token as its own
    value the way a default `store` action with `nargs=0` would leave ambiguous. Every other flag keeps its
    own real `"nargs"` (`None` for a single value, or one of `"?"`/`"*"`/`"+"`), so e.g. a repeatable flag
    keeps accepting further values in the same occurrence here too, rather than always being treated as
    single-valued regardless of what the real parser actually accepts.

    An `"is_file"` flag gets a real `FilesCompleter`, so its value still completes to filesystem paths. An
    `"is_json_file"` flag gets `_at_file_completer` instead, so its value completes to paths only once `@`
    itself is typed, leaving an inline JSON value or `-` (stdin) uncompleted.

    :param parser: App-level or leaf command parser to add the flags to
    :param opts: `OptSpec` list to add
    """
    for spec in opts:
        option_strings = spec["opts"]
        nargs = spec.get("nargs")
        if nargs == 0:
            parser.add_argument(*option_strings, action="store_true")
            continue

        add_kwargs: dict[str, Any] = {"nargs": nargs}
        choices = spec.get("choices")
        if choices:
            add_kwargs["choices"] = choices
        action = parser.add_argument(*option_strings, **add_kwargs)
        if spec.get("is_file"):
            from argcomplete.completers import FilesCompleter

            action.completer = FilesCompleter()  # type: ignore[attr-defined]
        elif spec.get("is_json_file"):
            action.completer = _at_file_completer  # type: ignore[attr-defined]

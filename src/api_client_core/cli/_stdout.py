"""Stdlib-only helpers that reserve the real `sys.stdout` for the CLI's own output.

`api-client`'s policy is that stdout carries only what the CLI itself emits (`--help`/usage text, `--version`, the
`--output json` payload), and everything else in the process, including a downstream project's own logging and any stray
`print()`, goes to stderr. `reserve_stdout()` establishes that policy once, at the process entry point, rather than
guarding every place downstream code might run: this is what lets `logging.config.dictConfig` binding a handler straight
to `ext://sys.stdout` at import time (as `openapi_test_client` does) end up on stderr instead. `dictConfig` resolves
that target to a stream *object* at config time, so the handler is bound for good, and only redirecting `sys.stdout`
before that import runs can affect it.

`cli_stdout()` gives the CLI's own output code a name for the reserved stream regardless of whether a reservation is
active, and `cli_output()` briefly points `sys.stdout` at a given stream (`cli_stdout()` by default) so text is
colorized against the stream it's actually written to, rather than against whichever stream `sys.stdout` names at the
time.

Stdlib-only, matching the existing `_paths.py`/`_cache.py` convention for internal CLI infrastructure, since this module
is imported at module scope by `_entrypoint.py`, which is on the shell-completion hot path.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TextIO

# Holds the real stdout while a `reserve_stdout()` block is active
_reserved: list[TextIO] = []


@contextmanager
def reserve_stdout() -> Iterator[None]:
    """Reserve the real `sys.stdout` and point `sys.stdout` at `sys.stderr` for the duration of the block.

    Re-entrant: a nested call is a no-op, so the outermost reservation always owns the real stream and an inner one
    (e.g. `_complete()` calling into code that also opens a reservation) can't clobber it.
    """
    if _reserved:
        yield
        return

    _reserved.append(sys.stdout)
    sys.stdout = sys.stderr
    try:
        yield
    finally:
        sys.stdout = _reserved.pop()


def cli_stdout() -> TextIO:
    """Return the stream the CLI's own output (help text, `--version`, `--output json`) should be written to.

    The reserved real stdout while a `reserve_stdout()` block is active, or plain `sys.stdout` otherwise, so code that
    calls `run()`/`dispatch()` directly (as tests do) without going through a reservation still writes to `sys.stdout`
    as it always has.
    """
    return _reserved[-1] if _reserved else sys.stdout


@contextmanager
def cli_output(stream: TextIO | None = None) -> Iterator[None]:
    """Point `sys.stdout` at `stream` for the duration of the block, restoring it afterward.

    `stream` defaults to `cli_stdout()`, so a call with no argument is a no-op outside a `reserve_stdout()` block
    (`cli_stdout()` is then `sys.stdout` already). Used to make a `color()` decision, which only ever consults
    `sys.stdout`, follow the stream some text is actually bound for: `cli_stdout()` for CLI-owned stdout text (the
    default), or an explicit `sys.stderr` for text written there (an `error:` line, a usage block printed to stderr).

    :param stream: Stream color decisions should follow for the duration of the block. Defaults to `cli_stdout()`
    """
    if stream is None:
        stream = cli_stdout()
    prior = sys.stdout
    sys.stdout = stream
    try:
        yield
    finally:
        sys.stdout = prior

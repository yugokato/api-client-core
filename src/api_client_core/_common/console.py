"""Stdlib-only helpers that reserve the real `sys.stdout` for a generator's own output, plus the shared
error-line formatter both generators write with.

Both generators share one policy: stdout carries only what the generator itself emits (help text,
`--version`, an output payload), and everything else - including a downstream project's logging and any
stray `print()` - goes to stderr. `reserve_stdout()` establishes that policy once, at the process entry
point: this is what lets a `logging.config.dictConfig` handler bound straight to `ext://sys.stdout` at
import time end up on stderr instead, since only redirecting `sys.stdout` before that import runs can
affect it.

Stays stdlib-only at module scope, and lives in this shared package, since both generators import it at
module scope on their own fast startup path and must not depend on each other. `write_error()` needs
color support, whose import is too heavy for that path, so it imports it function-locally instead.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TextIO

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
# setup_logging()'s delta_config, redirecting the console handler to stderr so `-h`/`--output`/etc. stay
# stdout-clean. Shared by both generators, which must apply the identical redirect.
STDERR_LOGGING_DELTA_CONFIG = {"handlers": {"console": {"stream": "ext://sys.stderr"}}}

# Holds the real stdout while a `reserve_stdout()` block is active
_reserved: list[TextIO] = []


@contextmanager
def reserve_stdout() -> Iterator[None]:
    """Reserve the real `sys.stdout` and point `sys.stdout` at `sys.stderr` for the duration of the block.

    Re-entrant: a nested call is a no-op, so the outermost reservation always owns the real stream and an
    inner one can't clobber it.
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


def real_stdout() -> TextIO:
    """Return the stream a generator's own output (help text, `--version`, an output payload) should be written to.

    The reserved real stdout while a `reserve_stdout()` block is active, or plain `sys.stdout` otherwise,
    so code that never goes through a reservation (e.g. a direct call in a test) still behaves as it
    always has.
    """
    return _reserved[-1] if _reserved else sys.stdout


@contextmanager
def output_to(stream: TextIO | None = None) -> Iterator[None]:
    """Point `sys.stdout` at `stream` for the duration of the block, restoring it afterward.

    Used to make a `color()` decision, which only ever consults `sys.stdout`, follow the stream some text
    is actually bound for, rather than whichever stream `sys.stdout` names at the time.

    :param stream: Stream color decisions should follow for the duration of the block. Defaults to `real_stdout()`
    """
    if stream is None:
        stream = real_stdout()
    prior = sys.stdout
    sys.stdout = stream
    try:
        yield
    finally:
        sys.stdout = prior


def format_error_message(err: BaseException | str) -> str:
    """Format an error as a one-line message: a `LookupError`/`RuntimeError` (the types this package's own
    CLI/MCP code raises for an already self-descriptive usage failure) as its bare message, since the
    class name adds nothing. Any other exception type keeps its class name prefixed, since a terser
    message (e.g. a bare `KeyError`'s quote-only `str()`) benefits from that context.

    :param err: The error message, or the exception being formatted
    """
    if isinstance(err, LookupError | RuntimeError):
        return str(err)
    if isinstance(err, BaseException):
        return f"{type(err).__name__}: {err}"
    return err


def write_error(err: BaseException | str) -> None:
    """Write a red `error: <err>` line to stderr.

    :param err: The error message, or the exception being reported
    """
    # Function-local: color support is too heavy for the shell-completion hot path that imports this
    # module at module scope. This only ever runs on an actual error path or a real command.
    from common_libs.ansi_colors import ColorCodes, color

    with output_to(sys.stderr):
        error_line = color(f"error: {format_error_message(err)}\n", color_code=ColorCodes.RED)
    sys.stderr.write(error_line)

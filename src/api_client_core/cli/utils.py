from __future__ import annotations

from shutil import get_terminal_size
from textwrap import wrap
from typing import Any

from common_libs.ansi_colors import color

from .._common.console import output_to


def color_output(text: Any, **kwargs: Any) -> str:
    """Colorize `text` bound for the CLI's own stdout.

    `color()` decides whether to emit ANSI from `sys.stdout`, which points at stderr for the duration of a
    `reserve_stdout()` block. Restoring the real stdout for the call makes the decision follow the stream
    this text is actually written to, rather than stderr's.

    :param text: The text to colorize
    :param kwargs: Keyword arguments forwarded to `color()`
    """
    with output_to():
        return color(text, **kwargs)


def get_terminal_width() -> int:
    """Return the current terminal width, falling back to 80 columns when it can't be determined."""
    return get_terminal_size(fallback=(80, 24)).columns


def indent_text(text: str, indent: int = 2) -> str:
    """Add indentation to every line in the given text.

    :param text: The text to be indented
    :param indent: The number of spaces to indent each line by
    """
    return "\n".join(" " * indent + line for line in text.splitlines())


_MIN_BOX_WIDTH = 20  # keeps a pathologically narrow terminal's box legible rather than crashing the whole command


def box_text(text: str, width: int | None = None, padding: int = 1) -> str:
    """Return `text` surrounded by a Unicode box.

    :param text: The text to be boxed
    :param width: The width of the box. Defaults to the terminal width, floored to `_MIN_BOX_WIDTH`. An
                  explicitly given `width` narrower than what `padding` needs still raises, rather than being
                  silently floored, since that's a caller bug rather than an environment quirk to work around
    :param padding: The number of spaces between the box's border and its text
    """
    if width is None:
        width = max(get_terminal_width(), _MIN_BOX_WIDTH)

    inner = width - 2 - 2 * padding
    if inner < 1:
        raise ValueError("width too small")

    lines = []
    for paragraph in text.splitlines():
        if not paragraph.strip():
            lines.append("")
        else:
            # A paragraph's own leading whitespace is kept on its wrapped continuation lines too (wrap()
            # only keeps it on the first), so an indented block (e.g. a nested list) doesn't lose its
            # indentation once it wraps. Capped below `inner`: an indent at or past the wrap width would
            # otherwise force wrap() to emit lines wider than `inner`, breaking the box's own border.
            own_indent = len(paragraph) - len(paragraph.lstrip())
            subsequent_indent = " " * min(own_indent, inner - 1)
            lines.extend(wrap(paragraph, inner, subsequent_indent=subsequent_indent))

    top = "┌" + "─" * (width - 2) + "┐"
    bottom = "└" + "─" * (width - 2) + "┘"

    body = [f"│{' ' * padding}{line:<{inner}}{' ' * padding}│" for line in lines]

    return "\n".join([top, *body, bottom])

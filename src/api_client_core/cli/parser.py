from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from itertools import takewhile
from textwrap import wrap
from typing import IO, Any, NoReturn, TextIO, cast

from common_libs.ansi_colors import ColorCodes, color

from ._constants import ELLIPSIS, Flag
from ._stdout import cli_output, cli_stdout
from .utils import get_terminal_width

_SHORT_HELP_TIP = "Use --help for full details"
_MIN_NOTE_WIDTH = 11  # mirrors argparse.HelpFormatter._format_text's own floor
# Attribute set on an argparse Action by cli/params.py's set_full_metavar(): a full, unelided companion to the
# action's own (possibly elided) metavar, substituted in by _HelpFormatter._metavar_formatter() under --help.
_FULL_METAVAR_ATTR = "_full_metavar"


class ArgumentParser(argparse.ArgumentParser):
    """Customized `argparse.ArgumentParser`."""

    def __init__(
        self, *args: Any, tips: Sequence[str] | None = None, warnings: Sequence[str] | None = None, **kwargs: Any
    ) -> None:
        """Initialize the parser.

        :param tips: Tips rendered as a colored, wrapped block in this parser's own help output
        :param warnings: Warnings rendered the same way, ahead of `tips`
        :param args: Positional arguments forwarded to `argparse.ArgumentParser`
        :param kwargs: Keyword arguments forwarded to `argparse.ArgumentParser`
        """
        kwargs.setdefault("formatter_class", _HelpFormatter)
        kwargs.setdefault("allow_abbrev", False)
        if sys.version_info >= (3, 14):
            kwargs.setdefault("suggest_on_error", True)
        super().__init__(*args, **kwargs)
        self.tips = tuple(tips or ())
        self.warnings = tuple(warnings or ())

    def error(self, message: str) -> NoReturn:
        """Print this parser's own usage line followed by a red `error: <message>` line to stderr, then exit
        with code 2, mirroring `argparse.ArgumentParser.error()`'s own contract but through this class's
        colored, stream-aware `print_usage()`/`cli_output()` instead of a bare `sys.stderr.write()`.

        :param message: The error message, without the leading `error: ` this method adds itself
        """
        self.print_usage(sys.stderr)
        with cli_output(sys.stderr):
            error_message = color(f"error: {message}\n", color_code=ColorCodes.RED)
        self.exit(2, error_message)

    def print_help(self, file: IO[str] | None = None, *, short: bool = False) -> None:
        """Resolve the default output stream before `argparse` replaces `None` with `sys.stdout`, preserving an explicit
        `sys.stderr` even when `reserve_stdout()` makes `sys.stdout is sys.stderr`. Wrapping in `cli_output()` keeps
        formatter color decisions consistent with that resolved stream. The `cast()` reflects what is already true at
        runtime (`file` is always stdout/stderr), and `_print_message()` preserves `argparse`'s normal broken-pipe
        handling.

        :param file: Stream to print to. Defaults to `cli_stdout()` (the reserved real stdout, if a
                     `reserve_stdout()` block is active, else `sys.stdout` itself), matching `argparse`'s own
                     default-to-`sys.stdout` behavior
        :param short: Forwarded to `format_help()`: whether to render the condensed `-h` form
        """
        stream = cli_stdout() if file is None else cast(TextIO, file)
        with cli_output(stream):
            self._print_message(self.format_help(short=short), stream)

    def print_usage(self, file: IO[str] | None = None) -> None:
        """Print this parser's own usage line, following the same stream-resolution and color rules as
        `print_help()`.

        :param file: Stream to print to. Defaults to `cli_stdout()`, matching `print_help()`'s own default
        """
        stream = cli_stdout() if file is None else cast(TextIO, file)
        with cli_output(stream):
            super().print_usage(stream)

    def format_help(self, *, short: bool = False) -> str:
        """Render this parser's help text.

        This parser's own `warnings`/`tips` (see `__init__`) are rendered as one colored, wrapped block
        appended after any `epilog=`, warnings first. With `short=True`, every flag's help is reduced to
        its own summary line, plus a clamped, single-line form of any indented nested detail beneath it
        (an unindented detail paragraph is dropped instead), keeping a leaf command's `-h` output scannable,
        and a tip pointing to `--help` is added to that block. `--help` always renders every flag's full
        help, with no such tip.

        :param short: Whether to render the condensed form
        """
        tips = [*self.tips, _SHORT_HELP_TIP] if short else self.tips
        notes = _format_notes(self.warnings, tips)

        # Swaps the public formatter_class/epilog attributes for the duration of this one render, rather
        # than overriding _get_formatter()/add_text() (which would need to tell an epilog= render apart
        # from a description= one), or mutating self._action_groups, which would make rendering mutate
        # the parser.
        formatter_class = self.formatter_class
        epilog = self.epilog
        if short:
            self.formatter_class = _ShortHelpFormatter
        self.epilog = "\n\n".join(part for part in (epilog, notes) if part) or None
        try:
            return super().format_help()
        finally:
            self.formatter_class = formatter_class
            self.epilog = epilog


class CollapsibleText(str):
    """An argument group's `description=` that collapses the whole group under `-h`.

    Renders as itself, followed by the group's own flags, under `--help`. Under `-h`, `short` renders
    in its place, uncolored, and the group's own flags are dropped.

    :param text: Text rendered under `--help`
    :param short: Text rendered under `-h`, in place of both this text and the group's own flags
    """

    short: str

    def __new__(cls, text: str, *, short: str) -> CollapsibleText:
        obj = super().__new__(cls, text)
        obj.short = short
        return obj


class HelpAction(argparse._HelpAction):
    """`-h`/`--help` action that renders condensed help for `-h` and full help for `--help`.

    Subclasses argparse's own `_HelpAction` rather than `argparse.Action` directly, reusing its
    `__init__` (`nargs=0`, etc.) unchanged. `parser` is always this module's `ArgumentParser`, since it's
    the only parser class this action is ever registered on, but the base `argparse.Action.__call__`
    signature types it as `argparse.ArgumentParser`.
    """

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[Any] | None,
        option_string: str | None = None,
    ) -> None:
        cast(ArgumentParser, parser).print_help(short=option_string != Flag.HELP)
        parser.exit()


def default_metavar(dest: str) -> str:
    """Render the metavar argparse falls back to for an action given no explicit `metavar`.

    Same as argparse's own `dest.upper()` default, minus any leading/trailing underscore, which the
    generated flag already drops (e.g. `from_` -> `--from FROM`).

    :param dest: Action's `dest`
    """
    return dest.strip("_").upper()


def set_full_metavar(action: argparse.Action, metavar: str) -> None:
    """Attach a full, unelided companion to an action's own `metavar` (e.g. a Literal/Enum choice group
    elided past a width cap), substituted in by `_HelpFormatter` in its place under the full `--help` form.
    The condensed `-h` form keeps the action's own `metavar` unchanged.

    :param action: Action whose own `metavar` is already the form shown under `-h` and in the usage line
    :param metavar: Full, unelided form to substitute when rendering `--help`
    """
    setattr(action, _FULL_METAVAR_ATTR, metavar)


def full_metavar(action: argparse.Action) -> str | None:
    """Return the full, unelided metavar `set_full_metavar()` attached to an action, or `None` if none was.

    :param action: Action to check
    """
    return cast("str | None", getattr(action, _FULL_METAVAR_ATTR, None))


def _format_notes(warnings: Sequence[str], tips: Sequence[str]) -> str | None:
    """Render a parser's `warnings`/`tips` as one colored, wrapped block, warnings first.

    A blank item is dropped, so it can't turn a section from inline to bulleted, or leave a phantom
    blank line, on its own.

    :param warnings: Warning messages
    :param tips: Tip messages
    """
    width = max(get_terminal_width() - 2, _MIN_NOTE_WIDTH)
    sections = []
    for label, items in (("warning", warnings), ("tip", tips)):
        items = [item for item in items if item.strip()]
        if items:
            sections.append(_format_note_section(label, items, width))
    return color("\n\n".join(sections), color_code=ColorCodes.YELLOW) if sections else None


def _format_note_section(label: str, items: Sequence[str], width: int) -> str:
    """Render one labeled section of notes: a single item inline, two or more as a bulleted list.

    :param label: Singular label for one item (e.g. `"tip"`), pluralized for a bulleted list's header
    :param items: The section's items
    :param width: Width to wrap each item to
    """
    if len(items) == 1:
        return _format_note(items[0], f"{label}: ", width)
    return "\n".join([f"{label}s:", *(_format_note(item, "  - ", width) for item in items)])


def _format_note(text: str, prefix: str, width: int) -> str:
    """Wrap one note item to `width`, aligning continuation lines under `prefix`.

    Each of `text`'s own source lines keeps its relative indentation under `prefix`'s width, so a nested
    detail line (e.g. one import failure per line) stays indented under the item it belongs to. A line
    that is itself a `- ` bullet gets two extra spaces of hanging indent, so its own wrapped continuation
    falls under its text rather than under the bullet.

    :param text: The note's text, one or more lines
    :param prefix: Label/bullet prefix rendered ahead of the first line
    :param width: Width to wrap each line to
    """
    hang = " " * len(prefix)
    lines = []
    for i, line in enumerate(text.splitlines()):
        stripped = line.lstrip()
        indent = hang + " " * (len(line) - len(stripped))
        initial = prefix if i == 0 else indent
        subsequent = f"{indent}  " if stripped.startswith("- ") else indent
        wrapped = wrap(
            stripped,
            width,
            initial_indent=initial,
            subsequent_indent=subsequent,
            break_long_words=False,
            break_on_hyphens=False,
        )
        lines.extend(wrapped or [""])
    return "\n".join(lines)


class _HelpFormatter(argparse.RawTextHelpFormatter):
    _short: bool = False  # overridden by _ShortHelpFormatter
    _collapsed_section: bool = False  # set by add_text() for the duration of one CollapsibleText group

    def __init__(self, prog: str):
        super().__init__(prog, max_help_position=40)  # default is 24

    def _get_default_metavar_for_optional(self, action: argparse.Action) -> str:
        return default_metavar(action.dest)

    def _metavar_formatter(self, action: argparse.Action, default_metavar: str) -> Callable[[int], tuple[str, ...]]:
        full_metavar = getattr(action, _FULL_METAVAR_ATTR, None)
        if self._short or full_metavar is None:
            return super()._metavar_formatter(action, default_metavar)
        # Swaps action.metavar for the duration of this one call, mirroring format_help()'s own
        # formatter_class/epilog swap-and-restore above: the base implementation reads it directly, and
        # _metavar_formatter()'s own returned closure is always invoked synchronously by its caller.
        original_metavar = action.metavar
        action.metavar = full_metavar
        try:
            return super()._metavar_formatter(action, default_metavar)
        finally:
            action.metavar = original_metavar

    def start_section(self, heading: str | None) -> None:
        if heading is not None:
            heading = color(heading, color_code=ColorCodes.BLUE, bold=True)
        super().start_section(heading)

    def end_section(self) -> None:
        self._collapsed_section = False
        super().end_section()

    def add_text(self, text: str | None) -> None:
        if isinstance(text, CollapsibleText):
            if self._short:
                # Left uncolored, unlike a real description, so the note reads as plain terminal text.
                self._collapsed_section = True
                super().add_text(text.short)
                return
            # RawTextHelpFormatter (our base class) emits description= text verbatim, which is exactly what a
            # box_text()-drawn parser description needs, but a CollapsibleText's own full form is authored as
            # one plain paragraph (see wrappers.py's _WRAPPERS_GROUP_DESCRIPTION) and needs the same reflow
            # every other paragraph in this formatter gets. Wrapped here, ahead of coloring, since color()
            # returns a plain str (an f-string interpolation, not a CollapsibleText), so this is the last
            # point this branch can still tell a group description apart from ordinary raw text.
            text = "\n".join(wrap(text, self._width, break_long_words=False, break_on_hyphens=False))
        if text is not None:
            text = color(text, color_code=ColorCodes.DARK_GREY)
        super().add_text(text)

    def add_argument(self, action: argparse.Action) -> None:
        if not self._collapsed_section:
            super().add_argument(action)

    def _get_help_string(self, action: argparse.Action) -> str | None:
        # `HelpFormatter._expand_help()` unconditionally `%`-expands the string this returns, so a literal `%` in
        # user-authored help text would otherwise raise `TypeError` once `--help` is rendered. Not needed for
        # description=/epilog= text, which argparse only %-expands text containing `%(prog)`.
        help_string = super()._get_help_string(action)
        if help_string and self._short:
            # A help= string is authored as `summary\ndetail...` precisely so this split is meaningful. An indented
            # continuation line is a different case: params.py's own `:param` description is authored this way
            # specifically so it survives here too, clamped to one rendered line by _split_lines() below rather than
            # dropped outright. A single-line help is unaffected, since it has nothing after the first line to drop or
            # keep.
            first, *rest = help_string.split("\n")
            kept = takewhile(lambda line: line[:1].isspace(), rest)
            help_string = "\n".join((first.removesuffix("."), *kept))
        return help_string.replace("%", "%%") if help_string else help_string

    def _format_usage(self, usage: str | None, actions: Any, groups: Any, prefix: Any) -> str:
        # `prefix`/`self._prog` are colorized only after the layout (wrap width, indent) is computed against their plain
        # lengths. Coloring the prefix before calling the base implementation would make argparse's own len(prefix)
        # include invisible ANSI bytes, throwing off every wrapped line.
        plain_prefix = prefix if prefix is not None else "usage: "
        text = super()._format_usage(usage, actions, groups, plain_prefix)
        text = text.replace(plain_prefix, color(plain_prefix, color_code=ColorCodes.BLUE, bold=True), 1)
        return text.replace(self._prog, color(self._prog, color_code=ColorCodes.MAGENTA, bold=True), 1)

    def _split_lines(self, text: str, width: int) -> list[str]:
        # RawTextHelpFormatter (our base class) emits help= text verbatim, so a hand-inserted line break would otherwise
        # freeze at whatever width it was written for. Re-wrapping each paragraph to the real terminal width here keeps
        # an intentional newline as a hard break while still fitting the terminal. break_long_words/break_on_hyphens are
        # off so a copy-pasteable spec string or hyphenated word is never split mid-token. Only help= text goes through
        # this; description=/epilog= text is filled separately.
        lines = []
        for para in text.splitlines():
            # A paragraph's own leading whitespace (e.g. params.py's _DESCRIPTION_INDENT) is kept on its
            # wrapped continuation lines too, not just its first (wrap()'s own default), so an indented
            # line (e.g. a documented parameter's own description) doesn't lose its indent once it wraps.
            # Dropped entirely, rather than merely capped, once the indent itself reaches `width`: with
            # break_long_words=False below, wrap() can't shrink a wrapped word to make room, so even an
            # indent just short of `width` can still overflow it once a following word no longer fits.
            own_indent = len(para) - len(para.lstrip())
            subsequent_indent = " " * own_indent if own_indent < width else ""
            wrapped = wrap(
                para, width, subsequent_indent=subsequent_indent, break_long_words=False, break_on_hyphens=False
            ) or [""]
            if self._short and own_indent and len(wrapped) > 1:
                # An indented paragraph that survived _get_help_string()'s own -h filtering (a documented parameter's
                # own description) is a nested detail, not the summary line itself, so under -h it's clamped to its own
                # first rendered line rather than wrapped across several, marked with a trailing ELLIPSIS. Re-wrapped at
                # a narrower width first, rather than slicing the line already wrapped above, so the cut lands on a word
                # boundary. break_long_words=False means wrap() can't shrink an unbreakable word to make room, so a
                # final hard truncation is still needed as a last resort to guarantee the result never exceeds `width`.
                clamped = (
                    wrap(
                        para,
                        max(width - len(ELLIPSIS), 1),
                        subsequent_indent=subsequent_indent,
                        break_long_words=False,
                        break_on_hyphens=False,
                    )
                    or [""]
                )[0] + ELLIPSIS
                if len(clamped) > width:
                    clamped = clamped[: width - len(ELLIPSIS)] + ELLIPSIS
                lines.append(clamped)
                continue
            lines.extend(wrapped)
        return lines


class _ShortHelpFormatter(_HelpFormatter):
    """`_HelpFormatter` that renders only a flag's own summary line, plus a clamped, single-line form of any
    indented detail beneath it (e.g. a documented parameter's own `:param` description), dropping an
    unindented detail paragraph entirely. Also collapses any `CollapsibleText`-described argument group to
    that text's own `short` note, dropping the group's flags.

    Swapped into `formatter_class` for the duration of one render by `ArgumentParser.format_help(short=True)`.
    """

    _short: bool = True

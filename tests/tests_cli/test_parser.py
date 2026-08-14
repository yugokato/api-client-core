"""Unit tests for `api_client_core.cli.parser`"""

from __future__ import annotations

import argparse
import sys

import pytest
from common_libs.ansi_colors import ColorCodes, color, remove_color_code

from api_client_core.cli._constants import ELLIPSIS
from api_client_core.cli.parser import (
    _SHORT_HELP_TIP,
    ArgumentParser,
    CollapsibleText,
    default_metavar,
    full_metavar,
    set_full_metavar,
)
from api_client_core.cli.utils import get_terminal_width


class TestAllowAbbrev:
    """Tests for `ArgumentParser`'s `allow_abbrev=False` default"""

    def test_a_shortened_long_flag_is_a_usage_error_rather_than_a_match(self) -> None:
        """Test that an unambiguous prefix of a long flag (e.g. `--stat` for `--status`) is rejected as an
        unrecognized argument rather than silently accepted, unlike stock `argparse`'s default behavior
        """
        parser = ArgumentParser(prog="test", exit_on_error=False)
        parser.add_argument("--status")
        with pytest.raises((argparse.ArgumentError, SystemExit)):
            parser.parse_args(["--stat", "active"])

    def test_the_full_flag_still_matches(self) -> None:
        """Test that the flag's exact spelling still parses normally, so `allow_abbrev=False` only removes
        the prefix-matching behavior rather than breaking the flag itself
        """
        parser = ArgumentParser(prog="test", exit_on_error=False)
        parser.add_argument("--status")
        args = parser.parse_args(["--status", "active"])
        assert args.status == "active"


class TestSuggestOnError:
    """Tests for `ArgumentParser`'s version-gated `suggest_on_error=True` default (Python 3.14+ only,
    where `argparse.ArgumentParser` first gained that parameter)
    """

    @pytest.mark.skipif(sys.version_info < (3, 14), reason="suggest_on_error requires Python 3.14+")
    def test_suggests_the_closest_choice_on_python_3_14_plus(self) -> None:
        """Test that a near-miss choice (e.g. a mistyped subcommand) gets a "maybe you meant" hint,
        rather than just the bare list of valid choices
        """
        parser = ArgumentParser(prog="test", exit_on_error=False)
        subparsers = parser.add_subparsers(dest="resource")
        subparsers.add_parser("products")
        subparsers.add_parser("users")
        with pytest.raises(argparse.ArgumentError) as exc_info:
            parser.parse_args(["prodcts"])
        assert "maybe you meant 'products'" in str(exc_info.value)

    @pytest.mark.skipif(sys.version_info >= (3, 14), reason="pins the pre-3.14 fallback behavior")
    def test_no_suggestion_before_python_3_14(self) -> None:
        """Test that a near-miss choice gets no suggestion before Python 3.14, where `suggest_on_error`
        doesn't exist as a parameter at all: the version guard must not pass it through unconditionally
        """
        parser = ArgumentParser(prog="test", exit_on_error=False)
        subparsers = parser.add_subparsers(dest="resource")
        subparsers.add_parser("products")
        subparsers.add_parser("users")
        with pytest.raises(argparse.ArgumentError) as exc_info:
            parser.parse_args(["prodcts"])
        assert "maybe you meant" not in str(exc_info.value)

    def test_an_explicit_suggest_on_error_is_not_overridden(self) -> None:
        """Test that an explicitly given `suggest_on_error` isn't silently overridden by the version-gated
        default, matching every other `kwargs.setdefault()` in `__init__`
        """
        kwargs = {"suggest_on_error": False} if sys.version_info >= (3, 14) else {}
        parser = ArgumentParser(prog="test", exit_on_error=False, **kwargs)
        if sys.version_info >= (3, 14):
            assert parser.suggest_on_error is False


class TestDefaultMetavar:
    """Tests for `default_metavar()`"""

    def test_strips_a_trailing_underscore(self) -> None:
        """Test that a single trailing underscore is stripped before upper-casing"""
        assert default_metavar("from_") == "FROM"

    def test_strips_a_leading_underscore(self) -> None:
        """Test that a single leading underscore is stripped before upper-casing"""
        assert default_metavar("_internal") == "INTERNAL"

    def test_keeps_an_internal_underscore(self) -> None:
        """Test that an internal underscore is preserved, matching argparse's own convention"""
        assert default_metavar("sort_by") == "SORT_BY"


class TestHelpFormatterDefaultMetavar:
    """Tests for `_HelpFormatter`'s default-metavar rendering through a real `ArgumentParser`"""

    def test_renders_an_edge_underscore_dest_without_its_underscore(self) -> None:
        """Test that a flag's own default metavar (no explicit `metavar=`) drops a leading/trailing
        underscore from `dest`, while an internal one is preserved
        """
        parser = ArgumentParser(prog="test", add_help=False)
        parser.add_argument("--from", dest="from_")
        parser.add_argument("--internal", dest="_internal")
        parser.add_argument("--sort-by", dest="sort_by")
        help_text = remove_color_code(parser.format_help())
        assert "--from FROM" in help_text
        assert "--internal INTERNAL" in help_text
        assert "--sort-by SORT_BY" in help_text

    def test_leaves_an_explicit_metavar_untouched(self) -> None:
        """Test that an explicit `metavar` is rendered as-is, not run through `default_metavar()`"""
        parser = ArgumentParser(prog="test", add_help=False)
        parser.add_argument("--status", dest="status", metavar="{ACTIVE,INACTIVE}")
        help_text = remove_color_code(parser.format_help())
        assert "--status {ACTIVE,INACTIVE}" in help_text


class TestFullMetavar:
    """Tests for `set_full_metavar()`/`full_metavar()`, and `_HelpFormatter`'s substitution of the full
    metavar they attach in place of an action's own (possibly elided) `metavar`, under `--help` only
    """

    def _build(self) -> tuple[ArgumentParser, argparse.Action]:
        parser = ArgumentParser(prog="test", add_help=False)
        action = parser.add_argument("--status", metavar="{ACTIVE,INACTIVE,…}")
        set_full_metavar(action, "{ACTIVE,INACTIVE,PENDING,ARCHIVED}")
        return parser, action

    def test_full_metavar_returns_none_before_its_set(self) -> None:
        """Test that `full_metavar()` returns `None` for an action no full metavar was ever attached to"""
        parser = ArgumentParser(prog="test", add_help=False)
        action = parser.add_argument("--status", metavar="{ACTIVE,INACTIVE}")
        assert full_metavar(action) is None

    def test_full_metavar_returns_what_was_set(self) -> None:
        """Test that `full_metavar()` returns exactly what `set_full_metavar()` attached"""
        _, action = self._build()
        assert full_metavar(action) == "{ACTIVE,INACTIVE,PENDING,ARCHIVED}"

    def test_short_help_keeps_the_actions_own_metavar(self) -> None:
        """Test that `-h` renders an action's own `metavar` unchanged, even once a full companion is set"""
        parser, _ = self._build()
        short_help = remove_color_code(parser.format_help(short=True))
        assert "--status {ACTIVE,INACTIVE,…}" in short_help
        assert "PENDING" not in short_help

    def test_full_help_substitutes_the_full_metavar(self) -> None:
        """Test that `--help` renders the full companion metavar in place of the action's own elided one"""
        parser, _ = self._build()
        full_help = remove_color_code(parser.format_help())
        assert "--status {ACTIVE,INACTIVE,PENDING,ARCHIVED}" in full_help
        assert "{ACTIVE,INACTIVE,…}" not in full_help

    def test_an_action_with_no_full_metavar_is_unaffected(self) -> None:
        """Test that an action carrying no full-metavar companion renders its own `metavar` identically
        under both `-h` and `--help`
        """
        parser = ArgumentParser(prog="test", add_help=False)
        parser.add_argument("--status", metavar="{ACTIVE,INACTIVE}")
        short_help = remove_color_code(parser.format_help(short=True))
        full_help = remove_color_code(parser.format_help())
        assert "--status {ACTIVE,INACTIVE}" in short_help
        assert "--status {ACTIVE,INACTIVE}" in full_help

    def test_full_help_does_not_permanently_overwrite_the_actions_own_metavar(self) -> None:
        """Test that rendering `--help` restores the action's own (short) `metavar` afterward, rather than
        permanently overwriting it with the full companion
        """
        parser, action = self._build()
        parser.format_help()
        assert action.metavar == "{ACTIVE,INACTIVE,…}"


class TestShortHelp:
    """Tests for `ArgumentParser.format_help(short=True)`"""

    def test_a_multi_line_help_string_is_reduced_to_its_first_line(self) -> None:
        """Test that `short=True` drops every unindented line after the first from a flag's own `help=`
        text (the `summary\\ndetail...` convention), while `short=False` renders it in full. An indented
        continuation line is a different case, covered by `TestShortHelp`'s own clamp tests below
        """
        parser = ArgumentParser(prog="test", add_help=False)
        parser.add_argument("--foo", help="Summary line.\nDetail line here.")

        short_help = remove_color_code(parser.format_help(short=True))
        full_help = remove_color_code(parser.format_help())

        assert "Summary line" in short_help
        assert "Detail line here." not in short_help
        assert "Summary line." in full_help
        assert "Detail line here." in full_help

    def test_a_short_indented_continuation_line_survives_in_full(self) -> None:
        """Test that an indented continuation line (e.g. `params.py`'s own `:param` description) that
        already fits on one rendered line survives `short=True` unclamped, unlike an unindented detail
        line, which is dropped outright
        """
        parser = ArgumentParser(prog="test", add_help=False)
        parser.add_argument("--foo", help="Summary.\n  A short detail.")

        short_help = remove_color_code(parser.format_help(short=True))

        assert "A short detail." in short_help

    def test_a_long_indented_continuation_line_is_clamped_to_one_line_with_an_ellipsis(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that an indented continuation line too long to fit on one rendered line is clamped to its
        own first line plus a trailing ellipsis under `short=True`, rather than dropped outright or wrapped
        across several lines the way `short=False` renders it
        """
        monkeypatch.setenv("COLUMNS", "40")
        parser = ArgumentParser(prog="test", add_help=False)
        parser.add_argument("--foo", help="Summary.\n  One two three four five six seven eight nine ten eleven twelve")

        short_help = remove_color_code(parser.format_help(short=True))
        full_help = remove_color_code(parser.format_help())

        short_lines = [line for line in short_help.splitlines() if line.strip().startswith("One two")]
        full_lines = [line for line in full_help.splitlines() if "One two" in line or "eleven twelve" in line]

        assert len(short_lines) == 1
        assert short_lines[0].rstrip().endswith(ELLIPSIS)
        assert len(full_lines) > 1  # the same text wraps across several lines under --help

    def test_a_clamped_line_never_exceeds_the_render_width_even_with_an_unbreakable_word(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that clamping an indented continuation line never pushes the result past the render
        width, even when the line's last surviving word is too long for `wrap()` to break
        (`break_long_words=False`) and would otherwise overflow once the trailing ellipsis is appended
        """
        monkeypatch.setenv("COLUMNS", "40")
        parser = ArgumentParser(prog="test", add_help=False)
        parser.add_argument(
            "--foo",
            help="Summary.\n  https://example.com/a/very/long/url/that/cannot/be/broken and then more words here",
        )

        short_help = remove_color_code(parser.format_help(short=True))

        assert all(len(line) <= get_terminal_width() for line in short_help.splitlines())

    def test_an_indented_line_after_an_unindented_detail_line_is_dropped_with_it(self) -> None:
        """Test that an indented line coming after an unindented `summary\\ndetail...` line is dropped
        along with it under `short=True`, rather than resurfacing once the unindented run ends
        """
        parser = ArgumentParser(prog="test", add_help=False)
        parser.add_argument("--foo", help="Summary.\nDetail line.\n  Indented line.")

        short_help = remove_color_code(parser.format_help(short=True))

        assert "Detail line." not in short_help
        assert "Indented line." not in short_help

    def test_a_single_line_help_string_is_unaffected(self) -> None:
        """Test that `short=True` renders a flag whose `help=` text has no second line identically to
        `short=False`, aside from the short-help tip appended to `short=True`'s own epilog
        """
        parser = ArgumentParser(prog="test", add_help=False)
        parser.add_argument("--foo", help="Only line")
        short_help = remove_color_code(parser.format_help(short=True))
        full_help = remove_color_code(parser.format_help())
        assert short_help == f"{full_help.rstrip(chr(10))}\n\ntip: {_SHORT_HELP_TIP}\n"

    def test_group_and_parser_description_text_are_unaffected(self) -> None:
        """Test that `short=True` only shortens a flag's own `help=` text, leaving a plain-`str` argument
        group's `description=` and the parser's own `description=`/`epilog=` intact. A `CollapsibleText`
        group's own `description=` is the one exception, covered by `TestCollapsibleGroups` below
        """
        parser = ArgumentParser(prog="test", add_help=False, description="parser description", epilog="an epilog")
        group = parser.add_argument_group(title="things", description="group description")
        group.add_argument("--foo", help="Summary line.\nDetail line here.")

        help_text = remove_color_code(parser.format_help(short=True))

        assert "parser description" in help_text
        assert "an epilog" in help_text
        assert "group description" in help_text

    def test_short_help_appends_a_tip_pointing_to_help(self) -> None:
        """Test that `short=True` appends a tip pointing to `--help` after the parser's own epilog, and
        that `short=False` renders no such tip
        """
        parser = ArgumentParser(prog="test", add_help=False, epilog="an epilog")
        parser.add_argument("--foo", help="Only line.")

        short_help = remove_color_code(parser.format_help(short=True))
        full_help = remove_color_code(parser.format_help())

        assert short_help.endswith(f"an epilog\n\ntip: {_SHORT_HELP_TIP}\n")
        assert _SHORT_HELP_TIP not in full_help

    def test_short_help_tip_is_yellow(self, force_color: None) -> None:
        """Test that the short-help tip is colored yellow, distinct from the epilog's own default coloring"""
        parser = ArgumentParser(prog="test", add_help=False)
        parser.add_argument("--foo", help="Only line.")

        help_text = parser.format_help(short=True)

        assert color(f"tip: {_SHORT_HELP_TIP}", color_code=ColorCodes.YELLOW) in help_text

    def test_short_render_does_not_mutate_the_parser(self) -> None:
        """Test that rendering short help restores `formatter_class`, and a subsequent full-help
        render is unaffected
        """
        parser = ArgumentParser(prog="test", add_help=False)
        parser.add_argument("--foo", help="Summary line.\nDetail line here.")
        formatter_class_before = parser.formatter_class
        full_help_before = parser.format_help()

        parser.format_help(short=True)

        assert parser.formatter_class is formatter_class_before
        assert parser.format_help() == full_help_before


class TestHelpTextIndentPreservedOnWrap:
    """Tests for `_HelpFormatter._split_lines()`'s own indent preservation under `--help`: a `help=`
    paragraph's leading whitespace (e.g. `params.py`'s indented `:param` description, on its own line
    beneath a flag's marker row) is kept on every wrapped continuation line, not just its first. `-h`'s own
    handling of the same indented paragraph (clamped to one line instead of wrapped) is covered by
    `TestShortHelp` above.
    """

    def test_a_wrapped_indented_paragraph_keeps_its_indent_on_every_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that an indented second line of a flag's own `help=` text, long enough to wrap, keeps its
        indentation on every wrapped line rather than only the first
        """
        monkeypatch.setenv("COLUMNS", "40")
        parser = ArgumentParser(prog="test", add_help=False)
        parser.add_argument("--foo", help="Summary.\n  One two three four five six seven eight nine ten eleven twelve")

        help_text = remove_color_code(parser.format_help())
        # Every line after the marker line ("--foo FOO  Summary.") belongs to the wrapped, originally
        # 2-space-indented paragraph.
        marker_index = next(i for i, line in enumerate(help_text.splitlines()) if "Summary." in line)
        wrapped_lines = help_text.splitlines()[marker_index + 1 :]

        assert len(wrapped_lines) > 1  # actually wrapped onto more than one line
        assert all(line.startswith("               ") for line in wrapped_lines)

    def test_a_wrapped_line_never_exceeds_the_render_width_even_with_a_deep_indent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that a `help=` paragraph indented at or past the wrap width doesn't push a wrapped
        continuation line past the render width. `wrap()` forces at least one character through per line
        once `subsequent_indent` is as wide as the wrap width itself, which would otherwise widen every
        wrapped line past the terminal
        """
        monkeypatch.setenv("COLUMNS", "40")
        parser = ArgumentParser(prog="test", add_help=False)
        parser.add_argument("--foo", help="Summary.\n" + " " * 60 + "one two three four five six seven")

        help_text = remove_color_code(parser.format_help())

        assert all(len(line) <= get_terminal_width() for line in help_text.splitlines())


class TestCollapsibleGroups:
    """Tests for `CollapsibleText`: an argument group `description=` that collapses under `-h`"""

    @staticmethod
    def _build_parser() -> ArgumentParser:
        parser = ArgumentParser(prog="test", add_help=False)
        group = parser.add_argument_group(
            title="things", description=CollapsibleText("Full group description.", short="Short note.")
        )
        group.add_argument("--foo", help="Foo help.")
        mutex = group.add_mutually_exclusive_group()
        mutex.add_argument("--bar", help="Bar help.")
        mutex.add_argument("--baz", help="Baz help.")
        return parser

    def test_short_help_renders_the_short_note_and_drops_the_groups_flags(self) -> None:
        """Test that `short=True` replaces a `CollapsibleText` group's description and every one of its
        flags, including one in a mutually exclusive group nested on it, with the short note, while the
        group's own heading still renders
        """
        help_text = remove_color_code(self._build_parser().format_help(short=True))

        assert "things:" in help_text
        assert "Short note." in help_text
        assert "Full group description." not in help_text
        # The flag names themselves still appear in the auto-generated usage line (this parser doesn't override it the
        # way the real CLI's leaf commands do), so absence is checked via each flag's own help text instead, which only
        # renders as part of the group's (now-dropped) argument listing.
        for help_string in ("Foo help.", "Bar help.", "Baz help."):
            assert help_string not in help_text

    def test_full_help_renders_the_full_description_and_every_flag(self) -> None:
        """Test that `short=False` renders a `CollapsibleText` group exactly like a plain `str`
        description would: the full text, followed by every one of its flags
        """
        help_text = remove_color_code(self._build_parser().format_help())

        assert "Full group description." in help_text
        assert "Short note." not in help_text
        for flag in ("--foo", "--bar", "--baz"):
            assert flag in help_text

    def test_full_helps_long_description_wraps_to_the_terminal_width(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that a `CollapsibleText` group's full description, unlike a plain `str` description (a
        pre-formatted `box_text()` block, left raw on purpose), wraps onto continuation lines that fit the
        terminal width instead of overflowing it
        """
        monkeypatch.setenv("COLUMNS", "40")
        parser = ArgumentParser(prog="test", add_help=False)
        parser.add_argument_group(title="things", description=CollapsibleText("word " * 20, short="Short note."))

        lines = remove_color_code(parser.format_help()).splitlines()

        assert len(lines) > 1
        assert all(len(line) <= get_terminal_width() - 2 for line in lines)

    def test_short_note_is_uncolored_while_a_plain_description_stays_dark_grey(self, force_color: None) -> None:
        """Test that a `CollapsibleText` group's short note renders in the terminal's default color,
        unlike a plain group `description=`, which is still colored dark grey. The group heading itself
        is unaffected, staying blue and bold either way
        """
        parser = self._build_parser()
        plain_group = parser.add_argument_group(title="plain", description="Plain description.")
        plain_group.add_argument("--qux", help="Qux help.")

        help_text = parser.format_help(short=True)

        assert "Short note." in help_text
        assert color("Short note.", color_code=ColorCodes.DARK_GREY) not in help_text
        assert color("Plain description.", color_code=ColorCodes.DARK_GREY) in help_text
        assert color("things", color_code=ColorCodes.BLUE, bold=True) in help_text

    def test_a_group_after_a_collapsed_group_still_renders_its_flags(self) -> None:
        """Test that collapsing one group under `-h` doesn't leak into the next group, guarding
        `_HelpFormatter.end_section()`'s reset of the collapsed-section state
        """
        parser = self._build_parser()
        after_group = parser.add_argument_group(title="after")
        after_group.add_argument("--quux", help="Quux help.")

        help_text = remove_color_code(parser.format_help(short=True))

        assert "--quux" in help_text

    def test_short_help_narrows_the_help_column_once_a_collapsed_groups_flags_no_longer_widen_it(self) -> None:
        """Test that `-h` no longer reserves help-column space for a `CollapsibleText` group's own
        longest flag invocation, once that group's flags are dropped from the render. This is an
        accepted side effect of computing the shared help column from only the flags actually rendered,
        pinned here so it stays an intentional, visible choice rather than a silent surprise
        """
        parser = ArgumentParser(prog="test", add_help=False)
        wide_group = parser.add_argument_group(title="things", description=CollapsibleText("Full.", short="Short."))
        wide_group.add_argument("--a-very-long-flag-name", help="Long flag.")
        other_group = parser.add_argument_group(title="other")
        other_group.add_argument("--x", help="X help.")

        full_line = next(
            line for line in remove_color_code(parser.format_help()).splitlines() if line.strip().startswith("--x")
        )
        short_line = next(
            line
            for line in remove_color_code(parser.format_help(short=True)).splitlines()
            if line.strip().startswith("--x")
        )

        # short=True's own summary-only truncation drops "X help."'s trailing period (see TestShortHelp), so the two
        # renders are compared on the shared "X help" substring.
        assert short_line.index("X help") < full_line.index("X help")


class TestNotes:
    """Tests for `ArgumentParser`'s `tips=`/`warnings=` rendering"""

    def test_a_single_tip_renders_inline_with_no_header(self) -> None:
        """Test that a lone tip renders as `tip: <text>`, with no `tips:` header"""
        parser = ArgumentParser(prog="test", add_help=False, tips=["Do the thing"])
        help_text = remove_color_code(parser.format_help())
        assert "tip: Do the thing" in help_text
        assert "tips:" not in help_text

    def test_multiple_tips_render_as_a_bulleted_list(self) -> None:
        """Test that two or more tips render under a `tips:` header, one bullet per tip"""
        parser = ArgumentParser(prog="test", add_help=False, tips=["First tip", "Second tip"])
        help_text = remove_color_code(parser.format_help())
        assert "tips:\n  - First tip\n  - Second tip" in help_text

    def test_a_single_warning_renders_inline_with_no_header(self) -> None:
        """Test that a lone warning renders as `warning: <text>`, with no `warnings:` header"""
        parser = ArgumentParser(prog="test", add_help=False, warnings=["Something's off"])
        help_text = remove_color_code(parser.format_help())
        assert "warning: Something's off" in help_text
        assert "warnings:" not in help_text

    def test_multiple_warnings_render_as_a_bulleted_list(self) -> None:
        """Test that two or more warnings render under a `warnings:` header, one bullet per warning"""
        parser = ArgumentParser(prog="test", add_help=False, warnings=["First warning", "Second warning"])
        help_text = remove_color_code(parser.format_help())
        assert "warnings:\n  - First warning\n  - Second warning" in help_text

    def test_warnings_render_before_tips(self) -> None:
        """Test that warnings and tips together render warnings first, separated by one blank line"""
        parser = ArgumentParser(prog="test", add_help=False, warnings=["A warning"], tips=["A tip"])
        help_text = remove_color_code(parser.format_help())
        assert "warning: A warning\n\ntip: A tip" in help_text

    def test_a_long_item_wraps_to_the_terminal_width(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that a note longer than the terminal width wraps onto continuation lines that fit
        within the same width, rather than argparse's own raw epilog rendering, which never wraps
        """
        monkeypatch.setenv("COLUMNS", "40")
        parser = ArgumentParser(prog="test", add_help=False, tips=["word " * 20])
        lines = remove_color_code(parser.format_help()).splitlines()
        tip_lines = [line for line in lines if line.startswith(("tip:", " " * 5))]
        assert len(tip_lines) > 1
        assert all(len(line) <= get_terminal_width() - 2 for line in tip_lines)
        # Continuation lines align exactly under "tip: "'s own width, no more and no less
        assert all(line.startswith(" " * 5) and not line.startswith(" " * 6) for line in tip_lines[1:])

    def test_a_very_narrow_terminal_does_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that a terminal too narrow to leave any wrap width still renders, rather than
        `textwrap.wrap()` raising `ValueError` for a non-positive width
        """
        monkeypatch.setenv("COLUMNS", "1")
        parser = ArgumentParser(prog="test", add_help=False, tips=["A tip"], warnings=["A warning"])
        assert "tip: A tip" in remove_color_code(parser.format_help())

    def test_a_hyphenated_token_is_never_split_across_lines(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that wrapping a note never breaks a hyphenated word (or a long flag) mid-token, even
        when it would otherwise overflow the wrap width
        """
        monkeypatch.setenv("COLUMNS", "30")
        parser = ArgumentParser(prog="test", add_help=False, tips=["Install the 'cli-completion' extra"])
        help_text = remove_color_code(parser.format_help())
        assert "'cli-completion'" in help_text

    def test_a_nested_detail_line_wraps_under_its_own_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that a nested `  - ` detail line long enough to wrap keeps its own continuation two
        columns further indented than its bullet, rather than falling back to the bullet's own indent
        """
        monkeypatch.setenv("COLUMNS", "50")
        item = "Some failures occurred:\n  - " + "word " * 10
        parser = ArgumentParser(prog="test", add_help=False, warnings=[item, "Another warning"])
        lines = remove_color_code(parser.format_help()).splitlines()
        bullet_line = next(line for line in lines if "- word" in line)
        continuation_lines = [line for line in lines if line.lstrip().startswith("word")]
        assert continuation_lines
        bullet_indent = len(bullet_line) - len(bullet_line.lstrip())
        assert all(line.startswith(" " * (bullet_indent + 2)) for line in continuation_lines)

    def test_a_blank_item_is_ignored(self) -> None:
        """Test that a blank tip is dropped rather than counting toward the inline-vs-bulleted decision
        or rendering as a phantom blank line
        """
        parser = ArgumentParser(prog="test", add_help=False, tips=["A tip", ""])
        help_text = remove_color_code(parser.format_help())
        assert "tip: A tip" in help_text
        assert "tips:" not in help_text

    def test_nested_detail_lines_stay_indented_under_their_parent_bullet(self) -> None:
        """Test that a note item containing its own `  - ` detail lines keeps them indented relative
        to the bullet they belong to, once rendered inside a multi-item bulleted list
        """
        item = "Some failures occurred:\n  - first detail\n  - second detail"
        parser = ArgumentParser(prog="test", add_help=False, warnings=[item, "Another warning"])
        help_text = remove_color_code(parser.format_help())
        assert "  - Some failures occurred:\n      - first detail\n      - second detail" in help_text

    def test_notes_are_colored_yellow(self, force_color: None) -> None:
        """Test that the whole warnings-and-tips block renders as a single yellow span"""
        parser = ArgumentParser(prog="test", add_help=False, warnings=["A warning"], tips=["A tip"])
        help_text = parser.format_help()
        assert color("warning: A warning\n\ntip: A tip", color_code=ColorCodes.YELLOW) in help_text

    def test_no_tips_or_warnings_leaves_the_epilog_untouched(self) -> None:
        """Test that a parser given neither `tips=` nor `warnings=` renders exactly its own `epilog=`,
        with no added block
        """
        parser = ArgumentParser(prog="test", add_help=False, epilog="an epilog")
        help_text = remove_color_code(parser.format_help())
        assert help_text.endswith("an epilog\n")

    def test_notes_render_after_an_explicit_epilog(self) -> None:
        """Test that a parser given both `epilog=` and notes renders the epilog ahead of the notes block"""
        parser = ArgumentParser(prog="test", add_help=False, epilog="an epilog", tips=["A tip"])
        help_text = remove_color_code(parser.format_help())
        assert "an epilog\n\ntip: A tip" in help_text
        assert parser.epilog == "an epilog"

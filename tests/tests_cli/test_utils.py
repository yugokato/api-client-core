"""Unit tests for `api_client_core.cli.utils`"""

from __future__ import annotations

from api_client_core.cli.utils import _MIN_BOX_WIDTH, box_text


class TestBoxText:
    """Tests for `box_text()`"""

    def test_every_rendered_line_is_exactly_the_given_width(self) -> None:
        """Test that the border and every body line, including a wrapped one, render at exactly `width`"""
        result = box_text("Title:\n  A line long enough that it wraps at least once given the width below", width=30)
        lines = result.splitlines()
        assert len(lines) > 3  # more than just the two border lines and one body line
        assert len({len(line) for line in lines}) == 1
        assert len(lines[0]) == 30

    def test_a_wrapped_paragraphs_own_indent_is_kept_on_every_continuation_line(self) -> None:
        """Test that a paragraph's own leading whitespace is kept on every wrapped continuation line, not
        just its first. `wrap()`'s own default drops it past the first line, which would otherwise render
        an indented block (e.g. a nested example) flush against the box's left edge once it wraps, instead
        of staying indented under the line above it
        """
        text = "Title\n\n  one two three four five six seven eight nine ten"
        result = box_text(text, width=20)
        body = [line[2:-2] for line in result.splitlines()[1:-1]]
        assert body[0].rstrip() == "Title"
        wrapped_lines = [line for line in body[2:] if line.strip()]
        assert len(wrapped_lines) > 1  # actually wrapped onto more than one line
        assert all(line.startswith("  ") for line in wrapped_lines)

    def test_the_box_stays_a_rectangle_even_when_a_paragraphs_own_indent_exceeds_the_wrap_width(self) -> None:
        """Test that a paragraph indented at or past the box's own inner width doesn't push a wrapped
        continuation line past the box's right border. `wrap()` forces at least one character through per
        line once `subsequent_indent` is as wide as the wrap width itself, which would otherwise widen
        every line past `width` and break the box's rectangle shape, right when `_MIN_BOX_WIDTH` most needs
        it not to
        """
        text = "Title\n" + " " * 30 + "one two three four five"
        result = box_text(text, width=_MIN_BOX_WIDTH)
        lines = result.splitlines()
        assert len({len(line) for line in lines}) == 1
        assert len(lines[0]) == _MIN_BOX_WIDTH

"""Unit tests for `api_client_core._common.docstring`"""

from __future__ import annotations

from api_client_core._common.docstring import first_doc_line, split_param_docs


class TestSplitParamDocs:
    """Tests for `split_param_docs()`, which splits an endpoint function's own docstring into its prose
    (everything but `:param` entries) and a dict of `:param <name>: <description>` entries.
    """

    def test_returns_empty_prose_and_dict_for_no_docstring(self) -> None:
        """Test that a missing docstring splits to an empty prose string and an empty dict, rather than
        raising
        """
        assert split_param_docs(None) == ("", {})

    def test_returns_empty_prose_and_dict_for_a_blank_docstring(self) -> None:
        """Test that a whitespace-only docstring splits the same as a missing one, rather than boxing a
        dangling title with nothing under it
        """
        assert split_param_docs("   \n  ") == ("", {})

    def test_a_docstring_with_no_param_entries_is_all_prose(self) -> None:
        """Test that a docstring with a summary but no `:param` lines splits to that summary as prose and
        an empty dict
        """
        assert split_param_docs("Just a summary, no params documented.") == (
            "Just a summary, no params documented.",
            {},
        )

    def test_parses_a_single_line_entry(self) -> None:
        """Test that one `:param name: description` line parses to `{"name": "description"}`, and the
        summary above it is returned as prose
        """
        doc = "Summary.\n\n:param name: The thing's own display name\n"
        assert split_param_docs(doc) == ("Summary.", {"name": "The thing's own display name"})

    def test_a_docstring_with_only_param_entries_has_no_prose(self) -> None:
        """Test that a docstring documenting only parameters, with no summary or other prose, splits to an
        empty prose string rather than a dangling blank line
        """
        assert split_param_docs(":param name: The thing's own display name\n") == (
            "",
            {"name": "The thing's own display name"},
        )

    def test_joins_a_continuation_line_with_a_single_space(self) -> None:
        """Test that a description wrapped onto an indented continuation line is joined back into one
        line with a single space, matching this project's own multi-line `:param` convention
        """
        doc = (
            "Summary.\n\n"
            ":param note: An optional free-form note attached to the thing, wrapped here onto a\n"
            "            second line\n"
        )
        assert split_param_docs(doc) == (
            "Summary.",
            {"note": "An optional free-form note attached to the thing, wrapped here onto a second line"},
        )

    def test_joins_an_unindented_continuation_line_too(self) -> None:
        """Test that a continuation line is joined into its entry's description even when it carries no
        indentation of its own, matching `:param` parsing's own indentation-agnostic rule
        """
        doc = "Summary.\n\n:param note: A note that\ncontinues here, unindented\n"
        assert split_param_docs(doc) == ("Summary.", {"note": "A note that continues here, unindented"})

    def test_a_blank_line_ends_the_current_entry_without_leaving_it_in_prose(self) -> None:
        """Test that a blank line after a `:param` entry ends it and is itself consumed, so a following
        paragraph (e.g. a docstring's closing prose) isn't absorbed as more of its description, and the
        entry's own closing blank line doesn't leave a stray gap in the returned prose
        """
        doc = "Summary.\n\n:param name: A name\n\nSome trailing prose, unrelated to any parameter.\n"
        assert split_param_docs(doc) == (
            "Summary.\n\nSome trailing prose, unrelated to any parameter.",
            {"name": "A name"},
        )

    def test_multiple_blank_lines_after_an_entry_collapse_to_one_in_prose(self) -> None:
        """Test that several consecutive blank lines following a `:param` entry - one consumed by the
        entry itself, the rest genuinely part of the docstring's own prose - still collapse to a single
        blank line in the returned prose, rather than stacking one blank line per source line
        """
        doc = "Summary.\n\n:param name: A name\n\n\n\nFar trailing prose.\n"
        assert split_param_docs(doc) == ("Summary.\n\nFar trailing prose.", {"name": "A name"})

    def test_a_following_field_marker_ends_the_current_entry_and_is_kept_as_prose(self) -> None:
        """Test that a following `:field:` marker (e.g. a second `:param`) ends the current entry rather
        than being absorbed as a continuation line of it, and that a marker this module doesn't recognize
        (e.g. `:return:`) is kept as prose instead of being silently dropped
        """
        doc = "Summary.\n\n:param a: First\n:param b: Second\n"
        assert split_param_docs(doc) == ("Summary.", {"a": "First", "b": "Second"})

        doc_with_return = "Summary.\n\n:param a: First\n:return: something\n"
        assert split_param_docs(doc_with_return) == ("Summary.\n\n:return: something", {"a": "First"})

    def test_normalizes_indentation_the_same_as_a_313_plus_compiled_docstring(self) -> None:
        """Test that a docstring carrying its raw source indentation (as every docstring does on Python
        versions before 3.13, which only strips it at compile time from there on) is normalized the same
        way on every supported version, so a continuation line's relative indentation is read correctly
        regardless of how deep the enclosing function body sits
        """
        doc = "Make a thing\n\n        :param name: A name that\n            continues here\n        "
        assert split_param_docs(doc) == ("Make a thing", {"name": "A name that continues here"})


class TestFirstDocLine:
    """Tests for `first_doc_line()`, which returns the first non-blank line of a docstring"""

    def test_returns_none_for_no_docstring(self) -> None:
        """Test that a missing docstring returns None"""
        assert first_doc_line(None) is None

    def test_returns_none_for_a_blank_docstring(self) -> None:
        """Test that a whitespace-only docstring returns None"""
        assert first_doc_line("   \n  ") is None

    def test_returns_the_single_line_of_a_one_line_docstring(self) -> None:
        """Test that a one-line docstring returns itself, stripped"""
        assert first_doc_line("  Just a summary.  ") == "Just a summary."

    def test_returns_only_the_first_line_of_a_multi_line_docstring(self) -> None:
        """Test that a multi-line docstring returns only its first line, not the rest"""
        assert first_doc_line("Summary.\n\nMore detail below.") == "Summary."

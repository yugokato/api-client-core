"""Unit tests for core endpoints/utils/param_type.py"""

import inspect
import typing
from collections.abc import Sequence
from typing import Annotated, Any, ForwardRef, Literal, TypeVar

import pytest

import api_client_core.core.endpoints.utils.param_type as param_type_util
from api_client_core.types import Alias, Query

_T = TypeVar("_T")
_OptionalAlias = _T | None


class _DictModel(dict[str, Any]):
    """Local dict subclass used as a stand-in for ParamModel in `matches_type` tests."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(kwargs)


class TestIsTypeOf:
    """Tests for param_type_util.is_type_of()"""

    @pytest.mark.parametrize(
        ("param_type", "type_to_check", "is_type_of"),
        [
            (str, str, True),
            (str | None, str, True),
            (Annotated[str, "meta"], str, True),
            (Annotated[str, "meta"] | None, str, True),
            (int, int, True),
            (int | None, int, True),
            (Annotated[int, "meta"], int, True),
            (Annotated[int, "meta"] | None, int, True),
            (bool, bool, True),
            (bool | None, bool, True),
            (Annotated[bool, "meta"], bool, True),
            (Annotated[bool, "meta"] | None, bool, True),
            (list, list, True),
            (list[str], list, True),
            (list[str] | None, list, True),
            (Annotated[list[str], "meta"], list, True),
            (Annotated[list[str], "meta"] | None, list, True),
            (str, int, False),
            (list[str], str, False),
            (Annotated[list[str], "meta"], str, False),
            (Annotated[list[str], "meta"] | None, str, False),
            (Annotated[Literal[1], "meta"] | None, _OptionalAlias, True),
            (Annotated[Literal[1], "meta"] | None, Annotated, True),
            (Annotated[Literal[1], "meta"] | None, Literal, True),
        ],
    )
    def test_is_type_of(self, param_type: Any, type_to_check: Any, is_type_of: bool) -> None:
        """Test that we can check if a specific Python type falls into the given Python type"""
        assert param_type_util.is_type_of(param_type, type_to_check) is is_type_of


class TestIsDeprecatedParam:
    """Tests for param_type_util.is_deprecated_param()"""

    @pytest.mark.parametrize(
        ("tp", "is_deprecated_param"),
        [
            (str, False),
            (int | Annotated[str, "meta"], False),
            (Annotated[str, "meta"], False),
            (Annotated[str, "meta", "deprecated"], True),
            (int | Annotated[str, "meta", "deprecated"], True),
            (Annotated[str, "meta", "deprecated"] | None, True),
            (int | Annotated[str, "meta", "deprecated"] | None, True),
            (int | None | Annotated[str, "meta", "deprecated"], True),
        ],
    )
    def test_is_deprecated_param(self, tp: Any, is_deprecated_param: bool) -> None:
        """Test that we can check whether a given type annotation has `Annotated[]` with "deprecated" in the metadata"""
        assert param_type_util.is_deprecated_param(tp) is is_deprecated_param


class TestIsQueryParam:
    """Tests for param_type_util.is_query_param()"""

    @pytest.mark.parametrize(
        ("tp", "is_query"),
        [
            # Recognized query-param forms
            (Annotated[str, Query()], True),  # canonical instance
            (Annotated[str, Query], True),  # bare class shortcut
            (Annotated[str, "query"], True),  # legacy string
            # Union arms — any arm that is a query param makes the whole annotation one
            (int | Annotated[str, Query()], True),
            (Annotated[str, Query()] | None, True),
            # Non-query annotations
            (str, False),  # not Annotated
            (int | str, False),  # plain union, no Annotated
            (Annotated[str, "meta"], False),  # wrong metadata value
            (Annotated[str, Alias("x")], False),  # Alias metadata, not Query
            (Annotated[str, "deprecated"], False),  # unrelated metadata
        ],
    )
    def test_is_query_param(self, tp: Any, is_query: bool) -> None:
        """Test that the three recognized query-param annotation forms are detected correctly"""
        assert param_type_util.is_query_param(tp) is is_query


class TestAnnotateType:
    """Tests for param_type_util.annotate_type()"""

    @pytest.mark.parametrize(
        ("tp", "metadata", "expected_type"),
        [
            (str, ["meta"], Annotated[str, "meta"]),
            (
                str,
                ["meta", Alias("foo"), Query(), "extra"],
                Annotated[str, "meta", Alias("foo"), Query(), "extra"],
            ),
            (str | int, ["meta"], Annotated[str | int, "meta"]),
            # Nullable types: the None arm is preserved and the inner type is annotated
            (str | None, ["meta"], Annotated[str, "meta"] | None),
            (_DictModel | None, ["meta"], Annotated[_DictModel, "meta"] | None),
            (
                ForwardRef("_DictModel") | None,
                ["meta"],
                Annotated[ForwardRef("_DictModel"), "meta"] | None,
            ),
            (str | int | None, ["meta"], Annotated[str | int, "meta"] | None),
        ],
    )
    def test_annotate_type(self, tp: Any, metadata: list[Any], expected_type: Any) -> None:
        """Test that a type annotation can be converted to an annotated type with metadata"""
        assert param_type_util.annotate_type(tp, *metadata) == expected_type


class TestGetAnnotatedType:
    """Tests for param_type_util.get_annotated_type()"""

    @pytest.mark.parametrize(
        ("tp", "annotated_type"),
        [
            (str, None),
            (str | None, None),
            (str | int, None),
            (list[str], None),
            (Annotated[str, "meta"], Annotated[str, "meta"]),
            (int | Annotated[str, "meta"], Annotated[str, "meta"]),
            (Annotated[str, "meta"] | int, Annotated[str, "meta"]),
            (Annotated[str, "meta"] | None, Annotated[str, "meta"]),
            (int | None | Annotated[str, "meta"], Annotated[str, "meta"]),
            (int | None | Annotated[str, "meta"], Annotated[str, "meta"]),
            (int | (Annotated[str, "meta"] | None), Annotated[str, "meta"]),
            (
                Annotated[str, "meta1"] | Annotated[int, "meta2"],
                (Annotated[str, "meta1"], Annotated[int, "meta2"]),
            ),
            (
                Annotated[str, Alias("x")] | Annotated[int, Query()],
                (Annotated[str, Alias("x")], Annotated[int, Query()]),
            ),
        ],
    )
    def test_get_annotated_type(self, tp: Any, annotated_type: Any) -> None:
        """Test that an annotated type that may/may not be nested inside the given type can be retrieved"""
        assert param_type_util.get_annotated_type(tp) == annotated_type

    @pytest.mark.parametrize(
        ("tp", "metadata_filter", "expected"),
        [
            # First filter matches
            (Annotated[str, Alias("x")], [Alias], Annotated[str, Alias("x")]),
            # Second filter matches (regression: early-return bug in the filter loop)
            (Annotated[str, Alias("x")], ["nonexistent", Alias], Annotated[str, Alias("x")]),
            # String filter matches
            (Annotated[str, "tag"], "tag", Annotated[str, "tag"]),
            # No filter matches
            (Annotated[str, Alias("x")], [Query, "nonexistent"], None),
            # Type not annotated at all
            (str, [Alias], None),
        ],
    )
    def test_get_annotated_type_with_metadata_filter(self, tp: Any, metadata_filter: list[Any], expected: Any) -> None:
        """Test that get_annotated_type correctly filters annotated types by metadata"""
        assert param_type_util.get_annotated_type(tp, metadata_filter=metadata_filter) == expected


class TestMatchesType:
    """Tests for param_type_util.matches_type()"""

    @pytest.mark.parametrize(
        ("value", "tp", "is_valid"),
        [
            # None value
            (None, int | None, True),
            # Any
            (1, Any, True),
            ([1, 2], Any, True),
            # int
            (1, int, True),
            (1, int | str, True),
            (1, int | None, True),
            (1, int | str | None, True),
            (1, int | None, True),
            (1, Annotated[int, "meta"] | None, True),
            (1, Annotated[int | str, "meta"] | None, True),
            # Literal
            (1, Literal[1, 2], True),
            # list
            ([1, 2], list[int], True),
            ([1, "2"], list[int | str], True),
            # dict
            ({}, dict[str, Any], True),
            ({"k": "v"}, dict[str, str], True),
            ({"k": "v"}, dict[str | int, str], True),
            # dict subclass
            (_DictModel(), _DictModel, True),
            (_DictModel(), dict, True),
            (_DictModel(param1="foo"), dict[str, str], True),
            (_DictModel(), dict | _DictModel, True),
            ({"k": "v"}, _DictModel | dict[str, Any], True),
            # invalid
            (None, int, False),
            (1, str, False),
            (1, str | bool, False),
            (1, str | None, False),
            (1, str | bool | None, False),
            (1, Annotated[str, "meta"] | None, False),
            (1, Annotated[str | bool, "meta"] | None, False),
            (1, Literal[2, 3], False),
            ([1, 2], int, False),
            ([1, 2], list[str], False),
            ([1, "2"], list[str], False),
            ([1, 2], list[str | bool], False),
            ({"k": 1}, dict[str, str], False),
            ({1: "v"}, dict[str, str], False),
            ({1: 2}, dict[str, str], False),
            ({}, _DictModel, False),
            (_DictModel(param1="foo"), dict[str, int], False),
            # parameterized generics without dedicated handling (validated against the origin type)
            ((1, 2), tuple[int, int], True),
            ({"a"}, set[str], True),
            ([1], set[str], False),
            # None against Annotated/Literal
            (None, Annotated[int | None, "meta"], True),
            (None, Annotated[int, "meta"], False),
            (None, Literal[None], True),
            (None, Literal["a", "b"], False),
        ],
    )
    def test_matches_type(self, value: Any, tp: Any, is_valid: bool) -> None:
        """Test that a value can be validated if it conforms to the type annotation"""
        assert param_type_util.matches_type(value, tp) is is_valid

    @pytest.mark.parametrize(
        ("value", "tp", "expected"),
        [
            (["a", "b"], list[str], True),
            ([1, 2], list[str], False),
            ({"a": 1}, dict[str, int], True),
            ({"a": "b"}, dict[str, int], False),
        ],
    )
    def test_list_and_dict_element_type_checking(self, value: Any, tp: Any, expected: bool) -> None:
        """Test that list and dict annotations validate element/key/value types correctly"""
        assert param_type_util.matches_type(value, tp) is expected


class TestSequenceElemType:
    """Unit tests for `sequence_elem_type()`'s bare-collection detection: an unparameterized `list`/
    `tuple`/`set`/`frozenset`/`Sequence` is still repeatable and declares no element type at all
    (`inspect.Parameter.empty`, a caller's own "no type declared" sentinel), while a fixed-length
    heterogeneous `tuple[str, int]`, a `dict`, and a plain `str` are not sequences at all
    """

    @pytest.mark.parametrize("annotation", [list, tuple, set, frozenset, Sequence, typing.Sequence])
    def test_an_unparameterized_collection_declares_no_element_type(self, annotation: Any) -> None:
        """Test that a bare, unparameterized collection annotation - `collections.abc.Sequence` and
        `typing.Sequence` alike, which `get_origin()` resolves differently from one another when bare -
        returns the "no type declared" sentinel rather than `None`, so the caller still treats it as
        repeatable
        """
        assert param_type_util.get_sequence_elem_type(annotation) is inspect.Parameter.empty

    @pytest.mark.parametrize("annotation", [tuple[str, int], dict, str])
    def test_a_non_sequence_shape_returns_none(self, annotation: Any) -> None:
        """Test that a fixed-length heterogeneous tuple, a `dict`, and a plain `str` return `None`, unlike
        an unparameterized collection
        """
        assert param_type_util.get_sequence_elem_type(annotation) is None

    @pytest.mark.parametrize(
        ("annotation", "elem_type"),
        [
            (list[str], str),
            (tuple[int, ...], int),
            (set[float], float),
            (frozenset[str], str),
            (Sequence[str], str),
        ],
    )
    def test_a_parameterized_collection_returns_its_element_type(self, annotation: Any, elem_type: Any) -> None:
        """Test that a parameterized homogeneous collection returns its declared element type"""
        assert param_type_util.get_sequence_elem_type(annotation) is elem_type


class TestUnwrapAnnotation:
    """Tests for `unwrap_annotation()`: stripping `Annotated[]` metadata and unwrapping a nullable union
    down to its non-`None` member(s), while reporting whether `None` was among them
    """

    def test_a_plain_annotation_is_returned_unchanged_and_not_nullable(self) -> None:
        """Test that an annotation with no `Annotated[]`/union wrapping at all passes through unchanged"""
        assert param_type_util.unwrap_annotation(str) == (str, False)

    def test_annotated_metadata_is_stripped(self) -> None:
        """Test that `Annotated[]` metadata is stripped down to the wrapped type"""
        assert param_type_util.unwrap_annotation(Annotated[str, "query"]) == (str, False)

    def test_a_single_member_optional_unwraps_to_its_bare_type_and_reports_nullable(self) -> None:
        """Test that `T | None` unwraps to `T`, reporting nullability"""
        assert param_type_util.unwrap_annotation(str | None) == (str, True)

    def test_a_non_nullable_multi_member_union_rebuilds_without_none(self) -> None:
        """Test that a multi-member union with no `None` rebuilds to the same members, not nullable"""
        base, nullable = param_type_util.unwrap_annotation(int | str)
        assert set(typing.get_args(base)) == {int, str}
        assert nullable is False

    def test_a_nullable_multi_member_union_rebuilds_without_none_and_reports_nullable(self) -> None:
        """Test that a multi-member union with `None` rebuilds to its non-`None` members, reporting
        nullability, so a caller never sees `NoneType` among the rebuilt union's own args
        """
        base, nullable = param_type_util.unwrap_annotation(int | str | None)
        assert typing.get_args(base) and type(None) not in typing.get_args(base)
        assert set(typing.get_args(base)) == {int, str}
        assert nullable is True

    def test_annotated_wrapping_a_nullable_union_unwraps_both_layers(self) -> None:
        """Test that `Annotated[]` wrapping a nullable union unwraps through both layers in one call"""
        assert param_type_util.unwrap_annotation(Annotated[str | None, "query"]) == (str, True)

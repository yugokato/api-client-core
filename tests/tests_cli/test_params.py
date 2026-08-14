"""Unit tests for `api_client_core.cli.params`"""

import argparse
import inspect
import io
import sys
import typing
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import Enum, StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Unpack
from uuid import UUID

import pytest
from common_libs.ansi_colors import ColorCodes, color, remove_color_code
from pytest import CaptureFixture
from pytest_mock import MockerFixture

from api_client_core import BaseAPI, endpoint
from api_client_core.cli import params as params_module
from api_client_core.cli._constants import ELLIPSIS
from api_client_core.cli.params import (
    _DESCRIPTION_INDENT,
    _MAX_CHOICE_GROUP_WIDTH,
    _MAX_UNION_TYPE_WIDTH,
    _PARAMS_GROUP_TITLE,
    _RESERVED_CALL_KWARGS,
    NOT_PROVIDED,
    _arg_spec,
    _flag_for,
    _format_choice_group,
    _format_default,
    _parse_json,
    _parse_json_or_str,
    _sequence_elem_type,
    accepts_file_path,
    accepts_json_file,
    add_endpoint_arguments,
    collect_call_kwargs,
    normalize_call_args,
    peek_log_level,
    split_param_docs,
)
from api_client_core.cli.parser import ArgumentParser, full_metavar
from api_client_core.types import Alias, File, Kwargs, Query, RestResponse, Unset

from .conftest import Status, WidgetsAPI, find_group_title


def _build_parser(endpoint_func_name: str, api_class: type[BaseAPI]) -> argparse.ArgumentParser:
    """Build a leaf parser for one API class's endpoint, mirroring what `builder.py` does."""
    endpoint = getattr(api_class, endpoint_func_name).endpoint
    parser = argparse.ArgumentParser()
    parser.set_defaults(_endpoint=endpoint)
    add_endpoint_arguments(parser, endpoint)
    return parser


@pytest.fixture
def reserved_name_api_class() -> type[BaseAPI]:
    """A synthetic API class with a parameter name that collides with a reserved model field name,
    so its model field gets renamed (`Query` -> `Query_`) by the framework's own aliasing.
    """

    class ReservedNameAPI(BaseAPI):
        """A synthetic API class exercising a reserved-name parameter."""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(self, Query: str, **kwargs: Unpack[Kwargs]) -> RestResponse:
            """Make a thing"""
            ...

    return ReservedNameAPI


@pytest.fixture
def reserved_cli_flag_api_class() -> type[BaseAPI]:
    """A synthetic API class with a parameter literally named `quiet`, colliding with the CLI's own
    reserved `--quiet` control flag (see `RESERVED_CLI_FLAGS`).
    """

    class ReservedFlagAPI(BaseAPI):
        """A synthetic API class exercising a reserved-CLI-flag collision."""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(self, quiet: str, **kwargs: Unpack[Kwargs]) -> RestResponse:
            """Make a thing"""
            ...

    return ReservedFlagAPI


@pytest.fixture
def reserved_help_flag_api_class() -> type[BaseAPI]:
    """A synthetic API class with a parameter literally named `help`, colliding with argparse's own
    auto-added `-h`/`--help` flag (see `RESERVED_CLI_FLAGS`).
    """

    class ReservedHelpFlagAPI(BaseAPI):
        """A synthetic API class exercising a `--help`-colliding parameter."""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(self, help: str, **kwargs: Unpack[Kwargs]) -> RestResponse:
            """Make a thing"""
            ...

    return ReservedHelpFlagAPI


@pytest.fixture
def reserved_header_flag_api_class() -> type[BaseAPI]:
    """A synthetic API class with a parameter literally named `header`, colliding with the CLI's own
    reserved `-H`/`--header` control flag (see `RESERVED_CLI_FLAGS`).
    """

    class ReservedHeaderFlagAPI(BaseAPI):
        """A synthetic API class exercising a `-H`/`--header`-colliding parameter."""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(self, header: str, **kwargs: Unpack[Kwargs]) -> RestResponse:
            """Make a thing"""
            ...

    return ReservedHeaderFlagAPI


@pytest.fixture
def reserved_call_kwarg_api_class() -> type[BaseAPI]:
    """A synthetic API class with parameters literally named `with_hooks`/`raw_options`, colliding with the
    control kwargs `run()` passes to every dispatched call (see `_RESERVED_CALL_KWARGS`).
    """

    class ReservedCallKwargAPI(BaseAPI):
        """A synthetic API class exercising a control-kwarg-name collision."""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(
            self, name: str, with_hooks: str = "ok", raw_options: str = "ok", **kwargs: Unpack[Kwargs]
        ) -> RestResponse:
            """Make a thing"""
            ...

    return ReservedCallKwargAPI


@pytest.fixture
def duplicate_flag_api_class() -> type[BaseAPI]:
    """A synthetic API class with two parameters whose derived flags collide (`a_b` and `a__b` both
    normalize to `--a-b`, see `_flag_for`).
    """

    class DuplicateFlagAPI(BaseAPI):
        """A synthetic API class exercising two parameters that collapse to the same CLI flag."""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(self, a_b: str = "first", a__b: str = "second", **kwargs: Unpack[Kwargs]) -> RestResponse:
            """Make a thing"""
            ...

    return DuplicateFlagAPI


@pytest.fixture
def trailing_underscore_api_class() -> type[BaseAPI]:
    """A synthetic API class with a parameter named with a trailing underscore (e.g. `pop_`), the common
    escape for a name that would otherwise shadow something else, whose derived flag (`--pop`) drops the
    underscore `_flag_for()` strips.
    """

    class TrailingUnderscoreAPI(BaseAPI):
        """A synthetic API class exercising a trailing-underscore parameter name."""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(self, pop_: int = 0, **kwargs: Unpack[Kwargs]) -> RestResponse:
            """Make a thing"""
            ...

    return TrailingUnderscoreAPI


@pytest.fixture
def bool_reserved_flag_api_class() -> type[BaseAPI]:
    """A synthetic API class with a bool parameter named `hooks`, whose `argparse.BooleanOptionalAction`
    `--no-hooks` form collides with the reserved `--no-hooks` control flag.
    """

    class BoolReservedFlagAPI(BaseAPI):
        """A synthetic API class exercising a bool parameter's --no- form colliding with --no-hooks"""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(self, hooks: bool = True, **kwargs: Unpack[Kwargs]) -> RestResponse:
            """Make a thing"""
            ...

    return BoolReservedFlagAPI


@pytest.fixture
def chained_alias_collision_api_class() -> type[BaseAPI]:
    """A synthetic API class with two parameters whose reserved-flag aliases collide with each other in
    turn, forcing the second one to escalate past a single trailing underscore.

    `output_` and `output` both derive the same reserved `--output` flag (`_flag_for()` strips a param
    name's own trailing underscore before building its flag), so both need aliasing. `output_`'s first
    attempt, `--output_`, succeeds and claims dest `output__` (its own trailing underscore, plus the
    alias loop's). `output`'s own first attempt, also `--output_`, then collides on the flag; its second
    attempt, `--output__`, would be a free flag but collides with `output_`'s own dest (`output__`)
    instead; only its third attempt, `--output___`, is free of both.
    """

    class ChainedAliasCollisionAPI(BaseAPI):
        """A synthetic API class exercising a multi-level trailing-underscore alias escalation"""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(self, output_: str = "a", output: str = "b", **kwargs: Unpack[Kwargs]) -> RestResponse:
            """Make a thing"""
            ...

    return ChainedAliasCollisionAPI


@pytest.fixture
def no_prefixed_bool_api_class() -> type[BaseAPI]:
    """A synthetic API class with a bool parameter whose own name already starts with `no_`, so its
    derived flag (`--no-cache`) itself starts with `--no-` (see `_arg_spec()`).
    """

    class NoPrefixedBoolAPI(BaseAPI):
        """A synthetic API class exercising a bool parameter named with a `no_` prefix"""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(self, no_cache: bool = False, **kwargs: Unpack[Kwargs]) -> RestResponse:
            """Make a thing"""
            ...

    return NoPrefixedBoolAPI


@pytest.fixture
def list_of_dicts_api_class() -> type[BaseAPI]:
    """A synthetic API class with a `list[dict[str, Any]]` parameter."""

    class ListOfDictsAPI(BaseAPI):
        """A synthetic API class exercising a list of non-scalar elements"""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(self, items: list[dict[str, Any]] = Unset, **kwargs: Unpack[Kwargs]) -> RestResponse:
            """Make a thing"""
            ...

    return ListOfDictsAPI


@pytest.fixture
def required_list_api_class() -> type[BaseAPI]:
    """A synthetic API class with a required `list[str]` parameter."""

    class RequiredListAPI(BaseAPI):
        """A synthetic API class exercising a required list parameter"""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(self, tags: list[str], **kwargs: Unpack[Kwargs]) -> RestResponse:
            """Make a thing"""
            ...

    return RequiredListAPI


@pytest.fixture
def required_json_list_api_class() -> type[BaseAPI]:
    """A synthetic API class with a required `list[dict[str, int]]` parameter, mirroring the real
    `DummyJSON` `create_cart` endpoint's `products` parameter.
    """

    class RequiredJsonListAPI(BaseAPI):
        """A synthetic API class exercising a required list of JSON objects."""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(self, products: list[dict[str, int]], **kwargs: Unpack[Kwargs]) -> RestResponse:
            """Make a thing"""
            ...

    return RequiredJsonListAPI


@pytest.fixture
def json_list_default_api_class() -> type[BaseAPI]:
    """A synthetic API class with an optional `list[dict[str, Any]] | None` parameter defaulting to
    `None`. `| None = None` rather than a bare `list` default, since a mutable default would fail model
    construction.
    """

    class JsonListDefaultAPI(BaseAPI):
        """A synthetic API class exercising an optional list of JSON objects with a `None` default."""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(self, items: list[dict[str, Any]] | None = None, **kwargs: Unpack[Kwargs]) -> RestResponse:
            """Make a thing"""
            ...

    return JsonListDefaultAPI


@pytest.fixture
def list_file_api_class() -> type[BaseAPI]:
    """A synthetic API class with a `list[File]` parameter, for a multi-file upload."""

    class ListFileAPI(BaseAPI):
        """A synthetic API class exercising a list of File uploads."""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(self, attachments: list[File], **kwargs: Unpack[Kwargs]) -> RestResponse:
            """Make a thing"""
            ...

    return ListFileAPI


@pytest.fixture
def unannotated_api_class() -> type[BaseAPI]:
    """A synthetic API class with parameters carrying no annotation at all, which the CLI can only map to a
    JSON-parsed flag with no nameable value type.
    """

    class UnannotatedAPI(BaseAPI):
        """A synthetic API class exercising parameters with no annotation at all, including one whose name
        collides with a reserved model name and is renamed by the model builder.
        """

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(  # type: ignore[no-untyped-def]
            self, name: str, param=Unset, legacy=None, Literal=Unset, **kwargs: Unpack[Kwargs]
        ) -> RestResponse:
            """Make a thing"""
            ...

    return UnannotatedAPI


@pytest.fixture
def list_bool_api_class() -> type[BaseAPI]:
    """A synthetic API class with a `list[bool]` parameter."""

    class ListBoolAPI(BaseAPI):
        """A synthetic API class exercising a list of bool elements."""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(self, flags: list[bool] = Unset, **kwargs: Unpack[Kwargs]) -> RestResponse:
            """Make a thing"""
            ...

    return ListBoolAPI


@pytest.fixture
def list_literal_api_class() -> type[BaseAPI]:
    """A synthetic API class with a `list[Literal["asc", "desc"]]` parameter."""

    class ListLiteralAPI(BaseAPI):
        """A synthetic API class exercising a list of Literal elements."""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(self, orders: list[Literal["asc", "desc"]] = Unset, **kwargs: Unpack[Kwargs]) -> RestResponse:
            """Make a thing"""
            ...

    return ListLiteralAPI


@pytest.fixture
def list_enum_api_class() -> type[BaseAPI]:
    """A synthetic API class with a `list[Status]` parameter."""

    class ListEnumAPI(BaseAPI):
        """A synthetic API class exercising a list of Enum elements."""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(self, statuses: list[Status] = Unset, **kwargs: Unpack[Kwargs]) -> RestResponse:
            """Make a thing"""
            ...

    return ListEnumAPI


@pytest.fixture
def nested_list_api_class() -> type[BaseAPI]:
    """A synthetic API class with a `list[list[int]]` parameter."""

    class NestedListAPI(BaseAPI):
        """A synthetic API class exercising a list of list elements."""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(self, groups: list[list[int]] = Unset, **kwargs: Unpack[Kwargs]) -> RestResponse:
            """Make a thing"""
            ...

    return NestedListAPI


@pytest.fixture
def mixed_literal_api_class() -> type[BaseAPI]:
    """A synthetic API class with a `Literal["a", 1]` parameter, mixing member types."""

    class MixedLiteralAPI(BaseAPI):
        """A synthetic API class exercising a mixed-type Literal parameter."""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(self, choice: Literal["a", 1] = Unset, **kwargs: Unpack[Kwargs]) -> RestResponse:
            """Make a thing"""
            ...

    return MixedLiteralAPI


@pytest.fixture
def enum_literal_api_class() -> type[BaseAPI]:
    """A synthetic API class with a `Literal[Status.ACTIVE]` parameter."""

    class EnumLiteralAPI(BaseAPI):
        """A synthetic API class exercising an enum-member Literal parameter."""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(self, status: Literal[Status.ACTIVE] = Unset, **kwargs: Unpack[Kwargs]) -> RestResponse:
            """Make a thing"""
            ...

    return EnumLiteralAPI


@pytest.fixture
def enum_default_api_class() -> type[BaseAPI]:
    """A synthetic API class with an `Enum`-typed parameter defaulting to a real member."""

    class EnumDefaultAPI(BaseAPI):
        """A synthetic API class exercising an Enum parameter with a non-Unset default."""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(self, status: Status = Status.ACTIVE, **kwargs: Unpack[Kwargs]) -> RestResponse:
            """Make a thing"""
            ...

    return EnumDefaultAPI


@pytest.fixture
def mixed_column_api_class() -> type[BaseAPI]:
    """A synthetic API class mixing a bool (no value-type column at all), a `str`, and a long
    multi-member union parameter, for an end-to-end check that the marker column still aligns across
    every row regardless of how wide (or absent) each one's own value-type name is.
    """

    class MixedColumnAPI(BaseAPI):
        """A synthetic API class exercising mixed value-type column widths."""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(
            self,
            active: bool = True,
            name: str = "x",
            wide: int | float | str = 1,
            **kwargs: Unpack[Kwargs],
        ) -> RestResponse:
            """Make a thing"""
            ...

    return MixedColumnAPI


@pytest.fixture
def file_or_str_api_class() -> type[BaseAPI]:
    """A synthetic API class with a `File | str` parameter, for an end-to-end check that it accepts
    either an existing file path or a plain string, rather than hard-requiring an existing file the way a
    top-level `is_type_of(annotation, File)` short-circuit used to for any File-containing union.
    """

    class FileOrStrAPI(BaseAPI):
        """A synthetic API class exercising a File-or-str union parameter."""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(self, attachment: File | str = Unset, **kwargs: Unpack[Kwargs]) -> RestResponse:
            """Make a thing"""
            ...

    return FileOrStrAPI


@pytest.fixture
def file_or_list_file_api_class() -> type[BaseAPI]:
    """A synthetic API class with a `File | list[File]` parameter, for an `accepts_file_path()` check."""

    class FileOrListFileAPI(BaseAPI):
        """A synthetic API class exercising a File-or-list-of-File union parameter."""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(self, attachments: File | list[File] = Unset, **kwargs: Unpack[Kwargs]) -> RestResponse:
            """Make a thing"""
            ...

    return FileOrListFileAPI


@pytest.fixture
def scalar_union_api_class() -> type[BaseAPI]:
    """A synthetic API class with a `str | int` parameter, for an end-to-end check of the plain (non-JSON)
    scalar-union CLI parsing `_arg_spec()` builds.
    """

    class ScalarUnionAPI(BaseAPI):
        """A synthetic API class exercising a str/int scalar union parameter."""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(self, item_id: str | int, **kwargs: Unpack[Kwargs]) -> RestResponse:
            """Make a thing"""
            ...

    return ScalarUnionAPI


@pytest.fixture
def json_default_api_class() -> type[BaseAPI]:
    """A synthetic API class with a JSON-typed (dict) parameter defaulting to Python `None`, for an
    end-to-end check that its help shows the JSON spelling `null` rather than Python's `None`.
    """

    class JsonDefaultAPI(BaseAPI):
        """A synthetic API class exercising a JSON-typed parameter with a None default."""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(self, metadata: dict[str, Any] | None = None, **kwargs: Unpack[Kwargs]) -> RestResponse:
            """Make a thing"""
            ...

    return JsonDefaultAPI


@pytest.fixture
def scalar_or_list_api_class() -> type[BaseAPI]:
    """A synthetic API class with an `int | list[int]` parameter, for an end-to-end check of the
    scalar-or-list CLI parsing `_arg_spec()` builds.
    """

    class ScalarOrListAPI(BaseAPI):
        """A synthetic API class exercising a scalar-or-list-of-the-same-scalar parameter."""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(self, ids: int | list[int] = Unset, **kwargs: Unpack[Kwargs]) -> RestResponse:
            """Make a thing"""
            ...

    return ScalarOrListAPI


@pytest.fixture
def scalar_or_differing_list_api_class() -> type[BaseAPI]:
    """A synthetic API class with an `int | list[str]` parameter, for an end-to-end check of the
    scalar-or-list CLI parsing `_arg_spec()` builds when the scalar and list-element types differ.
    """

    class ScalarOrDifferingListAPI(BaseAPI):
        """A synthetic API class exercising a scalar-or-list parameter with differing element types."""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(self, ids: int | list[str] = Unset, **kwargs: Unpack[Kwargs]) -> RestResponse:
            """Make a thing"""
            ...

    return ScalarOrDifferingListAPI


@pytest.fixture
def reserved_dest_api_class() -> type[BaseAPI]:
    """A synthetic API class with a parameter literally named `_endpoint`, colliding with the
    `_endpoint` argparse dest `build_client_parser()` sets via `set_defaults()` (see `_RESERVED_DESTS`).
    """

    class ReservedDestAPI(BaseAPI):
        """A synthetic API class exercising a reserved-dest collision."""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(self, _endpoint: str, name: str = "x", **kwargs: Unpack[Kwargs]) -> RestResponse:
            """Make a thing"""
            ...

    return ReservedDestAPI


@pytest.fixture
def paramless_api_class() -> type[BaseAPI]:
    """A synthetic API class whose endpoint takes no parameters beyond `**kwargs`."""

    class PingAPI(BaseAPI):
        """A synthetic API class exercising a parameterless endpoint."""

        app_name = "cli-test"

        @endpoint.get("/ping")
        def ping(self, **kwargs: Unpack[Kwargs]) -> RestResponse:
            """Ping"""
            ...

    return PingAPI


@pytest.fixture
def literal_bool_api_class() -> type[BaseAPI]:
    """A synthetic API class with a `Literal[True, False]` parameter, for a regression check that its
    help metavar and its own converter agree on the same `true`/`false` token spelling, rather than
    argparse deriving `{True,False}` from the raw Python values while the converter only ever accepts
    `true`/`false`.
    """

    class LiteralBoolAPI(BaseAPI):
        """A synthetic API class exercising a Literal[True, False] parameter."""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(self, flag: Literal[True, False] = Unset, **kwargs: Unpack[Kwargs]) -> RestResponse:
            """Make a thing"""
            ...

    return LiteralBoolAPI


@pytest.fixture
def status_or_str_api_class() -> type[BaseAPI]:
    """A synthetic API class with a `Status | str` parameter, for an end-to-end check that a union
    member's own Enum/Literal choices stay visible in the CLI value-type column, rather than being lost
    the way `_union_value_spec()`'s own combined `value_type` used to drop them.
    """

    class StatusOrStrAPI(BaseAPI):
        """A synthetic API class exercising an Enum-or-str union parameter."""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(self, status: Status | str = Unset, **kwargs: Unpack[Kwargs]) -> RestResponse:
            """Make a thing"""
            ...

    return StatusOrStrAPI


class _CustomLabel(str):
    """A `str` subclass, used to test that a str subclass still resolves to a plain `str`-typed CLI flag
    rather than falling back to JSON.
    """


@pytest.fixture
def stringly_typed_api_class() -> type[BaseAPI]:
    """A synthetic API class with `datetime`/`date`/`UUID`/`Decimal`/a `str`-subclass parameter, for an
    end-to-end check that each accepts a bare CLI token and displays as `str`, rather than falling back to
    the `json` fallback the way an arbitrary unmapped type does. The framework performs no runtime type
    validation on parameter values, so a plain string passes straight through regardless of which of these
    it actually is.
    """

    class StringlyTypedAPI(BaseAPI):
        """A synthetic API class exercising stringly-typed parameters."""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(
            self,
            created_at: datetime = Unset,
            due_date: date = Unset,
            request_id: UUID = Unset,
            amount: Decimal = Unset,
            label: _CustomLabel = Unset,
            **kwargs: Unpack[Kwargs],
        ) -> RestResponse:
            """Make a thing"""
            ...

    return StringlyTypedAPI


class _StrStatus(StrEnum):
    """A `str`-based Enum, used to test that Enum resolution still wins over the newly added
    str-subclass handling in `_value_spec()`.
    """

    ACTIVE = "active"


@pytest.fixture
def str_enum_api_class() -> type[BaseAPI]:
    """A synthetic API class with a `str`-based Enum parameter, for a regression check that it still
    resolves as an Enum (member-name tokens, its own metavar) rather than the str-subclass handling in
    `_value_spec()` shadowing it.
    """

    class StrEnumAPI(BaseAPI):
        """A synthetic API class exercising a str-based Enum parameter."""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(self, status: _StrStatus = Unset, **kwargs: Unpack[Kwargs]) -> RestResponse:
            """Make a thing"""
            ...

    return StrEnumAPI


class _PlainWidget:
    """A plain class with no generic origin, used to test that an unmapped custom type still falls back
    to JSON rather than being swept up by the new stringly-typed handling.
    """


@pytest.fixture
def plain_class_api_class() -> type[BaseAPI]:
    """A synthetic API class with a plain-custom-class-typed parameter, for a regression check that it
    still falls back to the `json` CLI value type.
    """

    class PlainClassAPI(BaseAPI):
        """A synthetic API class exercising a plain custom-class parameter."""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(self, payload: _PlainWidget = Unset, **kwargs: Unpack[Kwargs]) -> RestResponse:
            """Make a thing"""
            ...

    return PlainClassAPI


@pytest.fixture
def per_member_annotated_scalar_or_list_api_class() -> type[BaseAPI]:
    """A synthetic API class with a `str | list[str]` parameter where each union member carries its own
    `Annotated[..., Query()]` wrapping, rather than the single outer-`Annotated` form
    `param_type_util.annotate_type()` itself produces, for a regression check that `_scalar_or_list_spec()`
    still recognizes the `list` member despite the per-member wrapping.
    """

    class PerMemberAnnotatedAPI(BaseAPI):
        """A synthetic API class exercising a per-member Annotated scalar-or-list parameter."""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(
            self, tags: Annotated[str, Query()] | Annotated[list[str], Query()] = Unset, **kwargs: Unpack[Kwargs]
        ) -> RestResponse:
            """Make a thing"""
            ...

    return PerMemberAnnotatedAPI


@pytest.fixture
def sequence_container_api_class() -> type[BaseAPI]:
    """A synthetic API class with `tuple[str, ...]`/`set[int]`/`Sequence[str]`-typed parameters, plus a
    fixed-length, heterogeneous `tuple[str, int]` one, for an end-to-end check that `_arg_spec()` grants
    the former the same multi-value CLI treatment as `list[X]`, while the latter (no single element type
    to convert CLI tokens with) still falls back to a single JSON-typed flag.
    """

    class SequenceContainerAPI(BaseAPI):
        """A synthetic API class exercising non-`list` sequence-container parameters."""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(
            self,
            tags: tuple[str, ...] = Unset,
            codes: set[int] = Unset,
            labels: Sequence[str] = Unset,
            pair: tuple[str, int] = Unset,
            **kwargs: Unpack[Kwargs],
        ) -> RestResponse:
            """Make a thing"""
            ...

    return SequenceContainerAPI


@pytest.fixture
def bare_collection_api_class() -> type[BaseAPI]:
    """A synthetic API class with unparameterized `list`/`tuple`-typed parameters, plus a `list | None` one,
    each declaring no element type at all.
    """

    class BareCollectionAPI(BaseAPI):
        """A synthetic API class exercising unparameterized collection parameters."""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(
            self,
            ids: list = Unset,  # type: ignore[type-arg]
            pair: tuple = Unset,  # type: ignore[type-arg]
            legacy: list | None = None,  # type: ignore[type-arg]
            **kwargs: Unpack[Kwargs],
        ) -> RestResponse:
            """Make a thing"""
            ...

    return BareCollectionAPI


@pytest.fixture
def mixed_repeatable_column_api_class() -> type[BaseAPI]:
    """A synthetic API class mixing a bool (no value-type column at all), a `str`, and a `list[str] | None`
    parameter, for an end-to-end check that a repeatable flag's own `[]`-suffixed type name is included in
    the shared column-width calculation, not just its own rendered value.
    """

    class MixedRepeatableColumnAPI(BaseAPI):
        """A synthetic API class exercising a repeatable parameter's effect on column alignment."""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(
            self,
            active: bool = True,
            name: str = "x",
            tags: list[str] | None = None,
            **kwargs: Unpack[Kwargs],
        ) -> RestResponse:
            """Make a thing"""
            ...

    return MixedRepeatableColumnAPI


class TestAddEndpointArguments:
    """Tests for `add_endpoint_arguments()`"""

    def test_required_path_param_is_required(
        self, widgets_api_class: type[WidgetsAPI], capsys: CaptureFixture[str]
    ) -> None:
        """Test that a required path parameter becomes a required flag"""
        parser = _build_parser("get_widget", widgets_api_class)
        with pytest.raises(SystemExit):
            parser.parse_args([])

    def test_required_body_param_is_required(
        self, widgets_api_class: type[WidgetsAPI], capsys: CaptureFixture[str]
    ) -> None:
        """Test that a required body parameter (no signature default) is required, not merely because
        it's a path param. Both required and optional body/query fields default to `Unset` on the
        model, so this must come from the original signature, not the model default
        """
        parser = _build_parser("create_widget", widgets_api_class)
        with pytest.raises(SystemExit):
            parser.parse_args(["--name", "widget-1"])  # omits required --owner-id

    def test_optional_param_defaults_to_not_provided(self, widgets_api_class: type[WidgetsAPI]) -> None:
        """Test that an optional parameter omitted on the command line is left as NOT_PROVIDED"""
        parser = _build_parser("create_widget", widgets_api_class)
        args = parser.parse_args(["--name", "w", "--owner-id", "1"])
        assert args.tags is NOT_PROVIDED
        assert args.status is NOT_PROVIDED
        assert args.metadata is NOT_PROVIDED

    def test_bool_flag_uses_boolean_optional_action(self, widgets_api_class: type[WidgetsAPI]) -> None:
        """Test that a bool field maps to a --flag/--no-flag pair"""
        parser = _build_parser("create_widget", widgets_api_class)
        args = parser.parse_args(["--name", "w", "--owner-id", "1", "--active"])
        assert args.active is True
        args = parser.parse_args(["--name", "w", "--owner-id", "1", "--no-active"])
        assert args.active is False

    def test_list_param_accepts_multiple_values(self, widgets_api_class: type[WidgetsAPI]) -> None:
        """Test that a list[str] field maps to nargs='*'"""
        parser = _build_parser("create_widget", widgets_api_class)
        args = parser.parse_args(["--name", "w", "--owner-id", "1", "--tags", "a", "b", "c"])
        assert args.tags == ["a", "b", "c"]

    def test_literal_param_coerces_and_restricts_choices(
        self, widgets_api_class: type[WidgetsAPI], capsys: CaptureFixture[str]
    ) -> None:
        """Test that a non-str Literal field coerces the CLI string and restricts to its choices"""
        parser = _build_parser("create_widget", widgets_api_class)
        args = parser.parse_args(["--name", "w", "--owner-id", "1", "--priority", "2"])
        assert args.priority == 2
        with pytest.raises(SystemExit):
            parser.parse_args(["--name", "w", "--owner-id", "1", "--priority", "99"])

    def test_enum_param_converts_member_name(
        self, widgets_api_class: type[WidgetsAPI], status_enum: type[Status]
    ) -> None:
        """Test that an Enum field converts a member-name string to the enum member"""
        parser = _build_parser("create_widget", widgets_api_class)
        args = parser.parse_args(["--name", "w", "--owner-id", "1", "--status", "ACTIVE"])
        assert args.status is status_enum.ACTIVE

    def test_enum_param_help_shows_member_names_not_repr(self, widgets_api_class: type[WidgetsAPI]) -> None:
        """Test that an Enum field's help shows its bare member names (e.g. `{ACTIVE,INACTIVE}`)
        rather than argparse's default `choices` rendering (e.g. `{Status.ACTIVE,Status.INACTIVE}`),
        which would mislead the user into typing a value the parser doesn't actually accept
        """
        parser = _build_parser("create_widget", widgets_api_class)
        action = next(a for a in parser._actions if a.dest == "status")
        assert action.metavar == "{ACTIVE,INACTIVE}"

    def test_file_param_uses_a_path_metavar(self, widgets_api_class: type[WidgetsAPI]) -> None:
        """Test that a scalar `File` field's metavar is `PATH`, rather than the default `dest.upper()`
        (`AVATAR`), which says nothing about what the flag actually expects
        """
        parser = _build_parser("upload_avatar", widgets_api_class)
        action = next(a for a in parser._actions if a.dest == "avatar")
        assert action.metavar == "PATH"

    def test_list_of_files_param_uses_a_path_metavar(self, list_file_api_class: type[BaseAPI]) -> None:
        """Test that a `list[File]` field's metavar is also `PATH`, matching the scalar case, since
        `_arg_spec()`'s sequence branch reuses the same per-element `_ValueSpec`. This type-supplied
        metavar survives `add_endpoint_arguments()`'s own shared `VALUE` default for a repeatable flag with
        no explicit metavar, tested separately by `TestListMetavar`
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, list_file_api_class.make_thing.endpoint)
        action = next(a for a in parser._actions if a.dest == "attachments")
        assert action.metavar == "PATH"

    def test_file_or_str_union_param_keeps_the_default_metavar(self, file_or_str_api_class: type[BaseAPI]) -> None:
        """Test that a `File | str` union param is deliberately left with the default `dest.upper()`
        metavar: `_union_value_spec()` builds its own `add_argument()` kwargs without spreading a member
        spec's own `extra`, the same way it already drops `choices`
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, file_or_str_api_class.make_thing.endpoint)
        action = next(a for a in parser._actions if a.dest == "attachment")
        assert action.metavar is None

    def test_enum_param_rejects_an_unknown_value_with_the_member_names_listed(
        self, widgets_api_class: type[WidgetsAPI], capsys: CaptureFixture[str]
    ) -> None:
        """Test that an Enum flag given an unrecognized value is rejected with a clean error listing the
        accepted member names (matching a `Literal` flag's own `choose from ...` wording), rather than the
        unhelpful bare `invalid Status value: ...` a plain `ValueError` would produce
        """
        parser = _build_parser("create_widget", widgets_api_class)
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["--name", "w", "--owner-id", "1", "--status", "BOGUS"])
        assert exc_info.value.code == 2
        assert "invalid choice: 'BOGUS' (choose from ACTIVE, INACTIVE)" in capsys.readouterr().err

    def test_file_param_maps_to_path_type(self, widgets_api_class: type[WidgetsAPI], tmp_path: Path) -> None:
        """Test that a File field maps to a Path-typed flag"""
        avatar = tmp_path / "avatar.png"
        avatar.write_bytes(b"fake-png-bytes")
        parser = _build_parser("upload_avatar", widgets_api_class)
        args = parser.parse_args(["--widget-id", "1", "--avatar", str(avatar)])
        assert args.avatar == avatar

    def test_file_param_rejects_nonexistent_path(self, widgets_api_class: type[WidgetsAPI], tmp_path: Path) -> None:
        """Test that a File flag pointing at a nonexistent path is rejected by argparse itself (a clean
        error and exit code 2), not an uncaught FileNotFoundError once collect_call_kwargs() tries to
        read it
        """
        missing = tmp_path / "does-not-exist.png"
        parser = _build_parser("upload_avatar", widgets_api_class)
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["--widget-id", "1", "--avatar", str(missing)])
        assert exc_info.value.code == 2

    def test_dict_param_falls_back_to_json(self, widgets_api_class: type[WidgetsAPI]) -> None:
        """Test that a dict field falls back to parsing the CLI value as JSON"""
        parser = _build_parser("create_widget", widgets_api_class)
        args = parser.parse_args(["--name", "w", "--owner-id", "1", "--metadata", '{"k": "v"}'])
        assert args.metadata == {"k": "v"}

    def test_dict_param_rejects_malformed_json_with_a_clear_error(
        self, widgets_api_class: type[WidgetsAPI], capsys: CaptureFixture[str]
    ) -> None:
        """Test that a dict flag given malformed JSON is rejected by argparse itself with a clean
        `invalid JSON: ...` error (a clean error and exit code 2), not the confusing `invalid loads
        value` message a bare `json.loads` as `type=` would produce
        """
        parser = _build_parser("create_widget", widgets_api_class)
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["--name", "w", "--owner-id", "1", "--metadata", "{not json"])
        assert exc_info.value.code == 2
        assert "invalid JSON" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("api_class_fixture_name", "dropped_dests", "debug_substrings"),
        [
            ("reserved_cli_flag_api_class", ("quiet",), ("quiet",)),
            ("reserved_call_kwarg_api_class", ("with_hooks", "raw_options"), ("with_hooks", "raw_options")),
        ],
    )
    def test_param_matching_a_reserved_dest_or_call_kwarg_is_dropped_with_a_debug_log(
        self,
        request: pytest.FixtureRequest,
        api_class_fixture_name: str,
        dropped_dests: tuple[str, ...],
        debug_substrings: tuple[str, ...],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test that a parameter whose resolved name itself collides with a control kwarg `run()` passes
        to every call (`quiet`/`with_hooks`/`raw_options`) is skipped rather than added: renaming the
        parameter's own flag can't fix this, since `run()`'s `call(**call_kwargs, **ctrl_kwargs)` would
        still raise `got multiple values for keyword argument` once dispatched, under the parameter's own
        real name regardless of what CLI flag led to it. Logged at DEBUG, not WARNING, since parser
        building (and so this diagnostic) reruns on every single invocation of any command on the same
        client, not just when it's actually relevant
        """
        api_class: type[BaseAPI] = request.getfixturevalue(api_class_fixture_name)
        ep = api_class.make_thing.endpoint
        parser = argparse.ArgumentParser()
        with caplog.at_level("DEBUG", logger="api_client_core.cli.params"):
            add_endpoint_arguments(parser, ep)
        for dest in dropped_dests:
            assert not any(a.dest == dest for a in parser._actions)
        for substring in debug_substrings:
            assert substring in caplog.text

    @pytest.mark.parametrize(
        ("api_class_fixture_name", "original_flag", "alias_dest"),
        [
            ("reserved_header_flag_api_class", "--header", "header_"),
            ("reserved_help_flag_api_class", "--help", "help_"),
        ],
    )
    def test_param_colliding_with_a_reserved_flag_is_exposed_under_a_trailing_underscore_alias(
        self,
        request: pytest.FixtureRequest,
        api_class_fixture_name: str,
        original_flag: str,
        alias_dest: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test that a parameter whose own derived flag collides with a reserved CLI flag (`--header`,
        `--help`) is renamed to a trailing-underscore alias instead of dropped, so it stays reachable from
        the CLI, avoiding the opaque `conflicting option strings` argparse itself would otherwise raise
        once `builder._add_call_ctrl_arguments()` adds the same flag a second time. The rename is logged
        at DEBUG, not WARNING, since it reruns on every invocation of any command on the same client
        """
        api_class: type[BaseAPI] = request.getfixturevalue(api_class_fixture_name)
        ep = api_class.make_thing.endpoint
        parser = argparse.ArgumentParser()
        with caplog.at_level("DEBUG", logger="api_client_core.cli.params"):
            add_endpoint_arguments(parser, ep)
        alias_flag = f"{original_flag}_"
        action = next(a for a in parser._actions if a.dest == alias_dest)
        assert action.option_strings == [alias_flag]
        assert not any(original_flag in a.option_strings for a in parser._actions if a.dest != "help")
        assert alias_flag in caplog.text
        assert original_flag in caplog.text

    def test_bool_param_whose_no_form_collides_with_a_reserved_flag_is_exposed_under_a_trailing_underscore_alias(
        self, bool_reserved_flag_api_class: type[BaseAPI], caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that a bool parameter whose `--no-<name>` negation form collides with a reserved CLI flag
        (`hooks` -> `--no-hooks`, colliding with `--no-hooks`) is renamed to a `--hooks_`/`--no-hooks_`
        trailing-underscore alias pair instead of dropped
        """
        ep = bool_reserved_flag_api_class.make_thing.endpoint
        parser = argparse.ArgumentParser()
        with caplog.at_level("DEBUG", logger="api_client_core.cli.params"):
            add_endpoint_arguments(parser, ep)
        action = next(a for a in parser._actions if a.dest == "hooks_")
        assert action.option_strings == ["--hooks_", "--no-hooks_"]
        args = parser.parse_args(["--no-hooks_"])
        assert args.hooks_ is False

    def test_a_second_param_colliding_with_an_earlier_ones_flag_is_exposed_under_a_trailing_underscore_alias(
        self, duplicate_flag_api_class: type[BaseAPI], caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that two Python parameter names that normalize to the same flag (`a_b`/`a__b` -> `--a-b`,
        see `_flag_for`) don't crash the whole parser build with argparse's own `ArgumentError:
        conflicting option string`. The first parameter keeps the flag; the second is renamed to a
        trailing-underscore alias rather than dropped
        """
        ep = duplicate_flag_api_class.make_thing.endpoint
        parser = argparse.ArgumentParser()
        with caplog.at_level("DEBUG", logger="api_client_core.cli.params"):
            add_endpoint_arguments(parser, ep)
        args = parser.parse_args(["--a-b", "given", "--a-b_", "also given"])
        assert args.a_b == "given"
        assert args.a__b_ == "also given"
        assert "--a-b_" in caplog.text

    def test_an_alias_that_still_collides_escalates_to_more_trailing_underscores(
        self, chained_alias_collision_api_class: type[BaseAPI]
    ) -> None:
        """Test that when a parameter's own first trailing-underscore alias attempt still collides (here,
        with a flag/dest an earlier parameter's own alias already claimed), `_next_free_alias()` keeps
        adding one more underscore until it finds a free one, rather than giving up after a single retry
        """
        ep = chained_alias_collision_api_class.make_thing.endpoint
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, ep)
        args = parser.parse_args(["--output_", "first", "--output___", "second"])
        assert args.output__ == "first"
        assert args.output___ == "second"

    def test_bool_param_whose_own_flag_is_no_prefixed_registers_a_single_flag(
        self, no_prefixed_bool_api_class: type[BaseAPI]
    ) -> None:
        """Test that a bool parameter whose own derived flag already starts with `--no-` (e.g. `no_cache`
        -> `--no-cache`) registers as a single value-less flag instead of `argparse.BooleanOptionalAction`.

        `BooleanOptionalAction` would pair such a flag with a nonsensical `--no-no-cache` negation, and
        Python 3.14 outright rejects registering a `--no-`-prefixed option under it
        (`ValueError: invalid option name '--no-cache' for BooleanOptionalAction`), which would otherwise
        drop the whole command (see `TestUnmappableParameterFallback` for the same command-survival
        concern from a different cause).
        """
        ep = no_prefixed_bool_api_class.make_thing.endpoint
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, ep)
        action = next(a for a in parser._actions if a.dest == "no_cache")
        assert action.option_strings == ["--no-cache"]
        assert not any(a.dest == "no_cache" and "--no-no-cache" in a.option_strings for a in parser._actions)
        args = parser.parse_args(["--no-cache"])
        assert args.no_cache is True
        args = parser.parse_args([])
        assert args.no_cache is NOT_PROVIDED

    def test_param_matching_a_reserved_dest_is_dropped_with_a_debug_log(
        self, reserved_dest_api_class: type[BaseAPI], caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that a parameter named `_endpoint` is skipped rather than added: its derived flag
        (`--endpoint`) isn't itself reserved, but its dest is the same one `build_client_parser()` sets
        via `set_defaults()`, and a per-action default always wins over that on parse, which would
        otherwise silently clobber the resolved `Endpoint` with `NOT_PROVIDED`. Renaming its own flag
        (unlike a plain reserved-flag collision) can't fix this, since the collision is on the dest, not
        the flag, so it's dropped rather than aliased
        """
        ep = reserved_dest_api_class.make_thing.endpoint
        parser = argparse.ArgumentParser()
        parser.set_defaults(_endpoint=ep)
        with caplog.at_level("DEBUG", logger="api_client_core.cli.params"):
            add_endpoint_arguments(parser, ep)
        args = parser.parse_args(["--name", "x"])
        assert args._endpoint is ep
        assert "_endpoint" in caplog.text

    def test_list_of_dicts_parses_each_element_as_json(self, list_of_dicts_api_class: type[BaseAPI]) -> None:
        """Test that a `list[dict[str, Any]]` field parses each element as JSON, rather than falling back
        to the literal argument strings the way any non-scalar list element type used to
        """
        ep = list_of_dicts_api_class.make_thing.endpoint
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, ep)
        args = parser.parse_args(["--items", '{"a": 1}', '{"b": 2}'])
        assert args.items == [{"a": 1}, {"b": 2}]

    def test_required_list_param_rejects_zero_values(self, required_list_api_class: type[BaseAPI]) -> None:
        """Test that a required list parameter given with zero values is rejected by argparse itself
        (`nargs="+"`), rather than silently accepted as an empty list (`nargs="*"`)
        """
        ep = required_list_api_class.make_thing.endpoint
        parser = argparse.ArgumentParser(exit_on_error=False)
        add_endpoint_arguments(parser, ep)
        with pytest.raises(argparse.ArgumentError):
            parser.parse_args(["--tags"])
        args = parser.parse_args(["--tags", "a", "b"])
        assert args.tags == ["a", "b"]

    def test_required_param_help_shows_a_red_required_marker(self, widgets_api_class: type[WidgetsAPI]) -> None:
        """Test that a required parameter's help text carries a red `*required` marker, and that an
        optional parameter's help text carries neither the marker nor the color code
        """
        parser = _build_parser("create_widget", widgets_api_class)
        required_action = next(a for a in parser._actions if a.dest == "name")
        optional_action = next(a for a in parser._actions if a.dest == "active")
        assert required_action.help is not None
        assert color("*required", color_code=ColorCodes.RED) in required_action.help
        assert optional_action.help is not None
        assert "required" not in optional_action.help
        assert ColorCodes.RED not in optional_action.help

    def test_flags_are_added_to_the_endpoint_parameters_group(self, widgets_api_class: type[WidgetsAPI]) -> None:
        """Test that every endpoint-parameter flag lands in its own `endpoint parameters` group,
        separate from the CLI's call-control and execution-wrapper flags
        """
        parser = _build_parser("get_widget", widgets_api_class)
        assert find_group_title(parser, "--widget-id") == _PARAMS_GROUP_TITLE

    def test_group_is_created_but_renders_no_header_for_a_parameterless_endpoint(
        self, paramless_api_class: type[BaseAPI]
    ) -> None:
        """Test that the `endpoint parameters` group is still created for an endpoint with no
        parameters, but stays empty and so renders no header, rather than showing an empty section
        """
        ep = paramless_api_class.ping.endpoint
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, ep)
        assert any(g.title == _PARAMS_GROUP_TITLE for g in parser._action_groups)
        assert f"{_PARAMS_GROUP_TITLE}:" not in parser.format_help()

    def test_list_bool_param_converts_each_element_to_a_real_bool(self, list_bool_api_class: type[BaseAPI]) -> None:
        """Test that a `list[bool]` parameter converts each element to a real `bool`, rather than leaving
        every element as the literal string argparse itself would otherwise store
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, list_bool_api_class.make_thing.endpoint)
        args = parser.parse_args(["--flags", "true", "False"])
        assert args.flags == [True, False]

    def test_list_literal_param_rejects_a_value_outside_its_choices(
        self, list_literal_api_class: type[BaseAPI]
    ) -> None:
        """Test that a `list[Literal[...]]` parameter still enforces its own choices per element, rather
        than silently degrading to an unrestricted string list
        """
        parser = argparse.ArgumentParser(exit_on_error=False)
        add_endpoint_arguments(parser, list_literal_api_class.make_thing.endpoint)
        assert parser.parse_args(["--orders", "asc", "desc"]).orders == ["asc", "desc"]
        with pytest.raises(argparse.ArgumentError):
            parser.parse_args(["--orders", "bogus"])

    def test_nested_list_param_parses_each_element_as_json_rather_than_flattening(
        self, nested_list_api_class: type[BaseAPI]
    ) -> None:
        """Test that a `list[list[int]]` parameter parses each token as its own JSON array, rather than
        flattening every token into one `list[int]` the way a plain `int` element would
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, nested_list_api_class.make_thing.endpoint)
        args = parser.parse_args(["--groups", "[1, 2]", "[3]"])
        assert args.groups == [[1, 2], [3]]

    def test_mixed_type_literal_param_accepts_either_member_type(self, mixed_literal_api_class: type[BaseAPI]) -> None:
        """Test that `Literal["a", 1]` (mixing a str and an int member) still converts each choice to its
        own real type, rather than a naive `type=type(choices[0])` coercing every value to str
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, mixed_literal_api_class.make_thing.endpoint)
        assert parser.parse_args(["--choice", "a"]).choice == "a"
        assert parser.parse_args(["--choice", "1"]).choice == 1

    def test_enum_member_literal_param_accepts_the_member_name(
        self, enum_literal_api_class: type[BaseAPI], status_enum: type[Status]
    ) -> None:
        """Test that `Literal[SomeEnum.MEMBER]` accepts the member's own name, rather than being
        unconvertible entirely (the converter used to look the token up by the member's *value*)
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, enum_literal_api_class.make_thing.endpoint)
        args = parser.parse_args(["--status", "ACTIVE"])
        assert args.status is status_enum.ACTIVE


class TestValueTypeInHelp:
    """Tests for the CLI value type column `_help_text()` adds after the `[location]` marker"""

    def test_scalar_and_container_params_show_their_cli_value_type(self, widgets_api_class: type[WidgetsAPI]) -> None:
        """Test that str/int/list/Enum/Literal/dict params show the type of value the CLI flag actually
        accepts, in dark grey, rather than the underlying Python signature type
        """
        parser = _build_parser("create_widget", widgets_api_class)
        actions = {a.dest: a for a in parser._actions}
        expected_types = {
            "name": "str",
            "owner_id": "int",
            "tags": "str[]",
            "status": "str",
            "priority": "int",
            "metadata": "json",
        }
        for dest, value_type in expected_types.items():
            help_text = actions[dest].help
            assert help_text is not None
            assert value_type in remove_color_code(help_text).split()

    def test_bool_param_shows_no_value_type(self, widgets_api_class: type[WidgetsAPI]) -> None:
        """Test that a bool param's help shows no value type, since --flag/--no-flag takes no value at all"""
        parser = _build_parser("create_widget", widgets_api_class)
        active_action = next(a for a in parser._actions if a.dest == "active")
        assert active_action.help is not None
        assert ColorCodes.DARK_GREY not in active_action.help

    def test_file_param_shows_path_type(self, widgets_api_class: type[WidgetsAPI]) -> None:
        """Test that a File param's help shows `path`, since the CLI value is a filesystem path"""
        parser = _build_parser("upload_avatar", widgets_api_class)
        avatar_action = next(a for a in parser._actions if a.dest == "avatar")
        assert avatar_action.help is not None
        assert color("path", color_code=ColorCodes.DARK_GREY) in avatar_action.help

    def test_list_of_dicts_param_shows_json_list_type(self, list_of_dicts_api_class: type[BaseAPI]) -> None:
        """Test that a list[dict[str, Any]] param's help shows `json[]`, since each element is JSON-parsed
        and the flag is repeatable
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, list_of_dicts_api_class.make_thing.endpoint)
        items_action = next(a for a in parser._actions if a.dest == "items")
        assert items_action.help is not None
        assert "json[]" in remove_color_code(items_action.help).split()

    def test_required_list_param_shows_str_list_type(self, required_list_api_class: type[BaseAPI]) -> None:
        """Test that a required list[str] param's help shows `str[]`, keeping the suffix even once its own
        `nargs` is upgraded from `"*"` to `"+"` by the required-list handling in `add_endpoint_arguments()`
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, required_list_api_class.make_thing.endpoint)
        tags_action = next(a for a in parser._actions if a.dest == "tags")
        assert tags_action.nargs == "+"
        assert tags_action.help is not None
        assert "str[]" in remove_color_code(tags_action.help).split()

    def test_list_of_files_param_shows_path_list_type(self, list_file_api_class: type[BaseAPI]) -> None:
        """Test that a list[File] param's help shows `path[]`"""
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, list_file_api_class.make_thing.endpoint)
        attachments_action = next(a for a in parser._actions if a.dest == "attachments")
        assert attachments_action.help is not None
        assert "path[]" in remove_color_code(attachments_action.help).split()

    def test_a_repeatable_type_is_colored_the_same_as_a_scalar_type(
        self, required_list_api_class: type[BaseAPI]
    ) -> None:
        """Test that a repeatable param's `[]`-suffixed type column is still rendered in dark grey, the same
        coloring a scalar type column gets
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, required_list_api_class.make_thing.endpoint)
        tags_action = next(a for a in parser._actions if a.dest == "tags")
        assert tags_action.help is not None
        assert color("str[]", color_code=ColorCodes.DARK_GREY) in tags_action.help

    def test_marker_column_aligns_with_a_repeatable_type_in_the_mix(
        self, mixed_repeatable_column_api_class: type[BaseAPI]
    ) -> None:
        """Test that the marker column still starts at the same visible position for every parameter of an
        endpoint when one parameter's own displayed type carries the `[]` suffix, confirming the shared
        column-width calculation is computed from `display_type` (not the unsuffixed `value_type`): an
        implementation that suffixed only the rendered value and not the width calculation would still pass
        every other alignment test but misalign this column
        """
        parser = _build_parser("make_thing", mixed_repeatable_column_api_class)
        actions = {a.dest: a for a in parser._actions}
        positions = {}
        for dest in ("active", "name", "tags"):
            help_text = actions[dest].help
            assert help_text is not None
            positions[dest] = remove_color_code(help_text).index("(default:")
        assert len(set(positions.values())) == 1

    def test_unannotated_param_shows_no_value_type(self, unannotated_api_class: type[BaseAPI]) -> None:
        """Test that a parameter with no annotation at all shows no value type, unlike a genuinely
        JSON-shaped parameter, while a sibling `str` parameter on the same command still shows its own type
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, unannotated_api_class.make_thing.endpoint)
        actions = {a.dest: a for a in parser._actions}
        assert actions["param"].help is not None
        assert ColorCodes.DARK_GREY not in actions["param"].help
        assert actions["name"].help is not None
        assert color("str", color_code=ColorCodes.DARK_GREY) in actions["name"].help

    def test_unannotated_param_default_still_renders_as_json_syntax(self, unannotated_api_class: type[BaseAPI]) -> None:
        """Test that an unannotated parameter's `None` default still renders as `(default: null)`, not the
        untypeable `(default: None)`, since the flag itself stays JSON-parsed even with no displayed type
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, unannotated_api_class.make_thing.endpoint)
        legacy_action = next(a for a in parser._actions if a.dest == "legacy")
        assert legacy_action.help is not None
        assert "(default: null)" in legacy_action.help

    def test_no_help_text_ends_in_trailing_whitespace(
        self, widgets_api_class: type[WidgetsAPI], unannotated_api_class: type[BaseAPI]
    ) -> None:
        """Test that no parameter's help text ends in trailing whitespace, whether or not it carries a
        required/default/deprecated marker after the type column. Covers a blank type column too (an
        unannotated parameter), which is padded to the column width and then stripped just like a filled one
        """
        parser = _build_parser("create_widget", widgets_api_class)
        group = next(g for g in parser._action_groups if g.title == _PARAMS_GROUP_TITLE)
        for action in group._group_actions:
            assert action.help is not None
            assert action.help == action.help.rstrip()

        unannotated_parser = argparse.ArgumentParser()
        add_endpoint_arguments(unannotated_parser, unannotated_api_class.make_thing.endpoint)
        unannotated_group = next(g for g in unannotated_parser._action_groups if g.title == _PARAMS_GROUP_TITLE)
        for action in unannotated_group._group_actions:
            assert action.help is not None
            assert action.help == action.help.rstrip()

    def test_required_marker_aligns_regardless_of_the_preceding_type_column(
        self, widgets_api_class: type[WidgetsAPI]
    ) -> None:
        """Test that the marker column (`*required`, `(default: ...)`, `(deprecated)`) starts at the same
        visible position whether or not the preceding type column is filled in, so a bool param's blank
        type column doesn't shift its own markers out of line with the rest of the group
        """
        parser = _build_parser("create_widget", widgets_api_class)
        name_action = next(a for a in parser._actions if a.dest == "name")
        active_action = next(a for a in parser._actions if a.dest == "active")
        assert name_action.help is not None
        assert active_action.help is not None
        assert remove_color_code(name_action.help).index("*required") == remove_color_code(active_action.help).index(
            "(default:"
        )

    def test_json_typed_none_default_shows_as_null(self, json_default_api_class: type[BaseAPI]) -> None:
        """Test that a JSON-typed optional parameter with a Python `None` default shows `(default: null)`,
        matching what `--metadata null` actually parses to, rather than Python's own `(default: None)`
        spelling, which `_parse_json()` would reject as invalid JSON
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, json_default_api_class.make_thing.endpoint)
        metadata_action = next(a for a in parser._actions if a.dest == "metadata")
        assert metadata_action.help is not None
        assert "(default: null)" in metadata_action.help
        assert "(default: None)" not in metadata_action.help

    def test_json_list_typed_none_default_shows_as_null(self, json_list_default_api_class: type[BaseAPI]) -> None:
        """Test that a repeatable JSON-typed parameter's `None` default also shows `(default: null)`, even
        though its flag uses `_JsonListAction` rather than a plain `type=_parse_json` converter: `is_json`
        must recognize the action, not just the converter, for `_format_default()` to pick JSON syntax
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, json_list_default_api_class.make_thing.endpoint)
        items_action = next(a for a in parser._actions if a.dest == "items")
        assert items_action.help is not None
        assert "(default: null)" in items_action.help
        assert "(default: None)" not in items_action.help

    def test_list_of_enum_param_shows_member_names_not_repr(self, list_enum_api_class: type[BaseAPI]) -> None:
        """Test that a `list[Status]` param's help shows the bare member names (e.g. `{ACTIVE,INACTIVE}`)
        via its own `metavar`, the same as a scalar `Status` param, rather than losing that metavar (and
        so its documented valid values) the way the plain `list` branch used to for any non-scalar element.
        This type-supplied metavar survives the shared `VALUE` default the same way `PATH` does, tested
        separately by `TestListMetavar`
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, list_enum_api_class.make_thing.endpoint)
        statuses_action = next(a for a in parser._actions if a.dest == "statuses")
        assert statuses_action.metavar == "{ACTIVE,INACTIVE}"

    def test_marker_column_aligns_with_a_long_union_type_in_the_mix(
        self, mixed_column_api_class: type[BaseAPI]
    ) -> None:
        """Test that the marker column still starts at the same visible position for every parameter of an
        endpoint, even when one parameter's own value-type name (`int|float|str`) is much wider than the
        others' (or, for a bool, absent entirely), so a wide type name doesn't push its own row's markers
        out of line with the rest of the group
        """
        parser = _build_parser("make_thing", mixed_column_api_class)
        actions = {a.dest: a for a in parser._actions}
        positions = {}
        for dest, marker in (("active", "(default:"), ("name", "(default:"), ("wide", "(default:")):
            help_text = actions[dest].help
            assert help_text is not None
            positions[dest] = remove_color_code(help_text).index(marker)
        assert len(set(positions.values())) == 1

    def test_enum_default_renders_as_its_member_name(self, enum_default_api_class: type[BaseAPI]) -> None:
        """Test that an `Enum`-typed parameter defaulting to a real member shows `(default: ACTIVE)`,
        matching what the flag itself actually accepts, rather than Python's own `repr()` spelling
        (`<Status.ACTIVE: 'active'>`)
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, enum_default_api_class.make_thing.endpoint)
        status_action = next(a for a in parser._actions if a.dest == "status")
        assert status_action.help is not None
        assert "(default: ACTIVE)" in status_action.help
        assert "Status.ACTIVE" not in status_action.help


@pytest.fixture
def documented_params_api_class() -> type[BaseAPI]:
    """A synthetic API class whose endpoint documents two of its three parameters with their own
    `:param <name>: ...` docstring lines, one of them wrapped onto a continuation line, for testing that
    `_help_text()` shows each documented parameter's own description, in full under `--help` and clamped
    under `-h`.
    """

    class DocumentedParamsAPI(BaseAPI):
        """A synthetic API class exercising per-parameter `:param` documentation."""

        app_name = "cli-test"

        @endpoint.post("/things")
        def make_thing(
            self, name: str, note: str = Unset, undocumented: str = Unset, **kwargs: Unpack[Kwargs]
        ) -> RestResponse:
            """Make a thing

            :param name: The thing's own display name
            :param note: An optional free-form note attached to the thing, wrapped here onto a second
                        line to exercise the continuation-joining path
            """
            ...

    return DocumentedParamsAPI


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


class TestParamDescriptionInHelp:
    """Tests for `_help_text()`'s use of `split_param_docs()`: a parameter documented with its own
    `:param <name>: ...` docstring line shows that description on its own line beneath the existing
    location/type/marker line: in full under `--help`, clamped to that one line under `-h`.
    """

    def test_documented_parameter_shows_its_indented_description_after_the_marker_line(
        self, documented_params_api_class: type[BaseAPI]
    ) -> None:
        """Test that a documented parameter's help text is the existing marker line, a newline, and then
        its own description read from the endpoint function's docstring, indented so it reads as a nested
        detail rather than a continuation of the marker line, which itself stays unchanged (still
        `*required`, since `name` has no default)
        """
        parser = _build_parser("make_thing", documented_params_api_class)
        name_action = next(a for a in parser._actions if a.dest == "name")
        assert name_action.help is not None
        marker_line, _, description = name_action.help.partition("\n")
        assert "*required" in remove_color_code(marker_line)
        assert description == f"{_DESCRIPTION_INDENT}The thing's own display name"

    def test_a_wrapped_description_is_joined_before_being_shown(
        self, documented_params_api_class: type[BaseAPI]
    ) -> None:
        """Test that a parameter documented with a continuation-line description shows it already joined
        into one indented line, ready for the help formatter's own re-wrap to the real terminal width
        """
        parser = _build_parser("make_thing", documented_params_api_class)
        note_action = next(a for a in parser._actions if a.dest == "note")
        assert note_action.help is not None
        assert note_action.help.endswith(
            f"\n{_DESCRIPTION_INDENT}An optional free-form note attached to the thing, wrapped here onto a second "
            "line to exercise the continuation-joining path"
        )

    def test_undocumented_parameter_shows_no_second_line(self, documented_params_api_class: type[BaseAPI]) -> None:
        """Test that a parameter with no matching `:param` entry keeps the existing single-line help,
        with no trailing newline or description appended
        """
        parser = _build_parser("make_thing", documented_params_api_class)
        undocumented_action = next(a for a in parser._actions if a.dest == "undocumented")
        assert undocumented_action.help is not None
        assert "\n" not in undocumented_action.help

    def test_a_description_that_already_fits_renders_identically_under_short_and_full_help(
        self, documented_params_api_class: type[BaseAPI]
    ) -> None:
        """Test that a documented description short enough to already fit on one rendered line survives
        the condensed `-h` form unclamped, matching what the full `--help` form shows
        """
        parser = ArgumentParser()
        add_endpoint_arguments(parser, documented_params_api_class.make_thing.endpoint)
        short_help = remove_color_code(parser.format_help(short=True))
        full_help = remove_color_code(parser.format_help(short=False))
        assert "The thing's own display name" in short_help
        assert "The thing's own display name" in full_help

    def test_short_help_clamps_a_long_description_to_one_line_but_full_help_shows_it_in_full(
        self, documented_params_api_class: type[BaseAPI]
    ) -> None:
        """Test that a documented description too long to fit on one rendered line is clamped to its own
        first line plus a trailing ellipsis under the condensed `-h` form (see `TestShortHelp` in
        `test_parser.py` for that general mechanism), while the full `--help` form wraps it across several
        lines, end to end through the real formatter
        """
        parser = ArgumentParser()
        add_endpoint_arguments(parser, documented_params_api_class.make_thing.endpoint)
        short_help = remove_color_code(parser.format_help(short=True))
        full_help = remove_color_code(parser.format_help(short=False))

        note_short_lines = [line for line in short_help.splitlines() if "An optional free-form note" in line]
        note_full_lines = [
            line
            for line in full_help.splitlines()
            if "An optional free-form note" in line or "continuation-joining path" in line
        ]

        assert len(note_short_lines) == 1
        assert note_short_lines[0].rstrip().endswith(ELLIPSIS)
        assert "continuation-joining path" not in short_help
        assert len(note_full_lines) > 1
        assert "continuation-joining path" in full_help


class TestArgSpec:
    """Tests for `_arg_spec()`'s mapping from a resolved parameter type annotation to argparse keyword
    arguments and a displayable CLI value type, covering annotation shapes no endpoint fixture reaches
    """

    @pytest.mark.parametrize(
        ("annotation", "value_type"),
        [
            (float, "float"),
            (int | None, "int"),
            (Annotated[str, Query()], "str"),
            (str | int, "int|str"),
            (int | float, "int|float"),
            (float | str, "float|str"),
            (int | float | str, "int|float|str"),
            (str | int | None, "int|str"),
            ("list[str]", "json"),
            (list, "json"),
            (list[bool], "bool"),
            (Literal[Status.ACTIVE], "str"),
            (Literal[True], "bool"),
            (File | str, "path|str"),
            (int | list[int], "int"),
            (str | list[str], "str"),
            (int | list[int] | None, "int"),
            (int | list[str], "int|str"),
            (str | list[int], "str|int"),
            (float | list[int], "float|int"),
            (bool | list[bool], "bool"),
            (int | list[bool], "int|bool"),
            (Status | list[Status], "{ACTIVE,INACTIVE}"),
            (Literal[1, 2] | list[int], "{1,2}|int"),
            (dict | list[int], "json"),
            (int | list[int] | str, "int|str"),
            (tuple[str, ...], "str"),
            (set[int], "int"),
            (frozenset[str], "str"),
            (Sequence[str], "str"),
            (tuple[str, int], "json"),
            (tuple, "json"),
            (int | tuple[int, ...], "int"),
            (inspect.Parameter.empty, None),
            (Annotated[inspect.Parameter.empty, Alias("Literal")], None),
            (Any, "json"),
        ],
    )
    def test_value_type_for_annotation(self, annotation: Any, value_type: str | None) -> None:
        """Test that `_arg_spec()` names the CLI value type for annotation shapes not otherwise exercised
        by an endpoint fixture (a nullable scalar, a query-annotated scalar, every combination of a
        str/int/float union with and without a trailing `| None`, an unresolved string annotation, a bare
        list, a bool list element, a File-containing union, and a scalar-or-list union with a matching or
        differing element type, with and without a trailing `| None`), and stays silent only where the
        accepted value truly can't be named as one Python type (there is none left to test: even a bool or
        enum-member `Literal` now names its own shared type, see `_literal_value_type()`). A scalar union's
        displayed name always lists its members in the same order `_union_value_spec()` tries them (most
        restrictive first), regardless of the order they were written in the annotation (`str | int` and
        `int | str` both show `int|str`). A scalar-or-list union's displayed name always puts the scalar
        side first regardless of which side was written first in the annotation (`int | list[str]` and
        `list[str] | int` both show `int|str`), and collapses to one name when both sides match (`int |
        list[int]` shows `int`, not `int|int`) - this now also covers a `bool`/`Enum`/`Literal` element or
        scalar (`bool | list[bool]`, `int | list[bool]`), and a scalar side spanning more than one non-list
        member (`int | list[int] | str` shows `int|str`). An `Enum`/`Literal` union member shows its own
        choice group rather than its shared type name, so its choices stay visible even inside a union
        (`Status | list[Status]` shows `{ACTIVE,INACTIVE}`, collapsing to one copy since both sides share
        the same group; `Literal[1, 2] | list[int]` shows `{1,2}|int`, since the sides differ). It falls
        back to `json` only when a member has no single-token CLI form at all (a `dict`, as in
        `dict | list[int]`). `tuple[X, ...]`/`set[X]`/`frozenset[X]`/`Sequence[X]` (see
        `_sequence_elem_type()`) name their own element type exactly like `list[X]` already did, both
        standalone and as the list side of a scalar-or-list union (`int | tuple[int, ...]`), while a bare,
        unparameterized `tuple` and a fixed-length, heterogeneous `tuple[str, int]` (no single element type
        to convert with) both still fall back to `json`. A parameter with no annotation at all shows no
        value type at all (`None`), rather than `json`, since it never claimed a type to show, and this also
        holds when the missing annotation arrives `Annotated`-wrapped (the shape a reserved-name collision
        like a parameter literally named `Literal` produces). An explicit `Any` annotation is deliberately
        not treated the same way and still shows `json`, since it's a type the author did declare
        """
        spec = _arg_spec(annotation, "--flag")
        assert spec.value_type == value_type

    @pytest.mark.parametrize(
        ("annotation", "display_type"),
        [
            (str, "str"),
            (bool, None),
            (list[str], "str[]"),
            (list[dict[str, int]], "json[]"),
            (list[File], "path[]"),
            (tuple[str, ...], "str[]"),
            (tuple[str, int], "json"),
            (list, "json[]"),
            (int | list[str], "int|str[]"),
            (list[int | str], "int|str[]"),
        ],
    )
    def test_display_type_for_annotation(self, annotation: Any, display_type: str | None) -> None:
        """Test that `_ArgSpec.display_type` suffixes a repeatable flag's own `value_type` with `[]`,
        leaving a scalar flag's displayed type (and a flag with none at all) unchanged. A scalar-or-list
        union (`int | list[str]`) keeps the suffix on its whole combined name rather than excluding it, so a
        repeatable flag is never indistinguishable from a scalar one - a deliberate reading, since the same
        rendered name also results from a list of a union element (`list[int | str]`), where `[]` actually
        belongs to the whole union rather than binding to `str` alone
        """
        spec = _arg_spec(annotation, "--flag")
        assert spec.display_type == display_type

    def test_bool_uses_boolean_optional_action_when_the_flag_is_not_no_prefixed(self) -> None:
        """Test that a `bool` annotation uses `argparse.BooleanOptionalAction` as long as its own derived
        flag doesn't already start with `--no-`
        """
        spec = _arg_spec(bool, "--cache")
        assert spec.kwargs == {"action": argparse.BooleanOptionalAction}

    def test_bool_falls_back_to_a_single_flag_when_its_own_flag_is_no_prefixed(self) -> None:
        """Test that a `bool` annotation whose own derived flag already starts with `--no-` uses a plain
        `store_true` flag instead of `argparse.BooleanOptionalAction`, which would otherwise pair it with a
        nonsensical `--no-no-...` negation on Python 3.11-3.13, or raise `ValueError` outright when
        registered on Python 3.14 (see
        `TestAddEndpointArguments.test_bool_param_whose_own_flag_is_no_prefixed_registers_a_single_flag`
        for the end-to-end, command-survival version of this same concern)
        """
        spec = _arg_spec(bool, "--no-cache")
        assert spec.kwargs == {"action": "store_true"}
        assert spec.value_type is None

    @pytest.mark.parametrize(
        "annotation",
        [
            str,
            int,
            float,
            bool,
            list[str],
            list[bool],
            list[dict[str, Any]],
            Status,
            Literal[1, 2],
            File,
            dict,
            str | int,
            int | list[int],
            bool | list[bool],
        ],
    )
    def test_arg_spec_invariants(self, annotation: Any) -> None:
        """Test the two invariants that keep `_arg_spec()`'s displayed value type honest: a named value
        type always has a real conversion mechanism behind it, either a `type=` converter or a
        scalar-or-list/JSON-list `action=` (whose own converters are checked separately by
        `TestScalarOrListAction`/`TestJsonListAction`), and a flag that takes no value at all (bool) is
        never given a displayed value type
        """
        spec = _arg_spec(annotation, "--flag")
        has_converter = "type" in spec.kwargs or spec.kwargs.get("action") not in (
            None,
            argparse.BooleanOptionalAction,
        )
        assert spec.value_type is None or has_converter
        assert spec.kwargs.get("action") is not argparse.BooleanOptionalAction or spec.value_type is None


class TestSequenceElemType:
    """Unit tests for `_sequence_elem_type()`'s bare-collection detection: an unparameterized `list`/
    `tuple`/`set`/`frozenset`/`Sequence` is still repeatable and declares no element type at all
    (`inspect.Parameter.empty`, the same "no type declared" sentinel `_arg_spec()` uses elsewhere), while a
    fixed-length heterogeneous `tuple[str, int]`, a `dict`, and a plain `str` are not sequences at all
    """

    @pytest.mark.parametrize("annotation", [list, tuple, set, frozenset, Sequence, typing.Sequence])
    def test_an_unparameterized_collection_declares_no_element_type(self, annotation: Any) -> None:
        """Test that a bare, unparameterized collection annotation - `collections.abc.Sequence` and
        `typing.Sequence` alike, which `get_origin()` resolves differently from one another when bare -
        returns the "no type declared" sentinel rather than `None`, so the caller still treats it as
        repeatable
        """
        assert _sequence_elem_type(annotation) is inspect.Parameter.empty

    @pytest.mark.parametrize("annotation", [tuple[str, int], dict, str])
    def test_a_non_sequence_shape_returns_none(self, annotation: Any) -> None:
        """Test that a fixed-length heterogeneous tuple, a `dict`, and a plain `str` return `None`, unlike
        an unparameterized collection
        """
        assert _sequence_elem_type(annotation) is None


class TestScalarUnionConverter:
    """Tests for the `type=` converter `_arg_spec()`/`_union_value_spec()` build for a union of members that
    each have a single-token CLI form (`str`/`int`/`float`/`bool`/`Enum`/`Literal`, `Annotated[]`-wrapped or
    not), which lets a CLI value for e.g. `str | int` or `Status | str` be typed plainly (`--foo 42`, `--foo
    ACTIVE`) instead of requiring the JSON-quoted syntax a `dict`/unrestricted union falls back to
    """

    @pytest.mark.parametrize(
        ("annotation", "value", "expected"),
        [
            (str | int, "42", 42),
            (str | int, "bar", "bar"),
            (int | float, "42", 42),
            (int | float, "3.5", 3.5),
            (float | str, "42", 42.0),
            (float | str, "bar", "bar"),
            (Status | str, "ACTIVE", Status.ACTIVE),
            (Status | str, "unknown-member", "unknown-member"),
            (Literal["asc", "desc"] | str, "asc", "asc"),
            (Literal["asc", "desc"] | str, "anything", "anything"),
            (bool | str, "true", True),
            (bool | str, "not-a-bool", "not-a-bool"),
            (Annotated[str, Query()] | int, "42", 42),
            (Annotated[str, Query()] | int, "bar", "bar"),
        ],
    )
    def test_converts_to_the_most_restrictive_matching_type(self, annotation: Any, value: str, expected: Any) -> None:
        """Test that the converter returns the most restrictive type that successfully parses the value,
        regardless of which order the union's members were written in, falling through to a later, less
        restrictive member (ultimately `str`, which never fails) when an earlier one rejects the token.
        Covers a plain scalar union, an `Enum`/`Literal`/`bool` member (each raising instead of returning
        an unmatched raw token when chained, see `_choice_converter(strict=True)`), and an `Annotated[]`-
        wrapped member (unwrapped the same way a standalone parameter's annotation would be)
        """
        convert = _arg_spec(annotation, "--flag").kwargs["type"]
        result = convert(value)
        assert result == expected
        assert type(result) is type(expected)

    def test_raises_a_clear_error_when_no_member_type_parses(self) -> None:
        """Test that a value matching none of a non-str union's member types (here `int | float`, which has
        no str fallback to fall back to) is rejected by argparse itself with a clear error, not an
        uncaught ValueError from the last type tried
        """
        convert = _arg_spec(int | float, "--flag").kwargs["type"]
        with pytest.raises(argparse.ArgumentTypeError):
            convert("not-a-number")

    def test_scalar_union_param_accepts_a_plain_unquoted_value_end_to_end(
        self, scalar_union_api_class: type[BaseAPI]
    ) -> None:
        """Test that a real `str | int` endpoint parameter parses a plain CLI value end-to-end, through
        the actual parser (not just the converter in isolation), without requiring JSON-quoted input
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, scalar_union_api_class.make_thing.endpoint)
        args = parser.parse_args(["--item-id", "bar"])
        assert args.item_id == "bar"
        args = parser.parse_args(["--item-id", "42"])
        assert args.item_id == 42

    def test_three_way_union_with_a_list_member_still_parses_each_side_end_to_end(self) -> None:
        """Test that `int | str | list[int]` (two non-list scalar members plus a list member) parses a
        single token with its scalar side (trying `int` before `str`) and two or more tokens with the
        list side's own element type, end-to-end through the real parser
        """

        class ThreeWayAPI(BaseAPI):
            """A synthetic API class exercising a three-member scalar-or-list union parameter."""

            app_name = "cli-test"

            @endpoint.post("/things")
            def make_thing(self, value: int | str | list[int] = Unset, **kwargs: Unpack[Kwargs]) -> RestResponse:
                """Make a thing"""
                ...

        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, ThreeWayAPI.make_thing.endpoint)
        assert parser.parse_args(["--value", "5"]).value == 5
        assert parser.parse_args(["--value", "bar"]).value == "bar"
        assert parser.parse_args(["--value", "1", "2"]).value == [1, 2]


class TestScalarOrListAction:
    """Tests for `_scalar_or_list_action()`, which lets an `S | list[X]` endpoint parameter (`S` and `X`
    may be the same type, e.g. `int | list[int]`, or different, e.g. `int | list[str]`) accept either a
    bare value or several: a single token converts with `S`'s own type and is stored bare, two or more
    each convert with `X`'s type and are stored as a `list`
    """

    def test_a_single_value_collapses_to_a_bare_scalar(self, scalar_or_list_api_class: type[BaseAPI]) -> None:
        """Test that exactly one value is stored as a bare int, not a one-element list"""
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, scalar_or_list_api_class.make_thing.endpoint)
        args = parser.parse_args(["--ids", "5"])
        assert args.ids == 5

    def test_multiple_values_stay_a_list(self, scalar_or_list_api_class: type[BaseAPI]) -> None:
        """Test that two or more values are stored as a list of int, each converted from its token"""
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, scalar_or_list_api_class.make_thing.endpoint)
        args = parser.parse_args(["--ids", "1", "2", "3"])
        assert args.ids == [1, 2, 3]

    def test_the_flag_given_with_zero_values_is_an_empty_list(self, scalar_or_list_api_class: type[BaseAPI]) -> None:
        """Test that the bare flag with no following values stores an empty list, matching a plain
        optional `list[T]` parameter's own zero-value behavior
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, scalar_or_list_api_class.make_thing.endpoint)
        args = parser.parse_args(["--ids"])
        assert args.ids == []

    def test_a_required_scalar_or_list_param_still_requires_at_least_one_value(self) -> None:
        """Test that a required `T | list[T]` parameter is upgraded from `nargs=\"*\"` to `nargs=\"+\"`,
        the same required-list upgrade a plain `list[T]` parameter already gets, so the bare flag with no
        values is rejected rather than silently accepted as an empty list
        """

        class RequiredScalarOrListAPI(BaseAPI):
            """A synthetic API class exercising a required scalar-or-list parameter."""

            app_name = "cli-test"

            @endpoint.post("/things")
            def make_thing(self, ids: int | list[int], **kwargs: Unpack[Kwargs]) -> RestResponse:
                """Make a thing"""
                ...

        parser = argparse.ArgumentParser(exit_on_error=False)
        add_endpoint_arguments(parser, RequiredScalarOrListAPI.make_thing.endpoint)
        with pytest.raises(argparse.ArgumentError):
            parser.parse_args(["--ids"])
        args = parser.parse_args(["--ids", "5"])
        assert args.ids == 5

    def test_a_single_value_converts_with_the_scalar_sides_own_type(
        self, scalar_or_differing_list_api_class: type[BaseAPI]
    ) -> None:
        """Test that for `int | list[str]`, exactly one value converts with the scalar side's type (int),
        not the list side's element type (str)
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, scalar_or_differing_list_api_class.make_thing.endpoint)
        args = parser.parse_args(["--ids", "5"])
        assert args.ids == 5

    def test_multiple_values_convert_with_the_list_sides_element_type(
        self, scalar_or_differing_list_api_class: type[BaseAPI]
    ) -> None:
        """Test that for `int | list[str]`, two or more values each convert with the list side's element
        type (str), not the scalar side's type (int)
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, scalar_or_differing_list_api_class.make_thing.endpoint)
        args = parser.parse_args(["--ids", "a", "b"])
        assert args.ids == ["a", "b"]

    def test_a_single_value_that_fails_the_scalar_conversion_is_a_clean_argparse_error(
        self, scalar_or_differing_list_api_class: type[BaseAPI]
    ) -> None:
        """Test that a single value which can't convert with the scalar side's type (int) is rejected with
        a clean `invalid int value: ...` error, matching argparse's own native `type=` error wording,
        rather than a bare `type=` converter's raw exception message (e.g. `int()`'s own
        `invalid literal for int() with base 10: ...`) leaking through
        """
        parser = argparse.ArgumentParser(exit_on_error=False)
        add_endpoint_arguments(parser, scalar_or_differing_list_api_class.make_thing.endpoint)
        with pytest.raises(argparse.ArgumentError, match=r"invalid int value: 'not-a-number'"):
            parser.parse_args(["--ids", "not-a-number"])

    def test_one_invalid_list_element_among_several_is_a_clean_argparse_error_naming_the_element_type(
        self,
    ) -> None:
        """Test that, for `str | list[int]`, a multi-value invocation with one non-numeric token is
        rejected with a clean `invalid int value: ...` error naming the list side's element type (int),
        not the scalar side's type (str) - reproducing a real report where `--password 123 test` leaked
        `int()`'s raw message instead of reading like a normal argparse error
        """

        class ScalarStrOrListIntAPI(BaseAPI):
            """A synthetic API class exercising a str-scalar-or-int-list parameter."""

            app_name = "cli-test"

            @endpoint.post("/things")
            def make_thing(self, password: str | list[int] = Unset, **kwargs: Unpack[Kwargs]) -> RestResponse:
                """Make a thing"""
                ...

        parser = argparse.ArgumentParser(exit_on_error=False)
        add_endpoint_arguments(parser, ScalarStrOrListIntAPI.make_thing.endpoint)
        assert parser.parse_args(["--password", "test"]).password == "test"
        assert parser.parse_args(["--password", "1", "2"]).password == [1, 2]
        with pytest.raises(argparse.ArgumentError, match=r"invalid int value: 'test'"):
            parser.parse_args(["--password", "123", "test"])


class TestJsonListAction:
    """Tests for `_JsonListAction`, which lets a repeatable JSON-typed parameter (e.g. `list[dict[str,
    int]]`) accept a sole `-`/`@<path>` indirection as the whole parameter value, rather than reading it
    as a single element the way every other value on the flag is read. The indirection's own document must
    be a JSON array, since it stands for the whole collection.
    """

    @pytest.fixture(autouse=True)
    def _reset_stdin_consumed(self) -> None:
        """Reset the module-level "stdin already read" flag before each test, so test order can't leak
        one test's `-` usage into another's, mirroring `TestParseJson`'s own fixture of the same name
        """
        params_module.reset_stdin_state()

    def test_a_sole_at_path_value_becomes_the_whole_parameter_value(
        self, list_of_dicts_api_class: type[BaseAPI], tmp_path: Path
    ) -> None:
        """Test that a sole `@<path>` value is deserialized and stored as the whole parameter value,
        rather than wrapped in another list the way a `type=`-per-token flag would
        """
        json_file = tmp_path / "items.json"
        json_file.write_text('[{"id": 1}, {"id": 2}]')
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, list_of_dicts_api_class.make_thing.endpoint)
        args = parser.parse_args(["--items", f"@{json_file}"])
        assert args.items == [{"id": 1}, {"id": 2}]

    def test_a_sole_dash_value_reads_the_whole_parameter_value_from_stdin(
        self, list_of_dicts_api_class: type[BaseAPI], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that a sole `-` value reads and stores the whole parameter value from stdin, the same as
        a sole `@<path>` value does from a file
        """
        monkeypatch.setattr(sys, "stdin", io.StringIO('[{"id": 1}, {"id": 2}]'))
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, list_of_dicts_api_class.make_thing.endpoint)
        args = parser.parse_args(["--items", "-"])
        assert args.items == [{"id": 1}, {"id": 2}]

    @pytest.mark.parametrize(
        ("document", "type_name"),
        [
            pytest.param('{"id": 1}', "an object", id="object"),
            pytest.param('"hello"', "a string", id="string"),
            pytest.param("1", "a number", id="int"),
            pytest.param("1.5", "a number", id="float"),
            pytest.param("true", "a boolean", id="boolean"),
            pytest.param("null", "null", id="null"),
        ],
    )
    def test_a_sole_at_path_value_that_is_not_an_array_is_rejected(
        self, list_of_dicts_api_class: type[BaseAPI], tmp_path: Path, document: str, type_name: str
    ) -> None:
        """Test that a file holding a JSON document other than an array is rejected with a clean
        `ArgumentError` naming both the indirection and the document's own shape, rather than being stored
        as that bare document
        """
        json_file = tmp_path / "items.json"
        json_file.write_text(document)
        parser = argparse.ArgumentParser(exit_on_error=False)
        add_endpoint_arguments(parser, list_of_dicts_api_class.make_thing.endpoint)
        with pytest.raises(argparse.ArgumentError, match=f"must be an array, not {type_name}"):
            parser.parse_args(["--items", f"@{json_file}"])

    def test_a_sole_dash_value_that_is_not_an_array_is_rejected(
        self, list_of_dicts_api_class: type[BaseAPI], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that a `-` value reading a JSON object from stdin is rejected the same way a file holding
        one is, since both forms are the same whole-value indirection
        """
        monkeypatch.setattr(sys, "stdin", io.StringIO('{"id": 1}'))
        parser = argparse.ArgumentParser(exit_on_error=False)
        add_endpoint_arguments(parser, list_of_dicts_api_class.make_thing.endpoint)
        with pytest.raises(argparse.ArgumentError, match="must be an array, not an object"):
            parser.parse_args(["--items", "-"])

    def test_a_sole_at_path_empty_array_is_accepted(
        self, list_of_dicts_api_class: type[BaseAPI], tmp_path: Path
    ) -> None:
        """Test that a file holding an empty JSON array is still accepted as the whole parameter value,
        rather than being rejected: it's a valid, if empty, array
        """
        json_file = tmp_path / "items.json"
        json_file.write_text("[]")
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, list_of_dicts_api_class.make_thing.endpoint)
        args = parser.parse_args(["--items", f"@{json_file}"])
        assert args.items == []

    def test_a_lone_inline_json_object_stays_one_element(self, list_of_dicts_api_class: type[BaseAPI]) -> None:
        """Test that a single inline (non-indirection) object token is still read as one element, unaffected
        by the array requirement that applies only to a `-`/`@<path>` indirection's own document
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, list_of_dicts_api_class.make_thing.endpoint)
        args = parser.parse_args(["--items", '{"id": 1}'])
        assert args.items == [{"id": 1}]

    def test_a_lone_inline_json_array_stays_one_element(self, list_of_dicts_api_class: type[BaseAPI]) -> None:
        """Test that a single inline (non-indirection) token is still read as one element, even when it is
        itself a JSON array, so it nests rather than being flattened - only `-`/`@<path>` change to a
        whole-value reading, matching the still-nesting `list[list[int]]` behavior pinned elsewhere
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, list_of_dicts_api_class.make_thing.endpoint)
        args = parser.parse_args(["--items", '[{"id": 1}, {"id": 2}]'])
        assert args.items == [[{"id": 1}, {"id": 2}]]

    def test_an_at_path_value_combined_with_another_value_is_rejected(
        self, list_of_dicts_api_class: type[BaseAPI], tmp_path: Path
    ) -> None:
        """Test that `@<path>` given alongside another value on the same flag is rejected, since the
        indirection supplies the whole value and can't also stand for one element next to another
        """
        json_file = tmp_path / "items.json"
        json_file.write_text('{"id": 2}')
        parser = argparse.ArgumentParser(exit_on_error=False)
        add_endpoint_arguments(parser, list_of_dicts_api_class.make_thing.endpoint)
        with pytest.raises(argparse.ArgumentError, match="cannot be combined with other values"):
            parser.parse_args(["--items", '{"id": 1}', f"@{json_file}"])

    def test_a_dash_combined_with_another_value_is_rejected_without_consuming_stdin(
        self, list_of_dicts_api_class: type[BaseAPI], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that `-` given alongside another value is rejected before stdin is read, so a later, sole
        `-` in the same command still gets its own payload rather than reading empty content left behind
        by the rejected combination
        """
        monkeypatch.setattr(sys, "stdin", io.StringIO('[{"id": 1}]'))
        parser = argparse.ArgumentParser(exit_on_error=False)
        add_endpoint_arguments(parser, list_of_dicts_api_class.make_thing.endpoint)
        with pytest.raises(argparse.ArgumentError, match="cannot be combined with other values"):
            parser.parse_args(["--items", '{"id": 0}', "-"])
        args = parser.parse_args(["--items", "-"])
        assert args.items == [{"id": 1}]

    def test_malformed_json_is_a_clean_argparse_error(self, list_of_dicts_api_class: type[BaseAPI]) -> None:
        """Test that malformed inline JSON is rejected as a clean `ArgumentError`, rather than the raw
        `ArgumentTypeError` `_parse_json()` itself raises escaping `parse_args()` uncaught
        """
        parser = argparse.ArgumentParser(exit_on_error=False)
        add_endpoint_arguments(parser, list_of_dicts_api_class.make_thing.endpoint)
        with pytest.raises(argparse.ArgumentError, match="invalid JSON"):
            parser.parse_args(["--items", "not json"])

    def test_an_unreadable_at_path_is_a_clean_argparse_error(
        self, list_of_dicts_api_class: type[BaseAPI], tmp_path: Path
    ) -> None:
        """Test that a nonexistent `@<path>` file is rejected as a clean `ArgumentError` naming the path"""
        missing = tmp_path / "does-not-exist.json"
        parser = argparse.ArgumentParser(exit_on_error=False)
        add_endpoint_arguments(parser, list_of_dicts_api_class.make_thing.endpoint)
        with pytest.raises(argparse.ArgumentError, match="cannot read"):
            parser.parse_args(["--items", f"@{missing}"])

    def test_a_non_utf8_at_path_is_a_clean_argparse_error(
        self, list_of_dicts_api_class: type[BaseAPI], tmp_path: Path
    ) -> None:
        """Test that an `@<path>` file that isn't valid UTF-8 text is rejected as a clean `ArgumentError`
        naming the path, rather than an uncaught `UnicodeDecodeError` escaping `parse_args()`
        """
        json_file = tmp_path / "items.json"
        json_file.write_bytes(b"\xff\xfe\x00")
        parser = argparse.ArgumentParser(exit_on_error=False)
        add_endpoint_arguments(parser, list_of_dicts_api_class.make_thing.endpoint)
        with pytest.raises(argparse.ArgumentError, match="cannot read"):
            parser.parse_args(["--items", f"@{json_file}"])

    def test_a_required_json_list_still_requires_at_least_one_value(
        self, required_json_list_api_class: type[BaseAPI]
    ) -> None:
        """Test that a required `list[dict[...]]` parameter is still upgraded from `nargs=\"*\"` to
        `nargs=\"+\"`, the same required-list upgrade a plain `list[str]` parameter already gets, so the
        bare flag with no values is rejected rather than silently accepted as an empty list.
        Stock `argparse`'s own "required arguments" check only honors `exit_on_error=False` from Python
        3.12 on, still calling `self.error()` (a `SystemExit`) on 3.11, so both exception types are accepted
        here, matching the same two-type catch `peek_log_level()` already uses for this reason
        """
        parser = argparse.ArgumentParser(exit_on_error=False)
        add_endpoint_arguments(parser, required_json_list_api_class.make_thing.endpoint)
        with pytest.raises((argparse.ArgumentError, SystemExit)):
            parser.parse_args([])

    def test_the_flag_given_with_zero_values_is_an_empty_list(self, list_of_dicts_api_class: type[BaseAPI]) -> None:
        """Test that the bare optional flag with no following values stores an empty list, matching a
        plain `list[str]` parameter's own zero-value behavior
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, list_of_dicts_api_class.make_thing.endpoint)
        args = parser.parse_args(["--items"])
        assert args.items == []

    def test_an_omitted_flag_stays_not_provided(self, list_of_dicts_api_class: type[BaseAPI]) -> None:
        """Test that an omitted flag is left as `NOT_PROVIDED`, confirming the sentinel default survives a
        custom `action=` the same way it does a plain `type=` converter
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, list_of_dicts_api_class.make_thing.endpoint)
        args = parser.parse_args([])
        assert args.items is NOT_PROVIDED

    def test_a_sole_at_path_value_round_trips_through_collect_call_kwargs(
        self, list_of_dicts_api_class: type[BaseAPI], tmp_path: Path
    ) -> None:
        """Test that the whole document read from a sole `@<path>` value reaches the call kwargs
        untouched, in particular that `collect_call_kwargs()`'s own `Path`-to-`File` list mapping leaves a
        list of plain dicts alone rather than mistaking one for a `Path`
        """
        json_file = tmp_path / "items.json"
        json_file.write_text('[{"id": 1}, {"id": 2}]')
        endpoint = list_of_dicts_api_class.make_thing.endpoint
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, endpoint)
        namespace = parser.parse_args(["--items", f"@{json_file}"])
        assert collect_call_kwargs(endpoint, namespace) == {"items": [{"id": 1}, {"id": 2}]}


class TestListMetavar:
    """Tests for the shared `VALUE` metavar every repeatable (`nargs=\"*\"`/`\"+\"`) flag gets when its own
    type maps to no explicit metavar, naming the single value each repetition takes rather than the
    parameter's own, often-plural name
    """

    def test_json_list_param_shows_the_value_metavar(self, list_of_dicts_api_class: type[BaseAPI]) -> None:
        """Test that a `list[dict[...]]` param's metavar is `VALUE`, not the dest-derived `ITEMS`"""
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, list_of_dicts_api_class.make_thing.endpoint)
        action = next(a for a in parser._actions if a.dest == "items")
        assert action.metavar == "VALUE"

    def test_str_list_param_shows_the_value_metavar(self, required_list_api_class: type[BaseAPI]) -> None:
        """Test that a `list[str]` param's metavar is also `VALUE`, not the dest-derived `TAGS`"""
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, required_list_api_class.make_thing.endpoint)
        action = next(a for a in parser._actions if a.dest == "tags")
        assert action.metavar == "VALUE"

    def test_scalar_or_list_param_shows_the_value_metavar(self, scalar_or_list_api_class: type[BaseAPI]) -> None:
        """Test that an `int | list[int]` param's metavar is also `VALUE`, not the dest-derived `IDS`"""
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, scalar_or_list_api_class.make_thing.endpoint)
        action = next(a for a in parser._actions if a.dest == "ids")
        assert action.metavar == "VALUE"

    def test_a_type_supplied_metavar_is_not_overwritten(self, list_file_api_class: type[BaseAPI]) -> None:
        """Test that a `list[File]` param keeps its own `PATH` metavar rather than being overwritten by
        the shared `VALUE` default, since its type already supplies a more specific one
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, list_file_api_class.make_thing.endpoint)
        action = next(a for a in parser._actions if a.dest == "attachments")
        assert action.metavar == "PATH"

    def test_a_scalar_param_is_not_given_a_list_metavar(self, widgets_api_class: type[WidgetsAPI]) -> None:
        """Test that a scalar (non-repeatable) param is left with no explicit metavar, so argparse still
        derives one from its own dest, rather than the shared `VALUE` default leaking onto every flag
        """
        parser = _build_parser("create_widget", widgets_api_class)
        action = next(a for a in parser._actions if a.dest == "name")
        assert action.metavar is None

    def test_the_usage_line_renders_the_shared_value_metavar(self, required_list_api_class: type[BaseAPI]) -> None:
        """Test that the rendered usage line itself shows `VALUE`, not just the action's own attribute"""
        parser = argparse.ArgumentParser(prog="make-thing")
        add_endpoint_arguments(parser, required_list_api_class.make_thing.endpoint)
        assert "--tags VALUE [VALUE ...]" in parser.format_usage()


class TestFileOrStrUnion:
    """Tests for a `File | str` endpoint parameter, which must accept either an existing file path or a
    plain string value rather than hard-requiring a real file the way any File-containing union used to
    """

    def test_accepts_an_existing_file_path(self, file_or_str_api_class: type[BaseAPI], tmp_path: Path) -> None:
        """Test that an existing file path still converts to that `Path`, the same as a bare `File` param"""
        attachment = tmp_path / "attachment.txt"
        attachment.write_bytes(b"data")
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, file_or_str_api_class.make_thing.endpoint)
        args = parser.parse_args(["--attachment", str(attachment)])
        assert args.attachment == attachment

    def test_accepts_a_plain_string_that_is_not_an_existing_path(self, file_or_str_api_class: type[BaseAPI]) -> None:
        """Test that a value which isn't an existing file path falls through to a plain string, rather
        than being rejected the way a bare `File` param's own converter would reject it
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, file_or_str_api_class.make_thing.endpoint)
        args = parser.parse_args(["--attachment", "not-a-real-path"])
        assert args.attachment == "not-a-real-path"


class TestAcceptsFilePath:
    """Tests for `accepts_file_path()`, which drives shell path completion for a flag whose value(s) may
    be a filesystem path
    """

    @pytest.mark.parametrize(
        ("api_class_fixture_name", "endpoint_func_name", "dest", "expected"),
        [
            ("widgets_api_class", "upload_avatar", "avatar", True),
            ("list_file_api_class", "make_thing", "attachments", True),
            ("file_or_str_api_class", "make_thing", "attachment", True),
            ("file_or_list_file_api_class", "make_thing", "attachments", True),
            ("widgets_api_class", "create_widget", "name", False),
        ],
    )
    def test_marks_a_file_accepting_action_and_only_a_file_accepting_action(
        self,
        request: pytest.FixtureRequest,
        api_class_fixture_name: str,
        endpoint_func_name: str,
        dest: str,
        expected: bool,
    ) -> None:
        """Test that a bare `File` param, a `list[File]` param, a `File | str` param's chained converter
        (even though it isn't `_existing_file` itself), and a `File | list[File]` param's scalar-or-list
        action (even though it has no `type=` converter at all) are all marked as accepting a file path,
        while an ordinary `str` param's action is not
        """
        api_class: type[BaseAPI] = request.getfixturevalue(api_class_fixture_name)
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, getattr(api_class, endpoint_func_name).endpoint)
        action = next(a for a in parser._actions if a.dest == dest)
        assert accepts_file_path(action) is expected

    def test_path_like_converters_registry_was_removed(self) -> None:
        """Test that `_PATH_LIKE_CONVERTERS`, the unbounded global identity registry `accepts_file_path()`
        used to consult, no longer exists: path-ness is now answered solely by the per-action attribute
        `add_endpoint_arguments()` sets from `_ArgSpec.accepts_file_path`, so no converter closure is kept
        alive in a module-global set forever
        """
        assert not hasattr(params_module, "_PATH_LIKE_CONVERTERS")


class TestAcceptsJsonFile:
    """Tests for `accepts_json_file()`, which drives shell `@<path>` completion for a JSON-typed flag"""

    @pytest.mark.parametrize(
        ("endpoint_func_name", "dest", "expected"),
        [
            ("create_widget", "metadata", True),
            ("create_widget", "name", False),
            ("upload_avatar", "avatar", False),
        ],
    )
    def test_marks_a_json_typed_action_and_only_a_json_typed_action(
        self, widgets_api_class: type[WidgetsAPI], endpoint_func_name: str, dest: str, expected: bool
    ) -> None:
        """Test that a `dict`-typed param is marked as JSON-file-accepting, while an ordinary `str` param
        and a `File` param (accepts a path directly, not via `@<path>`) are not
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, getattr(widgets_api_class, endpoint_func_name).endpoint)
        action = next(a for a in parser._actions if a.dest == dest)
        assert accepts_json_file(action) is expected

    def test_marks_an_unannotated_param_too(self, unannotated_api_class: type[BaseAPI]) -> None:
        """Test that a parameter with no annotation at all is also marked as JSON-file-accepting, since its
        flag is still JSON-parsed even though it shows no value type
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, unannotated_api_class.make_thing.endpoint)
        action = next(a for a in parser._actions if a.dest == "param")
        assert accepts_json_file(action) is True

    def test_marks_a_repeatable_json_typed_action_too(self, list_of_dicts_api_class: type[BaseAPI]) -> None:
        """Test that a `list[dict[...]]` param's flag is also marked as JSON-file-accepting, even though it
        uses `_JsonListAction` rather than a plain `type=_parse_json` converter
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, list_of_dicts_api_class.make_thing.endpoint)
        action = next(a for a in parser._actions if a.dest == "items")
        assert accepts_json_file(action) is True


class TestLiteralBoolChoices:
    """Regression tests for a `Literal[True, False]` parameter's choice-token spelling"""

    def test_metavar_uses_true_false_tokens_not_python_repr(self, literal_bool_api_class: type[BaseAPI]) -> None:
        """Test that the flag's metavar shows `{true,false}`, matching what its own converter actually
        accepts, rather than argparse's default `{True,False}` derived from the raw Python values
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, literal_bool_api_class.make_thing.endpoint)
        flag_action = next(a for a in parser._actions if a.dest == "flag")
        assert flag_action.metavar == "{true,false}"

    def test_lowercase_token_parses_to_the_matching_bool(self, literal_bool_api_class: type[BaseAPI]) -> None:
        """Test that `--flag true` parses to `True`, the only token spelling the metavar advertises"""
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, literal_bool_api_class.make_thing.endpoint)
        args = parser.parse_args(["--flag", "true"])
        assert args.flag is True

    def test_python_repr_spelling_is_rejected(self, literal_bool_api_class: type[BaseAPI]) -> None:
        """Test that `--flag True` (Python's own bool repr, not a valid CLI token) is rejected by argparse
        itself with a clean usage error, rather than being silently accepted
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, literal_bool_api_class.make_thing.endpoint)
        with pytest.raises(SystemExit):
            parser.parse_args(["--flag", "True"])


class TestFormatChoiceGroup:
    """Tests for `_format_choice_group()`, which renders a Literal/Enum's choices as one brace-delimited
    group for the CLI value-type column
    """

    def test_renders_every_token_when_the_full_group_fits(self) -> None:
        """Test that a short choice list renders in full, with no elision"""
        assert _format_choice_group(["a", "b", "c"]) == "{a,b,c}"

    def test_elides_past_the_width_cap(self) -> None:
        """Test that a choice list wider than the cap keeps as many whole leading tokens as fit,
        appending a trailing `,…` marker rather than truncating mid-token or dropping the overflow count
        silently
        """
        choices = [f"CHOICE_{i}" for i in range(10)]
        result = _format_choice_group(choices)
        assert result.endswith(",…}")
        assert len(result) <= _MAX_CHOICE_GROUP_WIDTH
        assert "CHOICE_9" not in result

    def test_keeps_at_least_one_token_even_if_it_alone_exceeds_the_width_cap(self) -> None:
        """Test that a single very long choice is still rendered whole, rather than the group collapsing
        to an empty `{}`
        """
        result = _format_choice_group(["A_VERY_LONG_CHOICE_TOKEN_THAT_ALONE_EXCEEDS_THE_CAP"])
        assert result == "{A_VERY_LONG_CHOICE_TOKEN_THAT_ALONE_EXCEEDS_THE_CAP}"


class TestFullMetavar:
    """Tests for `_arg_spec()`'s `full_metavar`: a Literal/Enum's un-elided companion to its own (possibly
    `_MAX_CHOICE_GROUP_WIDTH`-elided) `metavar`, wired by `add_endpoint_arguments()` onto the action for
    `_HelpFormatter` to show in the elided one's place under `--help`
    """

    def test_a_wide_literal_gets_a_full_metavar_listing_every_choice(self) -> None:
        """Test that a Literal wide enough to elide its own `metavar` still gets a `full_metavar` listing
        every choice, unelided
        """
        choices = tuple(f"CHOICE_{i}" for i in range(10))
        spec = _arg_spec(Literal[choices], "--flag")
        assert spec.kwargs["metavar"] != spec.full_metavar
        assert spec.full_metavar == "{" + ",".join(choices) + "}"

    def test_a_short_literal_still_gets_a_full_metavar(self) -> None:
        """Test that a Literal short enough to need no elision still gets a `full_metavar`, matching its
        own `metavar` exactly
        """
        spec = _arg_spec(Literal["a", "b", "c"], "--flag")
        assert spec.full_metavar == spec.kwargs["metavar"] == "{a,b,c}"

    def test_standalone_enum_gets_a_full_metavar(self) -> None:
        """Test that a standalone (non-union) Enum field also gets a `full_metavar`, the same way a
        standalone Literal does
        """
        spec = _arg_spec(Status, "--flag")
        assert spec.full_metavar == spec.kwargs["metavar"] == "{ACTIVE,INACTIVE}"

    def test_a_union_members_choices_carry_no_full_metavar(self) -> None:
        """Test that a Literal/Enum used as one member of a union carries no `full_metavar`, matching
        `_union_value_spec()`'s own choice to not set a `metavar` for a union at all
        """
        spec = _arg_spec(Status | str, "--flag")
        assert spec.full_metavar is None

    def test_add_endpoint_arguments_wires_the_full_metavar_onto_the_action(
        self, widgets_api_class: type[WidgetsAPI]
    ) -> None:
        """Test that `add_endpoint_arguments()` attaches the full metavar onto the action itself, so
        `_HelpFormatter` can substitute it in under `--help`
        """
        parser = _build_parser("create_widget", widgets_api_class)
        action = next(a for a in parser._actions if a.dest == "status")
        assert full_metavar(action) == "{ACTIVE,INACTIVE}"


class TestUnionChoiceVisibility:
    """Tests for keeping a union member's own Enum/Literal choices visible in the CLI value-type column,
    rather than the union's combined display collapsing them down to the shared scalar type name
    """

    def test_status_or_str_shows_the_enum_choices_in_the_type_column(
        self, status_or_str_api_class: type[BaseAPI]
    ) -> None:
        """Test that a `Status | str` parameter's help shows `{ACTIVE,INACTIVE}|str`, keeping the enum's
        own choices visible rather than collapsing to the shared `str` display name
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, status_or_str_api_class.make_thing.endpoint)
        status_action = next(a for a in parser._actions if a.dest == "status")
        assert status_action.help is not None
        assert color("{ACTIVE,INACTIVE}|str", color_code=ColorCodes.DARK_GREY) in status_action.help

    def test_status_or_str_converts_a_matching_token_to_the_enum_member(
        self, status_or_str_api_class: type[BaseAPI]
    ) -> None:
        """Test that `--status ACTIVE` still converts to the enum member, and a non-matching token falls
        through to the plain string, matching the union's own chained conversion order
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, status_or_str_api_class.make_thing.endpoint)
        assert parser.parse_args(["--status", "ACTIVE"]).status is Status.ACTIVE
        assert parser.parse_args(["--status", "custom"]).status == "custom"

    def test_standalone_enum_still_shows_only_its_shared_type_name(self, widgets_api_class: type[WidgetsAPI]) -> None:
        """Test that a standalone (non-union) Enum param's help still shows plain `str` in the type
        column, since its own metavar already carries the choices, rather than a redundant choice group
        """
        parser = _build_parser("create_widget", widgets_api_class)
        status_action = next(a for a in parser._actions if a.dest == "status")
        assert status_action.help is not None
        assert color("str", color_code=ColorCodes.DARK_GREY) in status_action.help
        assert "{ACTIVE,INACTIVE}" not in remove_color_code(status_action.help)


class TestStringlyTypedParameters:
    """Tests for `datetime`/`date`/`UUID`/`Decimal`/str-subclass parameters, which resolve to a plain
    `str`-typed CLI flag rather than falling back to JSON
    """

    @pytest.mark.parametrize("dest", ["created_at", "due_date", "request_id", "amount", "label"])
    def test_shows_str_type_and_accepts_a_bare_token(self, stringly_typed_api_class: type[BaseAPI], dest: str) -> None:
        """Test that each stringly-typed parameter's help shows `str` and accepts a bare, unquoted CLI
        token, rather than requiring JSON-quoted input the way the fallback would
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, stringly_typed_api_class.make_thing.endpoint)
        action = next(a for a in parser._actions if a.dest == dest)
        assert action.help is not None
        assert color("str", color_code=ColorCodes.DARK_GREY) in action.help
        flag = action.option_strings[0]
        args = parser.parse_args([flag, "some-value"])
        assert getattr(args, dest) == "some-value"

    def test_str_based_enum_still_resolves_as_an_enum(self, str_enum_api_class: type[BaseAPI]) -> None:
        """Test that a `class Status(str, Enum)` parameter still converts a member-name token to the
        actual enum member, rather than the new str-subclass handling shadowing Enum resolution
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, str_enum_api_class.make_thing.endpoint)
        args = parser.parse_args(["--status", "ACTIVE"])
        assert isinstance(args.status, Enum)
        assert args.status.name == "ACTIVE"

    def test_plain_custom_class_still_falls_back_to_json(self, plain_class_api_class: type[BaseAPI]) -> None:
        """Test that a plain custom class (no generic origin, not str/Enum/File) still falls back to the
        `json` CLI value type, rather than the new stringly-typed handling sweeping it up too
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, plain_class_api_class.make_thing.endpoint)
        payload_action = next(a for a in parser._actions if a.dest == "payload")
        assert payload_action.help is not None
        assert color("json", color_code=ColorCodes.DARK_GREY) in payload_action.help


class TestPerMemberAnnotatedScalarOrList:
    """Regression tests for a scalar-or-list union whose members each carry their own
    `Annotated[..., Query()]` wrapping, rather than the single outer-`Annotated` form
    `param_type_util.annotate_type()` itself produces
    """

    def test_accepts_multiple_values_despite_the_per_member_annotated_wrapping(
        self, per_member_annotated_scalar_or_list_api_class: type[BaseAPI]
    ) -> None:
        """Test that `--tags a b` still parses to a list, rather than falling back to JSON because
        `get_origin()` on the raw (still-`Annotated`) member wasn't `list`
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, per_member_annotated_scalar_or_list_api_class.make_thing.endpoint)
        args = parser.parse_args(["--tags", "a", "b"])
        assert args.tags == ["a", "b"]

    def test_a_single_value_stays_a_bare_scalar(
        self, per_member_annotated_scalar_or_list_api_class: type[BaseAPI]
    ) -> None:
        """Test that a single `--tags` value stays a bare string rather than a one-element list"""
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, per_member_annotated_scalar_or_list_api_class.make_thing.endpoint)
        args = parser.parse_args(["--tags", "a"])
        assert args.tags == "a"


class TestSequenceContainerParams:
    """End-to-end tests for `tuple[X, ...]`/`set[X]`/`Sequence[X]` parameters getting the same multi-value
    CLI treatment as `list[X]` (see `_sequence_elem_type()`), and for a fixed-length, heterogeneous tuple
    staying a single JSON-typed flag since it has no one element type to convert CLI tokens with
    """

    @pytest.mark.parametrize("dest", ["tags", "codes", "labels"])
    def test_accepts_multiple_values_and_shows_the_element_type(
        self, sequence_container_api_class: type[BaseAPI], dest: str
    ) -> None:
        """Test that `tuple[str, ...]`/`set[int]`/`Sequence[str]` each accept multiple CLI tokens and show
        their own element type, exactly like `list[X]` already does
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, sequence_container_api_class.make_thing.endpoint)
        action = next(a for a in parser._actions if a.dest == dest)
        assert action.nargs == "*"
        assert action.help is not None

    def test_multiple_tuple_values_round_trip_through_collect_call_kwargs(
        self, sequence_container_api_class: type[BaseAPI]
    ) -> None:
        """Test that `--tags a b` parses to a plain `list` and round-trips through `collect_call_kwargs()`
        unchanged, the same value shape the framework already accepts for a `list[X]`-typed parameter
        """
        ep = sequence_container_api_class.make_thing.endpoint
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, ep)
        args = parser.parse_args(["--tags", "a", "b"])
        assert args.tags == ["a", "b"]
        assert collect_call_kwargs(ep, args)["tags"] == ["a", "b"]

    def test_set_element_type_still_converts(self, sequence_container_api_class: type[BaseAPI]) -> None:
        """Test that `set[int]`'s own element type still converts each CLI token, not just `list[X]`'s"""
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, sequence_container_api_class.make_thing.endpoint)
        args = parser.parse_args(["--codes", "1", "2"])
        assert args.codes == [1, 2]

    def test_fixed_length_heterogeneous_tuple_stays_a_single_json_flag(
        self, sequence_container_api_class: type[BaseAPI]
    ) -> None:
        """Test that `tuple[str, int]` (fixed-length, heterogeneous) is not swept up by the new
        `tuple[X, ...]` handling: it has no single element type to convert with, so it stays a single
        JSON-typed flag rather than `nargs='*'`
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, sequence_container_api_class.make_thing.endpoint)
        action = next(a for a in parser._actions if a.dest == "pair")
        assert action.nargs is None
        assert action.help is not None
        assert color("json", color_code=ColorCodes.DARK_GREY) in action.help


class TestUnparameterizedCollectionParams:
    """End-to-end tests for unparameterized collection parameters (bare `list`/`tuple`, declaring no
    element type at all), which get the same multi-value CLI treatment as `list[X]` rather than degrading to
    a single JSON-document flag. Each token is JSON-parsed with a plain-string fallback
    (`_LenientJsonListAction`), the same leniency an unannotated scalar parameter already gets, rather than
    `list[dict[str, Any]]`'s strict, JSON-only element parse.
    """

    @pytest.mark.parametrize("dest", ["ids", "pair"])
    def test_accepts_multiple_values_and_shows_the_repeatable_json_type(
        self, bare_collection_api_class: type[BaseAPI], dest: str
    ) -> None:
        """Test that a bare `list`/`tuple` parameter accepts multiple CLI tokens and its help shows `json[]`"""
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, bare_collection_api_class.make_thing.endpoint)
        action = next(a for a in parser._actions if a.dest == dest)
        assert action.nargs == "*"
        assert action.help is not None
        assert "json[]" in remove_color_code(action.help).split()

    def test_each_token_is_json_parsed_with_a_string_fallback(self, bare_collection_api_class: type[BaseAPI]) -> None:
        """Test that a numeric token converts to a JSON number and a boolean-looking token to a JSON
        boolean, while a bare word stays a plain string, all within the same call - the leniency that
        distinguishes this from `list[dict[str, Any]]`'s strict, JSON-only elements
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, bare_collection_api_class.make_thing.endpoint)
        args = parser.parse_args(["--ids", "1", "hello", "true"])
        assert args.ids == [1, "hello", True]

    def test_a_malformed_json_opener_token_is_still_rejected(self, bare_collection_api_class: type[BaseAPI]) -> None:
        """Test that a token unambiguously opening a JSON document (e.g. an unterminated `{`) is still
        rejected as malformed JSON rather than silently kept as a literal string, the same rule
        `_parse_json_or_str()` already applies to an unannotated scalar parameter
        """
        parser = argparse.ArgumentParser(exit_on_error=False)
        add_endpoint_arguments(parser, bare_collection_api_class.make_thing.endpoint)
        with pytest.raises(argparse.ArgumentError, match="invalid JSON"):
            parser.parse_args(["--ids", "{"])

    def test_a_sole_at_path_value_becomes_the_whole_parameter_value(
        self, bare_collection_api_class: type[BaseAPI], tmp_path: Path
    ) -> None:
        """Test that a sole `@<path>` value reads and stores the whole parameter value, the same
        whole-value indirection a strict JSON-list parameter already gets
        """
        json_file = tmp_path / "ids.json"
        json_file.write_text("[1, 2, 3]")
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, bare_collection_api_class.make_thing.endpoint)
        args = parser.parse_args(["--ids", f"@{json_file}"])
        assert args.ids == [1, 2, 3]

    def test_the_flag_given_with_zero_values_is_an_empty_list(self, bare_collection_api_class: type[BaseAPI]) -> None:
        """Test that the bare optional flag with no following values stores an empty list, matching a
        strict JSON-list parameter's own zero-value behavior
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, bare_collection_api_class.make_thing.endpoint)
        args = parser.parse_args(["--ids"])
        assert args.ids == []

    def test_none_default_still_renders_as_json_syntax(self, bare_collection_api_class: type[BaseAPI]) -> None:
        """Test that `_ArgSpec.is_json` still recognizes `_LenientJsonListAction` as a subclass of
        `_JsonListAction`, so a `None` default on a bare `list | None` parameter shows `(default: null)`
        rather than the untypeable `(default: None)`
        """
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, bare_collection_api_class.make_thing.endpoint)
        legacy_action = next(a for a in parser._actions if a.dest == "legacy")
        assert legacy_action.help is not None
        assert "(default: null)" in legacy_action.help


class TestUnionTypeWidthElision:
    """Tests for `_union_value_spec()`/`_scalar_or_list_spec()` capping a union's own combined type-column
    name to `_MAX_UNION_TYPE_WIDTH`, mirroring `_format_choice_group()`'s cap for a single Literal/Enum
    choice group (see `TestFormatChoiceGroup`), so a parameter with many union members can't blow out its
    endpoint's shared type-column width (`add_endpoint_arguments()`'s `type_width`)
    """

    def test_a_union_within_the_width_cap_is_not_elided(self) -> None:
        """Test that an ordinary short union (well under `_MAX_UNION_TYPE_WIDTH`) renders in full"""
        spec = _arg_spec(Status | str, "--flag")
        assert spec.value_type == "{ACTIVE,INACTIVE}|str"

    def test_a_join_landing_exactly_at_the_width_cap_is_not_elided(self) -> None:
        """Test that a join whose full length lands exactly at `_MAX_UNION_TYPE_WIDTH` still renders in
        full, not just one under it (`_elide_joined()`'s own boundary is `<=`, not `<`)
        """
        token = "a" * _MAX_UNION_TYPE_WIDTH
        result = params_module._elide_joined((token,), sep="|", max_width=_MAX_UNION_TYPE_WIDTH)
        assert result == token
        assert not result.endswith("…")

    def test_a_wide_union_elides_past_the_width_cap(self) -> None:
        """Test that a union with enough members to exceed `_MAX_UNION_TYPE_WIDTH` elides to a trailing
        `…` marker, and the rendered result never exceeds the cap"""

        class Palette(Enum):
            RED = "red"
            GREEN = "green"
            BLUE = "blue"

        wide = Palette | Literal["yes", "no", "maybe"] | int | float | str
        spec = _arg_spec(wide, "--flag")
        assert spec.value_type is not None
        assert spec.value_type.endswith("…")
        assert len(spec.value_type) <= _MAX_UNION_TYPE_WIDTH

    def test_a_wide_scalar_or_list_union_elides_past_the_width_cap(self) -> None:
        """Test that `_scalar_or_list_spec()`'s own combined name is capped the same way, not just
        `_union_value_spec()`'s
        """

        class Palette(Enum):
            RED = "red"
            GREEN = "green"
            BLUE = "blue"

        wide = Palette | Literal["yes", "no", "maybe"] | int | float | list[str]
        spec = _arg_spec(wide, "--flag")
        assert spec.value_type is not None
        assert spec.value_type.endswith("…")
        assert len(spec.value_type) <= _MAX_UNION_TYPE_WIDTH


class TestParseJson:
    """Tests for `_parse_json()`, the JSON-fallback `type=` converter, including its `@<path>`/`-` (stdin)
    indirection
    """

    @pytest.fixture(autouse=True)
    def _reset_stdin_consumed(self) -> None:
        """Reset the module-level "stdin already read" flag before each test, so test order can't leak
        one test's `-` usage into another's. Exercises the same `reset_stdin_state()` `runner.py`'s `run()`
        itself calls before every real dispatch, rather than reaching into the module's private state directly
        """
        params_module.reset_stdin_state()

    def test_parses_an_inline_value(self) -> None:
        """Test that a plain (non `-`/`@`) value is parsed as JSON directly"""
        assert _parse_json('{"a": 1}') == {"a": 1}

    def test_rejects_malformed_inline_json(self) -> None:
        """Test that malformed inline JSON raises ArgumentTypeError naming the offending value"""
        with pytest.raises(argparse.ArgumentTypeError) as exc_info:
            _parse_json("not json")
        assert "not json" in str(exc_info.value)

    def test_reads_from_a_file_given_as_at_path(self, tmp_path: Path) -> None:
        """Test that an `@<path>` value reads its JSON from the named file"""
        json_file = tmp_path / "payload.json"
        json_file.write_text('{"a": 1}')
        assert _parse_json(f"@{json_file}") == {"a": 1}

    def test_rejects_an_unreadable_file(self, tmp_path: Path) -> None:
        """Test that a nonexistent file given via `@<path>` raises a clean ArgumentTypeError naming the
        path, rather than an uncaught OSError
        """
        missing = tmp_path / "does-not-exist.json"
        with pytest.raises(argparse.ArgumentTypeError) as exc_info:
            _parse_json(f"@{missing}")
        assert str(missing) in str(exc_info.value)

    def test_rejects_malformed_json_from_a_file(self, tmp_path: Path) -> None:
        """Test that malformed JSON read from an `@<path>` file is rejected the same way inline JSON is"""
        json_file = tmp_path / "bad.json"
        json_file.write_text("not json")
        with pytest.raises(argparse.ArgumentTypeError):
            _parse_json(f"@{json_file}")

    def test_reads_from_stdin_given_a_bare_dash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that a bare `-` value reads its JSON from stdin"""
        monkeypatch.setattr(sys, "stdin", io.StringIO('{"a": 1}'))
        assert _parse_json("-") == {"a": 1}

    def test_rejects_a_dash_when_stdin_is_a_terminal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that a bare `-` value raises a clean ArgumentTypeError when stdin is a terminal, rather
        than blocking forever on `sys.stdin.read()` waiting for input that was never meant to come from a
        human typing at the prompt
        """
        stdin = io.StringIO('{"a": 1}')
        monkeypatch.setattr(stdin, "isatty", lambda: True)
        monkeypatch.setattr(sys, "stdin", stdin)
        with pytest.raises(argparse.ArgumentTypeError) as exc_info:
            _parse_json("-")
        assert "stdin is a terminal" in str(exc_info.value)

    def test_rejects_a_second_dash_in_the_same_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that a second `-` in the same command is rejected, since stdin can only be read once and a
        second read would otherwise silently see empty content rather than failing loudly
        """
        monkeypatch.setattr(sys, "stdin", io.StringIO('{"a": 1}'))
        _parse_json("-")
        with pytest.raises(argparse.ArgumentTypeError) as exc_info:
            _parse_json("-")
        assert "one parameter per command" in str(exc_info.value)

    def test_reset_stdin_state_allows_a_dash_in_a_later_command_in_the_same_process(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that `reset_stdin_state()` gives a later command its own fresh "one `-` allowed" budget,
        rather than the module-level tracking staying consumed for the rest of the process. `runner.py`'s
        `run()` calls this once before every real dispatch, which is what makes a second, unrelated
        `api-client` command dispatched in the same process (e.g. by an embedding harness, or this test
        suite itself) able to use `-` for its own JSON parameter too
        """
        monkeypatch.setattr(sys, "stdin", io.StringIO('{"a": 1}'))
        assert _parse_json("-") == {"a": 1}

        params_module.reset_stdin_state()

        monkeypatch.setattr(sys, "stdin", io.StringIO('{"b": 2}'))
        assert _parse_json("-") == {"b": 2}


class TestParseJsonOrStr:
    """Tests for `_parse_json_or_str()`, the JSON-or-string `type=` converter used for a parameter with no
    annotation at all
    """

    @pytest.fixture(autouse=True)
    def _reset_stdin_consumed(self) -> None:
        """Reset the module-level "stdin already read" flag before each test, matching `TestParseJson`'s own
        setup, since this converter delegates its `-`/`@<path>` handling to `_parse_json()`
        """
        params_module.reset_stdin_state()

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("123", 123),
            ("12.5", 12.5),
            ("true", True),
            ("null", None),
            ('{"a": 1}', {"a": 1}),
            ("[1, 2]", [1, 2]),
            ('"quoted"', "quoted"),
            ("hello", "hello"),
            ("2026-08-08", "2026-08-08"),
            ("-5", -5),
            ("", ""),
        ],
    )
    def test_parses_json_or_falls_back_to_the_raw_string(self, value: str, expected: Any) -> None:
        """Test that a value parses as JSON when it can (including a negative number, which stays an
        ordinary token rather than being read as a `-` stdin request), and is kept as the raw string when it
        can't, rather than raising
        """
        assert _parse_json_or_str(value) == expected

    def test_a_json_opening_token_still_parses_strictly(self) -> None:
        """Test that a value opening a JSON container or string (`{`, `[`, `"`) is still parsed strictly,
        rather than silently falling back to the raw string on a typo like a missing closing brace
        """
        with pytest.raises(argparse.ArgumentTypeError):
            _parse_json_or_str('{"a": 1')

    def test_reads_from_a_file_given_as_at_path(self, tmp_path: Path) -> None:
        """Test that an `@<path>` value still reads its JSON from the named file, exactly like `_parse_json()`"""
        json_file = tmp_path / "payload.json"
        json_file.write_text('{"a": 1}')
        assert _parse_json_or_str(f"@{json_file}") == {"a": 1}

    def test_reads_from_stdin_given_a_bare_dash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that a bare `-` value still reads its JSON from stdin, exactly like `_parse_json()`"""
        monkeypatch.setattr(sys, "stdin", io.StringIO('{"a": 1}'))
        assert _parse_json_or_str("-") == {"a": 1}


class TestUnmappableParameterFallback:
    """Tests that a parameter whose type can't be mapped to a CLI flag degrades that one parameter,
    rather than dropping the whole command the way `build_client_parser()`'s own broader try/except
    otherwise would
    """

    def test_arg_spec_failure_falls_back_without_dropping_the_command(
        self, widgets_api_class: type[WidgetsAPI], mocker: MockerFixture, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that a parameter whose `_arg_spec()` raises still gets a flag (JSON-typed, blank type
        column), every other parameter on the same endpoint is unaffected, a warning names the parameter,
        and the fallback still round-trips through `collect_call_kwargs()`
        """

        def fake_arg_spec(annotation: Any, flag: str) -> Any:
            if annotation == Literal[1, 2, 3]:
                raise RuntimeError("no mapping for this annotation")
            return _arg_spec(annotation, flag)

        mocker.patch("api_client_core.cli.params._arg_spec", side_effect=fake_arg_spec)
        ep = widgets_api_class.create_widget.endpoint
        parser = argparse.ArgumentParser()
        with caplog.at_level("WARNING", logger="api_client_core.cli.params"):
            add_endpoint_arguments(parser, ep)
            args = parser.parse_args(["--name", "x", "--owner-id", "1", "--priority", "2"])
            kwargs = collect_call_kwargs(ep, args)

        actions = {a.dest: a for a in parser._actions}
        assert {"name", "owner_id", "priority", "tags", "metadata"} <= set(actions)
        assert actions["priority"].help is not None
        assert ColorCodes.DARK_GREY not in actions["priority"].help
        assert sum("priority" in r.message and "RuntimeError" in r.message for r in caplog.records) == 1
        assert kwargs["priority"] == 2

    def test_help_text_failure_falls_back_to_the_bare_location_marker(
        self, widgets_api_class: type[WidgetsAPI], mocker: MockerFixture, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that a parameter whose help text fails to render for any reason (e.g. a pathological
        `__repr__` on its default value, surfacing inside `_format_default()`) still gets a flag, falling
        back to a bare `[location]` marker rather than `add_endpoint_arguments()` propagating the failure
        and dropping the whole command. Simulated by patching `_help_text()` itself, since a truly broken
        `__repr__` on a default value breaks dataclass model construction before the CLI ever sees the
        field, before `add_endpoint_arguments()` is even reached
        """
        real_help_text = params_module._help_text

        def fake_help_text(endpoint: Any, field: Any, **kwargs: Any) -> str:
            if field.name == "metadata":
                raise RuntimeError("bad repr")
            return real_help_text(endpoint, field, **kwargs)

        mocker.patch("api_client_core.cli.params._help_text", side_effect=fake_help_text)
        ep = widgets_api_class.create_widget.endpoint
        parser = argparse.ArgumentParser()
        with caplog.at_level("WARNING", logger="api_client_core.cli.params"):
            add_endpoint_arguments(parser, ep)
        actions = {a.dest: a for a in parser._actions}
        assert actions["metadata"].help == "[body]"
        assert sum("metadata" in r.message for r in caplog.records) == 1
        assert actions["name"].help is not None and "str" in actions["name"].help


class TestUnannotatedParameter:
    """End-to-end tests for a parameter with no annotation at all, through the real argparse pipeline
    `add_endpoint_arguments()` builds
    """

    @pytest.fixture(autouse=True)
    def _reset_stdin_consumed(self) -> None:
        """Reset the module-level "stdin already read" flag before each test, matching `TestParseJson`'s own
        setup, since `--param -` reaches `_parse_json()` the same way a fully JSON-typed flag does
        """
        params_module.reset_stdin_state()

    def _parser(self, unannotated_api_class: type[BaseAPI]) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(exit_on_error=False)
        add_endpoint_arguments(parser, unannotated_api_class.make_thing.endpoint)
        return parser

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("123", 123),
            ('{"a": 1}', {"a": 1}),
            ("hello", "hello"),
            ("-5", -5),
        ],
    )
    def test_param_parses_json_or_falls_back_to_a_string(
        self, unannotated_api_class: type[BaseAPI], value: str, expected: Any
    ) -> None:
        """Test that `--param` accepts a JSON value or an arbitrary string, through the real parser rather
        than `_parse_json_or_str()` in isolation
        """
        parser = self._parser(unannotated_api_class)
        args = parser.parse_args(["--name", "x", "--param", value])
        assert args.param == expected

    def test_param_dash_reads_from_stdin(
        self, unannotated_api_class: type[BaseAPI], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that `--param -` still reads JSON from stdin through the real parser"""
        monkeypatch.setattr(sys, "stdin", io.StringIO('{"a": 1}'))
        parser = self._parser(unannotated_api_class)
        args = parser.parse_args(["--name", "x", "--param", "-"])
        assert args.param == {"a": 1}

    def test_param_at_path_reads_from_a_file(self, unannotated_api_class: type[BaseAPI], tmp_path: Path) -> None:
        """Test that `--param @<path>` still reads JSON from the named file through the real parser"""
        json_file = tmp_path / "payload.json"
        json_file.write_text('{"a": 1}')
        parser = self._parser(unannotated_api_class)
        args = parser.parse_args(["--name", "x", "--param", f"@{json_file}"])
        assert args.param == {"a": 1}

    def test_param_rejects_malformed_json_from_a_file(
        self, unannotated_api_class: type[BaseAPI], tmp_path: Path
    ) -> None:
        """Test that a malformed `@<path>` file is still rejected, rather than its unparseable text being
        silently kept as one giant string
        """
        json_file = tmp_path / "bad.json"
        json_file.write_text("not json")
        parser = self._parser(unannotated_api_class)
        with pytest.raises(argparse.ArgumentError):
            parser.parse_args(["--name", "x", "--param", f"@{json_file}"])

    def test_param_rejects_malformed_inline_json(self, unannotated_api_class: type[BaseAPI]) -> None:
        """Test that a value opening a JSON object but never closing it is still rejected, rather than
        silently becoming a string that buries the typo"""
        parser = self._parser(unannotated_api_class)
        with pytest.raises(argparse.ArgumentError):
            parser.parse_args(["--name", "x", "--param", '{"a": 1'])

    def test_reserved_name_collision_still_shows_no_value_type(self, unannotated_api_class: type[BaseAPI]) -> None:
        """Test that an unannotated parameter named `Literal` (renamed to `Literal_` and `Alias`-annotated
        by the model builder, since `Literal` collides with a reserved model name) still shows no value type
        and still parses as JSON-or-string, exercising the `Annotated`-wrapped shape end to end rather than
        only through `_arg_spec()` directly
        """
        parser = self._parser(unannotated_api_class)
        action = next(a for a in parser._actions if a.dest == "Literal")
        assert action.help is not None
        assert ColorCodes.DARK_GREY not in action.help
        args = parser.parse_args(["--name", "x", "--Literal", "hello"])
        assert args.Literal == "hello"


class TestFormatDefault:
    """Tests for `_format_default()`, which renders a parameter's default value the way it would need to
    be typed on the CLI to reproduce it
    """

    @pytest.mark.parametrize(
        ("default", "is_json", "expected"),
        [
            (None, True, "null"),
            (True, True, "true"),
            (False, True, "false"),
            ({"a": 1}, True, '{"a": 1}'),
            (None, False, "None"),
            (10, False, "10"),
        ],
    )
    def test_renders_json_syntax_only_for_a_json_typed_default(
        self, default: Any, is_json: bool, expected: str
    ) -> None:
        """Test that a JSON-parsed flag's default is spelled in JSON syntax (`None` -> `null`, `True`/`False`
        -> `true`/`false`), matching what the parser would actually accept, while a non-JSON-parsed flag's
        default keeps Python's own `repr()` spelling
        """
        assert _format_default(default, is_json=is_json) == expected

    def test_falls_back_to_repr_for_a_non_json_serializable_default(self) -> None:
        """Test that a default value `json.dumps()` can't serialize falls back to `repr()` instead of raising"""
        sentinel = object()
        assert _format_default(sentinel, is_json=True) == repr(sentinel)

    def test_falls_back_to_repr_for_a_circular_default(self) -> None:
        """Test that a JSON-parsed flag's default containing a circular reference (`json.dumps()` raises
        `ValueError`, not `TypeError`, for this) also falls back to `repr()` instead of raising
        """
        circular: list[Any] = []
        circular.append(circular)
        assert _format_default(circular, is_json=True) == repr(circular)

    def test_enum_default_renders_as_its_member_name(self, status_enum: type[Status]) -> None:
        """Test that an `Enum` member default renders as its own bare `.name`, matching what the flag
        itself accepts, rather than Python's own `repr()` spelling (`<Status.ACTIVE: 'active'>`),
        regardless of whether the field's flag is JSON-parsed
        """
        assert _format_default(status_enum.ACTIVE, is_json=False) == "ACTIVE"


class TestCollectCallKwargs:
    """Tests for `collect_call_kwargs()`"""

    def test_omits_not_provided_flags(self, widgets_api_class: type[WidgetsAPI]) -> None:
        """Test that flags left as NOT_PROVIDED are omitted from the call kwargs entirely"""
        endpoint = widgets_api_class.create_widget.endpoint
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, endpoint)
        args = parser.parse_args(["--name", "w", "--owner-id", "1"])
        call_kwargs = collect_call_kwargs(endpoint, args)
        assert call_kwargs == {"name": "w", "owner_id": 1}

    def test_converts_path_to_file(self, widgets_api_class: type[WidgetsAPI], tmp_path: Path) -> None:
        """Test that a parsed Path value for a File field is converted to a File instance with real bytes"""
        avatar = tmp_path / "avatar.png"
        avatar.write_bytes(b"fake-png-bytes")
        endpoint = widgets_api_class.upload_avatar.endpoint
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, endpoint)
        args = parser.parse_args(["--widget-id", "1", "--avatar", str(avatar)])
        call_kwargs = collect_call_kwargs(endpoint, args)
        assert isinstance(call_kwargs["avatar"], File)
        assert call_kwargs["avatar"].content == b"fake-png-bytes"
        assert call_kwargs["avatar"].filename == "avatar.png"

    def test_converts_every_path_in_a_list_file_param_to_a_file(
        self, list_file_api_class: type[BaseAPI], tmp_path: Path
    ) -> None:
        """Test that every element of a `list[File]` parameter is converted from its parsed Path to a File
        instance with real bytes, not left as a bare Path (what argparse actually produces for a
        `nargs='*'` File flag, since the scalar-only Path check used to miss the list case entirely)
        """
        a = tmp_path / "a.txt"
        a.write_bytes(b"aaa")
        b = tmp_path / "b.txt"
        b.write_bytes(b"bbb")
        ep = list_file_api_class.make_thing.endpoint
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, ep)
        args = parser.parse_args(["--attachments", str(a), str(b)])
        call_kwargs = collect_call_kwargs(ep, args)
        attachments = call_kwargs["attachments"]
        assert all(isinstance(f, File) for f in attachments)
        assert [f.filename for f in attachments] == ["a.txt", "b.txt"]
        assert [f.content for f in attachments] == [b"aaa", b"bbb"]

    def test_reserved_cli_flag_collision_is_omitted_rather_than_leaking_the_control_flags_value(
        self, reserved_cli_flag_api_class: type[BaseAPI]
    ) -> None:
        """Test that a parameter colliding with the reserved --quiet flag is omitted from
        collect_call_kwargs() output entirely, rather than reading back the --quiet control flag's own
        boolean value under the parameter's name, which would otherwise collide with the `quiet`
        keyword runner.run() forwards separately as a call-control argument
        """
        ep = reserved_cli_flag_api_class.make_thing.endpoint
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, ep)
        parser.add_argument("--quiet", action="store_true")  # mirrors builder._add_call_ctrl_arguments
        args = parser.parse_args([])
        call_kwargs = collect_call_kwargs(ep, args)
        assert "quiet" not in call_kwargs

    def test_reserved_header_flag_collision_reads_back_from_the_alias_not_the_control_flag(
        self, reserved_header_flag_api_class: type[BaseAPI]
    ) -> None:
        """Test that a parameter colliding with the reserved -H/--header flag, aliased to `--header_`, is
        read back under its own real name (`header`) in `collect_call_kwargs()`'s output from its own
        alias dest, rather than accidentally reading back the --header control flag's own accumulated
        `(name, value)` list under the parameter's name
        """
        ep = reserved_header_flag_api_class.make_thing.endpoint
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, ep)
        parser.add_argument("-H", "--header", action="append", default=[])  # mirrors _add_call_ctrl_arguments
        args = parser.parse_args(["-H", "X: 1", "--header_", "given"])
        call_kwargs = collect_call_kwargs(ep, args)
        assert call_kwargs["header"] == "given"

    def test_reserved_help_flag_collision_reads_back_from_the_alias(
        self, reserved_help_flag_api_class: type[BaseAPI]
    ) -> None:
        """Test that a parameter colliding with -h/--help, aliased to `--help_`, is read back under its
        own real name (`help`) in `collect_call_kwargs()`'s output, rather than raising `AttributeError`
        trying to read a `help` attribute that argparse's own SUPPRESS-valued help action never sets on the
        namespace
        """
        ep = reserved_help_flag_api_class.make_thing.endpoint
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, ep)
        args = parser.parse_args(["--help_", "given"])
        call_kwargs = collect_call_kwargs(ep, args)
        assert call_kwargs["help"] == "given"

    def test_reserved_call_kwarg_collision_is_omitted_rather_than_leaking_a_second_value(
        self, reserved_call_kwarg_api_class: type[BaseAPI]
    ) -> None:
        """Test that parameters colliding with the with_hooks/raw_options control kwargs are omitted from
        collect_call_kwargs() output entirely, rather than reaching run()'s call(**call_kwargs,
        **ctrl_kwargs) and raising got multiple values for keyword argument
        """
        ep = reserved_call_kwarg_api_class.make_thing.endpoint
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, ep)
        args = parser.parse_args(["--name", "x"])
        call_kwargs = collect_call_kwargs(ep, args)
        assert call_kwargs == {"name": "x"}


class TestResolveParamsWarnings:
    """Tests for how `_resolve_params()`'s skip diagnostic is shared between `add_endpoint_arguments()` and
    `collect_call_kwargs()`
    """

    def test_skip_log_is_emitted_once_even_when_both_functions_run(
        self, reserved_cli_flag_api_class: type[BaseAPI], caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that the reserved-dest skip diagnostic is logged exactly once per parser build, not once
        more for every `collect_call_kwargs()` call that resolves the same endpoint's params at dispatch
        time. `build_client_parser()` walks every endpoint once via `add_endpoint_arguments()`, and
        `collect_call_kwargs()` re-resolves the same params on every dispatched call, so without this a
        single colliding parameter would double the log noise on every invocation
        """
        ep = reserved_cli_flag_api_class.make_thing.endpoint
        parser = argparse.ArgumentParser()
        with caplog.at_level("DEBUG", logger="api_client_core.cli.params"):
            add_endpoint_arguments(parser, ep)
            args = parser.parse_args([])
            collect_call_kwargs(ep, args)
        assert sum("quiet" in record.message for record in caplog.records) == 1


class TestReservedCallKwargs:
    """Tests for _RESERVED_CALL_KWARGS, the control kwargs `run()` passes to every dispatched call"""

    def test_quiet_is_included(self) -> None:
        """Test that `quiet` is covered by _RESERVED_CALL_KWARGS. It also collides via the plain
        `--quiet` flag reservation (`RESERVED_CLI_FLAGS`), but that alone isn't enough to drop it: without
        this deeper check, `--quiet`'s own flag collision would be "fixed" by aliasing it away to
        `--quiet_`, silently leaving the CLI broken, since the `-q`/`--quiet` control flag's own dest is
        also the literal string `quiet`. `collect_call_kwargs()` would then key the endpoint's own value
        by `quiet` too, colliding a second time with the identically-named `quiet` control kwarg `run()`
        passes to every call
        """
        assert "quiet" in _RESERVED_CALL_KWARGS

    def test_with_hooks_and_raw_options_are_included(self) -> None:
        """Test that with_hooks/raw_options, the two control kwargs whose flags are not otherwise
        reserved, are covered by _RESERVED_CALL_KWARGS
        """
        assert {"with_hooks", "raw_options"} <= _RESERVED_CALL_KWARGS


class TestResolveSignatureName:
    """Tests for CLI flag/dispatch naming when a model field is renamed away from its signature name"""

    def test_reserved_name_param_is_required_when_signature_has_no_default(
        self, reserved_name_api_class: type[BaseAPI]
    ) -> None:
        """Test that a parameter whose name collides with a reserved model name (so its model field is
        renamed, e.g. `Query` -> `Query_`) is still treated as required, driven by the original
        signature rather than the renamed model field which would otherwise look optional
        """
        ep = reserved_name_api_class.make_thing.endpoint
        assert list(ep.model.__dataclass_fields__) == ["Query_"]

        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, ep)
        action = next(a for a in parser._actions if a.dest == "Query")
        assert action.required is True

    def test_collect_call_kwargs_uses_the_original_name_for_a_renamed_field(
        self, reserved_name_api_class: type[BaseAPI]
    ) -> None:
        """Test that collect_call_kwargs() keys its output by the original signature name, not the
        renamed model field name, so the value actually binds to the endpoint's own parameter instead
        of being swallowed into its `**kwargs`
        """
        ep = reserved_name_api_class.make_thing.endpoint
        parser = argparse.ArgumentParser()
        add_endpoint_arguments(parser, ep)
        args = parser.parse_args(["--Query", "hello"])
        call_kwargs = collect_call_kwargs(ep, args)
        assert call_kwargs == {"Query": "hello"}


class TestFlagFor:
    """Tests for `_flag_for`'s derivation of a CLI flag from a parameter's signature name"""

    @pytest.mark.parametrize(
        ("param_name", "flag"),
        [
            ("widget_id", "--widget-id"),
            ("from_", "--from"),
            ("_private", "--private"),
            ("a__b", "--a-b"),
            ("__dunder__", "--dunder"),
        ],
    )
    def test_derives_a_well_formed_flag(self, param_name: str, flag: str) -> None:
        """Test that a leading, trailing, or doubled internal underscore never produces a malformed
        flag (e.g. a stray leading or doubled `-`), while the common trailing-underscore keyword
        escape (`from_`) still maps to its expected flag
        """
        assert _flag_for(param_name) == flag


class TestNormalizeCallArgs:
    """Tests for `normalize_call_args()`

    This utility exists solely for the CLI's own dispatch (`runner.py`), which can only ever produce
    keyword arguments. It must never be wired into the framework's own call-binding path (`split_params()`,
    `EndpointFunc`), which deliberately keeps enforcing positional-only-ness for every direct Python call.
    """

    def test_no_positional_only_params_returns_input_unchanged(self) -> None:
        """Test that a function with no positional-only params returns args/kwargs unchanged"""

        def _func(self: Any, a: str, b: str) -> None: ...

        args, kwargs = normalize_call_args(_func, (), {"a": "1", "b": "2"})
        assert (args, kwargs) == ((), {"a": "1", "b": "2"})

    def test_positional_only_param_already_given_positionally_returns_unchanged(self) -> None:
        """Test that a positional-only param already supplied positionally is left untouched"""

        def _func(self: Any, a: str, /, b: str) -> None: ...

        args, kwargs = normalize_call_args(_func, ("1",), {"b": "2"})
        assert (args, kwargs) == (("1",), {"b": "2"})

    def test_required_positional_only_param_by_keyword_moves_into_args(self) -> None:
        """Test that a required positional-only param passed by keyword is moved into the positional args"""

        def _func(self: Any, a: str, /, b: str = "B") -> None: ...

        args, kwargs = normalize_call_args(_func, (), {"a": "7"})
        assert (args, kwargs) == (("7",), {})

    def test_gap_before_named_param_is_filled_from_its_own_default(self) -> None:
        """Test that an earlier positional-only param the caller skipped is filled from its own default,
        the same value a direct positional call omitting it would bind
        """

        def _func(self: Any, a: int = 1, b: int = 2, /) -> None: ...

        args, kwargs = normalize_call_args(_func, (), {"b": 5})
        assert (args, kwargs) == ((1, 5), {})

    def test_gap_with_no_default_raises_naming_both_params(self) -> None:
        """Test that a skipped positional-only param with no default raises, naming both the unfillable param and
        the later one the caller tried to reach by keyword
        """

        def _func(self: Any, a: str, b: str, /) -> None: ...

        with pytest.raises(TypeError, match=r"_func\(\).*'b'.*'a'"):
            normalize_call_args(_func, (), {"b": "5"})

    def test_param_passed_positionally_and_by_name_with_var_keyword_returns_unchanged(self) -> None:
        """Test that a positional-only param given both positionally and by name is left for Python's own
        VAR_KEYWORD absorption, since it is already fully satisfied positionally
        """

        def _func(self: Any, a: int = 1, /, **kwargs: Any) -> None: ...

        args, kwargs = normalize_call_args(_func, (1,), {"a": 9})
        assert (args, kwargs) == ((1,), {"a": 9})

    def test_param_passed_positionally_and_by_name_without_var_keyword_returns_unchanged(self) -> None:
        """Test that a positional-only param given both positionally and by name with no VAR_KEYWORD is left
        unchanged, so the natural 'multiple values' TypeError still surfaces downstream
        """

        def _func(self: Any, a: str, /, b: str = "B") -> None: ...

        args, kwargs = normalize_call_args(_func, ("1",), {"a": "9"})
        assert (args, kwargs) == (("1",), {"a": "9"})

    def test_var_positional_param_is_untouched(self) -> None:
        """Test that a VAR_POSITIONAL parameter coexisting with positional-only params is left alone"""

        def _func(self: Any, a: str, /, *rest: Any, b: str = "B") -> None: ...

        args, kwargs = normalize_call_args(_func, (), {"a": "1", "b": "5"})
        assert (args, kwargs) == (("1",), {"b": "5"})

    def test_unset_default_is_treated_as_a_normal_default_for_gap_filling(self) -> None:
        """Test that a positional-only param defaulting to `Unset` is filled like any other default"""

        def _func(self: Any, a: Any = Unset, b: int = 2, /) -> None: ...

        args, kwargs = normalize_call_args(_func, (), {"b": 5})
        assert (args, kwargs) == ((Unset, 5), {})

    def test_does_not_mutate_the_caller_supplied_kwargs_dict(self) -> None:
        """Test that normalization operates on a copy and never mutates the caller's kwargs dict"""

        def _func(self: Any, a: str, /, b: str = "B") -> None: ...

        original_kwargs = {"a": "7"}
        normalize_call_args(_func, (), original_kwargs)
        assert original_kwargs == {"a": "7"}

    def test_is_idempotent(self) -> None:
        """Test that re-applying normalization to already-normalized args/kwargs is a no-op"""

        def _func(self: Any, a: str, /, b: str = "B") -> None: ...

        args1, kwargs1 = normalize_call_args(_func, (), {"a": "7"})
        args2, kwargs2 = normalize_call_args(_func, args1, kwargs1)
        assert (args2, kwargs2) == (args1, kwargs1)


class TestPeekLogLevel:
    """Tests for `peek_log_level()`, the lightweight pre-parse `dispatch()` uses ahead of the real,
    client-specific parser, to configure logging before discovery runs
    """

    def test_extracts_log_level_regardless_of_what_else_is_present(self) -> None:
        """Test that the flag is extracted out of a full, realistic argv"""
        log_level = peek_log_level(["--log-level", "DEBUG", "widgets", "get-widget", "--widget-id", "1"])
        assert log_level == "DEBUG"

    def test_returns_none_when_not_given(self) -> None:
        """Test that omitting the flag returns None rather than raising"""
        assert peek_log_level(["widgets", "get-widget"]) is None

    def test_malformed_input_is_ignored_rather_than_raised(self) -> None:
        """Test that a --log-level with no value (malformed) doesn't crash the peek: the real parser
        reports it cleanly once the full argv is parsed in full
        """
        assert peek_log_level(["--log-level"]) is None

    def test_invalid_log_level_value_is_ignored_rather_than_raised(self) -> None:
        """Test that a --log-level value outside the real parser's own choices doesn't crash the peek
        (previously an uncaught ValueError from setup_logging(), since the peek parser had no choices=
        of its own to reject it first): the real parser reports it cleanly once the full argv is parsed in
        full
        """
        assert peek_log_level(["--log-level", "BOGUS"]) is None

    def test_a_lowercase_log_level_is_normalized_to_uppercase(self) -> None:
        """Test that a lowercase --log-level value is accepted and normalized, matching the real flag's own
        case-insensitive `type=str.upper`, rather than being nulled out as an unrecognized choice
        """
        assert peek_log_level(["--log-level", "debug"]) == "DEBUG"

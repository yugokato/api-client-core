"""Unit tests for `api_client_core.mcp.schema`"""

from __future__ import annotations

import inspect
from datetime import date, datetime, time
from decimal import Decimal
from typing import Annotated, Any, Literal, Unpack
from uuid import UUID

import pytest

from api_client_core import BaseAPI, endpoint
from api_client_core.core.constants import VALID_METHODS
from api_client_core.mcp.catalog import ServerOptions
from api_client_core.mcp.schema import (
    _schema_for_annotation,
    annotations_for,
    build_input_schema,
    iter_param_fields,
    tool_description,
    tool_title,
)
from api_client_core.types import File, Kwargs, RestResponse, Unset

from ..tests_cli.conftest import CollisionAPI, Status, WidgetsAPI, module_scoped


class TestSchemaForAnnotationScalars:
    """Tests for `_schema_for_annotation()`'s dispatch of plain scalar/stringly types"""

    @pytest.mark.parametrize(
        ("annotation", "expected"),
        [
            (str, {"type": "string"}),
            (int, {"type": "integer"}),
            (float, {"type": "number"}),
            (bool, {"type": "boolean"}),
            (datetime, {"type": "string", "format": "date-time"}),
            (date, {"type": "string", "format": "date"}),
            (time, {"type": "string", "format": "time"}),
            (UUID, {"type": "string", "format": "uuid"}),
            (Decimal, {"type": "string"}),
        ],
    )
    def test_maps_a_scalar_type(self, annotation: Any, expected: dict[str, Any]) -> None:
        """Test that each supported scalar/stringly type maps to its expected JSON Schema fragment"""
        assert _schema_for_annotation(annotation, ServerOptions()) == expected

    def test_str_subclass_maps_to_string(self) -> None:
        """Test that a `str` subclass (e.g. a NewType-like wrapper) maps to a plain string schema"""

        class _CustomStr(str): ...

        assert _schema_for_annotation(_CustomStr, ServerOptions()) == {"type": "string"}

    def test_no_annotation_maps_to_an_empty_schema(self) -> None:
        """Test that the "no type declared" sentinel maps to an unconstrained ({}) schema"""
        assert _schema_for_annotation(inspect.Parameter.empty, ServerOptions()) == {}

    def test_unmappable_annotation_degrades_to_an_empty_schema(self) -> None:
        """Test that an annotation with no mapping at all (e.g. a bare, unparameterized dict already
        covered elsewhere, or a custom class) degrades to {} rather than raising
        """

        class _Unmappable: ...

        assert _schema_for_annotation(_Unmappable, ServerOptions()) == {}


class TestSchemaForAnnotationContainers:
    """Tests for `_schema_for_annotation()`'s dispatch of sequence/dict/tuple shapes"""

    def test_homogeneous_list_maps_to_array_with_items(self) -> None:
        """Test that list[X] maps to an array schema with an items schema for X"""
        assert _schema_for_annotation(list[int], ServerOptions()) == {"type": "array", "items": {"type": "integer"}}

    def test_bare_unparameterized_list_maps_to_a_plain_array(self) -> None:
        """Test that a bare, unparameterized list maps to an array with no items constraint"""
        assert _schema_for_annotation(list, ServerOptions()) == {"type": "array"}

    def test_variable_length_tuple_maps_like_a_list(self) -> None:
        """Test that tuple[X, ...] maps to an array with an items schema, like list[X]"""
        assert _schema_for_annotation(tuple[str, ...], ServerOptions()) == {
            "type": "array",
            "items": {"type": "string"},
        }

    def test_fixed_length_heterogeneous_tuple_maps_to_prefix_items(self) -> None:
        """Test that a fixed-length tuple[X, Y] maps to an array with prefixItems, minItems, and
        maxItems - a strictly more precise mapping than the CLI's own degrade-to-one-JSON-token handling
        """
        assert _schema_for_annotation(tuple[str, int], ServerOptions()) == {
            "type": "array",
            "prefixItems": [{"type": "string"}, {"type": "integer"}],
            "minItems": 2,
            "maxItems": 2,
        }

    def test_dict_with_value_type_maps_to_object_with_additional_properties(self) -> None:
        """Test that dict[str, X] maps to an object schema whose additionalProperties is X's schema"""
        assert _schema_for_annotation(dict[str, int], ServerOptions()) == {
            "type": "object",
            "additionalProperties": {"type": "integer"},
        }

    def test_bare_dict_maps_to_a_plain_object(self) -> None:
        """Test that a bare dict maps to an object with no property constraint"""
        assert _schema_for_annotation(dict, ServerOptions()) == {"type": "object"}

    def test_nested_optional_element_type_is_preserved(self) -> None:
        """Test that a container's nullable element type keeps its own null branch, not just the
        container's own top-level nullability
        """
        assert _schema_for_annotation(list[int | None], ServerOptions()) == {
            "type": "array",
            "items": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        }


class TestSchemaForAnnotationLiteralAndEnum:
    """Tests for `_schema_for_annotation()`'s dispatch of `Literal[...]` and `Enum`"""

    def test_literal_with_shared_int_type_gets_a_type_and_enum(self) -> None:
        """Test that a Literal of same-typed ints gets both a shared "type" and its "enum" choices"""
        assert _schema_for_annotation(Literal[1, 2, 3], ServerOptions()) == {"type": "integer", "enum": [1, 2, 3]}

    def test_literal_with_shared_str_type_gets_a_type_and_enum(self) -> None:
        """Test that a Literal of same-typed strings gets both a shared "type" and its "enum" choices"""
        assert _schema_for_annotation(Literal["a", "b"], ServerOptions()) == {"type": "string", "enum": ["a", "b"]}

    def test_literal_with_mixed_types_gets_no_shared_type(self) -> None:
        """Test that a Literal mixing types (e.g. a string and an int) omits "type" entirely, since no
        single JSON type covers every choice, but still exposes the enum
        """
        assert _schema_for_annotation(Literal["a", 1], ServerOptions()) == {"enum": ["a", 1]}

    def test_enum_maps_to_a_string_enum_of_member_names(self) -> None:
        """Test that an Enum maps to a string schema whose enum lists member NAMES (matching the CLI's
        name-based convention, so the runner can invert via cls[name]), not their values
        """
        assert _schema_for_annotation(Status, ServerOptions()) == {
            "type": "string",
            "enum": ["ACTIVE", "INACTIVE"],
        }


class TestSchemaForAnnotationUnionsAndNullability:
    """Tests for `_schema_for_annotation()`'s handling of unions and nullability - the one place this
    module deliberately diverges from the CLI's own `_effective_type()`, which discards nullability
    entirely
    """

    def test_optional_scalar_becomes_any_of_with_null(self) -> None:
        """Test that T | None becomes an anyOf of T's schema and a null branch"""
        assert _schema_for_annotation(str | None, ServerOptions()) == {"anyOf": [{"type": "string"}, {"type": "null"}]}

    def test_genuine_union_becomes_any_of_in_declaration_order(self) -> None:
        """Test that a real multi-type union becomes an anyOf of each member's own schema, in order"""
        assert _schema_for_annotation(int | str, ServerOptions()) == {
            "anyOf": [{"type": "integer"}, {"type": "string"}]
        }

    def test_nullable_multi_member_union_merges_null_into_the_same_any_of(self) -> None:
        """Test that a nullable multi-member union (int | str | None) merges the null branch into the
        same anyOf list, rather than nesting a second anyOf inside it
        """
        assert _schema_for_annotation(int | str | None, ServerOptions()) == {
            "anyOf": [{"type": "integer"}, {"type": "string"}, {"type": "null"}]
        }

    def test_annotated_optional_still_preserves_nullability(self) -> None:
        """Test that Annotated[] wrapping around a nullable type doesn't lose the null branch"""
        assert _schema_for_annotation(Annotated[str, "meta"] | None, ServerOptions()) == {
            "anyOf": [{"type": "string"}, {"type": "null"}]
        }

    def test_enum_member_of_a_genuine_union_is_schematized_as_its_own_enum_branch(self) -> None:
        """Test that Status | str publishes Status's enum-of-names branch alongside the plain
        string branch, matching what runner.py's _coerce_value() must convert back from
        """
        assert _schema_for_annotation(Status | str, ServerOptions()) == {
            "anyOf": [{"type": "string", "enum": [m.name for m in Status]}, {"type": "string"}]
        }


class TestFileSchema:
    """Tests for `_schema_for_annotation()`'s handling of `File`-typed parameters, and the
    `--allow-file-paths` safety gate on the local-path form
    """

    def test_default_schema_only_offers_inline_base64(self) -> None:
        """Test that without --allow-file-paths, only the base64 form is in the schema at all - a model
        is never invited to try a form the server will refuse
        """
        schema = _schema_for_annotation(File, ServerOptions())
        assert schema["required"] == ["filename", "content_base64"]
        assert "anyOf" not in schema

    def test_allow_file_paths_adds_the_path_form_as_an_alternative(self) -> None:
        """Test that --allow-file-paths adds a second anyOf branch accepting a local path, without
        removing the base64 form
        """
        schema = _schema_for_annotation(File, ServerOptions(allow_file_paths=True))
        assert "anyOf" in schema
        assert len(schema["anyOf"]) == 2
        forms = {tuple(sorted(f["required"])) for f in schema["anyOf"]}
        assert forms == {("content_base64", "filename"), ("path",)}


class TestIterParamFields:
    """Tests for `iter_param_fields()`, `build_input_schema()`'s own name-resolution half with no
    schema attached - the part `runner.py`'s `_coerce_arguments()` uses directly on every tool dispatch
    """

    def test_yields_param_name_and_required_with_no_schema(self) -> None:
        """Test that each yielded param carries its resolved name and required-ness, matching what
        build_input_schema() derives from the same walk, with no schema attached at all
        """
        params = {p.name: p for p in iter_param_fields(WidgetsAPI.create_widget.endpoint)}
        assert params["name"].required is True
        assert params["active"].required is False
        assert not hasattr(params["name"], "schema")

    def test_reserved_control_kwargs_are_skipped(self) -> None:
        """Test that a field resolving to a reserved control-kwarg name is skipped, matching
        build_input_schema()'s property set for the identical endpoint
        """
        names = {p.name for p in iter_param_fields(CollisionAPI.make_thing.endpoint)}
        assert names == {"name"}

    def test_skipped_reserved_kwarg_is_logged_under_warn(self, caplog: pytest.LogCaptureFixture) -> None:
        """Test that warn=True logs the same diagnostic build_input_schema()'s warn=True path does,
        since build_input_schema() delegates the skip itself to this function
        """
        with caplog.at_level("DEBUG", logger="api_client_core.mcp.schema"):
            list(iter_param_fields(CollisionAPI.make_thing.endpoint, warn=True))
        assert "quiet" in caplog.text


class TestBuildInputSchema:
    """Tests for `build_input_schema()`'s full per-endpoint pipeline"""

    def test_required_comes_from_the_signature_not_the_model(self) -> None:
        """Test that a required body parameter is marked required in the schema, even though
        `create_endpoint_model()` gives it an `Unset` default at the model level - required-ness must be
        read from the signature, not `field.default`. This is the single easiest bug to introduce here.
        """
        schema = build_input_schema(WidgetsAPI.create_widget.endpoint, ServerOptions())
        assert set(schema["required"]) == {"name", "owner_id"}
        assert "active" not in schema["required"]  # has a concrete default
        assert "tags" not in schema["required"]  # defaults to Unset

    def test_reserved_control_kwargs_never_appear_as_properties(self) -> None:
        """Test that quiet/with_hooks/raw_options-colliding parameters are dropped from the schema
        entirely, rather than reaching the model twice under the same keyword at dispatch time
        """
        schema = build_input_schema(CollisionAPI.make_thing.endpoint, ServerOptions())
        assert set(schema["properties"]) == {"name"}

        hook_schema = build_input_schema(CollisionAPI.make_hook_thing.endpoint, ServerOptions())
        assert set(hook_schema["properties"]) == {"name"}

    def test_skipped_reserved_kwarg_is_logged_under_warn(self, caplog: pytest.LogCaptureFixture) -> None:
        """Test that warn=True logs a diagnostic naming the skipped reserved parameter, mirroring the
        CLI's convention for a field it can't map to a flag
        """
        with caplog.at_level("DEBUG", logger="api_client_core.mcp.schema"):
            build_input_schema(CollisionAPI.make_thing.endpoint, ServerOptions(), warn=True)
        assert "quiet" in caplog.text

    def test_skipped_reserved_kwarg_is_silent_without_warn(self, caplog: pytest.LogCaptureFixture) -> None:
        """Test that the same skip logs nothing when warn isn't set, matching every other diagnostic in
        this module
        """
        with caplog.at_level("DEBUG", logger="api_client_core.mcp.schema"):
            build_input_schema(CollisionAPI.make_thing.endpoint, ServerOptions())
        assert caplog.text == ""

    def test_default_value_is_included_for_an_optional_parameter(self) -> None:
        """Test that a concrete (non-Unset) default is included in the property's schema"""
        schema = build_input_schema(WidgetsAPI.create_widget.endpoint, ServerOptions())
        assert schema["properties"]["active"]["default"] is True

    def test_unset_default_is_not_included(self) -> None:
        """Test that a parameter defaulting to Unset gets no "default" key at all"""
        schema = build_input_schema(WidgetsAPI.create_widget.endpoint, ServerOptions())
        assert "default" not in schema["properties"]["tags"]

    def test_implicit_optional_none_default_widens_the_schema_to_match(self) -> None:
        """Test that a def f(self, name: str = None)-shaped parameter - a None default with no T |
        None in the annotation itself - gets a nullable schema, not just a "default": null on a
        property its own type otherwise declares non-nullable
        """

        @module_scoped
        class _ImplicitOptionalAPI(BaseAPI):
            app_name = "implicit-optional-schema-test"

            @endpoint.get("/things")
            def list_things(
                self,
                name: str = None,  # type: ignore[assignment]  # noqa: RUF013
                **kwargs: Unpack[Kwargs],
            ) -> RestResponse: ...

        schema = build_input_schema(_ImplicitOptionalAPI.list_things.endpoint, ServerOptions())
        prop = schema["properties"]["name"]
        assert prop["anyOf"] == [{"type": "string"}, {"type": "null"}]
        assert prop["default"] is None

    def test_parameter_descriptions_come_from_the_docstring(self) -> None:
        """Test that a documented endpoint's :param: descriptions populate each property's description"""
        schema = build_input_schema(WidgetsAPI.get_widget.endpoint, ServerOptions())
        assert schema["properties"]["widget_id"].get("description") is None  # get_widget has no :param:

    def test_deprecated_endpoint_parameter_is_marked(self) -> None:
        """Test that a deprecated parameter's schema carries deprecated: true and a note in its
        description
        """

        @module_scoped
        class _DeprecatedParamAPI(BaseAPI):
            app_name = "deprecated-param-schema-test"

            @endpoint.post("/things")
            def make_thing(self, name: Annotated[str, "deprecated"] = Unset, **kwargs: Unpack[Kwargs]) -> RestResponse:
                """Make a thing

                :param name: The thing's name
                """
                ...

        schema = build_input_schema(_DeprecatedParamAPI.make_thing.endpoint, ServerOptions())
        assert schema["properties"]["name"]["deprecated"] is True
        assert "(deprecated)" in schema["properties"]["name"]["description"]

    def test_additional_properties_is_always_false(self) -> None:
        """Test that the top-level schema always forbids extra properties, so an unrecognized key is
        rejected at validation time rather than silently forwarded
        """
        schema = build_input_schema(WidgetsAPI.get_widget.endpoint, ServerOptions())
        assert schema["additionalProperties"] is False

    def test_no_required_key_when_every_parameter_is_optional(self) -> None:
        """Test that a "required" key is omitted entirely (not an empty list) when nothing is required"""
        schema = build_input_schema(WidgetsAPI.list_widgets.endpoint, ServerOptions())
        assert "required" not in schema

    def test_call_wrappers_property_is_nested_only_when_requested(self) -> None:
        """Test that the reserved `call_wrappers` property appears only with `with_call_wrappers=True`
        (static mode), and never in the default build (dynamic mode nests it on `call_endpoint` instead)
        """
        default_schema = build_input_schema(WidgetsAPI.get_widget.endpoint, ServerOptions())
        assert "call_wrappers" not in default_schema["properties"]

        static_schema = build_input_schema(WidgetsAPI.get_widget.endpoint, ServerOptions(), with_call_wrappers=True)
        assert "call_wrappers" in static_schema["properties"]
        assert "call_wrappers" not in static_schema.get("required", [])

    def test_a_parameter_named_call_wrappers_is_skipped_as_reserved(self) -> None:
        """Test that an endpoint parameter that resolves to `call_wrappers` is dropped from the schema,
        so it never collides with the reserved per-call wrappers property
        """

        @module_scoped
        class _CallWrappersParamAPI(BaseAPI):
            app_name = "call-wrappers-param-schema-test"

            @endpoint.post("/things")
            def make_thing(self, name: str, call_wrappers: str = Unset, **kwargs: Unpack[Kwargs]) -> RestResponse: ...

        schema = build_input_schema(_CallWrappersParamAPI.make_thing.endpoint, ServerOptions())
        assert set(schema["properties"]) == {"name"}


class TestParamLocationInSchema:
    """Tests that each property's own `x-location` in the built schema matches its real request
    location. The classification itself (`endpoint_call_util.param_location()`, shared with the CLI)
    has its own dedicated tests in `tests_endpoints/tests_utils/test_endpoint_call.py`.
    """

    def test_path_parameter_is_path(self) -> None:
        """Test that a path placeholder parameter's property is marked x-location: path"""
        schema = build_input_schema(WidgetsAPI.get_widget.endpoint, ServerOptions())
        assert schema["properties"]["widget_id"]["x-location"] == "path"

    def test_get_body_parameter_is_query(self) -> None:
        """Test that every parameter on a GET endpoint is marked x-location: query, regardless of its
        own annotation
        """
        schema = build_input_schema(WidgetsAPI.list_widgets.endpoint, ServerOptions())
        assert schema["properties"]["limit"]["x-location"] == "query"

    def test_explicit_query_annotation_on_a_post_endpoint_is_query(self) -> None:
        """Test that an Annotated[T, Query()] parameter on a non-GET endpoint is still marked
        x-location: query
        """
        schema = build_input_schema(WidgetsAPI.create_widget.endpoint, ServerOptions())
        assert schema["properties"]["notify"]["x-location"] == "query"

    def test_ordinary_post_parameter_is_body(self) -> None:
        """Test that an ordinary (non-Query) parameter on a POST endpoint is marked x-location: body"""
        schema = build_input_schema(WidgetsAPI.create_widget.endpoint, ServerOptions())
        assert schema["properties"]["name"]["x-location"] == "body"


class TestToolDescriptionAndTitle:
    """Tests for `tool_description()`/`tool_title()`'s docstring-derived text"""

    def test_description_includes_prose_and_the_http_call(self) -> None:
        """Test that a documented endpoint's description shows its prose followed by the HTTP call"""
        text = tool_description(WidgetsAPI.get_widget.endpoint)
        assert "Get a widget by ID" in text
        assert "HTTP: GET /widgets/{widget_id}" in text

    def test_undocumented_endpoint_falls_back_to_the_http_call_alone(self) -> None:
        """Test that an endpoint with no docstring at all falls back to just the HTTP call"""

        @module_scoped
        class _NoDocAPI(BaseAPI):
            app_name = "nodoc-schema-test"

            @endpoint.get("/things")
            def get_thing(self, **kwargs: Unpack[Kwargs]) -> RestResponse: ...

        text = tool_description(_NoDocAPI.get_thing.endpoint)
        assert text == "GET /things"

    def test_deprecated_endpoint_description_carries_a_deprecation_note(self) -> None:
        """Test that a deprecated endpoint's description names its own deprecation"""
        text = tool_description(WidgetsAPI.list_widgets.endpoint)
        assert "DEPRECATED" in text

    def test_title_is_the_docstring_summary_line(self) -> None:
        """Test that the title is just the docstring's first line, not the full prose"""
        assert tool_title(WidgetsAPI.get_widget.endpoint) == "Get a widget by ID"

    def test_title_falls_back_to_the_http_call_with_no_docstring(self) -> None:
        """Test that the title falls back to the HTTP call when there's no docstring at all"""

        @module_scoped
        class _NoDocTitleAPI(BaseAPI):
            app_name = "nodoc-title-schema-test"

            @endpoint.get("/things")
            def get_thing(self, **kwargs: Unpack[Kwargs]) -> RestResponse: ...

        assert tool_title(_NoDocTitleAPI.get_thing.endpoint) == "GET /things"


class TestAnnotationsFor:
    """Tests for `annotations_for()`'s honest per-method safety hint table"""

    @pytest.mark.parametrize(
        ("method", "expected"),
        [
            ("get", {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}),
            ("head", {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}),
            ("options", {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}),
            ("trace", {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}),
            ("put", {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True}),
            ("delete", {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True}),
            ("patch", {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False}),
            ("post", {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}),
        ],
    )
    def test_annotation_table(self, method: str, expected: dict[str, bool]) -> None:
        """Test that each HTTP method gets its exact expected combination of hints"""
        annotations = annotations_for(method)
        for key, value in expected.items():
            assert annotations[key] is value

    def test_open_world_hint_is_always_true(self) -> None:
        """Test that openWorldHint is true for every method, since every endpoint reaches an external
        HTTP API
        """
        for method in VALID_METHODS:
            assert annotations_for(method)["openWorldHint"] is True

    def test_every_field_is_set_explicitly_for_every_valid_method(self) -> None:
        """Test that annotations_for() returns all four keys for every VALID_METHODS entry, so none is
        ever left to an SDK default (whose destructiveHint defaults to True)
        """
        for method in VALID_METHODS:
            annotations = annotations_for(method)
            assert set(annotations) == {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}

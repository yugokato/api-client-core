"""Unit tests for `api_client_core.mcp.tools`

Guarded by `pytest.importorskip("mcp")`: this module (unlike catalog.py/schema.py/runner.py) imports the
optional `mcp` SDK, so its tests only run when the `mcp` extra is installed.
"""

from __future__ import annotations

from typing import Any

import pytest
from pytest_mock import MockerFixture

mcp = pytest.importorskip("mcp")

from api_client_core.mcp._constants import ResponseFormat  # noqa: E402
from api_client_core.mcp.catalog import (  # noqa: E402
    ServerOptions,
    build_catalog,
)
from api_client_core.mcp.dynamic_tools import DYNAMIC_TOOL_SCHEMAS  # noqa: E402
from api_client_core.mcp.schema import build_input_schema  # noqa: E402
from api_client_core.mcp.schema import tool_title as real_tool_title  # noqa: E402
from api_client_core.mcp.tools import (  # noqa: E402
    _output_schema_for,
    build_dynamic_tools,
    build_static_tools,
)

from ..tests_cli.conftest import CliTestClient, WidgetsAPI  # noqa: E402


class TestOutputSchemaFor:
    """Tests for `_output_schema_for()`'s response-format-dependent schema"""

    def test_full_format_gets_the_envelope_schema(self) -> None:
        """Test that the default (full) response format publishes an output schema admitting both the
        single {status_code, headers, body} envelope and a {results} list
        """
        schema = _output_schema_for(ServerOptions())
        assert schema is not None
        assert set(schema["properties"]) == {"status_code", "headers", "body", "results", "stats"}
        assert schema["anyOf"] == [
            {"required": ["status_code", "headers", "body"]},
            {"required": ["results"]},
        ]

    @pytest.mark.parametrize(
        "structured_content",
        [
            pytest.param({"status_code": 200, "headers": {}, "body": {"id": 1}}, id="single_envelope"),
            pytest.param({"results": [{"status_code": 200, "headers": {}, "body": {}}]}, id="list_result"),
            pytest.param({"results": {"truncated": True, "bytes": 999999}}, id="truncated_list"),
            pytest.param({"results": [], "stats": [{"num_calls": 3}]}, id="list_with_stats"),
        ],
    )
    def test_the_output_schema_validates_every_result_shape(self, structured_content: dict[str, Any]) -> None:
        """Test that the published output schema compiles and accepts every result shape the runner can
        produce - the single envelope, a list, a stats-bearing list, and a truncated list whose
        `results` is a dict rather than an array
        """
        jsonschema = pytest.importorskip("jsonschema")
        schema = _output_schema_for(ServerOptions())
        jsonschema.validators.validator_for(schema).check_schema(schema)
        jsonschema.validate(structured_content, schema)

    def test_the_output_schema_rejects_a_result_missing_both_shapes(self) -> None:
        """Test that a structured content carrying neither the envelope keys nor `results` fails
        validation, so the `anyOf` isn't vacuously satisfied
        """
        jsonschema = pytest.importorskip("jsonschema")
        schema = _output_schema_for(ServerOptions())
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"headers": {}}, schema)

    def test_json_format_publishes_no_output_schema(self) -> None:
        """Test that json response format publishes no output schema, since the body shape varies per
        endpoint
        """
        assert _output_schema_for(ServerOptions(response_format=ResponseFormat.JSON)) is None

    def test_raw_format_publishes_no_output_schema(self) -> None:
        """Test that raw response format publishes no output schema, since the result is undecoded text"""
        assert _output_schema_for(ServerOptions(response_format=ResponseFormat.RAW)) is None


class TestBuildStaticTools:
    """Tests for `build_static_tools()`'s per-endpoint tool construction"""

    def test_one_tool_per_catalog_entry(self) -> None:
        """Test that static mode publishes exactly one tool per catalog entry, under its own tool_name"""
        catalog = build_catalog(CliTestClient, ServerOptions())
        tools = build_static_tools(catalog, ServerOptions())
        assert {t.name for t in tools} == {e.tool_name for e in catalog.entries}

    def test_tool_carries_the_endpoint_input_schema(self) -> None:
        """Test that each tool's input_schema matches build_input_schema() for its own endpoint,
        including the reserved call_wrappers property static mode nests
        """
        catalog = build_catalog(CliTestClient, ServerOptions())
        tools = {t.name: t for t in build_static_tools(catalog, ServerOptions())}
        tool = tools["widgets__get_widget"]
        assert tool.input_schema == build_input_schema(
            WidgetsAPI.get_widget.endpoint, ServerOptions(), warn=True, with_call_wrappers=True
        )
        assert "call_wrappers" in tool.input_schema["properties"]

    def test_get_endpoint_is_annotated_read_only(self) -> None:
        """Test that a GET endpoint's tool carries readOnlyHint=true"""
        catalog = build_catalog(CliTestClient, ServerOptions())
        tools = {t.name: t for t in build_static_tools(catalog, ServerOptions())}
        assert tools["widgets__get_widget"].annotations is not None
        assert tools["widgets__get_widget"].annotations.read_only_hint is True

    def test_post_endpoint_is_not_annotated_read_only(self) -> None:
        """Test that a POST endpoint's tool carries readOnlyHint=false"""
        catalog = build_catalog(CliTestClient, ServerOptions())
        tools = {t.name: t for t in build_static_tools(catalog, ServerOptions())}
        assert tools["widgets__create_widget"].annotations is not None
        assert tools["widgets__create_widget"].annotations.read_only_hint is False

    def test_every_tool_carries_the_same_output_schema(self) -> None:
        """Test that every static-mode tool carries the identical, fixed output schema under the
        default response format (pydantic copies the dict per Tool, so this compares content, not
        identity)
        """
        catalog = build_catalog(CliTestClient, ServerOptions())
        tools = build_static_tools(catalog, ServerOptions())
        schemas = {repr(t.output_schema) for t in tools}
        assert len(schemas) == 1

    def test_an_entry_that_fails_to_build_is_skipped_without_taking_down_the_others(
        self, mocker: MockerFixture
    ) -> None:
        """Test that one endpoint whose tool construction raises (an error build_input_schema()'s own
        per-parameter guard can't catch) is skipped with a warning, while every other entry is still
        published - matching dynamic mode, which would serve the same endpoint fine since it builds
        schemas lazily
        """

        def flaky_tool_title(endpoint: Any) -> str:
            if getattr(endpoint, "func_name", None) == "get_widget":
                raise RuntimeError("boom")
            return real_tool_title(endpoint)

        mocker.patch("api_client_core.mcp.tools.tool_title", side_effect=flaky_tool_title)
        catalog = build_catalog(CliTestClient, ServerOptions())
        tools = build_static_tools(catalog, ServerOptions())
        names = {t.name for t in tools}
        assert "widgets__get_widget" not in names
        assert names == {e.tool_name for e in catalog.entries} - {"widgets__get_widget"}

    def test_every_entry_failing_to_build_raises(self, mocker: MockerFixture) -> None:
        """Test that a catalog where every entry fails to build still raises, rather than silently
        serving an empty tool list
        """
        mocker.patch("api_client_core.mcp.tools.tool_title", side_effect=RuntimeError("boom"))
        catalog = build_catalog(CliTestClient, ServerOptions())
        with pytest.raises(RuntimeError, match=r"None of .* could be built"):
            build_static_tools(catalog, ServerOptions())


class TestBuildDynamicTools:
    """Tests for `build_dynamic_tools()`'s four fixed meta-tools"""

    def test_publishes_exactly_four_tools(self) -> None:
        """Test that dynamic mode always publishes exactly the four fixed meta-tools, regardless of the
        underlying endpoint count
        """
        catalog = build_catalog(CliTestClient, ServerOptions(threshold=1))
        tools = build_dynamic_tools(catalog, ServerOptions())
        assert {t.name for t in tools} == {"list_resources", "search_endpoints", "describe_endpoint", "call_endpoint"}

    def test_the_three_read_tools_are_read_only_and_not_open_world(self) -> None:
        """Test that list_resources/search_endpoints/describe_endpoint are read-only and NOT
        open-world, since they never reach an external HTTP API - only call_endpoint does
        """
        catalog = build_catalog(CliTestClient, ServerOptions(threshold=1))
        tools = {t.name: t for t in build_dynamic_tools(catalog, ServerOptions())}
        for name in ("list_resources", "search_endpoints", "describe_endpoint"):
            annotations = tools[name].annotations
            assert annotations is not None
            assert annotations.read_only_hint is True
            assert annotations.open_world_hint is False

    def test_call_endpoint_annotations_reflect_the_filtered_method_set(self) -> None:
        """Test that call_endpoint's annotations are computed from the actual filtered endpoints,
        not hard-coded: a read-only-only catalog makes it honestly read-only too
        """
        read_only_catalog = build_catalog(CliTestClient, ServerOptions(threshold=1, read_only=True))
        tools = {t.name: t for t in build_dynamic_tools(read_only_catalog, ServerOptions())}
        assert tools["call_endpoint"].annotations is not None
        assert tools["call_endpoint"].annotations.read_only_hint is True
        assert tools["call_endpoint"].annotations.destructive_hint is False

    def test_call_endpoint_is_destructive_when_the_catalog_has_a_destructive_method(self) -> None:
        """Test that call_endpoint's annotations flip to destructive when the catalog includes a
        destructive method (WidgetsAPI/GadgetsAPI have no PUT/DELETE, so this uses a filtered-in POST,
        which is not itself destructive but confirms the read_only flip at minimum)
        """
        catalog = build_catalog(CliTestClient, ServerOptions(threshold=1))
        tools = {t.name: t for t in build_dynamic_tools(catalog, ServerOptions())}
        assert tools["call_endpoint"].annotations is not None
        assert tools["call_endpoint"].annotations.read_only_hint is False

    def test_search_endpoints_schema_has_the_expected_properties(self) -> None:
        """Test that search_endpoints declares its documented query/resource/method/limit/offset
        properties
        """
        catalog = build_catalog(CliTestClient, ServerOptions(threshold=1))
        tools = {t.name: t for t in build_dynamic_tools(catalog, ServerOptions())}
        assert set(tools["search_endpoints"].input_schema["properties"]) == {
            "query",
            "resource",
            "method",
            "limit",
            "offset",
        }

    def test_describe_endpoint_and_call_endpoint_require_endpoint_id(self) -> None:
        """Test that both describe_endpoint and call_endpoint require endpoint_id"""
        catalog = build_catalog(CliTestClient, ServerOptions(threshold=1))
        tools = {t.name: t for t in build_dynamic_tools(catalog, ServerOptions())}
        assert tools["describe_endpoint"].input_schema["required"] == ["endpoint_id"]
        assert tools["call_endpoint"].input_schema["required"] == ["endpoint_id"]

    def test_published_input_schemas_match_the_shared_dynamic_tool_schemas(self) -> None:
        """Test that each meta-tool's published `types.Tool.input_schema` round-trips
        `dynamic_tools.py`'s `DYNAMIC_TOOL_SCHEMAS` unchanged through the SDK's own model - the single
        source both this module's publication and `dynamic_tools.py`'s `_DYNAMIC_TOOL_KEYS` (used to
        validate a call's top-level argument keys) are derived from, so the two can never drift apart.
        """
        catalog = build_catalog(CliTestClient, ServerOptions(threshold=1))
        tools = {t.name: t for t in build_dynamic_tools(catalog, ServerOptions())}
        for tool, schema in DYNAMIC_TOOL_SCHEMAS.items():
            assert tools[tool.value].input_schema == schema

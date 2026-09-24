"""Unit tests for `api_client_core.mcp.dynamic_tools`"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from api_client_core.mcp import dynamic_tools
from api_client_core.mcp._constants import (
    SEARCH_ENDPOINTS_DEFAULT_LIMIT,
    SEARCH_ENDPOINTS_MAX_LIMIT,
    SEARCH_ENDPOINTS_MIN_LIMIT,
)
from api_client_core.mcp.catalog import ServerOptions, build_catalog
from api_client_core.mcp.dynamic_tools import (
    _describe_endpoint_payload,
    _json_result,
    _search_endpoints_result,
    dispatch_dynamic_tool,
)

from ..tests_cli.conftest import CliTestClient, Status


class TestSearchEndpoints:
    """Tests for `_search_endpoints_result()`'s `limit`/`offset` clamping.

    The low-level `mcp.server` SDK never validates a tool call's arguments against its own published
    input schema (only an SDK client does), so these bounds must be enforced here directly, not merely
    relied on to be honored by whatever calls this tool.
    """

    def test_oversized_limit_is_clamped_to_the_published_maximum(self) -> None:
        """Test that a limit far beyond SEARCH_ENDPOINTS_MAX_LIMIT is clamped down to it, rather than
        returning the entire catalog and defeating the reason dynamic mode exists
        """
        catalog = build_catalog(CliTestClient, ServerOptions())
        result = _search_endpoints_result(catalog, {"limit": 100_000})
        assert result.is_error is False
        assert result.structured["limit"] == SEARCH_ENDPOINTS_MAX_LIMIT

    def test_negative_offset_is_clamped_to_zero(self) -> None:
        """Test that a negative offset is clamped to 0, rather than silently returning a page counted
        from the end of the list
        """
        catalog = build_catalog(CliTestClient, ServerOptions())
        result = _search_endpoints_result(catalog, {"offset": -5})
        assert result.is_error is False
        assert result.structured["offset"] == 0

    def test_below_minimum_limit_is_clamped_up_to_it(self) -> None:
        """Test that a limit of 0 (or below) is clamped up to SEARCH_ENDPOINTS_MIN_LIMIT, not left as a
        page that can never return a result
        """
        catalog = build_catalog(CliTestClient, ServerOptions())
        result = _search_endpoints_result(catalog, {"limit": 0})
        assert result.is_error is False
        assert result.structured["limit"] == SEARCH_ENDPOINTS_MIN_LIMIT

    def test_in_range_values_pass_through_unchanged(self) -> None:
        """Test that a limit/offset already within bounds is used as given, not altered by clamping"""
        catalog = build_catalog(CliTestClient, ServerOptions())
        result = _search_endpoints_result(catalog, {"limit": 2, "offset": 1})
        assert result.is_error is False
        assert result.structured["limit"] == 2
        assert result.structured["offset"] == 1
        assert len(result.structured["results"]) == 2

    def test_default_limit_matches_the_published_default(self) -> None:
        """Test that omitting limit/offset falls back to SEARCH_ENDPOINTS_DEFAULT_LIMIT/0, matching the
        tool's own published schema defaults
        """
        catalog = build_catalog(CliTestClient, ServerOptions())
        result = _search_endpoints_result(catalog, {})
        assert result.is_error is False
        assert result.structured["limit"] == SEARCH_ENDPOINTS_DEFAULT_LIMIT
        assert result.structured["offset"] == 0

    def test_explicit_null_falls_back_to_the_default_the_same_as_omitting_the_key(self) -> None:
        """Test that an explicit JSON null for limit/offset is treated the same as an omitted key,
        not passed to int() as-is, since a model has no way to distinguish the two intents when
        leaving an optional argument unused

        Regression test: this used to raise "'limit' and 'offset' must be integers" for a null value,
        while the sibling resource/method/query arguments already tolerated null via a truthiness check.
        """
        catalog = build_catalog(CliTestClient, ServerOptions())
        result = _search_endpoints_result(catalog, {"limit": None, "offset": None})
        assert result.is_error is False
        assert result.structured["limit"] == SEARCH_ENDPOINTS_DEFAULT_LIMIT
        assert result.structured["offset"] == 0

    @pytest.mark.parametrize("value", [True, False, 1.5, "5", float("inf")])
    def test_non_int_values_are_rejected(self, value: Any) -> None:
        """Test that a bool, float, numeric string, or infinity is rejected as a non-integer, rather than
        silently coerced (a bool truthiness-coerced to 0/1, a float truncated, a numeric string parsed)
        """
        catalog = build_catalog(CliTestClient, ServerOptions())
        for arguments in ({"limit": value}, {"offset": value}):
            result = _search_endpoints_result(catalog, arguments)
            assert result.is_error is True
            assert result.content == "'limit' and 'offset' must be integers"


class TestDescribeEndpointPayload:
    """Tests for `_describe_endpoint_payload()`'s payload shape: one `input_schema`, not a second,
    duplicative `parameters` block
    """

    def test_payload_has_no_separate_parameters_block(self) -> None:
        """Test that the payload carries only input_schema for its parameters, not a second
        `parameters` key repeating the same required/deprecated/description facts
        """
        catalog = build_catalog(CliTestClient, ServerOptions())
        entry = catalog.by_name["widgets__get_widget"]
        payload = _describe_endpoint_payload(catalog, entry, ServerOptions())
        assert "parameters" not in payload
        assert "widget_id" in payload["input_schema"]["properties"]

    def test_each_property_carries_its_own_request_location(self) -> None:
        """Test that a path parameter's own input_schema property carries x-location: path, the one
        fact the removed `parameters` block used to add on its own
        """
        catalog = build_catalog(CliTestClient, ServerOptions())
        entry = catalog.by_name["widgets__get_widget"]
        payload = _describe_endpoint_payload(catalog, entry, ServerOptions())
        assert payload["input_schema"]["properties"]["widget_id"]["x-location"] == "path"

    def test_annotations_use_mcp_wire_key_casing(self) -> None:
        """Test that describe_endpoint's annotations use the MCP wire names (readOnlyHint, not
        read_only_hint) - this payload is embedded verbatim, not built through the SDK's own
        ToolAnnotations model, so a key mismatch here would silently diverge from what the same
        endpoint's static-mode tool actually publishes on the wire
        """
        catalog = build_catalog(CliTestClient, ServerOptions())
        entry = catalog.by_name["widgets__get_widget"]
        payload = _describe_endpoint_payload(catalog, entry, ServerOptions())
        assert set(payload["annotations"]) == {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}


class TestDescribeEndpointSchemaCache:
    """Tests that `describe_endpoint` builds an endpoint's input schema once per server, not once per
    call, unlike the pre-fix behavior that rebuilt it (and re-logged its diagnostics) every time
    """

    @staticmethod
    async def _describe(catalog: Any, endpoint_id: str) -> dict[str, Any]:
        """Dispatch one `describe_endpoint` call and return its decoded payload."""
        result = await dispatch_dynamic_tool(
            MagicMock(), catalog, "describe_endpoint", {"endpoint_id": endpoint_id}, ServerOptions()
        )
        return json.loads(result.content)

    async def test_repeated_describe_reuses_the_built_schema(self, mocker: MockerFixture) -> None:
        """Test that two describe_endpoint dispatches for the same endpoint build its schema once and
        hand back the same result, so a long-lived server doesn't re-walk the model per call
        """
        catalog = build_catalog(CliTestClient, ServerOptions())
        spy = mocker.spy(dynamic_tools, "build_input_schema")

        first = await self._describe(catalog, "widgets__create_widget")
        second = await self._describe(catalog, "widgets__create_widget")

        assert spy.call_count == 1
        assert first["input_schema"] == second["input_schema"]
        assert "widgets__create_widget" in catalog.input_schema_cache

    async def test_distinct_endpoints_are_cached_independently(self, mocker: MockerFixture) -> None:
        """Test that the cache is keyed per endpoint, so describing a second endpoint still builds its
        own schema rather than returning the first one's
        """
        catalog = build_catalog(CliTestClient, ServerOptions())
        spy = mocker.spy(dynamic_tools, "build_input_schema")

        for endpoint_id in ("widgets__create_widget", "widgets__get_widget", "widgets__create_widget"):
            await self._describe(catalog, endpoint_id)

        assert spy.call_count == 2
        assert set(catalog.input_schema_cache) == {"widgets__create_widget", "widgets__get_widget"}


class TestCallEndpointArguments:
    """Tests for `dispatch_dynamic_tool()`'s `call_endpoint` `"arguments"` validation, ahead of ever
    dispatching a real call
    """

    @pytest.mark.parametrize("bad_arguments", [[], 0, "", [1, 2]], ids=["empty_list", "zero", "empty_str", "list"])
    async def test_non_object_arguments_is_rejected_regardless_of_truthiness(self, bad_arguments: Any) -> None:
        """Test that every non-object "arguments" value is rejected the same way, including a falsy
        one (an empty list, 0, an empty string) that `... or {}` would otherwise silently treat as
        "no arguments" instead of a validation error
        """
        catalog = build_catalog(CliTestClient, ServerOptions(threshold=1))
        entry = catalog.by_name["widgets__list_widgets"]
        arguments = {"endpoint_id": entry.tool_name, "arguments": bad_arguments}
        result = await dispatch_dynamic_tool(None, catalog, "call_endpoint", arguments, ServerOptions())
        assert result.is_error is True
        assert "'arguments' must be an object" in result.content

    async def test_call_wrappers_is_read_at_the_top_level_and_forwarded_to_dispatch(
        self, mocker: MockerFixture
    ) -> None:
        """Test that `call_endpoint`'s own top-level `call_wrappers` key is passed through to
        `dispatch_endpoint_call()`, not folded into the endpoint's `arguments`
        """
        catalog = build_catalog(CliTestClient, ServerOptions(threshold=1))
        entry = catalog.by_name["widgets__list_widgets"]
        spy = mocker.patch.object(dynamic_tools, "dispatch_endpoint_call", autospec=True)
        arguments = {
            "endpoint_id": entry.tool_name,
            "arguments": {"limit": 5},
            "call_wrappers": {"with_repeat": {"num": 2}},
        }
        await dispatch_dynamic_tool(MagicMock(), catalog, "call_endpoint", arguments, ServerOptions())
        assert spy.call_args.kwargs["call_wrappers"] == {"with_repeat": {"num": 2}}
        assert spy.call_args.args[2] == {"limit": 5}

    async def test_a_bad_call_wrappers_spec_is_a_clean_error(self) -> None:
        """Test that a malformed `call_wrappers` object routed through `call_endpoint` surfaces as an
        isError result rather than a raised exception
        """
        catalog = build_catalog(CliTestClient, ServerOptions(threshold=1))
        entry = catalog.by_name["widgets__list_widgets"]
        arguments = {"endpoint_id": entry.tool_name, "arguments": {}, "call_wrappers": {"with_bogus": {}}}
        result = await dispatch_dynamic_tool(MagicMock(), catalog, "call_endpoint", arguments, ServerOptions())
        assert result.is_error is True
        assert "Unknown call wrapper" in result.content

    async def test_call_wrappers_nested_inside_arguments_gets_a_dedicated_hint(self) -> None:
        """Test that `call_wrappers` given inside `arguments` instead of beside it - an easy mistake,
        since `arguments` is described as "this endpoint's arguments" - is called out by name rather
        than reported as just another unknown endpoint parameter
        """
        catalog = build_catalog(CliTestClient, ServerOptions(threshold=1))
        entry = catalog.by_name["widgets__list_widgets"]
        arguments = {
            "endpoint_id": entry.tool_name,
            "arguments": {"limit": 5, "call_wrappers": {"with_repeat": {"num": 2}}},
        }
        result = await dispatch_dynamic_tool(MagicMock(), catalog, "call_endpoint", arguments, ServerOptions())
        assert result.is_error is True
        assert "call_endpoint({endpoint_id, arguments, call_wrappers})" in result.content


class TestJsonResult:
    """Tests for `_json_result()`'s handling of a payload that isn't already JSON-safe - e.g. a
    Literal[...] parameter whose own choices are Enum members or bytes, which _literal_schema() embeds
    verbatim rather than rendering by name the way an Enum-typed property's own schema does
    """

    def test_non_json_safe_value_is_stringified_rather_than_raising(self) -> None:
        """Test that a payload containing a raw Enum member doesn't raise TypeError - the bug this
        would otherwise cause: describe_endpoint (and search_endpoints/list_resources) crashing outright
        for any endpoint whose schema happens to embed one
        """
        result = _json_result({"input_schema": {"properties": {"color": {"enum": [Status.ACTIVE]}}}})
        assert result.is_error is False
        assert "ACTIVE" in result.content

    def test_structured_content_matches_the_stringified_text_content(self) -> None:
        """Test that structured is derived from the same rendered text as content, via a JSON round
        trip, rather than the original (possibly non-JSON-safe) payload dict directly
        """
        result = _json_result({"input_schema": {"properties": {"color": {"enum": [Status.ACTIVE]}}}})
        assert result.structured == json.loads(result.content)

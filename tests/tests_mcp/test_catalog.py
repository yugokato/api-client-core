"""Unit tests for `api_client_core.mcp.catalog`"""

from __future__ import annotations

from functools import cached_property
from typing import Unpack

import pytest

from api_client_core import APIClient, BaseAPI, endpoint
from api_client_core.mcp._constants import MAX_TOOL_NAME_LEN, Mode, ResponseFormat
from api_client_core.mcp.catalog import ServerOptions, _matches_filters, _resolve_mode, _tool_name_for, build_catalog
from api_client_core.types import Kwargs, RestResponse

from ..tests_cli.conftest import CliTestClient, WidgetsAPI, module_scoped


class TestServerOptionsCoercion:
    """Tests for `ServerOptions.__post_init__()`'s enum coercion"""

    def test_plain_string_mode_coerces_to_the_real_enum_member(self) -> None:
        """Test that a plain string given for mode coerces to the real Mode member, so downstream `is`
        comparisons (_resolve_mode(), ...) still match it
        """
        options = ServerOptions(mode="static")
        assert options.mode is Mode.STATIC

    def test_plain_string_response_format_coerces_to_the_real_enum_member(self) -> None:
        """Test that a plain string given for response_format coerces to the real ResponseFormat
        member, so downstream `is` comparisons (_shape_result(), ...) still match it
        """
        options = ServerOptions(response_format="json")
        assert options.response_format is ResponseFormat.JSON

    def test_real_enum_members_pass_through_unchanged(self) -> None:
        """Test that already-real enum members (as _options_from_args() always passes) round-trip as-is"""
        options = ServerOptions(mode=Mode.DYNAMIC, response_format=ResponseFormat.RAW)
        assert options.mode is Mode.DYNAMIC
        assert options.response_format is ResponseFormat.RAW


class TestToolNameFor:
    """Tests for `_tool_name_for()`'s derivation of an MCP tool name / catalog `endpoint_id`"""

    def test_joins_resource_and_func_name_with_double_underscore(self) -> None:
        """Test that a resource attribute name and a func name join as `resource__func_name`"""
        assert _tool_name_for("products", "get_product") == "products__get_product"

    def test_normalizes_case_and_illegal_characters(self) -> None:
        """Test that each part is lowercased and any char outside [a-z0-9_] becomes an underscore"""
        assert _tool_name_for("MyResource", "getThing") == "myresource__getthing"

    def test_collapses_and_strips_stray_underscores_without_widening_the_separator(self) -> None:
        """Test that leading/trailing/doubled underscores in either part are cleaned up before joining,
        so the join seam always ends up exactly `__`, never `___` or more
        """
        assert _tool_name_for("my_resource_", "_get__thing") == "my_resource__get_thing"

    def test_prefix_is_prepended_ahead_of_resource(self) -> None:
        """Test that a given `--tool-prefix` becomes the leading component"""
        assert _tool_name_for("products", "get_product", prefix="myapp") == "myapp__products__get_product"

    def test_no_prefix_when_none_given(self) -> None:
        """Test that omitting the prefix produces the same name as not passing it at all"""
        assert _tool_name_for("products", "get_product", prefix=None) == _tool_name_for("products", "get_product")

    def test_over_length_name_is_truncated_with_a_deterministic_hash_suffix(self) -> None:
        """Test that a name exceeding MAX_TOOL_NAME_LEN is shortened and given a hash suffix, rather than
        silently dropped, left overlong, or made non-deterministic
        """
        long_name = _tool_name_for("a" * 40, "b" * 40)
        assert len(long_name) <= MAX_TOOL_NAME_LEN
        assert long_name == _tool_name_for("a" * 40, "b" * 40)

    def test_over_length_names_that_differ_produce_different_suffixes(self) -> None:
        """Test that two overlong inputs sharing an identical truncated prefix still produce different
        names, proving the hash is computed over the full name rather than just the kept prefix
        """
        first = _tool_name_for("a" * 40, "b" * 15 + "1" * 25)
        second = _tool_name_for("a" * 40, "b" * 15 + "2" * 25)
        assert first != second


class TestResolveMode:
    """Tests for `_resolve_mode()`'s auto-mode threshold boundary"""

    def test_below_threshold_resolves_to_static(self) -> None:
        """Test that a count strictly below the threshold resolves to static mode"""
        assert _resolve_mode(Mode.AUTO, 49, 50) is Mode.STATIC

    def test_at_threshold_resolves_to_dynamic(self) -> None:
        """Test that a count exactly at the threshold resolves to dynamic mode, matching 'at or above'"""
        assert _resolve_mode(Mode.AUTO, 50, 50) is Mode.DYNAMIC

    def test_above_threshold_resolves_to_dynamic(self) -> None:
        """Test that a count above the threshold resolves to dynamic mode"""
        assert _resolve_mode(Mode.AUTO, 51, 50) is Mode.DYNAMIC

    def test_forced_static_is_honored_below_threshold(self) -> None:
        """Test that an explicitly requested static mode is returned unchanged when under the threshold"""
        assert _resolve_mode(Mode.STATIC, 10, 50) is Mode.STATIC

    def test_forced_static_is_honored_above_threshold(self) -> None:
        """Test that an explicitly requested static mode is still honored even above the threshold,
        since forcing it is the operator's call
        """
        assert _resolve_mode(Mode.STATIC, 100, 50) is Mode.STATIC

    def test_forced_dynamic_is_honored_below_threshold(self) -> None:
        """Test that an explicitly requested dynamic mode is honored even under the threshold"""
        assert _resolve_mode(Mode.DYNAMIC, 5, 50) is Mode.DYNAMIC


class TestMatchesFilters:
    """Tests for `_matches_filters()`'s per-endpoint filter precedence"""

    def test_no_filters_matches_everything(self) -> None:
        """Test that an endpoint survives when no filter is given at all"""
        endpoint_obj = WidgetsAPI.get_widget.endpoint
        assert _matches_filters("widgets", endpoint_obj, ServerOptions(), effective_methods=None)

    def test_resource_filter_excludes_a_non_matching_resource(self) -> None:
        """Test that a `--resource` filter naming other resources excludes this one"""
        endpoint_obj = WidgetsAPI.get_widget.endpoint
        options = ServerOptions(resources=("gadgets",))
        assert not _matches_filters("widgets", endpoint_obj, options, effective_methods=None)

    def test_resource_filter_keeps_a_matching_resource(self) -> None:
        """Test that a `--resource` filter naming this resource keeps it"""
        endpoint_obj = WidgetsAPI.get_widget.endpoint
        options = ServerOptions(resources=("widgets",))
        assert _matches_filters("widgets", endpoint_obj, options, effective_methods=None)

    def test_method_filter_excludes_a_non_matching_method(self) -> None:
        """Test that an effective method filter not containing the endpoint's method excludes it"""
        endpoint_obj = WidgetsAPI.create_widget.endpoint  # post
        assert not _matches_filters("widgets", endpoint_obj, ServerOptions(), effective_methods=frozenset({"get"}))

    def test_include_glob_excludes_a_non_matching_name(self) -> None:
        """Test that an `--include` glob that doesn't match the tool name excludes it"""
        endpoint_obj = WidgetsAPI.get_widget.endpoint
        options = ServerOptions(include=("gadgets__*",))
        assert not _matches_filters("widgets", endpoint_obj, options, effective_methods=None)

    def test_include_glob_keeps_a_matching_name(self) -> None:
        """Test that an `--include` glob matching the tool name keeps it"""
        endpoint_obj = WidgetsAPI.get_widget.endpoint
        options = ServerOptions(include=("widgets__*",))
        assert _matches_filters("widgets", endpoint_obj, options, effective_methods=None)

    def test_exclude_glob_wins_over_a_matching_include(self) -> None:
        """Test that `--exclude` applies last and wins even when `--include` would have kept the endpoint"""
        endpoint_obj = WidgetsAPI.get_widget.endpoint
        options = ServerOptions(include=("widgets__*",), exclude=("widgets__get_widget",))
        assert not _matches_filters("widgets", endpoint_obj, options, effective_methods=None)


class TestEffectiveMethods:
    """Tests for `_effective_methods()`'s combination of `--method` and `--read-only`, exercised through
    `build_catalog()` since it's a private helper
    """

    def test_method_and_read_only_intersect(self) -> None:
        """Test that `--method get --read-only` keeps only get (both already read-only)"""
        catalog = build_catalog(CliTestClient, ServerOptions(methods=("get",), read_only=True))
        assert {e.endpoint.method for e in catalog.entries} == {"get"}

    def test_empty_intersection_raises(self) -> None:
        """Test that `--method post --read-only` (no overlap: post isn't read-only) raises a usage error
        naming both, rather than silently serving zero tools. `RuntimeError`, not a bare `ValueError`,
        so `write_error()`/`_format_exception()` render it as a clean sentence, matching every other
        startup usage error `build_catalog()` raises
        """
        with pytest.raises(RuntimeError, match=r"post.*read-only|read-only.*post"):
            build_catalog(CliTestClient, ServerOptions(methods=("post",), read_only=True))


class TestBuildCatalog:
    """Tests for `build_catalog()`'s full discovery -> filter -> name -> mode-resolve pipeline"""

    def test_builds_every_endpoint_from_both_resources(self) -> None:
        """Test that every endpoint from both `widgets` (cached_property) and `gadgets` (property) is
        included, confirming both resource-discovery paths are walked
        """
        catalog = build_catalog(CliTestClient, ServerOptions())
        assert catalog.app_name == "cli-test"
        assert {e.tool_name for e in catalog.entries} == {
            "widgets__get_widget",
            "widgets__create_widget",
            "widgets__upload_avatar",
            "widgets__list_widgets",
            "gadgets__get_gadget",
        }

    def test_by_name_and_resources_indexes_match_entries(self) -> None:
        """Test that `by_name` and `resources` are exact groupings of the same `entries`"""
        catalog = build_catalog(CliTestClient, ServerOptions())
        assert set(catalog.by_name) == {e.tool_name for e in catalog.entries}
        assert sum(len(v) for v in catalog.resources.values()) == len(catalog.entries)
        assert {e.tool_name for e in catalog.resources["widgets"]} == {
            "widgets__get_widget",
            "widgets__create_widget",
            "widgets__upload_avatar",
            "widgets__list_widgets",
        }

    def test_default_threshold_resolves_a_small_client_to_static(self) -> None:
        """Test that the 5-endpoint synthetic client resolves to static mode under the default threshold"""
        catalog = build_catalog(CliTestClient, ServerOptions())
        assert catalog.mode is Mode.STATIC

    def test_low_threshold_resolves_the_same_client_to_dynamic(self) -> None:
        """Test that lowering --threshold below the endpoint count flips the same client to dynamic mode"""
        catalog = build_catalog(CliTestClient, ServerOptions(threshold=3))
        assert catalog.mode is Mode.DYNAMIC

    def test_resource_filter_narrows_before_mode_resolution(self) -> None:
        """Test that --resource is applied before the threshold check: filtering down to one resource
        changes both the entry count and (with a low threshold) the resolved mode
        """
        catalog = build_catalog(CliTestClient, ServerOptions(resources=("gadgets",), threshold=1))
        assert {e.tool_name for e in catalog.entries} == {"gadgets__get_gadget"}
        assert catalog.mode is Mode.DYNAMIC  # 1 endpoint >= threshold of 1

    def test_no_resources_discovered_raises(self) -> None:
        """Test that a client exposing no BaseAPI resource at all raises RuntimeError"""

        class _EmptyClient(APIClient):
            app_name = "empty-test"

        with pytest.raises(RuntimeError, match="No API classes discovered"):
            build_catalog(_EmptyClient, ServerOptions())

    def test_missing_app_name_raises(self) -> None:
        """Test that a client class with no app_name set raises RuntimeError naming it, rather than
        reaching discovery with an unusable app identity
        """

        class _UnnamedClient(APIClient):
            pass

        with pytest.raises(RuntimeError, match="app_name"):
            build_catalog(_UnnamedClient, ServerOptions())

    def test_filters_matching_nothing_raises(self) -> None:
        """Test that a filter combination excluding every endpoint raises RuntimeError, rather than
        silently building an empty catalog
        """
        with pytest.raises(RuntimeError, match="No endpoints matched"):
            build_catalog(CliTestClient, ServerOptions(resources=("nonexistent",)))

    def test_filters_matching_nothing_names_the_actually_discovered_resources_and_methods(self) -> None:
        """Test that the error names what was actually discovered - the resources and HTTP methods
        present - so an operator debugging a filter that matched nothing doesn't have to re-run with
        --log-level DEBUG just to see what was available
        """
        with pytest.raises(RuntimeError, match=r"Discovered resources: gadgets, widgets\. Methods present: .*get.*"):
            build_catalog(CliTestClient, ServerOptions(resources=("nonexistent",)))

    def test_client_with_resources_but_no_endpoints_gets_a_distinct_error(self) -> None:
        """Test that a client whose resource classes define no endpoints raises an error that names the
        real cause, not one telling the operator to loosen filters they never set, and that the
        `Methods present:` clause is dropped when nothing was discovered
        """

        @module_scoped
        class _StubAPI(BaseAPI):
            """A resource group that happens to define no endpoints yet."""

            app_name = "stub-catalog-test"

        @module_scoped
        class _StubClient(APIClient):
            app_name = "stub-catalog-test"

            @cached_property
            def things(self) -> _StubAPI:
                return _StubAPI(self)

        with pytest.raises(RuntimeError, match=r"none of them define any endpoints") as exc_info:
            build_catalog(_StubClient, ServerOptions())
        assert "filter" not in str(exc_info.value).lower()
        assert "Methods present" not in str(exc_info.value)

    def test_tool_prefix_that_normalizes_to_an_empty_token_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        """Test that a --tool-prefix made only of characters that normalize away (so it silently has no
        effect on any tool name) is called out with a warning rather than passing unnoticed
        """
        with caplog.at_level("WARNING"):
            catalog = build_catalog(CliTestClient, ServerOptions(tool_prefix="!!!"))
        assert all(not e.tool_name.startswith("_") for e in catalog.entries)
        assert "tool-prefix" in caplog.text
        assert "no effect" in caplog.text

    def test_colliding_tool_names_keep_the_first_in_order_and_warn(self, caplog: pytest.LogCaptureFixture) -> None:
        """Test that two endpoints whose tool names normalize to the same value keep the first
        (deterministic sorted-order) entry and log a warning naming the one dropped, rather than
        silently overwriting one with the other
        """

        @module_scoped
        class _FooAPI(BaseAPI):
            app_name = "collision-catalog-test"

            @endpoint.get("/bar")
            def bar(self, **kwargs: Unpack[Kwargs]) -> RestResponse: ...

        @module_scoped
        class _FooTrailingAPI(BaseAPI):
            app_name = "collision-catalog-test"

            @endpoint.get("/bar2")
            def bar(self, **kwargs: Unpack[Kwargs]) -> RestResponse: ...

        @module_scoped
        class _CollisionClient(APIClient):
            app_name = "collision-catalog-test"

            @cached_property
            def foo(self) -> _FooAPI:
                return _FooAPI(self)

            @cached_property
            def foo_(self) -> _FooTrailingAPI:
                return _FooTrailingAPI(self)

        with caplog.at_level("WARNING"):
            catalog = build_catalog(_CollisionClient, ServerOptions())

        assert len(catalog.entries) == 1
        assert catalog.entries[0].tool_name == "foo__bar"
        assert catalog.entries[0].resource == "foo"  # "foo" sorts before "foo_", so it wins
        assert "foo__bar" in caplog.text

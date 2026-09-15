"""Unit tests for `api_client_core.mcp.wrappers`"""

from __future__ import annotations

import re
from typing import Any

import pytest

from api_client_core.mcp._constants import CALL_WRAPPERS_KEY
from api_client_core.mcp.errors import ToolArgumentError
from api_client_core.mcp.wrappers import (
    call_wrappers_schema,
    parse_call_wrappers,
    split_call_wrappers,
)


class TestParseCallWrappersValid:
    """Tests for `parse_call_wrappers()`'s handling of valid input"""

    @pytest.mark.parametrize("value", [None, {}], ids=["none", "empty_object"])
    def test_absent_or_empty_yields_an_empty_plan(self, value: Any) -> None:
        """Test that a missing or empty `call_wrappers` object produces a plan with no chain, no stats,
        and no expected statuses
        """
        plan = parse_call_wrappers(value)
        assert plan.chain == ()
        assert plan.collect_stats is False
        assert plan.expected_statuses == ()

    def test_chain_is_ordered_canonically_regardless_of_key_order(self) -> None:
        """Test that wrappers fold in the registry's fixed order (retry before lock), not the order the
        JSON keys happen to appear, since JSON object key order is not semantically guaranteed
        """
        plan = parse_call_wrappers({"with_lock": {}, "with_retry": {"num_retries": 1}})
        assert [name for name, _ in plan.chain] == ["with_retry", "with_lock"]

    def test_with_stats_leaves_the_chain_and_sets_collect_stats(self) -> None:
        """Test that `with_stats` is recorded as `collect_stats` and dropped from the chain, since its
        report is attached by the runner's own stats scope rather than folded as a wrapper
        """
        plan = parse_call_wrappers({"with_stats": {}, "with_retry": {}})
        assert plan.collect_stats is True
        assert [name for name, _ in plan.chain] == ["with_retry"]

    def test_return_exceptions_defaults_to_true_for_a_multi_call_wrapper(self) -> None:
        """Test that `with_repeat`/`with_concurrency` get `return_exceptions=True` injected when unset,
        so an N-call group always runs every call, and that an explicit `false` is left alone
        """
        ((_, repeat_opts),) = parse_call_wrappers({"with_repeat": {"num": 3}}).chain
        assert repeat_opts == {"num": 3, "return_exceptions": True}

        ((_, concurrency_opts),) = parse_call_wrappers(
            {"with_concurrency": {"num": 3, "return_exceptions": False}}
        ).chain
        assert concurrency_opts["return_exceptions"] is False

    def test_expected_statuses_is_populated_from_with_expected_status(self) -> None:
        """Test that a `with_expected_status` link's codes surface on the plan's `expected_statuses`"""
        plan = parse_call_wrappers({"with_expected_status": {"status_codes": [404, 500]}})
        assert plan.expected_statuses == (404, 500)

    @pytest.mark.parametrize("condition", [429, [429, 503]], ids=["scalar", "list"])
    def test_retry_condition_accepts_a_scalar_or_a_list(self, condition: Any) -> None:
        """Test that `with_retry`'s `condition` option accepts either a single status code or a list,
        matching the framework parameter's own `int | Sequence[int]` shape
        """
        ((_, opts),) = parse_call_wrappers({"with_retry": {"condition": condition}}).chain
        assert opts == {"condition": condition}

    def test_a_wrapper_given_as_explicit_null_is_treated_the_same_as_empty_options(self) -> None:
        """Test that `{"with_stats": null}` - a plausible way for a model to say "no options" - is
        accepted the same as `{"with_stats": {}}`, rather than raising on the object-type check

        Regression test: this used to raise "'with_stats' options must be an object, got NoneType".
        """
        plan = parse_call_wrappers({"with_stats": None})
        assert plan.collect_stats is True

    def test_an_optional_sub_option_given_as_explicit_null_is_treated_as_omitted(self) -> None:
        """Test that `{"with_retry": {"num_retries": null}}` is accepted the same as omitting
        `num_retries` entirely, rather than raising on the scalar-type check

        Regression test: this used to raise "'with_retry' 'num_retries' must be a int, got NoneType".
        """
        ((_, opts),) = parse_call_wrappers({"with_retry": {"num_retries": None}}).chain
        assert opts == {}

    def test_a_required_sub_option_given_as_explicit_null_still_raises_as_missing(self) -> None:
        """Test that an explicit null for a *required* option still raises "requires ...", the same as
        omitting it, rather than reaching the (also correctly rejecting, but wrongly worded) scalar
        type check instead
        """
        with pytest.raises(ToolArgumentError, match="requires 'max_requests'"):
            parse_call_wrappers({"with_rate_limit": {"max_requests": None}})


class TestParseCallWrappersInvalid:
    """Tests for `parse_call_wrappers()`'s validation, the checks the low-level MCP SDK never applies"""

    @pytest.mark.parametrize(
        ("value", "match"),
        [
            pytest.param([], "must be an object", id="not_an_object"),
            pytest.param({"with_bogus": {}}, "Unknown call wrapper", id="unknown_wrapper"),
            pytest.param({"with_retry": {"bogus": 1}}, "Unknown option", id="unknown_option"),
            pytest.param({"with_retry": {"num_retries": "x"}}, "must be a int", id="wrong_scalar_type"),
            pytest.param({"with_retry": {"num_retries": -1}}, "must be >= 0", id="below_minimum"),
            pytest.param({"with_repeat": {"num": 0}}, "must be >= 1", id="below_minimum_num"),
            pytest.param(
                {"with_rate_limit": {"max_requests": 1, "interval": 0}}, "must be > 0", id="non_positive_interval"
            ),
            pytest.param({"with_expected_status": {}}, "requires 'status_codes'", id="missing_status_codes"),
            pytest.param({"with_expected_status": {"status_codes": []}}, "non-empty array", id="empty_status_codes"),
            pytest.param({"with_rate_limit": {}}, "requires 'max_requests'", id="missing_max_requests"),
            pytest.param(
                {"with_rate_limit": {"interval": 5}}, "requires 'max_requests'", id="missing_max_requests_with_interval"
            ),
            pytest.param({"with_max_response_time": {}}, "requires 'threshold_msecs'", id="missing_threshold_msecs"),
            pytest.param({"with_retry": {"condition": []}}, "must not be an empty array", id="empty_condition_list"),
            pytest.param({"with_lock": {"lock_name": "../escape"}}, "must match", id="lock_name_path_traversal"),
            pytest.param(
                {"with_repeat": {"num": 2}, "with_concurrency": {"num": 2}},
                "mutually exclusive",
                id="both_terminals",
            ),
        ],
    )
    def test_invalid_input_raises_tool_argument_error(self, value: Any, match: str) -> None:
        """Test that every malformed `call_wrappers` shape raises `ToolArgumentError` with a message
        naming the problem, so `dispatch_endpoint_call()` turns it into a clean `isError` result
        """
        with pytest.raises(ToolArgumentError, match=match):
            parse_call_wrappers(value)


class TestCallWrappersSchema:
    """Tests for `call_wrappers_schema()`"""

    def test_top_level_shape_has_one_closed_object_per_wrapper(self) -> None:
        """Test that the schema is a closed object with one sub-object property per registered wrapper"""
        schema = call_wrappers_schema()
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert "with_retry" in schema["properties"]
        assert schema["properties"]["with_retry"]["additionalProperties"] is False

    def test_retry_condition_is_a_scalar_or_non_empty_array_anyof(self) -> None:
        """Test that `with_retry`'s `condition` property is published as `anyOf` of an integer and a
        non-empty array of integers, the one option that isn't a plain scalar
        """
        condition = call_wrappers_schema()["properties"]["with_retry"]["properties"]["condition"]
        assert condition == {
            "anyOf": [{"type": "integer"}, {"type": "array", "items": {"type": "integer"}, "minItems": 1}]
        }

    def test_expected_status_requires_a_non_empty_integer_array(self) -> None:
        """Test that `with_expected_status` publishes `status_codes` as a required, non-empty integer
        array
        """
        wrapper = call_wrappers_schema()["properties"]["with_expected_status"]
        assert wrapper["required"] == ["status_codes"]
        assert wrapper["properties"]["status_codes"] == {
            "type": "array",
            "items": {"type": "integer"},
            "minItems": 1,
        }

    def test_multi_call_num_publishes_only_its_floor(self) -> None:
        """Test that `with_repeat`/`with_concurrency`'s `num` publishes its registry floor with no
        `maximum`, matching the framework's own `with_repeat()`/`with_concurrency()`, which impose no cap
        """
        num = call_wrappers_schema()["properties"]["with_repeat"]["properties"]["num"]
        assert num == {"type": "integer", "minimum": 1}

    def test_a_required_scalar_option_is_published_as_required(self) -> None:
        """Test that a `WrapperSpec.required` scalar option (not just an `array_options` one) is
        published in the wrapper's own `"required"` list
        """
        wrapper = call_wrappers_schema()["properties"]["with_rate_limit"]
        assert wrapper["required"] == ["max_requests"]
        wrapper = call_wrappers_schema()["properties"]["with_max_response_time"]
        assert wrapper["required"] == ["threshold_msecs"]

    def test_lock_name_publishes_its_restricting_pattern(self) -> None:
        """Test that `with_lock`'s `lock_name` publishes the character-class pattern it's restricted
        to, since it becomes a filesystem path component
        """
        lock_name = call_wrappers_schema()["properties"]["with_lock"]["properties"]["lock_name"]
        assert lock_name["type"] == "string"
        assert re.fullmatch(lock_name["pattern"], "job-1_2") is not None
        assert re.fullmatch(lock_name["pattern"], "../escape") is None


class TestSplitCallWrappers:
    """Tests for `split_call_wrappers()`"""

    def test_splits_the_reserved_key_off_the_rest(self) -> None:
        """Test that the reserved `call_wrappers` key is returned separately and removed from the
        remaining arguments
        """
        rest, wrappers = split_call_wrappers({"widget_id": 1, CALL_WRAPPERS_KEY: {"with_retry": {}}})
        assert rest == {"widget_id": 1}
        assert wrappers == {"with_retry": {}}

    def test_absent_reserved_key_yields_none(self) -> None:
        """Test that arguments without the reserved key are returned unchanged with `None` wrappers"""
        rest, wrappers = split_call_wrappers({"widget_id": 1})
        assert rest == {"widget_id": 1}
        assert wrappers is None

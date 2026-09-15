"""Unit tests for `api_client_core._common.wrappers`"""

from __future__ import annotations

from typing import Any

import pytest
from pytest_mock import MockerFixture

from api_client_core._common.wrappers import (
    WITH_CONCURRENCY,
    WITH_REPEAT,
    WRAPPERS,
    apply_wrappers,
    expected_statuses,
)


class TestRegistry:
    """Tests for the `WRAPPERS` registry's internal consistency"""

    def test_every_spec_is_self_consistent(self) -> None:
        """Test that each registry entry is keyed by its own `name`, carries a callable `apply` and a
        non-empty `summary`, and only ever references option names it actually declares
        """
        for name, spec in WRAPPERS.items():
            assert spec.name == name
            assert callable(spec.apply)
            assert spec.summary
            known = set(spec.options) | spec.array_options
            assert set(spec.multi) <= set(spec.options)
            assert set(spec.array_options) <= set(spec.options)
            assert set(spec.required) <= set(spec.options)
            assert set(spec.minimums) <= set(spec.options)
            assert spec.primary is None or spec.primary in known

    def test_only_the_multi_call_wrappers_are_terminal(self) -> None:
        """Test that `terminal` is set on exactly `with_repeat` and `with_concurrency`, the two wrappers
        `EndpointFunc` requires to come last in a chain
        """
        assert {name for name, spec in WRAPPERS.items() if spec.terminal} == {WITH_REPEAT, WITH_CONCURRENCY}


class TestApplyWrappers:
    """Tests for `apply_wrappers()`"""

    def test_returns_the_endpoint_func_unchanged_for_an_empty_chain(self) -> None:
        """Test that folding an empty chain returns the same object, so a call with no wrappers is
        untouched
        """
        ef = object()
        assert apply_wrappers(ef, []) is ef

    def test_folds_each_link_in_iteration_order_with_its_options(self, mocker: MockerFixture) -> None:
        """Test that each `(name, options)` pair calls the matching `with_xxx()` method, with the
        options as keyword arguments, in the order the chain lists them
        """
        ef = mocker.MagicMock()
        apply_wrappers(
            ef,
            [
                ("with_retry", {"num_retries": 2}),
                ("with_lock", {"lock_name": "job"}),
                ("with_expected_status", {"status_codes": [200, 201]}),
            ],
        )
        call_order = [call[0].rsplit(".", 1)[-1] for call in ef.mock_calls]
        assert call_order == ["with_retry", "with_lock", "with_expected_status"]
        assert ef.mock_calls[0].kwargs == {"num_retries": 2}
        assert ef.mock_calls[1].kwargs == {"lock_name": "job"}
        assert ef.mock_calls[2].args == (200, 201)

    def test_a_repeated_name_is_folded_once_per_occurrence(self, mocker: MockerFixture) -> None:
        """Test that the same wrapper name appearing twice in the chain chains two calls, matching
        `.with_x().with_x()` in Python
        """
        ef = mocker.MagicMock()
        apply_wrappers(ef, [("with_retry", {"num_retries": 1}), ("with_retry", {"num_retries": 5})])
        call_order = [call[0].rsplit(".", 1)[-1] for call in ef.mock_calls]
        assert call_order == ["with_retry", "with_retry"]

    def test_a_terminal_link_before_another_link_propagates_the_runtime_error(self, mocker: MockerFixture) -> None:
        """Test that a terminal wrapper folded before a non-terminal one propagates the same
        `RuntimeError` the equivalent Python chain raises, since `apply_wrappers()` never reorders links
        """
        ef = mocker.MagicMock()
        ef.with_repeat.return_value.with_retry.side_effect = RuntimeError(
            "`with_repeat()` is terminal and must always be the last wrapper in a chain."
        )
        with pytest.raises(RuntimeError, match="with_repeat"):
            apply_wrappers(ef, [(WITH_REPEAT, {"num": 3}), ("with_retry", {})])


class TestExpectedStatuses:
    """Tests for `expected_statuses()`"""

    @pytest.mark.parametrize(
        ("chain", "expected"),
        [
            pytest.param([], (), id="no_links"),
            pytest.param([("with_lock", {"lock_name": None})], (), id="no_expected_status_link"),
            pytest.param([("with_expected_status", {"status_codes": [200, 404]})], (200, 404), id="one_link"),
            pytest.param(
                [
                    ("with_expected_status", {"status_codes": [404]}),
                    ("with_lock", {"lock_name": None}),
                    ("with_expected_status", {"status_codes": [500, 502]}),
                ],
                (404, 500, 502),
                id="combined_across_links",
            ),
        ],
    )
    def test_expected_statuses(self, chain: list[tuple[str, dict[str, Any]]], expected: tuple[int, ...]) -> None:
        """Test that codes are flattened across every `with_expected_status` link in chain order, and an
        empty tuple is returned when there is none
        """
        assert expected_statuses(chain) == expected

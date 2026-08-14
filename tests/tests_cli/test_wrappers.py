"""Unit tests for `api_client_core.cli.wrappers`"""

import argparse
from typing import Any

import pytest
from pytest import CaptureFixture
from pytest_mock import MockerFixture

from api_client_core.cli._constants import NOT_PROVIDED, WrapperFlag
from api_client_core.cli.wrappers import (
    _APPLIERS,
    _WRAPPERS_GROUP_TITLE,
    add_wrapper_arguments,
    any_wrapper_given,
    apply_wrappers,
    expected_statuses,
)

from .conftest import find_group_title


def _build_parser() -> argparse.ArgumentParser:
    """Build a leaf parser with only the wrapper flags, mirroring what `builder.py` does."""
    parser = argparse.ArgumentParser()
    add_wrapper_arguments(parser)
    return parser


class TestAddWrapperArguments:
    """Tests for `add_wrapper_arguments()`"""

    def test_omitted_flags_default_to_not_provided(self) -> None:
        """Test that every wrapper flag left unset parses to NOT_PROVIDED, letting an omitted wrapper be
        distinguished from one explicitly given (see `add_wrapper_arguments()`'s own docstring)
        """
        args = _build_parser().parse_args([])
        assert args.with_retry is NOT_PROVIDED
        assert args.with_rate_limit is NOT_PROVIDED
        assert args.with_lock is NOT_PROVIDED
        assert args.with_expected_status is NOT_PROVIDED
        assert args.with_max_response_time is NOT_PROVIDED
        assert args.with_stats is NOT_PROVIDED
        assert args.with_repeat is NOT_PROVIDED
        assert args.with_concurrency is NOT_PROVIDED

    def test_bare_with_retry_uses_an_empty_spec(self) -> None:
        """Test that a bare --with-retry (no value) parses to an empty spec dict, so with_retry()'s
        own defaults apply
        """
        args = _build_parser().parse_args(["--with-retry"])
        assert args.with_retry == {}

    def test_with_retry_spec_parses_each_key_to_its_typed_value(self) -> None:
        """Test that --with-retry SPEC parses condition/num_retries/retry_after/safe_methods_only"""
        args = _build_parser().parse_args(
            ["--with-retry", "condition=429,num_retries=3,retry_after=2.5,safe_methods_only=true"]
        )
        assert args.with_retry == {"condition": 429, "num_retries": 3, "retry_after": 2.5, "safe_methods_only": True}

    def test_with_retry_single_condition_stays_a_scalar(self) -> None:
        """Test that a single `condition=` occurrence resolves to a bare int, not a one-item list"""
        args = _build_parser().parse_args(["--with-retry", "condition=429"])
        assert args.with_retry == {"condition": 429}

    def test_with_retry_repeated_condition_accumulates_into_a_list(self) -> None:
        """Test that repeating `condition=` within one SPEC accumulates every value into a list, in the
        order given, matching `with_retry()`'s own `condition: int | ... | Sequence[int | ...]` shape
        """
        args = _build_parser().parse_args(["--with-retry", "condition=429,condition=503,num_retries=2"])
        assert args.with_retry == {"condition": [429, 503], "num_retries": 2}

    def test_with_retry_bare_value_is_shorthand_for_num_retries(self) -> None:
        """Test that --with-retry N (a bare, unkeyed value) is shorthand for num_retries=N, mirroring the
        same bare-value shorthand --with-rate-limit/--with-repeat/--with-concurrency already offer for
        their own most common single option
        """
        args = _build_parser().parse_args(["--with-retry", "3"])
        assert args.with_retry == {"num_retries": 3}

    def test_with_retry_bare_boolean_key_is_true(self) -> None:
        """Test that a bare boolean spec key (no =value) is treated as key=True"""
        args = _build_parser().parse_args(["--with-retry", "safe_methods_only"])
        assert args.with_retry == {"safe_methods_only": True}

    def test_bare_non_boolean_key_requires_a_value(self) -> None:
        """Test that a bare mention of a non-boolean spec key exits cleanly (exit code 2), since only
        boolean keys may be given without a value
        """
        with pytest.raises(SystemExit) as exc_info:
            _build_parser().parse_args(["--with-retry", "num_retries"])
        assert exc_info.value.code == 2

    def test_unknown_spec_key_exits_cleanly(self) -> None:
        """Test that an unrecognized spec key raises SystemExit via argparse (exit code 2), not an
        uncaught error once parsing has already completed
        """
        with pytest.raises(SystemExit) as exc_info:
            _build_parser().parse_args(["--with-retry", "bogus=1"])
        assert exc_info.value.code == 2

    def test_unknown_spec_key_error_names_the_flag_and_valid_options(self, capsys: CaptureFixture[str]) -> None:
        """Test that the unknown-key error names the offending flag and key, and lists valid options.

        The flag name must appear exactly once in the error line itself (the usage summary above it also
        lists every flag, including this one, which is normal and unrelated): `argparse` already prepends
        `argument --with-retry: ` to a `type=` converter's own error message (see `ArgumentError.__str__`),
        so a converter that also includes the flag name in its own message would show it twice there
        """
        with pytest.raises(SystemExit):
            _build_parser().parse_args(["--with-retry", "bogus=1"])
        error_line = capsys.readouterr().err.splitlines()[-1]
        assert error_line.count("--with-retry") == 1
        assert "bogus" in error_line

    def test_non_numeric_value_for_an_int_key_exits_cleanly(self) -> None:
        """Test that a non-numeric value for an int-typed spec key exits cleanly (exit code 2)"""
        with pytest.raises(SystemExit) as exc_info:
            _build_parser().parse_args(["--with-retry", "num_retries=abc"])
        assert exc_info.value.code == 2

    def test_non_numeric_value_for_an_int_key_uses_the_an_article(self, capsys: CaptureFixture[str]) -> None:
        """Test that an int-typed spec key's error reads "must be an int", not the grammatically wrong
        "must be a int"
        """
        with pytest.raises(SystemExit):
            _build_parser().parse_args(["--with-retry", "num_retries=abc"])
        assert "must be an int" in capsys.readouterr().err

    def test_with_rate_limit_requires_a_value(self) -> None:
        """Test that --with-rate-limit with no value exits cleanly, since (unlike --with-retry) it
        has no bare-flag default
        """
        with pytest.raises(SystemExit) as exc_info:
            _build_parser().parse_args(["--with-rate-limit"])
        assert exc_info.value.code == 2

    def test_with_rate_limit_bare_value_maps_to_the_primary_key(self) -> None:
        """Test that a bare (unkeyed) --with-rate-limit value is taken as max_requests"""
        args = _build_parser().parse_args(["--with-rate-limit", "5"])
        assert args.with_rate_limit == {"max_requests": 5}

    def test_with_rate_limit_spec_parses_both_keys(self) -> None:
        """Test that --with-rate-limit max_requests=N,interval=SECONDS parses both keys"""
        args = _build_parser().parse_args(["--with-rate-limit", "max_requests=5,interval=2.5"])
        assert args.with_rate_limit == {"max_requests": 5, "interval": 2.5}

    def test_with_lock_bare_flag_defaults_to_none(self) -> None:
        """Test that a bare --with-lock (no name) parses to None, so with_lock()'s own auto-generated
        name applies
        """
        args = _build_parser().parse_args(["--with-lock"])
        assert args.with_lock is None

    def test_with_lock_accepts_a_name(self) -> None:
        """Test that --with-lock NAME parses to the given lock name"""
        args = _build_parser().parse_args(["--with-lock", "my-lock"])
        assert args.with_lock == "my-lock"

    def test_with_expected_status_accepts_multiple_codes(self) -> None:
        """Test that --with-expected-status accepts one or more status codes"""
        args = _build_parser().parse_args(["--with-expected-status", "200", "201"])
        assert args.with_expected_status == [200, 201]

    def test_with_max_response_time_parses_a_float(self) -> None:
        """Test that --with-max-response-time parses its value as a float"""
        args = _build_parser().parse_args(["--with-max-response-time", "500"])
        assert args.with_max_response_time == 500.0

    def test_with_stats_is_a_plain_boolean_flag(self) -> None:
        """Test that --with-stats takes no value"""
        args = _build_parser().parse_args(["--with-stats"])
        assert args.with_stats is True

    def test_with_repeat_bare_value_maps_to_the_primary_key(self) -> None:
        """Test that a bare (unkeyed) --with-repeat value is taken as num"""
        args = _build_parser().parse_args(["--with-repeat", "5"])
        assert args.with_repeat == {"num": 5}

    def test_with_repeat_primary_value_can_combine_with_a_keyed_option(self) -> None:
        """Test that a bare primary value and a keyed option can be combined in one spec"""
        args = _build_parser().parse_args(["--with-repeat", "5,return_exceptions=true"])
        assert args.with_repeat == {"num": 5, "return_exceptions": True}

    def test_with_concurrency_spec_parses_all_keys(self) -> None:
        """Test that --with-concurrency parses num/max_connections/return_exceptions together"""
        args = _build_parser().parse_args(["--with-concurrency", "num=5,max_connections=2,return_exceptions=true"])
        assert args.with_concurrency == {"num": 5, "max_connections": 2, "return_exceptions": True}

    def test_with_repeat_and_with_concurrency_are_mutually_exclusive(self) -> None:
        """Test that giving both --with-repeat and --with-concurrency exits cleanly (exit code 2),
        since EndpointFunc allows only one terminal wrapper per chain
        """
        with pytest.raises(SystemExit) as exc_info:
            _build_parser().parse_args(["--with-repeat", "3", "--with-concurrency", "3"])
        assert exc_info.value.code == 2

    def test_flags_are_added_to_the_call_wrappers_group(self) -> None:
        """Test that every with_xxx() flag lands in its own `call wrappers` group, separate
        from the endpoint's own parameters and the CLI's call-control flags
        """
        parser = _build_parser()
        assert find_group_title(parser, WrapperFlag.RETRY) == _WRAPPERS_GROUP_TITLE

    def test_terminal_mutex_group_is_nested_inside_the_wrappers_group(self) -> None:
        """Test that the --with-repeat/--with-concurrency mutually exclusive group is nested inside
        the `call wrappers` group rather than the parser's own default group
        """
        parser = _build_parser()
        assert find_group_title(parser, WrapperFlag.REPEAT) == _WRAPPERS_GROUP_TITLE
        assert find_group_title(parser, WrapperFlag.CONCURRENCY) == _WRAPPERS_GROUP_TITLE


class TestSpecMinimums:
    """Tests for `_spec_parser()`'s `minimums` bound checking"""

    @pytest.mark.parametrize(
        ("argv", "key", "bad_value"),
        [
            pytest.param(["--with-repeat", "0"], "num", "0", id="with_repeat_zero"),
            pytest.param(["--with-repeat", "-1"], "num", "-1", id="with_repeat_negative"),
            pytest.param(["--with-concurrency", "0"], "num", "0", id="with_concurrency_zero"),
            pytest.param(
                ["--with-concurrency", "num=2,max_connections=0"],
                "max_connections",
                "0",
                id="with_concurrency_max_connections_zero",
            ),
            pytest.param(["--with-rate-limit", "0"], "max_requests", "0", id="with_rate_limit_zero"),
            pytest.param(["--with-retry", "num_retries=-1"], "num_retries", "-1", id="with_retry_num_retries_negative"),
            pytest.param(
                ["--with-retry", "retry_after=-1"], "retry_after", "-1.0", id="with_retry_retry_after_negative"
            ),
        ],
    )
    def test_below_minimum_exits_cleanly(
        self, argv: list[str], key: str, bad_value: str, capsys: CaptureFixture[str]
    ) -> None:
        """Test that a spec value below its own key's minimum exits cleanly (exit code 2) rather than being
        silently accepted (e.g. `--with-repeat 0`, which would otherwise run the call zero times and still
        exit 0) or reaching an unrelated failure deeper in the call stack (e.g. `--with-concurrency 0`)
        """
        with pytest.raises(SystemExit) as exc_info:
            _build_parser().parse_args(argv)
        assert exc_info.value.code == 2
        error_line = capsys.readouterr().err.splitlines()[-1]
        assert key in error_line
        assert bad_value in error_line

    def test_at_minimum_is_accepted(self) -> None:
        """Test that a spec value exactly at its own key's minimum is accepted, not rejected off-by-one"""
        args = _build_parser().parse_args(["--with-repeat", "1"])
        assert args.with_repeat == {"num": 1}

    def test_max_response_time_zero_is_not_bound_checked(self) -> None:
        """Test that --with-max-response-time (not spec-parsed, so it has no `minimums` of its own) still
        accepts 0: it isn't part of the same defect class, since with_max_response_time() itself already
        raises a clear, correctly worded failure for a threshold no response time can ever meet
        """
        args = _build_parser().parse_args(["--with-max-response-time", "0"])
        assert args.with_max_response_time == 0.0


class TestAnyWrapperGiven:
    """Tests for `any_wrapper_given()`"""

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            pytest.param([], False, id="nothing_given"),
            pytest.param(["--with-retry"], True, id="a_not_provided_defaulted_flag_is_given"),
            pytest.param(["--with-stats"], True, id="only_with_stats_is_given"),
        ],
    )
    def test_any_wrapper_given(self, argv: list[str], expected: bool) -> None:
        """Test that any_wrapper_given() returns False when no wrapper flag was given, and True once a
        NOT_PROVIDED-defaulted flag is given, including --with-stats alone, whose own default is False
        rather than NOT_PROVIDED
        """
        args = _build_parser().parse_args(argv)
        assert any_wrapper_given(args) is expected


class TestExpectedStatuses:
    """Tests for `expected_statuses()`"""

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            pytest.param([], (), id="not_given"),
            pytest.param(["--with-expected-status", "200", "201"], (200, 201), id="a_single_occurrence"),
            pytest.param(
                ["--with-expected-status", "404", "--with-lock", "--with-expected-status", "500"],
                (404, 500),
                id="multiple_occurrences_are_combined",
            ),
        ],
    )
    def test_expected_statuses(self, argv: list[str], expected: tuple[int, ...]) -> None:
        """Test that expected_statuses() returns an empty tuple when --with-expected-status wasn't given,
        every code from a single occurrence, and codes combined across every occurrence rather than just
        the last one.

        Regression test (the last case): `namespace.with_expected_status` itself only ever holds the last
        occurrence's own codes (`_OrderedWrapperAction` overwrites it on each call), which `run()` used to
        read directly. A second `--with-expected-status` occurrence therefore silently dropped the first's
        codes from the CLI's own exit-code check, even though each chained `with_expected_status()` call
        still asserted its own codes against the response as it was made
        """
        args = _build_parser().parse_args(argv)
        assert expected_statuses(args) == expected


class TestApplyWrappers:
    """Tests for `apply_wrappers()`"""

    def test_returns_the_same_endpoint_func_when_nothing_was_given(self) -> None:
        """Test that apply_wrappers() returns the endpoint func unchanged when no wrapper was given"""
        args = _build_parser().parse_args([])
        ef = object()
        assert apply_wrappers(ef, args) is ef

    @pytest.mark.parametrize(
        ("argv", "wrapper_name", "expected_args", "expected_kwargs"),
        [
            pytest.param(
                ["--with-retry", "num_retries=3,condition=429"],
                "with_retry",
                (),
                {"num_retries": 3, "condition": 429},
                id="with_retry_spec_as_kwargs",
            ),
            pytest.param(
                ["--with-retry", "condition=429,condition=503"],
                "with_retry",
                (),
                {"condition": [429, 503]},
                id="with_retry_forwards_repeated_condition_as_a_list",
            ),
            pytest.param(
                ["--with-expected-status", "200", "201"],
                "with_expected_status",
                (200, 201),
                {},
                id="with_expected_status_codes_as_positional_args",
            ),
            pytest.param(["--with-lock"], "with_lock", (None,), {}, id="with_lock_bare_flag_is_none"),
        ],
    )
    def test_a_chainable_wrapper_is_called_with_its_parsed_spec(
        self,
        argv: list[str],
        wrapper_name: str,
        expected_args: tuple[Any, ...],
        expected_kwargs: dict[str, Any],
        mocker: MockerFixture,
    ) -> None:
        """Test that apply_wrappers() calls each chainable wrapper with its own parsed spec: keyword
        arguments for with_retry() (forwarding a repeated `condition=` spec key as a list, matching its own
        `Sequence[int | ...]` shape), positional status codes for with_expected_status() (matching its own
        variadic signature), and an explicit None for a bare --with-lock (parsed to None)
        """
        args = _build_parser().parse_args(argv)
        ef = mocker.MagicMock()
        apply_wrappers(ef, args)
        getattr(ef, wrapper_name).assert_called_once_with(*expected_args, **expected_kwargs)

    def test_with_stats_is_skipped_when_not_given(self, mocker: MockerFixture) -> None:
        """Test that with_stats() is not called when --with-stats was not given"""
        args = _build_parser().parse_args(["--with-retry"])
        ef = mocker.MagicMock()
        apply_wrappers(ef, args)
        ef.with_stats.assert_not_called()

    def test_chainable_wrappers_are_applied_in_the_order_the_flags_were_given(self, mocker: MockerFixture) -> None:
        """Test that chainable wrappers are applied in command-line order, mirroring
        `.with_x().with_y()` chaining, rather than a fixed order
        """
        args = _build_parser().parse_args(
            [
                "--with-stats",
                "--with-lock",
                "my-lock",
                "--with-retry",
                "--with-expected-status",
                "200",
                "--with-rate-limit",
                "5",
                "--with-max-response-time",
                "500",
            ]
        )
        ef = mocker.MagicMock()
        apply_wrappers(ef, args)
        call_order = [call[0].rsplit(".", 1)[-1] for call in ef.mock_calls]
        assert call_order == [
            "with_stats",
            "with_lock",
            "with_retry",
            "with_expected_status",
            "with_rate_limit",
            "with_max_response_time",
        ]

    def test_reversing_the_flag_order_reverses_the_call_order(self, mocker: MockerFixture) -> None:
        """Test that giving the same flags in the opposite order produces the opposite call order,
        confirming application order isn't incidentally fixed
        """
        args = _build_parser().parse_args(["--with-retry", "--with-lock"])
        ef = mocker.MagicMock()
        apply_wrappers(ef, args)
        call_order = [call[0].rsplit(".", 1)[-1] for call in ef.mock_calls]
        assert call_order == ["with_retry", "with_lock"]

        args = _build_parser().parse_args(["--with-lock", "--with-retry"])
        ef = mocker.MagicMock()
        apply_wrappers(ef, args)
        call_order = [call[0].rsplit(".", 1)[-1] for call in ef.mock_calls]
        assert call_order == ["with_lock", "with_retry"]

    def test_a_repeated_flag_is_applied_once_per_occurrence_in_order(self, mocker: MockerFixture) -> None:
        """Test that giving the same wrapper flag more than once chains one call per occurrence,
        matching `.with_x().with_x()` in Python, rather than only the last one taking effect
        """
        args = _build_parser().parse_args(
            ["--with-retry", "num_retries=2", "--with-lock", "--with-retry", "num_retries=5"]
        )
        ef = mocker.MagicMock()
        apply_wrappers(ef, args)
        call_order = [call[0].rsplit(".", 1)[-1] for call in ef.mock_calls]
        assert call_order == ["with_retry", "with_lock", "with_retry"]
        assert ef.mock_calls[0].kwargs == {"num_retries": 2}
        assert ef.mock_calls[2].kwargs == {"num_retries": 5}

    @pytest.mark.parametrize(
        ("argv", "terminal_wrapper", "expected_kwargs"),
        [
            pytest.param(["--with-stats", "--with-repeat", "3"], "with_repeat", {"num": 3}, id="with_repeat"),
            pytest.param(
                ["--with-stats", "--with-concurrency", "num=5,max_connections=2"],
                "with_concurrency",
                {"num": 5, "max_connections": 2},
                id="with_concurrency",
            ),
        ],
    )
    def test_a_terminal_wrapper_given_last_is_applied_last_and_its_result_is_returned(
        self, argv: list[str], terminal_wrapper: str, expected_kwargs: dict[str, Any], mocker: MockerFixture
    ) -> None:
        """Test that a terminal wrapper (with_repeat/with_concurrency) given after another wrapper flag is
        applied last, and apply_wrappers() returns its result directly since it's terminal
        """
        args = _build_parser().parse_args(argv)
        ef = mocker.MagicMock()
        result = apply_wrappers(ef, args)
        call_order = [call[0].rsplit(".", 1)[-1] for call in ef.mock_calls]
        assert call_order == ["with_stats", terminal_wrapper]
        assert ef.mock_calls[1].kwargs == expected_kwargs
        assert result is getattr(ef.with_stats.return_value, terminal_wrapper).return_value

    def test_a_terminal_flag_given_before_another_wrapper_flag_raises(self, mocker: MockerFixture) -> None:
        """Test that giving a terminal flag (--with-repeat/--with-concurrency) anywhere but last
        propagates the same RuntimeError the equivalent Python chain would raise, since
        apply_wrappers() folds wrappers in the order given rather than hoisting terminal ones
        """
        args = _build_parser().parse_args(["--with-repeat", "3", "--with-retry"])
        ef = mocker.MagicMock()
        ef.with_repeat.return_value._terminal_wrapper = "with_repeat"
        ef.with_repeat.return_value.with_retry.side_effect = RuntimeError(
            "`with_repeat()` is terminal and must always be the last wrapper in a chain."
        )
        with pytest.raises(RuntimeError, match="with_repeat"):
            apply_wrappers(ef, args)


class TestAppliersCoverage:
    """Tests that `_APPLIERS`, the wrapper-dest-to-applier-function table `apply_wrappers()` folds over,
    stays in sync with `WrapperFlag`, since it's keyed by each member's own derived `dest` rather than a
    hand-copied literal
    """

    def test_every_wrapper_flag_has_an_applier(self) -> None:
        """Test that every `WrapperFlag` member's own `dest` has a matching entry in `_APPLIERS`, so a
        member added to the enum without a matching applier would be caught here rather than failing only
        once that specific flag is actually given on a real command line
        """
        assert set(_APPLIERS) == {flag.dest for flag in WrapperFlag}

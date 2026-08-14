"""Unit tests for `api_client_core.cli._stdout`"""

from __future__ import annotations

import sys

import pytest

from api_client_core.cli._stdout import cli_output, cli_stdout, reserve_stdout


def _raise_value_error() -> None:
    raise ValueError("simulated failure")


class TestReserveStdout:
    """Tests for `reserve_stdout()`"""

    def test_points_stdout_at_stderr_inside_the_block(self) -> None:
        """Test that `sys.stdout` is `sys.stderr` for the duration of the block"""
        real_stdout = sys.stdout
        with reserve_stdout():
            assert sys.stdout is sys.stderr
            assert sys.stdout is not real_stdout

    def test_restores_the_real_stream_on_exit(self) -> None:
        """Test that `sys.stdout` is restored to the object it held before the block, not merely to
        whatever `sys.stdout` happens to be afterward
        """
        real_stdout = sys.stdout
        with reserve_stdout():
            pass
        assert sys.stdout is real_stdout

    def test_restores_the_real_stream_on_exception(self) -> None:
        """Test that a raised exception still restores the real stream, rather than leaving `sys.stdout`
        pointed at `sys.stderr` for the rest of the process
        """
        real_stdout = sys.stdout
        with reserve_stdout(), pytest.raises(ValueError):
            _raise_value_error()
        assert sys.stdout is real_stdout

    def test_nested_call_is_a_no_op(self) -> None:
        """Test that a nested call (e.g. `_complete()` reaching code that also opens a reservation) is a
        no-op: the outer reservation keeps owning the real stream rather than the inner call re-reserving
        the already-redirected `sys.stdout` as if it were real
        """
        real_stdout = sys.stdout
        with reserve_stdout():
            guarded = sys.stdout
            with reserve_stdout():
                assert sys.stdout is guarded
                assert cli_stdout() is real_stdout
            assert sys.stdout is guarded
            assert cli_stdout() is real_stdout
        assert sys.stdout is real_stdout

    def test_usable_as_a_decorator(self) -> None:
        """Test that `reserve_stdout()` also works as a decorator, since `contextmanager` re-creates the
        context manager on every call
        """
        real_stdout = sys.stdout
        seen: list[bool] = []

        @reserve_stdout()
        def f() -> None:
            seen.append(sys.stdout is sys.stderr)

        f()
        f()

        assert seen == [True, True]
        assert sys.stdout is real_stdout


class TestCliStdout:
    """Tests for `cli_stdout()`"""

    def test_returns_the_real_stdout_when_not_reserved(self) -> None:
        """Test that `cli_stdout()` returns plain `sys.stdout` outside a `reserve_stdout()` block, so
        `run()`/`dispatch()` called directly (as most tests do) write to `sys.stdout` as always
        """
        assert cli_stdout() is sys.stdout

    def test_returns_the_held_stream_while_reserved(self) -> None:
        """Test that `cli_stdout()` returns the real stream a `reserve_stdout()` block is holding aside,
        not the redirected `sys.stdout`
        """
        real_stdout = sys.stdout
        with reserve_stdout():
            assert cli_stdout() is real_stdout
            assert cli_stdout() is not sys.stdout


class TestCliOutput:
    """Tests for `cli_output()`"""

    def test_points_stdout_back_at_the_reserved_stream(self) -> None:
        """Test that `cli_output()` restores the real stdout for the duration of the block while a
        reservation is active
        """
        real_stdout = sys.stdout
        with reserve_stdout():
            assert sys.stdout is not real_stdout
            with cli_output():
                assert sys.stdout is real_stdout
            assert sys.stdout is not real_stdout

    def test_is_a_no_op_when_not_reserved(self) -> None:
        """Test that `cli_output()` doesn't change `sys.stdout` outside a `reserve_stdout()` block, since
        `cli_stdout()` is already `sys.stdout` in that case
        """
        real_stdout = sys.stdout
        with cli_output():
            assert sys.stdout is real_stdout
        assert sys.stdout is real_stdout

    def test_restores_the_prior_stream_on_exception(self) -> None:
        """Test that a raised exception still restores whatever `sys.stdout` held before the block"""
        with reserve_stdout():
            guarded = sys.stdout
            with pytest.raises(ValueError), cli_output():
                _raise_value_error()
            assert sys.stdout is guarded

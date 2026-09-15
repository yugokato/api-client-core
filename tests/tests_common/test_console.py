"""Unit tests for `api_client_core._common.console`"""

from __future__ import annotations

import sys

import pytest
from common_libs.ansi_colors import remove_color_code
from pytest import CaptureFixture

from api_client_core._common.console import output_to, real_stdout, reserve_stdout, write_error


def _raise_value_error() -> None:
    raise ValueError("simulated failure")


class TestReserveStdout:
    """Tests for `reserve_stdout()`"""

    def test_points_stdout_at_stderr_inside_the_block(self) -> None:
        """Test that `sys.stdout` is `sys.stderr` for the duration of the block"""
        saved_stdout = sys.stdout
        with reserve_stdout():
            assert sys.stdout is sys.stderr
            assert sys.stdout is not saved_stdout

    def test_restores_the_real_stream_on_exit(self) -> None:
        """Test that `sys.stdout` is restored to the object it held before the block, not merely to
        whatever `sys.stdout` happens to be afterward
        """
        saved_stdout = sys.stdout
        with reserve_stdout():
            pass
        assert sys.stdout is saved_stdout

    def test_restores_the_real_stream_on_exception(self) -> None:
        """Test that a raised exception still restores the real stream, rather than leaving `sys.stdout`
        pointed at `sys.stderr` for the rest of the process
        """
        saved_stdout = sys.stdout
        with reserve_stdout(), pytest.raises(ValueError):
            _raise_value_error()
        assert sys.stdout is saved_stdout

    def test_nested_call_is_a_no_op(self) -> None:
        """Test that a nested call (e.g. `_complete()` reaching code that also opens a reservation) is a
        no-op: the outer reservation keeps owning the real stream rather than the inner call re-reserving
        the already-redirected `sys.stdout` as if it were real
        """
        saved_stdout = sys.stdout
        with reserve_stdout():
            guarded = sys.stdout
            with reserve_stdout():
                assert sys.stdout is guarded
                assert real_stdout() is saved_stdout
            assert sys.stdout is guarded
            assert real_stdout() is saved_stdout
        assert sys.stdout is saved_stdout


class TestRealStdout:
    """Tests for `real_stdout()`"""

    def test_returns_the_real_stdout_when_not_reserved(self) -> None:
        """Test that `real_stdout()` returns plain `sys.stdout` outside a `reserve_stdout()` block, so
        `run()`/`dispatch()` called directly (as most tests do) write to `sys.stdout` as always
        """
        assert real_stdout() is sys.stdout

    def test_returns_the_held_stream_while_reserved(self) -> None:
        """Test that `real_stdout()` returns the real stream a `reserve_stdout()` block is holding aside,
        not the redirected `sys.stdout`
        """
        saved_stdout = sys.stdout
        with reserve_stdout():
            assert real_stdout() is saved_stdout
            assert real_stdout() is not sys.stdout


class TestOutputTo:
    """Tests for `output_to()`"""

    def test_points_stdout_back_at_the_reserved_stream(self) -> None:
        """Test that `output_to()` restores the real stdout for the duration of the block while a
        reservation is active
        """
        saved_stdout = sys.stdout
        with reserve_stdout():
            assert sys.stdout is not saved_stdout
            with output_to():
                assert sys.stdout is saved_stdout
            assert sys.stdout is not saved_stdout

    def test_is_a_no_op_when_not_reserved(self) -> None:
        """Test that `output_to()` doesn't change `sys.stdout` outside a `reserve_stdout()` block, since
        `real_stdout()` is already `sys.stdout` in that case
        """
        saved_stdout = sys.stdout
        with output_to():
            assert sys.stdout is saved_stdout
        assert sys.stdout is saved_stdout

    def test_restores_the_prior_stream_on_exception(self) -> None:
        """Test that a raised exception still restores whatever `sys.stdout` held before the block"""
        with reserve_stdout():
            guarded = sys.stdout
            with pytest.raises(ValueError), output_to():
                _raise_value_error()
            assert sys.stdout is guarded


class TestWriteError:
    """Tests for `write_error()`"""

    def test_writes_a_red_error_line_to_stderr(self, capsys: CaptureFixture[str]) -> None:
        """Test that a plain string message is written to stderr as a red `error: <message>` line"""
        write_error("something went wrong")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert remove_color_code(captured.err) == "error: something went wrong\n"

    @pytest.mark.parametrize(
        ("exception", "expected_message"),
        [
            pytest.param(
                ValueError("bad value"), "error: ValueError: bad value\n", id="unrecognized_type_keeps_its_name"
            ),
            pytest.param(
                LookupError("No API client found for app name 'x'"),
                "error: No API client found for app name 'x'\n",
                id="lookup_error_omits_its_type_name",
            ),
            pytest.param(
                RuntimeError("No usable commands discovered on FooClient"),
                "error: No usable commands discovered on FooClient\n",
                id="runtime_error_omits_its_type_name",
            ),
            pytest.param(
                KeyError("missing"),
                "error: 'missing'\n",
                id="lookup_error_subclass_also_omits_its_type_name",
            ),
            pytest.param(ValueError(), "error: ValueError: \n", id="unrecognized_type_with_no_message"),
        ],
    )
    def test_formats_an_exception_by_type(
        self, exception: BaseException, expected_message: str, capsys: CaptureFixture[str]
    ) -> None:
        """Test that an exception is reported as `Type: message` rather than its bare str(), so an
        exception whose own str() carries no useful information still names its type, except for a
        `LookupError`/`RuntimeError` (e.g. `find_client()`'s own "No API client found..." or
        `build_client_parser()`'s own "No usable commands discovered...", and a `LookupError`
        subclass like `KeyError`), which is reported as its bare message since that message already
        reads as a complete sentence and the class name adds nothing but noise
        """
        write_error(exception)
        assert remove_color_code(capsys.readouterr().err) == expected_message

"""Unit tests for `api_client_core.mcp._entrypoint`"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import textwrap
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from pytest import CaptureFixture
from pytest_mock import MockerFixture

from api_client_core._common import discovery
from api_client_core.mcp._constants import DEFAULT_MAX_FILE_BYTES, DEFAULT_THRESHOLD, Mode, ResponseFormat
from api_client_core.mcp._entrypoint import _build_parser, _options_from_args, _parse_header, main

from .conftest import patch_mcp_installed


class TestParseHeader:
    """Tests for `_parse_header()`'s NAME:VALUE splitting"""

    def test_splits_name_and_value(self) -> None:
        """Test that a well-formed header splits into a (name, value) tuple"""
        assert _parse_header("X-Custom: value") == ("X-Custom", "value")

    def test_strips_surrounding_whitespace(self) -> None:
        """Test that leading/trailing whitespace around both name and value is stripped"""
        assert _parse_header("  X-Custom  :  value  ") == ("X-Custom", "value")

    def test_missing_colon_raises(self) -> None:
        """Test that a header with no colon raises ArgumentTypeError, so argparse reports it as a
        usage error rather than a confusing downstream failure
        """
        with pytest.raises(argparse.ArgumentTypeError):
            _parse_header("not-a-header")


class TestBuildParser:
    """Tests for `_build_parser()`'s flag surface"""

    def test_app_name_is_optional_at_parse_time(self) -> None:
        """Test that app_name is optional at the argparse level - _run() reports its own actionable
        error when it's missing, rather than argparse's generic one
        """
        args = _build_parser().parse_args([])
        assert args.app_name is None

    def test_app_name_is_parsed_as_the_lone_positional(self) -> None:
        """Test that a bare app name parses as the positional argument"""
        args = _build_parser().parse_args(["my-app"])
        assert args.app_name == "my-app"

    def test_repeatable_flags_accumulate(self) -> None:
        """Test that --resource/--include/--exclude/--method/-H each accumulate across repetitions"""
        args = _build_parser().parse_args(
            [
                "my-app",
                "--resource",
                "products",
                "--resource",
                "users",
                "--include",
                "a*",
                "--exclude",
                "b*",
                "--method",
                "get",
                "-H",
                "X-A: 1",
                "-H",
                "X-B: 2",
            ]
        )
        assert args.resource == ["products", "users"]
        assert args.include == ["a*"]
        assert args.exclude == ["b*"]
        assert args.method == ["get"]
        assert args.header == [("X-A", "1"), ("X-B", "2")]

    def test_mode_defaults_to_auto(self) -> None:
        """Test that --mode defaults to auto when not given"""
        args = _build_parser().parse_args(["my-app"])
        assert args.mode == Mode.AUTO.value

    def test_response_format_defaults_to_full(self) -> None:
        """Test that --response-format defaults to full when not given"""
        args = _build_parser().parse_args(["my-app"])
        assert args.response_format == ResponseFormat.FULL.value

    def test_read_only_and_boolean_flags_default_false(self) -> None:
        """Test that every boolean flag defaults to False when not given"""
        args = _build_parser().parse_args(["my-app"])
        assert args.read_only is False
        assert args.allow_file_paths is False
        assert args.all_headers is False
        assert args.log_requests is False

    def test_invalid_mode_choice_is_rejected(self, capsys: CaptureFixture[str]) -> None:
        """Test that an invalid --mode value is rejected by argparse itself"""
        with pytest.raises(SystemExit):
            _build_parser().parse_args(["my-app", "--mode", "bogus"])


class TestOptionsFromArgs:
    """Tests for `_options_from_args()`'s namespace -> ServerOptions translation"""

    def test_read_only_expands_correctly(self) -> None:
        """Test that --read-only is threaded through to ServerOptions.read_only"""
        args = _build_parser().parse_args(["my-app", "--read-only"])
        options = _options_from_args(args)
        assert options.read_only is True

    def test_all_headers_flag_is_threaded_through(self) -> None:
        """Test that --all-headers is threaded through to ServerOptions.all_headers"""
        args = _build_parser().parse_args(["my-app", "--all-headers"])
        options = _options_from_args(args)
        assert options.all_headers is True

    def test_default_all_headers_is_false(self) -> None:
        """Test that the header allowlist is applied by default when --all-headers isn't given"""
        args = _build_parser().parse_args(["my-app"])
        options = _options_from_args(args)
        assert options.all_headers is False

    def test_unset_threshold_and_max_file_bytes_fall_back_to_defaults(self) -> None:
        """Test that omitting --threshold/--max-file-bytes falls back to this module's defaults, not
        None
        """
        args = _build_parser().parse_args(["my-app"])
        options = _options_from_args(args)
        assert options.threshold == DEFAULT_THRESHOLD
        assert options.max_file_bytes == DEFAULT_MAX_FILE_BYTES

    def test_given_threshold_and_max_file_bytes_override_the_default(self) -> None:
        """Test that explicit --threshold/--max-file-bytes values are used as given"""
        args = _build_parser().parse_args(["my-app", "--threshold", "5", "--max-file-bytes", "100"])
        options = _options_from_args(args)
        assert options.threshold == 5
        assert options.max_file_bytes == 100


class TestMain:
    """Tests for `main()`'s argv handling and exit codes"""

    def test_help_exits_zero(self, capsys: CaptureFixture[str]) -> None:
        """Test that --help exits 0 without requiring any app name or discovered project"""
        assert main(["--help"]) == 0

    def test_version_exits_zero(self, capsys: CaptureFixture[str]) -> None:
        """Test that --version exits 0 and prints this package's version"""
        assert main(["--version"]) == 0
        assert "api-client-mcp" in capsys.readouterr().out

    def test_missing_app_name_exits_2(self, capsys: CaptureFixture[str]) -> None:
        """Test that omitting app_name is reported as a usage error, exit 2"""
        assert main([]) == 2
        assert "app_name" in capsys.readouterr().err

    def test_unknown_app_name_exits_2_naming_discovered_apps(self, capsys: CaptureFixture[str]) -> None:
        """Test that an app name find_client() can't resolve exits 2, surfacing its own message (which
        names every app name actually discovered)
        """
        assert main(["NoSuchApp"]) == 2
        assert "NoSuchApp" in capsys.readouterr().err

    def test_missing_mcp_extra_exits_2_with_an_install_hint(
        self, mocker: MockerFixture, capsys: CaptureFixture[str]
    ) -> None:
        """Test that a missing `mcp` extra is reported before any discovery is attempted, with an
        actionable install hint
        """
        patch_mcp_installed(mocker, installed=False)
        assert main(["my-app"]) == 2
        assert "'mcp' extra isn't installed" in capsys.readouterr().err

    def test_invalid_choice_exits_2(self, capsys: CaptureFixture[str]) -> None:
        """Test that argparse's usage error (an invalid --mode choice) exits 2, handled by main()'s
        SystemExit conversion rather than propagating
        """
        assert main(["my-app", "--mode", "bogus"]) == 2

    def test_keyboard_interrupt_exits_130(self, mocker: MockerFixture) -> None:
        """Test that a KeyboardInterrupt during a real run exits 130, the conventional SIGINT code"""
        mocker.patch("api_client_core.mcp._entrypoint._run", side_effect=KeyboardInterrupt)
        assert main(["my-app"]) == 130

    def test_broken_pipe_during_a_real_run_exits_141_and_redirects_stdout_to_devnull(
        self, mocker: MockerFixture
    ) -> None:
        """Test that a `BrokenPipeError` during a real run (e.g. `--help` writing into a reader that
        already exited) exits `128 + SIGPIPE` (141), the usual shell convention, rather than an
        uncaught traceback. `stdout`, not `stderr`, is redirected to `os.devnull`: without it, Python
        still re-reports the same failure while flushing stdout again at shutdown regardless of the
        returned exit code
        """
        mocker.patch("api_client_core.mcp._entrypoint._run", side_effect=BrokenPipeError)
        mock_dup2 = mocker.patch("api_client_core.mcp._entrypoint.os.dup2")
        mock_close = mocker.patch("api_client_core.mcp._entrypoint.os.close")
        devnull_fd = object()
        mock_open = mocker.patch("api_client_core.mcp._entrypoint.os.open", return_value=devnull_fd)

        rc = main(["my-app"])

        assert rc == 128 + getattr(signal, "SIGPIPE", 13)
        mock_open.assert_called_once_with(os.devnull, os.O_WRONLY)
        mock_dup2.assert_called_once_with(devnull_fd, sys.stdout.fileno())
        mock_close.assert_called_once_with(devnull_fd)

    def test_broken_pipe_falls_back_to_13_when_sigpipe_is_unavailable(self, mocker: MockerFixture) -> None:
        """Test that the exit code falls back to `128 + 13` (13 being `SIGPIPE`'s universal POSIX
        value) when `signal.SIGPIPE` doesn't exist, e.g. on Windows, rather than the handler itself
        raising `AttributeError`
        """
        mocker.patch("api_client_core.mcp._entrypoint._run", side_effect=BrokenPipeError)
        mocker.patch("api_client_core.mcp._entrypoint.os.dup2")
        mocker.patch("api_client_core.mcp._entrypoint.os.close")
        mocker.patch("api_client_core.mcp._entrypoint.os.open", return_value=object())
        mocker.patch("api_client_core.mcp._entrypoint.signal", spec=[])

        rc = main(["my-app"])

        assert rc == 128 + 13

    def test_dispatches_to_serve_for_a_real_app_name(self, mocker: MockerFixture) -> None:
        """Test that a resolvable app name reaches prepare()/serve_connection(), not an earlier exit.

        Needs the `mcp` extra: `prepare()`/`serve_connection()` live in `.server`, which imports the SDK
        at module scope, so patching them requires that module to be importable in the first place -
        unlike the rest of this file, which never touches it.
        """
        pytest.importorskip("mcp")
        mock_prepare = mocker.patch("api_client_core.mcp.server.prepare", return_value=(mocker.Mock(), mocker.Mock()))
        mock_serve_connection = mocker.patch("api_client_core.mcp.server.serve_connection", return_value=0)
        assert main(["DummyJSON"]) == 0
        mock_prepare.assert_called_once()
        mock_serve_connection.assert_called_once()

    def test_prepare_runtime_error_is_reported_as_a_usage_error(
        self, mocker: MockerFixture, capsys: CaptureFixture[str]
    ) -> None:
        """Test that a RuntimeError raised while preparing the server (e.g. a client that can't accept
        async_mode=True) is reported cleanly and exits 2, rather than a bare traceback. Needs the `mcp`
        extra, for the same reason as the test above.
        """
        pytest.importorskip("mcp")
        mocker.patch("api_client_core.mcp.server.prepare", side_effect=RuntimeError("client is broken"))
        assert main(["DummyJSON"]) == 2
        assert "client is broken" in capsys.readouterr().err

    def test_serve_connection_failure_propagates_instead_of_being_reported_as_a_usage_error(
        self, mocker: MockerFixture
    ) -> None:
        """Test that a failure once the connection is already live (from serve_connection(), after a
        successful prepare()) propagates as a genuine exception rather than being caught and reported
        the same way a startup failure is - only a prepare() failure is a usage error.
        """
        pytest.importorskip("mcp")
        mocker.patch("api_client_core.mcp.server.prepare", return_value=(mocker.Mock(), mocker.Mock()))
        mocker.patch("api_client_core.mcp.server.serve_connection", side_effect=RuntimeError("connection dropped"))
        with pytest.raises(RuntimeError, match="connection dropped"):
            main(["DummyJSON"])

    def test_log_level_is_wired_to_setup_logging_before_and_after_discovery(self, mocker: MockerFixture) -> None:
        """Test that --log-level actually reaches setup_logging(), applied both before discovery (so a
        diagnostic raised during discovery itself is visible) and again right after it, since discovery
        may import a downstream project that resets it (see the clobbering regression test below). Needs
        the `mcp` extra, for the same reason as the tests above.
        """
        pytest.importorskip("mcp")
        mock_setup_logging = mocker.patch("api_client_core.logging.setup_logging")
        mocker.patch("api_client_core.mcp.server.prepare", return_value=(mocker.Mock(), mocker.Mock()))
        mocker.patch("api_client_core.mcp.server.serve_connection", return_value=0)
        assert main(["DummyJSON", "--log-level", "DEBUG"]) == 0
        assert mock_setup_logging.call_count == 2
        assert all(call.kwargs["level"] == "DEBUG" for call in mock_setup_logging.call_args_list)

    def test_unset_log_level_still_bootstraps_logging(self, mocker: MockerFixture) -> None:
        """Test that logging is bootstrapped unconditionally, even with no --log-level given, so a
        `logger.warning()`/`logger.debug()` call anywhere under mcp/ isn't silently dropped by the
        package's silent-by-default NullHandler.
        """
        pytest.importorskip("mcp")
        mock_setup_logging = mocker.patch("api_client_core.logging.setup_logging")
        mocker.patch("api_client_core.mcp.server.prepare", return_value=(mocker.Mock(), mocker.Mock()))
        mocker.patch("api_client_core.mcp.server.serve_connection", return_value=0)
        assert main(["DummyJSON"]) == 0
        assert mock_setup_logging.call_count == 2
        assert all(call.kwargs["level"] is None for call in mock_setup_logging.call_args_list)

    def test_log_level_survives_a_downstream_project_resetting_it_during_discovery(self, mocker: MockerFixture) -> None:
        """Test that a downstream project calling setup_logging() at import time during discovery
        doesn't leave --log-level permanently overridden: the real logger's effective level, not
        just the mocked call args, must reflect the operator's own choice by the time the server
        would start serving.
        """
        pytest.importorskip("mcp")
        import logging

        from api_client_core.logging import setup_logging as real_setup_logging

        def fake_find_client(app_name: str) -> Any:
            # Stands in for a downstream project's own import-time setup_logging() call, triggered by
            # discovery importing every top-level project module.
            real_setup_logging()
            return mocker.Mock()

        mocker.patch("api_client_core._common.discovery.find_client", side_effect=fake_find_client)
        mocker.patch("api_client_core.mcp.server.prepare", return_value=(mocker.Mock(), mocker.Mock()))
        mocker.patch("api_client_core.mcp.server.serve_connection", return_value=0)
        assert main(["DummyJSON", "--log-level", "DEBUG"]) == 0
        assert logging.getLogger("api_client_core").getEffectiveLevel() == logging.DEBUG

    def test_log_level_end_to_end_makes_a_real_mcp_warning_reach_stderr(
        self, mocker: MockerFixture, capsys: CaptureFixture[str]
    ) -> None:
        """Test end to end (no mocked setup_logging/logger) that --log-level actually surfaces an mcp/
        warning on stderr: catalog.py's forced-static-mode warning, triggered by --mode static on the
        73-endpoint DummyJSON client. Without this wiring the warning is silently dropped by the
        package's NullHandler, as it was before --log-level was ever applied.
        """
        pytest.importorskip("mcp")

        @asynccontextmanager
        async def fake_stdio_server() -> AsyncIterator[tuple[Any, Any]]:
            yield (mocker.AsyncMock(), mocker.AsyncMock())

        mocker.patch("api_client_core.mcp.server.stdio_server", fake_stdio_server)
        mocker.patch("api_client_core.mcp.server.Server.run", new=mocker.AsyncMock())

        assert main(["DummyJSON", "--mode", "static", "--log-level", "WARNING"]) == 0
        assert "forced with 73 endpoints" in capsys.readouterr().err


class TestMcpImportPrecedesDiscovery:
    """Tests that `_run()` imports `.server` (and therefore the real `mcp` package) before project
    discovery ever runs, so a project with its own top-level `mcp` package/module can't shadow the SDK
    for this or any later `import mcp` in the process.
    """

    def test_sdk_is_already_imported_by_the_time_discovery_runs(
        self, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture
    ) -> None:
        """Test that `sys.modules` already has the real `mcp` package bound by the time
        `ensure_project_on_sys_path()` runs. Forces a fresh `mcp` import at the exact moment `_run()`
        imports `.server`/`.tools` (which is what actually pulls in `mcp`), by clearing all three from
        `sys.modules` first - otherwise an earlier test's import would leave them cached, and the
        assertion would hold regardless of `_run()`'s statement order.
        """
        pytest.importorskip("mcp")
        monkeypatch.delitem(sys.modules, "mcp", raising=False)
        monkeypatch.delitem(sys.modules, "api_client_core.mcp.server", raising=False)
        monkeypatch.delitem(sys.modules, "api_client_core.mcp.tools", raising=False)

        real_ensure_project_on_sys_path = discovery.ensure_project_on_sys_path
        seen_mcp_already_imported: list[bool] = []

        def recording_ensure_project_on_sys_path() -> None:
            seen_mcp_already_imported.append("mcp" in sys.modules)
            real_ensure_project_on_sys_path()

        mocker.patch(
            "api_client_core._common.discovery.ensure_project_on_sys_path",
            side_effect=recording_ensure_project_on_sys_path,
        )
        mocker.patch("api_client_core.mcp.server.prepare", return_value=(mocker.Mock(), mocker.Mock()))
        mocker.patch("api_client_core.mcp.server.serve_connection", return_value=0)

        assert main(["DummyJSON"]) == 0
        assert seen_mcp_already_imported
        assert all(seen_mcp_already_imported)


class TestWorksWithoutTheMcpExtra:
    """Tests that catalog.py/schema.py/runner.py's claim - each imports no `mcp` SDK code, so they
    (and their tests) run without the optional `mcp` extra installed - actually holds.

    Must run in a subprocess with `import mcp` itself made to fail, simulating the extra being absent:
    CI now installs it unconditionally (`uv sync --all-extras`), and every other test in this repo runs
    with it genuinely present, so nothing else exercises this branch. A future import creeping into one
    of these three modules would otherwise go unnoticed until a user without the extra hit it.
    """

    def test_catalog_schema_and_runner_import_without_the_mcp_package(self) -> None:
        """Test that catalog.py, schema.py, and runner.py each import cleanly with `mcp` blocked"""
        script = textwrap.dedent("""
            import sys

            class _BlockMcp:
                def find_spec(self, name, path=None, target=None):
                    if name == "mcp" or name.startswith("mcp."):
                        raise ImportError(f"blocked: {name}")
                    return None

            sys.meta_path.insert(0, _BlockMcp())

            import api_client_core.mcp.catalog
            import api_client_core.mcp.schema
            import api_client_core.mcp.runner
            print("OK")
            """)
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout

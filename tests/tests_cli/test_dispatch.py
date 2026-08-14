"""Unit tests for `api_client_core.cli.dispatch`"""

from __future__ import annotations

from pathlib import Path

import pytest
from common_libs.ansi_colors import remove_color_code
from httpx2 import Client
from pytest import CaptureFixture
from pytest_mock import MockerFixture

from api_client_core import APIClient, __version__
from api_client_core.cli._constants import PROG
from api_client_core.cli._stdout import reserve_stdout
from api_client_core.cli.builder import _TAB_COMPLETION_TIP, build_initial_parser
from api_client_core.cli.discovery import DiscoveryResult
from api_client_core.cli.dispatch import dispatch
from examples.dummyjson.client import DummyJSONClient

from .conftest import CliTestClient, make_httpx_response, patch_argcomplete_installed


class TestStdoutDuringDiscovery:
    """Regression test for the reported bug: a log line emitted during discovery, including one from a
    downstream project's own logging setup at import time, reaching the real stdout ahead of `--help`
    text (or an `--output json` payload) rather than stderr.

    In a real run, `_entrypoint.py`'s `main()` opens the process-wide stdout reservation before `dispatch()`
    is ever reached. This test drives `dispatch()` directly, wrapped in an explicit `with reserve_stdout():`
    to reproduce that same condition.
    """

    def test_a_downstream_setup_logging_leak_during_discovery_lands_on_stderr(
        self,
        downstream_setup_logging_project: Path,
        capsys: CaptureFixture[str],
        _restore_logging_state: None,
    ) -> None:
        """Test that `-h`, which runs discovery via `build_initial_parser()`, keeps a downstream
        project's own import-time logging setup, and the log line it enables, off the real stdout
        """
        with reserve_stdout():
            exit_code = dispatch(["-h"])

        assert exit_code == 0
        out, err = capsys.readouterr()
        assert "Skipping NoAppNameClient" not in out
        assert "usage: api-client [-h]" in remove_color_code(out)
        assert "Skipping NoAppNameClient: no 'app_name' class attribute is set" in err


class TestDispatch:
    """Tests for `dispatch()`"""

    def test_dispatches_to_run_via_app_name(self, mocker: MockerFixture) -> None:
        """Test that dispatch() resolves the leading token as an app name and forwards the remaining argv to run()"""
        mock_find_client = mocker.patch("api_client_core.cli.dispatch.find_client", return_value=CliTestClient)
        mock_run = mocker.patch("api_client_core.cli.dispatch.run", return_value=0)

        exit_code = dispatch(["cli-test", "widgets", "get-widget", "--widget-id", "1"])

        assert exit_code == 0
        mock_find_client.assert_called_once_with("cli-test")
        assert mock_run.call_args.args == (CliTestClient, ["widgets", "get-widget", "--widget-id", "1"])

    def test_run_is_given_a_prog_reflecting_the_typed_app_name(self, mocker: MockerFixture) -> None:
        """Test that dispatch() passes run() a prog combining PROG with the app-name token as typed on
        the command line, so a resolved client's own --help usage text shows what the user actually
        typed rather than argparse's default inference from sys.argv[0]
        """
        mocker.patch("api_client_core.cli.dispatch.find_client", return_value=CliTestClient)
        mock_run = mocker.patch("api_client_core.cli.dispatch.run", return_value=0)

        dispatch(["Cli-Test", "widgets", "get-widget", "--widget-id", "1"])

        assert mock_run.call_args.kwargs["prog"] == f"{PROG} Cli-Test"

    def test_unknown_app_name_exits_cleanly_with_a_clean_error(self, capsys: CaptureFixture[str]) -> None:
        """Test that an app name with no discovered match is reported cleanly, not an uncaught LookupError"""
        exit_code = dispatch(["not-a-registered-app-name"])
        assert exit_code == 2
        assert "No API client found for app name" in capsys.readouterr().err

    def test_a_leading_flag_is_reported_as_a_usage_error_rather_than_an_unknown_app_name(
        self, capsys: CaptureFixture[str]
    ) -> None:
        """Test that a leading token that looks like a flag (but isn't `-h`/`--help`, `--base-url`, or
        `--log-level`) is reported as a dedicated usage error naming it, instead of a confusing
        "No API client found for app name '--some-flag'".

        Regression test: this used to reach `find_client("--some-flag")` directly, which fails exactly
        like any other unknown app name would
        """
        exit_code = dispatch(["--some-flag"])
        assert exit_code == 2
        err = capsys.readouterr().err
        plain_err = remove_color_code(err)
        assert "usage: api-client [-h]" in plain_err
        assert "unrecognized option: '--some-flag'" in plain_err
        assert "No API client found for app name" not in err

    def test_a_global_flag_before_the_app_name_still_resolves_it(
        self, mocker: MockerFixture, capsys: CaptureFixture[str]
    ) -> None:
        """Test that `--log-level`/`--base-url` given ahead of the app name resolve it correctly, instead of
        the app-name subparser mistaking the flag's own value for an invalid app name.

        Regression test: `--log-level DEBUG my-app ...` used to fail with "invalid choice: 'DEBUG'" since the
        leading-flag branch handed the whole argv to the initial parser, which knows nothing about
        `--log-level`/`--base-url`.
        """
        mock_client_class = mocker.MagicMock()
        mock_find_client = mocker.patch("api_client_core.cli.dispatch.find_client", return_value=mock_client_class)
        mock_run = mocker.patch("api_client_core.cli.dispatch.run", return_value=0)

        exit_code = dispatch(["--log-level", "DEBUG", "my-app", "resource", "command"])

        assert exit_code == 0
        mock_find_client.assert_called_once_with("my-app")
        mock_run.assert_called_once()
        assert mock_run.call_args.args == (mock_client_class, ["--log-level", "DEBUG", "resource", "command"])
        assert mock_run.call_args.kwargs["log_level"] == "DEBUG"

    def test_a_dangling_global_flag_with_no_app_name_shows_help(self, capsys: CaptureFixture[str]) -> None:
        """Test that `--base-url` given with no app name at all (its value consumed, nothing left) is
        treated the same as an empty argv, rather than blaming the URL for being an invalid app name."""
        exit_code = dispatch(["--base-url", "https://x"])
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "usage: api-client [-h]" in remove_color_code(err)
        assert "invalid choice" not in err

    def test_leading_version_flag_prints_the_version_without_running_discovery(
        self, mocker: MockerFixture, capsys: CaptureFixture[str]
    ) -> None:
        """Test that a leading --version prints the package version and exits 0 without ever calling
        find_client() or build_initial_parser() (and therefore without the full client-discovery import
        pass either of those triggers), unlike every other leading flag, which is only handed to the
        initial parser after _bootstrap()
        """
        mock_find_client = mocker.patch("api_client_core.cli.dispatch.find_client")
        mock_build_initial_parser = mocker.patch("api_client_core.cli.dispatch.build_initial_parser")

        exit_code = dispatch(["--version"])

        assert exit_code == 0
        assert capsys.readouterr().out.strip() == f"{PROG} {__version__}"
        mock_find_client.assert_not_called()
        mock_build_initial_parser.assert_not_called()

    def test_missing_target_exits_cleanly_with_usage_and_known_app_names(self, capsys: CaptureFixture[str]) -> None:
        """Test that omitting the target prints real `argparse`-style help listing the discovered app names as
        subparser choices, and exits 2
        """
        exit_code = dispatch([])
        assert exit_code == 2
        err = remove_color_code(capsys.readouterr().err)
        assert "usage: api-client [-h]" in err
        assert "positional arguments:" in err
        assert "options:" in err
        assert "-h, --help" in err

    @pytest.mark.parametrize(
        ("installed", "hint_expected"),
        [
            pytest.param(False, True, id="not_installed"),
            pytest.param(True, False, id="installed"),
        ],
    )
    def test_missing_target_shows_the_tab_completion_hint_only_when_argcomplete_is_not_installed(
        self, installed: bool, hint_expected: bool, mocker: MockerFixture, capsys: CaptureFixture[str]
    ) -> None:
        """Test that omitting the target surfaces the tab-completion setup tip when `argcomplete` isn't
        installed, since a bare `api-client` invocation is a common first point of contact with the CLI,
        and omits it once `argcomplete` is installed, since there's nothing left for the tip to cover
        """
        patch_argcomplete_installed(mocker, installed=installed)
        exit_code = dispatch([])
        assert exit_code == 2
        # Collapsed to single-spaced text so the assertion survives the tip wrapping onto a continuation
        # line at a narrower terminal width, rather than asserting the tip's exact unwrapped substring
        err = " ".join(remove_color_code(capsys.readouterr().err).split())
        assert (_TAB_COMPLETION_TIP in err) is hint_expected

    def test_missing_target_shows_discovery_warnings(self, mocker: MockerFixture, capsys: CaptureFixture[str]) -> None:
        """Test that omitting the target, with discovery having found nothing and hit an import failure,
        surfaces both warnings before the tab-completion tip, since a bare `api-client` invocation with
        no usable app is where a user most needs to know why
        """
        patch_argcomplete_installed(mocker, installed=False)
        mocker.patch(
            "api_client_core.cli.builder.discover_clients_with_failures",
            return_value=DiscoveryResult({}, ["broken_module: ImportError: no module named 'x'"], []),
        )
        exit_code = dispatch([])
        assert exit_code == 2
        err = remove_color_code(capsys.readouterr().err)
        assert "warnings:" in err
        assert "No API clients were discovered" in err
        assert "1 module(s) failed to import" in err
        assert err.index("warnings:") < err.index("tip:")

    @pytest.mark.parametrize("flag", ["-h", "--help"])
    def test_leading_help_flag_prints_usage_and_exits_zero(self, flag: str, capsys: CaptureFixture[str]) -> None:
        """Test that a leading -h/--help (no app name given yet) prints real `argparse`-style help to
        stdout and exits 0, rather than being treated as an unknown app name
        """
        exit_code = dispatch([flag])
        assert exit_code == 0
        out = remove_color_code(capsys.readouterr().out)
        assert "usage: api-client [-h]" in out
        assert "positional arguments:" in out
        assert "options:" in out
        assert "-h, --help" in out

    def test_help_flag_with_no_discovered_clients_omits_the_empty_choice_list(
        self, mocker: MockerFixture, capsys: CaptureFixture[str]
    ) -> None:
        """Test that -h with no discovered clients shows the `<app-name>` placeholder, instead of an empty `{}`
        choice list
        """
        mocker.patch(
            "api_client_core.cli.builder.discover_clients_with_failures", return_value=DiscoveryResult({}, [], [])
        )

        exit_code = dispatch(["-h"])

        assert exit_code == 0
        out = remove_color_code(capsys.readouterr().out)
        assert "usage: api-client [-h] [--version] <app-name> ..." in out
        assert "{}" not in out

    def test_help_flag_after_a_resolved_app_name_is_not_special_cased(self, mocker: MockerFixture) -> None:
        """Test that -h/--help is only special-cased as the leading token: once an app name
        resolves, -h is forwarded to run() like any other argument, where the client's own
        generated parser handles it
        """
        mock_find_client = mocker.patch("api_client_core.cli.dispatch.find_client", return_value=CliTestClient)
        mock_run = mocker.patch("api_client_core.cli.dispatch.run", return_value=0)

        dispatch(["cli-test", "-h"])

        mock_find_client.assert_called_once_with("cli-test")
        assert mock_run.call_args.args == (CliTestClient, ["-h"])

    def test_base_url_flag_has_no_bearing_on_whether_a_client_is_discoverable(self, mocker: MockerFixture) -> None:
        """Test that --base-url given before the resource has no effect on discoverability: discovery
        resolves an app name purely from its declared `app_name` class attribute, never by constructing the
        candidate, so a client whose constructor requires `base_url` (no default) is discoverable with the
        flag given, identically to without it (see the next test)
        """

        class NeedsBaseUrlClient(APIClient):
            app_name = "needs-base-url-test"

            def __init__(self, *, base_url: str) -> None:
                super().__init__(base_url=base_url)

        mock_run = mocker.patch("api_client_core.cli.dispatch.run", return_value=0)

        exit_code = dispatch(["needs-base-url-test", "--base-url", "https://example.com/api", "widgets", "get-widget"])

        assert exit_code == 0
        assert mock_run.call_args.args[0] is NeedsBaseUrlClient

    def test_a_client_requiring_base_url_is_discoverable_even_without_the_flag(self, mocker: MockerFixture) -> None:
        """Test that a client whose constructor requires `base_url` (no default) is still discoverable with
        no `--base-url` given at all, since discovery never constructs it - the resulting construction
        failure only surfaces later, from run()'s own construction, not from discovery
        """

        class StillNeedsBaseUrlClient(APIClient):
            app_name = "still-needs-base-url-test"

            def __init__(self, *, base_url: str) -> None:
                super().__init__(base_url=base_url)

        mock_run = mocker.patch("api_client_core.cli.dispatch.run", return_value=0)

        exit_code = dispatch(["still-needs-base-url-test", "widgets", "get-widget"])

        assert exit_code == 0
        assert mock_run.call_args.args[0] is StillNeedsBaseUrlClient

    def test_setup_logging_runs_before_discovery(self, mocker: MockerFixture) -> None:
        """Test that dispatch() configures logging before build_initial_parser() (and therefore before
        the discover_clients() call it triggers) runs, so a warning or debug message logged during
        discovery reaches the user instead of being silently dropped by the NullHandler attached until
        setup_logging() configures a real handler
        """
        mock_setup_logging = mocker.patch("api_client_core.cli.dispatch.setup_logging")
        mock_build_initial_parser = mocker.patch(
            "api_client_core.cli.dispatch.build_initial_parser", side_effect=build_initial_parser
        )
        manager = mocker.Mock()
        manager.attach_mock(mock_setup_logging, "setup_logging")
        manager.attach_mock(mock_build_initial_parser, "build_initial_parser")

        dispatch(["-h"])

        assert [call[0] for call in manager.mock_calls] == ["setup_logging", "build_initial_parser"]

    def test_invalid_log_level_is_reported_cleanly_by_the_real_parser(
        self, mocker: MockerFixture, capsys: CaptureFixture[str]
    ) -> None:
        """Test that an invalid --log-level value reaches the real client parser's own `choices`
        validation and is reported as a clean argparse error with exit code 2, rather than propagating
        out of _bootstrap()'s setup_logging() call as an uncaught ValueError
        """
        mocker.patch("api_client_core.cli.dispatch.find_client", return_value=CliTestClient)
        with pytest.raises(SystemExit) as exc_info:
            dispatch(["cli-test", "--log-level", "BOGUS", "widgets", "get-widget", "--widget-id", "1"])
        assert exc_info.value.code == 2
        assert "invalid choice: 'BOGUS'" in capsys.readouterr().err

    def test_the_resolved_client_class_is_constructed_exactly_once_per_real_run(
        self, mocker: MockerFixture, capsys: CaptureFixture[str]
    ) -> None:
        """Test that a real run constructs the resolved client class exactly once: `find_client()` resolves
        the app name to a bare class without ever constructing it, so the only construction in the whole
        pipeline is `run()`'s own, once argument parsing succeeds.

        Exercises the real, unmocked `find_client()`/`run()` pipeline end to end (only the underlying HTTP
        transport is mocked), against the example `DummyJSONClient`, which this checkout's own project scan
        already discovers (see `TestFindClient` in test_discovery.py)
        """
        init_spy = mocker.spy(DummyJSONClient, "__init__")
        mocker.patch.object(
            Client, "request", return_value=make_httpx_response(mocker, 200, json_body={"products": []})
        )

        exit_code = dispatch(["DummyJSON", "products", "list-products", "-q"])

        assert exit_code == 0
        assert init_spy.call_count == 1

"""Unit tests for `api_client_core.cli.runner`"""

import json
import sys
from typing import Any

import httpx2
import pytest
from common_libs.ansi_colors import ColorCodes, remove_color_code
from common_libs.clients.rest_client import AsyncRestClient, RestClient
from common_libs.clients.rest_client.types import Response
from common_libs.clients.rest_client.utils import set_request_to_exception
from httpx2 import Client, ConnectError, HTTPError, Request
from pytest import CaptureFixture
from pytest_mock import MockerFixture

from api_client_core import APIClient
from api_client_core.cli._stdout import cli_stdout, reserve_stdout
from api_client_core.cli.builder import build_client_parser
from api_client_core.cli.runner import _request_line, run
from api_client_core.endpoints.endpoint import Endpoint

from .conftest import CliTestClient, CollisionClient, PositionalOnlyClient, make_httpx_response, make_rest_response


class TestRun:
    """Tests for `run()`"""

    def test_dispatches_to_the_correct_endpoint_with_parsed_params(self, mocker: MockerFixture) -> None:
        """Test that run() resolves the parsed command to the right Endpoint and forwards path/body params"""
        mock_call = mocker.patch.object(Endpoint, "_call", return_value=make_rest_response(mocker, 200))
        run(
            CliTestClient,
            ["widgets", "create-widget", "--name", "w", "--owner-id", "7"],
            rest_client=RestClient("https://example.com/api"),
        )
        assert mock_call.call_args.kwargs["name"] == "w"
        assert mock_call.call_args.kwargs["owner_id"] == 7

    def test_quiet_and_no_hooks_flags_are_forwarded(self, mocker: MockerFixture) -> None:
        """Test that --quiet and --no-hooks, given after the command's own flags (mirroring the
        quiet/with_hooks kwargs accepted by a direct endpoint function call), are forwarded to the
        endpoint call as quiet=True/with_hooks=False
        """
        mock_call = mocker.patch.object(Endpoint, "_call", return_value=make_rest_response(mocker, 200))
        run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1", "--quiet", "--no-hooks"],
            rest_client=RestClient("https://example.com/api"),
        )
        assert mock_call.call_args.kwargs["quiet"] is True
        assert mock_call.call_args.kwargs["with_hooks"] is False

    def test_default_quiet_and_hooks_are_forwarded(self, mocker: MockerFixture) -> None:
        """Test that omitting --quiet/--no-hooks forwards quiet=None (defer to the client's own
        log_requests default) and with_hooks=True
        """
        mock_call = mocker.patch.object(Endpoint, "_call", return_value=make_rest_response(mocker, 200))
        run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1"],
            rest_client=RestClient("https://example.com/api"),
        )
        assert mock_call.call_args.kwargs["quiet"] is None
        assert mock_call.call_args.kwargs["with_hooks"] is True

    def test_raw_option_is_parsed_and_forwarded(self, mocker: MockerFixture) -> None:
        """Test that repeated --raw-option flags, given after the command's own flags, are parsed into
        a raw_options dict and forwarded
        """
        mock_call = mocker.patch.object(Endpoint, "_call", return_value=make_rest_response(mocker, 200))
        run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1", "--raw-option", "timeout=30"],
            rest_client=RestClient("https://example.com/api"),
        )
        assert mock_call.call_args.kwargs["raw_options"] == {"timeout": 30}

    def test_omitted_base_url_flag_does_not_override_client_kwargs(self, mocker: MockerFixture) -> None:
        """Test that omitting --base-url does not forward base_url=None, which would raise ValueError
        from APIClient.__init__ when a base_url was already supplied via client_kwargs
        """
        mocker.patch.object(Client, "request", return_value=mocker.MagicMock(spec=Response, status_code=200))
        mocker.patch.object(Endpoint, "_call", return_value=make_rest_response(mocker, 200))
        exit_code = run(
            CliTestClient, ["widgets", "get-widget", "--widget-id", "1"], base_url="https://example.com/api"
        )
        assert exit_code == 0

    def test_base_url_flag_overrides_client_kwargs(self, mocker: MockerFixture) -> None:
        """Test that --base-url overrides the base_url the client would otherwise use"""
        received: dict[str, Any] = {}

        class RecordingClient(CliTestClient):
            def __init__(self, **kwargs: Any) -> None:
                received.update(kwargs)
                super().__init__(**kwargs)

        mocker.patch.object(Client, "request", return_value=mocker.MagicMock(spec=Response, status_code=200))
        mocker.patch.object(Endpoint, "_call", return_value=make_rest_response(mocker, 200))
        run(
            RecordingClient,
            ["--base-url", "https://override.example.com", "widgets", "get-widget", "--widget-id", "1"],
        )
        assert received["base_url"] == "https://override.example.com"

    def test_end_to_end_request_reaches_httpx2_with_correct_method_and_path(self, mocker: MockerFixture) -> None:
        """Test that a parsed command reaches the real httpx2 client with the correct method and path,
        without mocking Endpoint._call, proving the full introspect -> argparse -> dispatch pipeline
        """
        response = make_httpx_response(mocker, 200, json_body={"id": 1})
        mock_request = mocker.patch.object(Client, "request", return_value=response)

        exit_code = run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1"],
            rest_client=RestClient("https://example.com/api"),
        )

        assert exit_code == 0
        assert mock_request.call_args.args == ("GET", "/widgets/1")

    def test_non_2xx_response_returns_nonzero_exit_code(self, mocker: MockerFixture) -> None:
        """Test that run() returns a non-zero exit code for a non-2xx response"""
        mocker.patch.object(Endpoint, "_call", return_value=make_rest_response(mocker, 404))
        exit_code = run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1"],
            rest_client=RestClient("https://example.com/api"),
        )
        assert exit_code == 1

    def test_required_flag_omission_exits_cleanly_rather_than_crashing(self, capsys: CaptureFixture[str]) -> None:
        """Test that omitting a required flag raises SystemExit via argparse, not a raw TypeError from the
        original endpoint function (the mechanic this project depends on: required-ness must be derived
        from the original signature, not the model, since both required and optional body/query fields
        default to Unset on the model), with the error naming the missing flag rather than leaving the
        user to guess which one was required
        """
        with pytest.raises(SystemExit) as exc_info:
            run(
                CliTestClient,
                ["widgets", "create-widget", "--name", "w"],
                rest_client=RestClient("https://example.com/api"),
            )
        assert exc_info.value.code == 2
        assert "--owner-id" in remove_color_code(capsys.readouterr().err)

    def test_malformed_raw_option_exits_cleanly_rather_than_crashing(self) -> None:
        """Test that a malformed --raw-option value raises SystemExit via argparse (a clean error and
        exit code 2), not an uncaught ArgumentTypeError once parsing has already completed
        """
        with pytest.raises(SystemExit) as exc_info:
            run(
                CliTestClient,
                ["widgets", "get-widget", "--widget-id", "1", "--raw-option", "not-a-kv-pair"],
                rest_client=RestClient("https://example.com/api"),
            )
        assert exc_info.value.code == 2

    def test_invalid_command_name_exits_with_a_usage_error_naming_it(self, capsys: CaptureFixture[str]) -> None:
        """Test that a mistyped command name is rejected by argparse's own generated `<command>`
        subparser (a clean error and exit code 2, not a crash), with the error naming the bad token
        rather than leaving the user to guess what went wrong
        """
        with pytest.raises(SystemExit) as exc_info:
            run(
                CliTestClient,
                ["widgets", "get-widgt", "--widget-id", "1"],
                rest_client=RestClient("https://example.com/api"),
            )
        assert exc_info.value.code == 2
        assert "get-widgt" in remove_color_code(capsys.readouterr().err)

    def test_invalid_resource_name_exits_with_a_usage_error_naming_it(self, capsys: CaptureFixture[str]) -> None:
        """Test that a mistyped resource name is rejected by argparse's own generated `<resource-group>`
        subparser, the same as a mistyped command name one level deeper: a clean error and exit code 2,
        with the error naming the bad token
        """
        with pytest.raises(SystemExit) as exc_info:
            run(
                CliTestClient,
                ["widgetz", "get-widget", "--widget-id", "1"],
                rest_client=RestClient("https://example.com/api"),
            )
        assert exc_info.value.code == 2
        assert "widgetz" in remove_color_code(capsys.readouterr().err)

    def test_no_resource_shows_the_app_levels_own_short_help(self, capsys: CaptureFixture[str]) -> None:
        """Test that omitting the resource entirely exits 2 and shows the app-level parser's own condensed
        help on stderr, naming the available resources - rather than argparse's bare "the following
        arguments are required" (`_resource`/`_command` are registered as not required for exactly this,
        see `build_client_parser()`) - so a first-time user is shown what's available, not just told that
        something is missing
        """
        exit_code = run(CliTestClient, [], rest_client=RestClient("https://example.com/api"))
        assert exit_code == 2
        err = remove_color_code(capsys.readouterr().err)
        assert "<resource-group>" in err
        assert "widgets" in err
        assert "gadgets" in err
        assert "_resource" not in err

    def test_no_command_shows_the_resource_levels_own_short_help(self, capsys: CaptureFixture[str]) -> None:
        """Test that a resource given with no command exits 2 and shows that resource's own condensed help
        on stderr, naming its available commands, mirroring
        `test_no_resource_shows_the_app_levels_own_short_help` one level down
        """
        exit_code = run(CliTestClient, ["widgets"], rest_client=RestClient("https://example.com/api"))
        assert exit_code == 2
        err = remove_color_code(capsys.readouterr().err)
        assert "<command>" in err
        assert "get-widget" in err
        assert "_command" not in err

    def test_no_client_is_constructed_for_an_incomplete_command(self, mocker: MockerFixture) -> None:
        """Test that an incomplete command never constructs a client at all, so a client whose constructor
        carries a side effect (e.g. a login call) isn't run just to report that the command is incomplete
        """
        init_spy = mocker.spy(CliTestClient, "__init__")
        run(CliTestClient, ["widgets"], rest_client=RestClient("https://example.com/api"))
        init_spy.assert_not_called()

    @pytest.mark.parametrize(
        ("error", "expected_message"),
        [
            (HTTPError("simulated failure"), "error: HTTPError: simulated failure"),
            (ValueError("bad params"), "error: ValueError: bad params"),
        ],
    )
    def test_error_from_dispatch_exits_cleanly_rather_than_crashing(
        self, mocker: MockerFixture, capsys: CaptureFixture[str], error: Exception, expected_message: str
    ) -> None:
        """Test that an exception raised during dispatch, whether an HTTPError (e.g. a transport failure, or
        raise_on_error surfacing a non-2xx as HTTPStatusError) or any other exception (e.g. a framework-level
        validation error), is caught and reported as a clean error on stderr with a non-zero exit code, not
        an uncaught traceback
        """
        mocker.patch.object(Endpoint, "_call", side_effect=error)
        exit_code = run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1"],
            rest_client=RestClient("https://example.com/api"),
        )
        assert exit_code == 1
        assert expected_message in capsys.readouterr().err

    def test_error_already_reported_by_the_rest_client_is_not_duplicated(
        self, mocker: MockerFixture, capsys: CaptureFixture[str]
    ) -> None:
        """Test that an exception the underlying REST client already logged in full (with its own
        request_id/traceback, via common_libs' HTTPClientMixin._handle_error(), which marks the
        exception with set_request_to_exception() before logging it) is not also reported a second
        time by write_error(): get_request_from_exception() detects that marker, so run() skips its
        own redundant error line and just returns the failure exit code
        """
        error = ConnectError("Connection refused")
        set_request_to_exception(error, Request("GET", "https://example.com/api/widgets/1"))
        mocker.patch.object(Endpoint, "_call", side_effect=error)
        exit_code = run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1"],
            rest_client=RestClient("https://example.com/api"),
        )
        assert exit_code == 1
        assert "error: ConnectError: Connection refused" not in capsys.readouterr().err

    def test_async_client_exits_cleanly_rather_than_crashing(self, capsys: CaptureFixture[str]) -> None:
        """Test that an async-mode client is rejected with a clean error and exit code 2, not a bare
        TypeError from APIClient.__enter__ (which expects 'async with', not 'with')
        """
        exit_code = run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1"],
            rest_client=AsyncRestClient("https://example.com/api"),
            async_mode=True,
        )
        assert exit_code == 2
        assert "async mode" in capsys.readouterr().err

    def test_async_client_is_closed_on_rejection(self, mocker: MockerFixture) -> None:
        """Test that the async-mode client instantiated to check async_mode is closed again before
        returning, rather than leaking its AsyncRestClient connection pool
        """
        close_spy = mocker.patch.object(APIClient, "aclose", autospec=True)
        run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1"],
            rest_client=AsyncRestClient("https://example.com/api"),
            async_mode=True,
        )
        close_spy.assert_called_once()

    def test_reserved_quiet_flag_collision_dispatches_using_the_params_own_default(
        self, mocker: MockerFixture, collision_client_class: type[CollisionClient]
    ) -> None:
        """Test that a command whose endpoint has a parameter colliding with --quiet still dispatches
        successfully, using that parameter's own Python default, rather than the --quiet control flag's
        value leaking into collect_call_kwargs() output and colliding with the `quiet` keyword run()
        forwards separately. This previously caused a TypeError, caught as a spurious 'error: ...' failure
        on every call to this endpoint
        """
        mock_call = mocker.patch.object(Endpoint, "_call", return_value=make_rest_response(mocker, 200))
        exit_code = run(
            collision_client_class,
            ["things", "make-thing", "--name", "foo"],
            rest_client=RestClient("https://example.com/api"),
        )
        assert exit_code == 0
        assert mock_call.call_args.kwargs["name"] == "foo"
        assert mock_call.call_args.kwargs["quiet"] is None

    def test_control_kwarg_name_collision_dispatches_without_raising(
        self, mocker: MockerFixture, collision_client_class: type[CollisionClient]
    ) -> None:
        """Test that a command whose endpoint has parameters literally named `with_hooks`/`raw_options`
        still dispatches successfully (using their own Python defaults), rather than run()'s
        call(**call_kwargs, **ctrl_kwargs) raising 'got multiple values for keyword argument' at dispatch
        """
        mock_call = mocker.patch.object(Endpoint, "_call", return_value=make_rest_response(mocker, 200))
        exit_code = run(
            collision_client_class,
            ["things", "make-hook-thing", "--name", "foo"],
            rest_client=RestClient("https://example.com/api"),
        )
        assert exit_code == 0
        assert mock_call.call_args.kwargs["name"] == "foo"
        assert mock_call.call_args.kwargs["with_hooks"] is True
        assert mock_call.call_args.kwargs["raw_options"] == {}

    def test_positional_only_path_param_end_to_end(
        self, mocker: MockerFixture, posonly_client_class: type[PositionalOnlyClient]
    ) -> None:
        """Test that a positional-only path param, unreachable from the CLI before normalize_call_args(),
        reaches the real httpx2 client with the correct method and path
        """
        response = make_httpx_response(mocker, 200, json_body={"id": 1})
        mock_request = mocker.patch.object(Client, "request", return_value=response)

        exit_code = run(
            posonly_client_class,
            ["items", "get-item", "--item-id", "7", "--note", "hi"],
            rest_client=RestClient("https://example.com/api"),
        )

        assert exit_code == 0
        assert mock_request.call_args.args == ("GET", "/items/7")

    def test_positional_only_gap_filled_from_default_end_to_end(
        self, mocker: MockerFixture, posonly_client_class: type[PositionalOnlyClient]
    ) -> None:
        """Test that run() fills a skipped earlier positional-only param (`page`) from its own default to
        reach a later one given by flag alone (`--size`), the same value a direct positional call omitting
        `page` would bind
        """
        response = make_httpx_response(mocker, 200, json_body={"id": 1})
        mock_request = mocker.patch.object(Client, "request", return_value=response)

        exit_code = run(
            posonly_client_class,
            ["items", "list-pages", "--size", "50"],
            rest_client=RestClient("https://example.com/api"),
        )

        assert exit_code == 0
        assert mock_request.call_args.args == ("GET", "/pages")
        assert mock_request.call_args.kwargs["params"] == {"page": 1, "size": 50}

    def test_reserved_help_flag_collision_dispatches_without_raising(
        self, mocker: MockerFixture, collision_client_class: type[CollisionClient]
    ) -> None:
        """Test that a command whose endpoint has a parameter colliding with -h/--help still dispatches
        successfully, rather than collect_call_kwargs() raising AttributeError trying to read a `help`
        attribute the namespace never has
        """
        mock_call = mocker.patch.object(Endpoint, "_call", return_value=make_rest_response(mocker, 200))
        exit_code = run(
            collision_client_class,
            ["things", "make-other-thing", "--name", "foo"],
            rest_client=RestClient("https://example.com/api"),
        )
        assert exit_code == 0
        assert mock_call.call_args.kwargs["name"] == "foo"

    def test_client_instantiation_failure_exits_cleanly_rather_than_crashing(self, capsys: CaptureFixture[str]) -> None:
        """Test that a client class that raises during construction (e.g. rejecting a keyword argument
        forwarded from --base-url) is reported as a clean error with exit code 2, not an uncaught
        traceback
        """

        class BrokenClient(CliTestClient):
            def __init__(self, **kwargs: Any) -> None:
                raise ValueError("cannot construct")

        exit_code = run(
            BrokenClient, ["widgets", "get-widget", "--widget-id", "1"], rest_client=RestClient("https://example.com")
        )
        assert exit_code == 2
        assert "error: ValueError: cannot construct" in capsys.readouterr().err

    def test_client_with_no_api_classes_exits_cleanly_rather_than_crashing(self, capsys: CaptureFixture[str]) -> None:
        """Test that a client exposing no discoverable API classes (build_parser()'s own RuntimeError)
        is reported as a clean error with exit code 2, not an uncaught traceback
        """

        class EmptyClient(APIClient):
            app_name = "empty"

        exit_code = run(EmptyClient, [], rest_client=RestClient("https://example.com/api"))
        assert exit_code == 2
        assert "No API classes discovered" in capsys.readouterr().err

    def test_non_runtime_error_from_build_parser_exits_cleanly_rather_than_crashing(
        self, mocker: MockerFixture, capsys: CaptureFixture[str]
    ) -> None:
        """Test that a non-RuntimeError exception raised while building the parser is reported as a clean
        error with exit code 2, not an uncaught traceback
        """
        mocker.patch("api_client_core.cli.runner.build_client_parser", side_effect=ValueError("simulated failure"))
        exit_code = run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1"],
            rest_client=RestClient("https://example.com/api"),
        )
        assert exit_code == 2
        assert "error: ValueError: simulated failure" in capsys.readouterr().err

    @pytest.mark.parametrize("prog", ["api-client cli-test", None])
    def test_prog_is_forwarded_to_build_client_parser(self, mocker: MockerFixture, prog: str | None) -> None:
        """Test that a given `prog` is forwarded to build_client_parser(), so the generated parser's usage
        text reflects the caller-supplied program name rather than argparse's own inference, and that
        omitting it (the default, `None`) still dispatches successfully, forwarding `prog=None` so
        build_client_parser() falls back to its own default of argparse's usual inference
        """
        mocker.patch.object(Endpoint, "_call", return_value=make_rest_response(mocker, 200))
        mock_build_client_parser = mocker.patch(
            "api_client_core.cli.runner.build_client_parser", side_effect=build_client_parser
        )
        exit_code = run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1"],
            prog=prog,
            rest_client=RestClient("https://example.com/api"),
        )
        assert mock_build_client_parser.call_args.kwargs["prog"] == prog
        assert exit_code == 0

    def test_setup_logging_runs_before_build_client_parser(self, mocker: MockerFixture) -> None:
        """Test that setup_logging() runs before build_client_parser(), so a warning logged while
        building the parser (e.g. a reserved-flag collision) reaches the user instead of being
        silently dropped by the NullHandler attached until setup_logging() configures a real handler
        """
        mock_setup_logging = mocker.patch("api_client_core.cli.runner.setup_logging")
        mock_build_client_parser = mocker.patch(
            "api_client_core.cli.runner.build_client_parser", side_effect=build_client_parser
        )
        manager = mocker.Mock()
        manager.attach_mock(mock_setup_logging, "setup_logging")
        manager.attach_mock(mock_build_client_parser, "build_client_parser")

        run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1"],
            rest_client=RestClient("https://example.com/api"),
        )

        assert [call[0] for call in manager.mock_calls] == ["setup_logging", "build_client_parser"]

    def test_log_level_param_is_applied_before_build_client_parser(self, mocker: MockerFixture) -> None:
        """Test that a `log_level` passed to run() (as dispatch() resolves via its own pre-parse of
        --log-level, before the real parser exists) is applied on the very first setup_logging() call,
        ahead of build_client_parser()'s own discovery, so a discovery-time warning is logged at the
        requested verbosity
        """
        mocker.patch.object(Endpoint, "_call", return_value=make_rest_response(mocker, 200))
        mock_setup_logging = mocker.patch("api_client_core.cli.runner.setup_logging")

        run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1"],
            rest_client=RestClient("https://example.com/api"),
            log_level="DEBUG",
        )

        assert mock_setup_logging.call_args_list[0].kwargs["level"] == "DEBUG"

    def test_log_level_flag_in_argv_is_applied_even_without_the_log_level_param(self, mocker: MockerFixture) -> None:
        """Test that --log-level parsed from argv is also applied, covering a caller invoking run()
        directly without going through dispatch()'s own pre-parse: the log_level param is omitted here,
        so only the flag itself (parsed from the real client parser) carries the requested level
        """
        mocker.patch.object(Endpoint, "_call", return_value=make_rest_response(mocker, 200))
        mock_setup_logging = mocker.patch("api_client_core.cli.runner.setup_logging")

        run(
            CliTestClient,
            ["--log-level", "WARNING", "widgets", "get-widget", "--widget-id", "1"],
            rest_client=RestClient("https://example.com/api"),
        )

        assert mock_setup_logging.call_args_list[-1].kwargs["level"] == "WARNING"

    def test_setup_logging_is_called_only_once_when_argv_agrees_with_log_level(self, mocker: MockerFixture) -> None:
        """Test that a real client parser resolving the same --log-level value already given via the
        `log_level` param (the common `dispatch()`-driven case: the value it already peeked to build the
        parser) reuses that first `setup_logging()` call instead of repeating an identical one.

        Regression test (C6): before this, a --log-level given anywhere on the command line always
        triggered a second, redundant setup_logging() call even when it resolved to the exact same value
        """
        mocker.patch.object(Endpoint, "_call", return_value=make_rest_response(mocker, 200))
        mock_setup_logging = mocker.patch("api_client_core.cli.runner.setup_logging")

        run(
            CliTestClient,
            ["--log-level", "DEBUG", "widgets", "get-widget", "--widget-id", "1"],
            rest_client=RestClient("https://example.com/api"),
            log_level="DEBUG",
        )

        assert mock_setup_logging.call_count == 1

    def test_setup_logging_is_called_twice_when_argv_disagrees_with_log_level(self, mocker: MockerFixture) -> None:
        """Test that a real client parser resolving a *different* --log-level than the `log_level` param
        still re-applies it, so the more specific, later-given value still wins in the rare case where they
        actually differ
        """
        mocker.patch.object(Endpoint, "_call", return_value=make_rest_response(mocker, 200))
        mock_setup_logging = mocker.patch("api_client_core.cli.runner.setup_logging")

        run(
            CliTestClient,
            ["--log-level", "WARNING", "widgets", "get-widget", "--widget-id", "1"],
            rest_client=RestClient("https://example.com/api"),
            log_level="DEBUG",
        )

        assert mock_setup_logging.call_count == 2
        assert mock_setup_logging.call_args_list[0].kwargs["level"] == "DEBUG"
        assert mock_setup_logging.call_args_list[1].kwargs["level"] == "WARNING"


class TestStdoutGuard:
    """Tests for `run()`'s behavior under the process-wide `reserve_stdout()` reservation, which a real run
    opens once in `_entrypoint.py`'s `main()`, around the whole `dispatch()` call. Reproduced here with an
    explicit `with reserve_stdout():` around each `run()` call, since these tests drive `run()` directly:
    a fixture that opens the reservation during its own setup doesn't work for this, since pytest's capture
    manager reinstalls `sys.stdout`/`sys.stderr` at the setup-to-call boundary, discarding whatever a
    fixture redirected them to during setup
    """

    def test_help_reaches_stdout_at_every_level(self, capsys: CaptureFixture[str]) -> None:
        """Test that `--help` on the top-level client parser, a resource parser, and a leaf command
        parser all reach the real stdout: parser construction and `parse_args()` both run before `run()`'s
        own call/teardown block, so a `--help` exit is unaffected by whether a reservation is active
        """
        for argv in (["--help"], ["widgets", "--help"], ["widgets", "get-widget", "--help"]):
            with pytest.raises(SystemExit) as exc_info:
                run(CliTestClient, argv, rest_client=RestClient("https://example.com/api"))
            assert exc_info.value.code == 0
            assert "usage:" in capsys.readouterr().out

    def test_help_colorization_follows_stdout_not_stderr(
        self, monkeypatch: pytest.MonkeyPatch, capsys: CaptureFixture[str]
    ) -> None:
        """Test that `--help` text is colorized against the real stdout rather than stderr: a stderr that
        happens to be a tty must not turn on color for help text bound for a piped (non-tty) stdout
        """
        monkeypatch.setattr(sys.stderr, "isatty", lambda: True, raising=False)
        with pytest.raises(SystemExit) as exc_info:
            run(CliTestClient, ["widgets", "get-widget", "--help"], rest_client=RestClient("https://example.com/api"))
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert out == remove_color_code(out)

    def test_client_teardown_stdout_write_is_redirected(
        self, mocker: MockerFixture, capsys: CaptureFixture[str]
    ) -> None:
        """Test that a stray stdout write made during client teardown (after the call itself has already
        returned) still lands on stderr while the reservation is active
        """
        mocker.patch.object(Endpoint, "_call", return_value=make_rest_response(mocker, 200))
        mocker.patch.object(APIClient, "close", side_effect=lambda *a, **k: print("stray teardown write"))  # noqa: T201

        with reserve_stdout():
            exit_code = run(
                CliTestClient,
                ["widgets", "get-widget", "--widget-id", "1"],
                rest_client=RestClient("https://example.com/api"),
            )

        assert exit_code == 0
        out, err = capsys.readouterr()
        assert out == ""
        assert "stray teardown write" in err

    def test_error_colorization_follows_stderr_regardless_of_the_reservation(
        self, monkeypatch: pytest.MonkeyPatch, capsys: CaptureFixture[str]
    ) -> None:
        """Test that with a tty stderr and a non-tty stdout, help text (stdout-bound) stays uncolored while
        an `error:` line (stderr-bound, from `ArgumentParser.error()`) is colored, both while a reservation
        is active: `color()` always decides from `sys.stdout`, which points at `sys.stderr` while the
        reservation is active, so a stderr-bound message colorizes correctly on its own, while
        `color_output()` has to restore the real stdout so a stdout-bound one doesn't follow stderr's
        tty-ness instead.

        `sys.stdout` and `sys.stderr` are the very same object while the reservation is active, so the two
        streams patched here are `sys.stderr` and `cli_stdout()` (the real stdout, patched before the
        reservation is opened, since it's the same object either way), not `sys.stdout` itself. Patched
        directly on the objects `capsys` installs, rather than relying on `common_libs`' autouse tty
        fixture, which only patches the streams that exist before `capsys` installs its own.

        The `--output bogus` case checks the usage block `ArgumentParser.error()` prints ahead of the
        `error:` line, not just the `error:` line itself: both are stderr-bound, so both must colorize
        against stderr's tty-ness rather than only the line `error()` composes directly
        """
        monkeypatch.setattr(sys.stderr, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(cli_stdout(), "isatty", lambda: False, raising=False)

        with reserve_stdout(), pytest.raises(SystemExit) as exc_info:
            run(CliTestClient, ["widgets", "get-widget", "--help"], rest_client=RestClient("https://example.com/api"))
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert out == remove_color_code(out)

        with reserve_stdout(), pytest.raises(SystemExit) as exc_info:
            run(
                CliTestClient,
                ["widgets", "get-widget", "--widget-id", "1", "--output", "bogus"],
                rest_client=RestClient("https://example.com/api"),
            )
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert err != remove_color_code(err)
        assert f"{ColorCodes.BLUE}{ColorCodes.BOLD}usage: " in err


class TestHeaderFlag:
    """Tests for `-H`/`--header`, applied to the client's underlying httpx2 client via `_apply_headers()`"""

    def test_repeated_header_flags_reach_the_underlying_httpx2_client(self, mocker: MockerFixture) -> None:
        """Test that repeated -H/--header flags, given after the command's own flags, are applied to the
        client's underlying httpx2 client headers
        """
        mocker.patch.object(Endpoint, "_call", return_value=make_rest_response(mocker, 200))
        rest_client = RestClient("https://example.com/api")

        run(
            CliTestClient,
            [
                "widgets",
                "get-widget",
                "--widget-id",
                "1",
                "-H",
                "X-API-Key: secret",
                "--header",
                "X-Trace-Id: abc",
            ],
            rest_client=rest_client,
        )

        assert rest_client.client.headers["X-API-Key"] == "secret"
        assert rest_client.client.headers["X-Trace-Id"] == "abc"

    def test_two_header_flags_naming_the_same_header_both_reach_the_request(self, mocker: MockerFixture) -> None:
        """Test that `-H` given twice for the same header name sends both values as separate headers,
        matching curl's own repeatable `-H`, rather than the second silently overwriting the first.

        `httpx2.Headers.update()` only replaces a value the client already had; it doesn't dedupe two brand
        new occurrences given in the same call.
        """
        mocker.patch.object(Endpoint, "_call", return_value=make_rest_response(mocker, 200))
        rest_client = RestClient("https://example.com/api")

        run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1", "-H", "X-Custom: a", "-H", "X-Custom: b"],
            rest_client=rest_client,
        )

        assert rest_client.client.headers.get_list("X-Custom") == ["a", "b"]

    def test_authorization_header_overrides_a_bearer_token_the_client_set_for_itself(
        self, mocker: MockerFixture
    ) -> None:
        """Test that an explicit -H "Authorization: ..." overrides a bearer token the client set for
        itself (e.g. from a prior login call's post_request_hook), rather than being silently overridden
        by BearerAuth.auth_flow() setting that same header on every request
        """
        mocker.patch.object(Endpoint, "_call", return_value=make_rest_response(mocker, 200))
        rest_client = RestClient("https://example.com/api")
        rest_client.set_bearer_token("old-token")

        run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1", "-H", "Authorization: Bearer new-token"],
            rest_client=rest_client,
        )

        assert rest_client.get_bearer_token() == "new-token"

    def test_omitting_header_flag_leaves_a_bearer_token_the_client_set_for_itself_untouched(
        self, mocker: MockerFixture
    ) -> None:
        """Test that omitting -H entirely doesn't clear a bearer token the client set for itself"""
        mocker.patch.object(Endpoint, "_call", return_value=make_rest_response(mocker, 200))
        rest_client = RestClient("https://example.com/api")
        rest_client.set_bearer_token("keep-me")

        run(CliTestClient, ["widgets", "get-widget", "--widget-id", "1"], rest_client=rest_client)

        assert rest_client.get_bearer_token() == "keep-me"

    def test_malformed_header_exits_cleanly_rather_than_crashing(self) -> None:
        """Test that a malformed -H value raises SystemExit via argparse (a clean error and exit code
        2), not an uncaught ArgumentTypeError once parsing has already completed
        """
        with pytest.raises(SystemExit) as exc_info:
            run(
                CliTestClient,
                ["widgets", "get-widget", "--widget-id", "1", "-H", "no-colon-here"],
                rest_client=RestClient("https://example.com/api"),
            )
        assert exc_info.value.code == 2


class TestOutputFlag:
    """Tests for `--output`'s `none`/`json` values, and `--quiet`, which forwards the `quiet` kwarg to the
    call itself and, like `--output json`, also turns off the client's own request/response logs (see
    `TestOutputJsonSuppressesRequestLogs`), reducing a failed response to a single `error: ...` line rather
    than fully suppressing it.
    """

    def test_default_output_writes_nothing_and_does_not_force_quiet(
        self, mocker: MockerFixture, capsys: CaptureFixture[str]
    ) -> None:
        """Test that omitting --output writes nothing to stdout and leaves quiet=None"""
        mock_call = mocker.patch.object(Endpoint, "_call", return_value=make_rest_response(mocker, 200))
        exit_code = run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1"],
            rest_client=RestClient("https://example.com/api"),
        )
        assert exit_code == 0
        assert mock_call.call_args.kwargs["quiet"] is None
        assert capsys.readouterr().out == ""

    def test_output_json_writes_only_the_response_body(
        self, mocker: MockerFixture, capsys: CaptureFixture[str]
    ) -> None:
        """Test that --output json writes just the response body as JSON to stdout, without forcing
        quiet=True: quiet is a kwarg forwarded to the call itself, left at None by --output alone
        """
        mock_call = mocker.patch.object(
            Endpoint, "_call", return_value=make_rest_response(mocker, 200, json_body={"id": 1, "quote": "hi"})
        )
        exit_code = run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1", "--output", "json"],
            rest_client=RestClient("https://example.com/api"),
        )
        assert exit_code == 0
        assert mock_call.call_args.kwargs["quiet"] is None
        assert capsys.readouterr().out == '{"id": 1, "quote": "hi"}\n'

    def test_quiet_forwards_true_and_does_not_suppress_the_output_json_payload(
        self, mocker: MockerFixture, capsys: CaptureFixture[str]
    ) -> None:
        """Test that --quiet forwards quiet=True regardless of --output, and doesn't itself suppress
        the --output json payload (only the request/response logs, which a successful call has none of)
        """
        mock_call = mocker.patch.object(
            Endpoint, "_call", return_value=make_rest_response(mocker, 200, json_body={"id": 1})
        )
        exit_code = run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1", "--output", "json", "--quiet"],
            rest_client=RestClient("https://example.com/api"),
        )
        assert exit_code == 0
        assert mock_call.call_args.kwargs["quiet"] is True
        assert capsys.readouterr().out == '{"id": 1}\n'

    def test_output_and_quiet_short_aliases(self, mocker: MockerFixture, capsys: CaptureFixture[str]) -> None:
        """Test that -o/-q parse identically to their --output/--quiet long forms"""
        mock_call = mocker.patch.object(
            Endpoint, "_call", return_value=make_rest_response(mocker, 200, json_body={"id": 1})
        )
        exit_code = run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1", "-o", "json", "-q"],
            rest_client=RestClient("https://example.com/api"),
        )
        assert exit_code == 0
        assert mock_call.call_args.kwargs["quiet"] is True
        assert capsys.readouterr().out == '{"id": 1}\n'

    def test_output_none_writes_nothing(self, mocker: MockerFixture, capsys: CaptureFixture[str]) -> None:
        """Test that --output none (the default) writes nothing at all to stdout, without forcing quiet"""
        mock_call = mocker.patch.object(Endpoint, "_call", return_value=make_rest_response(mocker, 200))
        exit_code = run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1", "--output", "none"],
            rest_client=RestClient("https://example.com/api"),
        )
        assert exit_code == 0
        assert mock_call.call_args.kwargs["quiet"] is None
        assert capsys.readouterr().out == ""

    def test_output_json_disables_log_requests_on_the_client(self, mocker: MockerFixture) -> None:
        """Test that --output json sets log_requests=False on the client's rest_client, turning off its
        request/response logs and console summary at the source
        """
        mocker.patch.object(Endpoint, "_call", return_value=make_rest_response(mocker, 200))
        rest_client = RestClient("https://example.com/api")
        run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1", "--output", "json"],
            rest_client=rest_client,
        )
        assert rest_client.log_requests is False

    def test_output_none_leaves_log_requests_enabled_on_the_client(self, mocker: MockerFixture) -> None:
        """Test that the default --output (none) leaves log_requests at its default of True"""
        mocker.patch.object(Endpoint, "_call", return_value=make_rest_response(mocker, 200))
        rest_client = RestClient("https://example.com/api")
        run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1"],
            rest_client=rest_client,
        )
        assert rest_client.log_requests is True

    def test_quiet_alone_disables_log_requests_on_the_client(self, mocker: MockerFixture) -> None:
        """Test that -q alone, without --output json, also sets log_requests=False on the client"""
        mocker.patch.object(Endpoint, "_call", return_value=make_rest_response(mocker, 200))
        rest_client = RestClient("https://example.com/api")
        run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1", "--quiet"],
            rest_client=rest_client,
        )
        assert rest_client.log_requests is False

    def test_output_json_on_a_with_repeat_list_writes_a_json_array(
        self, mocker: MockerFixture, capsys: CaptureFixture[str]
    ) -> None:
        """Test that --output json on a --with-repeat/--with-concurrency list result writes a JSON array
        of each item's own response body, not just the first one
        """
        mocker.patch.object(Client, "request", return_value=make_httpx_response(mocker, 200, json_body={"id": 1}))
        exit_code = run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1", "--with-repeat", "2", "--output", "json"],
            rest_client=RestClient("https://example.com/api"),
        )
        assert exit_code == 0
        assert capsys.readouterr().out == '[{"id": 1}, {"id": 1}]\n'

    def test_output_json_renders_a_captured_exception_as_an_error_object(
        self, mocker: MockerFixture, capsys: CaptureFixture[str]
    ) -> None:
        """Test that a captured exception in a --with-repeat return_exceptions=True list renders as
        `{"error": "..."}` rather than failing json.dumps() outright
        """
        mocker.patch.object(Client, "request", side_effect=HTTPError("simulated failure"))
        exit_code = run(
            CliTestClient,
            [
                "widgets",
                "get-widget",
                "--widget-id",
                "1",
                "--with-repeat",
                "num=1,return_exceptions=true",
                "--output",
                "json",
            ],
            rest_client=RestClient("https://example.com/api"),
        )
        assert exit_code == 1
        out = capsys.readouterr().out
        assert '"error": "HTTPError: simulated failure"' in out

    def test_output_raw_writes_the_undecoded_body_exactly_as_sent(
        self, mocker: MockerFixture, capsys: CaptureFixture[str]
    ) -> None:
        """Test that --output raw writes the response's own undecoded text body verbatim, rather than
        re-serializing the already-`.json()`-decoded body the way --output json does. This is what
        keeps a non-JSON response (plain text, XML, ...) intact instead of turning it into a quoted
        JSON string
        """
        mock_call = mocker.patch.object(
            Endpoint, "_call", return_value=make_rest_response(mocker, 200, text="<xml>not json</xml>")
        )
        exit_code = run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1", "--output", "raw"],
            rest_client=RestClient("https://example.com/api"),
        )
        assert exit_code == 0
        assert mock_call.call_args.kwargs["quiet"] is None
        assert capsys.readouterr().out == "<xml>not json</xml>\n"

    def test_output_raw_on_a_with_repeat_list_writes_one_line_per_item(
        self, mocker: MockerFixture, capsys: CaptureFixture[str]
    ) -> None:
        """Test that --output raw on a --with-repeat/--with-concurrency list result writes each item's own
        raw body on its own line, rather than only the first one
        """
        mocker.patch.object(Client, "request", return_value=make_httpx_response(mocker, 200, text="plain body"))
        exit_code = run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1", "--with-repeat", "2", "--output", "raw"],
            rest_client=RestClient("https://example.com/api"),
        )
        assert exit_code == 0
        assert capsys.readouterr().out == "plain body\nplain body\n"

    def test_output_raw_renders_a_captured_exception_as_its_failure_detail(
        self, mocker: MockerFixture, capsys: CaptureFixture[str]
    ) -> None:
        """Test that a captured exception in a --with-repeat return_exceptions=True list renders as its own
        one-line failure detail under --output raw, since it never received a body to show
        """
        mocker.patch.object(Client, "request", side_effect=HTTPError("simulated failure"))
        exit_code = run(
            CliTestClient,
            [
                "widgets",
                "get-widget",
                "--widget-id",
                "1",
                "--with-repeat",
                "num=1,return_exceptions=true",
                "--output",
                "raw",
            ],
            rest_client=RestClient("https://example.com/api"),
        )
        assert exit_code == 1
        assert capsys.readouterr().out == "HTTPError: simulated failure\n"

    def test_output_full_writes_status_code_headers_and_body(
        self, mocker: MockerFixture, capsys: CaptureFixture[str]
    ) -> None:
        """Test that --output full wraps the status code, response headers, and decoded body in one
        {status_code, headers, body} object, rather than just the bare body --output json writes
        """
        response = make_rest_response(mocker, 200, json_body={"id": 1})
        response._response.headers = {"Content-Type": "application/json"}
        mocker.patch.object(Endpoint, "_call", return_value=response)
        exit_code = run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1", "--output", "full"],
            rest_client=RestClient("https://example.com/api"),
        )
        assert exit_code == 0
        out = json.loads(capsys.readouterr().out)
        assert out == {"status_code": 200, "headers": {"Content-Type": "application/json"}, "body": {"id": 1}}

    def test_output_full_on_a_with_repeat_list_writes_a_json_array(
        self, mocker: MockerFixture, capsys: CaptureFixture[str]
    ) -> None:
        """Test that --output full on a --with-repeat/--with-concurrency list result writes an array of
        each item's own {status_code, headers, body} envelope
        """
        mocker.patch.object(Client, "request", return_value=make_httpx_response(mocker, 200, json_body={"id": 1}))
        exit_code = run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1", "--with-repeat", "2", "--output", "full"],
            rest_client=RestClient("https://example.com/api"),
        )
        assert exit_code == 0
        out = json.loads(capsys.readouterr().out)
        assert len(out) == 2
        assert all(item["status_code"] == 200 and item["body"] == {"id": 1} for item in out)

    def test_output_full_renders_a_captured_exception_as_an_error_object(
        self, mocker: MockerFixture, capsys: CaptureFixture[str]
    ) -> None:
        """Test that a captured exception in a --with-repeat return_exceptions=True list renders as
        {"error": "..."} under --output full, matching --output json's own handling of the same case
        """
        mocker.patch.object(Client, "request", side_effect=HTTPError("simulated failure"))
        exit_code = run(
            CliTestClient,
            [
                "widgets",
                "get-widget",
                "--widget-id",
                "1",
                "--with-repeat",
                "num=1,return_exceptions=true",
                "--output",
                "full",
            ],
            rest_client=RestClient("https://example.com/api"),
        )
        assert exit_code == 1
        out = capsys.readouterr().out
        assert '"error": "HTTPError: simulated failure"' in out

    @pytest.mark.parametrize(
        ("output_json", "expected_out"),
        [
            (True, '{"message": "nope"}\n'),
            (False, ""),
        ],
    )
    def test_stray_stdout_write_during_the_call_is_redirected(
        self, mocker: MockerFixture, capsys: CaptureFixture[str], output_json: bool, expected_out: str
    ) -> None:
        """Test that any write to stdout made during the call itself lands on stderr instead, both under
        --output json (so it can't corrupt the JSON payload a caller pipes into e.g. jq) and under the
        default --output (none), since the process-wide stdout reservation covers the whole call
        unconditionally rather than being scoped to --output json. Under --output none, this is what covers
        the underlying REST client's own non-2xx failure block, which it writes to stdout regardless of
        --quiet; under --output json, that same block is suppressed at the source (see
        `TestOutputJsonSuppressesRequestLogs`), so this guard is a general safety net for any other stray
        write instead
        """

        def fake_call(*args: Any, **kwargs: Any) -> Any:
            print("simulated stray stdout write")  # noqa: T201
            return make_rest_response(mocker, 404, json_body={"message": "nope"})

        mocker.patch.object(Endpoint, "_call", side_effect=fake_call)
        argv = ["widgets", "get-widget", "--widget-id", "1"]
        if output_json:
            argv += ["--output", "json"]
        with reserve_stdout():
            exit_code = run(CliTestClient, argv, rest_client=RestClient("https://example.com/api"))
        assert exit_code == 1
        out, err = capsys.readouterr()
        assert out == expected_out
        assert "simulated stray stdout write" in err

    @pytest.mark.parametrize(
        ("output_json", "expected_out"),
        [
            (True, '{"id": 1}\n'),
            (False, ""),
        ],
    )
    def test_with_stats_report_does_not_leak_onto_stdout(
        self, mocker: MockerFixture, capsys: CaptureFixture[str], output_json: bool, expected_out: str
    ) -> None:
        """Test that --with-stats's own report table lands on stderr rather than stdout, both when --output
        json is given (so it can't corrupt the JSON payload written after it) and under the default
        --output (none), since the process-wide stdout reservation covers the whole call unconditionally
        """
        mocker.patch.object(Client, "request", return_value=make_httpx_response(mocker, 200, json_body={"id": 1}))
        argv = ["widgets", "get-widget", "--widget-id", "1", "--with-stats"]
        if output_json:
            argv += ["--output", "json"]
        with reserve_stdout():
            exit_code = run(CliTestClient, argv, rest_client=RestClient("https://example.com/api"))
        assert exit_code == 0
        out, err = capsys.readouterr()
        assert out == expected_out
        assert "Calls" in err

    def test_invalid_output_value_exits_with_a_usage_error(self, capsys: CaptureFixture[str]) -> None:
        """Test that an --output value outside none/json is rejected by argparse itself (SystemExit,
        exit code 2), not an uncaught error once dispatched. `summary`, the previous default, is also
        rejected: it was removed as a choice
        """
        with pytest.raises(SystemExit) as exc_info:
            run(
                CliTestClient,
                ["widgets", "get-widget", "--widget-id", "1", "--output", "bogus"],
                rest_client=RestClient("https://example.com/api"),
            )
        assert exc_info.value.code == 2
        assert "invalid choice: 'bogus'" in capsys.readouterr().err

        with pytest.raises(SystemExit) as exc_info:
            run(
                CliTestClient,
                ["widgets", "get-widget", "--widget-id", "1", "--output", "summary"],
                rest_client=RestClient("https://example.com/api"),
            )
        assert exc_info.value.code == 2
        assert "invalid choice: 'summary'" in capsys.readouterr().err


def _mock_transport_response(status_code: int, body: dict[str, Any]) -> httpx2.Response:
    """Build a stream-backed JSON response for an `httpx2.MockTransport` handler, so the client reads it the
    same way a real transport does, rather than one preloaded via `json=`

    :param status_code: HTTP status code
    :param body: JSON body
    """
    return httpx2.Response(
        status_code, stream=httpx2.ByteStream(json.dumps(body).encode()), headers={"Content-Type": "application/json"}
    )


class TestOutputJsonSuppressesRequestLogs:
    """End-to-end tests that -q/--quiet and --output json turn off the client's request/response logs and
    console summary, and reduce a failed response to a single error: line instead, through a real
    `httpx2.MockTransport` rather than a mocked `Endpoint._call`/`Client.request`: the request/response
    hooks are dispatched from `SyncHTTPClient._send`, so patching `Client.request` directly (as elsewhere
    in this file) would bypass them entirely and make these assertions vacuous.

    Each `run()` call is wrapped in `reserve_stdout()`, matching a real run: without it, the REST client's
    own console summary (a direct `sys.stdout.write()`, not a log record) would land on captured stdout
    rather than stderr, contaminating the JSON payload assertions instead of the stderr ones.
    """

    def test_failed_call_writes_a_single_error_line_and_no_request_response_logs(
        self, capsys: CaptureFixture[str]
    ) -> None:
        """Test that a 404 with --output json writes the response body to stdout and reduces stderr to a
        single error: line, with neither the request/response log lines nor the console summary block
        """

        def handler(request: httpx2.Request) -> httpx2.Response:
            return _mock_transport_response(404, {"error": "not found"})

        rest_client = RestClient("https://example.com/api", transport=httpx2.MockTransport(handler))
        with reserve_stdout():
            exit_code = run(
                CliTestClient,
                ["widgets", "get-widget", "--widget-id", "1", "--output", "json"],
                rest_client=rest_client,
            )
        out, err = capsys.readouterr()
        assert exit_code == 1
        assert out == '{"error": "not found"}\n'
        error_line = remove_color_code(err)
        assert error_line.startswith("error: GET https://example.com/api/widgets/1 - 404 Not Found (request_id: ")
        assert error_line.endswith(")\n")

    def test_quiet_alone_writes_a_single_error_line_and_no_console_summary(self, capsys: CaptureFixture[str]) -> None:
        """Test that a 404 with -q alone (no --output json) also reduces stderr to a single error: line,
        with neither the request/response log lines nor the console summary block, and writes nothing to
        stdout since -q doesn't select the json output format
        """

        def handler(request: httpx2.Request) -> httpx2.Response:
            return _mock_transport_response(404, {"error": "not found"})

        rest_client = RestClient("https://example.com/api", transport=httpx2.MockTransport(handler))
        with reserve_stdout():
            exit_code = run(
                CliTestClient, ["widgets", "get-widget", "--widget-id", "1", "--quiet"], rest_client=rest_client
            )
        out, err = capsys.readouterr()
        assert exit_code == 1
        assert out == ""
        error_line = remove_color_code(err)
        assert error_line.startswith("error: GET https://example.com/api/widgets/1 - 404 Not Found (request_id: ")
        assert error_line.endswith(")\n")

    def test_failed_call_without_output_json_is_still_logged_and_gets_no_extra_error_line(
        self, capsys: CaptureFixture[str]
    ) -> None:
        """Test that the same 404 without --output json still logs the failure on stderr, guarding against
        over-reach: log_requests must stay enabled when --output isn't json, and the extra error: line is
        only ever added once log_requests has been turned off, so the two must not duplicate each other
        """

        def handler(request: httpx2.Request) -> httpx2.Response:
            return _mock_transport_response(404, {"error": "not found"})

        rest_client = RestClient("https://example.com/api", transport=httpx2.MockTransport(handler))
        with reserve_stdout():
            exit_code = run(CliTestClient, ["widgets", "get-widget", "--widget-id", "1"], rest_client=rest_client)
        out, err = capsys.readouterr()
        assert exit_code == 1
        assert out == ""
        err = remove_color_code(err)
        assert "response: 404" in err
        assert "status_code:" in err
        assert "error: HTTP 404" not in err

    def test_successful_call_writes_nothing_at_all_to_stderr(self, capsys: CaptureFixture[str]) -> None:
        """Test that a successful call with --output json and no --quiet produces no request/response
        lines, console summary, or error: line on stderr at all
        """

        def handler(request: httpx2.Request) -> httpx2.Response:
            return _mock_transport_response(200, {"id": 1})

        rest_client = RestClient("https://example.com/api", transport=httpx2.MockTransport(handler))
        with reserve_stdout():
            exit_code = run(
                CliTestClient,
                ["widgets", "get-widget", "--widget-id", "1", "--output", "json"],
                rest_client=rest_client,
            )
        out, err = capsys.readouterr()
        assert exit_code == 0
        assert out == '{"id": 1}\n'
        assert err == ""

    def test_log_level_does_not_reopen_suppressed_logs(self, capsys: CaptureFixture[str]) -> None:
        """Test that --output json --log-level DEBUG still writes only the error: line: an explicit
        --log-level is not an escape hatch from --output json's log suppression
        """

        def handler(request: httpx2.Request) -> httpx2.Response:
            return _mock_transport_response(404, {"error": "not found"})

        rest_client = RestClient("https://example.com/api", transport=httpx2.MockTransport(handler))
        with reserve_stdout():
            exit_code = run(
                CliTestClient,
                ["widgets", "get-widget", "--widget-id", "1", "--output", "json", "--log-level", "DEBUG"],
                rest_client=rest_client,
            )
        out, err = capsys.readouterr()
        assert exit_code == 1
        assert out == '{"error": "not found"}\n'
        error_line = remove_color_code(err)
        assert error_line.startswith("error: GET https://example.com/api/widgets/1 - 404 Not Found (request_id: ")
        assert error_line.endswith(")\n")

    def test_with_repeat_failures_are_summarized_as_one_error_line(self, capsys: CaptureFixture[str]) -> None:
        """Test that --with-repeat 2 --output json against one success and one failure writes the full
        JSON array to stdout and summarizes the failing item as a single error: line on stderr, rather
        than one line per item
        """
        statuses = iter([200, 500])

        def handler(request: httpx2.Request) -> httpx2.Response:
            status = next(statuses)
            body = {"id": 1} if status == 200 else {"error": "simulated failure"}
            return _mock_transport_response(status, body)

        rest_client = RestClient("https://example.com/api", transport=httpx2.MockTransport(handler))
        with reserve_stdout():
            exit_code = run(
                CliTestClient,
                ["widgets", "get-widget", "--widget-id", "1", "--with-repeat", "2", "--output", "json"],
                rest_client=rest_client,
            )
        out, err = capsys.readouterr()
        assert exit_code == 1
        assert out == '[{"id": 1}, {"error": "simulated failure"}]\n'
        assert (
            remove_color_code(err)
            == "error: GET https://example.com/api/widgets/1 - 1 of 2 calls failed: 500 Internal Server Error\n"
        )


class TestRequestLine:
    """Tests for `_request_line()`, the `<METHOD> <url>` prefix `_write_failure_summary()` puts ahead of a
    `with_repeat()`/`with_concurrency()` failure summary
    """

    def test_reads_the_request_off_a_rest_response(self, mocker: MockerFixture) -> None:
        """Test that a plain `RestResponse` item's own `.request` is used"""
        response = make_rest_response(mocker, 500)
        assert _request_line([response]) == f"GET {response.request.url}"

    def test_reads_the_request_off_a_captured_http_status_error(self, mocker: MockerFixture) -> None:
        """Test that a captured `HTTPStatusError` (from a `raise_on_error` client under
        `return_exceptions=True`) still yields a request line via `item.response.request`.

        Regression test: `get_request_from_exception()` only finds a request attached by the REST client's
        own `send()`. An `HTTPStatusError` raised by `RestResponse.raise_for_status()` after a response was
        already received never goes through `send()`'s exception path, so it never carries that attribute -
        `_request_line()` used to return `None` for a result made up entirely of such items.
        """
        response = make_httpx_response(mocker, 500)
        error = httpx2.HTTPStatusError("simulated failure", request=response.request, response=response)
        assert _request_line([error]) == f"GET {response.request.url}"

    def test_returns_none_when_no_item_carries_a_request(self) -> None:
        """Test that a result made up entirely of exceptions with no attached request (every call failed
        before a request could even be sent) returns `None` rather than raising
        """
        assert _request_line([ValueError("boom"), RuntimeError("also boom")]) is None

    def test_returns_the_first_request_found_in_a_mixed_result(self, mocker: MockerFixture) -> None:
        """Test that the first item carrying a request wins, regardless of its own kind"""
        response = make_httpx_response(mocker, 500)
        error = httpx2.HTTPStatusError("simulated failure", request=response.request, response=response)
        assert _request_line([ValueError("boom"), error]) == f"GET {response.request.url}"


class TestCallWrapperFlags:
    """Tests for dispatching a call through one or more `with_xxx()` call wrapper flags.

    These mock `Client.request` (real end-to-end dispatch) rather than `Endpoint._call`, since a
    wrapper flag routes the call through the bound `EndpointFunc` directly, bypassing the `Endpoint`
    facade entirely.
    """

    def test_no_wrapper_flags_still_uses_the_endpoint_facade(self, mocker: MockerFixture) -> None:
        """Test that omitting every wrapper flag still dispatches through the plain Endpoint facade
        (Endpoint._call), unaffected by the new wrapper-aware dispatch path
        """
        mock_call = mocker.patch.object(Endpoint, "_call", return_value=make_rest_response(mocker, 200))
        exit_code = run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1"],
            rest_client=RestClient("https://example.com/api"),
        )
        mock_call.assert_called_once()
        assert exit_code == 0

    def test_with_repeat_issues_the_given_number_of_requests(self, mocker: MockerFixture) -> None:
        """Test that --with-repeat N issues N sequential requests and exits 0 when all succeed"""
        mock_request = mocker.patch.object(Client, "request", return_value=make_httpx_response(mocker, 200))
        exit_code = run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1", "--with-repeat", "3", "--quiet"],
            rest_client=RestClient("https://example.com/api"),
        )
        assert mock_request.call_count == 3
        assert exit_code == 0

    def test_positional_only_path_param_via_wrapper_flag(
        self, mocker: MockerFixture, posonly_client_class: type[PositionalOnlyClient]
    ) -> None:
        """Test that a positional-only path param also resolves when dispatched through the with_xxx()
        wrapper path (apply_wrappers()), not just the plain Endpoint facade
        """
        mock_request = mocker.patch.object(Client, "request", return_value=make_httpx_response(mocker, 200))
        exit_code = run(
            posonly_client_class,
            ["items", "get-item", "--item-id", "7", "--with-repeat", "2", "--quiet"],
            rest_client=RestClient("https://example.com/api"),
        )
        assert exit_code == 0
        assert mock_request.call_count == 2
        assert mock_request.call_args.args == ("GET", "/items/7")

    def test_with_repeat_exits_nonzero_when_any_call_fails(self, mocker: MockerFixture) -> None:
        """Test that the process exits 1 when at least one call in a --with-repeat group is non-2xx,
        even though the others succeeded
        """
        mocker.patch.object(
            Client,
            "request",
            side_effect=[
                make_httpx_response(mocker, 200),
                make_httpx_response(mocker, 500),
                make_httpx_response(mocker, 200),
            ],
        )
        exit_code = run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1", "--with-repeat", "3", "--quiet"],
            rest_client=RestClient("https://example.com/api"),
        )
        assert exit_code == 1

    def test_with_repeat_and_expected_status_passes_when_every_call_matches(self, mocker: MockerFixture) -> None:
        """Test that combining --with-repeat and --with-expected-status exits 0 when every call in the
        group returns a matching non-2xx status, mirroring the single-call case in the list branch of
        _exit_code()
        """
        mocker.patch.object(Client, "request", return_value=make_httpx_response(mocker, 404))
        exit_code = run(
            CliTestClient,
            [
                "widgets",
                "get-widget",
                "--widget-id",
                "1",
                "--with-expected-status",
                "404",
                "--with-repeat",
                "3",
                "--quiet",
            ],
            rest_client=RestClient("https://example.com/api"),
        )
        assert exit_code == 0

    def test_with_concurrency_issues_the_given_number_of_requests(self, mocker: MockerFixture) -> None:
        """Test that --with-concurrency num=N issues N concurrent requests and exits 0 when all succeed"""
        mock_request = mocker.patch.object(Client, "request", return_value=make_httpx_response(mocker, 200))
        exit_code = run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1", "--with-concurrency", "num=5,max_connections=2", "--quiet"],
            rest_client=RestClient("https://example.com/api"),
        )
        assert mock_request.call_count == 5
        assert exit_code == 0

    def test_with_expected_status_passes_when_the_response_matches(self, mocker: MockerFixture) -> None:
        """Test that --with-expected-status exits 0 when the response status is among the given codes"""
        mocker.patch.object(Client, "request", return_value=make_httpx_response(mocker, 200))
        exit_code = run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1", "--with-expected-status", "200", "--quiet"],
            rest_client=RestClient("https://example.com/api"),
        )
        assert exit_code == 0

    def test_with_expected_status_passes_when_the_response_is_a_matching_non_2xx_status(
        self, mocker: MockerFixture
    ) -> None:
        """Test that --with-expected-status exits 0 for a non-2xx response whose status is among the
        given codes, not just a 2xx one: the assertion having passed (no AssertionError raised) is what
        determines success, not RestResponse.ok
        """
        mocker.patch.object(Client, "request", return_value=make_httpx_response(mocker, 404))
        exit_code = run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1", "--with-expected-status", "404", "--quiet"],
            rest_client=RestClient("https://example.com/api"),
        )
        assert exit_code == 0

    def test_with_expected_status_fails_when_the_response_does_not_match(self, mocker: MockerFixture) -> None:
        """Test that a status mismatch raises AssertionError, caught and reported as a clean error
        with exit code 1, rather than an uncaught traceback
        """
        mocker.patch.object(Client, "request", return_value=make_httpx_response(mocker, 200))
        exit_code = run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1", "--with-expected-status", "404", "--quiet"],
            rest_client=RestClient("https://example.com/api"),
        )
        assert exit_code == 1

    def test_with_retry_retries_until_success(self, mocker: MockerFixture) -> None:
        """Test that --with-retry retries a failing call and succeeds once a later attempt returns 2xx"""
        mocker.patch.object(
            Client,
            "request",
            side_effect=[make_httpx_response(mocker, 500), make_httpx_response(mocker, 200)],
        )
        exit_code = run(
            CliTestClient,
            [
                "widgets",
                "get-widget",
                "--widget-id",
                "1",
                "--with-retry",
                "condition=500,num_retries=1,retry_after=0",
                "--quiet",
            ],
            rest_client=RestClient("https://example.com/api"),
        )
        assert exit_code == 0

    def test_with_lock_dispatches_successfully(self, mocker: MockerFixture) -> None:
        """Test that --with-lock wires through to a real distributed lock without error"""
        mocker.patch.object(Client, "request", return_value=make_httpx_response(mocker, 200))
        exit_code = run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1", "--with-lock", "--quiet"],
            rest_client=RestClient("https://example.com/api"),
        )
        assert exit_code == 0

    def test_invalid_wrapper_spec_exits_with_a_usage_error_rather_than_a_call_failure(
        self, mocker: MockerFixture, capsys: CaptureFixture[str]
    ) -> None:
        """Test that a wrapper flag whose spec fails to construct the wrapper itself (e.g. --with-rate-limit
        given only interval=, with no max_requests) is reported as a usage error with exit code 2, the same
        as a client construction failure, rather than dispatched as if the call itself had failed (exit code
        1). No request should reach the transport at all
        """
        mock_request = mocker.patch.object(Client, "request")
        exit_code = run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1", "--with-rate-limit", "interval=2", "--quiet"],
            rest_client=RestClient("https://example.com/api"),
        )
        assert exit_code == 2
        assert "error: ValueError: Either `max_requests` or `limiter` must be provided" in capsys.readouterr().err
        mock_request.assert_not_called()

    def test_a_terminal_flag_given_before_another_wrapper_flag_exits_with_a_usage_error(
        self, mocker: MockerFixture, capsys: CaptureFixture[str]
    ) -> None:
        """Test that a terminal wrapper flag (--with-repeat/--with-concurrency) given anywhere but last
        is reported as a usage error with exit code 2, the same as the equivalent Python chain
        `.with_repeat(3).with_retry()` raising RuntimeError. No request should reach the transport
        """
        mock_request = mocker.patch.object(Client, "request")
        exit_code = run(
            CliTestClient,
            ["widgets", "get-widget", "--widget-id", "1", "--with-repeat", "3", "--with-retry", "--quiet"],
            rest_client=RestClient("https://example.com/api"),
        )
        assert exit_code == 2
        assert "terminal and must always be the last wrapper in a chain" in capsys.readouterr().err
        mock_request.assert_not_called()

    def test_a_repeated_with_expected_status_stacks_as_a_narrowing_assertion(self, mocker: MockerFixture) -> None:
        """Test that giving --with-expected-status more than once chains two independent status
        assertions, matching `.with_expected_status(a).with_expected_status(b)` in Python: the
        response's status must satisfy every occurrence, not just the last one. A status accepted by
        every occurrence still exits 0
        """
        mocker.patch.object(Client, "request", return_value=make_httpx_response(mocker, 200))
        exit_code = run(
            CliTestClient,
            [
                "widgets",
                "get-widget",
                "--widget-id",
                "1",
                "--with-expected-status",
                "200",
                "500",
                "--with-expected-status",
                "200",
                "--quiet",
            ],
            rest_client=RestClient("https://example.com/api"),
        )
        assert exit_code == 0

    def test_a_repeated_with_expected_status_with_disjoint_codes_fails_the_call(
        self, mocker: MockerFixture, capsys: CaptureFixture[str]
    ) -> None:
        """Test that stacked --with-expected-status occurrences with no overlap in what they'd accept
        for the actual response fail the call (exit code 1), since the response can't satisfy both
        assertions at once
        """
        mocker.patch.object(Client, "request", return_value=make_httpx_response(mocker, 200))
        exit_code = run(
            CliTestClient,
            [
                "widgets",
                "get-widget",
                "--widget-id",
                "1",
                "--with-expected-status",
                "404",
                "--with-expected-status",
                "200",
                "--quiet",
            ],
            rest_client=RestClient("https://example.com/api"),
        )
        assert exit_code == 1
        assert "Expected status code 404, but got 200" in capsys.readouterr().err

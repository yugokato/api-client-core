"""Unit tests for `api_client_core.cli.builder`"""

from __future__ import annotations

import argparse
import io
import json
import sys
from enum import Enum
from functools import cached_property
from pathlib import Path
from typing import Any, Literal, Unpack

import pytest
from common_libs.ansi_colors import ColorCodes, remove_color_code
from pytest import CaptureFixture
from pytest_mock import MockerFixture

from api_client_core import APIClient, BaseAPI, __version__, endpoint
from api_client_core.cli._cache import mark_completion_registered
from api_client_core.cli._constants import PROG, Flag, WrapperFlag
from api_client_core.cli.builder import (
    _OPTIONS_GROUP_TITLE,
    _TAB_COMPLETION_REGISTER_TIP,
    _TAB_COMPLETION_TIP,
    _compact_usage,
    _format_action_usage,
    _parse_header,
    _tab_completion_tips,
    _to_kebab_case,
    build_client_parser,
    build_completion_entry,
    build_completion_tree,
    build_initial_parser,
)
from api_client_core.cli.builder import _generate_description as real_generate_description
from api_client_core.cli.builder import add_endpoint_arguments as real_add_endpoint_arguments
from api_client_core.cli.discovery import DiscoveryResult
from api_client_core.cli.params import _PARAMS_GROUP_TITLE, accepts_json_file, reset_stdin_state
from api_client_core.cli.wrappers import (
    _WRAPPERS_GROUP_DESCRIPTION,
    _WRAPPERS_GROUP_SHORT_DESCRIPTION,
    _WRAPPERS_GROUP_TITLE,
)
from api_client_core.types import Kwargs, RestResponse

from .conftest import (
    CliTestClient,
    WidgetsAPI,
    find_group_title,
    get_subparsers_action,
    module_scoped,
    patch_argcomplete_installed,
)


class TestBuildClientParser:
    """Tests for `build_client_parser()`"""

    def test_builds_a_resource_per_discovered_api_class(self, cli_client_class: type[CliTestClient]) -> None:
        """Test that each discovered API class becomes a resource"""
        parser = build_client_parser(cli_client_class)
        resource_subparsers = get_subparsers_action(parser)
        assert set(resource_subparsers.choices) == {"widgets", "gadgets"}

    @pytest.mark.parametrize(("installed", "expected_tips"), [(False, (_TAB_COMPLETION_TIP,)), (True, ())])
    def test_tips_reflect_whether_argcomplete_is_installed(
        self,
        installed: bool,
        expected_tips: list[str],
        mocker: MockerFixture,
        cli_client_class: type[CliTestClient],
        cache_home: Path,
    ) -> None:
        """Test that the generated parser's `tips` surface the tab-completion setup tip when
        `argcomplete` isn't installed, so it shows up in the client's own `--help` output, and omit
        it once `argcomplete` is installed and a completion request has already been served (fully set
        up), since there's nothing left for the tip to cover. Checked at every level (client, resource,
        leaf command), since `tips=` is threaded through `add_parser()`'s kwargs forwarding at each one
        """
        patch_argcomplete_installed(mocker, installed=installed)
        if installed:
            mark_completion_registered()
        parser = build_client_parser(cli_client_class)
        assert parser.tips == expected_tips

        resource_parser = get_subparsers_action(parser).choices["widgets"]
        assert resource_parser.tips == expected_tips

        command_parser = get_subparsers_action(resource_parser).choices["get-widget"]
        assert command_parser.tips == expected_tips

    def test_description_names_the_class_when_the_client_class_has_no_docstring(self) -> None:
        """Test that a client class with no docstring still gets a description naming the class, rather
        than argparse's default of showing 'None'
        """

        class NoDocClient(CliTestClient):
            pass

        parser = build_client_parser(NoDocClient)
        assert parser.description is not None
        assert "NoDocClient" in parser.description

    def test_builds_a_kebab_case_command_per_endpoint(self, cli_client_class: type[CliTestClient]) -> None:
        """Test that each endpoint becomes a kebab-case subcommand under its resource"""
        parser = build_client_parser(cli_client_class)
        widgets_parser = get_subparsers_action(parser).choices["widgets"]
        command_subparsers = get_subparsers_action(widgets_parser)
        assert set(command_subparsers.choices) == {"get-widget", "create-widget", "upload-avatar", "list-widgets"}

    def test_an_endpoint_inherited_from_a_base_api_class_still_becomes_a_command(self) -> None:
        """Test that a resource whose API class declares no endpoints of its own, only inheriting one from
        a base `BaseAPI` subclass, still gets a command for it, rather than the resource being silently
        discarded as exposing no commands
        """

        class SharedBaseAPI(BaseAPI):
            app_name = "inherited-endpoint-test"

            @endpoint.get("/shared")
            def get_shared(self, **kwargs: Unpack[Kwargs]) -> RestResponse:
                """Get the shared thing"""
                ...

        @module_scoped
        class InheritedAPI(SharedBaseAPI):
            """An API class exposing no endpoints of its own, only one inherited from its base class"""

        class InheritedEndpointClient(APIClient):
            app_name = "inherited-endpoint-test"

            @cached_property
            def things(self) -> InheritedAPI:
                return InheritedAPI(self)

        parser = build_client_parser(InheritedEndpointClient)
        resource_subparsers = get_subparsers_action(parser)
        assert "things" in resource_subparsers.choices
        command_subparsers = get_subparsers_action(resource_subparsers.choices["things"])
        assert "get-shared" in command_subparsers.choices

    def test_a_resource_attribute_name_with_underscores_becomes_a_kebab_case_resource(self) -> None:
        """Test that a resource whose attribute name contains underscores (e.g. `user_profiles`) is
        exposed as a kebab-case resource (`user-profiles`), matching the kebab-casing already applied to
        endpoint commands, rather than mixing two different naming conventions on one command line
        """

        @module_scoped
        class UserProfilesAPI(BaseAPI):
            """A synthetic API class exposed under an underscore-containing attribute name"""

            app_name = "underscore-resource-test"

            @endpoint.get("/profiles")
            def list_profiles(self, **kwargs: Unpack[Kwargs]) -> RestResponse:
                """List profiles"""
                ...

        class UnderscoreResourceClient(APIClient):
            app_name = "underscore-resource-test"

            @cached_property
            def user_profiles(self) -> UserProfilesAPI:
                return UserProfilesAPI(self)

        parser = build_client_parser(UnderscoreResourceClient)
        resource_subparsers = get_subparsers_action(parser)
        assert "user-profiles" in resource_subparsers.choices
        assert "user_profiles" not in resource_subparsers.choices

    def test_a_leading_underscore_endpoint_name_keeps_the_underscore_rather_than_a_leading_hyphen(self) -> None:
        """Test that an endpoint function name with a leading underscore (e.g. a generated
        `_unnamed_endpoint_1` fallback name) keeps that leading underscore rather than turning it into a
        leading hyphen. `argparse` treats any token starting with `-` as an option, not a positional value,
        so a `-unnamed-endpoint-1` command name could never actually be selected on the command line.

        Regression test: this used to produce an unselectable command name
        """

        @module_scoped
        class UnnamedEndpointAPI(BaseAPI):
            """A synthetic API class exposing an endpoint with a leading-underscore function name"""

            app_name = "underscore-command-test"

            @endpoint.get("/entries")
            def _unnamed_endpoint_1(self, **kwargs: Unpack[Kwargs]) -> RestResponse:
                """Get activity log entries"""
                ...

        class UnnamedEndpointClient(APIClient):
            app_name = "underscore-command-test"

            @cached_property
            def entries(self) -> UnnamedEndpointAPI:
                return UnnamedEndpointAPI(self)

        parser = build_client_parser(UnnamedEndpointClient)
        entries_parser = get_subparsers_action(parser).choices["entries"]
        command_subparsers = get_subparsers_action(entries_parser)
        assert "_unnamed-endpoint-1" in command_subparsers.choices
        assert not any(name.startswith("-") for name in command_subparsers.choices)

        args = parser.parse_args(["entries", "_unnamed-endpoint-1"])
        assert str(args._endpoint) == "GET /entries"

    def test_a_leading_underscore_name_does_not_collide_with_its_unprefixed_sibling(self) -> None:
        """Test that `_get_widget` and `get_widget` on the same API class produce distinct commands
        (`_get-widget` and `get-widget`) rather than both kebab-casing to `get-widget`.

        Regression test: stripping the leading underscore instead of preserving it collapsed both names
        onto the same command, which crashes `build_client_parser()` outright (`argparse.add_parser()`
        raises `ArgumentError` on a duplicate subcommand name)
        """

        @module_scoped
        class CollidingNamesAPI(BaseAPI):
            """A synthetic API class with an underscore-prefixed endpoint and its unprefixed sibling"""

            app_name = "collision-command-test"

            @endpoint.get("/widgets/{widget_id}/internal")
            def _get_widget(self, widget_id: int, **kwargs: Unpack[Kwargs]) -> RestResponse:
                """Get a widget (internal fallback name)"""
                ...

            @endpoint.get("/widgets/{widget_id}")
            def get_widget(self, widget_id: int, **kwargs: Unpack[Kwargs]) -> RestResponse:
                """Get a widget by ID"""
                ...

        class CollidingNamesClient(APIClient):
            app_name = "collision-command-test"

            @cached_property
            def widgets(self) -> CollidingNamesAPI:
                return CollidingNamesAPI(self)

        parser = build_client_parser(CollidingNamesClient)
        widgets_parser = get_subparsers_action(parser).choices["widgets"]
        command_subparsers = get_subparsers_action(widgets_parser)
        assert set(command_subparsers.choices) == {"_get-widget", "get-widget"}

    def test_command_description_is_the_endpoint_docstring(self, cli_client_class: type[CliTestClient]) -> None:
        """Test that a command's description includes the endpoint and its original function's docstring"""
        parser = build_client_parser(cli_client_class)
        widgets_parser = get_subparsers_action(parser).choices["widgets"]
        get_widget_parser = get_subparsers_action(widgets_parser).choices["get-widget"]
        assert get_widget_parser.description is not None
        assert "GET /widgets/{widget_id}" in get_widget_parser.description
        assert "Get a widget by ID" in get_widget_parser.description

    def test_deprecated_endpoint_is_flagged_in_help(self, cli_client_class: type[CliTestClient]) -> None:
        """Test that a deprecated endpoint's command help is marked (deprecated)"""
        parser = build_client_parser(cli_client_class)
        widgets_parser = get_subparsers_action(parser).choices["widgets"]
        command_subparsers = get_subparsers_action(widgets_parser)
        list_widgets_action = next(a for a in command_subparsers._choices_actions if a.dest == "list-widgets")
        assert "(deprecated)" in list_widgets_action.help

    def test_command_description_omits_param_docs_shown_on_each_flag_instead(
        self, cli_client_class: type[CliTestClient]
    ) -> None:
        """Test that a command's boxed description shows its endpoint docstring's summary but leaves out
        any `:param <name>: ...` entries, since each one is already shown as that parameter's own flag
        `help=` (see `TestParamDescriptionInHelp` in `test_params.py`) and showing both would duplicate the
        same text twice under `--help`
        """

        @module_scoped
        class DocsAPI(BaseAPI):
            app_name = "docs-test"

            @endpoint.post("/things")
            def make_thing(self, name: str, **kwargs: Unpack[Kwargs]) -> RestResponse:
                """Make a thing

                :param name: The thing's own display name
                """
                ...

        class DocsClient(APIClient):
            app_name = "docs-test"

            @cached_property
            def things(self) -> DocsAPI:
                return DocsAPI(self)

        parser = build_client_parser(DocsClient)
        things_parser = get_subparsers_action(parser).choices["things"]
        make_thing_parser = get_subparsers_action(things_parser).choices["make-thing"]
        assert make_thing_parser.description is not None
        assert "Make a thing" in make_thing_parser.description
        assert ":param" not in make_thing_parser.description
        assert ":param" not in remove_color_code(make_thing_parser.format_help())

    def test_command_description_keeps_prose_that_trails_the_param_block(
        self, cli_client_class: type[CliTestClient]
    ) -> None:
        """Test that prose following an endpoint's `:param` block still shows in the boxed description,
        so only the `:param` entries themselves are left out, not any other part of the docstring
        """

        @module_scoped
        class DocsAPI(BaseAPI):
            app_name = "docs-test"

            @endpoint.post("/things")
            def make_thing(self, name: str, **kwargs: Unpack[Kwargs]) -> RestResponse:
                """Make a thing

                :param name: The thing's own display name

                See the API docs for the full response shape.
                """
                ...

        class DocsClient(APIClient):
            app_name = "docs-test"

            @cached_property
            def things(self) -> DocsAPI:
                return DocsAPI(self)

        parser = build_client_parser(DocsClient)
        things_parser = get_subparsers_action(parser).choices["things"]
        make_thing_parser = get_subparsers_action(things_parser).choices["make-thing"]
        assert make_thing_parser.description is not None
        assert "See the API docs for the full response shape." in make_thing_parser.description

    def test_a_params_only_docstring_boxes_a_bare_title_with_no_dangling_colon(
        self, cli_client_class: type[CliTestClient]
    ) -> None:
        """Test that an endpoint documenting only its parameters, with no summary or other prose, boxes a
        bare title line rather than a title followed by a dangling colon and an empty body
        """

        @module_scoped
        class DocsAPI(BaseAPI):
            app_name = "docs-test"

            @endpoint.post("/things")
            def make_thing(self, name: str, **kwargs: Unpack[Kwargs]) -> RestResponse:
                """:param name: The thing's own display name"""
                ...

        class DocsClient(APIClient):
            app_name = "docs-test"

            @cached_property
            def things(self) -> DocsAPI:
                return DocsAPI(self)

        parser = build_client_parser(DocsClient)
        things_parser = get_subparsers_action(parser).choices["things"]
        make_thing_parser = get_subparsers_action(things_parser).choices["make-thing"]
        assert make_thing_parser.description is not None
        title = str(DocsAPI.make_thing.endpoint)
        assert title in make_thing_parser.description
        assert f"{title}:" not in make_thing_parser.description

    def test_leaf_command_binds_its_endpoint(self, cli_client_class: type[CliTestClient]) -> None:
        """Test that parsing a command resolves the correct Endpoint via _endpoint"""
        parser = build_client_parser(cli_client_class)
        args = parser.parse_args(["widgets", "get-widget", "--widget-id", "1"])
        assert str(args._endpoint) == "GET /widgets/{widget_id}"

    def test_commands_are_listed_in_a_stable_alphabetical_order(
        self, cli_client_class: type[CliTestClient], widgets_api_class: type[WidgetsAPI]
    ) -> None:
        """Test that a resource's commands are sorted by name regardless of the order `endpoints_for()`
        returns them in, so `--help` output doesn't depend on whether `BaseAPI.init()` already
        populated `api_class.endpoints` in declaration order
        """
        widgets_api_class.endpoints = [
            widgets_api_class.list_widgets.endpoint,
            widgets_api_class.get_widget.endpoint,
            widgets_api_class.create_widget.endpoint,
            widgets_api_class.upload_avatar.endpoint,
        ]
        try:
            parser = build_client_parser(cli_client_class)
            widgets_parser = get_subparsers_action(parser).choices["widgets"]
            command_names = list(get_subparsers_action(widgets_parser).choices)
            assert command_names == sorted(command_names)
        finally:
            widgets_api_class.endpoints = None

    def test_commands_are_listed_in_natural_sort_order(self) -> None:
        """Test that a numeric suffix in command names sorts numerically (`...-2` before `...-10`), not
        lexicographically (`...-10` before `...-2`), matching how generated names like
        `unnamed-endpoint-N` read
        """

        @module_scoped
        class NumberedAPI(BaseAPI):
            """A synthetic API class with numerically suffixed endpoint names"""

            app_name = "natural-sort-test"

            @endpoint.get("/n1")
            def unnamed_endpoint_1(self, **kwargs: Unpack[Kwargs]) -> RestResponse:
                """First endpoint"""
                ...

            @endpoint.get("/n2")
            def unnamed_endpoint_2(self, **kwargs: Unpack[Kwargs]) -> RestResponse:
                """Second endpoint"""
                ...

            @endpoint.get("/n10")
            def unnamed_endpoint_10(self, **kwargs: Unpack[Kwargs]) -> RestResponse:
                """Tenth endpoint"""
                ...

        class NumberedClient(APIClient):
            app_name = "natural-sort-test"

            @cached_property
            def numbered(self) -> NumberedAPI:
                return NumberedAPI(self)

        parser = build_client_parser(NumberedClient)
        numbered_parser = get_subparsers_action(parser).choices["numbered"]
        command_names = list(get_subparsers_action(numbered_parser).choices)
        assert command_names == ["unnamed-endpoint-1", "unnamed-endpoint-2", "unnamed-endpoint-10"]

    def test_raises_when_no_api_classes_discovered(self) -> None:
        """Test that build_parser() raises when the client exposes no discoverable API classes, naming the
        actual discovery contract (a @cached_property/@property returning a BaseAPI subclass) rather than
        only pointing at a possible import failure, since a resource assigned as a plain instance attribute
        (e.g. in __init__) hits this same error with nothing for --log-level DEBUG to explain
        """

        class EmptyClient(APIClient):
            app_name = "empty"

        with pytest.raises(RuntimeError, match="No API classes discovered") as exc_info:
            build_client_parser(EmptyClient)
        assert "@cached_property" in str(exc_info.value)

    def test_resource_subparsers_use_a_metavar_rather_than_the_internal_dest_name(
        self, cli_client_class: type[CliTestClient]
    ) -> None:
        """Test that the resource subparsers action displays as `<resource-group>` rather than argparse's
        raw `_resource` dest, which would otherwise leak an internal, underscore-prefixed name into help and
        usage output (`_resource`/`_command` aren't required - see `TestIncompleteCommand` in
        `test_runner.py` for the help shown when one is omitted - so this is a static fact about the
        registered action, not something an error message needs to avoid leaking)
        """
        parser = build_client_parser(cli_client_class)
        resource_subparsers = get_subparsers_action(parser)
        assert resource_subparsers.dest == "_resource"
        assert resource_subparsers.metavar == "<resource-group>"

    def test_command_subparsers_use_a_metavar_rather_than_the_internal_dest_name(
        self, cli_client_class: type[CliTestClient]
    ) -> None:
        """Test that a resource's command subparsers action displays as `<command>` rather than argparse's
        raw `_command` dest, mirroring `test_resource_subparsers_use_a_metavar_rather_than_the_internal_dest_name`
        one level down
        """
        parser = build_client_parser(cli_client_class)
        widgets_parser = get_subparsers_action(parser).choices["widgets"]
        command_subparsers = get_subparsers_action(widgets_parser)
        assert command_subparsers.dest == "_command"
        assert command_subparsers.metavar == "<command>"

    def test_parser_error_is_colored_red(
        self, cli_client_class: type[CliTestClient], capsys: CaptureFixture[str], force_color: None
    ) -> None:
        """Test that a parser usage error is printed to stderr with the error line wrapped in red, while
        the usage block printed above it is colored separately (never red)
        """
        parser = build_client_parser(cli_client_class)
        with pytest.raises(SystemExit):
            parser.parse_args(["widgets", "get-widget"])
        usage_block, _, colored_error = capsys.readouterr().err.partition(ColorCodes.RED)
        assert remove_color_code(usage_block).startswith("usage:")
        assert ColorCodes.RED not in usage_block
        assert colored_error.startswith("error:")
        assert colored_error.rstrip().endswith(ColorCodes.DEFAULT)

    def test_a_reserved_flag_collision_drops_only_that_parameter_and_logs_a_debug_message(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that one endpoint parameter colliding with a reserved control kwarg (`quiet`, whose flag
        collision alone would otherwise be fixed by aliasing, see `_RESERVED_CALL_KWARGS`) is dropped from
        that command rather than the whole command being skipped, mirroring how a direct Python call's own
        `quiet` keyword always binds to the endpoint func's control parameter of that name rather than to a
        same-named parameter of the wrapped endpoint function. The command still builds, keeps its other
        parameters, and the reserved `--quiet` control flag still governs verbosity. Logged at DEBUG, not
        WARNING, since parser building (and so this diagnostic) reruns on every invocation of any command
        on the same client
        """

        @module_scoped
        class CollisionAPI(BaseAPI):
            """A synthetic API class with one parameter colliding with a reserved CLI flag"""

            app_name = "collision-test"

            @endpoint.post("/things")
            def make_thing(self, name: str, quiet: str = "ok", **kwargs: Unpack[Kwargs]) -> RestResponse:
                """An endpoint with one parameter colliding with --quiet"""
                ...

        class CollisionClient(APIClient):
            app_name = "collision-test"

            @cached_property
            def things(self) -> CollisionAPI:
                return CollisionAPI(self)

        with caplog.at_level("DEBUG", logger="api_client_core.cli.params"):
            parser = build_client_parser(CollisionClient)

        things_parser = get_subparsers_action(parser).choices["things"]
        make_thing_parser = get_subparsers_action(things_parser).choices["make-thing"]
        assert find_group_title(make_thing_parser, "--name") == _PARAMS_GROUP_TITLE
        assert find_group_title(make_thing_parser, "--quiet") == _OPTIONS_GROUP_TITLE
        assert "CollisionAPI.make_thing" in caplog.text
        assert "quiet" in caplog.text

    def test_percent_in_help_text_does_not_break_help_rendering(self, capsys: CaptureFixture[str]) -> None:
        """Test that a literal `%` in an endpoint's docstring or a parameter's default value doesn't
        crash `--help` rendering. `argparse` `%`-expands every action's help string, so an unescaped `%`
        sourced from user code (not part of a `%(...)s`-style placeholder) would otherwise raise
        `TypeError` once `--help` is rendered
        """

        @module_scoped
        class DiscountAPI(BaseAPI):
            """A synthetic API class exercising a literal `%` in help text"""

            app_name = "percent-test"

            @endpoint.get("/discount")
            def get_discount(self, rate: str = "50% off", **kwargs: Unpack[Kwargs]) -> RestResponse:
                """Get a discount of 50% off"""
                ...

        class DiscountClient(APIClient):
            app_name = "percent-test"

            @cached_property
            def discounts(self) -> DiscountAPI:
                return DiscountAPI(self)

        parser = build_client_parser(DiscountClient)
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["discounts", "get-discount", "--help"])
        assert exc_info.value.code == 0
        assert "50% off" in capsys.readouterr().out

    def test_a_broken_command_is_skipped_without_taking_down_its_resources_other_commands(
        self, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that an unexpected error while building one leaf command's arguments (e.g. an unforeseen
        bug, not one of the reserved/duplicate-flag collisions params.py already handles) is caught,
        logged, and the half-built command discarded, rather than taking down the whole client's CLI
        because of that one broken endpoint
        """

        @module_scoped
        class TwoCommandsAPI(BaseAPI):
            """A synthetic API class with one endpoint whose arguments fail to build"""

            app_name = "broken-cmd-test"

            @endpoint.get("/ok")
            def get_ok(self, **kwargs: Unpack[Kwargs]) -> RestResponse:
                """A healthy endpoint"""
                ...

            @endpoint.get("/broken")
            def get_broken(self, **kwargs: Unpack[Kwargs]) -> RestResponse:
                """An endpoint whose arguments fail to build"""
                ...

        class TwoCommandsClient(APIClient):
            app_name = "broken-cmd-test"

            @cached_property
            def things(self) -> TwoCommandsAPI:
                return TwoCommandsAPI(self)

        def flaky_add_endpoint_arguments(parser: Any, ep: Any) -> None:
            if ep.func_name == "get_broken":
                raise RuntimeError("simulated failure")
            real_add_endpoint_arguments(parser, ep)

        mocker.patch("api_client_core.cli.builder.add_endpoint_arguments", side_effect=flaky_add_endpoint_arguments)

        with caplog.at_level("WARNING", logger="api_client_core.cli.builder"):
            parser = build_client_parser(TwoCommandsClient)

        things_parser = get_subparsers_action(parser).choices["things"]
        commands = get_subparsers_action(things_parser)
        assert "get-ok" in commands.choices
        assert "get-broken" not in commands.choices
        assert "get-broken" not in things_parser.format_help()
        assert "get-broken" in caplog.text

    def test_a_command_whose_description_fails_to_build_is_skipped_without_taking_down_its_resources_other_commands(
        self, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that an unexpected error while building one leaf command's own `description=` (e.g. a
        pathological docstring) is caught, logged, and the half-built command discarded, the same as any
        other per-command build failure, rather than taking down the whole client's CLI. Unlike
        `add_endpoint_arguments()`, `_generate_description()`'s result feeds directly into `add_parser()`
        itself, ahead of everything else the per-command `try` covers, so it needs its own coverage
        """

        @module_scoped
        class TwoCommandsAPI(BaseAPI):
            """A synthetic API class with one endpoint whose description fails to build"""

            app_name = "broken-description-test"

            @endpoint.get("/ok")
            def get_ok(self, **kwargs: Unpack[Kwargs]) -> RestResponse:
                """A healthy endpoint"""
                ...

            @endpoint.get("/broken")
            def get_broken(self, **kwargs: Unpack[Kwargs]) -> RestResponse:
                """An endpoint whose description fails to build"""
                ...

        class TwoCommandsClient(APIClient):
            app_name = "broken-description-test"

            @cached_property
            def things(self) -> TwoCommandsAPI:
                return TwoCommandsAPI(self)

        def flaky_generate_description(obj: Any) -> str:
            if getattr(obj, "func_name", None) == "get_broken":
                raise RuntimeError("simulated failure")
            return real_generate_description(obj)

        mocker.patch("api_client_core.cli.builder._generate_description", side_effect=flaky_generate_description)

        with caplog.at_level("WARNING", logger="api_client_core.cli.builder"):
            parser = build_client_parser(TwoCommandsClient)

        things_parser = get_subparsers_action(parser).choices["things"]
        commands = get_subparsers_action(things_parser)
        assert "get-ok" in commands.choices
        assert "get-broken" not in commands.choices
        assert "get-broken" not in things_parser.format_help()
        assert "get-broken" in caplog.text

    def test_a_resource_with_no_endpoints_is_omitted(self) -> None:
        """Test that an API class exposing no endpoints is skipped entirely, rather than left behind as
        a resource whose own required `<command>` subparser has zero choices and can therefore never
        actually be selected
        """

        @module_scoped
        class EmptyAPI(BaseAPI):
            """A synthetic API class with no endpoints"""

            app_name = "empty-resource-test"

        @module_scoped
        class ThingsAPI(BaseAPI):
            """A synthetic API class with one endpoint"""

            app_name = "empty-resource-test"

            @endpoint.get("/things")
            def get_thing(self, **kwargs: Unpack[Kwargs]) -> RestResponse:
                """Get a thing"""
                ...

        class EmptyResourceClient(APIClient):
            app_name = "empty-resource-test"

            @cached_property
            def empty(self) -> EmptyAPI:
                return EmptyAPI(self)

            @cached_property
            def things(self) -> ThingsAPI:
                return ThingsAPI(self)

        parser = build_client_parser(EmptyResourceClient)
        resource_subparsers = get_subparsers_action(parser)
        assert "things" in resource_subparsers.choices
        assert "empty" not in resource_subparsers.choices

    def test_a_resource_whose_every_command_fails_to_build_is_discarded(self, mocker: MockerFixture) -> None:
        """Test that a resource is discarded entirely once every one of its commands has been skipped
        (see `test_a_broken_command_is_skipped_...` above), rather than left behind as a resource whose
        own required `<command>` subparser has zero choices
        """

        @module_scoped
        class AllBrokenAPI(BaseAPI):
            """A synthetic API class whose only endpoint fails to build"""

            app_name = "all-broken-test"

            @endpoint.get("/broken")
            def get_broken(self, **kwargs: Unpack[Kwargs]) -> RestResponse:
                """An endpoint whose arguments fail to build"""
                ...

        @module_scoped
        class ThingsAPI(BaseAPI):
            """A synthetic API class with one healthy endpoint"""

            app_name = "all-broken-test"

            @endpoint.get("/things")
            def get_thing(self, **kwargs: Unpack[Kwargs]) -> RestResponse:
                """Get a thing"""
                ...

        class AllBrokenClient(APIClient):
            app_name = "all-broken-test"

            @cached_property
            def broken(self) -> AllBrokenAPI:
                return AllBrokenAPI(self)

            @cached_property
            def things(self) -> ThingsAPI:
                return ThingsAPI(self)

        def flaky_add_endpoint_arguments(parser: Any, ep: Any) -> None:
            if ep.func_name == "get_broken":
                raise RuntimeError("simulated failure")
            real_add_endpoint_arguments(parser, ep)

        mocker.patch("api_client_core.cli.builder.add_endpoint_arguments", side_effect=flaky_add_endpoint_arguments)

        parser = build_client_parser(AllBrokenClient)
        resource_subparsers = get_subparsers_action(parser)
        assert "things" in resource_subparsers.choices
        assert "broken" not in resource_subparsers.choices

    def test_raises_when_every_resource_is_empty_or_broken(self, mocker: MockerFixture) -> None:
        """Test that build_client_parser() raises the same way it does for a client with no discoverable
        API classes at all, once every discovered one is either empty or has every command fail to build:
        a required `<resource-group>` subparser with zero choices is just as unusable
        """

        @module_scoped
        class EmptyAPI(BaseAPI):
            """A synthetic API class with no endpoints"""

            app_name = "all-empty-test"

        @module_scoped
        class BrokenAPI(BaseAPI):
            """A synthetic API class whose only endpoint fails to build"""

            app_name = "all-empty-test"

            @endpoint.get("/broken")
            def get_broken(self, **kwargs: Unpack[Kwargs]) -> RestResponse:
                """An endpoint whose arguments fail to build"""
                ...

        class AllEmptyClient(APIClient):
            app_name = "all-empty-test"

            @cached_property
            def empty(self) -> EmptyAPI:
                return EmptyAPI(self)

            @cached_property
            def broken(self) -> BrokenAPI:
                return BrokenAPI(self)

        mocker.patch(
            "api_client_core.cli.builder.add_endpoint_arguments", side_effect=RuntimeError("simulated failure")
        )

        with pytest.raises(RuntimeError, match="No usable commands discovered"):
            build_client_parser(AllEmptyClient)

    def test_camelcase_attribute_and_function_names_normalize_to_kebab_case(self) -> None:
        """Test that a resource/command derived from a capitalized or camelCase name (matching
        `openapi-test-client`'s own `client.Users`-style convention) still normalizes to an idiomatic,
        lowercase kebab-case CLI token, end to end through `build_client_parser()`
        """

        @module_scoped
        class UserAccountsAPI(BaseAPI):
            """A synthetic API class exposed under a CamelCase attribute name"""

            app_name = "camel-case-test"

            @endpoint.get("/accounts/{account_id}")
            def getAccountDetails(self, account_id: int, **kwargs: Unpack[Kwargs]) -> RestResponse:
                """Get account details"""
                ...

        class CamelCaseClient(APIClient):
            app_name = "camel-case-test"

            @cached_property
            def UserAccounts(self) -> UserAccountsAPI:
                return UserAccountsAPI(self)

        parser = build_client_parser(CamelCaseClient)
        resource_subparsers = get_subparsers_action(parser)
        assert "user-accounts" in resource_subparsers.choices
        command_subparsers = get_subparsers_action(resource_subparsers.choices["user-accounts"])
        assert "get-account-details" in command_subparsers.choices

    def test_duplicate_resource_names_keep_the_first_and_warn(self, caplog: pytest.LogCaptureFixture) -> None:
        """Test that two API classes whose attribute names normalize to the same resource token (`Users`
        and `users`) don't crash `argparse.add_parser()` on a duplicate subcommand name: the first one (in
        sorted order) is kept, the second is dropped with a warning naming it
        """

        @module_scoped
        class UsersAPI(BaseAPI):
            """A synthetic API class"""

            app_name = "dup-resource-test"

            @endpoint.get("/users-upper")
            def get_from_upper(self, **kwargs: Unpack[Kwargs]) -> RestResponse:
                """From the capitalized attribute"""
                ...

        @module_scoped
        class LowerUsersAPI(BaseAPI):
            """A synthetic API class"""

            app_name = "dup-resource-test"

            @endpoint.get("/users-lower")
            def get_from_lower(self, **kwargs: Unpack[Kwargs]) -> RestResponse:
                """From the lowercase attribute"""
                ...

        class DupResourceClient(APIClient):
            app_name = "dup-resource-test"

            @cached_property
            def Users(self) -> UsersAPI:
                return UsersAPI(self)

            @cached_property
            def users(self) -> LowerUsersAPI:
                return LowerUsersAPI(self)

        with caplog.at_level("WARNING", logger="api_client_core.cli.builder"):
            parser = build_client_parser(DupResourceClient)
        resource_subparsers = get_subparsers_action(parser)
        assert list(resource_subparsers.choices) == ["users"]
        command_subparsers = get_subparsers_action(resource_subparsers.choices["users"])
        assert "get-from-upper" in command_subparsers.choices
        assert "get-from-lower" not in command_subparsers.choices
        assert "users" in caplog.text

    def test_duplicate_command_names_keep_the_first_and_warn(self, caplog: pytest.LogCaptureFixture) -> None:
        """Test that two endpoints on the same API class whose function names normalize to the same
        command token (`getUser`/`get_user`) don't crash `argparse.add_parser()`: the first one (by
        function name, sorted) is kept, the second is dropped with a warning naming it
        """

        @module_scoped
        class DupCommandAPI(BaseAPI):
            """A synthetic API class with two endpoints colliding on the same command name"""

            app_name = "dup-command-test"

            @endpoint.get("/users/camel")
            def getUser(self, **kwargs: Unpack[Kwargs]) -> RestResponse:
                """From the camelCase function name"""
                ...

            @endpoint.get("/users/snake")
            def get_user(self, **kwargs: Unpack[Kwargs]) -> RestResponse:
                """From the snake_case function name"""
                ...

        class DupCommandClient(APIClient):
            app_name = "dup-command-test"

            @cached_property
            def users(self) -> DupCommandAPI:
                return DupCommandAPI(self)

        with caplog.at_level("WARNING", logger="api_client_core.cli.builder"):
            parser = build_client_parser(DupCommandClient)
        command_subparsers = get_subparsers_action(get_subparsers_action(parser).choices["users"])
        assert list(command_subparsers.choices) == ["get-user"]
        assert command_subparsers.choices["get-user"].description is not None
        assert "camelCase" in command_subparsers.choices["get-user"].description
        assert "get_user" in caplog.text

    def test_leaf_usage_names_its_own_resource_and_omits_wrapper_and_control_flags(
        self, cli_client_class: type[CliTestClient]
    ) -> None:
        """Test that a leaf command's own usage line (built by `_compact_usage()`) names the full
        `<app-name> <resource-group> <command>` path (see the resource-`prog` fix this pairs with) and lists only its
        own endpoint parameters, not the `--with-*` execution-wrapper flags or the call-control flags that
        would otherwise dominate it
        """
        parser = build_client_parser(cli_client_class, prog="api-client cli-test")
        widgets_parser = get_subparsers_action(parser).choices["widgets"]
        get_widget_parser = get_subparsers_action(widgets_parser).choices["get-widget"]
        usage = remove_color_code(get_widget_parser.format_usage())
        assert "api-client cli-test widgets get-widget" in usage
        assert "--widget-id" in usage
        assert "--with-retry" not in usage
        assert "--quiet" not in usage
        assert "--output" not in usage
        assert "[OPTIONS]" in usage


class TestBuildInitialParser:
    """Tests for `build_initial_parser()`"""

    @pytest.fixture(autouse=True)
    def _mock_discover_clients(self, mocker: MockerFixture, cli_client_class: type[CliTestClient]) -> None:
        """Isolate the tested parser-building logic from `discover_clients_with_failures()`'s own behavior
        (covered separately in `test_discovery.py`), matching the mocking pattern used for
        `TestBuildCompletionTree`. `build_initial_parser()` uses `discover_clients_with_failures()` (not the
        `discover_clients()` wrapper `TestBuildCompletionTree` mocks) since it also needs the failure lists,
        hence the `DiscoveryResult` here rather than a bare dict
        """
        mocker.patch(
            "api_client_core.cli.builder.discover_clients_with_failures",
            return_value=DiscoveryResult({"cli-test": cli_client_class}, [], []),
        )

    def test_prog_is_api_client(self) -> None:
        """Test that the parser's prog is the `api-client` console script name"""
        parser = build_initial_parser()
        assert parser.prog == PROG

    def test_version_flag_prints_the_package_version_and_exits_zero(self, capsys: CaptureFixture[str]) -> None:
        """Test that `--version` prints the package's own version (prefixed with the prog name, matching
        argparse's usual convention) and exits `0`, rather than being misreported as an unknown app name
        (see `test_dispatch.test_a_leading_flag_is_reported_as_a_usage_error_...`, which this makes a real,
        recognized flag instead of just a clean usage error)
        """
        parser = build_initial_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["--version"])
        assert exc_info.value.code == 0
        out = remove_color_code(capsys.readouterr().out)
        assert out.strip() == f"{PROG} {__version__}"

    def test_subparsers_metavar_is_app_name_placeholder(self) -> None:
        """Test that the subparsers action uses the `<app-name>` metavar rather than argparse's default
        choice-list rendering
        """
        parser = build_initial_parser()
        subparsers = get_subparsers_action(parser)
        assert subparsers.metavar == "<app-name>"

    def test_builds_a_subparser_per_discovered_app_name(self, mocker: MockerFixture) -> None:
        """Test that each discovered client becomes a subparser choice, keyed by its app name"""

        class OtherClient(CliTestClient):
            pass

        mocker.patch(
            "api_client_core.cli.builder.discover_clients_with_failures",
            return_value=DiscoveryResult({"cli-test": CliTestClient, "other-app": OtherClient}, [], []),
        )
        parser = build_initial_parser()
        subparsers = get_subparsers_action(parser)
        assert set(subparsers.choices) == {"cli-test", "other-app"}

    def test_app_names_are_listed_in_alphabetical_order(self, mocker: MockerFixture) -> None:
        """Test that app-name choices are sorted regardless of the order discover_clients() returns
        them in, so --help output doesn't depend on discovery's internal iteration order
        """

        class ZetaClient(CliTestClient):
            pass

        class AlphaClient(CliTestClient):
            pass

        mocker.patch(
            "api_client_core.cli.builder.discover_clients_with_failures",
            return_value=DiscoveryResult({"zeta-app": ZetaClient, "alpha-app": AlphaClient}, [], []),
        )
        parser = build_initial_parser()
        subparsers = get_subparsers_action(parser)
        assert list(subparsers.choices) == ["alpha-app", "zeta-app"]

    def test_help_text_uses_only_the_first_docstring_line(self, mocker: MockerFixture) -> None:
        """Test that an app's help text is only the first line of its client class's docstring, not
        the full multi-line text
        """

        class MultiLineDocClient(CliTestClient):
            """First line summary.

            Additional details that should not appear in the one-line help.
            """

        mocker.patch(
            "api_client_core.cli.builder.discover_clients_with_failures",
            return_value=DiscoveryResult({"multi-line": MultiLineDocClient}, [], []),
        )
        parser = build_initial_parser()
        subparsers = get_subparsers_action(parser)
        action = next(a for a in subparsers._choices_actions if a.dest == "multi-line")
        assert action.help == "First line summary."

    def test_help_text_falls_back_to_class_name_when_no_docstring(self, mocker: MockerFixture) -> None:
        """Test that a client class with no docstring gets a generated '<ClassName> commands' help text"""

        class NoDocClient(CliTestClient):
            pass

        mocker.patch(
            "api_client_core.cli.builder.discover_clients_with_failures",
            return_value=DiscoveryResult({"no-doc": NoDocClient}, [], []),
        )
        parser = build_initial_parser()
        subparsers = get_subparsers_action(parser)
        action = next(a for a in subparsers._choices_actions if a.dest == "no-doc")
        assert action.help == "NoDocClient commands"

    def test_no_discovered_clients_produces_no_choices(self, mocker: MockerFixture) -> None:
        """Test that no discovered clients yields a subparsers action with no choices, rather than raising"""
        mocker.patch(
            "api_client_core.cli.builder.discover_clients_with_failures", return_value=DiscoveryResult({}, [], [])
        )
        parser = build_initial_parser()
        subparsers = get_subparsers_action(parser)
        assert subparsers.choices == {}

    def test_warns_when_no_api_clients_are_discovered(self, mocker: MockerFixture) -> None:
        """Test that discovering no client at all (e.g. run from outside any project) gets its own
        warning naming the directory that was actually scanned, rather than leaving an empty app list
        unexplained
        """
        mocker.patch(
            "api_client_core.cli.builder.discover_clients_with_failures", return_value=DiscoveryResult({}, [], [])
        )
        root = Path("/some/project")
        mocker.patch("api_client_core.cli.builder.project_roots", return_value=[root])
        parser = build_initial_parser()
        assert f"No API clients were discovered under {root}" in parser.warnings[0]

    def test_omits_the_no_clients_warning_when_an_unnamed_client_was_recorded(self, mocker: MockerFixture) -> None:
        """Test that the directory-blaming warning is suppressed when discovery found no client but did
        record a candidate declaring no `app_name`, since that's positive proof a real `APIClient` subclass
        was found and imported, unlike a plain empty scan
        """
        mocker.patch(
            "api_client_core.cli.builder.discover_clients_with_failures",
            return_value=DiscoveryResult({}, [], ["MyClient"]),
        )
        mocker.patch("api_client_core.cli.builder.project_roots", return_value=[Path("/some/project")])
        parser = build_initial_parser()
        assert "No API clients were discovered" not in "\n".join(parser.warnings)
        assert "1 candidate class(es) declare no 'app_name'" in "\n".join(parser.warnings)

    def test_omits_the_no_clients_warning_when_clients_are_discovered(self, mocker: MockerFixture) -> None:
        """Test that the no-clients warning is absent once discovery finds at least one client, using
        the class's own autouse `cli-test` discovery mock
        """
        parser = build_initial_parser()
        assert parser.warnings == ()

    @pytest.mark.parametrize(("installed", "expected_tips"), [(False, (_TAB_COMPLETION_TIP,)), (True, ())])
    def test_tips_reflect_whether_argcomplete_is_installed(
        self, installed: bool, expected_tips: list[str], mocker: MockerFixture, cache_home: Path
    ) -> None:
        """Test that `tips` surfaces the tab-completion setup tip when `argcomplete` isn't installed,
        mirroring `build_client_parser()`'s tip behavior, and omits it once `argcomplete` is installed and
        a completion request has already been served (fully set up)
        """
        patch_argcomplete_installed(mocker, installed=installed)
        if installed:
            mark_completion_registered()
        parser = build_initial_parser()
        assert parser.tips == expected_tips

    def test_warns_about_a_module_that_failed_to_import_during_discovery(self, mocker: MockerFixture) -> None:
        """Test that a module that failed to import during discovery (and might have hidden an API client
        behind that failure) is named in a warning, rather than the bare/`--help` app list silently
        looking complete (or empty) with no indication why.

        Regression test: before this, `build_initial_parser()` called the `discover_clients()` wrapper,
        which discards the failure list `discover_clients_with_failures()` itself already collects
        """
        mocker.patch(
            "api_client_core.cli.builder.discover_clients_with_failures",
            return_value=DiscoveryResult(
                {"cli-test": CliTestClient}, ["broken_module: ImportError: no module named 'x'"], []
            ),
        )
        parser = build_initial_parser()
        assert "1 module(s) failed to import" in parser.warnings[0]
        assert "broken_module" in parser.warnings[0]


class TestTabCompletionTips:
    """Tests for `_tab_completion_tips()`"""

    def test_returns_the_install_tip_when_argcomplete_is_not_installed(self, mocker: MockerFixture) -> None:
        """Test that the install-the-extra tip is returned when `argcomplete` isn't importable at all,
        taking priority over the registration tip below
        """
        patch_argcomplete_installed(mocker, installed=False)
        assert _tab_completion_tips() == [_TAB_COMPLETION_TIP]

    def test_returns_the_register_tip_once_installed_but_not_yet_registered(
        self, mocker: MockerFixture, cache_home: Path
    ) -> None:
        """Test that once `argcomplete` is installed but no real shell-completion request has ever been
        served (the marker `_complete()` touches is absent), the registration tip is returned instead of
        silently showing nothing, since a user who installed the extra but hasn't added the `eval` line
        yet would otherwise press TAB, get nothing, and see no hint anywhere
        """
        patch_argcomplete_installed(mocker, installed=True)
        assert _tab_completion_tips() == [_TAB_COMPLETION_REGISTER_TIP]

    def test_returns_no_tip_once_a_completion_request_has_been_served(
        self, mocker: MockerFixture, cache_home: Path
    ) -> None:
        """Test that once a completion request was served at least once, there's nothing left to tell the
        user, and no tip is returned
        """
        patch_argcomplete_installed(mocker, installed=True)
        mark_completion_registered()
        assert _tab_completion_tips() == []


class TestCallArgumentPlacement:
    """Tests for where `--output`/`--quiet`/`--no-hooks`/`--raw-option`/`-H`/`--header` are accepted,
    mirroring the usability of calling an endpoint function directly, where `quiet`/`with_hooks`/
    `raw_options` are passed alongside the other call arguments rather than before them
    """

    def test_call_flags_are_accepted_after_the_command_own_flags(self, cli_client_class: type[CliTestClient]) -> None:
        """Test that --output/--quiet/--no-hooks/--raw-option/-H parse successfully when given after a
        command's own flags
        """
        parser = build_client_parser(cli_client_class)
        args = parser.parse_args(
            [
                "widgets",
                "get-widget",
                "--widget-id",
                "1",
                "--output",
                "json",
                "--quiet",
                "--no-hooks",
                "--raw-option",
                "timeout=30",
                "-H",
                "Authorization: Bearer tok",
            ]
        )
        assert args.output == "json"
        assert args.quiet is True
        assert args.no_hooks is True
        assert dict(args.raw_option) == {"timeout": 30}
        assert args.header == [("Authorization", "Bearer tok")]

    def test_call_flags_are_rejected_before_the_resource(self, cli_client_class: type[CliTestClient]) -> None:
        """Test that --output/--quiet/--no-hooks/--raw-option/-H are not accepted before the resource,
        since they belong to each endpoint's own subparser rather than the top-level parser
        """
        parser = build_client_parser(cli_client_class)
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["--quiet", "widgets", "get-widget", "--widget-id", "1"])
        assert exc_info.value.code == 2
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["--output", "json", "widgets", "get-widget", "--widget-id", "1"])
        assert exc_info.value.code == 2

    def test_header_flag_is_rejected_before_the_resource(self, cli_client_class: type[CliTestClient]) -> None:
        """Test that -H/--header is rejected before the resource. Unlike --base-url/--log-level, it can't
        be registered at multiple parser levels via the same SUPPRESS-default trick: an `action="append"`
        flag given at an inner level silently replaces (rather than extends) one given at an outer level,
        so it's registered leaf-only instead (see `_add_call_ctrl_arguments()`)
        """
        parser = build_client_parser(cli_client_class)
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["-H", "X-Trace: 1", "widgets", "get-widget", "--widget-id", "1"])
        assert exc_info.value.code == 2

    def test_base_url_is_still_accepted_before_the_resource(self, cli_client_class: type[CliTestClient]) -> None:
        """Test that --base-url, which configures the client itself rather than one call, still parses
        before the resource
        """
        parser = build_client_parser(cli_client_class)
        args = parser.parse_args(
            ["--base-url", "https://override.example.com", "widgets", "get-widget", "--widget-id", "1"]
        )
        assert args.base_url == "https://override.example.com"

    def test_base_url_and_log_level_are_also_accepted_after_the_command(
        self, cli_client_class: type[CliTestClient]
    ) -> None:
        """Test that --base-url/--log-level, unlike --quiet/--no-hooks/--raw-option, are accepted both
        before the resource and after the command's own flags, since a global option naturally belongs
        wherever the user reaches for it
        """
        parser = build_client_parser(cli_client_class)
        args = parser.parse_args(
            [
                "widgets",
                "get-widget",
                "--widget-id",
                "1",
                "--base-url",
                "https://override.example.com",
                "--log-level",
                "DEBUG",
            ]
        )
        assert args.base_url == "https://override.example.com"
        assert args.log_level == "DEBUG"

    def test_log_level_is_case_insensitive(self, cli_client_class: type[CliTestClient]) -> None:
        """Test that a lowercase --log-level value (e.g. "debug") is accepted and normalized to uppercase,
        rather than rejected as an invalid choice
        """
        parser = build_client_parser(cli_client_class)
        args = parser.parse_args(["widgets", "get-widget", "--widget-id", "1", "--log-level", "debug"])
        assert args.log_level == "DEBUG"

    def test_a_value_given_after_the_command_overrides_one_given_before_the_resource(
        self, cli_client_class: type[CliTestClient]
    ) -> None:
        """Test that a --base-url given after the command wins over one given before the resource,
        matching a normal keyword argument's own override semantics.

        Regression test: giving the leaf-level flag its own plain `default=None` (matching the top-level
        one) would instead have the *absence* of `--base-url` at the leaf silently overwrite the value
        already parsed at the top level with `None`, since argparse re-applies a subparser's own action
        defaults onto the shared namespace it already populated
        """
        parser = build_client_parser(cli_client_class)
        args = parser.parse_args(
            [
                "--base-url",
                "https://top-level.example.com",
                "widgets",
                "get-widget",
                "--widget-id",
                "1",
                "--base-url",
                "https://leaf-level.example.com",
            ]
        )
        assert args.base_url == "https://leaf-level.example.com"

    def test_omitting_the_leaf_level_flag_keeps_the_top_level_value(
        self, cli_client_class: type[CliTestClient]
    ) -> None:
        """Test that a --base-url given only before the resource survives all the way through the leaf
        parser's own parsing when it isn't repeated there, rather than being reset to `None`
        """
        parser = build_client_parser(cli_client_class)
        args = parser.parse_args(
            ["--base-url", "https://top-level.example.com", "widgets", "get-widget", "--widget-id", "1"]
        )
        assert args.base_url == "https://top-level.example.com"


class TestArgumentGroups:
    """Tests for how a leaf command's flags are grouped in `--help` output"""

    def test_groups_appear_in_the_expected_order(self, cli_client_class: type[CliTestClient]) -> None:
        """Test that a leaf command's populated argument groups render endpoint parameters first,
        then call wrappers, then the call-control group (options)
        """
        parser = build_client_parser(cli_client_class)
        widgets_parser = get_subparsers_action(parser).choices["widgets"]
        create_widget_parser = get_subparsers_action(widgets_parser).choices["create-widget"]
        populated_titles = [g.title for g in create_widget_parser._action_groups if g._group_actions]
        assert populated_titles == [
            _PARAMS_GROUP_TITLE,
            _WRAPPERS_GROUP_TITLE,
            _OPTIONS_GROUP_TITLE,
        ]

    def test_endpoint_parameter_flags_are_grouped_separately_from_call_control_flags(
        self, cli_client_class: type[CliTestClient]
    ) -> None:
        """Test that an endpoint's own parameter flags land in the endpoint parameters group, not
        alongside --quiet/--no-hooks/--raw-option
        """
        parser = build_client_parser(cli_client_class)
        widgets_parser = get_subparsers_action(parser).choices["widgets"]
        get_widget_parser = get_subparsers_action(widgets_parser).choices["get-widget"]
        assert find_group_title(get_widget_parser, "--widget-id") == _PARAMS_GROUP_TITLE

    def test_call_control_flags_land_in_the_options_group(self, cli_client_class: type[CliTestClient]) -> None:
        """Test that -h/--base-url/--no-hooks/-H/--raw-option and -o/--output/--log-level/-q/--quiet all
        land in the same options group, restoring the auto-added -h that add_help=False suppresses on the
        leaf parser
        """
        parser = build_client_parser(cli_client_class)
        widgets_parser = get_subparsers_action(parser).choices["widgets"]
        get_widget_parser = get_subparsers_action(widgets_parser).choices["get-widget"]
        for flag in ("-h", "--base-url", "--no-hooks", "-H", "--raw-option", "-o", "--log-level", "-q"):
            assert find_group_title(get_widget_parser, flag) == _OPTIONS_GROUP_TITLE

    @pytest.mark.parametrize("flag", ["-h", "--help"])
    def test_help_still_works_despite_add_help_false_on_the_leaf_parser(
        self, flag: str, cli_client_class: type[CliTestClient], capsys: CaptureFixture[str]
    ) -> None:
        """Test that both -h and --help on a leaf command still trigger a clean exit, confirming the
        manually re-added help flag behaves like the auto-added one
        """
        parser = build_client_parser(cli_client_class)
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["widgets", "get-widget", flag])
        assert exc_info.value.code == 0
        assert "usage:" in capsys.readouterr().out

    def test_short_help_shows_only_the_first_line_of_each_flags_help_and_help_shows_it_in_full(
        self, cli_client_class: type[CliTestClient], capsys: CaptureFixture[str]
    ) -> None:
        """Test that -h renders only the first line of every flag's own help text (dropping
        --output's own detail, listing every value), while --help keeps every flag's help in full
        """
        parser = build_client_parser(cli_client_class)

        with pytest.raises(SystemExit):
            parser.parse_args(["widgets", "get-widget", "-h"])
        short_help = remove_color_code(capsys.readouterr().out)

        with pytest.raises(SystemExit):
            parser.parse_args(["widgets", "get-widget", "--help"])
        full_help = remove_color_code(capsys.readouterr().out)

        assert "none: nothing" not in short_help
        assert "none: nothing" in full_help
        assert len(short_help.splitlines()) < len(full_help.splitlines())

    def test_short_help_collapses_the_call_wrappers_group_to_a_note(
        self, cli_client_class: type[CliTestClient], capsys: CaptureFixture[str]
    ) -> None:
        """Test that -h replaces the call wrappers group's own description and every wrapper flag
        with a short note, while --help renders the group in full, unaffected
        """
        parser = build_client_parser(cli_client_class)

        with pytest.raises(SystemExit):
            parser.parse_args(["widgets", "get-widget", "-h"])
        short_help = remove_color_code(capsys.readouterr().out)

        with pytest.raises(SystemExit):
            parser.parse_args(["widgets", "get-widget", "--help"])
        full_help = remove_color_code(capsys.readouterr().out)

        # Each line of a group description is indented independently, so the short note's two lines are checked
        # separately rather than as one substring spanning the embedded newline.
        for line in _WRAPPERS_GROUP_SHORT_DESCRIPTION.splitlines():
            assert line in short_help
            assert line not in full_help
        # Collapsed to single-spaced text so the assertion survives the description wrapping onto more than
        # one line at a narrower terminal width, rather than asserting its exact unwrapped substring.
        assert " ".join(_WRAPPERS_GROUP_DESCRIPTION.split()) not in " ".join(short_help.split())
        assert " ".join(_WRAPPERS_GROUP_DESCRIPTION.split()) in " ".join(full_help.split())
        for flag in ("--with-retry", "--with-rate-limit", "--with-lock", "--with-expected-status"):
            assert flag not in short_help
            assert flag in full_help


class TestReservedFlagCoverage:
    """Tests that `_constants.RESERVED_CLI_FLAGS`'s two source enums stay in sync with what a real leaf
    command parser actually registers, since the frozenset is derived from `Flag`/`WrapperFlag` rather than
    hand-listed
    """

    def test_every_flag_and_wrapper_flag_is_registered_on_a_leaf_command(
        self, cli_client_class: type[CliTestClient]
    ) -> None:
        """Test that every `Flag` member except `VERSION` (top-level only) and every `WrapperFlag` member is
        registered as an option string on a real leaf command parser, so a member added to either enum
        without a matching `add_argument()` call would be caught here
        """
        parser = build_client_parser(cli_client_class)
        widgets_parser = get_subparsers_action(parser).choices["widgets"]
        create_widget_parser = get_subparsers_action(widgets_parser).choices["create-widget"]
        registered = {opt for action in create_widget_parser._actions for opt in action.option_strings}
        assert set(Flag) - {Flag.VERSION} <= registered
        assert set(WrapperFlag) <= registered


class TestRawOptionParsing:
    """Tests for `--raw-option` parsing on the generated parser"""

    @pytest.mark.parametrize(
        ("raw_option", "expected"),
        [
            ("timeout=30", {"timeout": 30}),
            ("Authorization=Bearer abc", {"Authorization": "Bearer abc"}),
        ],
    )
    def test_parses_json_value_and_falls_back_to_string(
        self, raw_option: str, expected: dict[str, Any], cli_client_class: type[CliTestClient]
    ) -> None:
        """Test that a JSON-parseable value is decoded, and a non-JSON value is kept as a plain string"""
        parser = build_client_parser(cli_client_class)
        args = parser.parse_args(["widgets", "get-widget", "--widget-id", "1", "--raw-option", raw_option])
        assert dict(args.raw_option) == expected

    def test_missing_equals_sign_exits_cleanly_rather_than_crashing(
        self, cli_client_class: type[CliTestClient]
    ) -> None:
        """Test that a --raw-option value without '=' raises SystemExit via argparse (a clean error and
        exit code 2), not an uncaught ArgumentTypeError once parsing has already completed
        """
        parser = build_client_parser(cli_client_class)
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["widgets", "get-widget", "--widget-id", "1", "--raw-option", "not-a-kv-pair"])
        assert exc_info.value.code == 2


class TestParseHeader:
    """Tests for `_parse_header()`"""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("X-API-Key:secret", [("X-API-Key", "secret")]),
            ("Authorization: Bearer abc", [("Authorization", "Bearer abc")]),
            ("Authorization: Bearer a:b", [("Authorization", "Bearer a:b")]),
            ("X-Flag:", [("X-Flag", "")]),
        ],
    )
    def test_parses_name_and_value(self, value: str, expected: list[tuple[str, str]]) -> None:
        """Test that a well-formed NAME:VALUE string parses to a single-item list holding its (name, value)
        pair, that whitespace around the name and value is stripped (matching curl's -H spelling), that a
        value containing its own colon survives intact, and that an empty value (some APIs key off a
        header's mere presence) is allowed
        """
        assert _parse_header(value) == expected

    @pytest.mark.parametrize("value", ["no-colon-here", ": value-only"])
    def test_missing_colon_or_empty_name_raises(self, value: str) -> None:
        """Test that a value with no ':', or with an empty name before ':', raises ArgumentTypeError"""
        with pytest.raises(argparse.ArgumentTypeError):
            _parse_header(value)

    def test_reads_one_header_per_line_from_a_file(self, tmp_path: Path) -> None:
        """Test that `@<path>` reads the file and parses one header per non-blank line"""
        path = tmp_path / "headers.txt"
        path.write_text("Authorization: Bearer tok\n\nX-Trace: 1\n")
        assert _parse_header(f"@{path}") == [("Authorization", "Bearer tok"), ("X-Trace", "1")]

    def test_reads_one_header_per_line_from_stdin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that `-` reads every non-blank line from stdin as a header, so a sensitive value never has
        to be typed into argv
        """
        monkeypatch.setattr(sys, "stdin", io.StringIO("Authorization: Bearer tok\nX-Trace: 1\n"))
        reset_stdin_state()
        assert _parse_header("-") == [("Authorization", "Bearer tok"), ("X-Trace", "1")]

    def test_a_file_with_no_headers_raises(self, tmp_path: Path) -> None:
        """Test that an `@<path>` file with no non-blank lines raises ArgumentTypeError rather than
        silently contributing zero headers
        """
        path = tmp_path / "empty.txt"
        path.write_text("\n\n")
        with pytest.raises(argparse.ArgumentTypeError):
            _parse_header(f"@{path}")

    def test_an_unreadable_file_raises(self, tmp_path: Path) -> None:
        """Test that a nonexistent `@<path>` file raises ArgumentTypeError, matching the JSON-typed
        indirection's own handling of the same failure
        """
        with pytest.raises(argparse.ArgumentTypeError):
            _parse_header(f"@{tmp_path / 'does-not-exist.txt'}")


class TestHeaderParsingOnParser:
    """Tests for `-H`/`--header` parsing on the generated parser"""

    @pytest.mark.parametrize(
        ("extra_argv", "expected_header"),
        [
            ([], []),
            (
                ["-H", "Authorization: Bearer tok", "--header", "X-Trace: 1"],
                [("Authorization", "Bearer tok"), ("X-Trace", "1")],
            ),
        ],
    )
    def test_header_flags_accumulate_or_default_to_empty(
        self, extra_argv: list[str], expected_header: list[tuple[str, str]], cli_client_class: type[CliTestClient]
    ) -> None:
        """Test that omitting -H/--header entirely parses to an empty list, not None, and that
        repeated -H/--header flags, mixing the short and long spelling, accumulate as a list of
        (name, value) pairs in the order given
        """
        parser = build_client_parser(cli_client_class)
        args = parser.parse_args(["widgets", "get-widget", "--widget-id", "1", *extra_argv])
        assert args.header == expected_header

    def test_an_at_path_occurrence_flattens_into_the_accumulated_list(
        self, cli_client_class: type[CliTestClient], tmp_path: Path
    ) -> None:
        """Test that a `-H @<path>` occurrence contributes every line in the file as a separate header,
        flattened into the same accumulated list a plain `-H NAME:VALUE` occurrence would append to,
        rather than nesting the file's own headers as one list-shaped item
        """
        path = tmp_path / "headers.txt"
        path.write_text("Authorization: Bearer tok\nX-Trace: 1\n")
        parser = build_client_parser(cli_client_class)
        args = parser.parse_args(["widgets", "get-widget", "--widget-id", "1", "-H", f"@{path}", "-H", "X-Extra: 2"])
        assert args.header == [("Authorization", "Bearer tok"), ("X-Trace", "1"), ("X-Extra", "2")]

    def test_the_header_flag_offers_file_path_completion_once_at_is_typed(
        self, cli_client_class: type[CliTestClient]
    ) -> None:
        """Test that `-H`/`--header` is marked the same way a JSON-typed flag is, so its value completes to
        real filesystem paths only once `@` itself is typed, matching the file-indirection it now accepts
        """
        parser = build_client_parser(cli_client_class)
        command_parser = get_subparsers_action(get_subparsers_action(parser).choices["widgets"]).choices["get-widget"]
        header_action = next(a for a in command_parser._actions if "-H" in a.option_strings)
        assert accepts_json_file(header_action)

    def test_malformed_header_exits_cleanly_rather_than_crashing(self, cli_client_class: type[CliTestClient]) -> None:
        """Test that a malformed -H value raises SystemExit via argparse (a clean error and exit code
        2), not an uncaught ArgumentTypeError once parsing has already completed
        """
        parser = build_client_parser(cli_client_class)
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["widgets", "get-widget", "--widget-id", "1", "-H", "no-colon-here"])
        assert exc_info.value.code == 2

    def test_headers_do_not_leak_between_separate_parses_of_the_same_parser(
        self, cli_client_class: type[CliTestClient]
    ) -> None:
        """Test that one parse's own -H/--header values don't persist into a later parse of the same
        parser object.

        `_HeaderAction` accumulates into the list already on the namespace, which for the very first
        occurrence is `action.default` itself, i.e. the same list every parse of this parser starts from.
        Mutating it in place, rather than rebinding to a fresh list, would leak one parse's own headers into
        every parse that follows, including one that gives no -H/--header at all. This is invisible to the
        real `api-client` process (a fresh parser per run), but not to a caller that dispatches more than one
        command through the same built parser, e.g. `runner.run()` called directly in a loop, or this
        package's own test suite reusing a `cli_client_class` fixture's parser
        """
        parser = build_client_parser(cli_client_class)
        first = parser.parse_args(["widgets", "get-widget", "--widget-id", "1", "-H", "X-A: 1"])
        assert first.header == [("X-A", "1")]

        second = parser.parse_args(["widgets", "get-widget", "--widget-id", "1"])
        assert second.header == []


def _find_opt(opts: list[dict[str, Any]], flag: str) -> dict[str, Any]:
    """Return the `optspec` in `opts` whose `opts` list contains `flag`.

    :param opts: `optspec` list, as produced by `build_completion_tree()`
    :param flag: Flag string to look up (e.g. `--active`)
    """
    return next(spec for spec in opts if flag in spec["opts"])


class Color(Enum):
    """Synthetic enum used to test that a `Literal[Enum member]` param's choices survive
    completion-tree serialization.
    """

    RED = "red"


class LiteralEnumAPI(BaseAPI):
    """A synthetic API class exposing a `Literal[Color.RED]`-typed param."""

    app_name = "cli-test"

    @endpoint.post("/colors")
    def set_color(self, color: Literal[Color.RED], **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Set a color"""
        ...


class LiteralEnumClient(CliTestClient):
    """A synthetic client exposing `LiteralEnumAPI`, for testing completion-tree serialization of a
    `Literal[Enum member]` param end-to-end.
    """

    @cached_property
    def colors(self) -> LiteralEnumAPI:
        return LiteralEnumAPI(self)


class LiteralBoolAPI(BaseAPI):
    """A synthetic API class exposing a `Literal[True, False]`-typed param."""

    app_name = "cli-test"

    @endpoint.post("/flags")
    def set_flag(self, flag: Literal[True, False], **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Set a flag"""
        ...


class LiteralBoolClient(CliTestClient):
    """A synthetic client exposing `LiteralBoolAPI`, for testing completion-tree serialization of a
    `Literal[True, False]` param end-to-end.
    """

    @cached_property
    def flags(self) -> LiteralBoolAPI:
        return LiteralBoolAPI(self)


class TestBuildCompletionTree:
    """Tests for `build_completion_tree()`, the on-disk shell-completion cache's tree producer"""

    @pytest.fixture(autouse=True)
    def _mock_discover_clients(self, mocker: MockerFixture, cli_client_class: type[CliTestClient]) -> None:
        """Isolate the tree-serialization logic under test from `discover_clients()`'s own behavior
        (covered separately in `test_discovery.py`), matching the mocking pattern used for `main.py`
        """
        mocker.patch("api_client_core.cli.builder.discover_clients", return_value={"cli-test": cli_client_class})

    def test_keys_the_tree_by_app_name(self) -> None:
        """Test that the tree has one entry per discovered client, keyed by its app name"""
        tree = build_completion_tree()
        assert set(tree) == {"cli-test"}

    def test_app_level_opts_include_base_url(self) -> None:
        """Test that the client-construction flag (--base-url) is captured at the app level, not
        nested under a resource/command (it's added directly to the app parser, not a subparser)
        """
        tree = build_completion_tree()
        assert _find_opt(tree["cli-test"]["opts"], "--base-url") == {
            "opts": ["--base-url"],
            "choices": None,
            "nargs": None,
            "is_file": False,
            "is_json_file": False,
        }

    def test_file_param_is_marked_is_file(self) -> None:
        """Test that a File-typed param (upload_avatar's `avatar`) is marked is_file, so the rebuilt
        parser can offer it real path completion while other value flags don't get one
        """
        opts = build_completion_tree()["cli-test"]["resources"]["widgets"]["commands"]["upload-avatar"]
        assert _find_opt(opts, "--avatar")["is_file"] is True

    def test_non_file_param_is_not_marked_is_file(self) -> None:
        """Test that an ordinary str param isn't marked is_file"""
        opts = build_completion_tree()["cli-test"]["resources"]["widgets"]["commands"]["create-widget"]
        assert _find_opt(opts, "--name")["is_file"] is False

    def test_json_typed_param_is_marked_is_json_file(self) -> None:
        """Test that a JSON-typed param (create_widget's `metadata`) is marked is_json_file, so the rebuilt
        parser can offer it `@<path>` completion while other value flags don't get one
        """
        opts = build_completion_tree()["cli-test"]["resources"]["widgets"]["commands"]["create-widget"]
        assert _find_opt(opts, "--metadata")["is_json_file"] is True

    def test_non_json_param_is_not_marked_is_json_file(self) -> None:
        """Test that an ordinary str param isn't marked is_json_file"""
        opts = build_completion_tree()["cli-test"]["resources"]["widgets"]["commands"]["create-widget"]
        assert _find_opt(opts, "--name")["is_json_file"] is False

    def test_resources_and_commands_match_build_parser(self) -> None:
        """Test that the tree's resource/command names match what build_parser() itself generates"""
        tree = build_completion_tree()
        resources = tree["cli-test"]["resources"]
        assert set(resources) == {"widgets", "gadgets"}
        assert set(resources["widgets"]["commands"]) == {
            "get-widget",
            "create-widget",
            "upload-avatar",
            "list-widgets",
        }

    def test_resource_level_opts_include_base_url(self) -> None:
        """Test that --base-url/--log-level are also captured at the resource level, distinct from each
        of its commands' own opts (added directly to the resource parser, see `_add_global_arguments()`)
        """
        opts = build_completion_tree()["cli-test"]["resources"]["widgets"]["opts"]
        assert _find_opt(opts, "--base-url") == {
            "opts": ["--base-url"],
            "choices": None,
            "nargs": None,
            "is_file": False,
            "is_json_file": False,
        }

    def test_boolean_optional_flag_preserves_nargs_0_with_both_option_strings(self) -> None:
        """Test that a bool-typed param (rebuilt as BooleanOptionalAction) is captured with both its
        --flag/--no-flag option strings and nargs=0, so completion won't consume the next token as its
        value
        """
        opts = build_completion_tree()["cli-test"]["resources"]["widgets"]["commands"]["create-widget"]
        spec = _find_opt(opts, "--active")
        assert spec["opts"] == ["--active", "--no-active"]
        assert spec["nargs"] == 0

    def test_store_true_call_flag_preserves_nargs_0(self) -> None:
        """Test that the constant --quiet/--no-hooks call flags (store_true) serialize nargs=0"""
        opts = build_completion_tree()["cli-test"]["resources"]["widgets"]["commands"]["get-widget"]
        assert _find_opt(opts, "--quiet")["nargs"] == 0
        assert _find_opt(opts, "--no-hooks")["nargs"] == 0

    def test_output_flag_serializes_its_short_alias_and_choices(self) -> None:
        """Test that --output serializes both its -o/--output option strings and its none/json/raw/full
        choices, with nargs=None since it takes exactly one value
        """
        opts = build_completion_tree()["cli-test"]["resources"]["widgets"]["commands"]["get-widget"]
        spec = _find_opt(opts, "--output")
        assert spec["opts"] == ["-o", "--output"]
        assert spec["choices"] == ["none", "json", "raw", "full"]
        assert spec["nargs"] is None

    def test_literal_param_carries_its_value_choices(self) -> None:
        """Test that a Literal-typed param's value choices are preserved for value completion"""
        opts = build_completion_tree()["cli-test"]["resources"]["widgets"]["commands"]["create-widget"]
        assert _find_opt(opts, "--priority")["choices"] == [1, 2, 3]

    def test_with_xxx_wrapper_optional_value_flag_preserves_its_nargs(self) -> None:
        """Test that an optional-value wrapper flag (--with-retry) serializes its real nargs="?", so the
        rebuilt completion parser doesn't consume the next token as its value the way it would if this
        degraded to a plain single-value flag (nargs=None)

        Regression test: before OptSpec tracked the real nargs, every value-taking flag (regardless of its
        own arity) was rebuilt as a plain single-value flag
        """
        opts = build_completion_tree()["cli-test"]["resources"]["widgets"]["commands"]["get-widget"]
        assert _find_opt(opts, "--with-retry")["nargs"] == "?"

    def test_with_expected_status_repeatable_flag_preserves_its_nargs(self) -> None:
        """Test that the repeatable --with-expected-status flag serializes its real nargs="+", so the
        rebuilt completion parser keeps accepting further values in the same occurrence instead of treating
        the second one as unrelated

        Regression test: before OptSpec tracked the real nargs, this flag was rebuilt as a plain
        single-value flag, unable to accept more than one CODE per occurrence
        """
        opts = build_completion_tree()["cli-test"]["resources"]["widgets"]["commands"]["get-widget"]
        assert _find_opt(opts, "--with-expected-status")["nargs"] == "+"

    def test_with_stats_wrapper_flag_preserves_nargs_0(self) -> None:
        """Test that the zero-arg --with-stats wrapper flag serializes nargs=0, same as the constant
        --quiet/--no-hooks call flags
        """
        opts = build_completion_tree()["cli-test"]["resources"]["widgets"]["commands"]["get-widget"]
        assert _find_opt(opts, "--with-stats")["nargs"] == 0

    def test_help_flag_is_never_serialized(self) -> None:
        """Test that the auto-added -h/--help is excluded everywhere, since argparse re-adds it
        itself on rebuild and a duplicate would raise ArgumentError
        """
        client_tree = build_completion_tree()["cli-test"]
        resource_opts = [r["opts"] for r in client_tree["resources"].values()]
        command_opts = [opts for r in client_tree["resources"].values() for opts in r["commands"].values()]
        all_opts = [spec for scope in (client_tree["opts"], *resource_opts, *command_opts) for spec in scope]
        assert not any({"-h", "--help"} & set(spec["opts"]) for spec in all_opts)

    def test_tree_is_json_serializable(self) -> None:
        """Test that the tree round-trips through JSON, since it's written to the on-disk cache as-is"""
        tree = build_completion_tree()
        assert json.loads(json.dumps(tree)) == tree

    def test_a_literal_of_enum_members_does_not_break_tree_serialization(self, mocker: MockerFixture) -> None:
        """Test that a `Literal[SomeEnum.MEMBER]` param's choices are stringified via its member name
        (matching what the real CLI flag itself accepts) rather than left as raw Enum members, which
        `json.dumps` can't serialize.

        Regression test: this used to raise `TypeError` inside `_cache.save_cache()`, before
        `argcomplete.autocomplete()` ever ran, silently killing tab completion for every client on every
        request until the offending param was removed
        """
        mocker.patch("api_client_core.cli.builder.discover_clients", return_value={"cli-test": LiteralEnumClient})

        tree = build_completion_tree()

        opts = tree["cli-test"]["resources"]["colors"]["commands"]["set-color"]
        assert _find_opt(opts, "--color")["choices"] == [Color.RED.name]
        assert json.loads(json.dumps(tree)) == tree

    def test_a_literal_of_bools_serializes_true_false_tokens_not_python_repr(self, mocker: MockerFixture) -> None:
        """Test that a `Literal[True, False]` param's choices serialize as `["true", "false"]`, matching
        what the real CLI flag's own converter accepts, rather than JSON's native `[true, false]` spelling
        (Python's `True`/`False`), which the real flag would then reject.

        Regression test: before this fix, completion offered `True`/`False` (passed through unchanged as a
        JSON-native `bool`) for a token the real parser only ever accepts spelled `true`/`false`
        """
        mocker.patch("api_client_core.cli.builder.discover_clients", return_value={"cli-test": LiteralBoolClient})

        tree = build_completion_tree()

        opts = tree["cli-test"]["resources"]["flags"]["commands"]["set-flag"]
        assert _find_opt(opts, "--flag")["choices"] == ["true", "false"]
        assert json.loads(json.dumps(tree)) == tree

    def test_skips_a_client_whose_parser_cannot_be_built(self, mocker: MockerFixture) -> None:
        """Test that a client whose parser build raises (e.g. one exposing no discoverable API
        classes) is skipped rather than aborting the whole tree, so one broken client doesn't break
        completion for every other, healthy client
        """

        class EmptyClient(APIClient):
            app_name = "empty"

        mocker.patch(
            "api_client_core.cli.builder.discover_clients",
            return_value={"cli-test": CliTestClient, "empty": EmptyClient},
        )
        tree = build_completion_tree()
        assert set(tree) == {"cli-test"}

    def test_skipping_a_broken_client_logs_a_debug_message(self, mocker: MockerFixture) -> None:
        """Test that skipping a client whose parser can't be built logs a debug message naming it, so a
        client missing from completion stays diagnosable via DEBUG logging
        """

        class EmptyClient(APIClient):
            app_name = "empty"

        mocker.patch(
            "api_client_core.cli.builder.discover_clients",
            return_value={"cli-test": CliTestClient, "empty": EmptyClient},
        )
        mock_log = mocker.patch("api_client_core.cli.builder.logger")
        build_completion_tree()
        matching_calls = [call for call in mock_log.debug.call_args_list if "empty" in call[0][0]]
        assert len(matching_calls) == 1


class TestBuildCompletionEntry:
    """Tests for `build_completion_entry()`, the single-client `build_completion_tree()` schema producer"""

    def test_matches_the_entry_build_completion_tree_produces_for_the_same_client(self, mocker: MockerFixture) -> None:
        """Test that calling it directly for one client produces the same entry `build_completion_tree()`
        itself would produce for that client, since the latter is defined in terms of the former
        """
        mocker.patch("api_client_core.cli.builder.discover_clients", return_value={"cli-test": CliTestClient})

        entry = build_completion_entry(CliTestClient)
        tree = build_completion_tree()

        assert entry == tree["cli-test"]


class TestToKebabCase:
    """Tests for `_to_kebab_case()`'s mapping from a Python identifier (attribute or function name) to a
    lowercase, kebab-case CLI token
    """

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("users", "users"),
            ("Users", "users"),
            ("get_user", "get-user"),
            ("getUser", "get-user"),
            ("UserProfiles", "user-profiles"),
            ("APIKeys", "api-keys"),
            ("OAuth2Tokens", "o-auth2-tokens"),
            ("_unnamed_endpoint_1", "_unnamed-endpoint-1"),
            ("_PrivateWidget", "_private-widget"),
        ],
    )
    def test_normalizes_to_the_expected_kebab_case_token(self, name: str, expected: str) -> None:
        """Test that a plain snake_case name, a capitalized or camelCase name, a multi-word CamelCase
        name, an all-caps acronym followed by a capitalized word, a digit-then-capital boundary, and a
        leading-underscore name (preserved, not turned into a leading hyphen - see
        `TestBuildClientParser.test_a_leading_underscore_endpoint_name_...`) all normalize to the same
        idiomatic, lowercase kebab-case token a hand-written CLI command name would use
        """
        assert _to_kebab_case(name) == expected


class TestCompactUsage:
    """Tests for `_compact_usage()`/`_format_action_usage()`, the leaf-command usage line covering only an
    endpoint's own parameters plus an `[OPTIONS]` placeholder for the wrapper/control flags added after
    """

    def test_format_action_usage_wraps_an_optional_scalar_flag_in_brackets(self) -> None:
        """Test that an optional, single-value flag renders as `[--flag METAVAR]`"""
        parser = argparse.ArgumentParser(add_help=False)
        action = parser.add_argument("--limit", dest="limit", required=False)
        assert _format_action_usage(action) == "[--limit LIMIT]"

    def test_format_action_usage_does_not_bracket_a_required_flag(self) -> None:
        """Test that a required, single-value flag renders without brackets"""
        parser = argparse.ArgumentParser(add_help=False)
        action = parser.add_argument("--q", dest="q", required=True)
        assert _format_action_usage(action) == "--q Q"

    def test_format_action_usage_uses_an_explicit_metavar_when_given(self) -> None:
        """Test that an action's own explicit `metavar` (e.g. a Literal/Enum's own choice group) is used
        in place of the default `dest.upper()` fallback
        """
        parser = argparse.ArgumentParser(add_help=False)
        action = parser.add_argument("--status", dest="status", metavar="{ACTIVE,INACTIVE}", required=False)
        assert _format_action_usage(action) == "[--status {ACTIVE,INACTIVE}]"

    def test_format_action_usage_strips_edge_underscores_from_a_default_metavar(self) -> None:
        """Test that a `dest` with no explicit `metavar` (e.g. `from_`, derived from a trailing-underscore
        parameter name) drops its leading/trailing underscore instead of falling back to `dest.upper()`
        """
        parser = argparse.ArgumentParser(add_help=False)
        action = parser.add_argument("--from", dest="from_", required=False)
        assert _format_action_usage(action) == "[--from FROM]"

    def test_format_action_usage_renders_a_value_less_flag_without_a_metavar(self) -> None:
        """Test that a value-less flag (`nargs=0`, e.g. `argparse.BooleanOptionalAction`/`store_true`)
        renders as the bare flag, with no value placeholder
        """
        parser = argparse.ArgumentParser(add_help=False)
        action = parser.add_argument("--cache", dest="cache", action="store_true", required=False)
        assert _format_action_usage(action) == "[--cache]"

    def test_format_action_usage_brackets_an_optional_boolean_pair(self) -> None:
        """Test that an optional `bool` flag (`argparse.BooleanOptionalAction`, registering both
        `--flag`/`--no-flag`) renders both option strings, bracketed as one group, matching argparse's own
        rendering of the same flag
        """
        parser = argparse.ArgumentParser(add_help=False)
        action = parser.add_argument("--merge", dest="merge", action=argparse.BooleanOptionalAction, required=False)
        assert _format_action_usage(action) == "[--merge | --no-merge]"

    def test_format_action_usage_parenthesizes_a_required_boolean_pair(self) -> None:
        """Test that a required `bool` flag renders both option strings wrapped in parens rather than left
        bare: a bare `--flag | --no-flag` mid-usage-line reads ambiguously next to a neighboring flag,
        unlike argparse's own rendering of the same flag standing alone
        """
        parser = argparse.ArgumentParser(add_help=False)
        action = parser.add_argument(
            "--completed", dest="completed", action=argparse.BooleanOptionalAction, required=True
        )
        assert _format_action_usage(action) == "(--completed | --no-completed)"

    def test_format_action_usage_renders_a_repeatable_flag_with_an_ellipsis(self) -> None:
        """Test that an `nargs='*'`/`'+'` (repeatable) flag renders its metavar twice, the second wrapped
        with a literal `...`, matching argparse's own convention for such a flag
        """
        parser = argparse.ArgumentParser(add_help=False)
        star_action = parser.add_argument("--tags", dest="tags", nargs="*", required=False)
        plus_action = parser.add_argument("--ids", dest="ids", nargs="+", required=True)
        assert _format_action_usage(star_action) == "[--tags [TAGS ...]]"
        assert _format_action_usage(plus_action) == "--ids IDS [IDS ...]"

    def test_compact_usage_omits_wrapper_and_control_flags(self) -> None:
        """Test that `_compact_usage()` only ever renders the given `param_actions` (plus `[options]`),
        never anything about the wrapper/control flags it has no knowledge of
        """
        parser = argparse.ArgumentParser(add_help=False)
        action = parser.add_argument("--widget-id", dest="widget_id", required=True)
        usage = _compact_usage("api-client my-app widgets get-widget", [action])
        assert usage == "api-client my-app widgets get-widget --widget-id WIDGET_ID [OPTIONS]"

    def test_compact_usage_wraps_across_lines_once_too_long_for_the_terminal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that a compact usage line too long to fit the terminal width wraps across multiple,
        indented lines rather than argparse's own usual behavior of never wrapping an explicitly given
        `usage=` string at all (see `_compact_usage()`'s own docstring). Uses a `prog` short enough that it
        still shares its line with the first wrapped token, exercising the common case where the aligned-
        under-`prog` indent is used (see `test_compact_usage_gives_a_long_prog_its_own_line` for the
        alternative, where `prog` itself is long enough that it can't)
        """
        monkeypatch.setenv("COLUMNS", "42")
        parser = argparse.ArgumentParser(add_help=False)
        actions = [
            parser.add_argument("--limit", dest="limit", required=False),
            parser.add_argument("--skip", dest="skip", required=False),
            parser.add_argument("--select", dest="select", required=False),
        ]
        usage = _compact_usage("api-client x", actions)
        lines = usage.splitlines()
        assert len(lines) > 1
        assert lines[0] == "api-client x [--limit LIMIT]"
        assert all(len(line) <= 40 for line in ("usage: " + usage).splitlines())
        indent = len("usage: ") + len("api-client x") + 1
        assert all(line.startswith(" " * indent) for line in lines[1:])

    def test_compact_usage_gives_a_long_prog_its_own_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that once `prog` itself is long enough that the usual aligned-under-`prog` indent wouldn't
        leave room for even the longest remaining token, `prog` gets its own line, indented continuation
        lines use a minimal indent instead, and every wrapped line (`prog`'s own line included) still fits
        within the terminal width - rather than every continuation line overflowing unconditionally
        regardless of its own length, which is what the aligned-under-`prog` indent alone would produce here
        """
        monkeypatch.setenv("COLUMNS", "60")
        parser = argparse.ArgumentParser(add_help=False)
        actions = [
            parser.add_argument("--limit", dest="limit", required=False),
            parser.add_argument("--skip", dest="skip", required=False),
            parser.add_argument("--select", dest="select", required=False),
        ]
        usage = _compact_usage("api-client my-app users list-users", actions)
        lines = usage.splitlines()
        assert lines[0] == "api-client my-app users list-users"
        assert all(len(line) <= 58 for line in ("usage: " + usage).splitlines())
        assert all(line.startswith(" " * len("usage: ")) for line in lines[1:])

    def test_compact_usage_escapes_a_literal_percent(self) -> None:
        """Test that a literal `%` reaching `_compact_usage()` (e.g. via an unusual `metavar`) is escaped,
        so `argparse.ArgumentParser._format_usage()`'s own `%`-substitution of the result doesn't raise
        (mirroring `_HelpFormatter._get_help_string()`'s same concern for help text)
        """
        parser = argparse.ArgumentParser(add_help=False)
        action = parser.add_argument("--rate", dest="rate", metavar="50%", required=False)
        usage = _compact_usage("api-client my-app things get-thing", [action])
        assert "%%" in usage
        assert "50%%" in usage


class TestHelpFitsTerminalWidth:
    """Tests that a leaf command's rendered `--help` output actually fits the terminal width it was built
    for, at a representative range of widths - the regression coverage neither the hand-wrapped `help=`
    strings (fixed width, no wrapping at all) nor the original `_wrap_usage_tokens()` (which overflowed once
    `prog` itself grew long enough) had.
    """

    _KNOWN_UNWRAPPED_LINES = (
        "When multiple wrappers are specified, they are chained in the order they appear on the command line.",
    )
    """`add_wrapper_arguments()`'s own argument-group `description=` string, present under --help only
    now that -h collapses the whole group to its own short note. Pre-existing and out of scope here: an
    argparse group description is rendered via `_fill_text()`, which `_HelpFormatter`'s own
    `_split_lines()` override (unlike `box_text()`, which sizes itself dynamically) does not touch, so it
    stays a single unwrapped line exactly as it was before either regression this class guards against."""

    @pytest.mark.parametrize("short", [False, True], ids=["--help", "-h"])
    @pytest.mark.parametrize("columns", [60, 80, 100, 160])
    def test_leaf_help_never_exceeds_the_terminal_width(
        self, columns: int, short: bool, monkeypatch: pytest.MonkeyPatch, cli_client_class: type[CliTestClient]
    ) -> None:
        """Test that every line of `create-widget`'s -h and --help output (parameters, call wrappers,
        options, and the compact usage line) fits within `COLUMNS`, across a representative range of
        terminal widths
        """
        monkeypatch.setenv("COLUMNS", str(columns))
        parser = build_client_parser(cli_client_class, prog="api-client cli-test")
        widgets_parser = get_subparsers_action(parser).choices["widgets"]
        leaf_parser = get_subparsers_action(widgets_parser).choices["create-widget"]

        help_text = remove_color_code(leaf_parser.format_help(short=short))

        overflowing = [
            line
            for line in help_text.splitlines()
            if len(line) > columns and line.strip() not in self._KNOWN_UNWRAPPED_LINES
        ]
        assert overflowing == []

    def test_box_text_description_stays_unaffected_by_help_wrapping(
        self, monkeypatch: pytest.MonkeyPatch, cli_client_class: type[CliTestClient]
    ) -> None:
        """Test that the boxed endpoint description (`description=`, rendered via `_fill_text()`) still
        renders at its own dynamically-computed width and isn't mangled by `_HelpFormatter._split_lines()`,
        which only re-wraps `help=` text
        """
        monkeypatch.setenv("COLUMNS", "60")
        parser = build_client_parser(cli_client_class, prog="api-client cli-test")
        widgets_parser = get_subparsers_action(parser).choices["widgets"]
        leaf_parser = get_subparsers_action(widgets_parser).choices["create-widget"]

        help_text = remove_color_code(leaf_parser.format_help())
        box_lines = [line for line in help_text.splitlines() if line.startswith(("┌", "│", "└"))]

        assert box_lines
        assert len({len(line) for line in box_lines}) == 1
        assert len(box_lines[0]) == 60

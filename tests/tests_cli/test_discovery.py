"""Unit tests for `api_client_core.cli.discovery`"""

import sys
from pathlib import Path
from typing import Any

import pytest
from pytest import CaptureFixture
from pytest_mock import MockerFixture

from api_client_core.base import APIClient, BaseAPI
from api_client_core.cli import discovery
from api_client_core.cli._stdout import reserve_stdout
from api_client_core.cli.discovery import (
    discover_clients,
    discover_resources,
    endpoints_for,
    ensure_project_on_sys_path,
    find_client,
)
from examples.dummyjson.client import DummyJSONClient

from .conftest import CliTestClient, GadgetsAPI, WidgetsAPI


class TestDiscoverResources:
    """Tests for `discover_resources()`"""

    def test_discover_resources_resolves_cached_property_annotation(
        self, cli_client_class: type[CliTestClient], widgets_api_class: type[WidgetsAPI]
    ) -> None:
        """Test that discover_resources() resolves a cached_property-based resource from its return annotation"""
        resources = discover_resources(cli_client_class)
        assert resources["widgets"] is widgets_api_class

    def test_discover_resources_resolves_property_annotation(
        self, cli_client_class: type[CliTestClient], gadgets_api_class: type[GadgetsAPI]
    ) -> None:
        """Test that discover_resources() resolves a plain property-based resource via its .fget function"""
        resources = discover_resources(cli_client_class)
        assert resources["gadgets"] is gadgets_api_class

    def test_discover_resources_excludes_non_api_descriptors(self) -> None:
        """Test that a resolvable non-BaseAPI return annotation is excluded, without affecting the other
        legitimate resources on the same client
        """

        class OtherClient(CliTestClient):
            @property
            def not_an_api(self) -> int:
                return 1

        resources = discover_resources(OtherClient)
        assert "not_an_api" not in resources
        assert "widgets" in resources

    def test_discover_resources_logs_a_debug_message_for_an_excluded_descriptor(self, mocker: MockerFixture) -> None:
        """Test that a resolvable non-BaseAPI return annotation is skipped with a `debug` message naming it,
        so `--log-level DEBUG` can distinguish "not a resource on purpose" from a resource whose descriptor
        was never picked up at all (e.g. one assigned as a plain instance attribute rather than exposed
        through a property)
        """

        class OtherClient(CliTestClient):
            @property
            def not_an_api(self) -> int:
                return 1

        mock_log = mocker.patch.object(discovery, "logger")
        discover_resources(OtherClient)
        matching_calls = [call for call in mock_log.debug.call_args_list if "not_an_api" in call[0][0]]
        assert len(matching_calls) == 1

    def test_discover_resources_warns_and_skips_an_unresolvable_annotation(self, mocker: MockerFixture) -> None:
        """Test that a descriptor whose return annotation can't be resolved statically (e.g. a forward
        reference to a locally scoped class, not visible in the defining module's globals) is skipped with
        a warning naming the resource, rather than recovered by instantiating the client, since discovery
        must never construct one
        """

        class LocalGadgetsAPI(BaseAPI):
            app_name = "local-test"

        class LocalClient(APIClient):
            app_name = "local-test"

            @property
            def gadgets(self) -> "LocalGadgetsAPI":
                return LocalGadgetsAPI(self)

        mock_log = mocker.patch.object(discovery, "logger")
        resources = discover_resources(LocalClient)
        assert "gadgets" not in resources
        matching_calls = [call for call in mock_log.warning.call_args_list if "gadgets" in call[0][0]]
        assert len(matching_calls) == 1


class TestEndpointsFor:
    """Tests for `endpoints_for()`"""

    def test_endpoints_for_derives_when_not_initialized(self, widgets_api_class: type[WidgetsAPI]) -> None:
        """Test that endpoints_for() derives the endpoint list when BaseAPI.init() was never called"""
        assert widgets_api_class.endpoints is None
        endpoints = endpoints_for(widgets_api_class)
        func_names = {e.func_name for e in endpoints}
        assert {"get_widget", "create_widget", "upload_avatar", "list_widgets"} <= func_names

    def test_endpoints_for_uses_populated_list(self, widgets_api_class: type[WidgetsAPI]) -> None:
        """Test that endpoints_for() returns api_class.endpoints directly when already populated"""
        sentinel = [widgets_api_class.get_widget.endpoint]
        widgets_api_class.endpoints = sentinel
        try:
            assert endpoints_for(widgets_api_class) is sentinel
        finally:
            widgets_api_class.endpoints = None

    def test_endpoints_for_does_not_inherit_a_base_classs_stale_populated_list(
        self, widgets_api_class: type[WidgetsAPI]
    ) -> None:
        """Test that a subclass whose own `endpoints` was never populated derives its own endpoints fresh
        from its `EndpointHandler` descriptors - here, all inherited from its base class, since it declares
        none of its own - rather than inheriting its base class's already-populated (and here, deliberately
        stale) `endpoints` list through plain attribute lookup. `endpoints` is an ordinary class attribute,
        not one scoped per-class, so a subclass `BaseAPI.init()` never processed directly (e.g. one defined
        outside the discovered `api` module tree) would otherwise resolve to its base's populated list
        as-is, whatever it happens to hold, rather than freshly deriving its own
        """
        stale = [widgets_api_class.get_widget.endpoint]
        widgets_api_class.endpoints = stale
        try:

            class SubWidgetsAPI(WidgetsAPI):
                pass

            assert "endpoints" not in SubWidgetsAPI.__dict__
            endpoints = endpoints_for(SubWidgetsAPI)
            assert endpoints is not stale
            func_names = {e.func_name for e in endpoints}
            assert {"get_widget", "create_widget", "upload_avatar", "list_widgets"} <= func_names
        finally:
            widgets_api_class.endpoints = None


class TestDiscoverClients:
    """Tests for `discover_clients()`"""

    def test_discovers_the_example_client_by_its_app_name(self) -> None:
        """Test that discover_clients() finds the real examples/dummyjson client when run from the
        repo root, with zero configuration
        """
        clients = discover_clients()
        assert clients.get("DummyJSON") is DummyJSONClient

    def test_skips_a_leaf_candidate_that_declares_no_app_name(self) -> None:
        """Test that a leaf candidate declaring no `app_name` class attribute is skipped and reported,
        rather than raising, so it doesn't block discovery of the rest
        """

        class UndeclaredClient(APIClient):
            pass

        result = discovery.discover_clients_with_failures()
        assert any("UndeclaredClient" in qualname for qualname in result.unnamed_clients)
        assert not any(client_class is UndeclaredClient for client_class in result.clients.values())

    def test_app_name_collision_deterministically_keeps_the_first_by_module_and_qualname(self) -> None:
        """Test that a duplicate app name is resolved deterministically (sorted by module, then
        qualified name), rather than depending on `get_subclasses()`'s set iteration order
        """

        class AaaClient(APIClient):
            app_name = "sorted-dup-test"

        class ZzzClient(APIClient):
            app_name = "sorted-dup-test"

        clients = discover_clients()
        assert clients["sorted-dup-test"] is AaaClient

    def test_two_leaves_inheriting_the_same_declared_app_name_from_a_shared_base_collide(self) -> None:
        """Test that two leaf classes inheriting the same `app_name` from a shared base (neither declaring
        its own) collide exactly like two leaves each declaring the same literal value, deterministically
        keeping the first by module/qualname
        """

        class SharedBaseClient(APIClient):
            app_name = "shared-base-dup-test"

        class AaaLeafClient(SharedBaseClient):
            pass

        class ZzzLeafClient(SharedBaseClient):
            pass

        clients = discover_clients()
        assert clients["shared-base-dup-test"] is AaaLeafClient

    def test_app_name_collision_logs_a_warning_naming_the_dropped_candidate(self, mocker: MockerFixture) -> None:
        """Test that a duplicate app name logs a warning naming both the kept and dropped classes.

        Other tests in this class define their own duplicate-app-name candidates as locally scoped
        classes; since a class's own `__mro__` self-reference keeps it alive until the next cyclic
        GC pass, those may still be discovered (and warned about) here too. This only asserts on the
        warning for this test's own app name, ignoring any others in the call list.
        """

        class FirstClient(APIClient):
            app_name = "warn-dup-test"

        class SecondClient(APIClient):
            app_name = "warn-dup-test"

        mock_log = mocker.patch.object(discovery, "logger")
        discover_clients()
        matching_calls = [call for call in mock_log.warning.call_args_list if "warn-dup-test" in call[0][0]]
        assert len(matching_calls) == 1
        message = matching_calls[0][0][0]
        assert "FirstClient" in message
        assert "SecondClient" in message

    def test_skipping_a_candidate_with_no_app_name_logs_a_debug_message(self, mocker: MockerFixture) -> None:
        """Test that skipping a leaf candidate declaring no `app_name` logs a debug message naming it, so a
        client that silently fails to be discovered stays diagnosable via DEBUG logging
        """

        class NoAppNameDebugClient(APIClient):
            pass

        mock_log = mocker.patch.object(discovery, "logger")
        discover_clients()
        matching_calls = [call for call in mock_log.debug.call_args_list if "NoAppNameDebugClient" in call[0][0]]
        assert len(matching_calls) == 1

    def test_logs_a_no_app_name_debug_message_even_when_another_module_failed_to_import(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture
    ) -> None:
        """Test that a candidate's own no-`app_name` debug message is still logged even when an unrelated
        module has already failed to import during the same run, since the two are independent: an import
        failure elsewhere says nothing about whether *this* candidate declares an `app_name`, and both
        `find_client()`'s and `build_client_parser()`'s own error messages already point a user at
        `--log-level DEBUG` to see exactly this diagnostic
        """

        class NoAppNameWithImportFailureClient(APIClient):
            pass

        pkg = tmp_path / "brokenpkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "bad_module.py").write_text("raise RuntimeError('simulated failure')\n")

        monkeypatch.chdir(tmp_path)
        mock_log = mocker.patch.object(discovery, "logger")
        discover_clients()
        assert any("NoAppNameWithImportFailureClient" in call[0][0] for call in mock_log.debug.call_args_list)

    def test_excludes_an_intermediate_base_class_that_has_a_subclass(self) -> None:
        """Test that an `APIClient` subclass with a subclass of its own (e.g. a project's own
        `OpenAPIClient`-style base, meant only to be subclassed further, declaring no `app_name`) is excluded
        as a candidate, while its concrete leaf subclass, which declares its own `app_name`, is still
        discovered normally
        """

        class IntermediateBaseClient(APIClient):
            pass

        class ConcreteLeafClient(IntermediateBaseClient):
            app_name = "intermediate-base-test"

        clients = discover_clients()
        assert clients.get("intermediate-base-test") is ConcreteLeafClient

    def test_excluding_an_intermediate_base_class_logs_no_debug_message(self, mocker: MockerFixture) -> None:
        """Test that an intermediate base class with a subclass of its own is excluded silently, unlike a
        genuinely unnamed leaf candidate (see `test_skipping_a_candidate_with_no_app_name_logs_a_debug_message`),
        since it declaring no `app_name` of its own is expected, not diagnostic
        """

        class QuietIntermediateBaseClient(APIClient):
            pass

        class QuietConcreteLeafClient(QuietIntermediateBaseClient):
            app_name = "quiet-intermediate-base-test"

        mock_log = mocker.patch.object(discovery, "logger")
        discover_clients()
        assert not any("QuietIntermediateBaseClient" in call[0][0] for call in mock_log.debug.call_args_list)


class TestImportModuleTree:
    """Tests for `_import_module_tree()`"""

    def test_does_not_import_a_nested_skipped_directory(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that a nested `tests/` package is neither imported nor recursed into, so a client
        defined only there isn't discovered, and importing the containing package doesn't pull in a
        whole nested test suite as a discovery side effect
        """
        pkg = tmp_path / "skipdirpkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        nested = pkg / "tests"
        nested.mkdir()
        (nested / "__init__.py").write_text("raise RuntimeError('should never be imported')\n")
        (nested / "client.py").write_text(
            "from api_client_core import APIClient\nclass HiddenClient(APIClient):\n    app_name = 'hidden-test'\n"
        )

        monkeypatch.chdir(tmp_path)
        clients = discover_clients()
        assert "hidden-test" not in clients
        assert "skipdirpkg.tests" not in sys.modules

    def test_import_failure_logs_only_a_debug_message_on_a_successful_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture
    ) -> None:
        """Test that a submodule failing to import logs only a `debug` message naming it, rather than a
        `warning`, so a project with an unimportable module (e.g. one behind an optional dependency)
        doesn't get a discovery diagnostic printed before every command's own output on a run that
        otherwise succeeds. `find_client()` surfaces the same failure at full visibility, but only once a
        lookup actually fails (see `test_lookup_error_names_a_module_that_failed_to_import` below)
        """
        pkg = tmp_path / "brokenpkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "bad_module.py").write_text("raise RuntimeError('simulated failure')\n")

        monkeypatch.chdir(tmp_path)
        mock_log = mocker.patch.object(discovery, "logger")
        discover_clients()
        assert not any("brokenpkg.bad_module" in call[0][0] for call in mock_log.warning.call_args_list)
        matching_calls = [call for call in mock_log.debug.call_args_list if "brokenpkg.bad_module" in call[0][0]]
        assert len(matching_calls) == 1

    def test_records_a_system_exit_at_import_instead_of_propagating_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that a top-level module raising SystemExit at import time (e.g. a stray script calling
        sys.exit()) is recorded as a failed import like any other, rather than propagating out of discovery
        and terminating the whole process with that module's own exit code
        """
        (tmp_path / "exits_at_import.py").write_text("import sys\nsys.exit(3)\n")

        monkeypatch.chdir(tmp_path)
        result = discovery.discover_clients_with_failures()
        assert any("exits_at_import: SystemExit: 3" in f for f in result.import_failures)

    def test_records_a_system_exit_from_a_nested_submodule_at_import(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that a SystemExit raised while importing a nested submodule, not just a top-level module, is
        recorded the same way, and doesn't stop the rest of that package's own submodules from being scanned
        """
        pkg = tmp_path / "exitpkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "bad.py").write_text("raise SystemExit(1)\n")
        (pkg / "good.py").write_text(
            "from api_client_core import APIClient\n\n\nclass StillFoundClient(APIClient):\n"
            "    app_name = 'still-found'\n"
        )

        monkeypatch.chdir(tmp_path)
        result = discovery.discover_clients_with_failures()
        assert any("exitpkg.bad: SystemExit: 1" in f for f in result.import_failures)
        assert "still-found" in result.clients

    def test_keyboard_interrupt_at_import_still_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that a genuine KeyboardInterrupt raised at import time still propagates out of discovery,
        rather than being swallowed the way every other BaseException now is
        """
        (tmp_path / "interrupts_at_import.py").write_text("raise KeyboardInterrupt\n")

        monkeypatch.chdir(tmp_path)
        with pytest.raises(KeyboardInterrupt):
            discovery.discover_clients_with_failures()


class TestFindClient:
    """Tests for `find_client()`"""

    def test_raises_lookup_error_listing_known_app_names(self) -> None:
        """Test that an unknown app name raises LookupError listing the app names that were found"""
        try:
            find_client("not-a-registered-app-name")
        except LookupError as e:
            assert "not-a-registered-app-name" in str(e)
            assert "DummyJSON" in str(e)
        else:
            raise AssertionError("find_client() did not raise LookupError")

    def test_find_client_resolves_the_real_example_client(self) -> None:
        """Test that find_client() resolves the real examples/dummyjson client by its declared app name"""
        client_class = find_client("DummyJSON")
        assert client_class is DummyJSONClient

    def test_find_client_never_constructs_the_resolved_class(self) -> None:
        """Test that a successful lookup never constructs the resolved class, since discovery is purely
        static and resolves an app name entirely from the class attribute
        """
        constructed = []

        class SideEffectClient(APIClient):
            app_name = "side-effect-test"

            def __init__(self, **kwargs: Any) -> None:
                constructed.append(self)
                super().__init__(**kwargs)

        client_class = find_client("side-effect-test")
        assert client_class is SideEffectClient
        assert constructed == []

    def test_lookup_error_names_a_candidate_declaring_no_app_name(self) -> None:
        """Test that an unknown app name's `LookupError` also names every candidate class that declares no
        `app_name`, so a client hidden behind a missing `app_name` isn't indistinguishable from a plain typo
        """

        class LookupNoAppNameClient(APIClient):
            pass

        with pytest.raises(LookupError, match="LookupNoAppNameClient"):
            find_client("not-a-registered-app-name")

    def test_lookup_error_names_a_module_that_failed_to_import(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that an unknown app name's `LookupError` also names every module that failed to import
        during discovery and the real exception each raised, so a client hidden behind one of those isn't
        silently indistinguishable from a plain typo in the app name
        """
        pkg = tmp_path / "brokenpkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "bad_module.py").write_text("raise RuntimeError('simulated failure')\n")

        monkeypatch.chdir(tmp_path)
        with pytest.raises(LookupError, match=r"brokenpkg\.bad_module: RuntimeError: simulated failure"):
            find_client("not-a-registered-app-name")

    def test_lookup_error_caps_a_long_list_of_import_failures(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that an unknown app name's `LookupError` caps its import-failure list rather than printing a
        wall of text when many modules fail to import, appending an `... and N more` summary for the rest
        """
        shown = discovery._MAX_DISCOVERY_FAILURES_SHOWN
        total = shown + 3
        for i in range(total):
            pkg = tmp_path / f"brokenpkg{i}"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("raise RuntimeError('simulated failure')\n")

        monkeypatch.chdir(tmp_path)
        with pytest.raises(LookupError) as exc_info:
            find_client("not-a-registered-app-name")
        message = str(exc_info.value)
        assert message.count("RuntimeError: simulated failure") == shown
        assert f"... and {total - shown} more" in message


class TestStdoutRedirection:
    """Tests pinning `discover_clients_with_failures()`'s behavior under the process-wide
    `reserve_stdout()` reservation (reproduced via an explicit `with reserve_stdout():`, since a real run
    opens it once in `_entrypoint.py`'s `main()`, well before discovery runs): downstream project code it
    imports never reaches the real stdout
    """

    def test_a_downstream_setup_logging_binds_to_stderr_for_good(
        self,
        downstream_setup_logging_project: Path,
        capsys: CaptureFixture[str],
        _restore_logging_state: None,
    ) -> None:
        """Test the reported bug's root cause end to end: a project module reconfiguring
        `api_client_core`'s own logger straight to `ext://sys.stdout` at import time binds it to stderr
        instead, because `discover_clients_with_failures()` runs the import while the reservation points
        `sys.stdout` at `sys.stderr`. A line logged after `discover_clients()` has already returned, and the
        reservation has already closed, is still on stderr, since the handler bound to a stream object
        rather than to the name `sys.stdout`
        """
        with reserve_stdout():
            discover_clients()
        discovery.logger.debug("logged after discover_clients() returned")

        out, err = capsys.readouterr()
        assert out == ""
        assert "Skipping NoAppNameClient: no 'app_name' class attribute is set" in err
        assert "logged after discover_clients() returned" in err

    def test_a_stray_print_during_project_import_lands_on_stderr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: CaptureFixture[str]
    ) -> None:
        """Test that a project module's own `print()` at import time lands on stderr rather than the
        real stdout
        """
        pkg = tmp_path / "stray_print_pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("print('stray import-time write')\n")
        monkeypatch.chdir(tmp_path)

        with reserve_stdout():
            discover_clients()

        out, err = capsys.readouterr()
        assert out == ""
        assert "stray import-time write" in err


class TestEnsureProjectOnSysPath:
    """Tests for `ensure_project_on_sys_path()`"""

    def test_adds_the_current_working_directory_to_sys_path(self) -> None:
        """Test that the current working directory ends up on sys.path"""
        ensure_project_on_sys_path()
        assert str(Path.cwd()) in sys.path

    def test_is_idempotent(self) -> None:
        """Test that calling it repeatedly doesn't duplicate the entry on sys.path"""
        ensure_project_on_sys_path()
        ensure_project_on_sys_path()
        assert sys.path.count(str(Path.cwd())) == 1

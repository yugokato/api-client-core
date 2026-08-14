"""Fixtures and shared helpers for the CLI generator's tests.

`WidgetsAPI`/`GadgetsAPI`/`CliTestClient` form a synthetic client exercising every parameter kind
the CLI generator maps: required/optional path and body params, a query param on a non-GET endpoint
(via `Query`), a wire-name alias, a bool with a concrete default, a list, an int `Literal`, an enum,
a file upload, a JSON-fallback dict, and a deprecated endpoint. `GadgetsAPI` is exposed via a plain
`property` (rather than `cached_property`) to exercise the `.fget` discovery path.
"""

from __future__ import annotations

import argparse
import importlib.util
import logging
import sys
from collections.abc import Iterator
from enum import Enum
from functools import cached_property
from pathlib import Path
from typing import Annotated, Any, Literal, Unpack
from uuid import uuid4

import pytest
from common_libs.clients.rest_client.types import Response
from pytest_mock import MockerFixture

from api_client_core import APIClient, BaseAPI, endpoint
from api_client_core.cli.parser import ArgumentParser
from api_client_core.types import Alias, File, Kwargs, Query, RestResponse, Unset


class Status(Enum):
    """Synthetic status enum used to test enum-typed CLI flags."""

    ACTIVE = "active"
    INACTIVE = "inactive"


class WidgetsAPI(BaseAPI):
    """A synthetic API class exercising every parameter kind the CLI generator maps."""

    app_name = "cli-test"

    @endpoint.get("/widgets/{widget_id}")
    def get_widget(self, widget_id: int, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Get a widget by ID"""
        ...

    @endpoint.post("/widgets")
    def create_widget(
        self,
        name: str,
        owner_id: Annotated[int, Alias("ownerId")],
        active: bool = True,
        tags: list[str] = Unset,
        status: Status = Unset,
        priority: Literal[1, 2, 3] = Unset,
        notify: Annotated[bool, Query()] = Unset,
        metadata: dict[str, Any] = Unset,
        **kwargs: Unpack[Kwargs],
    ) -> RestResponse:
        """Create a widget"""
        ...

    @endpoint.post("/widgets/{widget_id}/avatar")
    def upload_avatar(self, widget_id: int, avatar: File, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Upload a widget avatar"""
        ...

    @endpoint.get("/widgets")
    @endpoint.is_deprecated
    def list_widgets(self, limit: int = 10, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """List widgets"""
        ...


class CollisionAPI(BaseAPI):
    """A synthetic API class with endpoint parameters that collide with reserved CLI flags (`--quiet`,
    `-h`/`--help`) or with a control kwarg `run()` passes to every call (`with_hooks`, `raw_options`), used to
    test that each collision is handled consistently by both `add_endpoint_arguments()` and
    `collect_call_kwargs()`.
    """

    app_name = "collision-test"

    @endpoint.post("/things")
    def make_thing(self, name: str, quiet: str = "ok", **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Make a thing"""
        ...

    @endpoint.post("/other-things")
    def make_other_thing(self, name: str, help: str = "ok", **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Make another thing"""
        ...

    @endpoint.post("/hook-things")
    def make_hook_thing(
        self, name: str, with_hooks: str = "ok", raw_options: str = "ok", **kwargs: Unpack[Kwargs]
    ) -> RestResponse:
        """Make a thing with parameters colliding with the `with_hooks`/`raw_options` control kwargs"""
        ...


class CollisionClient(APIClient):
    """A synthetic client exposing `CollisionAPI`, for testing a reserved-flag-colliding endpoint
    parameter end-to-end through `run()`.
    """

    app_name = "collision-test"

    @cached_property
    def things(self) -> CollisionAPI:
        return CollisionAPI(self)


@pytest.fixture
def collision_client_class() -> type[CollisionClient]:
    """The synthetic client exposing a reserved-flag-colliding endpoint parameter."""
    return CollisionClient


class PositionalOnlyAPI(BaseAPI):
    """A synthetic API class exercising a positional-only path parameter, used to test that the CLI (which can
    only produce keyword arguments) can still reach it.
    """

    app_name = "posonly-test"

    @endpoint.get("/items/{item_id}")
    def get_item(self, item_id: int, /, note: str = "x", **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Get an item by ID"""
        ...

    @endpoint.get("/pages")
    def list_pages(self, page: int = 1, size: int = 20, /, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """List pages, with two consecutive positional-only params (the first defaulted), so the CLI must
        gap-fill `page` from its own default to reach `size` given by flag alone
        """
        ...


class PositionalOnlyClient(APIClient):
    """A synthetic client exposing `PositionalOnlyAPI`, for testing a positional-only path param end-to-end
    through `run()`.
    """

    app_name = "posonly-test"

    @cached_property
    def items(self) -> PositionalOnlyAPI:
        return PositionalOnlyAPI(self)


@pytest.fixture
def posonly_client_class() -> type[PositionalOnlyClient]:
    """The synthetic client exposing a positional-only path parameter endpoint."""
    return PositionalOnlyClient


class GadgetsAPI(BaseAPI):
    """A second synthetic API class, exposed via a `property` resource on the client."""

    app_name = "cli-test"

    @endpoint.get("/gadgets/{gadget_id}")
    def get_gadget(self, gadget_id: int, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Get a gadget by ID"""
        ...


class CliTestClient(APIClient):
    """A synthetic client exposing both `cached_property`- and `property`-based API resources."""

    app_name = "cli-test"

    @cached_property
    def widgets(self) -> WidgetsAPI:
        return WidgetsAPI(self)

    @property
    def gadgets(self) -> GadgetsAPI:
        return GadgetsAPI(self)


@pytest.fixture
def cli_client_class() -> type[CliTestClient]:
    """The synthetic CLI test client class."""
    return CliTestClient


@pytest.fixture
def widgets_api_class() -> type[WidgetsAPI]:
    """The synthetic `WidgetsAPI` class."""
    return WidgetsAPI


@pytest.fixture
def gadgets_api_class() -> type[GadgetsAPI]:
    """The synthetic `GadgetsAPI` class."""
    return GadgetsAPI


@pytest.fixture
def status_enum() -> type[Status]:
    """The synthetic `Status` enum."""
    return Status


@pytest.fixture
def project_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A minimal project directory, chdir'd into, for `project_roots()`/`cache_key()` to scan.

    Marked with a `pyproject.toml` so `find_project_root()` resolves it as the project root, matching a
    real project rather than relying on the no-marker fallback to `Path.cwd()`.
    """
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").touch()
    (project / "app.py").write_text("x = 1\n")
    monkeypatch.chdir(project)
    return project


@pytest.fixture
def cache_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated cache home, so cache tests never touch the real user cache directory.

    `_cache._cache_dir()` reads `%LOCALAPPDATA%` on Windows and `$XDG_CACHE_HOME` everywhere else, so this
    sets whichever one `sys.platform` actually resolves to on the machine running the test, matching
    `_cache._cache_dir()`'s own platform check.
    """
    cache_home = tmp_path / "cache-home"
    env_var = "LOCALAPPDATA" if sys.platform == "win32" else "XDG_CACHE_HOME"
    monkeypatch.setenv(env_var, str(cache_home))
    return cache_home


@pytest.fixture(autouse=True)
def _pin_terminal_width(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin `$COLUMNS` to 80, matching `get_terminal_width()`'s own fallback, so every test that renders
    help/usage/box text is deterministic regardless of the real terminal or `pytest -s` disabling output
    capture (which otherwise lets `shutil.get_terminal_size()` see a real tty instead of always falling
    back). A test exercising a specific width still overrides this with its own `monkeypatch.setenv`.
    """
    monkeypatch.setenv("COLUMNS", "80")


@pytest.fixture(autouse=True)
def _restore_logging_state() -> Iterator[None]:
    """Snapshot and restore logging state so a test's own `dictConfig` call doesn't leak into others.

    Autouse: `run()`/`dispatch()` call `setup_logging()` on every real dispatch, which binds a handler
    straight to the `sys.stderr` object active at that moment (e.g. `capsys`'s own capture buffer). Left
    unrestored, that stale, possibly-closed stream reference survives into the next test and raises there
    the next time anything logs through it, so every test in this package needs the same cleanup, not just
    the few that call `setup_logging()` directly. Also covers `common_libs`, which `api_client_core`'s
    `setup_logging()` mirrors the `api_client_core` logger's own config onto.
    """
    loggers = [logging.getLogger(), logging.getLogger("api_client_core"), logging.getLogger("common_libs")]
    snapshot = {logger: (logger.level, logger.propagate, list(logger.handlers), logger.disabled) for logger in loggers}
    yield
    for logger, (level, propagate, handlers, disabled) in snapshot.items():
        logger.setLevel(level)
        logger.propagate = propagate
        logger.handlers = handlers
        logger.disabled = disabled


@pytest.fixture
def downstream_setup_logging_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp project, chdir'd into, reproducing the reported bug's root cause: a top-level package whose
    `__init__.py` reconfigures `api_client_core`'s own logger straight to `ext://sys.stdout` at import
    time, mirroring what a downstream project's own `setup_logging()` call does at import time, alongside
    an `APIClient` subclass declaring no `app_name`, so `discover_clients_with_failures()`'s own
    `Skipping <client>: no 'app_name' class attribute is set` debug log fires right after, through the
    logger the package's own import just reconfigured.

    The package name is unique per call (rather than a fixed name), since `importlib.import_module()`
    is a no-op on a name already in `sys.modules`: a fixed name would only ever run its `__init__.py` the
    first time this fixture is used across the whole test session.
    """
    pkg = tmp_path / f"downstream_pkg_{uuid4().hex}"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "from logging.config import dictConfig\n\n"
        "dictConfig({\n"
        "    'version': 1,\n"
        "    'disable_existing_loggers': False,\n"
        "    'handlers': {'h': {'class': 'logging.StreamHandler', 'stream': 'ext://sys.stdout'}},\n"
        "    'loggers': {'api_client_core': {'level': 'DEBUG', 'handlers': ['h'], 'propagate': False}},\n"
        "})\n"
    )
    (pkg / "client.py").write_text(
        "from api_client_core import APIClient\n\n\nclass NoAppNameClient(APIClient):\n    pass\n"
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def module_scoped(cls: type) -> type:
    """Bind a locally scoped class into its defining module's globals, as a decorator.

    `discover_resources()` resolves a `cached_property`/`property` return annotation via `get_type_hints()`,
    which evaluates a deferred annotation string (every annotation in a module importing `from __future__
    import annotations`, as every test module here does) against the defining function's `__globals__` only,
    never an enclosing function's locals. A `BaseAPI` subclass defined inside a test function and returned by
    a client's resource property is otherwise unresolvable for exactly that reason. Apply directly above such
    a class's definition.

    :param cls: The class to make resolvable from its own module's global namespace
    """
    sys.modules[cls.__module__].__dict__[cls.__name__] = cls
    return cls


def get_subparsers_action(parser: argparse.ArgumentParser) -> argparse._SubParsersAction[ArgumentParser]:
    """Return the `_SubParsersAction` added to a parser (there is exactly one, added by build_parser).

    Typed as returning our own `ArgumentParser`, not the stdlib base class, since every subparser this
    codebase builds is one, and `.choices[...]` off the result is where a test reaches for
    `format_help(short=...)`/`print_help(short=...)`, which only exists on the subclass.
    """
    return next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))


def find_group_title(parser: argparse.ArgumentParser, flag: str) -> str | None:
    """Return the title of the argument group that owns the action for `flag` on `parser`, or `None`
    if no action defines it.

    :param parser: Parser (or leaf subparser) to search
    :param flag: Option string to look up (e.g. `-h`, `--quiet`)
    """
    for group in parser._action_groups:
        for action in group._group_actions:
            if flag in action.option_strings:
                return group.title
    return None


def patch_argcomplete_installed(mocker: MockerFixture, *, installed: bool) -> None:
    """Patch `importlib.util.find_spec` so `argcomplete`'s installed state is deterministic for the test.

    Delegates to the real `find_spec` for every other module name, so this only affects lookups of
    `argcomplete` itself rather than breaking import machinery other code relies on during the test.

    :param mocker: `pytest-mock` fixture used to install the patch
    :param installed: Whether `argcomplete` should appear installed to `builder.tab_completion_hint()`
    """
    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "argcomplete":
            return object() if installed else None
        return real_find_spec(name, *args, **kwargs)

    mocker.patch("api_client_core.cli.builder.importlib.util.find_spec", side_effect=fake_find_spec)


def make_httpx_response(mocker: MockerFixture, status_code: int, *, json_body: Any = None, text: str = "") -> Response:
    """Build a minimal mocked httpx2 `Response` with the given status code and JSON/text body.

    :param mocker: pytest-mock fixture
    :param status_code: HTTP status code the mocked response reports
    :param json_body: Value returned by the mocked response's `.json()`. Defaults to `{}`
    :param text: Value for the mocked response's `.text`, used as a fallback body
    """
    r = mocker.MagicMock(spec=Response)
    r.status_code = status_code
    r.is_success = status_code < 300
    r.reason_phrase = "OK" if status_code < 300 else "Not Found"
    r.headers = {}
    r.content = b"{}"
    r.is_stream = False
    r.elapsed = mocker.MagicMock()
    r.elapsed.total_seconds.return_value = 0.0
    r.json.return_value = {} if json_body is None else json_body
    r.text = text
    r.request = mocker.MagicMock()
    r.request.request_id = "test-request-id"
    r.request.method = "GET"
    r.request.url = "https://example.com/api/widgets/1"
    return r


def make_rest_response(
    mocker: MockerFixture, status_code: int, json_body: Any = None, *, text: str = ""
) -> RestResponse:
    """Build a real `RestResponse` wrapping a mocked httpx2 `Response`.

    :param mocker: pytest-mock fixture
    :param status_code: HTTP status code the mocked response reports
    :param json_body: Value returned by the mocked response's `.json()`. Defaults to `{}`
    :param text: Value for the mocked response's `.text`, used as a fallback body
    """
    return RestResponse(_response=make_httpx_response(mocker, status_code, json_body=json_body, text=text))

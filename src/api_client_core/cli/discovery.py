from __future__ import annotations

import importlib
import inspect
import pkgutil
import sys
from functools import cached_property
from pathlib import Path
from types import ModuleType
from typing import Any, NamedTuple, cast, get_type_hints

from common_libs.logging import get_logger

from api_client_core.base import APIClient, BaseAPI
from api_client_core.base.api_class import get_endpoints, get_subclasses
from api_client_core.endpoints import Endpoint

from ._paths import is_own_package_dir, is_skipped_dir, is_venv_dir, project_roots

logger = get_logger(__name__)

_MAX_DISCOVERY_FAILURES_SHOWN = 10


class DiscoveryResult(NamedTuple):
    """Result of one discovery scan.

    `import_failures` and `unnamed_clients` are kept apart rather than merged into one list: an import
    failure (an arbitrary module that couldn't even be imported) says nothing about whether it hid an
    `APIClient` subclass, while an unnamed client (only ever recorded for an actual `APIClient` subclass that
    was found and imported, but declares no `app_name`) is positive proof that one did. Callers that only
    care whether discovery hit any trouble at all (e.g. deciding whether to show a "you're probably in the
    wrong directory" hint) need that distinction; callers reporting failures to the user (e.g.
    `find_client()`'s `LookupError`) show both.

    :param clients: Discovered `APIClient` subclasses, keyed by app name
    :param import_failures: One description per module that failed to import
    :param unnamed_clients: One qualname per leaf candidate class that declares no `app_name`
    """

    clients: dict[str, type[APIClient]]
    import_failures: list[str]
    unnamed_clients: list[str]


def discover_clients() -> dict[str, type[APIClient]]:
    """Discover every `APIClient` subclass importable from the current project, keyed by its app name."""
    return discover_clients_with_failures().clients


def discover_clients_with_failures() -> DiscoveryResult:
    """Discover every `APIClient` subclass importable from the current project, keyed by its app name, along
    with a description of every module that failed to import, and every candidate class that declares no
    `app_name`, along the way.

    Only leaf classes (those with no subclass of their own) are treated as candidates, so an intermediate
    base class meant only to be subclassed further (which typically declares no `app_name` of its own) is
    never reported as an unnamed client. Candidates are resolved in a deterministic order, so the same app
    name always resolves to the same class if more than one candidate would otherwise match.
    """
    import_failures: list[str] = []
    for module_name in _iter_project_modules():
        _import_module_tree(module_name, import_failures)

    candidates = sorted(
        (c for c in get_subclasses(APIClient) if not c.__subclasses__() and not c.__name__.startswith("_")),
        key=lambda c: (c.__module__, c.__qualname__),
    )
    clients: dict[str, type[APIClient]] = {}
    unnamed_clients: list[str] = []
    for candidate in candidates:
        app_name = candidate.app_name
        if not app_name:
            logger.debug(f"Skipping {candidate.__qualname__}: no 'app_name' class attribute is set")
            unnamed_clients.append(candidate.__qualname__)
            continue
        if app_name in clients:
            logger.warning(
                f"Multiple API clients resolve to app name {app_name!r}: keeping "
                f"{clients[app_name].__qualname__}, ignoring {candidate.__qualname__}"
            )
            continue
        clients[app_name] = candidate
    return DiscoveryResult(clients, import_failures, unnamed_clients)


def find_client(app_name: str) -> type[APIClient]:
    """Resolve an app name to its discovered `APIClient` subclass.

    If `app_name` isn't found, the raised `LookupError` also names every module that failed to import, and
    every candidate class that declares no `app_name`, during discovery, so a client hidden behind either
    kind of failure isn't indistinguishable from a plain typo.

    :param app_name: App name to resolve
    """
    result = discover_clients_with_failures()
    try:
        return result.clients[app_name]
    except KeyError:
        known_app_names = ", ".join(sorted(result.clients)) if result.clients else "(none found)"
        message = f"No API client found for app name {app_name!r}. Discovered app names: {known_app_names}"
        if result.import_failures:
            message += (
                f"\n{len(result.import_failures)} module(s) failed to import and were skipped during discovery:\n"
                f"{format_discovery_failures(result.import_failures)}"
            )
        if result.unnamed_clients:
            message += (
                f"\n{len(result.unnamed_clients)} API client class(es) has no 'app_name' and were skipped "
                f"during discovery:\n{format_discovery_failures(result.unnamed_clients)}"
            )
        raise LookupError(message)


def discover_resources(client_class: type[APIClient]) -> dict[str, type[BaseAPI[Any]]]:
    """Discover every API class exposed on a client, keyed by its attribute name.

    Walks the client class's `cached_property`/`property` descriptors and keeps those whose resolved return
    type is a `BaseAPI` subclass. A descriptor whose return annotation can't be resolved statically (e.g. the
    return type is only imported under `TYPE_CHECKING`) is skipped with a warning rather than recovered by
    instantiating the client, since discovery must never construct one.

    :param client_class: Concrete `APIClient` subclass to introspect
    """
    resources: dict[str, type[BaseAPI[Any]]] = {}
    seen: set[str] = set()
    for klass in client_class.__mro__:
        for name, descr in vars(klass).items():
            if name in seen or not isinstance(descr, cached_property | property):
                continue
            seen.add(name)
            func = getattr(descr, "func", None) or getattr(descr, "fget", None)
            if func is None:
                continue
            try:
                hints = get_type_hints(func)
            except (NameError, TypeError, AttributeError) as e:
                logger.warning(
                    f"Skipping resource {name!r} on {client_class.__qualname__}: its return annotation "
                    f"couldn't be resolved ({type(e).__name__}: {e}). Import the return type at runtime "
                    f"rather than only under TYPE_CHECKING for it to be discoverable."
                )
                continue
            return_type = hints.get("return")
            if inspect.isclass(return_type) and issubclass(return_type, BaseAPI):
                resources[name] = return_type
            else:
                logger.debug(
                    f"Skipping {name!r} on {client_class.__qualname__}: its return annotation "
                    f"({return_type!r}) doesn't resolve to a BaseAPI subclass."
                )

    return resources


def endpoints_for(api_class: type[BaseAPI[Any]]) -> list[Endpoint[Any]]:
    """Return every `Endpoint` defined on an API class, in no particular order.

    Uses the class's own already-populated endpoint list when available, checked via `__dict__` rather than
    a plain attribute read so a class that never populated its own list doesn't inherit an unrelated one from
    a base class. Callers that need a stable order are responsible for sorting it themselves.

    :param api_class: API class to introspect
    """
    endpoints = api_class.__dict__.get("endpoints")
    if endpoints is not None:
        return cast(list[Endpoint[Any]], endpoints)

    return get_endpoints(api_class)


def format_discovery_failures(failures: list[str]) -> str:
    """Render a discovery-failure list (a module that failed to import, or a candidate class that declares no
    `app_name`) for an error message, capped to avoid a wall of text.

    :param failures: Failure descriptions, one per module or candidate class
    """
    shown = failures[:_MAX_DISCOVERY_FAILURES_SHOWN]
    lines = "\n".join(f"  - {f}" for f in shown)
    remaining = len(failures) - len(shown)
    if remaining:
        lines += f"\n  ... and {remaining} more"
    return lines


def ensure_project_on_sys_path() -> None:
    """Prepend the project's root (and its `src/` subdirectory, if present) to `sys.path`.

    A console-script entry point, unlike `python -m`, doesn't automatically put the current directory on
    `sys.path`, so a client living in the current project wouldn't otherwise be importable. Safe to call on
    every invocation.
    """
    for root in project_roots():
        root_str = str(root)
        if root_str not in sys.path:
            sys.path.insert(0, root_str)


def _iter_project_modules() -> list[str]:
    """List every top-level, importable module/package name found under the project root.

    Common non-source directories, dunder/private names, and dotted names are skipped, as is this
    framework's own installed package (by resolved path, not name, so a differently-purposed project
    directory that happens to share that literal name isn't skipped too), and a virtual environment's own
    root regardless of its name (so a venv named e.g. `env/` isn't imported wholesale).
    """
    ensure_project_on_sys_path()

    names: list[str] = []
    seen: set[str] = set()
    for root in project_roots():
        for module_info in pkgutil.iter_modules(path=[str(root)]):
            name = module_info.name
            if name in seen or is_skipped_dir(name) or is_own_package_dir(root / name) or is_venv_dir(root / name):
                continue
            seen.add(name)
            names.append(name)
    return names


def _import_module_tree(module_name: str, failures: list[str]) -> None:
    """Import a top-level module, and every non-skipped submodule beneath it if it's a package.

    Recurses through subpackages manually rather than via `pkgutil.walk_packages`, so an excluded subpackage
    (e.g. a nested `tests/` package) is never imported at all. Import failures are swallowed (via
    `_try_import()`) so one broken or irrelevant module never blocks discovery of the rest.

    :param module_name: Top-level module or package name to import
    :param failures: List collecting one entry per module that failed to import, appended to in place
    """
    module = _try_import(module_name, failures)
    if module is None:
        return

    path = getattr(module, "__path__", None)
    if path is None:
        return

    for module_info in pkgutil.iter_modules(path, prefix=f"{module_name}."):
        leaf_name = module_info.name.rsplit(".", 1)[-1]
        if leaf_name.startswith("_") or (
            module_info.ispkg and (is_skipped_dir(leaf_name) or is_own_package_dir(Path(path[0], leaf_name)))
        ):
            continue
        if _try_import(module_info.name, failures) is None:
            continue
        if module_info.ispkg:
            _import_module_tree(module_info.name, failures)


def _try_import(module_name: str, failures: list[str]) -> ModuleType | None:
    """Import a single module, recording (rather than raising) any failure short of `KeyboardInterrupt`.

    Catches `BaseException`, not `Exception`: a project module that calls `sys.exit()` (or otherwise raises
    `SystemExit`) at import time, or one that raises `argparse.ArgumentParser.exit()`-style `SystemExit` from
    module-level argument parsing, must be recorded as a failed import like any other, rather than silently
    terminating the whole discovery scan (and, on the shell-completion hot path, the calling shell) with that
    module's own exit code.

    :param module_name: Module name to import
    :param failures: List collecting one entry per module that failed to import, appended to in place
    """
    try:
        return importlib.import_module(module_name)
    except KeyboardInterrupt:
        raise
    except BaseException as e:
        logger.debug(f"Skipping module {module_name!r}: import failed: {e}")
        failures.append(f"{module_name}: {type(e).__name__}: {e}")
        return None

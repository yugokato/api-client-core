from importlib import import_module
from logging import NullHandler, getLogger
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from api_client_core.base import APIClient, BaseAPI
    from api_client_core.endpoints import Endpoint, EndpointFunc, Stats, endpoint
    from api_client_core.logging import setup_logging

__all__ = ["APIClient", "BaseAPI", "Endpoint", "EndpointFunc", "Stats", "__version__", "endpoint", "setup_logging"]

_LAZY_ATTRS: Final[dict[str, str]] = {
    "APIClient": "api_client_core.base",
    "BaseAPI": "api_client_core.base",
    "Endpoint": "api_client_core.endpoints",
    "EndpointFunc": "api_client_core.endpoints",
    "Stats": "api_client_core.endpoints",
    "endpoint": "api_client_core.endpoints",
    "setup_logging": "api_client_core.logging",
}


getLogger(__name__).addHandler(NullHandler())


def __getattr__(name: str) -> Any:
    """Lazily import and cache package re-exports (PEP 562).

    This speeds up CLI tab completion by avoiding importing heavyweight runtime dependencies until one of the
    re-exported symbols is actually accessed. `__version__` gets the same treatment, computed here rather
    than at module scope.

    :param name: The attribute being accessed on the package
    """
    if name == "__version__":
        from importlib.metadata import PackageNotFoundError, version

        try:
            value = version("api-client-core")
        except PackageNotFoundError:
            value = "unknown"
    else:
        try:
            module_name = _LAZY_ATTRS[name]
        except KeyError:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
        value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)

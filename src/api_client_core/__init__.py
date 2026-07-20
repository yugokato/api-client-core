from importlib.metadata import PackageNotFoundError, version

from api_client_core.base import APIClient, BaseAPI
from api_client_core.endpoints import Endpoint, EndpointFunc, Stats, endpoint
from api_client_core.logging import setup_logging

__all__ = ["APIClient", "BaseAPI", "Endpoint", "EndpointFunc", "Stats", "__version__", "endpoint", "setup_logging"]

try:
    __version__ = version("api-client-core")
except PackageNotFoundError:
    __version__ = "unknown"

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from common_libs.logging import get_logger, setup_logging

from api_client_core.base import APIBase, APIClient
from api_client_core.endpoints import Endpoint, EndpointFunc, Stats, endpoint

__all__ = ["APIBase", "APIClient", "Endpoint", "EndpointFunc", "Stats", "__version__", "endpoint"]

try:
    __version__ = version("api-client-core")
except PackageNotFoundError:
    __version__ = "unknown"

_CONFIG_DIR = Path(__file__).parent / "cfg"

setup_logging(_CONFIG_DIR / "logging.yaml")
logger = get_logger(__name__)

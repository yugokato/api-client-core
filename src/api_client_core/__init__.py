from collections.abc import Mapping
from importlib import resources
from importlib.metadata import PackageNotFoundError, version
from logging import NullHandler, getLogger
from typing import Any

import yaml
from common_libs.logging import setup_logging as _setup_logging

from api_client_core.base import APIClient, BaseAPI
from api_client_core.endpoints import Endpoint, EndpointFunc, Stats, endpoint

__all__ = ["APIClient", "BaseAPI", "Endpoint", "EndpointFunc", "Stats", "__version__", "endpoint", "setup_logging"]

try:
    __version__ = version("api-client-core")
except PackageNotFoundError:
    __version__ = "unknown"


def setup_logging(config: Mapping[str, Any] | None = None, delta_config: Mapping[str, Any] | None = None) -> None:
    """Set up logging for `api_client_core`, including its REST request/response logs.

    Calling this is optional. Until it is called, `api_client_core` is silent (a `NullHandler` is attached
    at import), so downstream projects that never call this function won't see any output or warnings.

    When `config` is not specified, the package's bundled config is applied. That default enables colored
    console output at the `INFO` level for all of `api_client_core`'s logs, including per-request
    request/response logs.

    :param config: Base logging config, following the `logging.config.dictConfig` schema. Defaults to the
                   package's bundled config when not specified
    :param delta_config: Delta logging config to merge onto the base config
    """
    if config is None:
        config_text = (resources.files(__name__) / "cfg" / "logging.yaml").read_text(encoding="utf-8")
        config = yaml.safe_load(config_text)
    _setup_logging(config, delta_config)


# Silent by default. Downstream projects can opt in by calling `api_client_core.setup_logging()` explicitly.
# Until then, attach a NullHandler so this logger never triggers the "No handlers could be found" warning.
getLogger(__name__).addHandler(NullHandler())

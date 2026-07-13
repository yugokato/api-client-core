from collections.abc import Mapping
from copy import deepcopy
from importlib import resources
from importlib.metadata import PackageNotFoundError, version
from logging import NullHandler, getLogger
from typing import Any

import yaml
from common_libs.logging import setup_logging as _setup_logging
from common_libs.utils import merge_dicts

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

    `api_client_core` builds on `common_libs` (eg. for its REST client), so `common_libs`' own logs need to be
    enabled too for full visibility. When the resolved config configures an `api_client_core` logger but no
    `common_libs` logger, this function mirrors the `api_client_core` logger settings onto `common_libs` so
    callers only need to think about `api_client_core`. Add an explicit `common_libs` entry (in `config` or
    `delta_config`) to control it independently instead.

    :param config: Base logging config, following the `logging.config.dictConfig` schema. Defaults to the
                   package's bundled config when not specified
    :param delta_config: Delta logging config to merge onto the base config
    """
    _validate_config(config, "config")
    _validate_config(delta_config, "delta_config")
    if config is None:
        config_text = (resources.files(__name__) / "cfg" / "logging.yaml").read_text(encoding="utf-8")
        config = yaml.safe_load(config_text)
    if delta_config:
        config = merge_dicts(dict(config), dict(delta_config))
    config = _with_mirrored_common_libs_logger(config)
    _setup_logging(config)


def _validate_config(config: Any, name: str) -> None:
    """Validate that a logging config argument is a `Mapping` or `None`

    :param config: The value to validate
    :param name: The parameter name to reference in the error message
    """
    if config is not None and not isinstance(config, Mapping):
        raise TypeError(f"`{name}` must be a Mapping, not {type(config).__name__}")


def _with_mirrored_common_libs_logger(config: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return `config` with a `common_libs` logger mirroring `api_client_core`'s, unless already configured

    :param config: Logging config to inspect
    """
    loggers = config.get("loggers", {})
    if "api_client_core" not in loggers or "common_libs" in loggers:
        return config
    return {**config, "loggers": {**loggers, "common_libs": deepcopy(loggers["api_client_core"])}}


# Silent by default. Downstream projects can opt in by calling `api_client_core.setup_logging()` explicitly.
# Until then, attach a NullHandler so this logger never triggers the "No handlers could be found" warning.
getLogger(__name__).addHandler(NullHandler())

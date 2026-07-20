from collections.abc import Mapping
from copy import deepcopy
from importlib import resources
from logging import NullHandler, getLevelNamesMapping, getLogger
from typing import Any

import yaml
from common_libs.logging import setup_logging as _setup_logging
from common_libs.utils import merge_dicts

__all__ = ["setup_logging"]


def setup_logging(
    config: Mapping[str, Any] | None = None,
    delta_config: Mapping[str, Any] | None = None,
    level: int | str | None = None,
) -> None:
    """Set up logging for `api_client_core` and its upstream `common-libs`.

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
    :param level: Log level to apply to the `api_client_core` logger, overriding any level set via `config`
                  or `delta_config`. A shortcut for the common case of only changing verbosity. Mirrored onto
                  `common_libs` the same way as any other `api_client_core` logger setting. Must be a
                  recognized level name (eg. `"WARNING"`) or an int, including a custom level registered via
                  `logging.addLevelName()`
    """
    _validate_config(config, "config")
    _validate_config(delta_config, "delta_config")
    if level is not None:
        _validate_level(level)
    if config is None:
        config_text = (resources.files(__package__) / "cfg" / "logging.yaml").read_text(encoding="utf-8")
        config = yaml.safe_load(config_text)
    if delta_config:
        config = merge_dicts(dict(config), dict(delta_config))
    if level is not None:
        config = _with_level_override(config, level)
    config = _with_mirrored_common_libs_logger(config)
    _setup_logging(config)


def _validate_config(config: Any, name: str) -> None:
    """Validate that a logging config argument is a `Mapping` or `None`

    :param config: The value to validate
    :param name: The parameter name to reference in the error message
    """
    if config is not None and not isinstance(config, Mapping):
        raise TypeError(f"`{name}` must be a Mapping, not {type(config).__name__}")


def _validate_level(level: Any) -> None:
    """Validate that a `level` argument is a recognized level name or an int

    :param level: The value to validate
    """
    if isinstance(level, str):
        level_names = getLevelNamesMapping()
        if level not in level_names:
            raise ValueError(f"Invalid `level`: {level!r}. Must be one of {sorted(level_names)} or an int")
    elif not isinstance(level, int) or isinstance(level, bool):
        raise TypeError(f"`level` must be an int or str, not {type(level).__name__}")


def _with_logger_config(config: Mapping[str, Any], name: str, logger_config: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return `config` with logger `name`'s config replaced by `logger_config`

    :param config: Logging config to update
    :param name: Logger name to set
    :param logger_config: The logger's resolved config
    """
    loggers = config.get("loggers", {})
    return {**config, "loggers": {**loggers, name: logger_config}}


def _with_level_override(config: Mapping[str, Any], level: int | str) -> Mapping[str, Any]:
    """Return `config` with the `api_client_core` logger's level set to `level`

    :param config: Logging config to override
    :param level: Log level to apply to the `api_client_core` logger
    """
    api_client_core_logger = config.get("loggers", {}).get(__package__, {})
    if not isinstance(api_client_core_logger, Mapping):
        raise TypeError(f"{__package__} logger config must be a Mapping, not {type(api_client_core_logger).__name__}")
    return _with_logger_config(config, __package__, {**api_client_core_logger, "level": level})


def _with_mirrored_common_libs_logger(config: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return `config` with a `common_libs` logger mirroring `api_client_core`'s, unless already configured

    :param config: Logging config to inspect
    """
    loggers = config.get("loggers", {})
    if __package__ not in loggers or "common_libs" in loggers:
        return config
    return _with_logger_config(config, "common_libs", deepcopy(loggers[__package__]))


# Silent by default. Downstream projects can opt in by calling `api_client_core.setup_logging()` explicitly.
# Until then, attach a NullHandler so this logger never triggers the "No handlers could be found" warning.
getLogger(__package__).addHandler(NullHandler())

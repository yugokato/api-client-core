"""Unit tests for api_client_core's logging setup (`logging.py`)."""

from __future__ import annotations

import logging
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from typing import Any

import pytest
from common_libs.logging import ColoredStreamHandler

from api_client_core import setup_logging


@pytest.fixture(autouse=True)
def _restore_logging_state() -> Iterator[None]:
    """Snapshot and restore logging state so `setup_logging()`/`dictConfig` calls don't leak between tests"""
    loggers = [logging.getLogger(), logging.getLogger("api_client_core"), logging.getLogger("common_libs")]
    snapshot = {logger: (logger.level, logger.propagate, list(logger.handlers), logger.disabled) for logger in loggers}
    yield
    for logger, (level, propagate, handlers, disabled) in snapshot.items():
        logger.setLevel(level)
        logger.propagate = propagate
        logger.handlers = handlers
        logger.disabled = disabled


class TestDefaultLoggingState:
    """Tests for api_client_core's logging state before `setup_logging()` is called"""

    def test_logger_only_has_a_null_handler(self) -> None:
        """Test that api_client_core's logger only has a NullHandler until setup_logging() is called"""
        logger = logging.getLogger("api_client_core")
        assert any(isinstance(handler, logging.NullHandler) for handler in logger.handlers)
        assert not any(isinstance(handler, ColoredStreamHandler) for handler in logger.handlers)

    def test_importing_the_package_does_not_disable_pre_existing_loggers(self) -> None:
        """Test that importing api_client_core in a fresh process leaves other loggers untouched

        This must run in a subprocess: by the time any other test runs, api_client_core is already imported
        (via conftest.py), so there is no in-process way to observe the state of a logger from before the import.
        """
        script = textwrap.dedent("""
            import logging

            pre_existing_logger = logging.getLogger("subprocess_pre_existing_logger")
            assert pre_existing_logger.disabled is False

            import api_client_core

            assert pre_existing_logger.disabled is False, "importing api_client_core disabled a pre-existing logger"
            api_client_core_logger = logging.getLogger("api_client_core")
            assert len(api_client_core_logger.handlers) == 1
            assert type(api_client_core_logger.handlers[0]).__name__ == "NullHandler"
            """)
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr


class TestSetupLogging:
    """Tests for `api_client_core.setup_logging()`"""

    @pytest.mark.parametrize("logger_name", ["api_client_core", "common_libs"])
    def test_default_config_configures_logger(self, logger_name: str) -> None:
        """Test that setup_logging() with no arguments applies the bundled config to the given logger"""
        setup_logging()
        logger = logging.getLogger(logger_name)
        assert logger.level == logging.INFO
        assert logger.propagate is False
        assert any(isinstance(handler, ColoredStreamHandler) for handler in logger.handlers)

    def test_default_config_does_not_disable_pre_existing_loggers(self) -> None:
        """Test that setup_logging() does not disable a logger that already existed when it was called"""
        pre_existing_logger = logging.getLogger("some_other_pre_existing_logger")

        setup_logging()

        assert pre_existing_logger.disabled is False

    def test_custom_config_overrides_bundled_default(self) -> None:
        """Test that a custom config replaces the bundled default config"""
        config: dict[str, Any] = {
            "version": 1,
            "disable_existing_loggers": False,
            "loggers": {"api_client_core": {"level": "WARNING"}},
        }

        setup_logging(config)

        assert logging.getLogger("api_client_core").level == logging.WARNING

    def test_delta_config_merges_onto_bundled_default(self) -> None:
        """Test that delta_config merges an override onto the bundled default config"""
        delta_config: dict[str, Any] = {"loggers": {"api_client_core": {"level": "ERROR"}}}

        setup_logging(delta_config=delta_config)

        logger = logging.getLogger("api_client_core")
        assert logger.level == logging.ERROR
        assert any(isinstance(handler, ColoredStreamHandler) for handler in logger.handlers)

    def test_custom_config_without_common_libs_logger_still_configures_it(self) -> None:
        """Test that a custom config lacking a `common_libs` logger has one mirrored from `api_client_core`'s"""
        config: dict[str, Any] = {
            "version": 1,
            "disable_existing_loggers": False,
            "handlers": {
                "console": {
                    "class": "common_libs.logging.ColoredStreamHandler",
                    "formatter": "default",
                    "stream": "ext://sys.stdout",
                }
            },
            "formatters": {"default": {"class": "common_libs.logging.LogFormatter"}},
            "loggers": {"api_client_core": {"level": "WARNING", "handlers": ["console"], "propagate": False}},
        }

        setup_logging(config)

        logger = logging.getLogger("common_libs")
        assert logger.level == logging.WARNING
        assert logger.propagate is False
        assert any(isinstance(handler, ColoredStreamHandler) for handler in logger.handlers)

    def test_custom_config_with_explicit_common_libs_logger_is_not_overridden(self) -> None:
        """Test that an explicit `common_libs` logger entry in a custom config is left untouched"""
        config: dict[str, Any] = {
            "version": 1,
            "disable_existing_loggers": False,
            "loggers": {
                "api_client_core": {"level": "WARNING"},
                "common_libs": {"level": "ERROR"},
            },
        }

        setup_logging(config)

        assert logging.getLogger("common_libs").level == logging.ERROR

    def test_config_without_api_client_core_logger_leaves_common_libs_untouched(self) -> None:
        """Test that mirroring is a no-op when the config has no `api_client_core` logger"""
        pre_existing_logger = logging.getLogger("common_libs")

        config: dict[str, Any] = {
            "version": 1,
            "disable_existing_loggers": False,
            "loggers": {"some_other_logger": {"level": "WARNING"}},
        }

        setup_logging(config)

        assert pre_existing_logger.disabled is False

    def test_delta_config_override_of_api_client_core_level_is_reflected_in_mirror(self) -> None:
        """Test that mirroring reflects `api_client_core`'s level after `delta_config` is applied"""
        config: dict[str, Any] = {
            "version": 1,
            "disable_existing_loggers": False,
            "loggers": {"api_client_core": {"level": "WARNING"}},
        }
        delta_config: dict[str, Any] = {"loggers": {"api_client_core": {"level": "ERROR"}}}

        setup_logging(config, delta_config=delta_config)

        assert logging.getLogger("api_client_core").level == logging.ERROR
        assert logging.getLogger("common_libs").level == logging.ERROR

    def test_delta_config_with_explicit_common_libs_logger_is_not_overridden(self) -> None:
        """Test that an explicit `common_libs` logger entry in `delta_config` is left untouched by mirroring"""
        config: dict[str, Any] = {
            "version": 1,
            "disable_existing_loggers": False,
            "loggers": {"api_client_core": {"level": "WARNING", "propagate": False}},
        }
        delta_config: dict[str, Any] = {"loggers": {"common_libs": {"level": "ERROR"}}}

        setup_logging(config, delta_config=delta_config)

        logger = logging.getLogger("common_libs")
        assert logger.level == logging.ERROR
        assert logger.propagate is True

    @pytest.mark.parametrize("kwarg", ["config", "delta_config"])
    def test_invalid_config_type_raises_type_error(self, kwarg: str) -> None:
        """Test that a non-Mapping `config`/`delta_config` raises a `TypeError`"""
        with pytest.raises(TypeError, match="must be a Mapping"):
            setup_logging(**{kwarg: 42})

    @pytest.mark.parametrize(
        "level, expected",
        [
            pytest.param("DEBUG", logging.DEBUG, id="name"),
            pytest.param(logging.DEBUG, logging.DEBUG, id="standard_int"),
            pytest.param(15, 15, id="custom_int"),
        ],
    )
    def test_level_accepts_valid_values(self, level: int | str, expected: int) -> None:
        """Test that `level` accepts a level name, a standard numeric level, or a custom numeric level"""
        setup_logging(level=level)

        assert logging.getLogger("api_client_core").level == expected

    def test_level_is_mirrored_to_common_libs(self) -> None:
        """Test that `level` is reflected in the mirrored `common_libs` logger"""
        setup_logging(level="DEBUG")

        assert logging.getLogger("common_libs").level == logging.DEBUG

    @pytest.mark.parametrize(
        "kwargs",
        [
            pytest.param(
                {
                    "config": {
                        "version": 1,
                        "disable_existing_loggers": False,
                        "loggers": {"api_client_core": {"level": "WARNING"}},
                    }
                },
                id="config",
            ),
            pytest.param({"delta_config": {"loggers": {"api_client_core": {"level": "ERROR"}}}}, id="delta_config"),
        ],
    )
    def test_level_takes_precedence(self, kwargs: dict[str, Any]) -> None:
        """Test that `level` overrides a level set via `config` or `delta_config`"""
        setup_logging(**kwargs, level="DEBUG")

        assert logging.getLogger("api_client_core").level == logging.DEBUG

    def test_level_does_not_override_explicit_common_libs_logger(self) -> None:
        """Test that `level` only changes `api_client_core`, leaving an explicit `common_libs` entry alone"""
        config: dict[str, Any] = {
            "version": 1,
            "disable_existing_loggers": False,
            "loggers": {
                "api_client_core": {"level": "WARNING"},
                "common_libs": {"level": "ERROR"},
            },
        }

        setup_logging(config, level="DEBUG")

        assert logging.getLogger("api_client_core").level == logging.DEBUG
        assert logging.getLogger("common_libs").level == logging.ERROR

    def test_level_creates_api_client_core_logger_when_missing(self) -> None:
        """Test that `level` adds an `api_client_core` logger entry when the resolved config has none"""
        config: dict[str, Any] = {
            "version": 1,
            "disable_existing_loggers": False,
            "loggers": {"some_other_logger": {"level": "WARNING"}},
        }

        setup_logging(config, level="DEBUG")

        assert logging.getLogger("api_client_core").level == logging.DEBUG

    def test_level_raises_type_error_for_non_mapping_api_client_core_logger(self) -> None:
        """Test that `level` raises a `TypeError` when the config's `api_client_core` logger isn't a Mapping"""
        config: dict[str, Any] = {
            "version": 1,
            "disable_existing_loggers": False,
            "loggers": {"api_client_core": None},
        }

        with pytest.raises(TypeError, match="api_client_core logger config must be a Mapping, not NoneType"):
            setup_logging(config, level="DEBUG")

    @pytest.mark.parametrize("level", ["foo", "warning"], ids=["invalid_name", "lowercase_name"])
    def test_invalid_level_name_raises(self, level: str) -> None:
        """Test that a `level` name that is invalid or not uppercase raises a `ValueError`"""
        with pytest.raises(ValueError, match=f"Invalid `level`: '{level}'"):
            setup_logging(level=level)

    @pytest.mark.parametrize("level", [20.0, True, False], ids=["float", "true", "false"])
    def test_invalid_level_type_raises_type_error(self, level: Any) -> None:
        """Test that a `level` that is neither an int nor a str raises a `TypeError`"""
        with pytest.raises(TypeError, match=f"`level` must be an int or str, not {type(level).__name__}"):
            setup_logging(level=level)

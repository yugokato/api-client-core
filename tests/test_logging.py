"""Unit tests for api_client_core's logging setup (`__init__.py`)."""

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

            import api_client_core  # noqa: E402

            assert pre_existing_logger.disabled is False, "importing api_client_core disabled a pre-existing logger"
            api_client_core_logger = logging.getLogger("api_client_core")
            assert len(api_client_core_logger.handlers) == 1
            assert type(api_client_core_logger.handlers[0]).__name__ == "NullHandler"
            """)
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr


class TestSetupLogging:
    """Tests for `api_client_core.setup_logging()`"""

    def test_default_config_configures_api_client_core_logger(self) -> None:
        """Test that setup_logging() with no arguments applies the bundled config to the api_client_core logger"""
        setup_logging()
        logger = logging.getLogger("api_client_core")
        assert logger.level == logging.INFO
        assert logger.propagate is False
        assert any(isinstance(handler, ColoredStreamHandler) for handler in logger.handlers)

    def test_default_config_configures_common_libs_logger(self) -> None:
        """Test that setup_logging() with no arguments also applies the bundled config to the common_libs logger"""
        setup_logging()
        logger = logging.getLogger("common_libs")
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

"""Fixtures for the shared-machinery tests.

`test_discovery.py` exercises `api_client_core._common.discovery`, which was split out of `cli/`. The
fixtures it needs are defined in `tests_cli/conftest.py` and are directory-scoped there, so they are
re-exported here. `tests_mcp/` already reaches into `tests_cli/conftest.py` the same way.
"""

from ..tests_cli.conftest import (
    _restore_logging_state,
    cli_client_class,
    downstream_setup_logging_project,
    gadgets_api_class,
    widgets_api_class,
)

__all__ = [
    "_restore_logging_state",
    "cli_client_class",
    "downstream_setup_logging_project",
    "gadgets_api_class",
    "widgets_api_class",
]

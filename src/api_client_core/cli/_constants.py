"""Vocabulary shared across the CLI generator.

Stays stdlib-only, since it's imported at module scope on the shell-completion hot path. It must never
import from another `cli/` module or from `api_client_core.*`. A constant that can't be expressed with the
stdlib alone belongs next to its own consumer instead.
"""

from __future__ import annotations

from enum import StrEnum

PROG = "api-client"
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
# Marker appended where a rendered value was elided to fit a width budget (a long choice group, a clamped
# help= line under -h, ...)
ELLIPSIS = "…"
# Default for a flag whose absence must stay distinguishable from every value it could be given
NOT_PROVIDED = object()
WRAPPER_CHAIN_DEST = "_wrapper_chain"


class Flag(StrEnum):
    """Every long option string the CLI registers for itself, outside the call wrappers."""

    HELP = "--help"
    VERSION = "--version"
    BASE_URL = "--base-url"
    LOG_LEVEL = "--log-level"
    OUTPUT = "--output"
    QUIET = "--quiet"
    NO_HOOKS = "--no-hooks"
    HEADER = "--header"
    RAW_OPTION = "--raw-option"


class WrapperFlag(StrEnum):
    """Every long option string for a CLI-expressible `with_xxx()` call wrapper."""

    RETRY = "--with-retry"
    RATE_LIMIT = "--with-rate-limit"
    LOCK = "--with-lock"
    EXPECTED_STATUS = "--with-expected-status"
    MAX_RESPONSE_TIME = "--with-max-response-time"
    STATS = "--with-stats"
    REPEAT = "--with-repeat"
    CONCURRENCY = "--with-concurrency"

    @property
    def dest(self) -> str:
        """The `argparse` dest this flag resolves to, derived the way `argparse` derives it."""
        return self.removeprefix("--").replace("-", "_")


class Output(StrEnum):
    """Accepted `--output` values, controlling what a dispatched call writes to stdout."""

    NONE = "none"
    JSON = "json"
    RAW = "raw"
    FULL = "full"


HELP_FLAGS: tuple[str, ...] = ("-h", Flag.HELP)
# Option strings a generated endpoint-parameter flag must not collide with. --version is registered only on
# the top-level parser, which carries no generated parameters, so it can't collide and is left out here.
RESERVED_CLI_FLAGS: frozenset[str] = (frozenset(Flag) | frozenset(WrapperFlag)) - {Flag.VERSION}
# Plain str, not Output members: argparse's own invalid-choice error stringifies each choice with repr()
# rather than str() on Python 3.11, which would render a StrEnum member as "<Output.NONE: 'none'>" instead
# of "none". Never pass an Output member itself to `choices=` for this reason.
OUTPUT_CHOICES: tuple[str, ...] = tuple(o.value for o in Output)

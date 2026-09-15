"""Shared call-wrapper registry for the CLI and MCP generators.

One `WrapperSpec` per framework `with_xxx()` call wrapper the two generators expose: its option names
and types, its per-option lower bounds, whether it is chain-terminal, and a callable that applies it to
a bound endpoint func. The CLI turns each spec into a `--with-*` flag. The MCP server turns each into a
JSON Schema fragment under a tool's `call_wrappers` property. Neither `with_polling` nor
`with_pagination` is here, since both need a Python callable no text or JSON front end can supply.

Stays stdlib-only at module scope. The appliers call methods on a passed-in object rather than importing
anything from `core/`, so importing this module never pulls in `httpx2`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

# Wrapper names referenced by name in generator logic. The canonical fold order is the order the specs
# appear in `WRAPPERS` below, not this list.
WITH_EXPECTED_STATUS = "with_expected_status"
WITH_LOCK = "with_lock"
WITH_STATS = "with_stats"
WITH_REPEAT = "with_repeat"
WITH_CONCURRENCY = "with_concurrency"

_Applier = Callable[[Any, Mapping[str, Any]], Any]
_Chain = Iterable[tuple[str, Mapping[str, Any]]]


@dataclass(frozen=True)
class WrapperSpec:
    """One chainable `with_xxx()` call wrapper, described once for both generators.

    :param name: The wrapper method name, e.g. `"with_retry"` (also the CLI flag's own `dest`)
    :param summary: One-line description, reused as CLI help and the MCP JSON Schema `description`
    :param apply: Applies the wrapper to a bound endpoint func given an options mapping, returning the
                  new chained endpoint func
    :param options: Each accepted option name mapped to its scalar type (`int`/`float`/`bool`/`str`)
    :param primary: The option a bare CLI value (e.g. `--with-retry 429`) maps to, if any
    :param multi: Options accepting either a scalar or a list of that scalar (`condition`)
    :param array_options: Options that are always a non-empty list (`status_codes`)
    :param required: Scalar options with no usable default that a caller must always supply, distinct
                     from `array_options` (also always required, but list-shaped rather than scalar)
    :param minimums: Each option name mapped to its inclusive lower bound, if any
    :param terminal: Whether this wrapper must be last in a chain (`with_repeat`/`with_concurrency`)
    """

    name: str
    summary: str
    apply: _Applier
    options: Mapping[str, type] = field(default_factory=dict)
    primary: str | None = None
    multi: frozenset[str] = frozenset()
    array_options: frozenset[str] = frozenset()
    required: frozenset[str] = frozenset()
    minimums: Mapping[str, int | float] = field(default_factory=dict)
    terminal: bool = False


WRAPPERS: Mapping[str, WrapperSpec] = {
    spec.name: spec
    for spec in (
        WrapperSpec(
            name="with_retry",
            summary="Retry the call while a response is not OK, or matches the given status code(s).",
            apply=lambda ef, o: ef.with_retry(**o),
            options={"condition": int, "num_retries": int, "retry_after": float, "safe_methods_only": bool},
            primary="condition",
            multi=frozenset({"condition"}),
            minimums={"num_retries": 0, "retry_after": 0},
        ),
        WrapperSpec(
            name="with_rate_limit",
            summary="Throttle calls with a client-side token bucket.",
            apply=lambda ef, o: ef.with_rate_limit(**o),
            options={"max_requests": int, "interval": float},
            primary="max_requests",
            required=frozenset({"max_requests"}),
            minimums={"max_requests": 1},
        ),
        WrapperSpec(
            name=WITH_EXPECTED_STATUS,
            summary="Assert the response status is one of the given codes.",
            apply=lambda ef, o: ef.with_expected_status(*o["status_codes"]),
            options={"status_codes": int},
            primary="status_codes",
            array_options=frozenset({"status_codes"}),
        ),
        WrapperSpec(
            name="with_max_response_time",
            summary="Assert the response time does not exceed the threshold, in milliseconds.",
            apply=lambda ef, o: ef.with_max_response_time(**o),
            options={"threshold_msecs": float},
            primary="threshold_msecs",
            required=frozenset({"threshold_msecs"}),
        ),
        WrapperSpec(
            name=WITH_LOCK,
            summary="Hold a distributed lock for the duration of the call.",
            apply=lambda ef, o: ef.with_lock(**o),
            options={"lock_name": str},
            primary="lock_name",
        ),
        WrapperSpec(
            name=WITH_STATS,
            summary="Collect per-endpoint call statistics for this call.",
            apply=lambda ef, o: ef.with_stats(),
        ),
        WrapperSpec(
            name=WITH_REPEAT,
            summary="Repeat the call sequentially, returning a list of responses.",
            apply=lambda ef, o: ef.with_repeat(**o),
            options={"num": int, "return_exceptions": bool},
            primary="num",
            minimums={"num": 1},
            terminal=True,
        ),
        WrapperSpec(
            name=WITH_CONCURRENCY,
            summary="Repeat the call concurrently, returning a list of responses.",
            apply=lambda ef, o: ef.with_concurrency(**o),
            options={"num": int, "max_connections": int, "return_exceptions": bool},
            primary="num",
            minimums={"num": 1, "max_connections": 1},
            terminal=True,
        ),
    )
}


def apply_wrappers(endpoint_func: Any, chain: _Chain) -> Any:
    """Fold every `(wrapper_name, options)` pair in `chain` onto `endpoint_func`, in iteration order.

    Mirrors `ef.with_x(**o1).with_y(**o2)` in Python: a terminal wrapper not folded last raises
    `RuntimeError` from `EndpointFunc` itself, exactly as the equivalent chain would, and a name
    repeated in `chain` is applied once per occurrence.

    :param endpoint_func: The bound `EndpointFunc` to apply wrappers to
    :param chain: `(wrapper_name, options)` pairs, each name a key of `WRAPPERS`
    """
    ef = endpoint_func
    for name, options in chain:
        ef = WRAPPERS[name].apply(ef, options)
    return ef


def expected_statuses(chain: _Chain) -> tuple[int, ...]:
    """Every status code across all `with_expected_status` links in `chain`, combined in order.

    :param chain: `(wrapper_name, options)` pairs, as passed to `apply_wrappers()`
    """
    return tuple(
        code for name, options in chain if name == WITH_EXPECTED_STATUS for code in options.get("status_codes", ())
    )

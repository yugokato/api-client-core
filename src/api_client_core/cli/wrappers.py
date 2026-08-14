from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any

from ._constants import NOT_PROVIDED, WRAPPER_CHAIN_DEST, WrapperFlag
from .parser import CollapsibleText

_WRAPPERS_GROUP_TITLE = "call wrappers"
_WRAPPERS_GROUP_DESCRIPTION = (
    "When multiple wrappers are specified, they are chained in the order they appear on the command line."
)
# Rendered verbatim (no re-wrap) under -h in place of the description above and every flag in the group, so each line
# must already fit a narrow terminal.
_WRAPPERS_GROUP_SHORT_DESCRIPTION = "This command supports call wrappers.\nUse --help for available options and syntax."
_RETRY_SPEC: dict[str, type] = {"condition": int, "num_retries": int, "retry_after": float, "safe_methods_only": bool}
_RATE_LIMIT_SPEC: dict[str, type] = {"max_requests": int, "interval": float}
_REPEAT_SPEC: dict[str, type] = {"num": int, "return_exceptions": bool}
_CONCURRENCY_SPEC: dict[str, type] = {"num": int, "max_connections": int, "return_exceptions": bool}
# Inclusive lower bounds for a spec key whose value would otherwise silently no-op (num=0 repeats/runs the call
# zero times) or reach an unrelated, poorly-worded failure deeper in the call stack (max_connections=0/num=0 for
# with_concurrency() reaches a bare, message-less assertion in common_libs' job executor).
_RETRY_MINIMUMS: dict[str, int | float] = {"num_retries": 0, "retry_after": 0}
_RATE_LIMIT_MINIMUMS: dict[str, int | float] = {"max_requests": 1}
_REPEAT_MINIMUMS: dict[str, int | float] = {"num": 1}
_CONCURRENCY_MINIMUMS: dict[str, int | float] = {"num": 1, "max_connections": 1}

_APPLIERS: dict[str, Callable[[Any, Any], Any]] = {
    WrapperFlag.RETRY.dest: lambda ef, spec: ef.with_retry(**spec),
    WrapperFlag.RATE_LIMIT.dest: lambda ef, spec: ef.with_rate_limit(**spec),
    WrapperFlag.LOCK.dest: lambda ef, name: ef.with_lock(name),
    WrapperFlag.EXPECTED_STATUS.dest: lambda ef, codes: ef.with_expected_status(*codes),
    WrapperFlag.MAX_RESPONSE_TIME.dest: lambda ef, threshold: ef.with_max_response_time(threshold),
    WrapperFlag.STATS.dest: lambda ef, _: ef.with_stats(),
    WrapperFlag.REPEAT.dest: lambda ef, spec: ef.with_repeat(**spec),
    WrapperFlag.CONCURRENCY.dest: lambda ef, spec: ef.with_concurrency(**spec),
}


def add_wrapper_arguments(parser: argparse.ArgumentParser) -> None:
    """Add one flag per CLI-expressible `with_xxx()` call wrapper to a leaf subparser, in its own
    `call wrappers` group.

    Every flag defaults to `NOT_PROVIDED` so an omitted wrapper is distinguishable from one explicitly given,
    letting the wrapper's own default apply. `with_retry`/`with_rate_limit`/`with_repeat`/`with_concurrency`
    accept several options, given as a single comma-separated `key=value` spec rather than one flag per
    option. `with_repeat` and `with_concurrency` are mutually exclusive, since both are terminal.

    Every flag records its occurrence in command-line order, so a flag given more than once contributes one
    chain link per occurrence, matching `.with_x().with_x()` in Python.

    Each flag's own `help=` leads with a one-line summary, with any further detail (the `SPEC:` syntax, for
    the flags that accept one) on a following line, shown only under `--help`. A leaf command's `-h` shows
    none of this group's flags at all, collapsing the whole group to a short note instead.

    :param parser: Leaf subparser for a single endpoint command
    """
    group = parser.add_argument_group(
        title=_WRAPPERS_GROUP_TITLE,
        description=CollapsibleText(_WRAPPERS_GROUP_DESCRIPTION, short=_WRAPPERS_GROUP_SHORT_DESCRIPTION),
    )
    group.add_argument(
        WrapperFlag.RETRY,
        nargs="?",
        const={},
        default=NOT_PROVIDED,
        type=_spec_parser(_RETRY_SPEC, primary="num_retries", multi=frozenset({"condition"}), minimums=_RETRY_MINIMUMS),
        action=_OrderedWrapperAction,
        metavar="SPEC",
        help="Retry on failure. Bare flag retries any non-2xx response once.\nSPEC: NUM_RETRIES, or "
        "condition=STATUS (repeatable), num_retries=N, retry_after=SECONDS, safe_methods_only=BOOL",
    )
    group.add_argument(
        WrapperFlag.RATE_LIMIT,
        default=NOT_PROVIDED,
        type=_spec_parser(_RATE_LIMIT_SPEC, primary="max_requests", minimums=_RATE_LIMIT_MINIMUMS),
        action=_OrderedWrapperAction,
        metavar="SPEC",
        help="Throttle calls with a client-side token bucket.\nSPEC: MAX_REQUESTS, or max_requests=N, interval=SECONDS",
    )
    group.add_argument(
        WrapperFlag.LOCK,
        nargs="?",
        const=None,
        default=NOT_PROVIDED,
        action=_OrderedWrapperAction,
        metavar="NAME",
        help="Hold a distributed lock during the call.\nNAME defaults to an auto-generated one",
    )
    group.add_argument(
        WrapperFlag.EXPECTED_STATUS,
        nargs="+",
        type=int,
        default=NOT_PROVIDED,
        action=_OrderedWrapperAction,
        metavar="CODE",
        help="Assert the response status is one of the given codes",
    )
    group.add_argument(
        WrapperFlag.MAX_RESPONSE_TIME,
        type=float,
        default=NOT_PROVIDED,
        action=_OrderedWrapperAction,
        metavar="MSECS",
        help="Assert the response time does not exceed a threshold.\nMSECS is the threshold, in milliseconds",
    )
    group.add_argument(
        WrapperFlag.STATS,
        nargs=0,
        const=True,
        default=NOT_PROVIDED,
        action=_OrderedWrapperAction,
        help="Print a scoped stats report after the call",
    )

    terminal_group = group.add_mutually_exclusive_group()
    terminal_group.add_argument(
        WrapperFlag.REPEAT,
        nargs="?",
        const={},
        default=NOT_PROVIDED,
        type=_spec_parser(_REPEAT_SPEC, primary="num", minimums=_REPEAT_MINIMUMS),
        action=_OrderedWrapperAction,
        metavar="SPEC",
        help="Repeat the call sequentially, returning a list of responses.\n"
        "SPEC: NUM, or num=N, return_exceptions=BOOL",
    )
    terminal_group.add_argument(
        WrapperFlag.CONCURRENCY,
        nargs="?",
        const={},
        default=NOT_PROVIDED,
        type=_spec_parser(_CONCURRENCY_SPEC, primary="num", minimums=_CONCURRENCY_MINIMUMS),
        action=_OrderedWrapperAction,
        metavar="SPEC",
        help="Repeat the call concurrently, returning a list of responses.\n"
        "SPEC: NUM, or num=N, max_connections=N, return_exceptions=BOOL",
    )


def any_wrapper_given(namespace: argparse.Namespace) -> bool:
    """Whether at least one execution-wrapper flag was given on `namespace`.

    :param namespace: Namespace produced by parsing the registered wrapper flags
    """
    return bool(_wrapper_chain(namespace))


def apply_wrappers(endpoint_func: Any, namespace: argparse.Namespace) -> Callable[..., Any]:
    """Fold every wrapper flag given on `namespace` onto a bound endpoint func, in the order the flags were given.

    This mirrors Python's own `.with_x().with_y()` chaining: `--with-x --with-y` is equivalent to
    `endpoint_func.with_x().with_y()`, and `--with-y --with-x` to `endpoint_func.with_y().with_x()`. A flag
    given more than once contributes one chain link per occurrence. If a terminal wrapper
    (`with_repeat`/`with_concurrency`) isn't given last, the call raises `RuntimeError` when the next wrapper
    is folded in, exactly as it would for the equivalent Python chain.

    :param endpoint_func: The bound `EndpointFunc` to apply the selected wrappers to
    :param namespace: Namespace produced by parsing the registered wrapper flags
    """
    ef = endpoint_func
    for dest, value in _wrapper_chain(namespace):
        ef = _APPLIERS[dest](ef, value)
    return ef


def expected_statuses(namespace: argparse.Namespace) -> tuple[int, ...]:
    """Every status code given across all `--with-expected-status` occurrences, combined.

    The namespace attribute itself only ever holds the *last* occurrence's own codes, since every wrapper
    flag's namespace value is just a by-product of recording each occurrence into the wrapper chain.
    `--with-expected-status` is different: the exit-code check needs every code from every occurrence, not
    just the last one.

    :param namespace: Namespace produced by parsing the registered wrapper flags
    """
    return tuple(
        code for dest, value in _wrapper_chain(namespace) if dest == WrapperFlag.EXPECTED_STATUS.dest for code in value
    )


def _wrapper_chain(namespace: argparse.Namespace) -> tuple[tuple[str, Any], ...]:
    """The `(dest, value)` pairs recorded for every wrapper flag given, in command-line order.

    Empty when no wrapper flag was given, since the attribute is only created on first use.

    :param namespace: Namespace produced by parsing the registered wrapper flags
    """
    return tuple(getattr(namespace, WRAPPER_CHAIN_DEST, ()))


class _OrderedWrapperAction(argparse.Action):
    """Store a wrapper flag's value and append it to the namespace's ordered wrapper chain.

    `argparse` invokes an action once per occurrence of its flag, in command-line order, so recording each
    occurrence here is what lets the flags be folded back in the order they were given, rather than in a
    fixed order. A zero-arg (`nargs=0`) flag records its `const`, mirroring `store_true`.
    """

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        value = self.const if self.nargs == 0 else values
        setattr(namespace, self.dest, value)
        chain = getattr(namespace, WRAPPER_CHAIN_DEST, None)
        if chain is None:
            chain = []
            setattr(namespace, WRAPPER_CHAIN_DEST, chain)
        chain.append((self.dest, value))


def _spec_parser(
    allowed: dict[str, type],
    *,
    primary: str | None = None,
    multi: frozenset[str] = frozenset(),
    minimums: dict[str, int | float] | None = None,
) -> Callable[[str], dict[str, Any]]:
    """Build an `argparse` `type=` callable that parses a comma-separated `key=value` spec string.

    Each item is `key=value`, or a bare boolean key (interpreted as `key=True`). If `primary` is given, a first item
    with no key and no matching allowed key name is instead taken as that key's value (e.g. `--with-repeat 5` is
    shorthand for `--with-repeat num=5`).

    A key listed in `multi` accumulates across repeated occurrences instead of the last one overwriting the
    others: given once it still resolves to a bare scalar, matching every other key, and given more than once it
    resolves to a `list` of each occurrence's own value, in the order given (e.g. `condition=429,condition=503`
    for `with_retry()`, whose own `condition` parameter accepts either shape).

    A key listed in `minimums` is rejected here, at parse time, when its converted value falls below the given
    bound, rather than being passed through to the wrapper itself: some out-of-range values (e.g. `num=0` for
    `with_repeat()`/`with_concurrency()`) would otherwise silently run the call zero times instead of failing,
    and others reach an unrelated, poorly-worded failure deep in a lower layer instead of a clean usage error.

    Every raised `argparse.ArgumentTypeError` message deliberately omits the flag itself: argparse already
    prepends `argument --flag: ` to a `type=` converter's own error once it reaches the user, so including it
    here too would show the flag name twice.

    :param allowed: Mapping of accepted spec keys to their target type (`int`, `float`, or `bool`)
    :param primary: Key a first bare (unkeyed) item's value is assigned to, if any
    :param multi: Spec keys that accumulate into a `list` across repeated occurrences, rather than each
                  occurrence overwriting the last
    :param minimums: Mapping of spec keys to their own inclusive lower bound, if any
    """

    def parse(value: str) -> dict[str, Any]:
        spec: dict[str, Any] = {}
        accumulated: dict[str, list[Any]] = {}
        for i, item in enumerate(item for item in value.split(",") if item):
            key, sep, raw = item.partition("=")
            if sep:
                if key not in allowed:
                    raise argparse.ArgumentTypeError(_unknown_key_error(key, allowed))
                converted = _coerce(key, raw, allowed[key])
            elif key in allowed:
                if allowed[key] is not bool:
                    raise argparse.ArgumentTypeError(f"{key!r} requires a value ({allowed[key].__name__}=...)")
                converted = True
            elif primary is not None and i == 0:
                key, converted = primary, _coerce(primary, key, allowed[primary])
            else:
                raise argparse.ArgumentTypeError(_unknown_key_error(key, allowed))
            if minimums is not None and key in minimums and converted < minimums[key]:
                raise argparse.ArgumentTypeError(f"{key!r} must be >= {minimums[key]}, got {converted}")
            if key in multi:
                accumulated.setdefault(key, []).append(converted)
            else:
                spec[key] = converted
        for key, values in accumulated.items():
            spec[key] = values[0] if len(values) == 1 else values
        return spec

    return parse


def _coerce(key: str, raw: str, kind: type) -> Any:
    """Convert one spec value's raw string to its target type, raising a clean error on failure.

    :param key: Spec key the value belongs to, used in the error message
    :param raw: Raw string value to convert
    :param kind: Target type (`int`, `float`, or `bool`)
    """
    if kind is bool:
        lowered = raw.lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no"):
            return False
        raise argparse.ArgumentTypeError(f"{key!r} must be a boolean, got {raw!r}")
    try:
        return kind(raw)
    except ValueError:
        article = "an" if kind is int else "a"
        raise argparse.ArgumentTypeError(f"{key!r} must be {article} {kind.__name__}, got {raw!r}") from None


def _unknown_key_error(key: str, allowed: dict[str, type]) -> str:
    """Format an 'unknown spec key' error message.

    :param key: The unrecognized key
    :param allowed: The flag's accepted spec keys
    """
    return f"unknown option {key!r}. Valid options: {', '.join(sorted(allowed))}"

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from functools import partial
from typing import Any

from common_libs.clients.rest_client.utils import (
    format_request_failure,
    get_request_from_exception,
    get_response_reason,
)
from httpx2 import HTTPStatusError, Response

from api_client_core.base import APIClient
from api_client_core.endpoints import Endpoint
from api_client_core.logging import setup_logging
from api_client_core.types import RestResponse

from ._constants import Output
from ._stdout import cli_stdout
from .builder import build_client_parser
from .params import collect_call_kwargs, normalize_call_args, reset_stdin_state
from .utils import write_error
from .wrappers import any_wrapper_given, apply_wrappers, expected_statuses

STDERR_LOGGING_DELTA_CONFIG = {"handlers": {"console": {"stream": "ext://sys.stderr"}}}


def run(
    client_class: type[APIClient],
    argv: list[str] | None = None,
    *,
    prog: str | None = None,
    log_level: int | str | None = None,
    **client_kwargs: Any,
) -> int:
    """Build a CLI for `client_class`, parse `argv`, and dispatch the resolved endpoint call.

    Any exception raised while dispatching the call is reported as a clean `error: ...` on stderr with exit
    code 1, unless it was already logged in full elsewhere (it carries the request from the REST client
    layer), in which case only the exit code is returned to avoid duplicating that block. A `client_class`
    that can't produce a CLI, can't be constructed, or whose given `with_xxx()` wrapper flags fail to
    construct is likewise reported as a clean `error: ...`, with exit code 2, since each of those is a usage
    error rather than the call itself failing.

    When one or more `with_xxx()` call wrapper flags are given, the call is routed through the bound
    `EndpointFunc` with those wrappers applied in the order given, instead of through the plain `Endpoint`
    facade. `with_repeat()`/`with_concurrency()` then return a list of responses rather than a single one, and
    the process exits `0` only if every item is a 2xx response or one of the codes given to
    `--with-expected-status`.

    A positional-only endpoint parameter, which the CLI can only ever produce as a keyword flag, is moved
    back into a positional argument before dispatch. This is a CLI-only concern: a direct Python call
    deliberately keeps raising for a positional-only parameter given by keyword.

    `-q`/`--quiet` and `--output json` both turn off the call's request/response logs and console summary at the
    client, via the REST client's own `log_requests` attribute, so a failed response is reduced to a single
    `error: ...` line on stderr instead, written after `log_requests` has already silenced the REST client's own
    failure log so the two can't duplicate each other (a connection-level failure, e.g. a timeout, bypasses this
    and still logs in full, since it never reaches the REST client's request/response hooks at all). `--output
    json` implies `-q`, so a clean JSON payload on stdout isn't followed by a pretty-printed rendering of the
    same body on stderr regardless of whether `-q` was also given. The switch is applied once the client is
    constructed, so a client that issues its own request from `__init__` (a login call, say) predates it and
    isn't covered. The real stdout is reserved by the entry point for the whole process, so a stray write made
    anywhere during the call or client teardown lands on stderr rather than corrupting the JSON payload.

    One or more `-H`/`--header` flags are applied to the client's underlying httpx2 client once constructed,
    so protected endpoints can be reached without a client-specific auth mechanism. A `-H` header named
    `Authorization` overrides any auth the client installed for itself, rather than being silently
    overridden by it.

    A resource or command given with nothing after it (e.g. `api-client my-app users`, with no command) is
    reported by printing that level's own condensed help to stderr and exiting `2`, rather than argparse's
    bare "the following arguments are required" - so a first-time user is shown what's available at that
    level (`build_client_parser()` registers each subparsers action as not required for this exact reason),
    not just told that something is missing.

    :param client_class: Concrete `APIClient` subclass to drive. Must construct in sync mode (`async_mode=False`, the
                         default), since the CLI drives it synchronously
    :param argv: Argument list to parse. Defaults to `sys.argv[1:]` (argparse's own default)
    :param prog: Program name shown in generated help. Defaults to argparse's usual inference
    :param log_level: Log level to configure ahead of resource discovery.
    :param client_kwargs: Extra keyword arguments forwarded to the client constructor.
    """
    setup_logging(delta_config=STDERR_LOGGING_DELTA_CONFIG, level=log_level)

    try:
        parser = build_client_parser(client_class, prog=prog)
    except Exception as e:
        write_error(e)
        return 2

    reset_stdin_state()
    args = parser.parse_args(argv)
    endpoint: Endpoint[Any] | None = getattr(args, "_endpoint", None)
    if endpoint is None:
        # A resource or command was given with nothing after it: `_resource`/`_command` aren't required
        # (see build_client_parser()), so this parsed successfully instead of raising argparse's own
        # "arguments are required" error. Show the deepest parser actually reached - the app, or a
        # resource - so the choices available at that level are named, not just that something is missing.
        args._parser.print_help(sys.stderr, short=True)
        return 2

    if args.log_level and args.log_level != log_level:
        # Skipped when it already matches log_level (the value this function's own call above just applied):
        # a client resolved via dispatch() passes the very same --log-level it already peeked to build this
        # parser, so re-running the same dictConfig() (which re-reads and re-parses the bundled YAML config
        # from disk on every call) would be pure duplicate work in the common case where the peek and this
        # parser's own authoritative parse agree, which they always do since both scan the identical argv.
        setup_logging(delta_config=STDERR_LOGGING_DELTA_CONFIG, level=args.log_level)
    if args.base_url:
        client_kwargs["base_url"] = args.base_url

    try:
        client = client_class(**client_kwargs)
    except Exception as e:
        write_error(e)
        return 2

    if client.async_mode:
        asyncio.run(client.aclose())
        write_error(f"{client_class.__name__} is in async mode. The CLI only supports sync clients.")
        return 2

    _apply_headers(client, args.header)
    logs_suppressed = args.quiet or args.output != Output.NONE
    if logs_suppressed:
        client.rest_client.log_requests = False

    try:
        with client:
            call_kwargs = collect_call_kwargs(endpoint, args)
            try:
                call_args, call_kwargs = normalize_call_args(endpoint.original_func, (), call_kwargs)
            except TypeError as e:
                write_error(e)
                return 2
            ctrl_kwargs: dict[str, Any] = {
                "quiet": args.quiet,
                "with_hooks": not args.no_hooks,
                "raw_options": dict(args.raw_option),
            }
            try:
                call = _resolve_call(endpoint, client, args)
            except Exception as e:
                write_error(e)
                return 2
            result = call(*call_args, **call_kwargs, **ctrl_kwargs)
    except Exception as e:
        if get_request_from_exception(e) is None:
            write_error(format_request_failure(e.response) if isinstance(e, HTTPStatusError) else e)
        return 1

    _write_output(result, args.output)
    expected = expected_statuses(args)
    exit_code = _exit_code(result, expected)
    if exit_code and logs_suppressed:
        _write_failure_summary(result, expected)
    return exit_code


def _apply_headers(client: APIClient, headers: list[tuple[str, str]]) -> None:
    """Apply `-H`/`--header` flags to an already-instantiated client's underlying httpx2 client.

    Applied post-construction rather than threaded through the constructor the way `--base-url` is: unlike
    `base_url`, a header isn't something every `APIClient` subclass's `__init__` can be expected to accept,
    and discovery silently drops any candidate whose constructor rejects an unexpected kwarg, so threading
    headers the same way would risk hiding an otherwise-valid client the moment it's given a `-H` flag.

    An explicit header named `Authorization` (case-insensitively) first clears any auth the client may have
    installed for itself, since that auth is otherwise applied on every request after header merging and
    would silently override an explicit `-H "Authorization: ..."`. The given headers then replace any value
    the client itself already set for the same name, matching `httpx2.Headers.update()`'s own behavior. Two
    `-H` flags naming the same header both still reach the request as separate values (`httpx2.Headers.update()`
    doesn't dedupe within one call), matching curl's own repeatable `-H`.

    :param client: Instantiated, still-sync API client to apply the headers to
    :param headers: `(name, value)` pairs collected from `-H`/`--header`, in the order given
    """
    if not headers:
        return
    if any(name.lower() == "authorization" for name, _ in headers):
        client.rest_client.auth = None
    client.rest_client.client.headers.update(headers)


def _resolve_call(endpoint: Endpoint[Any], client: APIClient, args: argparse.Namespace) -> Callable[..., Any]:
    """Bind `endpoint` to `client`, folding in any `with_xxx()` call wrapper flags given on `args`.

    Returns the plain bound `Endpoint` call when no wrapper flag was given, or the wrapper-composed call
    otherwise. Kept separate from actually invoking the returned callable, so a wrapper construction error
    surfaces as a distinct failure from one raised by the call itself, reported as a usage error (exit 2)
    rather than a call failure (exit 1).

    :param endpoint: Endpoint the parsed namespace was built for
    :param client: Instantiated API client to bind the endpoint to
    :param args: Namespace produced by parsing arguments, including any `with_xxx()` wrapper flags
    """
    if not any_wrapper_given(args):
        return partial(endpoint, client)
    api_class = endpoint.api_class(client)
    endpoint_func = getattr(api_class, endpoint.func_name)
    return apply_wrappers(endpoint_func, args)


def _write_output(result: RestResponse | list[Any], output: str) -> None:
    """Write a successfully dispatched call's result to stdout, according to `--output`.

    `none` (the default) writes nothing. `json` writes each item's own already-decoded response body as a
    single JSON value, or a JSON array of them for a list result. `full` wraps each item's status code,
    response headers, and decoded body in one `{status_code, headers, body}` object, or an array of them.
    `raw` writes each item's own undecoded response body, as text, exactly as the server sent it - for a
    list result, each item's own raw body is written on its own line, best-effort, since concatenating
    arbitrary raw bodies has no single correct separator. Written via `cli_stdout()` so it lands on the real
    stdout even while the process reservation points `sys.stdout` at stderr.

    :param result: The value returned by the dispatched endpoint call
    :param output: The resolved `--output` value
    """
    if output == Output.NONE:
        return
    if output == Output.RAW:
        items = result if isinstance(result, list) else [result]
        print("\n".join(_raw_payload(item) for item in items), file=cli_stdout())
        return
    payload_func = _full_payload if output == Output.FULL else _response_payload
    payload = [payload_func(item) for item in result] if isinstance(result, list) else payload_func(result)
    print(json.dumps(payload, default=str), file=cli_stdout())


def _response_payload(item: RestResponse | BaseException) -> Any:
    """Return one call result's own JSON-friendly payload: its already-decoded response body.

    A captured exception (from `with_repeat()`/`with_concurrency()`'s own `return_exceptions=True`) renders
    as `{"error": "<detail>"}` rather than failing `json.dumps()` outright. A streaming response's own
    `response` is already `None`, so nothing further is needed for that case.

    :param item: One `RestResponse`, or a captured `Exception` (from `return_exceptions=True`)
    """
    if isinstance(item, BaseException):
        return {"error": _failure_detail(item)}
    return item.response


def _full_payload(item: RestResponse | BaseException) -> Any:
    """Return one call result's own `{status_code, headers, body}` envelope.

    A captured exception renders as `{"error": "<detail>"}`, matching `_response_payload()`'s own handling
    of the same case.

    :param item: One `RestResponse`, or a captured `Exception` (from `return_exceptions=True`)
    """
    if isinstance(item, BaseException):
        return {"error": _failure_detail(item)}
    return {"status_code": item.status_code, "headers": dict(item._response.headers), "body": item.response}


def _raw_payload(item: RestResponse | BaseException) -> str:
    """Return one call result's own undecoded response body, as text, exactly as the server sent it.

    A captured exception renders as its own one-line failure detail instead, since it never received a body
    to show.

    :param item: One `RestResponse`, or a captured `Exception` (from `return_exceptions=True`)
    """
    if isinstance(item, BaseException):
        return _failure_detail(item)
    return item._response.text


def _write_failure_summary(result: RestResponse | list[Any], expected_statuses: tuple[int, ...] = ()) -> None:
    """Write a single `error: ...` line to stderr for a failed call.

    :param result: The value returned by the dispatched endpoint call
    :param expected_statuses: Status codes given to `--with-expected-status`, if any
    """
    if isinstance(result, list):
        failed = [item for item in result if not _is_ok(item, expected_statuses)]
        details = ", ".join(dict.fromkeys(_failure_detail(item) for item in failed))
        summary = f"{len(failed)} of {len(result)} calls failed: {details}"
        if request_line := _request_line(result):
            summary = f"{request_line} - {summary}"
        write_error(summary)
    else:
        write_error(format_request_failure(result))


def _request_line(result: list[Any]) -> str | None:
    """Return `<METHOD> <url>` from the first item in a `with_repeat()`/`with_concurrency()` result that
    carries a request, since every item was dispatched against the same one.

    A captured `HTTPStatusError` (from a `raise_on_error` client under `return_exceptions=True`) carries its
    request via `item.response.request` rather than `get_request_from_exception()`: that exception is raised
    by `RestResponse.raise_for_status()` after a response was already received, not by the REST client's own
    `send()`, which is the only place that attaches the latter. Mirrors `_failure_detail()`'s own handling of
    this same case.

    Returns `None` if no item carries a request at all, which only happens when every call failed before a
    request could even be attached to its captured exception (`return_exceptions=True`).

    :param result: The value returned by a `with_repeat()`/`with_concurrency()` call
    """
    for item in result:
        if isinstance(item, RestResponse):
            request = item.request
        elif isinstance(item, HTTPStatusError):
            request = item.response.request
        else:
            request = get_request_from_exception(item)
        if request is not None:
            return f"{request.method.upper()} {request.url!s}"
    return None


def _failure_detail(item: RestResponse | BaseException) -> str:
    """Return one failed call result's own one-line failure detail.

    A captured `HTTPStatusError` (from a `raise_on_error` client under `return_exceptions=True`) renders in the
    same compact form as a `RestResponse` rather than httpx2's own verbose message. Any other captured exception renders
    as `<type>: <message>`.

    :param item: One `RestResponse`, or a captured `Exception` (from `return_exceptions=True`)
    """
    if isinstance(item, HTTPStatusError):
        return _compact_status(item.response)
    if isinstance(item, BaseException):
        return f"{type(item).__name__}: {item}"
    return _compact_status(item._response)


def _compact_status(response: Response) -> str:
    """Return `<status_code> (<reason>)` for a response, without a `request_id`.

    :param response: The failed response
    """
    status = f"{response.status_code}"
    if reason := get_response_reason(response):
        status += f" {reason}"
    return status


def _is_ok(result: Any, expected_statuses: tuple[int, ...] = ()) -> bool:
    """Return whether one call result counts as a success.

    A `RestResponse` counts as a success if it's a 2xx response, or its status is one of `expected_statuses`
    (given via `--with-expected-status`, which already asserted the match by raising otherwise). A captured
    `Exception` (`return_exceptions=True`) never does.

    :param result: One `RestResponse`, or a captured `Exception` (from `return_exceptions=True`)
    :param expected_statuses: Status codes given to `--with-expected-status`, if any
    """
    return isinstance(result, RestResponse) and (result.ok or result.status_code in expected_statuses)


def _exit_code(result: RestResponse | list[Any], expected_statuses: tuple[int, ...] = ()) -> int:
    """Return the process exit code for a dispatched call's result.

    A single result exits `0` iff `_is_ok()` on it. A `list` (from `with_repeat()`/`with_concurrency()`) exits
    `0` only if every item does.

    :param result: The value returned by the dispatched endpoint call
    :param expected_statuses: Status codes given to `--with-expected-status`, if any
    """
    if isinstance(result, list):
        return 0 if all(_is_ok(r, expected_statuses) for r in result) else 1
    return 0 if _is_ok(result, expected_statuses) else 1

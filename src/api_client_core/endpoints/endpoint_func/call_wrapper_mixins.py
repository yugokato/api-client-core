from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from contextvars import copy_context
from copy import copy
from functools import wraps
from typing import TYPE_CHECKING, Any, Concatenate, Generic, Literal, ParamSpec, Self, TypeVar, cast, overload

from common_libs.clients.rest_client.retry import BackoffStrategy, retry_on
from common_libs.job_executor import Job, run_concurrent
from common_libs.lock import AsyncLock, Lock
from common_libs.logging import get_logger

from ..stats import Stats

if TYPE_CHECKING:
    from ...base import BaseAPI
    from ...base.api_client import APIClient
    from ...types import RestResponse, _ResponseList, _ResponseOrExceptionList, _ResponsePages
    from ..endpoint import Endpoint
    from ..stats import StatsCollector


__all__ = ["AsyncCallWrapperMixin", "CallWrapperMixin", "SyncCallWrapperMixin"]


P = ParamSpec("P")
# _T is intentionally unparameterized: bound="CallWrapperMixin[Any]" widens the class-scoped P to Any in the return
# type of requires_instance-decorated methods that return Callable[P, R], which breaks the propagation of P
_T = TypeVar("_T", bound="CallWrapperMixin")  # type: ignore[type-arg]
_P = ParamSpec("_P")
_R = TypeVar("_R")
_F = TypeVar("_F", bound=Callable[..., Any])

_CallWrapper = Callable[[Callable[..., Any]], Callable[..., Any]]
# Where a with_xxx() wrapper composes relative to a multi-call wrapper (with_concurrency/with_repeat):
# - "call": wraps each individual call inside the group (with_retry, with_expected_status, with_max_response_time,
#   with_polling)
# - "calls": wraps the whole group of calls as one unit (with_lock, with_stats)
_WrapperScope = Literal["call", "calls"]

logger = get_logger(__name__)


def _terminal(f: _F) -> _F:
    """Mark a `with_xxx()` wrapper method as terminal.

    Sets `._terminal_wrapper` on the returned `EndpointFunc` copy so that `_with_wrapper` can raise if any further
    wrapper is chained after this one.
    """

    @wraps(f)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        wrapped = f(self, *args, **kwargs)
        wrapped._terminal_wrapper = f.__name__
        return wrapped

    return cast(_F, wrapper)


def requires_instance(f: Callable[Concatenate[_T, _P], _R]) -> Callable[Concatenate[_T, _P], _R]:
    @wraps(f)
    def wrapper(self: _T, *args: _P.args, **kwargs: _P.kwargs) -> _R:
        if self._instance is None:
            func_name = self._original_func.__name__ if f.__name__ == "__call__" else f.__name__
            raise TypeError(f"You cannot access {func_name}() directly through the {self._owner.__name__} class.")
        return f(self, *args, **kwargs)

    return wrapper


class CallWrapperMixin(Generic[P]):
    """Mixin providing the `with_xxx()` chainable call wrappers shared by sync and async endpoint funcs.

    Expects to be mixed into a concrete `EndpointFunc` subclass, which provides the attributes declared below.
    """

    # Provided by EndpointFunc.__init__
    api_client: APIClient | None
    endpoint: Endpoint[P]
    _instance: BaseAPI[Any] | None
    _owner: type[BaseAPI[Any]]
    _original_func: Callable[..., RestResponse]
    _base_call: Callable[..., Any] | None
    _call_wrappers: tuple[tuple[_CallWrapper, _WrapperScope], ...]
    _multi_call_wrapper: _CallWrapper | None  # with_concurrency() / with_repeat()
    _outermost_wrapper: _CallWrapper | None  # with_pagination()
    _terminal_wrapper: str | None
    _expected_status_codes: tuple[int, ...]

    @requires_instance
    def with_retry(
        self,
        condition: int
        | type[Exception]
        | Sequence[int | type[Exception]]
        | Callable[[RestResponse | Exception], bool] = lambda r: not r.ok,
        *,
        num_retries: int = 1,
        retry_after: float | int | Callable[[RestResponse | Exception], float | int] | BackoffStrategy = 5,
        safe_methods_only: bool = False,
    ) -> Self:
        """Return a configured, chainable endpoint func that retries on the given condition.

        Call the returned callable with the endpoint's own parameters, or chain with other with_xxx() wrappers before
        the final call. When chained before `with_concurrency()`/`with_repeat()`, the retry applies to each
        individual call in the group (for exception conditions too).

        :param condition: Either status code(s), a callable that takes the response object, or an exception class
                          (or tuple of exception classes) to retry on when raised
        :param num_retries: The max number of retries
        :param retry_after: Wait time in seconds before each retry. Accepts a number, a callable
                            `(response | exception) -> float`, or a `BackoffStrategy` instance for exponential backoff
                            with optional jitter.
        :param safe_methods_only: Only retry for safe HTTP methods (GET, HEAD, OPTIONS)
        """

        def call_with_retry(f: Callable[..., Any]) -> Callable[..., Any]:
            @wraps(f)
            def wrapper(*args: Any, **kwargs: Any) -> RestResponse:
                return retry_on(
                    condition,
                    num_retries=num_retries,
                    retry_after=retry_after,
                    safe_methods_only=safe_methods_only,
                    _async_mode=self.api_client.async_mode,
                )(f)(*args, **kwargs)

            return wrapper

        return self._with_wrapper(chain_wrapper=(call_with_retry, "call"))

    @requires_instance
    def with_lock(self, lock_name: str | None = None) -> Self:
        """Return a configured, chainable endpoint func that holds a distributed lock during the call.

        The lock is applied at the API endpoint function level, which means any other API calls in the same/other
        processes using the same API function will wait until the lock is acquired.

        Call the returned callable with the endpoint's own parameters, or chain with other with_xxx() wrappers before
        the final call.

        :param lock_name: Explicitly specify the lock name. Use this when the same lock needs to be
                          shared among multiple endpoints. Defaults to
                          '{app_name}-{APIClass}.{func_name}'.
        """
        if not lock_name:
            lock_name = f"{self.api_client.app_name}-{type(self._instance).__name__}.{self._original_func.__name__}"

        def call_with_lock(f: Callable[..., Any]) -> Callable[..., Any]:
            if self.api_client.async_mode:

                @wraps(f)
                async def wrapper(*args: Any, **kwargs: Any) -> RestResponse:
                    async with AsyncLock(lock_name):
                        return await f(*args, **kwargs)
            else:

                @wraps(f)
                def wrapper(*args: Any, **kwargs: Any) -> RestResponse:
                    with Lock(lock_name):
                        return f(*args, **kwargs)

            return wrapper

        return self._with_wrapper(chain_wrapper=(call_with_lock, "calls"))

    @requires_instance
    def with_expected_status(self, *status_codes: int) -> Self:
        """Return a configured, chainable endpoint func that asserts the response status code.

        Raises AssertionError if the response status code is not one of the expected codes.
        Call the returned callable with the endpoint's own parameters, or chain with other with_xxx() wrappers before
        the final call. When chained before `with_concurrency()`/`with_repeat()`, the assertion applies to each
        individual call in the group.

        When non-2xx status codes are provided on a client with `raise_on_error=True`, the `raise_for_status()` call
        is skipped for those codes.

        :param status_codes: One or more acceptable HTTP status codes
        """
        if not status_codes:
            raise ValueError("At least one expected status code must be given")

        def call_with_expected_status(f: Callable[..., Any]) -> Callable[..., Any]:
            def check(r: RestResponse) -> RestResponse:
                if r.status_code not in status_codes:
                    expected = "/".join(str(s) for s in status_codes)
                    raise AssertionError(f"Expected status code {expected}, but got {r.status_code}")
                return r

            if self.api_client.async_mode:

                @wraps(f)
                async def wrapper(*args: Any, **kwargs: Any) -> RestResponse:
                    return check(await f(*args, **kwargs))

            else:

                @wraps(f)
                def wrapper(*args: Any, **kwargs: Any) -> RestResponse:
                    return check(f(*args, **kwargs))

            return wrapper

        _self = self._with_wrapper(chain_wrapper=(call_with_expected_status, "call"))
        # The raise_on_error wrapper reads this at call time to exempt the expected codes from the raise.
        # Accumulate so stacked with_expected_status() calls all stay exempt. A response only reaches the raise
        # wrapper after passing every stacked assertion, so its status is already in the intersection of all
        # declared sets, which is a subset of this union.
        _self._expected_status_codes = (*_self._expected_status_codes, *status_codes)
        return _self

    @requires_instance
    def with_max_response_time(self, threshold_msecs: float | int) -> Self:
        """Return a configured, chainable endpoint func that asserts the response time.

        Raises AssertionError if the server response time exceeds threshold_msecs.

        Call the returned callable with the endpoint's own parameters, or chain with other with_xxx() wrappers before
        the final call. When chained before `with_concurrency()`/`with_repeat()`, the assertion applies to each
        individual call in the group.

        :param threshold_msecs: The maximum acceptable response time in milliseconds
        """

        def call_with_max_response_time(f: Callable[..., Any]) -> Callable[..., Any]:
            def check(r: RestResponse) -> RestResponse:
                if r.response_time * 1000 > threshold_msecs:
                    raise AssertionError(
                        f"Response time {int(r.response_time * 1000)} msecs exceeded the threshold of "
                        f"{threshold_msecs} msecs"
                    )
                return r

            if self.api_client.async_mode:

                @wraps(f)
                async def wrapper(*args: Any, **kwargs: Any) -> RestResponse:
                    return check(await f(*args, **kwargs))

            else:

                @wraps(f)
                def wrapper(*args: Any, **kwargs: Any) -> RestResponse:
                    return check(f(*args, **kwargs))

            return wrapper

        return self._with_wrapper(chain_wrapper=(call_with_max_response_time, "call"))

    @requires_instance
    def with_polling(
        self, until: Callable[[RestResponse], bool], *, interval: float | int = 5, timeout: float | int = 60
    ) -> Self:
        """Return a configured, chainable endpoint func that polls until a condition is met.

        Repeatedly calls the endpoint until until(response) returns True, waiting interval seconds between calls.
        Raises TimeoutError if the condition is not met within timeout seconds. Unlike with_retry
        (which retries on failure), this polls successful responses — e.g. for eventual consistency or async job
        completion. The endpoint is always called at least once.

        Call the returned callable with the endpoint's own parameters, or chain with other with_xxx() wrappers before
        the final call. When chained before `with_concurrency()`/`with_repeat()`, each individual call in the group
        polls independently.

        :param until: A callable taking the response object that returns True when polling should stop
        :param interval: Wait time in seconds between polls
        :param timeout: Maximum total time in seconds to keep polling before raising TimeoutError
        """

        def call_with_polling(f: Callable[..., Any]) -> Callable[..., Any]:
            msg = f"Polling condition was not met within {timeout} seconds"
            if self.api_client.async_mode:

                @wraps(f)
                async def wrapper(*args: Any, **kwargs: Any) -> RestResponse:
                    deadline = time.monotonic() + timeout
                    while True:
                        r = await f(*args, **kwargs)
                        if until(r):
                            return r
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError(msg)
                        await asyncio.sleep(min(interval, remaining))

            else:

                @wraps(f)
                def wrapper(*args: Any, **kwargs: Any) -> RestResponse:
                    deadline = time.monotonic() + timeout
                    while True:
                        r = f(*args, **kwargs)
                        if until(r):
                            return r
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError(msg)
                        time.sleep(min(interval, remaining))

            return wrapper

        return self._with_wrapper(chain_wrapper=(call_with_polling, "call"))

    @requires_instance
    def with_stats(self) -> Self:
        """Return a configured, chainable endpoint func that reports collected statistics after the call.

        Opens a scoped `Stats.collect()` block around the call and prints the scoped statistics table
        once the call completes (including on failure). The report covers only the calls made through
        this wrapper, so chaining with `with_concurrency`/`with_repeat` aggregates every call in the
        group.

        Call the returned callable with the endpoint's own parameters, or chain with other with_xxx()
        wrappers before the final call.
        """

        def call_with_stats(f: Callable[..., Any]) -> Callable[..., Any]:
            endpoint_str = str(self.endpoint)
            if self.api_client.async_mode:

                @wraps(f)
                async def wrapper(*args: Any, **kwargs: Any) -> RestResponse:
                    with Stats.collect(endpoint_str) as stats:
                        try:
                            return await f(*args, **kwargs)
                        finally:
                            _show_stats(stats)

            else:

                @wraps(f)
                def wrapper(*args: Any, **kwargs: Any) -> RestResponse:
                    with Stats.collect(endpoint_str) as stats:
                        try:
                            return f(*args, **kwargs)
                        finally:
                            _show_stats(stats)

            def _show_stats(stats: StatsCollector) -> None:
                # A reporting failure must not mask the exception raised by the API call itself
                try:
                    stats.show(endpoint=endpoint_str)
                except Exception as show_err:
                    logger.warning(f"Failed to show API statistics: {show_err}")

            return wrapper

        return self._with_wrapper(chain_wrapper=(call_with_stats, "calls"))

    @_terminal
    @requires_instance
    def with_pagination(
        self, get_next: Callable[[RestResponse], dict[str, Any] | None], *, limit: int | None = None
    ) -> Callable[P, _ResponsePages]:
        """Return a callable that iterates over paginated responses, one page per API call.

        Calls the endpoint, passes each response to `get_next`, and merges the returned params into the next call until
        `get_next` returns `None` (or `limit` is reached). `get_next` typically reads the next-page cursor/token from
        the response headers (`response._response.headers`) or body.
        Returning an empty dict continues with no new params, while returning `None` stops pagination.

        Pagination is always the outermost layer: any wrappers chained before it (e.g. `with_retry`) and the client's
        `raise_on_error` apply per page, inside the pagination loop. Call the returned callable with the endpoint's
        own parameters and iterate the result with `for` (sync) or `async for` (async).

        NOTE: This is terminal and must always be the last wrapper in a chain.

        :param get_next: A callable taking the latest response and returning a dict of endpoint parameter(s) to merge
                         into the next call (e.g. `{"cursor": "abc"}`), or `None` to stop pagination. The callable is
                         always invoked synchronously (no async `get_next`), so it should not perform blocking I/O.
        :param limit: Optional maximum number of pages (API calls) to fetch before stopping
        """
        if limit is not None and limit < 1:
            raise ValueError("limit must be a positive integer")

        def call_with_pagination(f: Callable[..., Any]) -> Callable[..., Any]:
            if self.api_client.async_mode:

                @wraps(f)
                async def wrapper(*args: Any, **kwargs: Any) -> Any:
                    call_kwargs = dict(kwargs)
                    page = 0
                    while True:
                        r = await f(*args, **call_kwargs)
                        yield r
                        page += 1
                        if limit is not None and page >= limit:
                            return
                        nxt = get_next(r)
                        if nxt is None:
                            return
                        call_kwargs = {**call_kwargs, **nxt}

            else:

                @wraps(f)
                def wrapper(*args: Any, **kwargs: Any) -> Any:
                    call_kwargs = dict(kwargs)
                    page = 0
                    while True:
                        r = f(*args, **call_kwargs)
                        yield r
                        page += 1
                        if limit is not None and page >= limit:
                            return
                        nxt = get_next(r)
                        if nxt is None:
                            return
                        call_kwargs = {**call_kwargs, **nxt}

            return wrapper

        return cast("Callable[P, _ResponsePages]", self._with_wrapper(outermost_wrapper=call_with_pagination))

    def _with_wrapper(
        self,
        *,
        chain_wrapper: tuple[_CallWrapper, _WrapperScope] | None = None,
        multi_call_wrapper: _CallWrapper | None = None,
        outermost_wrapper: _CallWrapper | None = None,
    ) -> Self:
        """Return a copy of this endpoint func with one added wrapper and a freshly composed `__call__`.

        :param chain_wrapper: A `(wrapper, scope)` pair to append to the chain (`with_retry`, `with_lock`, etc.)
        :param multi_call_wrapper: The multi-call wrapper to set (`with_concurrency`/`with_repeat`)
        :param outermost_wrapper: The absolute outermost wrapper to set (`with_pagination`)
        """
        if self._terminal_wrapper is not None:
            raise RuntimeError(
                f"`{self._terminal_wrapper}()` is terminal and must always be the last wrapper in a chain."
            )
        _self = copy(self)
        if chain_wrapper is not None:
            _self._call_wrappers = (*_self._call_wrappers, chain_wrapper)
        if multi_call_wrapper is not None:
            _self._multi_call_wrapper = multi_call_wrapper
        if outermost_wrapper is not None:
            _self._outermost_wrapper = outermost_wrapper
        _cls = type(type(_self).__name__, (type(_self),), {})
        _cls.__call__ = _self._build_call()  # type: ignore[method-assign]
        _self.__class__ = _cls
        return _self

    def _build_call(self) -> Callable[..., Any]:
        """Compose `__call__` from `_base_call` and the accumulated wrappers as nested layers.

        Innermost to outermost: `call` scoped chain wrappers, `raise_on_error`, the multi-call wrapper (if any),
        `calls` scoped chain wrappers, then the outermost wrapper (if any). A multi-call wrapper returns a list of
        responses, so once one is set, `call` scoped wrappers and `raise_on_error` compose around each individual
        call instead of around the returned list, while `calls` scoped wrappers still wrap the whole group.
        Without a multi-call wrapper the `call`/`calls` split is irrelevant, so every chain wrapper composes in
        chain order (first chained = outermost) inside `raise_on_error`. This is also what makes relative chain
        order between `call` and `calls` scoped wrappers meaningful in a single-call chain.
        Chain wrappers within a scope are applied in reverse so the first chained wrapper of that scope becomes
        the outermost within it (intuitive left-to-right reading).
        """
        assert self._base_call is not None  # always set for instance-bound funcs. with_xxx() requires an instance
        is_multi_call = self._multi_call_wrapper is not None
        call = self._base_call
        for wrapper, scope in reversed(self._call_wrappers):
            if not is_multi_call or scope == "call":
                call = wrapper(call)
        if self.api_client is not None and self.api_client.raise_on_error:
            call = self._make_raise_on_error_wrapper(call)
        if self._multi_call_wrapper is not None:
            call = self._multi_call_wrapper(call)
            for wrapper, scope in reversed(self._call_wrappers):
                if scope == "calls":
                    call = wrapper(call)
        if self._outermost_wrapper is not None:
            call = self._outermost_wrapper(call)
        return call

    def _make_raise_on_error_wrapper(self, f: Callable[..., Any]) -> Callable[..., Any]:
        """Return a sync or async wrapper that calls `raise_for_status()` on non-2xx responses.

        The wrapper branches on `async_mode` so it correctly handles both execution paths.
        It is applied outside stats recording, retry logic, and post-request hooks so they all run before any
        exception is raised. Status codes declared via `with_expected_status()` do not trigger the raise.
        When a multi-call wrapper (`with_concurrency`/`with_repeat`) is chained, this applies per individual call
        inside the group, since the raise fires per call rather than on the returned list.

        :param f: The callable to wrap (either the base call or the per-call unit of a composed chain)
        """
        if self.api_client.async_mode:

            @wraps(f)
            async def async_wrapper(*args: Any, **kwargs: Any) -> RestResponse:
                r = await f(*args, **kwargs)
                if not r.ok and r.status_code not in self._expected_status_codes:
                    r.raise_for_status()
                return r

            return async_wrapper
        else:

            @wraps(f)
            def sync_wrapper(*args: Any, **kwargs: Any) -> RestResponse:
                r = f(*args, **kwargs)
                if not r.ok and r.status_code not in self._expected_status_codes:
                    r.raise_for_status()
                return r

            return sync_wrapper


class SyncCallWrapperMixin(CallWrapperMixin[P]):
    """Mixin providing the sync `with_concurrency()`/`with_repeat()` multi-call wrappers."""

    @overload
    def with_concurrency(
        self, num: int = 2, *, max_connections: int | None = None, return_exceptions: Literal[False] = ...
    ) -> Callable[P, _ResponseList]: ...
    @overload
    def with_concurrency(
        self, num: int = 2, *, max_connections: int | None = None, return_exceptions: Literal[True]
    ) -> Callable[P, _ResponseOrExceptionList]: ...
    @_terminal
    @requires_instance
    def with_concurrency(
        self, num: int = 2, *, max_connections: int | None = None, return_exceptions: bool = False
    ) -> Callable[P, _ResponseList] | Callable[P, _ResponseOrExceptionList]:
        """Return a callable that concurrently makes duplicated API calls to the endpoint.

        Call the returned callable with the endpoint's own parameters.

        Wrappers chained before this one apply per individual call in the group (`with_retry`,
        `with_expected_status`, `with_max_response_time`, `with_polling`) or around the whole group
        (`with_lock`, `with_stats`), regardless of their relative chain order. With `raise_on_error=True`,
        `raise_for_status()` also applies per call: the first failure propagates `HTTPStatusError`, or the
        exceptions are collected in the returned list when `return_exceptions=True`.

        NOTE: This is terminal and must always be the last wrapper in a chain.

        :param num: Number of concurrent API calls
        :param max_connections: Maximum number of concurrent HTTP connections (i.e. `ThreadPoolExecutor` workers).
                                Use this to avoid `OSError: [Errno 24] Too many open files` when `num` is large.
        :param return_exceptions: If True, exceptions raised during calls are collected and included in the returned
                                  list instead of being propagated
        """

        def call_with_concurrency(f: Callable[..., Any]) -> Callable[..., Any]:
            @wraps(f)
            def wrapper(*args: Any, **kwargs: Any) -> list[RestResponse]:
                # ThreadPoolExecutor does not propagate contextvars to worker threads. Capture a
                # snapshot of the current context (including any active `Stats.collect()` scope)
                # per job so scoped stats see concurrent calls correctly.
                jobs = [Job(copy_context().run, (f, *args), kwargs) for _ in range(num)]
                return run_concurrent(jobs, max_workers=max_connections, return_exceptions=return_exceptions)

            return wrapper

        return cast(
            "Callable[P, _ResponseList] | Callable[P, _ResponseOrExceptionList]",
            self._with_wrapper(multi_call_wrapper=call_with_concurrency),
        )

    @overload
    def with_repeat(self, num: int = 2, *, return_exceptions: Literal[False] = ...) -> Callable[P, _ResponseList]: ...
    @overload
    def with_repeat(
        self, num: int = 2, *, return_exceptions: Literal[True]
    ) -> Callable[P, _ResponseOrExceptionList]: ...
    @_terminal
    @requires_instance
    def with_repeat(
        self, num: int = 2, *, return_exceptions: bool = False
    ) -> Callable[P, _ResponseList] | Callable[P, _ResponseOrExceptionList]:
        """Return a callable that sequentially makes duplicated API calls to the endpoint.

        Call the returned callable with the endpoint's own parameters. The endpoint is called num times sequentially.
        When return_exceptions=True, exceptions are collected in the returned list instead of being propagated —
        so all num calls run even when some fail (KeyboardInterrupt/SystemExit still propagate).

        Wrappers chained before this one apply per individual call in the group (`with_retry`,
        `with_expected_status`, `with_max_response_time`, `with_polling`) or around the whole group
        (`with_lock`, `with_stats`), regardless of their relative chain order. With `raise_on_error=True`,
        `raise_for_status()` also applies per call: the first failure propagates `HTTPStatusError`, or the
        exceptions are collected in the returned list when `return_exceptions=True`.

        NOTE: This is terminal and must always be the last wrapper in a chain.

        :param num: Number of sequential API calls
        :param return_exceptions: If True, exceptions raised during calls are collected and included in the returned
                                  list instead of being propagated.
        """

        def call_with_repeat(f: Callable[..., Any]) -> Callable[..., Any]:
            @wraps(f)
            def wrapper(*args: Any, **kwargs: Any) -> list[RestResponse] | list[RestResponse | Exception]:
                if return_exceptions:
                    results: list[RestResponse | Exception] = []
                    for _ in range(num):
                        try:
                            results.append(f(*args, **kwargs))
                        except Exception as e:
                            results.append(e)
                    return results
                return [f(*args, **kwargs) for _ in range(num)]

            return wrapper

        return cast(
            "Callable[P, _ResponseList] | Callable[P, _ResponseOrExceptionList]",
            self._with_wrapper(multi_call_wrapper=call_with_repeat),
        )


class AsyncCallWrapperMixin(CallWrapperMixin[P]):
    """Mixin providing the async `with_concurrency()`/`with_repeat()` multi-call wrappers."""

    @overload
    def with_concurrency(
        self, num: int = 2, *, max_connections: int | None = None, return_exceptions: Literal[False] = ...
    ) -> Callable[P, _ResponseList]: ...
    @overload
    def with_concurrency(
        self, num: int = 2, *, max_connections: int | None = None, return_exceptions: Literal[True]
    ) -> Callable[P, _ResponseOrExceptionList]: ...
    @_terminal
    @requires_instance
    def with_concurrency(
        self, num: int = 2, *, max_connections: int | None = None, return_exceptions: bool = False
    ) -> Callable[P, _ResponseList] | Callable[P, _ResponseOrExceptionList]:
        """Return a coroutine callable that concurrently makes duplicated API calls to the endpoint.

        Call the returned callable with the endpoint's own parameters.

        Wrappers chained before this one apply per individual call in the group (`with_retry`,
        `with_expected_status`, `with_max_response_time`, `with_polling`) or around the whole group
        (`with_lock`, `with_stats`), regardless of their relative chain order. With `raise_on_error=True`,
        `raise_for_status()` also applies per call: the first failure propagates `HTTPStatusError`, or the
        exceptions are collected in the returned list when `return_exceptions=True`.

        NOTE: This is terminal and must always be the last wrapper in a chain.

        :param num: Number of concurrent API calls
        :param max_connections: Maximum number of concurrent HTTP connections. When set, a `asyncio.Semaphore`
                                limits active tasks to this value even when `num` is larger, preventing
                                resource exhaustion.
        :param return_exceptions: If True, exceptions raised during calls are collected and included in the returned
                                  list instead of being propagated.
        """

        def call_with_concurrency(f: Callable[..., Any]) -> Callable[..., Any]:
            @wraps(f)
            async def wrapper(*args: Any, **kwargs: Any) -> list[RestResponse] | list[RestResponse | Exception]:
                if return_exceptions:
                    # Catch only Exception so BaseException/CancelledError still propagate.
                    # asyncio.gather(return_exceptions=True) would capture and suppress them.
                    async def safe_f(*a: Any, **kw: Any) -> RestResponse | Exception:
                        try:
                            return await f(*a, **kw)
                        except Exception as e:
                            return e

                    target: Callable[..., Any] = safe_f
                else:
                    target = f

                if max_connections is not None:
                    sem = asyncio.Semaphore(max_connections)

                    async def _run() -> RestResponse | Exception:
                        async with sem:
                            return await target(*args, **kwargs)

                    coros = [_run() for _ in range(num)]
                else:
                    coros = [target(*args, **kwargs) for _ in range(num)]
                return list(await asyncio.gather(*coros))

            return wrapper

        return cast(
            "Callable[P, _ResponseList] | Callable[P, _ResponseOrExceptionList]",
            self._with_wrapper(multi_call_wrapper=call_with_concurrency),
        )

    @overload
    def with_repeat(self, num: int = 2, *, return_exceptions: Literal[False] = ...) -> Callable[P, _ResponseList]: ...
    @overload
    def with_repeat(
        self, num: int = 2, *, return_exceptions: Literal[True]
    ) -> Callable[P, _ResponseOrExceptionList]: ...
    @_terminal
    @requires_instance
    def with_repeat(
        self, num: int = 2, *, return_exceptions: bool = False
    ) -> Callable[P, _ResponseList] | Callable[P, _ResponseOrExceptionList]:
        """Return a coroutine callable that sequentially makes duplicated API calls to the endpoint.

        Call the returned callable with the endpoint's own parameters. The endpoint is called num times sequentially.
        When return_exceptions=True, exceptions are collected in the returned list instead of being propagated —
        so all num calls run even when some fail (KeyboardInterrupt/SystemExit/CancelledError still propagate).

        Wrappers chained before this one apply per individual call in the group (`with_retry`,
        `with_expected_status`, `with_max_response_time`, `with_polling`) or around the whole group
        (`with_lock`, `with_stats`), regardless of their relative chain order. With `raise_on_error=True`,
        `raise_for_status()` also applies per call: the first failure propagates `HTTPStatusError`, or the
        exceptions are collected in the returned list when `return_exceptions=True`.

        NOTE: This is terminal and must always be the last wrapper in a chain.

        :param num: Number of sequential API calls
        :param return_exceptions: If True, exceptions raised during calls are collected and included in the returned
                                  list instead of being propagated.
        """

        def call_with_repeat(f: Callable[..., Any]) -> Callable[..., Any]:
            @wraps(f)
            async def wrapper(*args: Any, **kwargs: Any) -> list[RestResponse] | list[RestResponse | Exception]:
                if return_exceptions:
                    results: list[RestResponse | Exception] = []
                    for _ in range(num):
                        try:
                            results.append(await f(*args, **kwargs))
                        except Exception as e:
                            results.append(e)
                    return results
                return [await f(*args, **kwargs) for _ in range(num)]

            return wrapper

        return cast(
            "Callable[P, _ResponseList] | Callable[P, _ResponseOrExceptionList]",
            self._with_wrapper(multi_call_wrapper=call_with_repeat),
        )

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine, Generator
from contextlib import asynccontextmanager, contextmanager
from functools import cache, partial, wraps
from typing import TYPE_CHECKING, Any, Concatenate, ParamSpec, TypeVar, cast

from common_libs.clients.rest_client import AsyncRestClient, RestClient
from common_libs.logging import get_logger
from common_libs.naming import to_class_name
from httpx import HTTPError

from ...types import EndpointModel, RestResponse, _QualNameReprMeta
from ..executors import AsyncExecutor, SyncExecutor
from ..stats import collect_stats
from ..utils import endpoint_call as endpoint_call_util
from ..utils import endpoint_model as endpoint_model_util
from .call_wrapper_mixins import AsyncCallWrapperMixin, CallWrapperMixin, SyncCallWrapperMixin, requires_instance

if TYPE_CHECKING:
    from ...base import BaseAPI
    from ...types import _ResponseStream
    from ..endpoint import Endpoint
    from ..endpoint_handler import EndpointHandler


__all__ = ["AsyncEndpointFunc", "EndpointFunc", "SyncEndpointFunc"]


P = ParamSpec("P")
# _T is intentionally unparameterized: requires_sync_def is applied across differently-parameterized EndpointFunc
# subclasses, and bound="EndpointFunc[Any]" would require binding a concrete P here
_T = TypeVar("_T", bound="EndpointFunc")  # type: ignore[type-arg]
_P = ParamSpec("_P")
_R = TypeVar("_R")

logger = get_logger(__name__)


def _as_response(f: Callable[_P, Awaitable[RestResponse]]) -> Callable[_P, RestResponse]:
    """Retype an async callable as a plain callable returning RestResponse.

    Applied to AsyncEndpointFunc.__call__ so that the SyncEndpointFunc | AsyncEndpointFunc union appears as a single
    non-coroutine callable type to the type checker.
    At runtime this is a no-op.
    """
    return cast(Callable[_P, RestResponse], f)


def _as_response_stream(f: Callable[_P, object]) -> Callable[_P, _ResponseStream]:
    """Retype a context-manager callable as returning the dual _ResponseStream.

    Applied to both SyncEndpointFunc.stream() and AsyncEndpointFunc.stream() so the
    SyncEndpointFunc | AsyncEndpointFunc union presents a single type that supports both `with` and `async with`.
    At runtime this is a no-op.
    """
    return cast(Callable[_P, "_ResponseStream"], f)


def requires_sync_def(f: Callable[Concatenate[_T, _P], _R]) -> Callable[Concatenate[_T, _P], _R]:
    """Raise if the API method or any request hook is defined with `async def`."""

    @wraps(f)
    def wrapper(self: _T, *args: _P.args, **kwargs: _P.kwargs) -> _R:
        for name, is_async in (
            (self._original_func.__name__, self._is_async_func),
            (self._owner.pre_request_hook.__name__, self._is_async_pre_request_hook),
            (self._owner.post_request_hook.__name__, self._is_async_post_request_hook),
        ):
            if is_async:
                raise RuntimeError(
                    f"`{self._owner.__name__}.{name}` is defined with `async def` but called by a sync client. "
                    f"Either use an async client (async_mode=True), or define the method with `def`."
                )
        return f(self, *args, **kwargs)

    return wrapper


class EndpointFunc(CallWrapperMixin[P], metaclass=_QualNameReprMeta):
    """Base class for Sync/Async Endpoint function classes"""

    executor: SyncExecutor[P] | AsyncExecutor[P] | None = None

    def __init__(self, endpoint_handler: EndpointHandler[P], instance: BaseAPI[Any] | None, owner: type[BaseAPI[Any]]):
        """Initialize endpoint function"""
        self.method = endpoint_handler.method
        self.path = endpoint_handler.path
        self.rest_client: RestClient | AsyncRestClient | None
        if instance:
            self.api_client = instance.api_client
            self.rest_client = self.api_client.rest_client
        else:
            self.api_client = None
            self.rest_client = None

        # State used by _build_call to compose with_xxx() wrappers in left-to-right (first=outermost) order
        self._call_wrappers = ()
        self._multi_call_wrapper = None  # with_concurrency() / with_repeat()
        self._outermost_wrapper = None  # with_pagination()
        self._base_call = None
        self._terminal_wrapper = None
        # Status codes declared via with_expected_status(), exempt from the client's raise_on_error
        self._expected_status_codes = ()

        self._instance = instance
        self._owner = owner
        self._original_func: Callable[..., RestResponse] = endpoint_handler.original_func
        self._use_query_string = endpoint_handler.use_query_string
        self._raw_options = endpoint_handler.default_raw_options
        self._model: type[EndpointModel] | None = None

        # An `async def` method is async-only. These are detected once here and enforced on the sync call paths.
        self._is_async_func = inspect.iscoroutinefunction(inspect.unwrap(self._original_func))
        self._is_async_pre_request_hook = inspect.iscoroutinefunction(inspect.unwrap(owner.pre_request_hook))
        self._is_async_post_request_hook = inspect.iscoroutinefunction(inspect.unwrap(owner.post_request_hook))

        self.endpoint = cast(
            "Endpoint[P]",
            owner._endpoint_class(
                api_class=owner,
                method=self.method,
                path=self.path,
                func_name=self._original_func.__name__,
                model=self.model,
                url=f"{self.rest_client.base_url.rstrip('/')}/{self.path.lstrip('/')}" if instance else None,
                content_type=endpoint_handler.content_type,
                is_public=endpoint_handler.is_public,
                is_documented=owner.is_documented and endpoint_handler.is_documented,
                is_deprecated=owner.is_deprecated or endpoint_handler.is_deprecated,
            ),
        )

        # Decorate the __call__ and stream() if wrappers are defined in the API class, or if decorators are
        # registered. If both request wrapper and endpoint decorators exist, endpoint decorators will be
        # processed first.
        #
        # A fresh per-instantiation subclass is created so wrappers are applied to an instance-private class
        # rather than to the shared cached class returned by _create().
        if instance:
            my_class = type(type(self).__name__, (type(self),), {})
            self.__class__ = my_class
            if request_wrappers := instance.request_wrapper():
                for request_wrapper in request_wrappers[::-1]:
                    my_class.__call__ = request_wrapper(my_class.__call__)  # type: ignore[method-assign]
            if stream_wrappers := instance.stream_wrapper():
                for stream_wrapper in stream_wrappers[::-1]:
                    my_class.stream = stream_wrapper(my_class.stream)  # type: ignore[attr-defined]
            for decorator in endpoint_handler.decorators:
                if isinstance(decorator, partial):
                    my_class.__call__ = decorator()(my_class.__call__)  # type: ignore[method-assign]
                else:
                    my_class.__call__ = decorator(my_class.__call__)  # type: ignore[method-assign]
            # Snapshot the fully-decorated __call__ as the base that with_xxx() wrappers compose around.
            # This must remain unwrapped so that with_xxx() chains compose on the clean base. The
            # raise_on_error wrapper (if active) is applied separately as the outermost layer below.
            self._base_call = my_class.__call__
            if instance.api_client.raise_on_error:
                my_class.__call__ = self._make_raise_on_error_wrapper(self._base_call)  # type: ignore[method-assign]

    def __repr__(self) -> str:
        return (
            f"<{type(self).__qualname__} object at {hex(id(self))}>\n"
            f"  endpoint: {self.endpoint}\n"
            f"  mapped to: {self._original_func!r}"
        )

    @requires_instance
    @collect_stats
    async def __call__(self, *args: P.args, **kwargs: P.kwargs) -> RestResponse:
        """Make an API call to the endpoint. This logic is commonly used for sync/acync API calls"""
        return await self._call(*args, **kwargs)  # type: ignore[arg-type]

    async def _call(
        self,
        *args: Any,
        quiet: bool = False,
        with_hooks: bool | None = True,
        raw_options: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> RestResponse:
        """Make an API call to the endpoint. This logic is commonly used for sync/async API calls

        Parameters can be passed either positionally or as keyword arguments. Path parameters are identified by
        matching their names against the `{placeholder}` tokens in the endpoint path. All remaining parameters are
        treated as body or query parameters.

        :param args: Endpoint parameters provided as positional arguments (path and/or body/query parameters)
        :param quiet: A flag to suppress API request/response log
        :param with_hooks: Invoke pre/post request hooks
        :param raw_options: Raw request options passed to the underlying HTTP library
        :param kwargs: Endpoint parameters provided as keyword arguments (path and/or body/query parameters)
        """
        path_params, body_or_query_params = endpoint_call_util.split_params(
            self._original_func, self.path, args, kwargs
        )
        path = endpoint_call_util.complete_endpoint(self.endpoint, path_params)
        endpoint_call_util.validate_params(self.endpoint, body_or_query_params, raw_options)

        # pre-request hook
        if with_hooks:
            await self._acall_pre_request_hook(path_params, body_or_query_params)

        # Make a request
        r = None
        exception = None
        try:
            # Call the original function first to make sure any custom function logic (if implemented) is executed.
            # If it returns a RestResponse obj, we will use it. If nothing is returned (the default behavior),
            # we will automatically make an API call
            # Undocumented endpoints manually added/updated by users might not always have **kwargs like the regular
            # endpoints updated/managed by our script. To avoid an error by giving unexpected keyword argument, we pass
            # parameters for rest client only when the user explicitly requests them
            call_kwargs: dict[str, Any] = {}
            if raw_options:
                call_kwargs.update(raw_options=raw_options)
            if quiet:
                call_kwargs.update(quiet=quiet)
            r = await self._call_original_func(args, kwargs, call_kwargs)
            if r is not None:
                if not isinstance(r, RestResponse):
                    raise RuntimeError(f"Custom endpoint must return a RestResponse object, got {type(r).__name__}")
            else:
                params = self._generate_call_params(quiet, raw_options, body_or_query_params)
                r = await self._call_api_func(path, params)
            return r
        except HTTPError as e:
            exception = e
            raise
        except BaseException:
            with_hooks = False
            raise
        finally:
            if with_hooks:
                await self._acall_post_request_hook(r, exception, path_params, body_or_query_params)

    @property
    def model(self) -> type[EndpointModel]:
        """Return the dynamically created model of the endpoint (created once per endpoint func and cached)"""
        if self._model is None:
            self._model = self._create_model()
        return self._model

    def help(self) -> None:
        """Display the API function definition"""
        help(self._original_func)

    @staticmethod
    @cache
    def _create(
        api_class: type[BaseAPI[Any]], orig_func: Callable[..., Any], async_mode: bool
    ) -> type[SyncEndpointFunc[Any]] | type[AsyncEndpointFunc[Any]]:
        """Dynamically create an EndpointFunc class for the given endpoint function"""
        base_class = api_class._async_endpoint_func_class if async_mode else api_class._sync_endpoint_func_class
        class_name = f"{api_class.__name__}{to_class_name(orig_func.__name__, suffix=EndpointFunc.__name__)}"
        return cast(type[SyncEndpointFunc[Any]] | type[AsyncEndpointFunc[Any]], type(class_name, (base_class,), {}))

    def _create_model(self) -> type[EndpointModel]:
        """Create the endpoint model.

        Override this in a subclass to customize model creation (e.g. to inject a custom field-name sanitizer).
        The result is cached by the `model` property.
        """
        return endpoint_model_util.create_endpoint_model(self)

    def _generate_call_params(
        self, quiet: bool, raw_options: dict[str, Any] | None, body_or_query_params: dict[str, Any]
    ) -> dict[str, Any]:
        """Generate params to pass to the underlying rest client call for a request."""
        sig_defaults = endpoint_call_util.get_signature_defaults(self._original_func, self.path)
        return endpoint_call_util.generate_rest_func_params(
            self.endpoint,
            {**sig_defaults, **body_or_query_params},
            self.rest_client.client.headers,
            quiet=quiet,
            use_query_string=self._use_query_string,
            **self._raw_options | (raw_options or {}),
        )

    async def _acall_pre_request_hook(self, path_params: tuple[Any, ...], body_or_query_params: dict[str, Any]) -> None:
        """Call `pre_request_hook`, awaiting the result when the hook is async."""
        result = self._instance.pre_request_hook(self.endpoint, *path_params, **body_or_query_params)
        if asyncio.iscoroutine(result):
            await result

    async def _acall_post_request_hook(
        self,
        r: RestResponse | None,
        exception: Exception | None,
        path_params: tuple[Any, ...],
        body_or_query_params: dict[str, Any],
    ) -> None:
        """Call `post_request_hook` with standard error handling, awaiting the result when the hook is async."""
        try:
            result = self._instance.post_request_hook(self.endpoint, r, exception, *path_params, **body_or_query_params)
            if asyncio.iscoroutine(result):
                await result
        except AssertionError:
            raise
        except Exception as e:
            logger.exception(e)

    async def _call_original_func(
        self, func_args: tuple[Any, ...], func_kwargs: dict[str, Any], kwargs: dict[str, Any]
    ) -> RestResponse | None:
        """Call the user-defined original endpoint function with the original args/kwargs.

        :param func_args: Positional arguments as received by __call__
        :param func_kwargs: Keyword arguments as received by __call__
        :param kwargs: Extra kwargs for the original func when explicitly set
        """
        r = self._original_func(self._instance, *func_args, **{**func_kwargs, **kwargs})
        if self.api_client.async_mode and asyncio.iscoroutine(r):
            # The original function returned a coroutine, either because it is itself `async def`, or because it
            # is a plain `def` that called the AsyncRestClient. Either way, we can await it and get the actual
            # value in here
            r = await r
        return r

    async def _call_api_func(self, path: str, params: dict[str, Any]) -> RestResponse:
        if self.api_client.async_mode:
            async_self = cast(AsyncEndpointFunc[Any], self)
            return await async_self.executor.execute(async_self, path, params)
        else:
            sync_self = cast(SyncEndpointFunc[Any], self)
            return sync_self.executor.execute(sync_self, path, params)


class SyncEndpointFunc(SyncCallWrapperMixin[P], EndpointFunc[P]):
    """Endpoint function class (Sync)

    All parameters passed to the original API class function call will be passed through to the __call__()
    """

    executor = SyncExecutor()

    @requires_instance
    @requires_sync_def
    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> RestResponse:
        """Make a sync API call to the endpoint"""
        return self._run_coroutine_sync(super().__call__(*args, **kwargs))

    @_as_response_stream
    @contextmanager
    @requires_instance
    def stream(self, *args: P.args, **kwargs: P.kwargs) -> Generator[RestResponse]:
        """Stream the response

        :param args: Endpoint parameters provided as positional arguments (path and/or body/query parameters)
        :param kwargs: Endpoint parameters provided as keyword arguments (path and/or body/query parameters)
        """
        with self._stream(*args, **kwargs) as r:  # type: ignore[arg-type]
            yield r

    @contextmanager
    @requires_sync_def
    def _stream(
        self,
        *args: Any,
        quiet: bool = False,
        with_hooks: bool | None = True,
        raw_options: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Generator[RestResponse]:
        """Stream the response (implementation)

        :param args: Endpoint parameters provided as positional arguments (path and/or body/query parameters)
        :param quiet: A flag to suppress API request/response log
        :param with_hooks: Invoke pre/post request hooks
        :param raw_options: Raw request options passed to the underlying HTTP library
        :param kwargs: Endpoint parameters provided as keyword arguments (path and/or body/query parameters)
        """
        path_params, body_or_query_params = endpoint_call_util.split_params(
            self._original_func, self.path, args, kwargs
        )
        path = endpoint_call_util.complete_endpoint(self.endpoint, path_params)
        endpoint_call_util.validate_params(self.endpoint, body_or_query_params, raw_options)
        if with_hooks:
            self._run_coroutine_sync(self._acall_pre_request_hook(path_params, body_or_query_params))
        params = self._generate_call_params(quiet, raw_options, body_or_query_params)
        r = None
        exception = None
        try:
            with self.executor.execute_stream(self, path, params) as r:
                yield r
        except HTTPError as e:
            exception = e
            raise
        except BaseException:
            with_hooks = False
            raise
        finally:
            if with_hooks:
                self._run_coroutine_sync(self._acall_post_request_hook(r, exception, path_params, body_or_query_params))

    @staticmethod
    def _run_coroutine_sync(coro: Coroutine[Any, Any, _R]) -> _R:
        """Drive a non-suspending coroutine to completion without an event loop.

        The sync request path is async-shaped only to share `EndpointFunc._call` with the async path.
        In sync mode none of its awaits suspend (the sync executor performs blocking I/O), so stepping
        the coroutine directly avoids per-call event loop creation. This is also what allows a custom
        sync endpoint body to call another sync endpoint (re-entrant/nested call) without hitting
        `asyncio.run() cannot be called from a running event loop`.

        :param coro: A coroutine that completes without ever suspending on the event loop
        """
        try:
            coro.send(None)
        except StopIteration as e:
            return cast(_R, e.value)
        coro.close()
        raise RuntimeError(
            "A sync API call unexpectedly awaited a real async operation. Use async_mode=True for async execution."
        )


class AsyncEndpointFunc(AsyncCallWrapperMixin[P], EndpointFunc[P]):
    """Endpoint function class (Async)

    All parameters passed to the original API class function call will be passed through to the __call__()
    """

    executor = AsyncExecutor()

    @_as_response
    @requires_instance
    async def __call__(self, *args: P.args, **kwargs: P.kwargs) -> RestResponse:
        """Make an async API call to the endpoint"""
        return await super().__call__(*args, **kwargs)

    @_as_response_stream
    @asynccontextmanager
    @requires_instance
    async def stream(self, *args: P.args, **kwargs: P.kwargs) -> AsyncGenerator[RestResponse]:
        """Stream response from an API call to the endpoint

        :param args: Endpoint parameters provided as positional arguments (path and/or body/query parameters)
        :param kwargs: Endpoint parameters provided as keyword arguments (path and/or body/query parameters)
        """
        async with self._stream(*args, **kwargs) as r:  # type: ignore[arg-type]
            yield r

    @asynccontextmanager
    async def _stream(
        self,
        *args: Any,
        quiet: bool = False,
        with_hooks: bool | None = True,
        raw_options: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[RestResponse]:
        """Stream response from an API call to the endpoint (implementation)

        :param args: Endpoint parameters provided as positional arguments (path and/or body/query parameters)
        :param quiet: A flag to suppress API request/response log
        :param with_hooks: Invoke pre/post request hooks
        :param raw_options: Raw request options passed to the underlying HTTP library
        :param kwargs: Endpoint parameters provided as keyword arguments (path and/or body/query parameters)
        """
        path_params, body_or_query_params = endpoint_call_util.split_params(
            self._original_func, self.path, args, kwargs
        )
        path = endpoint_call_util.complete_endpoint(self.endpoint, path_params)
        endpoint_call_util.validate_params(self.endpoint, body_or_query_params, raw_options)
        if with_hooks:
            await self._acall_pre_request_hook(path_params, body_or_query_params)
        params = self._generate_call_params(quiet, raw_options, body_or_query_params)
        r = None
        exception = None
        try:
            async with self.executor.execute_stream(self, path, params) as r:
                yield r
        except HTTPError as e:
            exception = e
            raise
        except BaseException:
            with_hooks = False
            raise
        finally:
            if with_hooks:
                await self._acall_post_request_hook(r, exception, path_params, body_or_query_params)

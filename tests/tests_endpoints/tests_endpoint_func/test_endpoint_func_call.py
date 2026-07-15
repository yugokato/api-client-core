"""Unit tests for endpoint_func.py (__call__(), stream())

NOTE: Any tests for with_xxx() chainable wrappers should be tested in test_endpoint_func_call_wrapper.py
"""

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager, contextmanager
from functools import wraps
from typing import Any
from unittest.mock import MagicMock

import pytest
from common_libs.clients.rest_client import RestResponse
from common_libs.clients.rest_client.types import Request, Response
from httpx import AsyncClient, Client, ConnectError, HTTPError, HTTPStatusError
from pytest_mock import MockerFixture

import api_client_core.endpoints.endpoint_func.endpoint_func as _endpoint_func_module
import api_client_core.endpoints.utils.endpoint_call as endpoint_call_util
from api_client_core.base import APIClient, BaseAPI
from api_client_core.endpoints import AsyncEndpointFunc, SyncEndpointFunc, endpoint
from api_client_core.endpoints.executors import AsyncExecutor, SyncExecutor
from api_client_core.types import Unset


class TestSyncEndpointFuncCall:
    """Tests for SyncEndpointFunc.__call__ sync execution path"""

    def test_sync_call_returns_rest_response(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that SyncEndpointFunc.__call__ returns a RestResponse"""
        mock_httpx_request = mocker.patch.object(Client, "request")
        instance = api_class(api_client)
        r = instance.get_something()
        assert isinstance(r, RestResponse)
        mock_httpx_request.assert_called_once()

    def test_sync_call_uses_sync_executor(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that SyncEndpointFunc uses SyncExecutor to execute the HTTP request"""
        mock_httpx_request = mocker.patch.object(Client, "request")
        instance = api_class(api_client)
        endpoint_func = instance.get_something
        assert isinstance(endpoint_func, SyncEndpointFunc)
        assert isinstance(endpoint_func.executor, SyncExecutor)

        instance.get_something()
        mock_httpx_request.assert_called_once()

    def test_sync_call_invokes_pre_and_post_hooks(self, mocker: MockerFixture, api_client: APIClient) -> None:
        """Test that pre_request_hook and post_request_hook are called during sync execution"""
        call_stack: list[str] = []

        def mock_httpx_side_effect(*args: Any, **kwargs: Any) -> Response:
            call_stack.append("call")
            mock_response = mocker.MagicMock(spec=Response)
            mock_response.status_code = 200
            mock_response.is_stream = False
            return mock_response

        mocker.patch.object(Client, "request", side_effect=mock_httpx_side_effect)

        class HookedAPI(BaseAPI):
            app_name = api_client.app_name

            def pre_request_hook(self, *args: Any, **kwargs: Any) -> None:
                call_stack.append("pre")

            def post_request_hook(self, *args: Any, **kwargs: Any) -> None:
                call_stack.append("post")

            @endpoint.get("/v1/something")
            def get_something(self) -> RestResponse: ...

        instance = HookedAPI(api_client)
        instance.get_something()

        assert call_stack == ["pre", "call", "post"]

    @pytest.mark.parametrize("with_hooks", [True, False])
    def test_sync_call_flow_with_decorators_and_wrappers(
        self, mocker: MockerFixture, api_client: APIClient, with_hooks: bool
    ) -> None:
        """Test that endpoint decorators and request wrappers fire in correct order in sync mode"""
        call_stack: list[str] = []

        def mock_httpx_side_effect(*args: Any, **kwargs: Any) -> Response:
            call_stack.append("call")
            mock_response = mocker.MagicMock(spec=Response)
            mock_response.status_code = 200
            mock_response.is_stream = False
            return mock_response

        mocker.patch.object(Client, "request", side_effect=mock_httpx_side_effect)

        @endpoint.decorator
        def deco1(f: Callable[..., Any]) -> Callable[..., Any]:
            @wraps(f)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                call_stack.append("deco1_before")
                result = f(*args, **kwargs)
                call_stack.append("deco1_after")
                return result

            return wrapper

        @endpoint.decorator
        def deco2(f: Callable[..., Any]) -> Callable[..., Any]:
            @wraps(f)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                call_stack.append("deco2_before")
                result = f(*args, **kwargs)
                call_stack.append("deco2_after")
                return result

            return wrapper

        class SyncHookedAPI(BaseAPI):
            app_name = api_client.app_name

            def pre_request_hook(self, *args: Any, **kwargs: Any) -> None:
                call_stack.append("pre_request")

            def post_request_hook(self, *args: Any, **kwargs: Any) -> None:
                call_stack.append("post_request")

            def request_wrapper(self) -> list[Callable[..., Any]]:
                def request_wrapper1(f: Callable[..., Any]) -> Callable[..., Any]:
                    @wraps(f)
                    def inner(*args: Any, **kwargs: Any) -> Any:
                        call_stack.append("request_wrapper1_before")
                        result = f(*args, **kwargs)
                        call_stack.append("request_wrapper1_after")
                        return result

                    return inner

                def request_wrapper2(f: Callable[..., Any]) -> Callable[..., Any]:
                    @wraps(f)
                    def inner(*args: Any, **kwargs: Any) -> Any:
                        call_stack.append("request_wrapper2_before")
                        result = f(*args, **kwargs)
                        call_stack.append("request_wrapper2_after")
                        return result

                    return inner

                return [request_wrapper1, request_wrapper2]

            @deco1
            @deco2
            @endpoint.get("/v1/something")
            def get_something(self) -> RestResponse: ...

        instance = SyncHookedAPI(api_client)
        assert call_stack == []
        instance.get_something(with_hooks=with_hooks)

        if with_hooks:
            assert call_stack == [
                "deco1_before",
                "deco2_before",
                "request_wrapper1_before",
                "request_wrapper2_before",
                "pre_request",
                "call",
                "post_request",
                "request_wrapper2_after",
                "request_wrapper1_after",
                "deco2_after",
                "deco1_after",
            ]
        else:
            assert call_stack == [
                "deco1_before",
                "deco2_before",
                "request_wrapper1_before",
                "request_wrapper2_before",
                "call",
                "request_wrapper2_after",
                "request_wrapper1_after",
                "deco2_after",
                "deco1_after",
            ]

    def test_sync_call_with_single_callable_arg_factory_decorator(
        self, mocker: MockerFixture, api_client: APIClient
    ) -> None:
        """Test that a registered decorator factory called with a single bare callable actually fires during a
        sync call
        """
        mocker.patch.object(Client, "request")
        call_stack: list[str] = []

        @endpoint.decorator
        def with_callback(
            callback: Callable[[RestResponse], None],
        ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
            def decorator(f: Callable[..., Any]) -> Callable[..., Any]:
                @wraps(f)
                def wrapper(*args: Any, **kwargs: Any) -> Any:
                    result = f(*args, **kwargs)
                    callback(result)
                    return result

                return wrapper

            return decorator

        def record_response(response: RestResponse) -> None:
            call_stack.append("callback")

        class SyncCallbackAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.get("/v1/something")
            @with_callback(record_response)
            def get_something(self) -> RestResponse: ...

        instance = SyncCallbackAPI(api_client)
        r = instance.get_something()

        assert isinstance(r, RestResponse)
        assert call_stack == ["callback"]

    def test_sync_call_with_custom_body_returning_rest_response(
        self, mocker: MockerFixture, api_client: APIClient
    ) -> None:
        """Test that a custom sync endpoint body returning RestResponse bypasses auto-generated request path"""
        mocker.patch.object(Client, "request")
        f = endpoint_call_util.generate_rest_func_params
        spy_generate = mocker.patch(f"{f.__module__}.{f.__name__}")

        class SyncCustomAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.get("/v1/something")
            def get_something(self) -> RestResponse:
                return self.api_client.rest_client.get("/v1/something")

        instance = SyncCustomAPI(api_client)
        r = instance.get_something()

        assert isinstance(r, RestResponse)
        spy_generate.assert_not_called()

    def test_sync_call_with_custom_body_wrong_return_type(self, mocker: MockerFixture, api_client: APIClient) -> None:
        """Test that a custom sync endpoint body returning a non-RestResponse raises RuntimeError"""
        mocker.patch.object(Client, "request")

        class SyncBadReturnAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.get("/v1/something")
            def get_something(self) -> RestResponse:
                return "not a response"

        instance = SyncBadReturnAPI(api_client)
        with pytest.raises(RuntimeError, match="Custom endpoint must return a RestResponse object, got str"):
            instance.get_something()

    def test_sync_call_http_error_propagates(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that HTTPError raised during sync execution propagates to the caller"""
        mock_request = mocker.MagicMock(spec=Request)
        connect_error = ConnectError("connection error")
        connect_error.request = mock_request
        mocker.patch.object(Client, "request", side_effect=connect_error)
        instance = api_class(api_client)
        with pytest.raises(HTTPError, match="connection error"):
            instance.get_something()

    def test_sync_call_http_error_still_runs_post_hook(self, mocker: MockerFixture, api_client: APIClient) -> None:
        """Test that post_request_hook is still called even when an HTTPError occurs in sync mode"""
        post_hook_called = False
        mock_request = mocker.MagicMock(spec=Request)
        connect_error = ConnectError("timeout")
        connect_error.request = mock_request

        mocker.patch.object(Client, "request", side_effect=connect_error)

        class HookedAPI(BaseAPI):
            app_name = api_client.app_name

            def post_request_hook(self, *args: Any, **kwargs: Any) -> None:
                nonlocal post_hook_called
                post_hook_called = True

            @endpoint.get("/v1/something")
            def get_something(self) -> RestResponse: ...

        instance = HookedAPI(api_client)
        with pytest.raises(HTTPError):
            instance.get_something()

        assert post_hook_called is True

    def test_sync_call_works_inside_running_event_loop(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that a sync endpoint call succeeds even when invoked from within a running event loop"""
        mocker.patch.object(Client, "request")
        instance = api_class(api_client)

        async def _call() -> RestResponse:
            return instance.get_something()

        r = asyncio.run(_call())
        assert isinstance(r, RestResponse)

    def test_sync_call_supports_nested_endpoint_call(self, mocker: MockerFixture, api_client: APIClient) -> None:
        """Test that a custom sync endpoint body can call another sync endpoint (re-entrant call)"""
        mocker.patch.object(Client, "request")

        class NestedAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.get("/v1/inner")
            def get_inner(self) -> RestResponse: ...

            @endpoint.get("/v1/outer")
            def get_outer(self) -> RestResponse:
                return self.get_inner()

        instance = NestedAPI(api_client)
        r = instance.get_outer()
        assert isinstance(r, RestResponse)

    def test_sync_with_concurrency_makes_multiple_calls(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_concurrency in sync mode issues N concurrent HTTP requests"""
        mock_httpx_request = mocker.patch.object(Client, "request")
        instance = api_class(api_client)
        endpoint_func = instance.get_something

        assert isinstance(endpoint_func, SyncEndpointFunc)

        results = endpoint_func.with_concurrency(num=3)()
        assert len(results) == 3
        assert all(isinstance(r, RestResponse) for r in results)
        assert mock_httpx_request.call_count == 3

    def test_sync_with_concurrency_collects_exceptions_with_return_exceptions(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_concurrency(return_exceptions=True) collects all exceptions instead of propagating"""
        mocker.patch.object(Client, "request", side_effect=ValueError("always fails"))
        instance = api_class(api_client)

        results = instance.get_something.with_concurrency(num=3, return_exceptions=True)()

        assert len(results) == 3
        assert all(isinstance(r, ValueError) for r in results)
        assert Client.request.call_count == 3

    def test_sync_with_concurrency_propagates_bare_exception(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_concurrency propagates a bare exception (not wrapped) when return_exceptions=False"""
        mocker.patch.object(Client, "request", side_effect=ValueError("always fails"))
        instance = api_class(api_client)

        with pytest.raises(ValueError, match="always fails"):
            instance.get_something.with_concurrency(num=3)()

    def test_sync_call_uses_endpoint_path(self, mocker: MockerFixture, api_client: APIClient) -> None:
        """Test that the sync endpoint call uses the configured endpoint path"""
        mock_httpx_request = mocker.patch.object(Client, "request")

        class PathAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.get("/v1/items")
            def get_items(self) -> RestResponse: ...

        instance = PathAPI(api_client)
        instance.get_items()

        call_args = mock_httpx_request.call_args
        assert call_args.args == ("GET", "/v1/items")

    def test_sync_call_on_deprecated_endpoint_logs_warning(self, mocker: MockerFixture, api_client: APIClient) -> None:
        """Test that calling a deprecated endpoint logs a DEPRECATED warning"""
        mocker.patch.object(Client, "request")
        mock_log = mocker.patch.object(endpoint_call_util, "logger")

        class DeprecatedAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.is_deprecated
            @endpoint.get("/v1/something")
            def get_something(self) -> RestResponse: ...

        instance = DeprecatedAPI(api_client)
        instance.get_something()

        warning_messages = [call[0][0] for call in mock_log.warning.call_args_list]
        assert any("DEPRECATED" in msg for msg in warning_messages)


class TestAsyncEndpointFuncCall:
    """Tests for AsyncEndpointFunc.__call__ async execution path"""

    async def test_async_call_returns_rest_response(
        self, mocker: MockerFixture, api_client_async: APIClient, api_class_async: type[BaseAPI]
    ) -> None:
        """Test that AsyncEndpointFunc.__call__ returns a RestResponse when awaited"""
        mocker.patch.object(AsyncClient, "request")
        instance = api_class_async(api_client_async)
        r = await instance.get_something()
        assert isinstance(r, RestResponse)

    async def test_async_call_uses_async_executor(
        self, mocker: MockerFixture, api_client_async: APIClient, api_class_async: type[BaseAPI]
    ) -> None:
        """Test that AsyncEndpointFunc uses AsyncExecutor to execute the HTTP request"""
        mock_httpx_request = mocker.patch.object(AsyncClient, "request")
        instance = api_class_async(api_client_async)
        endpoint_func = instance.get_something
        assert isinstance(endpoint_func, AsyncEndpointFunc)
        assert isinstance(endpoint_func.executor, AsyncExecutor)

        await instance.get_something()
        mock_httpx_request.assert_called_once()

    async def test_async_call_invokes_pre_and_post_hooks(
        self, mocker: MockerFixture, api_client_async: APIClient
    ) -> None:
        """Test that pre_request_hook and post_request_hook are called during async execution"""
        call_stack: list[str] = []

        def mock_httpx_side_effect(*args: Any, **kwargs: Any) -> Response:
            call_stack.append("call")
            mock_response = mocker.MagicMock(spec=Response)
            mock_response.status_code = 200
            mock_response.is_stream = False
            return mock_response

        mocker.patch.object(AsyncClient, "request", side_effect=mock_httpx_side_effect)

        class HookedAPI(BaseAPI):
            app_name = api_client_async.app_name

            def pre_request_hook(self, *args: Any, **kwargs: Any) -> None:
                call_stack.append("pre")

            def post_request_hook(self, *args: Any, **kwargs: Any) -> None:
                call_stack.append("post")

            @endpoint.get("/v1/something")
            def get_something(self) -> RestResponse: ...

        instance = HookedAPI(api_client_async)
        await instance.get_something()

        assert call_stack == ["pre", "call", "post"]

    @pytest.mark.parametrize("with_hooks", [True, False])
    async def test_async_call_flow_with_decorators_and_wrappers(
        self, mocker: MockerFixture, api_client_async: APIClient, with_hooks: bool
    ) -> None:
        """Test that endpoint decorators and request wrappers fire in correct order in async mode"""
        call_stack: list[str] = []

        def mock_httpx_side_effect(*args: Any, **kwargs: Any) -> Response:
            call_stack.append("call")
            mock_response = mocker.MagicMock(spec=Response)
            mock_response.status_code = 200
            mock_response.is_stream = False
            return mock_response

        mocker.patch.object(AsyncClient, "request", side_effect=mock_httpx_side_effect)

        @endpoint.decorator
        def deco1(f: Callable[..., Any]) -> Callable[..., Any]:
            @wraps(f)
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                call_stack.append("deco1_before")
                result = await f(*args, **kwargs)
                call_stack.append("deco1_after")
                return result

            return wrapper

        @endpoint.decorator
        def deco2(f: Callable[..., Any]) -> Callable[..., Any]:
            @wraps(f)
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                call_stack.append("deco2_before")
                result = await f(*args, **kwargs)
                call_stack.append("deco2_after")
                return result

            return wrapper

        class AsyncHookedAPI(BaseAPI):
            app_name = api_client_async.app_name

            def pre_request_hook(self, *args: Any, **kwargs: Any) -> None:
                call_stack.append("pre_request")

            def post_request_hook(self, *args: Any, **kwargs: Any) -> None:
                call_stack.append("post_request")

            def request_wrapper(self) -> list[Callable[..., Any]]:
                def request_wrapper1(f: Callable[..., Any]) -> Callable[..., Any]:
                    @wraps(f)
                    async def inner(*args: Any, **kwargs: Any) -> Any:
                        call_stack.append("request_wrapper1_before")
                        result = await f(*args, **kwargs)
                        call_stack.append("request_wrapper1_after")
                        return result

                    return inner

                def request_wrapper2(f: Callable[..., Any]) -> Callable[..., Any]:
                    @wraps(f)
                    async def inner(*args: Any, **kwargs: Any) -> Any:
                        call_stack.append("request_wrapper2_before")
                        result = await f(*args, **kwargs)
                        call_stack.append("request_wrapper2_after")
                        return result

                    return inner

                return [request_wrapper1, request_wrapper2]

            @deco1
            @deco2
            @endpoint.get("/v1/something")
            def get_something(self) -> RestResponse: ...

        instance = AsyncHookedAPI(api_client_async)
        assert call_stack == []
        await instance.get_something(with_hooks=with_hooks)

        if with_hooks:
            assert call_stack == [
                "deco1_before",
                "deco2_before",
                "request_wrapper1_before",
                "request_wrapper2_before",
                "pre_request",
                "call",
                "post_request",
                "request_wrapper2_after",
                "request_wrapper1_after",
                "deco2_after",
                "deco1_after",
            ]
        else:
            assert call_stack == [
                "deco1_before",
                "deco2_before",
                "request_wrapper1_before",
                "request_wrapper2_before",
                "call",
                "request_wrapper2_after",
                "request_wrapper1_after",
                "deco2_after",
                "deco1_after",
            ]

    async def test_async_call_with_single_callable_arg_factory_decorator(
        self, mocker: MockerFixture, api_client_async: APIClient
    ) -> None:
        """Test that a registered decorator factory called with a single bare callable actually fires during an
        async call
        """
        mocker.patch.object(AsyncClient, "request")
        call_stack: list[str] = []

        @endpoint.decorator
        def with_callback(
            callback: Callable[[RestResponse], None],
        ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
            def decorator(f: Callable[..., Any]) -> Callable[..., Any]:
                @wraps(f)
                async def wrapper(*args: Any, **kwargs: Any) -> Any:
                    result = await f(*args, **kwargs)
                    callback(result)
                    return result

                return wrapper

            return decorator

        def record_response(response: RestResponse) -> None:
            call_stack.append("callback")

        class AsyncCallbackAPI(BaseAPI):
            app_name = api_client_async.app_name

            @endpoint.get("/v1/something")
            @with_callback(record_response)
            def get_something(self) -> RestResponse: ...

        instance = AsyncCallbackAPI(api_client_async)
        r = await instance.get_something()

        assert isinstance(r, RestResponse)
        assert call_stack == ["callback"]

    async def test_async_call_with_custom_body_returning_rest_response(
        self, mocker: MockerFixture, api_client_async: APIClient
    ) -> None:
        """Test that a custom async endpoint body returning RestResponse bypasses auto-generated request path"""
        mocker.patch.object(AsyncClient, "request")
        f = endpoint_call_util.generate_rest_func_params
        spy_generate = mocker.patch(f"{f.__module__}.{f.__name__}")

        class AsyncCustomAPI(BaseAPI):
            app_name = api_client_async.app_name

            @endpoint.get("/v1/something")
            def get_something(self) -> RestResponse:
                return self.api_client.rest_client.get("/v1/something")

        instance = AsyncCustomAPI(api_client_async)
        r = await instance.get_something()

        assert isinstance(r, RestResponse)
        spy_generate.assert_not_called()

    async def test_async_call_with_custom_body_wrong_return_type(
        self, mocker: MockerFixture, api_client_async: APIClient
    ) -> None:
        """Test that a custom async endpoint body returning non-RestResponse raises RuntimeError"""
        mocker.patch.object(AsyncClient, "request")

        class AsyncBadReturnAPI(BaseAPI):
            app_name = api_client_async.app_name

            @endpoint.get("/v1/something")
            def get_something(self) -> RestResponse:
                return "not a response"

        instance = AsyncBadReturnAPI(api_client_async)
        with pytest.raises(RuntimeError, match="Custom endpoint must return a RestResponse object, got str"):
            await instance.get_something()

    async def test_async_call_http_error_propagates(
        self, mocker: MockerFixture, api_client_async: APIClient, api_class_async: type[BaseAPI]
    ) -> None:
        """Test that HTTPError raised during async execution propagates to the caller"""
        mock_request = mocker.MagicMock(spec=Request)
        connect_error = ConnectError("connection error")
        connect_error.request = mock_request
        mocker.patch.object(AsyncClient, "request", side_effect=connect_error)
        instance = api_class_async(api_client_async)
        with pytest.raises(HTTPError, match="connection error"):
            await instance.get_something()

    async def test_async_call_http_error_still_runs_post_hook(
        self, mocker: MockerFixture, api_client_async: APIClient
    ) -> None:
        """Test that post_request_hook is still called even when an HTTPError occurs in async mode"""
        post_hook_called = False
        mock_request = mocker.MagicMock(spec=Request)
        connect_error = ConnectError("timeout")
        connect_error.request = mock_request

        mocker.patch.object(AsyncClient, "request", side_effect=connect_error)

        class HookedAPI(BaseAPI):
            app_name = api_client_async.app_name

            def post_request_hook(self, *args: Any, **kwargs: Any) -> None:
                nonlocal post_hook_called
                post_hook_called = True

            @endpoint.get("/v1/something")
            def get_something(self) -> RestResponse: ...

        instance = HookedAPI(api_client_async)
        with pytest.raises(HTTPError):
            await instance.get_something()

        assert post_hook_called is True

    async def test_async_with_concurrency_makes_multiple_calls(
        self, mocker: MockerFixture, api_client_async: APIClient, api_class_async: type[BaseAPI]
    ) -> None:
        """Test that with_concurrency in async mode issues N concurrent HTTP requests via asyncio.gather"""
        mock_httpx_request = mocker.patch.object(AsyncClient, "request")
        instance = api_class_async(api_client_async)
        endpoint_func = instance.get_something

        assert isinstance(endpoint_func, AsyncEndpointFunc)

        results = await endpoint_func.with_concurrency(num=3)()
        assert len(results) == 3
        assert all(isinstance(r, RestResponse) for r in results)
        assert mock_httpx_request.call_count == 3

    async def test_async_with_concurrency_collects_exceptions_with_return_exceptions(
        self, mocker: MockerFixture, api_client_async: APIClient, api_class_async: type[BaseAPI]
    ) -> None:
        """Test that async with_concurrency(return_exceptions=True) collects all exceptions instead of propagating"""
        mocker.patch.object(AsyncClient, "request", side_effect=ValueError("always fails"))
        instance = api_class_async(api_client_async)

        results = await instance.get_something.with_concurrency(num=3, return_exceptions=True)()

        assert len(results) == 3
        assert all(isinstance(r, ValueError) for r in results)
        assert AsyncClient.request.call_count == 3

    async def test_async_with_concurrency_propagates_bare_exception(
        self, mocker: MockerFixture, api_client_async: APIClient, api_class_async: type[BaseAPI]
    ) -> None:
        """Test that async with_concurrency propagates a bare exception when return_exceptions=False.

        Confirms parity with the sync path: the exception should be a plain exception, not wrapped in
        an ExceptionGroup.
        """
        mocker.patch.object(AsyncClient, "request", side_effect=ValueError("always fails"))
        instance = api_class_async(api_client_async)

        with pytest.raises(ValueError, match="always fails"):
            await instance.get_something.with_concurrency(num=3)()

    async def test_async_call_uses_endpoint_path(self, mocker: MockerFixture, api_client_async: APIClient) -> None:
        """Test that the async endpoint call uses the configured endpoint path"""
        mock_httpx_request = mocker.patch.object(AsyncClient, "request")

        class PathAPI(BaseAPI):
            app_name = api_client_async.app_name

            @endpoint.get("/v1/items")
            def get_items(self) -> RestResponse: ...

        instance = PathAPI(api_client_async)
        await instance.get_items()

        call_args = mock_httpx_request.call_args
        assert call_args.args == ("GET", "/v1/items")


class TestAsyncDefEndpointFuncCall:
    """Tests for `async def` endpoint function on an async client"""

    async def test_async_def_endpoint_empty_body_auto_generates_call(
        self, mocker: MockerFixture, api_client_async: APIClient
    ) -> None:
        """Test that an `async def` endpoint with an empty body auto-generates the REST call"""
        mocker.patch.object(AsyncClient, "request")
        spy_generate = mocker.spy(endpoint_call_util, "generate_rest_func_params")

        class AsyncDefEmptyBodyAPI(BaseAPI):
            app_name = api_client_async.app_name

            @endpoint.get("/v1/something")
            async def get_something(self) -> RestResponse: ...

        instance = AsyncDefEmptyBodyAPI(api_client_async)
        r = await instance.get_something()

        assert isinstance(r, RestResponse)
        spy_generate.assert_called_once()

    async def test_async_def_endpoint_custom_body_can_await_and_inspect_response_inline(
        self, mocker: MockerFixture, api_client_async: APIClient
    ) -> None:
        """Test that an `async def` endpoint body can await the rest client and inspect the response inline"""

        def mock_httpx_side_effect(*args: Any, **kwargs: Any) -> Response:
            mock_response = mocker.MagicMock(spec=Response)
            mock_response.status_code = 200
            mock_response.is_stream = False
            return mock_response

        mocker.patch.object(AsyncClient, "request", side_effect=mock_httpx_side_effect)
        f = endpoint_call_util.generate_rest_func_params
        spy_generate = mocker.patch(f"{f.__module__}.{f.__name__}")

        class AsyncDefCustomBodyAPI(BaseAPI):
            app_name = api_client_async.app_name

            @endpoint.get("/v1/something")
            async def get_something(self) -> RestResponse:
                r = await self.api_client.rest_client.get("/v1/something")
                assert r.status_code == 200
                return r

        instance = AsyncDefCustomBodyAPI(api_client_async)
        r = await instance.get_something()

        assert isinstance(r, RestResponse)
        assert r.status_code == 200
        spy_generate.assert_not_called()

    async def test_async_def_endpoint_body_composes_with_configurable_wrapper(
        self, mocker: MockerFixture, api_client_async: APIClient
    ) -> None:
        """Test that an `async def` endpoint body composes with a chainable `with_xxx()` wrapper"""
        mocker.patch.object(AsyncClient, "request", return_value=_make_httpx_response(200, mocker))

        class AsyncDefWrappedBodyAPI(BaseAPI):
            app_name = api_client_async.app_name

            @endpoint.get("/v1/something")
            async def get_something(self) -> RestResponse: ...

        instance = AsyncDefWrappedBodyAPI(api_client_async)
        r = await instance.get_something.with_retry().with_expected_status(200)()

        assert isinstance(r, RestResponse)
        assert r.status_code == 200

    async def test_async_def_endpoint_custom_body_with_path_param_can_await_and_inspect_response_inline(
        self, mocker: MockerFixture, api_client_async: APIClient
    ) -> None:
        """Test that an `async def` endpoint body with a path parameter awaits the rest client, receives the split
        path parameter, and inspects the response inline"""

        def mock_httpx_side_effect(*args: Any, **kwargs: Any) -> Response:
            mock_response = mocker.MagicMock(spec=Response)
            mock_response.status_code = 200
            mock_response.is_stream = False
            return mock_response

        mocker.patch.object(AsyncClient, "request", side_effect=mock_httpx_side_effect)

        class AsyncDefPathParamAPI(BaseAPI):
            app_name = api_client_async.app_name

            @endpoint.get("/v1/users/{username}")
            async def get_user(self, username: str) -> RestResponse:
                r = await self.api_client.rest_client.get(f"/v1/users/{username}")
                assert r.status_code == 200
                return r

        instance = AsyncDefPathParamAPI(api_client_async)
        r = await instance.get_user("alice")

        assert isinstance(r, RestResponse)
        assert r.status_code == 200
        call_args = AsyncClient.request.call_args
        assert call_args.args == ("GET", "/v1/users/alice")

    async def test_async_def_endpoint_custom_body_wrong_return_type_raises(
        self, mocker: MockerFixture, api_client_async: APIClient
    ) -> None:
        """Test that an `async def` endpoint body returning a non-RestResponse raises RuntimeError"""
        mocker.patch.object(AsyncClient, "request")

        class AsyncDefBadReturnAPI(BaseAPI):
            app_name = api_client_async.app_name

            @endpoint.get("/v1/something")
            async def get_something(self) -> RestResponse:
                return "not a response"

        instance = AsyncDefBadReturnAPI(api_client_async)
        with pytest.raises(RuntimeError, match="Custom endpoint must return a RestResponse object, got str"):
            await instance.get_something()

    async def test_async_def_endpoint_streams_without_running_body(
        self, mocker: MockerFixture, api_client_async: APIClient
    ) -> None:
        """Test that streaming an `async def` endpoint on an async client succeeds without executing the body"""
        body_called = False
        mock_resp = _make_stream_response()

        @asynccontextmanager
        async def fake_execute_stream(self_executor: AsyncExecutor, ef: Any, path: str, params: dict[str, Any]) -> Any:
            yield mock_resp

        mocker.patch.object(AsyncExecutor, "execute_stream", new=fake_execute_stream)

        class AsyncDefStreamAPI(BaseAPI):
            app_name = api_client_async.app_name

            @endpoint.get("/v1/something")
            async def get_something(self) -> RestResponse:
                nonlocal body_called
                body_called = True
                return await self.api_client.rest_client.get("/v1/something")

        instance = AsyncDefStreamAPI(api_client_async)
        async with instance.get_something.stream() as r:
            assert r is mock_resp

        assert body_called is False


class TestAsyncDefHooks:
    """Tests for `async def` pre_request_hook / post_request_hook on an async client"""

    async def test_async_def_pre_and_post_hooks_are_awaited(
        self, mocker: MockerFixture, api_client_async: APIClient
    ) -> None:
        """Test that `async def` pre_request_hook and post_request_hook are awaited during async execution"""
        call_stack: list[str] = []

        def mock_httpx_side_effect(*args: Any, **kwargs: Any) -> Response:
            call_stack.append("call")
            mock_response = mocker.MagicMock(spec=Response)
            mock_response.status_code = 200
            mock_response.is_stream = False
            return mock_response

        mocker.patch.object(AsyncClient, "request", side_effect=mock_httpx_side_effect)

        class AsyncHookedAPI(BaseAPI):
            app_name = api_client_async.app_name

            async def pre_request_hook(self, *args: Any, **kwargs: Any) -> None:
                call_stack.append("pre")

            async def post_request_hook(self, *args: Any, **kwargs: Any) -> None:
                call_stack.append("post")

            @endpoint.get("/v1/something")
            def get_something(self) -> RestResponse: ...

        instance = AsyncHookedAPI(api_client_async)
        await instance.get_something()

        assert call_stack == ["pre", "call", "post"]

    async def test_async_def_pre_hook_exception_skips_call_and_post_hook(
        self, mocker: MockerFixture, api_client_async: APIClient
    ) -> None:
        """Test that an exception raised inside an `async def` pre_request_hook propagates and skips both the
        request and post_request_hook"""
        call_stack: list[str] = []

        def mock_httpx_side_effect(*args: Any, **kwargs: Any) -> Response:
            call_stack.append("call")
            mock_response = mocker.MagicMock(spec=Response)
            mock_response.status_code = 200
            mock_response.is_stream = False
            return mock_response

        mocker.patch.object(AsyncClient, "request", side_effect=mock_httpx_side_effect)

        class AsyncHookedAPI(BaseAPI):
            app_name = api_client_async.app_name

            async def pre_request_hook(self, *args: Any, **kwargs: Any) -> None:
                raise ValueError("pre hook failed")

            async def post_request_hook(self, *args: Any, **kwargs: Any) -> None:
                call_stack.append("post")

            @endpoint.get("/v1/something")
            def get_something(self) -> RestResponse: ...

        instance = AsyncHookedAPI(api_client_async)
        with pytest.raises(ValueError, match="pre hook failed"):
            await instance.get_something()

        assert call_stack == []

    async def test_async_def_post_hook_assertion_error_propagates(
        self, mocker: MockerFixture, api_client_async: APIClient
    ) -> None:
        """Test that an AssertionError raised inside an `async def` post_request_hook propagates"""
        mocker.patch.object(AsyncClient, "request")

        class AsyncHookedAPI(BaseAPI):
            app_name = api_client_async.app_name

            async def post_request_hook(self, *args: Any, **kwargs: Any) -> None:
                raise AssertionError("hook assertion failed")

            @endpoint.get("/v1/something")
            def get_something(self) -> RestResponse: ...

        instance = AsyncHookedAPI(api_client_async)
        with pytest.raises(AssertionError, match="hook assertion failed"):
            await instance.get_something()

    async def test_async_def_post_hook_other_exception_is_logged_not_propagated(
        self, mocker: MockerFixture, api_client_async: APIClient
    ) -> None:
        """Test that a non-AssertionError raised inside an `async def` post_request_hook is logged, not propagated"""
        mocker.patch.object(AsyncClient, "request")
        mock_log = mocker.patch.object(_endpoint_func_module, "logger")

        class AsyncHookedAPI(BaseAPI):
            app_name = api_client_async.app_name

            async def post_request_hook(self, *args: Any, **kwargs: Any) -> None:
                raise ValueError("hook failed")

            @endpoint.get("/v1/something")
            def get_something(self) -> RestResponse: ...

        instance = AsyncHookedAPI(api_client_async)
        r = await instance.get_something()

        assert isinstance(r, RestResponse)
        mock_log.exception.assert_called_once()

    async def test_async_def_pre_and_post_hooks_are_awaited_during_stream(
        self, mocker: MockerFixture, api_client_async: APIClient
    ) -> None:
        """Test that `async def` pre_request_hook and post_request_hook are awaited during an async stream"""
        call_stack: list[str] = []
        mock_resp = _make_stream_response()

        @asynccontextmanager
        async def fake_execute_stream(self_executor: AsyncExecutor, ef: Any, path: str, params: dict[str, Any]) -> Any:
            call_stack.append("call")
            yield mock_resp

        mocker.patch.object(AsyncExecutor, "execute_stream", new=fake_execute_stream)

        class AsyncHookedAPI(BaseAPI):
            app_name = api_client_async.app_name

            async def pre_request_hook(self, *args: Any, **kwargs: Any) -> None:
                call_stack.append("pre")

            async def post_request_hook(self, *args: Any, **kwargs: Any) -> None:
                call_stack.append("post")

            @endpoint.get("/v1/something")
            def get_something(self) -> RestResponse: ...

        instance = AsyncHookedAPI(api_client_async)
        async with instance.get_something.stream():
            pass

        assert call_stack == ["pre", "call", "post"]


class TestAsyncOnlyRejectedOnSyncClient:
    """Tests that `async def` endpoint functions/hooks are rejected on a sync client"""

    def test_async_def_endpoint_on_sync_client_raises(self, mocker: MockerFixture, api_client: APIClient) -> None:
        """Test that calling an `async def` endpoint on a sync client raises RuntimeError"""
        mocker.patch.object(Client, "request")

        class AsyncOnlyAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.get("/v1/something")
            async def get_something(self) -> RestResponse: ...

        instance = AsyncOnlyAPI(api_client)
        with pytest.raises(RuntimeError, match=r"`AsyncOnlyAPI\.get_something` is defined with `async def`"):
            instance.get_something()

    def test_async_def_endpoint_on_sync_client_stream_raises(
        self, mocker: MockerFixture, api_client: APIClient
    ) -> None:
        """Test that streaming an `async def` endpoint on a sync client raises RuntimeError"""
        mocker.patch.object(Client, "request")

        class AsyncOnlyAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.get("/v1/something")
            async def get_something(self) -> RestResponse: ...

        instance = AsyncOnlyAPI(api_client)
        with pytest.raises(RuntimeError, match=r"`AsyncOnlyAPI\.get_something` is defined with `async def`"):
            with instance.get_something.stream():
                pass

    def test_async_def_pre_request_hook_on_sync_client_raises(
        self, mocker: MockerFixture, api_client: APIClient
    ) -> None:
        """Test that an `async def` pre_request_hook on a sync client raises RuntimeError"""
        mocker.patch.object(Client, "request")

        class AsyncHookAPI(BaseAPI):
            app_name = api_client.app_name

            async def pre_request_hook(self, *args: Any, **kwargs: Any) -> None:
                pass

            @endpoint.get("/v1/something")
            def get_something(self) -> RestResponse: ...

        instance = AsyncHookAPI(api_client)
        with pytest.raises(RuntimeError, match=r"`AsyncHookAPI\.pre_request_hook` is defined with `async def`"):
            instance.get_something()

    def test_async_def_pre_request_hook_on_sync_client_raises_even_with_hooks_disabled(
        self, mocker: MockerFixture, api_client: APIClient
    ) -> None:
        """Test that an `async def` pre_request_hook on a sync client raises RuntimeError even when
        with_hooks=False, since the rejection does not depend on hook execution"""
        mocker.patch.object(Client, "request")

        class AsyncHookAPI(BaseAPI):
            app_name = api_client.app_name

            async def pre_request_hook(self, *args: Any, **kwargs: Any) -> None:
                pass

            @endpoint.get("/v1/something")
            def get_something(self) -> RestResponse: ...

        instance = AsyncHookAPI(api_client)
        with pytest.raises(RuntimeError, match=r"`AsyncHookAPI\.pre_request_hook` is defined with `async def`"):
            instance.get_something(with_hooks=False)

    def test_async_def_post_request_hook_on_sync_client_raises(
        self, mocker: MockerFixture, api_client: APIClient
    ) -> None:
        """Test that an `async def` post_request_hook on a sync client raises RuntimeError"""
        mocker.patch.object(Client, "request")

        class AsyncHookAPI(BaseAPI):
            app_name = api_client.app_name

            async def post_request_hook(self, *args: Any, **kwargs: Any) -> None:
                pass

            @endpoint.get("/v1/something")
            def get_something(self) -> RestResponse: ...

        instance = AsyncHookAPI(api_client)
        with pytest.raises(RuntimeError, match=r"`AsyncHookAPI\.post_request_hook` is defined with `async def`"):
            instance.get_something()


class TestSyncEndpointFuncStreamCall:
    """Tests for SyncEndpointFunc.stream()"""

    def test_sync_stream_yields_rest_response(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that stream() context manager yields a RestResponse"""
        mock_resp = _make_stream_response()

        @contextmanager
        def fake_execute_stream(self_executor: SyncExecutor, ef: Any, path: str, params: dict[str, Any]) -> Any:
            yield mock_resp

        mocker.patch.object(SyncExecutor, "execute_stream", new=fake_execute_stream)
        instance = api_class(api_client)
        with instance.get_something.stream() as r:
            assert r is mock_resp

    def test_sync_stream_on_deprecated_endpoint_logs_warning(
        self, mocker: MockerFixture, api_client: APIClient
    ) -> None:
        """Test that streaming a deprecated endpoint logs a DEPRECATED warning"""
        mock_resp = _make_stream_response()

        @contextmanager
        def fake_execute_stream(self_executor: SyncExecutor, ef: Any, path: str, params: dict[str, Any]) -> Any:
            yield mock_resp

        mocker.patch.object(SyncExecutor, "execute_stream", new=fake_execute_stream)
        mock_log = mocker.patch.object(endpoint_call_util, "logger")

        class DeprecatedAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.is_deprecated
            @endpoint.get("/v1/something")
            def get_something(self) -> RestResponse: ...

        instance = DeprecatedAPI(api_client)
        with instance.get_something.stream():
            pass

        warning_messages = [call[0][0] for call in mock_log.warning.call_args_list]
        assert any("DEPRECATED" in msg for msg in warning_messages)

    def test_sync_stream_http_error_propagates(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that an HTTPError raised inside stream() propagates to the caller"""
        mock_request = mocker.MagicMock(spec=Request)
        connect_error = ConnectError("stream connection failed")
        connect_error.request = mock_request

        class _RaisingCM:
            def __enter__(self) -> Any:
                raise connect_error

            def __exit__(self, *args: Any) -> None:
                pass

        mocker.patch.object(SyncExecutor, "execute_stream", return_value=_RaisingCM())
        instance = api_class(api_client)
        with pytest.raises(HTTPError, match="stream connection failed"):
            with instance.get_something.stream():
                pass

    def test_sync_stream_post_hook_called_after_success(self, mocker: MockerFixture, api_client: APIClient) -> None:
        """Test that post_request_hook is called after a successful stream"""
        post_called = False
        mock_resp = _make_stream_response()

        @contextmanager
        def fake_execute_stream(self_executor: SyncExecutor, ef: Any, path: str, params: dict[str, Any]) -> Any:
            yield mock_resp

        mocker.patch.object(SyncExecutor, "execute_stream", new=fake_execute_stream)

        class HookedAPI(BaseAPI):
            app_name = api_client.app_name

            def post_request_hook(self, *args: Any, **kwargs: Any) -> None:
                nonlocal post_called
                post_called = True

            @endpoint.get("/v1/something")
            def get_something(self) -> RestResponse: ...

        instance = HookedAPI(api_client)
        with instance.get_something.stream():
            pass

        assert post_called is True

    def test_sync_stream_post_hook_skipped_on_non_http_exception(
        self, mocker: MockerFixture, api_client: APIClient
    ) -> None:
        """Test that post_request_hook is skipped when a non-HTTPError exception is raised inside stream()"""
        post_called = False
        mock_resp = _make_stream_response()

        @contextmanager
        def fake_execute_stream(self_executor: SyncExecutor, ef: Any, path: str, params: dict[str, Any]) -> Any:
            yield mock_resp

        mocker.patch.object(SyncExecutor, "execute_stream", new=fake_execute_stream)

        class HookedAPI(BaseAPI):
            app_name = api_client.app_name

            def post_request_hook(self, *args: Any, **kwargs: Any) -> None:
                nonlocal post_called
                post_called = True

            @endpoint.get("/v1/something")
            def get_something(self) -> RestResponse: ...

        instance = HookedAPI(api_client)
        caught: Exception | None = None
        try:
            with instance.get_something.stream():
                raise ValueError("deliberate")
        except ValueError as e:
            caught = e

        assert caught is not None and str(caught) == "deliberate"
        assert post_called is False

    def test_sync_stream_post_hook_assertion_error_propagates(
        self, mocker: MockerFixture, api_client: APIClient
    ) -> None:
        """Test that an AssertionError raised inside post_request_hook during a sync stream propagates"""
        mock_resp = _make_stream_response()

        @contextmanager
        def fake_execute_stream(self_executor: SyncExecutor, ef: Any, path: str, params: dict[str, Any]) -> Any:
            yield mock_resp

        mocker.patch.object(SyncExecutor, "execute_stream", new=fake_execute_stream)

        class HookedAPI(BaseAPI):
            app_name = api_client.app_name

            def post_request_hook(self, *args: Any, **kwargs: Any) -> None:
                raise AssertionError("hook assertion failed")

            @endpoint.get("/v1/something")
            def get_something(self) -> RestResponse: ...

        instance = HookedAPI(api_client)
        with pytest.raises(AssertionError, match="hook assertion failed"):
            with instance.get_something.stream():
                pass

    def test_sync_stream_post_hook_other_exception_is_logged_not_propagated(
        self, mocker: MockerFixture, api_client: APIClient
    ) -> None:
        """Test that a non-AssertionError raised inside post_request_hook during a sync stream is logged, not
        propagated"""
        mock_resp = _make_stream_response()
        mock_log = mocker.patch.object(_endpoint_func_module, "logger")

        @contextmanager
        def fake_execute_stream(self_executor: SyncExecutor, ef: Any, path: str, params: dict[str, Any]) -> Any:
            yield mock_resp

        mocker.patch.object(SyncExecutor, "execute_stream", new=fake_execute_stream)

        class HookedAPI(BaseAPI):
            app_name = api_client.app_name

            def post_request_hook(self, *args: Any, **kwargs: Any) -> None:
                raise ValueError("hook failed")

            @endpoint.get("/v1/something")
            def get_something(self) -> RestResponse: ...

        instance = HookedAPI(api_client)
        with instance.get_something.stream() as r:
            assert r is mock_resp

        mock_log.exception.assert_called_once()

    def test_sync_stream_merges_signature_defaults_into_payload(
        self, mocker: MockerFixture, api_client: APIClient
    ) -> None:
        """Test that stream() merges signature defaults into the payload via get_signature_defaults"""
        captured_params: dict[str, Any] = {}

        @contextmanager
        def fake_execute_stream(self_executor: SyncExecutor, ef: Any, path: str, params: dict[str, Any]) -> Any:
            captured_params.update(params)
            yield _make_stream_response()

        mocker.patch.object(SyncExecutor, "execute_stream", new=fake_execute_stream)

        class DefaultsAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.get("/v1/items")
            def list_items(self, *, page: int = 1, per_page: int = Unset) -> RestResponse: ...

        instance = DefaultsAPI(api_client)
        with instance.list_items.stream():
            pass

        # page=1 default should appear. per_page=Unset should be excluded
        assert captured_params.get("json", captured_params.get("params", {})).get("page") == 1 or (
            # generate_rest_func_params puts params inside json/params key. Check the raw call
            True  # verified via spy below
        )
        # Use spy to capture the merged payload sent to generate_rest_func_params
        spy_params: list[Any] = []
        original_generate = endpoint_call_util.generate_rest_func_params

        def capturing_generate(ep: Any, params: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
            spy_params.append(params)
            return original_generate(ep, params, *args, **kwargs)

        mocker.patch.object(
            __import__(endpoint_call_util.__name__, fromlist=["generate_rest_func_params"]),
            "generate_rest_func_params",
            side_effect=capturing_generate,
        )

        with instance.list_items.stream():
            pass

        assert spy_params, "generate_rest_func_params was not called"
        merged = spy_params[0]
        assert merged.get("page") == 1
        assert "per_page" not in merged


class TestAsyncEndpointFuncStreamCall:
    """Tests for AsyncEndpointFunc.stream()"""

    async def test_async_stream_yields_rest_response(
        self, mocker: MockerFixture, api_client_async: APIClient, api_class_async: type[BaseAPI]
    ) -> None:
        """Test that async stream() context manager yields a RestResponse"""
        mock_resp = _make_stream_response()

        @asynccontextmanager
        async def fake_execute_stream(self_executor: AsyncExecutor, ef: Any, path: str, params: dict[str, Any]) -> Any:
            yield mock_resp

        mocker.patch.object(AsyncExecutor, "execute_stream", new=fake_execute_stream)
        instance = api_class_async(api_client_async)
        async with instance.get_something.stream() as r:
            assert r is mock_resp

    async def test_async_stream_http_error_propagates(
        self, mocker: MockerFixture, api_client_async: APIClient, api_class_async: type[BaseAPI]
    ) -> None:
        """Test that an HTTPError raised inside async stream() propagates to the caller"""
        mock_request = mocker.MagicMock(spec=Request)
        connect_error = ConnectError("async stream connection failed")
        connect_error.request = mock_request

        class _AsyncRaisingCM:
            async def __aenter__(self) -> Any:
                raise connect_error

            async def __aexit__(self, *args: Any) -> None:
                pass

        mocker.patch.object(AsyncExecutor, "execute_stream", return_value=_AsyncRaisingCM())
        instance = api_class_async(api_client_async)
        with pytest.raises(HTTPError, match="async stream connection failed"):
            async with instance.get_something.stream():
                pass

    async def test_async_stream_post_hook_called_after_success(
        self, mocker: MockerFixture, api_client_async: APIClient
    ) -> None:
        """Test that post_request_hook is called after a successful async stream"""
        post_called = False
        mock_resp = _make_stream_response()

        @asynccontextmanager
        async def fake_execute_stream(self_executor: AsyncExecutor, ef: Any, path: str, params: dict[str, Any]) -> Any:
            yield mock_resp

        mocker.patch.object(AsyncExecutor, "execute_stream", new=fake_execute_stream)

        class AsyncHookedAPI(BaseAPI):
            app_name = api_client_async.app_name

            def post_request_hook(self, *args: Any, **kwargs: Any) -> None:
                nonlocal post_called
                post_called = True

            @endpoint.get("/v1/something")
            def get_something(self) -> RestResponse: ...

        instance = AsyncHookedAPI(api_client_async)
        async with instance.get_something.stream():
            pass

        assert post_called is True

    async def test_async_stream_post_hook_skipped_on_non_http_exception(
        self, mocker: MockerFixture, api_client_async: APIClient
    ) -> None:
        """Test that post_request_hook is skipped on a non-HTTPError in async stream()"""
        post_called = False
        mock_resp = _make_stream_response()

        @asynccontextmanager
        async def fake_execute_stream(self_executor: AsyncExecutor, ef: Any, path: str, params: dict[str, Any]) -> Any:
            yield mock_resp

        mocker.patch.object(AsyncExecutor, "execute_stream", new=fake_execute_stream)

        class AsyncHookedAPI(BaseAPI):
            app_name = api_client_async.app_name

            def post_request_hook(self, *args: Any, **kwargs: Any) -> None:
                nonlocal post_called
                post_called = True

            @endpoint.get("/v1/something")
            def get_something(self) -> RestResponse: ...

        instance = AsyncHookedAPI(api_client_async)
        caught: Exception | None = None
        try:
            async with instance.get_something.stream():
                raise RuntimeError("deliberate async")
        except RuntimeError as e:
            caught = e

        assert caught is not None and str(caught) == "deliberate async"
        assert post_called is False


def _make_stream_response() -> MagicMock:
    """Return a MagicMock that looks like a streaming RestResponse."""
    r = MagicMock(spec=RestResponse)
    r.is_stream = True
    r.status_code = 200
    return r


def _make_httpx_response(status_code: int, mocker: MockerFixture, headers: dict[str, str] | None = None) -> Response:
    """Build a minimal mock httpx response with the given status code and optional headers."""
    r = mocker.MagicMock(spec=Response)
    r.status_code = status_code
    r.is_success = status_code < 300
    r.headers = headers or {}
    r.content = b""
    r.is_stream = False
    r.elapsed = mocker.MagicMock()
    r.elapsed.total_seconds.return_value = 0.0
    r.json.return_value = {}
    r.text = ""
    if status_code >= 300:
        r.raise_for_status.side_effect = HTTPStatusError(str(status_code), request=mocker.MagicMock(), response=r)
    return r

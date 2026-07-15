"""Unit tests for endpoint_func.py with_xxx() chainable call wrappers"""

import re
from collections.abc import Callable, Generator
from typing import Any, NoReturn
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from common_libs.clients.rest_client import RestResponse
from common_libs.clients.rest_client.retry import retry_on
from common_libs.clients.rest_client.types import Request, Response
from common_libs.clients.rest_client.utils import set_request_to_exception
from common_libs.lock import AsyncLock, Lock
from filelock import Timeout as FileLockTimeout
from httpx import AsyncClient, Client, HTTPStatusError
from pytest_mock import MockerFixture

import api_client_core.endpoints.endpoint_func.call_wrappers as _call_wrappers_module
from api_client_core.base import APIClient, BaseAPI
from api_client_core.endpoints import AsyncEndpointFunc, Stats, SyncEndpointFunc, endpoint
from api_client_core.endpoints.stats import StatsCollector
from api_client_core.types import Unset


class TestEndpointFuncCallWithLock:
    """Tests for EndpointFunc.with_lock()"""

    def test_auto_lock_name_uses_app_class_and_func_name(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_lock auto-generates a lock name as '{app_name}-{APIClass}.{func_name}'"""
        mocker.patch.object(Client, "request")
        mock_lock = mocker.patch(f"{_call_wrappers_module.__name__}.{Lock.__name__}")
        mock_lock.return_value.__enter__ = mocker.MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = mocker.MagicMock(return_value=False)

        instance = api_class(api_client)
        instance.get_something.with_lock()()

        expected_lock_name = f"{api_client.app_name}-{api_class.__name__}.get_something"
        mock_lock.assert_called_once_with(expected_lock_name)

    def test_explicit_lock_name_overrides_auto_name(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that explicitly providing lock_name overrides the auto-generated name"""
        mocker.patch.object(Client, "request")
        mock_lock = mocker.patch(f"{_call_wrappers_module.__name__}.{Lock.__name__}")
        mock_lock.return_value.__enter__ = mocker.MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = mocker.MagicMock(return_value=False)

        instance = api_class(api_client)
        instance.get_something.with_lock(lock_name="my-custom-lock")()

        mock_lock.assert_called_once_with("my-custom-lock")

    def test_lock_is_entered_and_exited_around_request(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that the Lock context manager is entered before and exited after the request"""
        call_order: list[str] = []

        mock_request = mocker.patch.object(Client, "request")

        def _request_side_effect(*a: Any, **kw: Any) -> MagicMock:
            call_order.append("request")
            return mocker.MagicMock(
                status_code=200,
                headers={},
                content=b"",
                is_stream=False,
                elapsed=mocker.MagicMock(total_seconds=lambda: 0.0),
            )

        mock_request.side_effect = _request_side_effect

        mock_lock_cls = mocker.patch(f"{_call_wrappers_module.__name__}.{Lock.__name__}")
        mock_lock_instance = mocker.MagicMock()
        mock_lock_cls.return_value = mock_lock_instance
        mock_lock_instance.__enter__ = mocker.MagicMock(side_effect=lambda: call_order.append("lock_enter"))
        mock_lock_instance.__exit__ = mocker.MagicMock(side_effect=lambda *a: call_order.append("lock_exit"))

        instance = api_class(api_client)
        instance.get_something.with_lock()()

        assert call_order == ["lock_enter", "request", "lock_exit"]

    async def test_lock_is_held_across_awaited_request_async(
        self, mocker: MockerFixture, api_client_async: APIClient, api_class_async: type[BaseAPI]
    ) -> None:
        """Test that in async mode the Lock context manager is entered before and exited after the awaited request"""
        call_order: list[str] = []

        mock_request = mocker.patch.object(AsyncClient, "request")

        def _request_side_effect(*a: Any, **kw: Any) -> MagicMock:
            call_order.append("request")
            return mocker.MagicMock(
                status_code=200,
                headers={},
                content=b"",
                is_stream=False,
                elapsed=mocker.MagicMock(total_seconds=lambda: 0.0),
            )

        mock_request.side_effect = _request_side_effect

        mock_lock_cls = mocker.patch(f"{_call_wrappers_module.__name__}.{AsyncLock.__name__}")
        mock_lock_instance = mocker.MagicMock()
        mock_lock_cls.return_value = mock_lock_instance
        mock_lock_instance.__aenter__.side_effect = lambda: call_order.append("lock_enter")
        mock_lock_instance.__aexit__.side_effect = lambda *a: call_order.append("lock_exit")

        instance = api_class_async(api_client_async)
        await instance.get_something.with_lock()()

        assert call_order == ["lock_enter", "request", "lock_exit"]

    def test_with_lock_returns_rest_response(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_lock returns a RestResponse"""
        mocker.patch.object(Client, "request")
        mock_lock = mocker.patch(f"{_call_wrappers_module.__name__}.{Lock.__name__}")
        mock_lock.return_value.__enter__ = mocker.MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = mocker.MagicMock(return_value=False)

        instance = api_class(api_client)
        r = instance.get_something.with_lock()()
        assert isinstance(r, RestResponse)

    async def test_lock_is_released_after_async_call(
        self, mocker: MockerFixture, api_client_async: APIClient, api_class_async: type[BaseAPI]
    ) -> None:
        """Test that the distributed lock is fully released after an awaited with_lock() call.

        Uses a real (un-mocked) Lock to catch thread-affinity bugs where acquire and release run
        on different threads, causing the OS-level file lock to silently leak.
        """
        mock_request = mocker.patch.object(AsyncClient, "request")
        mock_request.return_value = mocker.MagicMock(
            status_code=200,
            headers={},
            content=b"",
            is_stream=False,
            elapsed=mocker.MagicMock(total_seconds=lambda: 0.0),
        )
        lock_name = f"test-with-lock-{uuid4()}"

        instance = api_class_async(api_client_async)
        await instance.get_something.with_lock(lock_name=lock_name)()

        # An independent acquire of the same lock must succeed immediately after the call.
        # If the lock leaked (e.g. released on a different thread than it was acquired on),
        # this will block until the timeout and raise FileLockTimeout.
        # is_singleton=False ensures this is an independent FileLock instance that contends
        # on the OS-level flock rather than sharing the wrapper's singleton lock counter.
        try:
            with Lock(lock_name, is_singleton=False, timeout=2):
                pass
        except FileLockTimeout:
            pytest.fail("Lock was not released after the awaited with_lock() call completed")


class TestEndpointFuncCallWithRetrySync:
    """Tests for SyncEndpointFunc.with_retry()"""

    def test_no_retry_when_condition_not_met(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_retry does not retry when the condition is not satisfied"""
        mock_request = mocker.patch.object(Client, "request", return_value=_make_httpx_response(200, mocker))
        instance = api_class(api_client)
        r = instance.get_something.with_retry(condition=503, num_retries=3, retry_after=0)()
        assert isinstance(r, RestResponse)
        assert mock_request.call_count == 1

    def test_retry_on_matching_status_code(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_retry retries when the response matches the given status code"""
        mocker.patch.object(
            Client,
            "request",
            side_effect=[_make_httpx_response(503, mocker), _make_httpx_response(200, mocker)],
        )
        instance = api_class(api_client)
        r = instance.get_something.with_retry(condition=503, num_retries=1, retry_after=0)()
        assert isinstance(r, RestResponse)
        assert r.status_code == 200

    def test_retry_on_callable_condition(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_retry accepts a callable condition and retries when it returns True"""
        mocker.patch.object(
            Client,
            "request",
            side_effect=[_make_httpx_response(429, mocker), _make_httpx_response(200, mocker)],
        )
        instance = api_class(api_client)
        r = instance.get_something.with_retry(
            condition=lambda resp: resp.status_code == 429, num_retries=1, retry_after=0
        )()
        assert isinstance(r, RestResponse)
        assert r.status_code == 200

    def test_retry_exhausts_up_to_num_retries(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_retry stops after num_retries retries even if condition keeps matching"""
        # 1 initial call + 2 retries = 3 total calls
        mocker.patch.object(
            Client,
            "request",
            side_effect=[
                _make_httpx_response(503, mocker),
                _make_httpx_response(503, mocker),
                _make_httpx_response(503, mocker),
            ],
        )
        instance = api_class(api_client)
        mock_request = Client.request
        r = instance.get_something.with_retry(condition=503, num_retries=2, retry_after=0)()
        assert isinstance(r, RestResponse)
        assert r.status_code == 503
        assert mock_request.call_count == 3

    def test_retry_passes_original_args_and_kwargs(self, mocker: MockerFixture, api_client: APIClient) -> None:
        """Test that with_retry forwards the original call args/kwargs to each retry"""

        class PathAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.get("/v1/items/{item_id}")
            def get_item(self, item_id: int) -> RestResponse: ...

        mocker.patch.object(
            Client,
            "request",
            side_effect=[_make_httpx_response(503, mocker), _make_httpx_response(200, mocker)],
        )
        instance = PathAPI(api_client)
        r = instance.get_item.with_retry(condition=503, num_retries=1, retry_after=0)(item_id=42)
        assert isinstance(r, RestResponse)
        assert r.status_code == 200
        # Both calls should have used /v1/items/42
        for call in Client.request.call_args_list:
            assert "42" in str(call)

    def test_retry_passes_correct_kwargs_to_retry_on(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_retry passes the correct keyword arguments to retry_on"""
        mock_retry_on = mocker.patch(f"{_call_wrappers_module.__name__}.{retry_on.__name__}")
        identity: Callable[..., Any] = lambda f: f
        mock_retry_on.return_value = identity

        mocker.patch.object(Client, "request", return_value=_make_httpx_response(200, mocker))
        instance = api_class(api_client)
        my_condition = lambda r: r.status_code == 503
        instance.get_something.with_retry(condition=my_condition, num_retries=5, retry_after=2)()

        mock_retry_on.assert_called_once_with(
            my_condition,
            num_retries=5,
            retry_after=2,
            safe_methods_only=False,
            _async_mode=False,
        )

    def test_safe_methods_only_is_forwarded_to_retry_on(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that safe_methods_only=True is forwarded to retry_on"""
        mock_retry_on = mocker.patch(f"{_call_wrappers_module.__name__}.{retry_on.__name__}")
        identity: Callable[..., Any] = lambda f: f
        mock_retry_on.return_value = identity

        mocker.patch.object(Client, "request", return_value=_make_httpx_response(200, mocker))
        instance = api_class(api_client)
        instance.get_something.with_retry(condition=503, safe_methods_only=True)()

        mock_retry_on.assert_called_once_with(
            503,
            num_retries=1,
            retry_after=5,
            safe_methods_only=True,
            _async_mode=False,
        )


class TestEndpointFuncCallWithRetryAsync:
    """Tests for AsyncEndpointFunc.with_retry()"""

    async def test_no_retry_when_condition_not_met(
        self, mocker: MockerFixture, api_client_async: APIClient, api_class_async: type[BaseAPI]
    ) -> None:
        """Test that async with_retry does not retry when the condition is not satisfied"""
        mock_request = mocker.patch.object(AsyncClient, "request", return_value=_make_httpx_response(200, mocker))
        instance = api_class_async(api_client_async)
        r = await instance.get_something.with_retry(condition=503, num_retries=3, retry_after=0)()
        assert isinstance(r, RestResponse)
        assert mock_request.call_count == 1

    async def test_retry_on_matching_status_code(
        self, mocker: MockerFixture, api_client_async: APIClient, api_class_async: type[BaseAPI]
    ) -> None:
        """Test that async with_retry retries when the response matches the given status code"""
        mocker.patch.object(
            AsyncClient,
            "request",
            side_effect=[_make_httpx_response(503, mocker), _make_httpx_response(200, mocker)],
        )
        instance = api_class_async(api_client_async)
        r = await instance.get_something.with_retry(condition=503, num_retries=1, retry_after=0)()
        assert isinstance(r, RestResponse)
        assert r.status_code == 200

    async def test_retry_passes_correct_kwargs_to_retry_on(
        self, mocker: MockerFixture, api_client_async: APIClient, api_class_async: type[BaseAPI]
    ) -> None:
        """Test that async with_retry passes async_mode=True to retry_on"""
        mock_retry_on = mocker.patch(f"{_call_wrappers_module.__name__}.{retry_on.__name__}")
        identity: Callable[..., Any] = lambda f: f
        mock_retry_on.return_value = identity

        mocker.patch.object(AsyncClient, "request", return_value=_make_httpx_response(200, mocker))
        instance = api_class_async(api_client_async)
        await instance.get_something.with_retry(condition=503, num_retries=1, retry_after=0)()

        mock_retry_on.assert_called_once_with(
            503,
            num_retries=1,
            retry_after=0,
            safe_methods_only=False,
            _async_mode=True,
        )

    async def test_safe_methods_only_is_forwarded_to_retry_on(
        self, mocker: MockerFixture, api_client_async: APIClient, api_class_async: type[BaseAPI]
    ) -> None:
        """Test that safe_methods_only=True is forwarded to retry_on"""
        mock_retry_on = mocker.patch(f"{_call_wrappers_module.__name__}.{retry_on.__name__}")
        identity: Callable[..., Any] = lambda f: f
        mock_retry_on.return_value = identity

        mocker.patch.object(AsyncClient, "request", return_value=_make_httpx_response(200, mocker))
        instance = api_class_async(api_client_async)
        await instance.get_something.with_retry(condition=503, safe_methods_only=True)()

        mock_retry_on.assert_called_once_with(
            503,
            num_retries=1,
            retry_after=5,
            safe_methods_only=True,
            _async_mode=True,
        )


class TestEndpointFuncCallWithRetryOnException:
    """Tests for with_retry() using exception classes as the condition"""

    def test_sync_retry_on_matching_exception_class(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_retry retries when the raised exception matches the condition class"""
        call_count = 0

        def request_side_effect(*a: Any, **kw: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("transient error")
            return _make_httpx_response(200, mocker)

        mocker.patch.object(Client, "request", side_effect=request_side_effect)
        instance = api_class(api_client)
        r = instance.get_something.with_retry(condition=ValueError, num_retries=1, retry_after=0)()
        assert isinstance(r, RestResponse)
        assert call_count == 2

    def test_sync_no_retry_on_non_matching_exception_class(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_retry does not retry when the raised exception does not match the condition class"""
        mocker.patch.object(Client, "request", side_effect=TypeError("unexpected type"))
        instance = api_class(api_client)
        with pytest.raises(TypeError):
            instance.get_something.with_retry(condition=ValueError, num_retries=1, retry_after=0)()

    def test_sync_retry_on_tuple_of_exception_classes(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_retry retries when the raised exception matches any class in a tuple condition"""
        call_count = 0

        def request_side_effect(*a: Any, **kw: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise TypeError("type error")
            return _make_httpx_response(200, mocker)

        mocker.patch.object(Client, "request", side_effect=request_side_effect)
        instance = api_class(api_client)
        r = instance.get_something.with_retry(condition=(ValueError, TypeError), num_retries=1, retry_after=0)()
        assert isinstance(r, RestResponse)
        assert call_count == 2

    def test_sync_retry_exhausts_up_to_num_retries_on_exception(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_retry stops after num_retries retries even if the exception keeps being raised"""
        mocker.patch.object(Client, "request", side_effect=ValueError("always fails"))
        instance = api_class(api_client)
        with pytest.raises(ValueError):
            instance.get_something.with_retry(condition=ValueError, num_retries=2, retry_after=0)()
        # 1 initial call + 2 retries = 3 total
        assert Client.request.call_count == 3

    def test_sync_safe_methods_only_skips_retry_for_unsafe_method(
        self, mocker: MockerFixture, api_client: APIClient
    ) -> None:
        """Test that safe_methods_only=True skips retry when the endpoint uses a non-safe HTTP method"""

        class PostAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.post("/v1/something")
            def post_something(self) -> RestResponse: ...

        mocker.patch.object(
            Client, "request", side_effect=lambda *a, **kw: _raise_with_request(ValueError("transient error"), "POST")
        )
        instance = PostAPI(api_client)
        with pytest.raises(ValueError):
            instance.post_something.with_retry(
                condition=ValueError, num_retries=2, retry_after=0, safe_methods_only=True
            )()
        assert Client.request.call_count == 1

    def test_sync_safe_methods_only_retries_for_safe_method(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that safe_methods_only=True still retries when the endpoint uses a safe HTTP method (GET)"""
        call_count = 0

        def request_side_effect(*a: Any, **kw: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                _raise_with_request(ValueError("transient error"), "GET")
            return _make_httpx_response(200, mocker)

        mocker.patch.object(Client, "request", side_effect=request_side_effect)
        instance = api_class(api_client)
        r = instance.get_something.with_retry(
            condition=ValueError, num_retries=1, retry_after=0, safe_methods_only=True
        )()
        assert isinstance(r, RestResponse)
        assert call_count == 2

    async def test_async_retry_on_matching_exception_class(
        self, mocker: MockerFixture, api_client_async: APIClient, api_class_async: type[BaseAPI]
    ) -> None:
        """Test that async with_retry retries when the raised exception matches the condition class"""
        call_count = 0

        def request_side_effect(*a: Any, **kw: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("transient error")
            return _make_httpx_response(200, mocker)

        mocker.patch.object(AsyncClient, "request", side_effect=request_side_effect)
        instance = api_class_async(api_client_async)
        r = await instance.get_something.with_retry(condition=ValueError, num_retries=1, retry_after=0)()
        assert isinstance(r, RestResponse)
        assert call_count == 2

    async def test_async_safe_methods_only_skips_retry_for_unsafe_method(
        self, mocker: MockerFixture, api_client_async: APIClient
    ) -> None:
        """Test that in async mode safe_methods_only=True skips retry for non-safe HTTP methods"""

        class PostAPI(BaseAPI):
            app_name = api_client_async.app_name

            @endpoint.post("/v1/something")
            def post_something(self) -> RestResponse: ...

        mocker.patch.object(
            AsyncClient,
            "request",
            side_effect=lambda *a, **kw: _raise_with_request(ValueError("transient error"), "POST"),
        )
        instance = PostAPI(api_client_async)
        with pytest.raises(ValueError):
            await instance.post_something.with_retry(
                condition=ValueError, num_retries=2, retry_after=0, safe_methods_only=True
            )()
        assert AsyncClient.request.call_count == 1

    async def test_async_safe_methods_only_retries_for_safe_method(
        self, mocker: MockerFixture, api_client_async: APIClient, api_class_async: type[BaseAPI]
    ) -> None:
        """Test that in async mode safe_methods_only=True still retries for safe HTTP methods (GET)"""
        call_count = 0

        def request_side_effect(*a: Any, **kw: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                _raise_with_request(ValueError("transient error"), "GET")
            return _make_httpx_response(200, mocker)

        mocker.patch.object(AsyncClient, "request", side_effect=request_side_effect)
        instance = api_class_async(api_client_async)
        r = await instance.get_something.with_retry(
            condition=ValueError, num_retries=1, retry_after=0, safe_methods_only=True
        )()
        assert isinstance(r, RestResponse)
        assert call_count == 2


class TestEndpointFuncCallWithExpectedStatus:
    """Tests for EndpointFunc.with_expected_status()"""

    def test_sync_passes_through_when_status_matches(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_expected_status returns the RestResponse when the status code matches"""
        mocker.patch.object(Client, "request", return_value=_make_httpx_response(200, mocker))
        instance = api_class(api_client)
        r = instance.get_something.with_expected_status(200)()
        assert isinstance(r, RestResponse)
        assert r.status_code == 200

    def test_sync_raises_assertion_when_status_does_not_match(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_expected_status raises AssertionError when the status code does not match"""
        mocker.patch.object(Client, "request", return_value=_make_httpx_response(404, mocker))
        instance = api_class(api_client)
        with pytest.raises(AssertionError, match=r"Expected status code 200, but got 404"):
            instance.get_something.with_expected_status(200)()

    def test_sync_accepts_multiple_expected_statuses(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_expected_status accepts multiple codes and passes when any one matches"""
        mocker.patch.object(Client, "request", return_value=_make_httpx_response(201, mocker))
        instance = api_class(api_client)
        r = instance.get_something.with_expected_status(200, 201)()
        assert isinstance(r, RestResponse)
        assert r.status_code == 201

    def test_sync_raises_when_none_of_multiple_statuses_match(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_expected_status raises AssertionError when none of the expected codes match"""
        mocker.patch.object(Client, "request", return_value=_make_httpx_response(400, mocker))
        instance = api_class(api_client)
        with pytest.raises(AssertionError, match=r"Expected status code 200/201, but got 400"):
            instance.get_something.with_expected_status(200, 201)()

    def test_sync_useful_for_negative_test_scenarios(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_expected_status can assert on error status codes for negative test scenarios"""
        mocker.patch.object(Client, "request", return_value=_make_httpx_response(400, mocker))
        instance = api_class(api_client)
        r = instance.get_something.with_expected_status(400, 422)()
        assert isinstance(r, RestResponse)
        assert r.status_code == 400

    def test_raises_value_error_when_called_with_no_statuses(
        self, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_expected_status raises ValueError immediately when given no status codes"""
        instance = api_class(api_client)
        with pytest.raises(ValueError, match="At least one expected status code must be given"):
            instance.get_something.with_expected_status()

    async def test_async_passes_through_when_status_matches(
        self, mocker: MockerFixture, api_client_async: APIClient, api_class_async: type[BaseAPI]
    ) -> None:
        """Test that async with_expected_status returns the RestResponse when the status code matches"""
        mocker.patch.object(AsyncClient, "request", return_value=_make_httpx_response(200, mocker))
        instance = api_class_async(api_client_async)
        r = await instance.get_something.with_expected_status(200)()
        assert isinstance(r, RestResponse)
        assert r.status_code == 200

    async def test_async_raises_assertion_when_status_does_not_match(
        self, mocker: MockerFixture, api_client_async: APIClient, api_class_async: type[BaseAPI]
    ) -> None:
        """Test that async with_expected_status raises AssertionError when the status code does not match"""
        mocker.patch.object(AsyncClient, "request", return_value=_make_httpx_response(404, mocker))
        instance = api_class_async(api_client_async)
        with pytest.raises(AssertionError, match=r"Expected status code 200, but got 404"):
            await instance.get_something.with_expected_status(200)()


class TestEndpointFuncCallWithMaxResponseTime:
    """Tests for EndpointFunc.with_max_response_time()"""

    def test_sync_passes_when_response_time_is_within_limit(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_max_response_time returns the response when the response time is within the limit"""
        resp = _make_httpx_response(200, mocker)
        resp.elapsed.total_seconds.return_value = 100 / 1000
        mocker.patch.object(Client, "request", return_value=resp)
        instance = api_class(api_client)
        r = instance.get_something.with_max_response_time(1000)()
        assert isinstance(r, RestResponse)

    def test_sync_raises_assertion_when_response_time_exceeds_limit(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_max_response_time raises AssertionError when the response time exceeds the limit"""
        resp = _make_httpx_response(200, mocker)
        resp.elapsed.total_seconds.return_value = 200 / 1000
        mocker.patch.object(Client, "request", return_value=resp)
        instance = api_class(api_client)
        with pytest.raises(AssertionError, match=r"Response time 200 msecs exceeded the threshold of 100 msecs"):
            instance.get_something.with_max_response_time(100)()

    def test_sync_passes_when_response_time_equals_limit(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_max_response_time passes when response time exactly equals the limit (boundary: > not >=)"""
        resp = _make_httpx_response(200, mocker)
        resp.elapsed.total_seconds.return_value = 100 / 1000
        mocker.patch.object(Client, "request", return_value=resp)
        instance = api_class(api_client)
        r = instance.get_something.with_max_response_time(100)()
        assert isinstance(r, RestResponse)

    async def test_async_passes_when_response_time_is_within_limit(
        self, mocker: MockerFixture, api_client_async: APIClient, api_class_async: type[BaseAPI]
    ) -> None:
        """Test that async with_max_response_time returns the response when the response time is within the limit"""
        resp = _make_httpx_response(200, mocker)
        resp.elapsed.total_seconds.return_value = 100 / 1000
        mocker.patch.object(AsyncClient, "request", return_value=resp)
        instance = api_class_async(api_client_async)
        r = await instance.get_something.with_max_response_time(1000)()
        assert isinstance(r, RestResponse)

    async def test_async_raises_assertion_when_response_time_exceeds_limit(
        self, mocker: MockerFixture, api_client_async: APIClient, api_class_async: type[BaseAPI]
    ) -> None:
        """Test that async with_max_response_time raises AssertionError when the response time exceeds the limit"""
        resp = _make_httpx_response(200, mocker)
        resp.elapsed.total_seconds.return_value = 200 / 1000
        mocker.patch.object(AsyncClient, "request", return_value=resp)
        instance = api_class_async(api_client_async)
        with pytest.raises(AssertionError, match=r"Response time 200 msecs exceeded the threshold of 100 msecs"):
            await instance.get_something.with_max_response_time(100)()


class TestEndpointFuncCallWithPolling:
    """Tests for EndpointFunc.with_polling()"""

    def test_sync_returns_immediately_when_condition_met_on_first_call(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_polling returns on the first call when until() is immediately True"""
        mock_request = mocker.patch.object(Client, "request", return_value=_make_httpx_response(200, mocker))
        mocker.patch("time.sleep")
        instance = api_class(api_client)
        r = instance.get_something.with_polling(until=lambda resp: resp.ok, interval=0.1, timeout=60)()
        assert isinstance(r, RestResponse)
        assert mock_request.call_count == 1

    def test_sync_polls_until_condition_is_met(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_polling keeps calling the endpoint until until() returns True"""
        # First two calls return 202 (condition False). Third returns 200 (condition True)
        mocker.patch.object(
            Client,
            "request",
            side_effect=[
                _make_httpx_response(202, mocker),
                _make_httpx_response(202, mocker),
                _make_httpx_response(200, mocker),
            ],
        )
        # Patch time in call_wrappers's namespace only so asyncio's internal time.monotonic() is unaffected.
        # Return values: deadline_call=0.0, check_after_1st_poll=1.0, check_after_2nd_poll=2.0
        mock_time = mocker.MagicMock()
        mock_time.monotonic.side_effect = [0.0, 1.0, 2.0]
        mocker.patch.object(_call_wrappers_module, "time", mock_time)
        instance = api_class(api_client)
        r = instance.get_something.with_polling(until=lambda resp: resp.status_code == 200, interval=0.5, timeout=60)()
        assert isinstance(r, RestResponse)
        assert r.status_code == 200
        assert Client.request.call_count == 3
        assert mock_time.sleep.call_count == 2
        mock_time.sleep.assert_called_with(0.5)

    def test_sync_raises_timeout_when_condition_never_met(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_polling raises TimeoutError when the condition is never satisfied within timeout"""
        mocker.patch.object(Client, "request", return_value=_make_httpx_response(202, mocker))
        # deadline = 0.0 + 5 = 5.0. After one failed poll, monotonic returns 10.0 → 10.0 + 0.1 >= 5.0
        mock_time = mocker.MagicMock()
        mock_time.monotonic.side_effect = [0.0, 10.0]
        mocker.patch.object(_call_wrappers_module, "time", mock_time)
        instance = api_class(api_client)
        with pytest.raises(TimeoutError, match="Polling condition was not met within 5 seconds"):
            instance.get_something.with_polling(until=lambda resp: resp.status_code == 200, interval=0.1, timeout=5)()

    def test_sync_endpoint_is_always_called_at_least_once(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_polling always makes at least one request even with a very short timeout"""
        mock_request = mocker.patch.object(Client, "request", return_value=_make_httpx_response(200, mocker))
        # condition immediately True → returns after first call without even checking the deadline
        instance = api_class(api_client)
        instance.get_something.with_polling(until=lambda resp: resp.ok, timeout=0)()
        assert mock_request.call_count == 1

    def test_sync_polling_does_not_timeout_early(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_polling() does not raise TimeoutError before the deadline when interval > timeout"""
        mocker.patch.object(Client, "request")

        # Simulate: t=0 (start), t=0.1 (after first call), t=0.9 (after sleep), t=1.1 (after second call → expired)
        mock_time = mocker.MagicMock()
        mock_time.monotonic.side_effect = [0.0, 0.1, 0.9, 1.1]
        mocker.patch.object(_call_wrappers_module, "time", mock_time)
        sleep_mock = mock_time.sleep

        call_count = 0

        def until(r: RestResponse) -> bool:
            nonlocal call_count
            call_count += 1
            return call_count >= 2

        instance = api_class(api_client)
        result = instance.get_something.with_polling(until=until, interval=10, timeout=1.0)()

        assert isinstance(result, RestResponse)
        assert call_count == 2
        # sleep was called with min(10, ~0.9) ≈ 0.9, not the full interval of 10
        sleep_mock.assert_called_once()
        slept = sleep_mock.call_args[0][0]
        assert slept < 10

    async def test_async_returns_immediately_when_condition_met_on_first_call(
        self, mocker: MockerFixture, api_client_async: APIClient, api_class_async: type[BaseAPI]
    ) -> None:
        """Test that async with_polling returns on the first call when until() is immediately True"""
        mock_request = mocker.patch.object(AsyncClient, "request", return_value=_make_httpx_response(200, mocker))
        instance = api_class_async(api_client_async)
        r = await instance.get_something.with_polling(until=lambda resp: resp.ok, interval=0, timeout=60)()
        assert isinstance(r, RestResponse)
        assert mock_request.call_count == 1

    async def test_async_polls_until_condition_is_met(
        self, mocker: MockerFixture, api_client_async: APIClient, api_class_async: type[BaseAPI]
    ) -> None:
        """Test that async with_polling keeps calling the endpoint until until() returns True"""
        mocker.patch.object(
            AsyncClient,
            "request",
            side_effect=[
                _make_httpx_response(202, mocker),
                _make_httpx_response(202, mocker),
                _make_httpx_response(200, mocker),
            ],
        )
        mock_sleep = mocker.patch("asyncio.sleep")
        # Patch time in call_wrappers's namespace only so asyncio's internal time.monotonic() is unaffected.
        # Return values: deadline_call=0.0, check_after_1st_poll=1.0, check_after_2nd_poll=2.0
        mock_time = mocker.MagicMock()
        mock_time.monotonic.side_effect = [0.0, 1.0, 2.0]
        mocker.patch.object(_call_wrappers_module, "time", mock_time)
        instance = api_class_async(api_client_async)
        r = await instance.get_something.with_polling(
            until=lambda resp: resp.status_code == 200, interval=0.5, timeout=60
        )()
        assert isinstance(r, RestResponse)
        assert r.status_code == 200
        assert AsyncClient.request.call_count == 3
        assert mock_sleep.call_count == 2
        mock_sleep.assert_called_with(0.5)

    async def test_async_raises_timeout_when_condition_never_met(
        self, mocker: MockerFixture, api_client_async: APIClient, api_class_async: type[BaseAPI]
    ) -> None:
        """Test that async with_polling raises TimeoutError when the condition is never satisfied within timeout"""
        mocker.patch.object(AsyncClient, "request", return_value=_make_httpx_response(202, mocker))
        mocker.patch("asyncio.sleep")
        # deadline = 0.0 + 5 = 5.0. After one failed poll, monotonic returns 10.0 → 10.0 + 0.1 >= 5.0
        mock_time = mocker.MagicMock()
        mock_time.monotonic.side_effect = [0.0, 10.0]
        mocker.patch.object(_call_wrappers_module, "time", mock_time)
        instance = api_class_async(api_client_async)
        with pytest.raises(TimeoutError, match="Polling condition was not met within 5 seconds"):
            await instance.get_something.with_polling(
                until=lambda resp: resp.status_code == 200, interval=0.1, timeout=5
            )()


class TestEndpointFuncCallWithChaining:
    """Tests for chaining multiple with_xxx() wrappers"""

    def test_sync_with_lock_then_with_retry(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_lock().with_retry() acquires the lock AND retries on the given condition.

        Because with_lock() is the outer wrapper (first in chain), it wraps the entire retry sequence:
        the lock is acquired once and held for all retry attempts.
        """
        mocker.patch.object(
            Client,
            "request",
            side_effect=[_make_httpx_response(503, mocker), _make_httpx_response(200, mocker)],
        )
        mock_lock = mocker.patch(f"{_call_wrappers_module.__name__}.{Lock.__name__}")
        mock_lock.return_value.__enter__ = mocker.MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = mocker.MagicMock(return_value=False)

        instance = api_class(api_client)
        r = instance.get_something.with_lock().with_retry(condition=503, num_retries=1, retry_after=0)()

        assert isinstance(r, RestResponse)
        assert r.status_code == 200
        # Lock wraps the whole retry sequence: acquired once, held for all attempts
        assert mock_lock.call_count == 1

    def test_sync_with_retry_then_with_lock(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_retry().with_lock() acquires the lock AND retries on the given condition.

        Because with_retry() is the outer wrapper (first in chain), it wraps the lock:
        the lock is acquired on each individual attempt (= num_retries + 1 total).
        """
        mocker.patch.object(
            Client,
            "request",
            side_effect=[_make_httpx_response(503, mocker), _make_httpx_response(200, mocker)],
        )
        mock_lock = mocker.patch(f"{_call_wrappers_module.__name__}.{Lock.__name__}")
        mock_lock.return_value.__enter__ = mocker.MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = mocker.MagicMock(return_value=False)

        instance = api_class(api_client)
        r = instance.get_something.with_retry(condition=503, num_retries=1, retry_after=0).with_lock()()

        assert isinstance(r, RestResponse)
        assert r.status_code == 200
        # Lock is acquired once per attempt: initial try + 1 retry = 2 total
        assert mock_lock.call_count == 2

    async def test_async_with_lock_then_with_retry(
        self, mocker: MockerFixture, api_client_async: APIClient, api_class_async: type[BaseAPI]
    ) -> None:
        """Test that async with_lock().with_retry() acquires the lock AND retries on the given condition.

        Because with_lock() is the outer wrapper (first in chain), it wraps the entire retry sequence:
        the lock is acquired once and held for all retry attempts.
        """
        mocker.patch.object(
            AsyncClient,
            "request",
            side_effect=[_make_httpx_response(503, mocker), _make_httpx_response(200, mocker)],
        )
        mock_lock = mocker.patch(f"{_call_wrappers_module.__name__}.{AsyncLock.__name__}")

        instance = api_class_async(api_client_async)
        r = await instance.get_something.with_lock().with_retry(condition=503, num_retries=1, retry_after=0)()

        assert isinstance(r, RestResponse)
        assert r.status_code == 200
        # Lock wraps the whole retry sequence: acquired once, held for all attempts
        assert mock_lock.call_count == 1

    def test_sync_with_expected_status_then_with_retry(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_expected_status().with_retry() asserts on the final response after retries.

        Because with_expected_status() is the outer wrapper (first in chain), it asserts after
        the entire retry sequence has finished — not on each individual attempt.
        """
        # First call returns 503 (retry condition). Second returns 200 (retry ends, assertion passes)
        mocker.patch.object(
            Client,
            "request",
            side_effect=[_make_httpx_response(503, mocker), _make_httpx_response(200, mocker)],
        )
        instance = api_class(api_client)
        r = instance.get_something.with_expected_status(200).with_retry(condition=503, num_retries=1, retry_after=0)()
        assert isinstance(r, RestResponse)
        assert r.status_code == 200

    def test_sync_with_polling_then_with_expected_status(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_polling().with_expected_status() applies status assertion on each polled response.

        Because with_polling() is the outer wrapper (first in chain), it re-invokes with_expected_status()
        on every poll — meaning an unexpected status on an intermediate poll raises immediately.
        """
        # All responses are 200 (both assertion and polling condition pass on first call)
        mocker.patch.object(Client, "request", return_value=_make_httpx_response(200, mocker))
        mocker.patch("time.sleep")
        instance = api_class(api_client)
        r = instance.get_something.with_polling(until=lambda resp: resp.ok, timeout=60).with_expected_status(200)()
        assert isinstance(r, RestResponse)
        assert r.status_code == 200

    def test_sync_with_expected_status_then_with_repeat_asserts_each_call(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_expected_status().with_repeat() asserts the status of each individual call in the group"""
        mocker.patch.object(Client, "request", return_value=_make_httpx_response(404, mocker))
        instance = api_class(api_client)

        results = instance.get_something.with_expected_status(404).with_repeat(num=2)()

        assert len(results) == 2
        assert all(isinstance(r, RestResponse) and r.status_code == 404 for r in results)

    def test_sync_with_expected_status_then_with_repeat_raises_on_unexpected_status(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_expected_status().with_repeat() raises AssertionError when a call in the group returns
        an unexpected status"""
        mocker.patch.object(Client, "request", return_value=_make_httpx_response(500, mocker))
        instance = api_class(api_client)

        with pytest.raises(AssertionError, match="Expected status code 404"):
            instance.get_something.with_expected_status(404).with_repeat(num=2)()

    def test_sync_with_expected_status_then_with_repeat_collects_assertion_errors_when_return_exceptions(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_expected_status().with_repeat(return_exceptions=True) collects the per-call
        AssertionError for each call with an unexpected status"""
        mocker.patch.object(Client, "request", return_value=_make_httpx_response(500, mocker))
        instance = api_class(api_client)

        results = instance.get_something.with_expected_status(404).with_repeat(num=2, return_exceptions=True)()

        assert len(results) == 2
        assert all(isinstance(r, AssertionError) for r in results)

    def test_sync_with_retry_then_with_repeat_retries_each_call(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_retry().with_repeat() retries each individual call in the group on a matching status"""
        mocker.patch.object(
            Client,
            "request",
            side_effect=[
                _make_httpx_response(503, mocker),
                _make_httpx_response(200, mocker),
                _make_httpx_response(200, mocker),
            ],
        )
        instance = api_class(api_client)

        results = instance.get_something.with_retry(condition=503, num_retries=1, retry_after=0).with_repeat(num=2)()

        assert len(results) == 2
        assert all(isinstance(r, RestResponse) and r.status_code == 200 for r in results)
        # First call: 503 then a retried 200. Second call: 200 on the first attempt
        assert Client.request.call_count == 3

    def test_sync_with_retry_on_exception_then_with_repeat_retries_failing_call_only(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_retry() with an exception condition chained before with_repeat() retries only the
        failing individual call, not the whole group"""
        call_count = 0

        def request_side_effect(*a: Any, **kw: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("transient error")
            return _make_httpx_response(200, mocker)

        mocker.patch.object(Client, "request", side_effect=request_side_effect)
        instance = api_class(api_client)

        endpoint_func = instance.get_something.with_retry(condition=ValueError, num_retries=1, retry_after=0)
        results = endpoint_func.with_repeat(num=2)()

        assert len(results) == 2
        assert all(isinstance(r, RestResponse) and r.status_code == 200 for r in results)
        # First call: failed then retried. Second call: succeeded on the first attempt
        assert call_count == 3

    def test_sync_with_max_response_time_then_with_concurrency_asserts_each_call(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_max_response_time().with_concurrency() asserts the response time of each individual
        call in the group"""
        mocker.patch.object(Client, "request", return_value=_make_httpx_response(200, mocker))
        instance = api_class(api_client)

        results = instance.get_something.with_max_response_time(1000).with_concurrency(num=2)()

        assert len(results) == 2
        assert all(isinstance(r, RestResponse) and r.status_code == 200 for r in results)

    def test_sync_with_polling_then_with_repeat_polls_each_call(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_polling().with_repeat() polls each individual call in the group until the condition
        is met"""
        mocker.patch.object(
            Client,
            "request",
            side_effect=[
                _make_httpx_response(503, mocker),
                _make_httpx_response(200, mocker),
                _make_httpx_response(200, mocker),
            ],
        )
        mocker.patch("time.sleep")
        instance = api_class(api_client)

        results = instance.get_something.with_polling(until=lambda resp: resp.ok, timeout=60).with_repeat(num=2)()

        assert len(results) == 2
        assert all(isinstance(r, RestResponse) and r.status_code == 200 for r in results)
        # First call: polled twice (503 then 200). Second call: 200 on the first poll
        assert Client.request.call_count == 3

    def test_sync_with_lock_and_with_retry_then_with_repeat_composes_by_scope(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that chaining group and per-call wrappers before with_repeat() holds the lock around the whole
        group while retrying each individual call"""
        mocker.patch.object(
            Client,
            "request",
            side_effect=[
                _make_httpx_response(503, mocker),
                _make_httpx_response(200, mocker),
                _make_httpx_response(200, mocker),
            ],
        )
        mock_lock = mocker.patch(f"{_call_wrappers_module.__name__}.{Lock.__name__}")
        mock_lock.return_value.__enter__ = mocker.MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = mocker.MagicMock(return_value=False)
        instance = api_class(api_client)

        endpoint_func = instance.get_something.with_lock().with_retry(condition=503, num_retries=1, retry_after=0)
        results = endpoint_func.with_repeat(num=2)()

        assert len(results) == 2
        assert all(isinstance(r, RestResponse) and r.status_code == 200 for r in results)
        # The lock (group scope) is acquired once for the whole group, while retry (call scope) runs per call
        assert mock_lock.call_count == 1
        assert Client.request.call_count == 3

    async def test_async_with_expected_status_then_with_concurrency_asserts_each_call(
        self, mocker: MockerFixture, api_client_async: APIClient, api_class_async: type[BaseAPI]
    ) -> None:
        """Test that async with_expected_status().with_concurrency() asserts the status of each individual call
        in the group"""
        mocker.patch.object(AsyncClient, "request", return_value=_make_httpx_response(404, mocker))
        instance = api_class_async(api_client_async)

        results = await instance.get_something.with_expected_status(404).with_concurrency(num=2)()

        assert len(results) == 2
        assert all(isinstance(r, RestResponse) and r.status_code == 404 for r in results)

    def test_sync_terminal_wrapper_in_the_middle_raises(self, api_client: APIClient, api_class: type[BaseAPI]) -> None:
        """Test that chaining any wrapper after a terminal one raises RuntimeError at chain-build time."""
        instance = api_class(api_client)
        with pytest.raises(RuntimeError, match="terminal"):
            instance.get_something.with_concurrency().with_retry()

    def test_sync_terminal_wrapper_after_another_terminal_raises(
        self, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that chaining a second terminal wrapper after the first raises RuntimeError."""
        instance = api_class(api_client)
        with pytest.raises(RuntimeError, match="terminal"):
            instance.get_something.with_concurrency().with_repeat()

    def test_sync_repeat_terminal_wrapper_in_the_middle_raises(
        self, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that chaining any wrapper after with_repeat() raises RuntimeError."""
        instance = api_class(api_client)
        with pytest.raises(RuntimeError, match="terminal"):
            instance.get_something.with_repeat().with_expected_status(200)

    async def test_async_terminal_wrapper_in_the_middle_raises(
        self, api_client_async: APIClient, api_class_async: type[BaseAPI]
    ) -> None:
        """Test that chaining any wrapper after a terminal one raises RuntimeError in async mode."""
        instance = api_class_async(api_client_async)
        with pytest.raises(RuntimeError, match="terminal"):
            instance.get_something.with_concurrency().with_retry()


class TestEndpointFuncCallWithRepeat:
    """Tests for with_repeat() — sequential repeated calls that collect all results"""

    def test_sync_with_repeat_returns_all_responses_on_success(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_repeat in sync mode fires N sequential calls and returns all responses"""
        mocker.patch.object(Client, "request", return_value=_make_httpx_response(200, mocker))
        instance = api_class(api_client)
        endpoint_func = instance.get_something

        assert isinstance(endpoint_func, SyncEndpointFunc)

        results = endpoint_func.with_repeat(num=3)()
        assert len(results) == 3
        assert all(isinstance(r, RestResponse) for r in results)
        assert Client.request.call_count == 3

    def test_sync_with_repeat_collects_exceptions_without_propagating(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_repeat collects raised exceptions in-order without stopping the loop"""
        mocker.patch.object(
            Client,
            "request",
            side_effect=[
                _make_httpx_response(200, mocker),
                ValueError("transient error"),
                _make_httpx_response(201, mocker),
            ],
        )
        instance = api_class(api_client)

        # Must not raise despite the middle call failing
        results = instance.get_something.with_repeat(num=3, return_exceptions=True)()

        assert len(results) == 3
        assert isinstance(results[0], RestResponse)
        assert isinstance(results[1], ValueError)
        assert isinstance(results[2], RestResponse)
        # All N calls ran
        assert Client.request.call_count == 3

    def test_sync_with_repeat_collects_all_exceptions(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_repeat collects all failures when every call raises"""
        num = 3
        mocker.patch.object(Client, "request", side_effect=ValueError("always fails"))
        instance = api_class(api_client)

        results = instance.get_something.with_repeat(num=num, return_exceptions=True)()

        assert len(results) == num
        assert all(isinstance(r, ValueError) for r in results)
        assert Client.request.call_count == num

    def test_sync_with_repeat_propagates_exception_by_default(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_repeat propagates exceptions and stops on first failure by default"""
        mocker.patch.object(
            Client,
            "request",
            side_effect=[
                _make_httpx_response(200, mocker),
                ValueError("transient error"),
                _make_httpx_response(200, mocker),
            ],
        )
        instance = api_class(api_client)

        with pytest.raises(ValueError):
            instance.get_something.with_repeat(num=3)()

        # Stopped after the first exception — third call never made
        assert Client.request.call_count == 2

    def test_sync_with_repeat_default_num(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that with_repeat() uses the default of 2 calls when num is not specified"""
        mocker.patch.object(Client, "request", return_value=_make_httpx_response(200, mocker))
        instance = api_class(api_client)

        results = instance.get_something.with_repeat()()

        assert len(results) == 2
        assert Client.request.call_count == 2

    async def test_async_with_repeat_returns_all_responses_on_success(
        self, mocker: MockerFixture, api_client_async: APIClient, api_class_async: type[BaseAPI]
    ) -> None:
        """Test that with_repeat in async mode fires N sequential calls and returns all responses"""
        mocker.patch.object(AsyncClient, "request", return_value=_make_httpx_response(200, mocker))
        instance = api_class_async(api_client_async)
        endpoint_func = instance.get_something

        assert isinstance(endpoint_func, AsyncEndpointFunc)

        results = await endpoint_func.with_repeat(num=3)()
        assert len(results) == 3
        assert all(isinstance(r, RestResponse) for r in results)
        assert AsyncClient.request.call_count == 3

    async def test_async_with_repeat_collects_exceptions_without_propagating(
        self, mocker: MockerFixture, api_client_async: APIClient, api_class_async: type[BaseAPI]
    ) -> None:
        """Test that async with_repeat collects raised exceptions in-order without stopping the loop"""
        mocker.patch.object(
            AsyncClient,
            "request",
            side_effect=[
                _make_httpx_response(200, mocker),
                ValueError("transient error"),
                _make_httpx_response(201, mocker),
            ],
        )
        instance = api_class_async(api_client_async)

        # Must not raise despite the middle call failing
        results = await instance.get_something.with_repeat(num=3, return_exceptions=True)()

        assert len(results) == 3
        assert isinstance(results[0], RestResponse)
        assert isinstance(results[1], ValueError)
        assert isinstance(results[2], RestResponse)
        # All N calls ran
        assert AsyncClient.request.call_count == 3

    async def test_async_with_repeat_collects_all_exceptions(
        self, mocker: MockerFixture, api_client_async: APIClient, api_class_async: type[BaseAPI]
    ) -> None:
        """Test that async with_repeat collects all failures when every call raises"""
        num = 3
        mocker.patch.object(AsyncClient, "request", side_effect=ValueError("always fails"))
        instance = api_class_async(api_client_async)

        results = await instance.get_something.with_repeat(num=num, return_exceptions=True)()

        assert len(results) == num
        assert all(isinstance(r, ValueError) for r in results)
        assert AsyncClient.request.call_count == num

    async def test_async_with_repeat_propagates_exception_by_default(
        self, mocker: MockerFixture, api_client_async: APIClient, api_class_async: type[BaseAPI]
    ) -> None:
        """Test that async with_repeat propagates exceptions and stops on first failure by default"""
        mocker.patch.object(
            AsyncClient,
            "request",
            side_effect=[
                _make_httpx_response(200, mocker),
                ValueError("transient error"),
                _make_httpx_response(200, mocker),
            ],
        )
        instance = api_class_async(api_client_async)

        with pytest.raises(ValueError):
            await instance.get_something.with_repeat(num=3)()

        # Stopped after the first exception — third call never made
        assert AsyncClient.request.call_count == 2

    async def test_async_with_repeat_default_num(
        self, mocker: MockerFixture, api_client_async: APIClient, api_class_async: type[BaseAPI]
    ) -> None:
        """Test that async with_repeat() uses the default of 2 calls when num is not specified"""
        mocker.patch.object(AsyncClient, "request", return_value=_make_httpx_response(200, mocker))
        instance = api_class_async(api_client_async)

        results = await instance.get_something.with_repeat()()

        assert len(results) == 2
        assert AsyncClient.request.call_count == 2


class TestEndpointFuncCallWithStats:
    """Tests for EndpointFunc.with_stats()."""

    @pytest.fixture(autouse=True)
    def reset_stats(self) -> Generator[None, None, None]:
        """Reset the global Stats collector and restore enabled state before and after each test."""
        Stats.reset()
        Stats.enable()
        yield
        Stats.reset()
        Stats.enable()

    def test_sync_with_stats_returns_response_and_shows_report(
        self,
        mocker: MockerFixture,
        api_client: APIClient,
        api_class: type[BaseAPI],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Test that with_stats() returns a RestResponse and prints a stats report without the Endpoint column."""
        mocker.patch.object(Client, "request", return_value=_make_httpx_response(200, mocker))
        instance = api_class(api_client)

        assert isinstance(instance.get_something, SyncEndpointFunc)

        r = instance.get_something.with_stats()()

        assert isinstance(r, RestResponse)
        output = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
        assert "Calls" in output
        assert "GET /v1/something" not in output

    def test_sync_with_stats_shows_report_on_failure(
        self,
        mocker: MockerFixture,
        api_client: APIClient,
        api_class: type[BaseAPI],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Test that with_stats() prints the stats report even when the call raises an exception."""
        mocker.patch.object(Client, "request", side_effect=ValueError("simulated failure"))
        instance = api_class(api_client)

        with pytest.raises(ValueError):
            instance.get_something.with_stats()()

        output = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
        assert "Calls" in output
        assert "GET /v1/something" not in output

    def test_sync_with_stats_show_failure_does_not_mask_call_outcome(
        self,
        mocker: MockerFixture,
        api_client: APIClient,
        api_class: type[BaseAPI],
    ) -> None:
        """Test that a failure in the report printing neither masks the call's exception nor breaks its result."""
        mocker.patch.object(StatsCollector, "show", side_effect=RuntimeError("simulated show failure"))
        instance = api_class(api_client)

        mocker.patch.object(Client, "request", return_value=_make_httpx_response(200, mocker))
        r = instance.get_something.with_stats()()
        assert isinstance(r, RestResponse)

        mocker.patch.object(Client, "request", side_effect=ValueError("simulated failure"))
        with pytest.raises(ValueError, match="simulated failure"):
            instance.get_something.with_stats()()

    def test_sync_with_stats_composes_with_concurrency(
        self,
        mocker: MockerFixture,
        api_client: APIClient,
        api_class: type[BaseAPI],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Test that with_stats().with_concurrency() aggregates all concurrent calls in the report."""
        mocker.patch.object(Client, "request", return_value=_make_httpx_response(200, mocker))
        instance = api_class(api_client)

        results = instance.get_something.with_stats().with_concurrency(num=3)()

        assert len(results) == 3
        assert all(isinstance(r, RestResponse) for r in results)
        assert Client.request.call_count == 3
        stat = Stats.get("GET /v1/something")
        assert stat is not None
        assert stat.num_calls == 3
        output = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
        assert "Calls" in output
        assert "GET /v1/something" not in output

    async def test_async_with_stats_returns_response_and_shows_report(
        self,
        mocker: MockerFixture,
        api_client_async: APIClient,
        api_class_async: type[BaseAPI],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Test that async with_stats() returns a RestResponse and prints a stats report without the Endpoint column."""
        mocker.patch.object(AsyncClient, "request", return_value=_make_httpx_response(200, mocker))
        instance = api_class_async(api_client_async)

        assert isinstance(instance.get_something, AsyncEndpointFunc)

        r = await instance.get_something.with_stats()()

        assert isinstance(r, RestResponse)
        output = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
        assert "Calls" in output
        assert "GET /v1/something" not in output

    async def test_async_with_stats_composes_with_concurrency(
        self,
        mocker: MockerFixture,
        api_client_async: APIClient,
        api_class_async: type[BaseAPI],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Test that async with_stats().with_concurrency() aggregates all concurrent calls in the report."""
        mocker.patch.object(AsyncClient, "request", return_value=_make_httpx_response(200, mocker))
        instance = api_class_async(api_client_async)

        results = await instance.get_something.with_stats().with_concurrency(num=3)()

        assert len(results) == 3
        assert all(isinstance(r, RestResponse) for r in results)
        assert AsyncClient.request.call_count == 3
        stat = Stats.get("GET /v1/something")
        assert stat is not None
        assert stat.num_calls == 3
        output = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
        assert "Calls" in output
        assert "GET /v1/something" not in output


class TestEndpointFuncCallRaiseOnError:
    """Tests for APIClient.raise_on_error flag and its interaction with the request lifecycle"""

    def test_sync_non_2xx_raises_http_status_error_when_flag_enabled(
        self, mocker: MockerFixture, api_class: type[BaseAPI], api_client_factory: Any
    ) -> None:
        """Test that a non-2xx response raises HTTPStatusError when raise_on_error=True"""
        client = api_client_factory(raise_on_error=True)
        mocker.patch.object(Client, "request", return_value=_make_httpx_response(404, mocker))
        instance = api_class(client)

        with pytest.raises(HTTPStatusError):
            instance.get_something()

    def test_sync_2xx_does_not_raise_when_flag_enabled(
        self, mocker: MockerFixture, api_class: type[BaseAPI], api_client_factory: Any
    ) -> None:
        """Test that a 2xx response does not raise even when raise_on_error=True"""
        client = api_client_factory(raise_on_error=True)
        mocker.patch.object(Client, "request", return_value=_make_httpx_response(200, mocker))
        instance = api_class(client)

        r = instance.get_something()
        assert isinstance(r, RestResponse)
        assert r.status_code == 200

    def test_sync_non_2xx_does_not_raise_when_flag_disabled(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that a non-2xx response is returned normally when raise_on_error=False (default)"""
        mocker.patch.object(Client, "request", return_value=_make_httpx_response(404, mocker))
        instance = api_class(api_client)

        r = instance.get_something()
        assert isinstance(r, RestResponse)
        assert r.status_code == 404

    def test_sync_raise_on_error_does_not_prevent_retry_from_seeing_responses(
        self, mocker: MockerFixture, api_class: type[BaseAPI], api_client_factory: Any
    ) -> None:
        """Test that raise_on_error=True with with_retry still retries on matching responses.

        The raise must fire AFTER retry has consumed responses, not before. A 503-then-200
        sequence must retry and succeed rather than raising on the first 503.
        """
        client = api_client_factory(raise_on_error=True)
        mocker.patch.object(
            Client,
            "request",
            side_effect=[_make_httpx_response(503, mocker), _make_httpx_response(200, mocker)],
        )
        instance = api_class(client)

        r = instance.get_something.with_retry(condition=503, num_retries=1, retry_after=0)()

        assert isinstance(r, RestResponse)
        assert r.status_code == 200
        assert Client.request.call_count == 2

    def test_sync_raise_on_error_raises_after_all_retries_exhausted(
        self, mocker: MockerFixture, api_class: type[BaseAPI], api_client_factory: Any
    ) -> None:
        """Test that raise_on_error=True raises only after retries are exhausted"""
        client = api_client_factory(raise_on_error=True)
        mocker.patch.object(
            Client,
            "request",
            side_effect=[
                _make_httpx_response(503, mocker),
                _make_httpx_response(503, mocker),
            ],
        )
        instance = api_class(client)

        with pytest.raises(HTTPStatusError):
            instance.get_something.with_retry(condition=503, num_retries=1, retry_after=0)()

        assert Client.request.call_count == 2

    async def test_async_non_2xx_raises_http_status_error_when_flag_enabled(
        self, mocker: MockerFixture, api_class_async: type[BaseAPI], api_client_factory: Any
    ) -> None:
        """Test that async mode non-2xx raises HTTPStatusError when raise_on_error=True"""
        client = api_client_factory(async_mode=True, raise_on_error=True)
        mocker.patch.object(AsyncClient, "request", return_value=_make_httpx_response(500, mocker))
        instance = api_class_async(client)

        with pytest.raises(HTTPStatusError):
            await instance.get_something()

    async def test_async_raise_on_error_does_not_prevent_retry_from_seeing_responses(
        self, mocker: MockerFixture, api_class_async: type[BaseAPI], api_client_factory: Any
    ) -> None:
        """Test that async raise_on_error=True with with_retry still retries on matching responses"""
        client = api_client_factory(async_mode=True, raise_on_error=True)
        mocker.patch.object(
            AsyncClient,
            "request",
            side_effect=[_make_httpx_response(503, mocker), _make_httpx_response(200, mocker)],
        )
        instance = api_class_async(client)

        r = await instance.get_something.with_retry(condition=503, num_retries=1, retry_after=0)()

        assert isinstance(r, RestResponse)
        assert r.status_code == 200
        assert AsyncClient.request.call_count == 2

    def test_sync_raise_on_error_with_concurrency_returns_responses_on_2xx(
        self, mocker: MockerFixture, api_class: type[BaseAPI], api_client_factory: Any
    ) -> None:
        """Test that raise_on_error=True with with_concurrency returns all responses when every call is 2xx"""
        client = api_client_factory(raise_on_error=True)
        mocker.patch.object(Client, "request", return_value=_make_httpx_response(200, mocker))
        instance = api_class(client)

        results = instance.get_something.with_concurrency(num=2)()

        assert len(results) == 2
        assert all(isinstance(r, RestResponse) for r in results)

    def test_sync_raise_on_error_with_concurrency_collects_errors_when_return_exceptions(
        self, mocker: MockerFixture, api_class: type[BaseAPI], api_client_factory: Any
    ) -> None:
        """Test that raise_on_error=True applies per call inside with_concurrency, so return_exceptions=True
        collects the raised HTTPStatusError for each non-2xx call"""
        client = api_client_factory(raise_on_error=True)
        mocker.patch.object(Client, "request", return_value=_make_httpx_response(404, mocker))
        instance = api_class(client)

        results = instance.get_something.with_concurrency(num=2, return_exceptions=True)()

        assert len(results) == 2
        assert all(isinstance(r, HTTPStatusError) for r in results)

    def test_sync_raise_on_error_with_repeat_raises_per_call_on_non_2xx(
        self, mocker: MockerFixture, api_class: type[BaseAPI], api_client_factory: Any
    ) -> None:
        """Test that raise_on_error=True with with_repeat raises on the first failing call in the group"""
        client = api_client_factory(raise_on_error=True)
        mocker.patch.object(
            Client,
            "request",
            side_effect=[
                _make_httpx_response(200, mocker),
                _make_httpx_response(404, mocker),
                _make_httpx_response(200, mocker),
            ],
        )
        instance = api_class(client)

        with pytest.raises(HTTPStatusError):
            instance.get_something.with_repeat(num=3)()

        assert Client.request.call_count == 2

    def test_sync_raise_on_error_with_lock_chained_before_repeat_holds_lock_around_group(
        self, mocker: MockerFixture, api_class: type[BaseAPI], api_client_factory: Any
    ) -> None:
        """Test that a wrapper chained before a multi-call wrapper still wraps the whole group while
        raise_on_error fires per call inside it"""
        client = api_client_factory(raise_on_error=True)
        mocker.patch.object(
            Client,
            "request",
            side_effect=[_make_httpx_response(200, mocker), _make_httpx_response(404, mocker)],
        )
        mock_lock = mocker.patch(f"{_call_wrappers_module.__name__}.{Lock.__name__}")
        mock_lock.return_value.__enter__ = mocker.MagicMock(return_value=None)
        mock_lock.return_value.__exit__ = mocker.MagicMock(return_value=False)
        instance = api_class(client)

        with pytest.raises(HTTPStatusError):
            instance.get_something.with_lock().with_repeat(num=2)()

        mock_lock.assert_called_once()
        assert Client.request.call_count == 2

    async def test_async_raise_on_error_with_concurrency_returns_responses_on_2xx(
        self, mocker: MockerFixture, api_class_async: type[BaseAPI], api_client_factory: Any
    ) -> None:
        """Test that async raise_on_error=True with with_concurrency returns all responses when every call is 2xx"""
        client = api_client_factory(async_mode=True, raise_on_error=True)
        mocker.patch.object(AsyncClient, "request", return_value=_make_httpx_response(200, mocker))
        instance = api_class_async(client)

        results = await instance.get_something.with_concurrency(num=2)()

        assert len(results) == 2
        assert all(isinstance(r, RestResponse) for r in results)

    async def test_async_raise_on_error_with_repeat_collects_errors_when_return_exceptions(
        self, mocker: MockerFixture, api_class_async: type[BaseAPI], api_client_factory: Any
    ) -> None:
        """Test that async raise_on_error=True applies per call inside with_repeat, so return_exceptions=True
        collects the raised HTTPStatusError for each non-2xx call"""
        client = api_client_factory(async_mode=True, raise_on_error=True)
        mocker.patch.object(AsyncClient, "request", return_value=_make_httpx_response(404, mocker))
        instance = api_class_async(client)

        results = await instance.get_something.with_repeat(num=2, return_exceptions=True)()

        assert len(results) == 2
        assert all(isinstance(r, HTTPStatusError) for r in results)

    def test_sync_raise_on_error_with_expected_status_returns_expected_non_2xx(
        self, mocker: MockerFixture, api_class: type[BaseAPI], api_client_factory: Any
    ) -> None:
        """Test that a non-2xx status declared via with_expected_status is exempt from raise_on_error"""
        client = api_client_factory(raise_on_error=True)
        mocker.patch.object(Client, "request", return_value=_make_httpx_response(404, mocker))
        instance = api_class(client)

        r = instance.get_something.with_expected_status(404)()

        assert isinstance(r, RestResponse)
        assert r.status_code == 404

    def test_sync_raise_on_error_with_expected_status_raises_assertion_error_for_unexpected_status(
        self, mocker: MockerFixture, api_class: type[BaseAPI], api_client_factory: Any
    ) -> None:
        """Test that an unexpected status on a with_expected_status chain raises AssertionError from the
        status check (which sits inside the raise wrapper, so HTTPStatusError is never reached)"""
        client = api_client_factory(raise_on_error=True)
        mocker.patch.object(Client, "request", return_value=_make_httpx_response(500, mocker))
        instance = api_class(client)

        with pytest.raises(AssertionError, match="Expected status code 404"):
            instance.get_something.with_expected_status(404)()

    def test_sync_raise_on_error_with_expected_status_exemption_propagates_through_chaining(
        self, mocker: MockerFixture, api_class: type[BaseAPI], api_client_factory: Any
    ) -> None:
        """Test that the expected-status exemption survives further with_xxx() chaining"""
        client = api_client_factory(raise_on_error=True)
        mocker.patch.object(Client, "request", return_value=_make_httpx_response(404, mocker))
        instance = api_class(client)

        r = instance.get_something.with_expected_status(404).with_retry(condition=503, num_retries=1, retry_after=0)()

        assert isinstance(r, RestResponse)
        assert r.status_code == 404

    def test_sync_raise_on_error_original_endpoint_func_is_unaffected_by_with_expected_status(
        self, mocker: MockerFixture, api_class: type[BaseAPI], api_client_factory: Any
    ) -> None:
        """Test that with_expected_status only exempts the returned copy, not the original endpoint func"""
        client = api_client_factory(raise_on_error=True)
        mocker.patch.object(Client, "request", return_value=_make_httpx_response(404, mocker))
        instance = api_class(client)

        endpoint_func = instance.get_something
        endpoint_func.with_expected_status(404)  # discard the returned copy

        with pytest.raises(HTTPStatusError):
            endpoint_func()

    async def test_async_raise_on_error_with_expected_status_returns_expected_non_2xx(
        self, mocker: MockerFixture, api_class_async: type[BaseAPI], api_client_factory: Any
    ) -> None:
        """Test that async mode exempts a non-2xx status declared via with_expected_status from raise_on_error"""
        client = api_client_factory(async_mode=True, raise_on_error=True)
        mocker.patch.object(AsyncClient, "request", return_value=_make_httpx_response(404, mocker))
        instance = api_class_async(client)

        r = await instance.get_something.with_expected_status(404)()

        assert isinstance(r, RestResponse)
        assert r.status_code == 404

    def test_sync_raise_on_error_with_expected_status_chained_before_repeat_returns_expected_non_2xx(
        self, mocker: MockerFixture, api_class: type[BaseAPI], api_client_factory: Any
    ) -> None:
        """Test that the expected-status exemption applies to each individual call in a with_repeat() group"""
        client = api_client_factory(raise_on_error=True)
        mocker.patch.object(Client, "request", return_value=_make_httpx_response(404, mocker))
        instance = api_class(client)

        results = instance.get_something.with_expected_status(404).with_repeat(num=2)()

        assert len(results) == 2
        assert all(isinstance(r, RestResponse) and r.status_code == 404 for r in results)

    def test_sync_raise_on_error_with_expected_status_chained_before_repeat_mixed_statuses(
        self, mocker: MockerFixture, api_class: type[BaseAPI], api_client_factory: Any
    ) -> None:
        """Test that within a with_repeat() group, the expected-status exemption and assertion both apply per
        call: a call with the expected status is exempt and returned, while a call with a different non-2xx
        status fails its own assertion instead of being exempted by the other call's outcome"""
        client = api_client_factory(raise_on_error=True)
        mocker.patch.object(
            Client,
            "request",
            side_effect=[_make_httpx_response(404, mocker), _make_httpx_response(503, mocker)],
        )
        instance = api_class(client)

        results = instance.get_something.with_expected_status(404).with_repeat(num=2, return_exceptions=True)()

        assert len(results) == 2
        assert isinstance(results[0], RestResponse)
        assert results[0].status_code == 404
        assert isinstance(results[1], AssertionError)

    def test_sync_raise_on_error_with_retry_chained_before_repeat_sees_responses(
        self, mocker: MockerFixture, api_class: type[BaseAPI], api_client_factory: Any
    ) -> None:
        """Test that raise_on_error=True with with_retry().with_repeat() still retries per call on matching
        responses.

        The per-call raise must fire AFTER retry has consumed responses, not before. A 503-then-200 sequence
        within the group must retry and succeed rather than raising on the 503.
        """
        client = api_client_factory(raise_on_error=True)
        mocker.patch.object(
            Client,
            "request",
            side_effect=[
                _make_httpx_response(503, mocker),
                _make_httpx_response(200, mocker),
                _make_httpx_response(200, mocker),
            ],
        )
        instance = api_class(client)

        results = instance.get_something.with_retry(condition=503, num_retries=1, retry_after=0).with_repeat(num=2)()

        assert len(results) == 2
        assert all(isinstance(r, RestResponse) and r.status_code == 200 for r in results)
        assert Client.request.call_count == 3


class TestEndpointFuncCallWithPagination:
    """Tests for with_pagination() — lazily paginated API calls driven by a get_next callback"""

    def test_sync_multi_page_yields_all_responses(self, mocker: MockerFixture, api_client: APIClient) -> None:
        """Test that sync with_pagination yields one RestResponse per page until get_next returns None."""

        class PaginatedListAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.get("/v1/items")
            def list_items(self, *, cursor: str = Unset) -> RestResponse: ...

        mocker.patch.object(
            Client,
            "request",
            side_effect=[
                _make_httpx_response(200, mocker, headers={"X-Next-Cursor": "page2"}),
                _make_httpx_response(200, mocker, headers={"X-Next-Cursor": "page3"}),
                _make_httpx_response(200, mocker),
            ],
        )

        def get_next(r: RestResponse) -> dict[str, Any] | None:
            cursor = r._response.headers.get("X-Next-Cursor")
            return {"cursor": cursor} if cursor else None

        instance = PaginatedListAPI(api_client)
        pages = list(instance.list_items.with_pagination(get_next)())

        assert len(pages) == 3
        assert all(isinstance(p, RestResponse) for p in pages)
        assert Client.request.call_count == 3

    async def test_async_multi_page_yields_all_responses(
        self, mocker: MockerFixture, api_client_async: APIClient
    ) -> None:
        """Test that async with_pagination yields one RestResponse per page until get_next returns None."""

        class PaginatedListAPI(BaseAPI):
            app_name = api_client_async.app_name

            @endpoint.get("/v1/items")
            def list_items(self, *, cursor: str = Unset) -> RestResponse: ...

        mocker.patch.object(
            AsyncClient,
            "request",
            side_effect=[
                _make_httpx_response(200, mocker, headers={"X-Next-Cursor": "page2"}),
                _make_httpx_response(200, mocker, headers={"X-Next-Cursor": "page3"}),
                _make_httpx_response(200, mocker),
            ],
        )

        def get_next(r: RestResponse) -> dict[str, Any] | None:
            cursor = r._response.headers.get("X-Next-Cursor")
            return {"cursor": cursor} if cursor else None

        instance = PaginatedListAPI(api_client_async)
        pages = [p async for p in instance.list_items.with_pagination(get_next)()]

        assert len(pages) == 3
        assert all(isinstance(p, RestResponse) for p in pages)
        assert AsyncClient.request.call_count == 3

    def test_sync_single_page_stops_when_get_next_returns_none(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that sync with_pagination stops after the first page when get_next returns None."""
        mocker.patch.object(Client, "request", return_value=_make_httpx_response(200, mocker))
        instance = api_class(api_client)

        pages = list(instance.get_something.with_pagination(lambda r: None)())

        assert len(pages) == 1
        assert isinstance(pages[0], RestResponse)
        assert Client.request.call_count == 1

    async def test_async_single_page_stops_when_get_next_returns_none(
        self, mocker: MockerFixture, api_client_async: APIClient, api_class_async: type[BaseAPI]
    ) -> None:
        """Test that async with_pagination stops after the first page when get_next returns None."""
        mocker.patch.object(AsyncClient, "request", return_value=_make_httpx_response(200, mocker))
        instance = api_class_async(api_client_async)

        pages = [p async for p in instance.get_something.with_pagination(lambda r: None)()]

        assert len(pages) == 1
        assert isinstance(pages[0], RestResponse)
        assert AsyncClient.request.call_count == 1

    def test_sync_empty_dict_continues_without_new_params(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that returning {} from get_next continues pagination without adding new params."""
        mocker.patch.object(Client, "request", return_value=_make_httpx_response(200, mocker))
        instance = api_class(api_client)

        pages = list(instance.get_something.with_pagination(lambda r: {}, limit=3)())

        assert len(pages) == 3
        assert Client.request.call_count == 3

    def test_sync_limit_caps_the_number_of_pages(self, mocker: MockerFixture, api_client: APIClient) -> None:
        """Test that limit stops pagination regardless of what get_next returns."""

        class PaginatedListAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.get("/v1/items")
            def list_items(self, *, cursor: str = Unset) -> RestResponse: ...

        mocker.patch.object(Client, "request", return_value=_make_httpx_response(200, mocker))
        instance = PaginatedListAPI(api_client)

        pages = list(instance.list_items.with_pagination(lambda r: {"cursor": "next"}, limit=2)())

        assert len(pages) == 2
        assert Client.request.call_count == 2

    async def test_async_limit_caps_the_number_of_pages(
        self, mocker: MockerFixture, api_client_async: APIClient
    ) -> None:
        """Test that async limit stops pagination regardless of what get_next returns."""

        class PaginatedListAPI(BaseAPI):
            app_name = api_client_async.app_name

            @endpoint.get("/v1/items")
            def list_items(self, *, cursor: str = Unset) -> RestResponse: ...

        mocker.patch.object(AsyncClient, "request", return_value=_make_httpx_response(200, mocker))
        instance = PaginatedListAPI(api_client_async)

        pages = [p async for p in instance.list_items.with_pagination(lambda r: {"cursor": "next"}, limit=2)()]

        assert len(pages) == 2
        assert AsyncClient.request.call_count == 2

    def test_sync_next_page_params_are_merged_into_subsequent_requests(
        self, mocker: MockerFixture, api_client: APIClient
    ) -> None:
        """Test that the params returned by get_next are passed as query params to the next request."""

        class PaginatedListAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.get("/v1/items")
            def list_items(self, *, cursor: str = Unset) -> RestResponse: ...

        mocker.patch.object(
            Client,
            "request",
            side_effect=[
                _make_httpx_response(200, mocker, headers={"X-Next-Cursor": "abc"}),
                _make_httpx_response(200, mocker),
            ],
        )

        def get_next(r: RestResponse) -> dict[str, Any] | None:
            cursor = r._response.headers.get("X-Next-Cursor")
            return {"cursor": cursor} if cursor else None

        instance = PaginatedListAPI(api_client)
        list(instance.list_items.with_pagination(get_next)())

        assert Client.request.call_count == 2
        first_params = Client.request.call_args_list[0].kwargs.get("params") or {}
        second_params = Client.request.call_args_list[1].kwargs.get("params") or {}
        assert "cursor" not in first_params
        assert second_params.get("cursor") == "abc"

    def test_sync_with_retry_applies_per_page_inside_pagination_loop(
        self, mocker: MockerFixture, api_client: APIClient
    ) -> None:
        """Test that with_retry().with_pagination() retries each page independently inside the loop."""

        class PaginatedListAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.get("/v1/items")
            def list_items(self, *, cursor: str = Unset) -> RestResponse: ...

        mocker.patch.object(
            Client,
            "request",
            side_effect=[
                _make_httpx_response(503, mocker),  # page 1 attempt 1 (retry)
                _make_httpx_response(200, mocker, headers={"X-Next-Cursor": "p2"}),  # page 1 attempt 2 (success)
                _make_httpx_response(200, mocker),  # page 2 (success, no cursor)
            ],
        )

        def get_next(r: RestResponse) -> dict[str, Any] | None:
            cursor = r._response.headers.get("X-Next-Cursor")
            return {"cursor": cursor} if cursor else None

        instance = PaginatedListAPI(api_client)
        pages = list(
            instance.list_items.with_retry(condition=503, num_retries=1, retry_after=0).with_pagination(get_next)()
        )

        assert len(pages) == 2
        assert all(isinstance(p, RestResponse) for p in pages)
        assert Client.request.call_count == 3  # 2 attempts for page 1 + 1 for page 2

    def test_sync_raise_on_error_applies_per_page_inside_pagination_loop(
        self, mocker: MockerFixture, api_class: type[BaseAPI], api_client_factory: Any
    ) -> None:
        """Test that raise_on_error=True raises on the failing page and not on prior successful pages."""
        client = api_client_factory(raise_on_error=True)
        mocker.patch.object(
            Client,
            "request",
            side_effect=[
                _make_httpx_response(200, mocker),
                _make_httpx_response(404, mocker),
            ],
        )
        instance = api_class(client)
        paginator = instance.get_something.with_pagination(lambda r: {}, limit=3)()

        first_page = next(paginator)
        assert isinstance(first_page, RestResponse)
        assert first_page.status_code == 200

        with pytest.raises(HTTPStatusError):
            next(paginator)

    async def test_async_with_retry_applies_per_page_inside_pagination_loop(
        self, mocker: MockerFixture, api_client_async: APIClient
    ) -> None:
        """Test that async with_retry().with_pagination() retries each page independently inside the loop."""

        class PaginatedListAPI(BaseAPI):
            app_name = api_client_async.app_name

            @endpoint.get("/v1/items")
            def list_items(self, *, cursor: str = Unset) -> RestResponse: ...

        mocker.patch.object(
            AsyncClient,
            "request",
            side_effect=[
                _make_httpx_response(503, mocker),  # page 1 attempt 1 (retry)
                _make_httpx_response(200, mocker, headers={"X-Next-Cursor": "p2"}),  # page 1 attempt 2 (success)
                _make_httpx_response(200, mocker),  # page 2 (success, no cursor)
            ],
        )

        def get_next(r: RestResponse) -> dict[str, Any] | None:
            cursor = r._response.headers.get("X-Next-Cursor")
            return {"cursor": cursor} if cursor else None

        instance = PaginatedListAPI(api_client_async)
        pages = [
            p
            async for p in instance.list_items.with_retry(condition=503, num_retries=1, retry_after=0).with_pagination(
                get_next
            )()
        ]

        assert len(pages) == 2
        assert all(isinstance(p, RestResponse) for p in pages)
        assert AsyncClient.request.call_count == 3  # 2 attempts for page 1 + 1 for page 2

    async def test_async_raise_on_error_applies_per_page_inside_pagination_loop(
        self, mocker: MockerFixture, api_class_async: type[BaseAPI], api_client_factory: Any
    ) -> None:
        """Test that async raise_on_error=True raises on the failing page and not on prior successful pages."""
        client = api_client_factory(async_mode=True, raise_on_error=True)
        mocker.patch.object(
            AsyncClient,
            "request",
            side_effect=[
                _make_httpx_response(200, mocker),
                _make_httpx_response(404, mocker),
            ],
        )
        instance = api_class_async(client)
        paginator = instance.get_something.with_pagination(lambda r: {}, limit=3)()

        first_page = await paginator.__anext__()
        assert isinstance(first_page, RestResponse)
        assert first_page.status_code == 200

        with pytest.raises(HTTPStatusError):
            await paginator.__anext__()

    def test_sync_pagination_is_lazy(
        self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that calling the paginator does not make any request until the first iteration step."""
        mocker.patch.object(Client, "request", return_value=_make_httpx_response(200, mocker))
        instance = api_class(api_client)

        paginator = instance.get_something.with_pagination(lambda r: None)()
        assert Client.request.call_count == 0

        next(paginator)
        assert Client.request.call_count == 1

    async def test_async_pagination_is_lazy(
        self, mocker: MockerFixture, api_client_async: APIClient, api_class_async: type[BaseAPI]
    ) -> None:
        """Test that calling the async paginator does not make any request until the first iteration step."""
        mocker.patch.object(AsyncClient, "request", return_value=_make_httpx_response(200, mocker))
        instance = api_class_async(api_client_async)

        paginator = instance.get_something.with_pagination(lambda r: None)()
        assert AsyncClient.request.call_count == 0

        await paginator.__anext__()
        assert AsyncClient.request.call_count == 1

    def test_limit_below_one_raises(self, api_client: APIClient, api_class: type[BaseAPI]) -> None:
        """Test that limit values below 1 raise a ValueError."""
        instance = api_class(api_client)
        with pytest.raises(ValueError, match="limit"):
            instance.get_something.with_pagination(lambda r: None, limit=0)
        with pytest.raises(ValueError, match="limit"):
            instance.get_something.with_pagination(lambda r: None, limit=-1)

    def test_sync_terminal_before_pagination_raises(self, api_client: APIClient, api_class: type[BaseAPI]) -> None:
        """Test that chaining with_pagination after a terminal wrapper raises RuntimeError."""
        instance = api_class(api_client)
        with pytest.raises(RuntimeError, match="terminal"):
            instance.get_something.with_repeat().with_pagination(lambda r: None)

    def test_sync_pagination_is_terminal(self, api_client: APIClient, api_class: type[BaseAPI]) -> None:
        """Test that with_pagination itself is terminal — further chaining raises RuntimeError."""
        instance = api_class(api_client)
        with pytest.raises(RuntimeError, match="terminal"):
            instance.get_something.with_pagination(lambda r: None).with_retry()


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


def _raise_with_request(exc: Exception, method: str) -> NoReturn:
    """Attach a request with the given HTTP method to the exception and raise it.

    Mimics common-libs' RestClient.send(), which attaches the original request to any raised
    exception so retry_on() can read the HTTP method for its safe_methods_only check.

    :param exc: Exception to raise
    :param method: HTTP method string (e.g. "GET", "POST") to embed in the attached request
    """
    set_request_to_exception(exc, Request(method, "https://example.com/api/v1/something"))
    raise exc

from collections.abc import Callable
from typing import TypeVar

import pytest
from common_libs.clients.rest_client import AsyncRestClient, RestClient, RestResponse
from common_libs.clients.rest_client.utils import get_supported_request_parameters
from httpx import AsyncClient, Client
from pytest import FixtureRequest
from pytest_mock import MockerFixture

from api_client_core import endpoint
from api_client_core.base import APIClient, BaseAPI

pytest_plugins = ["common_libs.testing.pytest_plugins.common"]

ClientT = TypeVar("ClientT", bound=APIClient)
ClassT = TypeVar("ClassT", bound=BaseAPI)

# `get_supported_request_parameters()` is `lru_cache`d against the real `httpx.Client.request` signature.
# Call it here, at collection time, so the cache is primed before `api_client_factory` below replaces
# `Client.request` with a mock (which would otherwise poison the cache for the rest of this worker process).
get_supported_request_parameters()


@pytest.fixture(scope="module")
def api_client_factory(session_mocker: MockerFixture) -> Callable[..., APIClient]:
    """Core API client factory"""

    def create(async_mode: bool = False, raise_on_error: bool = False) -> APIClient:
        base_url = "https://example.com/api"
        rest_client: RestClient | AsyncRestClient
        if async_mode:
            session_mocker.patch.object(AsyncClient, "request")
            rest_client = AsyncRestClient(base_url)
        else:
            session_mocker.patch.object(Client, "request")
            rest_client = RestClient(base_url)
        return APIClient("test", rest_client=rest_client, async_mode=async_mode, raise_on_error=raise_on_error)

    return create


@pytest.fixture(scope="module")
def api_class_factory() -> Callable[..., type[BaseAPI]]:
    """API class factory that creates a testable API class with one endpoint function"""

    def create_api_class(api_client: APIClient) -> type[BaseAPI]:
        class TestAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.get("/v1/something")
            def get_something(self) -> RestResponse: ...

        return TestAPI

    return create_api_class


@pytest.fixture
def api_client(request: FixtureRequest, api_client_factory: Callable[..., ClientT]) -> ClientT:
    """A general API client for testing, with support for async mode via test parameterization"""
    if hasattr(request, "param"):
        mode = request.param
        assert mode in ["sync", "async"], "Invalid mode parameter, must be 'sync' or 'async'"
        is_async = mode == "async"
        return api_client_factory(async_mode=is_async)
    return api_client_factory()


@pytest.fixture(scope="module")
def api_client_async(api_client_factory: Callable[..., ClientT]) -> ClientT:
    """A general API client for testing (async)"""
    return api_client_factory(async_mode=True)


@pytest.fixture
def api_class(api_client: APIClient, api_class_factory: Callable[[ClientT], type[ClassT]]) -> type[ClassT]:
    """A testable API class with one endpoint function"""
    return api_class_factory(api_client)


@pytest.fixture(scope="module")
def api_class_async(api_client_async: APIClient, api_class_factory: Callable[[ClientT], type[ClassT]]) -> type[ClassT]:
    """A testable API class with one endpoint function (async)"""
    return api_class_factory(api_client_async)

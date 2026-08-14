"""Unit tests for BaseAPI (api_class.py)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from common_libs.clients.rest_client import RestResponse
from pytest_mock import MockerFixture

from api_client_core import endpoint
from api_client_core.base import APIClient, BaseAPI
from api_client_core.base.api_class import get_api_classes, get_endpoints
from api_client_core.endpoints import Endpoint


class TestBaseAPIInit:
    """Tests for BaseAPI.init()"""

    def test_init_supports_direct_baseapi_subclassing(
        self, mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that init() can be called directly on BaseAPI when API classes subclass BaseAPI directly"""
        # BaseAPI.endpoints is a shared class attribute. Restore it after the test since init() assigns it directly.
        monkeypatch.setattr(BaseAPI, "endpoints", BaseAPI.endpoints)

        class DirectAuthAPI(BaseAPI):
            app_name = "direct-test"

            @endpoint.get("/v1/auth")
            def get_auth(self) -> RestResponse: ...

        class DirectUsersAPI(BaseAPI):
            app_name = "direct-test"

            @endpoint.get("/v1/users")
            def get_users(self) -> RestResponse: ...

        mock_prev_frame = MagicMock()
        mock_prev_frame.f_globals = {"__name__": "fake_direct_module"}
        mocker.patch("inspect.currentframe", return_value=MagicMock(f_back=mock_prev_frame))
        mocker.patch("inspect.getframeinfo", return_value=MagicMock(filename="/fake/api/__init__.py"))
        mocker.patch(
            f"{get_api_classes.__module__}.{get_api_classes.__name__}",
            return_value=[DirectAuthAPI, DirectUsersAPI],
        )

        result: list[type[BaseAPI]] = BaseAPI.init()

        assert result == [DirectAuthAPI, DirectUsersAPI]
        assert DirectAuthAPI.endpoints is not None
        assert len(DirectAuthAPI.endpoints) == 1
        assert DirectUsersAPI.endpoints is not None
        assert len(DirectUsersAPI.endpoints) == 1
        assert BaseAPI.endpoints is not None
        paths = [ep.path for ep in BaseAPI.endpoints]
        assert paths == ["/v1/auth", "/v1/users"]

    def test_init_raises_runtime_error_when_not_called_from_init_py(self) -> None:
        """Test that calling init() from a non-__init__.py file raises RuntimeError"""

        class MyBaseAPI(BaseAPI):
            app_name = "test"

        # This test file is not __init__.py, so calling init() here raises
        with pytest.raises(RuntimeError, match=r"API classes must be initialized in __init__\.py"):
            MyBaseAPI.init()

    def test_init_populates_endpoints_on_discovered_classes(self, mocker: MockerFixture) -> None:
        """Test that init() populates the endpoints list on each discovered API class"""

        class DiscoveryBaseAPI(BaseAPI):
            app_name = "discovery-test"

        class DiscoveryConcreteAPI(DiscoveryBaseAPI):
            app_name = "discovery-test"

            @endpoint.get("/v1/items")
            def list_items(self) -> RestResponse: ...

            @endpoint.post("/v1/items")
            def create_item(self, name: str) -> RestResponse: ...

        mock_prev_frame = MagicMock()
        mock_prev_frame.f_globals = {"__name__": "fake_api_module"}
        mocker.patch("inspect.currentframe", return_value=MagicMock(f_back=mock_prev_frame))
        mocker.patch("inspect.getframeinfo", return_value=MagicMock(filename="/fake/api/__init__.py"))
        mocker.patch(
            f"{get_api_classes.__module__}.{get_api_classes.__name__}",
            return_value=[DiscoveryConcreteAPI],
        )

        result = DiscoveryBaseAPI.init()

        assert DiscoveryConcreteAPI in result
        assert DiscoveryConcreteAPI.endpoints is not None
        assert len(DiscoveryConcreteAPI.endpoints) == 2
        assert all(isinstance(ep, Endpoint) for ep in DiscoveryConcreteAPI.endpoints)

    def test_init_populates_base_class_endpoints_as_sorted_aggregate(self, mocker: MockerFixture) -> None:
        """Test that init() populates the base class endpoints list with sorted aggregate of all subclass endpoints"""

        class AggregateBaseAPI(BaseAPI):
            app_name = "agg-test"

        # AggregateAlphaAPI sorts before AggregateBetaAPI by class name
        class AggregateAlphaAPI(AggregateBaseAPI):
            app_name = "agg-test"

            @endpoint.get("/v1/alpha")
            def alpha(self) -> RestResponse: ...

        class AggregateBetaAPI(AggregateBaseAPI):
            app_name = "agg-test"

            @endpoint.get("/v1/beta")
            def beta(self) -> RestResponse: ...

        mock_prev_frame = MagicMock()
        mock_prev_frame.f_globals = {"__name__": "fake_agg_module"}
        mocker.patch("inspect.currentframe", return_value=MagicMock(f_back=mock_prev_frame))
        mocker.patch("inspect.getframeinfo", return_value=MagicMock(filename="/fake/api/__init__.py"))
        mocker.patch(
            f"{get_api_classes.__module__}.{get_api_classes.__name__}",
            return_value=[AggregateAlphaAPI, AggregateBetaAPI],
        )

        AggregateBaseAPI.init()

        assert AggregateBaseAPI.endpoints is not None
        # sorted by (api_class.__name__, method, path): AggregateAlphaAPI < AggregateBetaAPI
        paths = [ep.path for ep in AggregateBaseAPI.endpoints]
        assert paths == ["/v1/alpha", "/v1/beta"]

    def test_init_returns_discovered_api_classes(self, mocker: MockerFixture) -> None:
        """Test that init() returns the list of discovered API classes"""

        class ReturnBaseAPI(BaseAPI):
            app_name = "return-test"

        class ReturnAPI(ReturnBaseAPI):
            app_name = "return-test"

            @endpoint.get("/v1/things")
            def list_things(self) -> RestResponse: ...

        mock_prev_frame = MagicMock()
        mock_prev_frame.f_globals = {"__name__": "fake_return_module"}
        mocker.patch("inspect.currentframe", return_value=MagicMock(f_back=mock_prev_frame))
        mocker.patch("inspect.getframeinfo", return_value=MagicMock(filename="/fake/api/__init__.py"))
        mocker.patch(
            f"{get_api_classes.__module__}.{get_api_classes.__name__}",
            return_value=[ReturnAPI],
        )

        result = ReturnBaseAPI.init()

        assert result == [ReturnAPI]


class TestGetEndpoints:
    """Tests for get_endpoints()"""

    def test_returns_endpoint_per_handler_in_declaration_order(self, api_client: APIClient) -> None:
        """Test that get_endpoints() returns one Endpoint per EndpointHandler descriptor, in declaration order"""

        class TestAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.get("/v1/first")
            def first(self) -> RestResponse: ...

            @endpoint.post("/v1/second")
            def second(self) -> RestResponse: ...

        endpoints = get_endpoints(TestAPI)
        assert [ep.func_name for ep in endpoints] == ["first", "second"]

    def test_ignores_non_endpoint_handler_attributes(self, api_client: APIClient) -> None:
        """Test that get_endpoints() ignores class attributes that aren't EndpointHandler descriptors"""

        class TestAPI(BaseAPI):
            app_name = api_client.app_name
            some_constant = 42

            @endpoint.get("/v1/only")
            def only(self) -> RestResponse: ...

        endpoints = get_endpoints(TestAPI)
        assert [ep.func_name for ep in endpoints] == ["only"]

    def test_includes_an_endpoint_inherited_from_a_base_api_subclass(self, api_client: APIClient) -> None:
        """Test that an endpoint defined on a base `BaseAPI` subclass is included for a subclass that
        declares no endpoints of its own, and that the resolved `Endpoint.api_class` reports the subclass
        (not the base class the descriptor was actually defined on), matching normal Python attribute-lookup
        semantics
        """

        class SharedBaseAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.get("/v1/shared")
            def shared(self) -> RestResponse: ...

        class ConcreteAPI(SharedBaseAPI):
            app_name = api_client.app_name

        endpoints = get_endpoints(ConcreteAPI)
        assert [ep.func_name for ep in endpoints] == ["shared"]
        assert endpoints[0].api_class is ConcreteAPI

    def test_a_subclass_override_shadows_the_base_classs_own_endpoint(self, api_client: APIClient) -> None:
        """Test that a subclass overriding an endpoint under the same attribute name as a base class yields
        only the subclass's own version, not both
        """

        class OverriddenBaseAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.get("/v1/old")
            def get_thing(self) -> RestResponse: ...

        class OverridingAPI(OverriddenBaseAPI):
            app_name = api_client.app_name

            @endpoint.get("/v1/new")
            def get_thing(self) -> RestResponse: ...

        endpoints = get_endpoints(OverridingAPI)
        assert [(ep.func_name, ep.path) for ep in endpoints] == [("get_thing", "/v1/new")]

    def test_combines_inherited_and_own_endpoints(self, api_client: APIClient) -> None:
        """Test that a subclass's own endpoints and an inherited one from its base class are both included,
        with the subclass's own endpoints listed first (most-derived class scanned first)
        """

        class MixedBaseAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.get("/v1/inherited")
            def inherited(self) -> RestResponse: ...

        class MixedAPI(MixedBaseAPI):
            app_name = api_client.app_name

            @endpoint.get("/v1/own")
            def own(self) -> RestResponse: ...

        endpoints = get_endpoints(MixedAPI)
        assert [ep.func_name for ep in endpoints] == ["own", "inherited"]

    def test_a_subclass_shadows_an_inherited_endpoint_with_a_plain_attribute(self, api_client: APIClient) -> None:
        """Test that a subclass overriding an inherited endpoint's attribute name with a plain, non-endpoint
        attribute drops that endpoint instead of raising, matching how an override with another endpoint
        (test_a_subclass_override_shadows_the_base_classs_own_endpoint) is already handled
        """

        class ShadowedBaseAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.get("/v1/thing")
            def get_thing(self) -> RestResponse: ...

        class ShadowingAPI(ShadowedBaseAPI):
            app_name = api_client.app_name

            def get_thing(self) -> str:
                return "not an endpoint"

        endpoints = get_endpoints(ShadowingAPI)
        assert endpoints == []


class TestBaseAPIInstantiation:
    """Tests for BaseAPI.__init__"""

    def test_app_name_mismatch_raises_value_error(self, api_client: APIClient) -> None:
        """Test that instantiating an API class with a mismatched app_name raises ValueError"""

        class MismatchedAPI(BaseAPI):
            app_name = "wrong-app"

            @endpoint.get("/v1/test")
            def get_test(self) -> RestResponse: ...

        with pytest.raises(ValueError, match="app_name for API class"):
            MismatchedAPI(api_client)

    def test_unset_app_name_skips_validation(self, api_client: APIClient) -> None:
        """Test that instantiating an API class with no app_name set does not raise, regardless of client app_name"""

        class NoAppNameAPI(BaseAPI):
            @endpoint.get("/v1/test")
            def get_test(self) -> RestResponse: ...

        instance = NoAppNameAPI(api_client)
        assert instance.api_client is api_client

    def test_instantiation_sets_env_and_rest_client(self, api_client: APIClient) -> None:
        """Test that BaseAPI.__init__ copies env and rest_client from the API client"""

        class MatchedAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.get("/v1/test")
            def get_test(self) -> RestResponse: ...

        instance = MatchedAPI(api_client)
        assert instance.api_client is api_client
        assert instance.rest_client is api_client.rest_client
        assert instance.env == api_client.env

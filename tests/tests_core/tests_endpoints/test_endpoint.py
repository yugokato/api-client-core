"""Unit tests for endpoint.py"""

from __future__ import annotations

import inspect
from typing import Annotated, Unpack

import pytest
from common_libs.clients.rest_client import RestResponse
from httpx2 import AsyncClient, Client
from pytest_mock import MockerFixture

from api_client_core import Endpoint, EndpointFunc, EndpointIntrospection, endpoint
from api_client_core.core.base import APIClient, BaseAPI
from api_client_core.types import Kwargs, Unset


class TestEndpointObject:
    """Tests for the Endpoint object attached to EndpointFunc"""

    @pytest.mark.parametrize("with_instance", [True, False])
    def test_attrs(self, api_client: APIClient, api_class: type[BaseAPI], with_instance: bool) -> None:
        """Test that Endpoint has correct default field values"""
        if with_instance:
            ep = api_class(api_client).get_something.endpoint
        else:
            ep = api_class.get_something.endpoint
        assert ep.api_class is api_class
        assert ep.method == "get"
        assert ep.path == "/v1/something"
        assert ep.func_name == "get_something"
        if with_instance:
            assert ep.url == "https://example.com/api/v1/something"
        else:
            assert ep.url is None
        assert ep.content_type is None
        assert ep.use_query_string is True  # api_class's get_something uses @endpoint.get, always forced True
        assert ep.is_public is False
        assert ep.is_documented is True
        assert ep.is_deprecated is False

    def test_use_query_string_reflects_the_decorator_flag(self, api_client: APIClient) -> None:
        """Test that Endpoint.use_query_string reflects the flag given to a non-GET method decorator"""

        class TestAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.post("/v1/something", use_query_string=True)
            def post_something(self) -> RestResponse: ...

            @endpoint.post("/v1/other")
            def post_other(self) -> RestResponse: ...

        assert TestAPI.post_something.endpoint.use_query_string is True
        assert TestAPI.post_other.endpoint.use_query_string is False

    def test_str(self, api_class: type[BaseAPI]) -> None:
        """Test that Endpoint.__str__ formats the method and path correctly"""

        ep = api_class.get_something.endpoint
        assert str(ep) == f"{ep.method.upper()} {ep.path}"

    def test_eq(self, api_class: type[BaseAPI]) -> None:
        """Test that endpoints with the same api_class, method, and path are equal regardless of func_name"""
        ep = api_class.get_something.endpoint
        other = Endpoint(
            api_class=ep.api_class,
            method=ep.method,
            path=ep.path,
            func_name="different_name",
            model=ep.model,
        )
        assert ep == other

    def test_eq_not_equal(self, api_class: type[BaseAPI]) -> None:
        """Test that Endpoint objects with different method or path are not equal"""
        ep = api_class.get_something.endpoint

        different_path = Endpoint(
            api_class=ep.api_class,
            method=ep.method,
            path="/v1/other",
            func_name=ep.func_name,
            model=ep.model,
        )
        assert ep != different_path

        different_method = Endpoint(
            api_class=ep.api_class,
            method="post",
            path=ep.path,
            func_name=ep.func_name,
            model=ep.model,
        )
        assert ep != different_method

    def test_hash_is_stable_and_consistent(self, api_class: type[BaseAPI]) -> None:
        """Test that Endpoint hash is stable (same value each call) and consistent between equal endpoints"""
        ep = api_class.get_something.endpoint
        other = Endpoint(
            api_class=ep.api_class,
            method=ep.method,
            path=ep.path,
            func_name="different_name",
            model=ep.model,
        )
        assert hash(ep) == hash(ep)
        assert ep == other
        assert hash(ep) == hash(other)

    def test_eq_different_api_class_not_equal(self, api_client: APIClient) -> None:
        """Test that endpoints with the same method and path on different API classes are not equal"""

        class API1(BaseAPI):
            app_name = api_client.app_name

            @endpoint.get("/v1/shared")
            def shared_endpoint(self) -> RestResponse: ...

        class API2(BaseAPI):
            app_name = api_client.app_name

            @endpoint.get("/v1/shared")
            def shared_endpoint(self) -> RestResponse: ...

        ep1 = API1.shared_endpoint.endpoint
        ep2 = API2.shared_endpoint.endpoint

        assert ep1 != ep2
        assert hash(ep1) != hash(ep2)

    def test_is_frozen(self, api_class: type[BaseAPI]) -> None:
        """Test that Endpoint is a frozen dataclass and attributes cannot be modified"""
        ep = api_class.get_something.endpoint
        with pytest.raises(AttributeError, match="cannot assign to field"):
            ep.path = "/new/path"

    def test_endpoint_metadata_propagates(self, api_client: APIClient) -> None:
        """Test that endpoint metadata applied on an endpoint function propagates from endpoint handler to Endpoint"""

        class TestAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.undocumented
            @endpoint.is_public
            @endpoint.is_deprecated
            @endpoint.content_type("application/xml")
            @endpoint.get("/v1/something")
            def get_something(self) -> RestResponse: ...

        instance = TestAPI(api_client)
        ep = instance.get_something.endpoint

        assert ep.is_documented is False
        assert ep.is_public is True
        assert ep.is_deprecated is True
        assert ep.content_type == "application/xml"

    def test_class_level_endpoint_flag_propagates(self, api_client: APIClient) -> None:
        """Test that endpoint metadata applied on API class propagates from endpoint handler to Endpoint"""

        @endpoint.undocumented
        @endpoint.is_deprecated
        class TestAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.get("/v1/something")
            def get_something(self) -> RestResponse: ...

        instance = TestAPI(api_client)
        ep = instance.get_something.endpoint

        assert ep.is_documented is False
        assert ep.is_public is False
        assert ep.is_deprecated is True

    def test_endpoint_call(self, mocker: MockerFixture, api_client: APIClient, api_class: type[BaseAPI]) -> None:
        """Test that Endpoint.__call__ makes the correct HTTP call and returns RestResponse in sync mode"""
        mock_httpx_request = mocker.patch.object(Client, "request")
        ep = api_class.get_something.endpoint
        r = ep(api_client)
        assert isinstance(r, RestResponse)
        mock_httpx_request.assert_called_once()

    async def test_endpoint_call_async(
        self, mocker: MockerFixture, api_client_async: APIClient, api_class: type[BaseAPI]
    ) -> None:
        """Test that Endpoint.__call__ makes the correct HTTP call and returns RestResponse in async mode"""
        mock_httpx_request = mocker.patch.object(AsyncClient, "request")
        ep = api_class.get_something.endpoint
        r = await ep(api_client_async)
        assert isinstance(r, RestResponse)
        mock_httpx_request.assert_called_once()

    def test_endpoint_call_forwards_auth_raw_option(self, mocker: MockerFixture, api_client: APIClient) -> None:
        """Test that a per-call `raw_options={"auth": ...}` passed to Endpoint.__call__ reaches the underlying
        HTTP request"""

        class TestAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.get("/v1/something")
            def get_something(self, **kwargs: Unpack[Kwargs]) -> RestResponse: ...

        mock_httpx_request = mocker.patch.object(Client, "request")
        ep = TestAPI.get_something.endpoint
        ep(api_client, raw_options={"auth": None})
        assert mock_httpx_request.call_args.kwargs["auth"] is None


class TestEndpointIntrospection:
    """Tests for `Endpoint.introspection` and `EndpointIntrospection`'s
    `original_func`/`signature`/`description`/`summary`/`param_docs`
    """

    def test_introspection_returns_a_view_bound_to_the_same_endpoint(self, api_client: APIClient) -> None:
        """Test that `introspection` returns an `EndpointIntrospection` whose `endpoint` is the exact
        endpoint it was reached from, not merely an equal one
        """

        class TestAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.get("/v1/something")
            def get_something(self) -> RestResponse: ...

        ep = TestAPI.get_something.endpoint
        introspection = ep.introspection
        assert isinstance(introspection, EndpointIntrospection)
        assert introspection.endpoint is ep

    def test_original_func_returns_the_original_function(self, api_client: APIClient) -> None:
        """Test that `original_func` returns the original API class function, giving introspection access
        to its signature and docstring without reaching into the generated model's internals"""

        class TestAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.get("/v1/something")
            def get_something(self, value: int) -> RestResponse:
                """Get something"""
                ...

        ep = TestAPI.get_something.endpoint
        assert ep.introspection.original_func.__doc__ == "Get something"
        assert list(inspect.signature(ep.introspection.original_func).parameters) == ["self", "value"]

    def test_signature_has_self_removed(self, api_client: APIClient) -> None:
        """Test that `signature` matches the original function's signature with `self` stripped"""

        class TestAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.post("/v1/things")
            def create_thing(self, name: str, note: str = "n/a") -> RestResponse: ...

        assert list(TestAPI.create_thing.endpoint.introspection.signature.parameters) == ["name", "note"]

    def test_description_summary_and_param_docs_split_the_original_docstring(self, api_client: APIClient) -> None:
        """Test that `description`/`summary`/`param_docs` split the original function's docstring the same
        way `split_param_docs()` does, with `:param` entries excluded from `description`
        """

        class TestAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.post("/v1/things")
            def create_thing(self, name: str) -> RestResponse:
                """Create a thing.

                :param name: The thing's display name
                """
                ...

        introspection = TestAPI.create_thing.endpoint.introspection
        assert introspection.description == "Create a thing."
        assert introspection.summary == "Create a thing."
        assert introspection.param_docs == {"name": "The thing's display name"}

    def test_description_summary_and_param_docs_are_empty_for_no_docstring(self, api_client: APIClient) -> None:
        """Test that `description`/`summary`/`param_docs` degrade to an empty string/`None`/an empty dict
        for a function with no docstring at all, rather than raising
        """

        class TestAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.post("/v1/things")
            def create_thing(self, name: str) -> RestResponse: ...

        introspection = TestAPI.create_thing.endpoint.introspection
        assert introspection.description == ""
        assert introspection.summary is None
        assert introspection.param_docs == {}

    def test_param_docs_is_read_only(self, api_client: APIClient) -> None:
        """Test that `param_docs` returns a read-only mapping, so a caller can't corrupt the cache
        `split_param_docs()` shares across every endpoint with the same docstring text
        """

        class TestAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.post("/v1/things")
            def create_thing(self, name: str) -> RestResponse:
                """Create a thing.

                :param name: The thing's display name
                """
                ...

        with pytest.raises(TypeError):
            TestAPI.create_thing.endpoint.introspection.param_docs["name"] = "tampered"


class TestEndpointBind:
    """Tests for `Endpoint.bind()`"""

    def test_bind_returns_a_callable_bound_endpoint_func_for_the_same_endpoint(self, api_client: APIClient) -> None:
        """Test that `bind()` returns an `EndpointFunc` for this endpoint, callable the same way
        `client.<resource>.<func_name>` is
        """

        class TestAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.get("/v1/things")
            def list_things(self) -> RestResponse: ...

        ep = TestAPI.list_things.endpoint
        bound = ep.bind(api_client)
        assert isinstance(bound, EndpointFunc)
        assert bound.endpoint == ep

    def test_bind_result_makes_the_correct_http_call(self, mocker: MockerFixture, api_client: APIClient) -> None:
        """Test that calling the object `bind()` returns actually dispatches the endpoint's HTTP call"""

        class TestAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.get("/v1/things")
            def list_things(self) -> RestResponse: ...

        mock_httpx_request = mocker.patch.object(Client, "request")
        ep = TestAPI.list_things.endpoint
        r = ep.bind(api_client)()
        assert isinstance(r, RestResponse)
        mock_httpx_request.assert_called_once()


class TestEndpointIterParams:
    """Tests for `EndpointIntrospection.iter_params()` and `EndpointParam`"""

    def test_required_reflects_the_signature_default(self, api_client: APIClient) -> None:
        """Test that `EndpointParam.required` is True only for a parameter with no signature default"""

        class TestAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.post("/v1/things")
            def create_thing(self, name: str, note: str = "n/a") -> RestResponse: ...

        params = {p.name: p for p in TestAPI.create_thing.endpoint.introspection.iter_params()}
        assert params["name"].required is True
        assert params["note"].required is False

    def test_yields_a_parameter_resolving_to_a_reserved_name_unfiltered(self, api_client: APIClient) -> None:
        """Test that `iter_params()` yields every parameter, including one resolving to a reserved
        control-kwarg name (e.g. `quiet`), leaving reserved-name filtering to the caller
        """

        class TestAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.post("/v1/things")
            def create_thing(self, quiet: str = "ok", **kwargs: Unpack[Kwargs]) -> RestResponse: ...

        names = {p.name for p in TestAPI.create_thing.endpoint.introspection.iter_params()}
        assert "quiet" in names

    def test_resolves_an_aliased_field_back_to_its_signature_name(self, api_client: APIClient) -> None:
        """Test that a model field renamed away from a reserved name (here `quiet` -> `quiet_`) resolves,
        via its `Alias` metadata, back to the original signature parameter name rather than the renamed
        model field name
        """

        class TestAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.post("/v1/things")
            def create_thing(self, quiet: str = "ok", **kwargs: Unpack[Kwargs]) -> RestResponse: ...

        ep = TestAPI.create_thing.endpoint
        assert "quiet_" in ep.model.__dataclass_fields__
        assert {p.name for p in ep.introspection.iter_params()} == {"quiet"}

    def test_annotation_default_has_default_and_deprecated(self, api_client: APIClient) -> None:
        """Test that `EndpointParam.annotation`/`default`/`has_default`/`deprecated` reflect the model
        field each parameter was resolved from
        """

        class TestAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.post("/v1/things")
            def create_thing(
                self, name: str, note: str = "n/a", legacy: Annotated[str, "deprecated"] = Unset
            ) -> RestResponse: ...

        params = {p.name: p for p in TestAPI.create_thing.endpoint.introspection.iter_params()}
        assert params["name"].annotation is str
        assert params["name"].has_default is False
        assert params["name"].deprecated is False

        assert params["note"].has_default is True
        assert params["note"].default == "n/a"
        assert params["note"].deprecated is False

        assert params["legacy"].deprecated is True

    def test_location_is_path_for_a_path_placeholder(self, api_client: APIClient) -> None:
        """Test that `EndpointParam.location` is `path` for a parameter matching a `{placeholder}`"""

        class TestAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.post("/v1/things/{thing_id}")
            def get_thing(self, thing_id: str) -> RestResponse: ...

        params = {p.name: p for p in TestAPI.get_thing.endpoint.introspection.iter_params()}
        assert params["thing_id"].location == "path"

    def test_location_is_body_by_default_on_a_non_get_method(self, api_client: APIClient) -> None:
        """Test that `EndpointParam.location` is `body` for a non-path parameter on a non-GET endpoint
        with no `use_query_string` and no `Query` annotation
        """

        class TestAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.post("/v1/things")
            def create_thing(self, name: str) -> RestResponse: ...

        params = {p.name: p for p in TestAPI.create_thing.endpoint.introspection.iter_params()}
        assert params["name"].location == "body"

    def test_location_is_query_for_a_get_method(self, api_client: APIClient) -> None:
        """Test that `EndpointParam.location` is `query` for a non-path parameter on a GET endpoint"""

        class TestAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.get("/v1/things")
            def list_things(self, name: str) -> RestResponse: ...

        params = {p.name: p for p in TestAPI.list_things.endpoint.introspection.iter_params()}
        assert params["name"].location == "query"

    def test_location_is_query_when_use_query_string_forces_it(self, api_client: APIClient) -> None:
        """Test that `EndpointParam.location` is `query` for a non-path parameter on a non-GET endpoint
        whose `use_query_string` flag forces it
        """

        class TestAPI(BaseAPI):
            app_name = api_client.app_name

            @endpoint.post("/v1/things", use_query_string=True)
            def create_thing(self, name: str) -> RestResponse: ...

        params = {p.name: p for p in TestAPI.create_thing.endpoint.introspection.iter_params()}
        assert params["name"].location == "query"

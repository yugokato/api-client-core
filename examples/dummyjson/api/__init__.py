from typing import TYPE_CHECKING, Any

from httpx2 import HTTPError

from api_client_core import BaseAPI, Endpoint
from api_client_core.types import RestResponse

if TYPE_CHECKING:
    from examples.dummyjson.client import DummyJSONClient


class DummyJSONBaseAPI(BaseAPI["DummyJSONClient"]):
    """Base class for all DummyJSON (https://dummyjson.com/) API classes."""

    app_name = "DummyJSON"

    def post_request_hook(
        self,
        endpoint: Endpoint,
        response: RestResponse | None,
        exception: HTTPError | None,
        *path_params: Any,
        **params: Any,
    ) -> None:
        """Automatically attach the access token from a successful login/refresh as the bearer token"""
        auth_endpoints = (self.api_client.auth.login.endpoint, self.api_client.auth.refresh_token.endpoint)
        if response and response.ok and endpoint in auth_endpoints:
            self.api_client.rest_client.token = response.response["accessToken"]


API_CLASSES = DummyJSONBaseAPI.init()

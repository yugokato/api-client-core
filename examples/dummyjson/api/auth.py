from typing import Annotated, Unpack

from api_client_core import endpoint
from api_client_core.types import Alias, Kwargs, RestResponse, Unset

from . import DummyJSONBaseAPI


class AuthAPI(DummyJSONBaseAPI):
    """Auth APIs

    https://dummyjson.com/docs/auth
    """

    @endpoint.post("/auth/login")
    def login(
        self,
        username: str,
        password: str,
        expires_in_mins: Annotated[int, Alias("expiresInMins")] = Unset,
        **kwargs: Unpack[Kwargs],
    ) -> RestResponse:
        """Log in and get an access/refresh token pair

        :param username: Username of the user to authenticate
        :param password: Password of the user to authenticate
        :param expires_in_mins: Access/refresh token validity period, in minutes
        """
        ...

    @endpoint.get("/auth/me")
    def get_current_user(self, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Get the currently authenticated user"""
        ...

    @endpoint.post("/auth/refresh")
    def refresh_token(
        self,
        refresh_token: Annotated[str, Alias("refreshToken")] = Unset,
        expires_in_mins: Annotated[int, Alias("expiresInMins")] = Unset,
        **kwargs: Unpack[Kwargs],
    ) -> RestResponse:
        """Refresh the access token

        :param refresh_token: Refresh token to exchange for a new access token
        :param expires_in_mins: Access/refresh token validity period, in minutes
        """
        ...

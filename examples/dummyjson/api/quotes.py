from typing import Unpack

from api_client_core import endpoint
from api_client_core.types import Kwargs, RestResponse, Unset

from . import DummyJSONBaseAPI


class QuotesAPI(DummyJSONBaseAPI):
    """Quote APIs

    https://dummyjson.com/docs/quotes
    """

    @endpoint.get("/quotes")
    def list_quotes(self, limit: int = Unset, skip: int = Unset, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """List quotes, with optional pagination

        :param limit: Maximum number of quotes to return
        :param skip: Number of quotes to skip, for pagination
        """
        ...

    @endpoint.get("/quotes/{quote_id}")
    def get_quote(self, quote_id: int, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Get a quote by ID

        :param quote_id: ID of the quote to fetch
        """
        ...

    @endpoint.get("/quotes/random")
    def get_random_quote(self, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Get a single random quote. Changes on every call"""
        ...

    @endpoint.get("/quotes/random/{length}")
    def get_random_quotes(self, length: int, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Get up to 10 random quotes. Changes on every call

        :param length: Number of random quotes to return, up to 10
        """
        ...

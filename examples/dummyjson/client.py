from functools import cached_property
from typing import Any

from api_client_core import APIClient

from .api.auth import AuthAPI
from .api.carts import CartsAPI
from .api.comments import CommentsAPI
from .api.posts import PostsAPI
from .api.products import ProductsAPI
from .api.quotes import QuotesAPI
from .api.recipes import RecipesAPI
from .api.todos import TodosAPI
from .api.users import UsersAPI


class DummyJSONClient(APIClient):
    """API client for the DummyJSON example service."""

    app_name = "DummyJSON"

    def __init__(self, *, base_url: str = "https://dummyjson.com", async_mode: bool = False, **kwargs: Any) -> None:
        super().__init__(base_url=base_url, async_mode=async_mode, **kwargs)

    @cached_property
    def auth(self) -> AuthAPI:
        return AuthAPI(self)

    @cached_property
    def products(self) -> ProductsAPI:
        return ProductsAPI(self)

    @cached_property
    def users(self) -> UsersAPI:
        return UsersAPI(self)

    @cached_property
    def carts(self) -> CartsAPI:
        return CartsAPI(self)

    @cached_property
    def posts(self) -> PostsAPI:
        return PostsAPI(self)

    @cached_property
    def comments(self) -> CommentsAPI:
        return CommentsAPI(self)

    @cached_property
    def todos(self) -> TodosAPI:
        return TodosAPI(self)

    @cached_property
    def quotes(self) -> QuotesAPI:
        return QuotesAPI(self)

    @cached_property
    def recipes(self) -> RecipesAPI:
        return RecipesAPI(self)

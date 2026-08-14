from typing import Annotated, Literal, Unpack

from api_client_core import endpoint
from api_client_core.types import Alias, Kwargs, RestResponse, Unset

from . import DummyJSONBaseAPI


class UsersAPI(DummyJSONBaseAPI):
    """User APIs

    https://dummyjson.com/docs/users
    """

    @endpoint.get("/users")
    def list_users(
        self,
        limit: int = Unset,
        skip: int = Unset,
        select: str = Unset,
        sort_by: Annotated[str, Alias("sortBy")] = Unset,
        order: Literal["asc", "desc"] = Unset,
        **kwargs: Unpack[Kwargs],
    ) -> RestResponse:
        """List users, with optional pagination, field selection, and sorting

        :param limit: Maximum number of users to return
        :param skip: Number of users to skip, for pagination
        :param select: Comma-separated field names to include in each user, e.g. `firstName,age`
        :param sort_by: Field name to sort by, e.g. `firstName`
        :param order: Sort direction to apply when `sort_by` is given
        """
        ...

    @endpoint.get("/users/{user_id}")
    def get_user(self, user_id: int, select: str = Unset, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Get a user by ID

        :param user_id: ID of the user to fetch
        :param select: Comma-separated field names to include in the response, e.g. `firstName,age`
        """
        ...

    @endpoint.get("/users/search")
    def search_users(self, q: str, limit: int = Unset, skip: int = Unset, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Search users by query term

        :param q: Search query term
        :param limit: Maximum number of users to return
        :param skip: Number of users to skip, for pagination
        """
        ...

    @endpoint.get("/users/filter")
    def filter_users(
        self,
        key: str,
        value: str,
        limit: int = Unset,
        skip: int = Unset,
        select: str = Unset,
        **kwargs: Unpack[Kwargs],
    ) -> RestResponse:
        """Filter users by a field name and value

        :param key: Field name to filter by, e.g. `hair.color`
        :param value: Value to match against `key`
        :param limit: Maximum number of users to return
        :param skip: Number of users to skip, for pagination
        :param select: Comma-separated field names to include in each user, e.g. `firstName,age`
        """
        ...

    @endpoint.get("/users/{user_id}/carts")
    def get_user_carts(self, user_id: int, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Get all carts belonging to a user

        :param user_id: ID of the user whose carts to fetch
        """
        ...

    @endpoint.get("/users/{user_id}/posts")
    def get_user_posts(self, user_id: int, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Get all posts belonging to a user

        :param user_id: ID of the user whose posts to fetch
        """
        ...

    @endpoint.get("/users/{user_id}/todos")
    def get_user_todos(self, user_id: int, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Get all todos belonging to a user

        :param user_id: ID of the user whose todos to fetch
        """
        ...

    @endpoint.post("/users/add")
    def create_user(
        self,
        first_name: Annotated[str, Alias("firstName")],
        last_name: Annotated[str, Alias("lastName")],
        age: int,
        **kwargs: Unpack[Kwargs],
    ) -> RestResponse:
        """Create a user

        :param first_name: User's first name
        :param last_name: User's last name
        :param age: User's age
        """
        ...

    @endpoint.put("/users/{user_id}")
    def update_user(
        self,
        user_id: int,
        first_name: Annotated[str, Alias("firstName")],
        last_name: Annotated[str, Alias("lastName")],
        age: int,
        **kwargs: Unpack[Kwargs],
    ) -> RestResponse:
        """Replace a user

        :param user_id: ID of the user to update
        :param first_name: New first name
        :param last_name: New last name
        :param age: New age
        """
        ...

    @endpoint.patch("/users/{user_id}")
    def patch_user(
        self,
        user_id: int,
        first_name: Annotated[str, Alias("firstName")] = Unset,
        last_name: Annotated[str, Alias("lastName")] = Unset,
        age: int = Unset,
        **kwargs: Unpack[Kwargs],
    ) -> RestResponse:
        """Partially update a user

        :param user_id: ID of the user to update
        :param first_name: New first name
        :param last_name: New last name
        :param age: New age
        """
        ...

    @endpoint.delete("/users/{user_id}")
    def delete_user(self, user_id: int, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Delete a user

        :param user_id: ID of the user to delete
        """
        ...

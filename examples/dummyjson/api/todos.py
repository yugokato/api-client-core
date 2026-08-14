from typing import Annotated, Unpack

from api_client_core import endpoint
from api_client_core.types import Alias, Kwargs, RestResponse, Unset

from . import DummyJSONBaseAPI


class TodosAPI(DummyJSONBaseAPI):
    """Todo APIs

    https://dummyjson.com/docs/todos
    """

    @endpoint.get("/todos")
    def list_todos(self, limit: int = Unset, skip: int = Unset, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """List todos, with optional pagination

        :param limit: Maximum number of todos to return
        :param skip: Number of todos to skip, for pagination
        """
        ...

    @endpoint.get("/todos/{todo_id}")
    def get_todo(self, todo_id: int, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Get a todo by ID

        :param todo_id: ID of the todo to fetch
        """
        ...

    @endpoint.get("/todos/random")
    def get_random_todo(self, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Get a single random todo. Changes on every call"""
        ...

    @endpoint.get("/todos/random/{length}")
    def get_random_todos(self, length: int, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Get up to 10 random todos. Changes on every call

        :param length: Number of random todos to return, up to 10
        """
        ...

    @endpoint.get("/todos/user/{user_id}")
    def list_todos_by_user(self, user_id: int, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """List todos belonging to a user

        :param user_id: ID of the user whose todos to list
        """
        ...

    @endpoint.post("/todos/add")
    def create_todo(
        self,
        todo: str,
        completed: bool,
        user_id: Annotated[int, Alias("userId")],
        **kwargs: Unpack[Kwargs],
    ) -> RestResponse:
        """Create a todo

        :param todo: Todo text
        :param completed: Whether the todo is completed
        :param user_id: ID of the user the todo belongs to
        """
        ...

    @endpoint.put("/todos/{todo_id}")
    def update_todo(
        self,
        todo_id: int,
        todo: str,
        completed: bool,
        user_id: Annotated[int, Alias("userId")],
        **kwargs: Unpack[Kwargs],
    ) -> RestResponse:
        """Replace a todo

        :param todo_id: ID of the todo to update
        :param todo: New todo text
        :param completed: Whether the todo is completed
        :param user_id: ID of the user the todo belongs to
        """
        ...

    @endpoint.patch("/todos/{todo_id}")
    def patch_todo(
        self,
        todo_id: int,
        todo: str = Unset,
        completed: bool = Unset,
        user_id: Annotated[int, Alias("userId")] = Unset,
        **kwargs: Unpack[Kwargs],
    ) -> RestResponse:
        """Partially update a todo

        :param todo_id: ID of the todo to update
        :param todo: New todo text
        :param completed: Whether the todo is completed
        :param user_id: ID of the user the todo belongs to
        """
        ...

    @endpoint.delete("/todos/{todo_id}")
    def delete_todo(self, todo_id: int, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Delete a todo

        :param todo_id: ID of the todo to delete
        """
        ...

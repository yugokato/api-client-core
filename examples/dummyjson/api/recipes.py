from typing import Annotated, Literal, Unpack

from api_client_core import endpoint
from api_client_core.types import Alias, Kwargs, RestResponse, Unset

from . import DummyJSONBaseAPI


class RecipesAPI(DummyJSONBaseAPI):
    """Recipe APIs

    https://dummyjson.com/docs/recipes
    """

    @endpoint.get("/recipes")
    def list_recipes(
        self,
        limit: int = Unset,
        skip: int = Unset,
        select: str = Unset,
        sort_by: Annotated[str, Alias("sortBy")] = Unset,
        order: Literal["asc", "desc"] = Unset,
        **kwargs: Unpack[Kwargs],
    ) -> RestResponse:
        """List recipes, with optional pagination, field selection, and sorting

        :param limit: Maximum number of recipes to return
        :param skip: Number of recipes to skip, for pagination
        :param select: Comma-separated field names to include in each recipe, e.g. `name,cuisine`
        :param sort_by: Field name to sort by, e.g. `name`
        :param order: Sort direction to apply when `sort_by` is given
        """
        ...

    @endpoint.get("/recipes/{recipe_id}")
    def get_recipe(self, recipe_id: int, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Get a recipe by ID

        :param recipe_id: ID of the recipe to fetch
        """
        ...

    @endpoint.get("/recipes/search")
    def search_recipes(self, q: str, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Search recipes by query term

        :param q: Search query term
        """
        ...

    @endpoint.get("/recipes/tags")
    def list_recipe_tags(self, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """List all recipe tags"""
        ...

    @endpoint.get("/recipes/tag/{tag}")
    def list_recipes_by_tag(self, tag: str, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """List recipes tagged with a given tag

        :param tag: Tag to filter recipes by
        """
        ...

    @endpoint.get("/recipes/meal-type/{meal_type}")
    def list_recipes_by_meal_type(self, meal_type: str, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """List recipes belonging to a meal type

        :param meal_type: Meal type to filter recipes by, e.g. `breakfast`
        """
        ...

    @endpoint.post("/recipes/add")
    def create_recipe(self, name: str, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Create a recipe

        :param name: Recipe name
        """
        ...

    @endpoint.put("/recipes/{recipe_id}")
    def update_recipe(self, recipe_id: int, name: str, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Replace a recipe

        :param recipe_id: ID of the recipe to update
        :param name: New recipe name
        """
        ...

    @endpoint.patch("/recipes/{recipe_id}")
    def patch_recipe(self, recipe_id: int, name: str = Unset, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Partially update a recipe

        :param recipe_id: ID of the recipe to update
        :param name: New recipe name
        """
        ...

    @endpoint.delete("/recipes/{recipe_id}")
    def delete_recipe(self, recipe_id: int, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Delete a recipe

        :param recipe_id: ID of the recipe to delete
        """
        ...

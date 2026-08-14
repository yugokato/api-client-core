from typing import Annotated, Unpack

from api_client_core import endpoint
from api_client_core.types import Alias, Kwargs, RestResponse, Unset

from . import DummyJSONBaseAPI


class CartsAPI(DummyJSONBaseAPI):
    """Cart APIs

    https://dummyjson.com/docs/carts
    """

    @endpoint.get("/carts")
    def list_carts(self, limit: int = Unset, skip: int = Unset, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """List all carts, with optional pagination

        :param limit: Maximum number of carts to return
        :param skip: Number of carts to skip, for pagination
        """
        ...

    @endpoint.get("/carts/{cart_id}")
    def get_cart(self, cart_id: int, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Get a cart by ID

        :param cart_id: ID of the cart to fetch
        """
        ...

    @endpoint.get("/carts/user/{user_id}")
    def list_user_carts(
        self, user_id: int, limit: int = Unset, skip: int = Unset, **kwargs: Unpack[Kwargs]
    ) -> RestResponse:
        """List all carts belonging to a user, with optional pagination

        :param user_id: ID of the user whose carts to list
        :param limit: Maximum number of carts to return
        :param skip: Number of carts to skip, for pagination
        """
        ...

    @endpoint.post("/carts/add")
    def create_cart(
        self, user_id: Annotated[int, Alias("userId")], products: list[dict[str, int]], **kwargs: Unpack[Kwargs]
    ) -> RestResponse:
        """Create a cart for a user from a list of products

        :param user_id: ID of the user to create the cart for
        :param products: Products to add to the cart, each as `{"id": <product_id>, "quantity": <quantity>}`
        """
        ...

    @endpoint.put("/carts/{cart_id}")
    def update_cart(
        self, cart_id: int, products: list[dict[str, int]], merge: bool = Unset, **kwargs: Unpack[Kwargs]
    ) -> RestResponse:
        """Replace a cart's products, optionally merging with the existing ones

        :param cart_id: ID of the cart to update
        :param products: New products for the cart, each as `{"id": <product_id>, "quantity": <quantity>}`
        :param merge: Whether to merge `products` with the cart's existing products instead of replacing them
        """
        ...

    @endpoint.patch("/carts/{cart_id}")
    def patch_cart(
        self,
        cart_id: int,
        products: list[dict[str, int]] = Unset,
        merge: bool = Unset,
        **kwargs: Unpack[Kwargs],
    ) -> RestResponse:
        """Partially update a cart's products, optionally merging with the existing ones

        :param cart_id: ID of the cart to update
        :param products: Products to apply to the cart, each as `{"id": <product_id>, "quantity": <quantity>}`
        :param merge: Whether to merge `products` with the cart's existing products instead of replacing them
        """
        ...

    @endpoint.delete("/carts/{cart_id}")
    def delete_cart(self, cart_id: int, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Delete a cart

        :param cart_id: ID of the cart to delete
        """
        ...

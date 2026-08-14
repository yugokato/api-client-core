from typing import Annotated, Literal, Unpack

from api_client_core import endpoint
from api_client_core.types import Alias, Kwargs, Query, RestResponse, Unset

from . import DummyJSONBaseAPI


class ProductsAPI(DummyJSONBaseAPI):
    """Product APIs

    https://dummyjson.com/docs/products
    """

    @endpoint.get("/products")
    def list_products(
        self,
        limit: int = Unset,
        skip: int = Unset,
        select: str = Unset,
        sort_by: Annotated[str, Alias("sortBy")] = Unset,
        order: Literal["asc", "desc"] = Unset,
        **kwargs: Unpack[Kwargs],
    ) -> RestResponse:
        """List products, with optional pagination, field selection, and sorting

        :param limit: Maximum number of products to return
        :param skip: Number of products to skip, for pagination
        :param select: Comma-separated field names to include in each product, e.g. `title,price`
        :param sort_by: Field name to sort by, e.g. `title`
        :param order: Sort direction to apply when `sort_by` is given
        """
        ...

    @endpoint.get("/products/{product_id}")
    def get_product(self, product_id: int, select: str = Unset, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Get a product by ID, optionally selecting specific fields

        :param product_id: ID of the product to fetch
        :param select: Comma-separated field names to include in the response, e.g. `title,price`
        """
        ...

    @endpoint.get("/products/search")
    def search_products(self, q: str, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Search products by query term

        :param q: Search query term
        """
        ...

    @endpoint.get("/products/categories")
    def list_product_categories(self, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """List all product categories, with their slugs, names, and URLs"""
        ...

    @endpoint.get("/products/category-list")
    def list_product_category_names(self, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """List all product category slugs"""
        ...

    @endpoint.get("/products/category/{category}")
    def list_products_by_category(self, category: str, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """List products belonging to a category

        :param category: Category slug to filter products by
        """
        ...

    @endpoint.post("/products/add")
    def create_product(
        self,
        title: str,
        price: float,
        discount_percentage: Annotated[float, Alias("discountPercentage")] = Unset,
        delay: Annotated[int, Query()] = Unset,
        **kwargs: Unpack[Kwargs],
    ) -> RestResponse:
        """Create a product

        :param title: Product title
        :param price: Product price
        :param discount_percentage: Discount percentage applied to `price`
        :param delay: Artificial response delay, in milliseconds, useful for testing slow-response handling
        """
        ...

    @endpoint.put("/products/{product_id}")
    def update_product(self, product_id: int, title: str, price: float, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Replace a product

        :param product_id: ID of the product to update
        :param title: New product title
        :param price: New product price
        """
        ...

    @endpoint.patch("/products/{product_id}")
    def patch_product(
        self, product_id: int, title: str = Unset, price: float = Unset, **kwargs: Unpack[Kwargs]
    ) -> RestResponse:
        """Partially update a product

        :param product_id: ID of the product to update
        :param title: New product title
        :param price: New product price
        """
        ...

    @endpoint.delete("/products/{product_id}")
    def delete_product(self, product_id: int, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Delete a product

        :param product_id: ID of the product to delete
        """
        ...

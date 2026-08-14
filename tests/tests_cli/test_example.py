"""Tests for the `examples/dummyjson` runnable example.

Guards against the example rotting out of sync with `api_client_core.cli`, since `examples/` is
intentionally excluded from the strict `mypy src` gate (see the CLI generator plan for why).
"""

from __future__ import annotations

from common_libs.ansi_colors import remove_color_code
from httpx2 import Client
from pytest_mock import MockerFixture

from api_client_core.cli.builder import build_client_parser
from api_client_core.cli.runner import run
from examples.dummyjson.client import DummyJSONClient

from .conftest import get_subparsers_action, make_httpx_response


class TestDummyJSONExampleParser:
    """Tests that the example client builds the expected command tree"""

    def test_builds_expected_resources(self) -> None:
        """Test that the example client exposes every DummyJSON resource"""
        parser = build_client_parser(DummyJSONClient)
        resources = get_subparsers_action(parser)
        assert set(resources.choices) == {
            "auth",
            "products",
            "users",
            "carts",
            "posts",
            "comments",
            "todos",
            "quotes",
            "recipes",
        }

    def test_products_resource_has_expected_commands(self) -> None:
        """Test that the products resource exposes commands for every HTTP verb the example demonstrates"""
        parser = build_client_parser(DummyJSONClient)
        products_parser = get_subparsers_action(parser).choices["products"]
        commands = get_subparsers_action(products_parser)
        expected_commands = {
            "list-products",
            "get-product",
            "create-product",
            "update-product",
            "patch-product",
            "delete-product",
        }
        assert expected_commands <= set(commands.choices)

    def test_create_cart_products_shows_the_repeatable_json_type(self) -> None:
        """Test that `carts create-cart --products` help shows `json[]`, since it takes a repeatable list
        of JSON objects (`list[dict[str, int]]`)
        """
        parser = build_client_parser(DummyJSONClient)
        carts_parser = get_subparsers_action(parser).choices["carts"]
        create_cart_parser = get_subparsers_action(carts_parser).choices["create-cart"]
        products_action = next(a for a in create_cart_parser._actions if a.dest == "products")
        assert products_action.help is not None
        assert "json[]" in remove_color_code(products_action.help).split()


class TestDummyJSONExampleRun:
    """Tests that the example client dispatches through `api_client_core.cli` against a mocked httpx2"""

    def test_get_product_reaches_the_expected_url(self, mocker: MockerFixture) -> None:
        """Test that `products get-product --product-id 1` issues a GET to /products/1"""
        response = make_httpx_response(mocker, 200, json_body={"id": 1, "title": "t", "price": 9.99})
        mock_request = mocker.patch.object(Client, "request", return_value=response)

        exit_code = run(DummyJSONClient, ["products", "get-product", "--product-id", "1"])

        assert exit_code == 0
        assert mock_request.call_args.args == ("GET", "/products/1")

    def test_list_products_sends_aliased_query_param(self, mocker: MockerFixture) -> None:
        """Test that `products list-products --sort-by price` sends the aliased `sortBy` query param, not `sort_by`"""
        response = make_httpx_response(mocker, 200, json_body={"products": []})
        mock_request = mocker.patch.object(Client, "request", return_value=response)

        run(DummyJSONClient, ["products", "list-products", "--sort-by", "price"])

        assert mock_request.call_args.kwargs.get("params") == {"sortBy": "price"}

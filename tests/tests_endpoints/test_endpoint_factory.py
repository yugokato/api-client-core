"""Unit tests for endpoint_factory.py"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial, wraps
from typing import Any, ParamSpec, TypeVar

import pytest
from common_libs.clients.rest_client import RestResponse

from api_client_core.base import APIBase
from api_client_core.constants import VALID_METHODS
from api_client_core.endpoints import EndpointHandler, endpoint
from api_client_core.endpoints.endpoint_handler import PendingHandler

P = ParamSpec("P")
R = TypeVar("R")


class TestEndpointFactory:
    """Tests for endpoint factory with endpoint.<method>(<path>) decorators"""

    @pytest.mark.parametrize("method", VALID_METHODS)
    def test_endpoint_factory_creates_endpoint_handler(self, method: str) -> None:
        """Test that each HTTP method decorator returns a decorator that creates an EndpointHandler"""
        path = "/v1/something"
        endpoint_factory = getattr(endpoint, method)(path)

        def do_something(self: Any) -> RestResponse: ...

        endpoint_handler = endpoint_factory(do_something)
        assert isinstance(endpoint_handler, EndpointHandler)
        assert endpoint_handler.method == method
        assert endpoint_handler.path == path
        assert endpoint_handler.use_query_string is (method == "get")
        assert endpoint_handler.original_func is do_something

    def test_endpoint_factory_with_use_query_string_opt(self) -> None:
        """Test that use_query_string can be overridden for non-GET methods"""

        @endpoint.post("/v1/something", use_query_string=True)
        def do_something(self: Any) -> RestResponse: ...

        assert do_something.use_query_string is True

    def test_endpoint_factory_with_default_raw_options(self) -> None:
        """Test that raw options are stored in handler's default_raw_options"""

        @endpoint.get("/v1/something", timeout=30, follow_redirects=True)
        def do_something(self: Any) -> RestResponse: ...

        assert do_something.default_raw_options == {"timeout": 30, "follow_redirects": True}

    def test_endpoint_factory_default_raw_options(self) -> None:
        """Test that default_raw_options is empty dict when no raw options are given"""

        @endpoint.get("/v1/something")
        def do_something(self: Any) -> RestResponse: ...

        assert do_something.default_raw_options == {}

    def test_endpoint_factory_with_unsupported_raw_option_raises(self) -> None:
        """Test that a raw option not supported by the HTTP client raises RuntimeError at decoration time"""
        with pytest.raises(RuntimeError, match="Invalid raw option"):

            @endpoint.get("/v1/something", bogus=True)
            def do_something(self: Any) -> RestResponse: ...

    def test_endpoint_factory_with_quiet_raw_option_raises(self) -> None:
        """Test that `quiet` raises RuntimeError at decoration time since it collides with the `quiet` keyword
        `EndpointFunc._call` always passes explicitly
        """
        with pytest.raises(RuntimeError, match="Invalid raw option"):

            @endpoint.get("/v1/something", quiet=True)
            def do_something(self: Any) -> RestResponse: ...


class TestEndpointMetadataDecorators:
    """Tests for endpoint metadata decorators: undocumented, is_public, is_deprecated, content_type"""

    def test_endpoint_is_undocumented(self) -> None:
        """Test that endpoint.undocumented sets is_documented=False on EndpointHandler"""

        @endpoint.undocumented
        @endpoint.get("/v1/hidden")
        def do_something(self: Any) -> RestResponse: ...

        assert do_something.is_documented is False

    def test_endpoint_is_documented_default_is_true(self) -> None:
        """Test that EndpointHandler sets is_documented=True by default"""

        @endpoint.get("/v1/something")
        def do_something(self: Any) -> RestResponse: ...

        assert do_something.is_documented is True

    def test_endpoint_is_undocumented_on_class_level(self) -> None:
        """Test that endpoint.undocumented can be set on the API class level"""

        @endpoint.undocumented
        class TestAPI(APIBase):
            app_name = "test"

        assert TestAPI.is_documented is False

    def test_endpoint_is_public(self) -> None:
        """Test that endpoint.is_public sets is_public=True on EndpointHandler"""

        @endpoint.is_public
        @endpoint.get("/v1/something")
        def do_something(self: Any) -> RestResponse: ...

        assert do_something.is_public is True

    def test_endpoint_is_public_default_is_false(self) -> None:
        """Test that EndpointHandler is_public=False by default"""

        @endpoint.get("/v1/something")
        def do_something(self: Any) -> RestResponse: ...

        assert do_something.is_public is False

    def test_endpoint_is_deprecated(self) -> None:
        """Test that endpoint.is_deprecated sets is_deprecated=True on EndpointHandler"""

        @endpoint.is_deprecated
        @endpoint.get("/v1/something")
        def do_something(self: Any) -> RestResponse: ...

        assert do_something.is_deprecated is True

    def test_endpoint_is_deprecated_default_is_false(self) -> None:
        """Test that EndpointHandler is_deprecated=False by default"""

        @endpoint.get("/v1/something")
        def do_something(self: Any) -> RestResponse: ...

        assert do_something.is_deprecated is False

    def test_endpoint_is_deprecated_on_class_level(self) -> None:
        """Test that endpoint.is_deprecated can be set on the class level"""

        @endpoint.is_deprecated
        class TestAPI(APIBase):
            app_name = "test"

        assert TestAPI.is_deprecated is True

    def test_endpoint_content_type(self) -> None:
        """Test that endpoint.content_type() sets content_type on EndpointHandler"""

        @endpoint.content_type("application/xml")
        @endpoint.post("/v1/something")
        def do_something(self: Any) -> RestResponse: ...

        assert do_something.content_type == "application/xml"

    def test_endpoint_content_type_default_is_none(self) -> None:
        """Test that EndpointHandler content_type=None by default"""

        @endpoint.get("/v1/something")
        def do_something(self: Any) -> RestResponse: ...

        assert do_something.content_type is None


class TestEndpointDecoratorRegistration:
    """Tests endpoint decorator registration with endpoint.decorator()"""

    @pytest.mark.parametrize("with_args", [False, True])
    def test_endpoint_decorator_registration(self, with_args: bool) -> None:
        """Test that endpoint.decorator registers a decorator on EndpointHandler"""

        if with_args:

            @endpoint.decorator
            def decorator_with_args(*deco_args: Any, **deco_kwargs: Any) -> Callable[[Callable[P, R]], Callable[P, R]]:
                def decorator(f: Callable[P, R]) -> Callable[P, R]:
                    @wraps(f)
                    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                        return f(*args, **kwargs)

                    return wrapper

                return decorator

            decorator = decorator_with_args("a", "b", c=123)
        else:

            @endpoint.decorator
            def regular_decorator(f: Callable[P, R]) -> Callable[P, R]:
                @wraps(f)
                def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                    return f(*args, **kwargs)

                return wrapper

            decorator = regular_decorator

        @decorator
        @endpoint.get("/v1/something")
        def do_something(self: Any) -> RestResponse: ...

        assert isinstance(do_something, EndpointHandler)
        assert len(do_something.decorators) == 1
        registered_decorator = do_something.decorators[0]
        if with_args:
            assert isinstance(registered_decorator, partial)
            assert registered_decorator.func is decorator.__wrapped__
        else:
            assert registered_decorator is decorator.__wrapped__

    def test_endpoint_decorator_registration_multi(self) -> None:
        """Test that multiple endpoint.decorator registers all decorators"""

        @endpoint.decorator
        def deco1(f: Callable[P, R]) -> Callable[P, R]:
            @wraps(f)
            def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                return f(*args, **kwargs)

            return wrapper

        @endpoint.decorator
        def deco2(f: Callable[P, R]) -> Callable[P, R]:
            @wraps(f)
            def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                return f(*args, **kwargs)

            return wrapper

        @deco1
        @deco2
        @endpoint.get("/v1/something")
        def do_something(self: Any) -> RestResponse: ...

        assert isinstance(do_something, EndpointHandler)
        assert len(do_something.decorators) == 2
        assert do_something.decorators == [deco2.__wrapped__, deco1.__wrapped__]


class TestEndpointDecoratorSingleCallableFactory:
    """Tests that a registered decorator factory can receive a single bare callable as its only argument"""

    @pytest.mark.parametrize("method_decorator_above", [False, True])
    def test_factory_call_with_single_bare_callable_registers_partial(self, method_decorator_above: bool) -> None:
        """Test that `@my_decorator(callback)` registers `partial(my_decorator, callback)`, regardless of position
        relative to `@endpoint.<method>()`
        """

        @endpoint.decorator
        def deco_with_callable_arg(callback: Callable[[Any], Any]) -> Callable[[Callable[P, R]], Callable[P, R]]:
            def decorator(f: Callable[P, R]) -> Callable[P, R]:
                @wraps(f)
                def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                    return callback(f(*args, **kwargs))

                return wrapper

            return decorator

        def my_callback(x: Any) -> Any:
            return x

        if method_decorator_above:

            @deco_with_callable_arg(my_callback)
            @endpoint.get("/v1/something")
            def do_something(self: Any) -> RestResponse: ...
        else:

            @endpoint.get("/v1/something")
            @deco_with_callable_arg(my_callback)
            def do_something(self: Any) -> RestResponse: ...

        assert isinstance(do_something, EndpointHandler)
        assert len(do_something.decorators) == 1
        registered_decorator = do_something.decorators[0]
        assert isinstance(registered_decorator, partial)
        assert registered_decorator.func is deco_with_callable_arg.__wrapped__
        assert registered_decorator.args == (my_callback,)

    def test_factory_call_with_lambda_as_single_argument(self) -> None:
        """Test that a lambda passed as the single factory argument is registered as a partial"""

        @endpoint.decorator
        def deco_with_callable_arg(callback: Callable[[Any], Any]) -> Callable[[Callable[P, R]], Callable[P, R]]:
            def decorator(f: Callable[P, R]) -> Callable[P, R]:
                @wraps(f)
                def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                    return callback(f(*args, **kwargs))

                return wrapper

            return decorator

        callback = lambda x: x

        @endpoint.get("/v1/something")
        @deco_with_callable_arg(callback)
        def do_something(self: Any) -> RestResponse: ...

        assert isinstance(do_something, EndpointHandler)
        registered_decorator = do_something.decorators[0]
        assert isinstance(registered_decorator, partial)
        assert registered_decorator.args == (callback,)

    @pytest.mark.parametrize(
        "callback",
        [print, partial(print, sep=", "), str.upper],
        ids=["builtin", "functools.partial", "method_descriptor"],
    )
    def test_factory_call_with_non_function_callable_still_registers_partial(
        self, callback: Callable[..., Any]
    ) -> None:
        """Test that a single non-function callable argument (builtin, `functools.partial`, method descriptor)
        still takes the factory-call branch rather than being misread as bare application
        """

        @endpoint.decorator
        def deco_with_callable_arg(callback: Callable[[Any], Any]) -> Callable[[Callable[P, R]], Callable[P, R]]:
            def decorator(f: Callable[P, R]) -> Callable[P, R]:
                @wraps(f)
                def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                    return callback(f(*args, **kwargs))

                return wrapper

            return decorator

        @endpoint.get("/v1/something")
        @deco_with_callable_arg(callback)
        def do_something(self: Any) -> RestResponse: ...

        assert isinstance(do_something, EndpointHandler)
        registered_decorator = do_something.decorators[0]
        assert isinstance(registered_decorator, partial)
        assert registered_decorator.args == (callback,)

    @pytest.mark.parametrize("arg", [42, "x", [1, 2, 3]], ids=["int", "str", "list"])
    def test_factory_call_with_single_non_callable_arg_registers_partial(self, arg: Any) -> None:
        """Test that a factory call with a single non-callable positional argument registers a partial"""

        @endpoint.decorator
        def deco_with_arg(config: Any) -> Callable[[Callable[P, R]], Callable[P, R]]:
            def decorator(f: Callable[P, R]) -> Callable[P, R]:
                @wraps(f)
                def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                    return f(*args, **kwargs)

                return wrapper

            return decorator

        @endpoint.get("/v1/something")
        @deco_with_arg(arg)
        def do_something(self: Any) -> RestResponse: ...

        assert isinstance(do_something, EndpointHandler)
        registered_decorator = do_something.decorators[0]
        assert isinstance(registered_decorator, partial)
        assert registered_decorator.args == (arg,)

    def test_factory_result_reused_across_endpoints(self) -> None:
        """Test that a single-callable factory result can be reused, unmutated, on multiple endpoints"""

        @endpoint.decorator
        def deco_with_callable_arg(callback: Callable[[Any], Any]) -> Callable[[Callable[P, R]], Callable[P, R]]:
            def decorator(f: Callable[P, R]) -> Callable[P, R]:
                @wraps(f)
                def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                    return callback(f(*args, **kwargs))

                return wrapper

            return decorator

        def my_callback(x: Any) -> Any:
            return x

        reusable = deco_with_callable_arg(my_callback)

        @endpoint.get("/v1/a")
        @reusable
        def get_a(self: Any) -> RestResponse: ...

        @endpoint.get("/v1/b")
        @reusable
        def get_b(self: Any) -> RestResponse: ...

        for handler in (get_a, get_b):
            assert isinstance(handler, EndpointHandler)
            assert len(handler.decorators) == 1
            assert isinstance(handler.decorators[0], partial)
            assert handler.decorators[0].args == (my_callback,)

    def test_mixed_stack_with_factory_call_bare_decorator_and_flag(self) -> None:
        """Test a stack mixing an ambiguous factory call, a bare registered decorator, and a flag decorator"""

        @endpoint.decorator
        def deco_with_callable_arg(callback: Callable[[Any], Any]) -> Callable[[Callable[P, R]], Callable[P, R]]:
            def decorator(f: Callable[P, R]) -> Callable[P, R]:
                @wraps(f)
                def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                    return callback(f(*args, **kwargs))

                return wrapper

            return decorator

        @endpoint.decorator
        def bare_deco(f: Callable[P, R]) -> Callable[P, R]:
            @wraps(f)
            def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                return f(*args, **kwargs)

            return wrapper

        def my_callback(x: Any) -> Any:
            return x

        @endpoint.is_public
        @deco_with_callable_arg(my_callback)
        @bare_deco
        @endpoint.get("/v1/something")
        def do_something(self: Any) -> RestResponse: ...

        assert isinstance(do_something, EndpointHandler)
        assert do_something.is_public is True
        assert len(do_something.decorators) == 2
        assert do_something.decorators[0] is bare_deco.__wrapped__
        assert isinstance(do_something.decorators[1], partial)
        assert do_something.decorators[1].args == (my_callback,)

    def test_nested_registered_decorator_call_raises(self) -> None:
        """Test that passing one registered decorator's ambiguous call result to another raises a descriptive
        TypeError instead of silently dropping a decorator
        """

        @endpoint.decorator
        def deco_with_callable_arg(callback: Callable[[Any], Any]) -> Callable[[Callable[P, R]], Callable[P, R]]:
            def decorator(f: Callable[P, R]) -> Callable[P, R]:
                @wraps(f)
                def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                    return callback(f(*args, **kwargs))

                return wrapper

            return decorator

        @endpoint.decorator
        def outer_deco(f: Callable[P, R]) -> Callable[P, R]:
            @wraps(f)
            def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                return f(*args, **kwargs)

            return wrapper

        def my_callback(x: Any) -> Any:
            return x

        with pytest.raises(TypeError, match=r"cannot be nested"):

            @outer_deco(deco_with_callable_arg(my_callback))
            @endpoint.get("/v1/something")
            def do_something(self: Any) -> RestResponse: ...


class TestDecoratorPositionIndependence:
    """Tests for endpoint_factory.py position-independent decorator behavior"""

    def test_method_decorator_above_is_public(self) -> None:
        """Test that endpoint.is_public works when placed below @endpoint.<method>()"""

        @endpoint.post("/v1/something")
        @endpoint.is_public
        def do_something(self: Any) -> RestResponse: ...

        assert isinstance(do_something, EndpointHandler)
        assert do_something.is_public is True

    def test_method_decorator_above_is_deprecated(self) -> None:
        """Test that endpoint.is_deprecated works when placed below @endpoint.<method>()"""

        @endpoint.post("/v1/something")
        @endpoint.is_deprecated
        def do_something(self: Any) -> RestResponse: ...

        assert isinstance(do_something, EndpointHandler)
        assert do_something.is_deprecated is True

    def test_method_decorator_above_undocumented(self) -> None:
        """Test that endpoint.undocumented works when placed below @endpoint.<method>()"""

        @endpoint.get("/v1/something")
        @endpoint.undocumented
        def do_something(self: Any) -> RestResponse: ...

        assert isinstance(do_something, EndpointHandler)
        assert do_something.is_documented is False

    def test_method_decorator_above_content_type(self) -> None:
        """Test that endpoint.content_type() works when placed below @endpoint.<method>()"""

        @endpoint.post("/v1/something")
        @endpoint.content_type("application/xml")
        def do_something(self: Any) -> RestResponse: ...

        assert isinstance(do_something, EndpointHandler)
        assert do_something.content_type == "application/xml"

    def test_method_decorator_outermost_all_flags_below(self) -> None:
        """Test that @endpoint.<method>() works as the outermost decorator with all flags below it"""

        @endpoint.post("/v1/something")
        @endpoint.is_public
        @endpoint.is_deprecated
        @endpoint.content_type("application/xml")
        def do_something(self: Any) -> RestResponse: ...

        assert isinstance(do_something, EndpointHandler)
        assert do_something.is_public is True
        assert do_something.is_deprecated is True
        assert do_something.content_type == "application/xml"

    def test_method_decorator_in_middle(self) -> None:
        """Test that @endpoint.<method>() works in the middle with flags both above and below"""

        @endpoint.is_public
        @endpoint.post("/v1/something")
        @endpoint.is_deprecated
        def do_something(self: Any) -> RestResponse: ...

        assert isinstance(do_something, EndpointHandler)
        assert do_something.is_public is True
        assert do_something.is_deprecated is True

    def test_original_func_preserved_when_factory_outermost(self) -> None:
        """Test that original_func on the EndpointHandler points to the real function when flags are below"""

        @endpoint.post("/v1/something")
        @endpoint.is_public
        def do_something(self: Any) -> RestResponse: ...

        assert isinstance(do_something, EndpointHandler)
        assert do_something.original_func.__name__ == "do_something"

    def test_endpoint_decorator_below_method_decorator(self) -> None:
        """Test that an @endpoint.decorator-wrapped decorator works when placed below @endpoint.<method>()"""

        @endpoint.decorator
        def my_decorator(f: Callable[P, R]) -> Callable[P, R]:
            @wraps(f)
            def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                return f(*args, **kwargs)

            return wrapper

        @endpoint.get("/v1/something")
        @my_decorator
        def do_something(self: Any) -> RestResponse: ...

        assert isinstance(do_something, EndpointHandler)
        assert len(do_something.decorators) == 1
        assert do_something.decorators[0] is my_decorator.__wrapped__

    def test_endpoint_decorator_order_preserved_when_mixed(self) -> None:
        """Test that decorator registration order is the same regardless of position relative to @endpoint.<method>()"""

        @endpoint.decorator
        def deco1(f: Callable[P, R]) -> Callable[P, R]:
            @wraps(f)
            def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                return f(*args, **kwargs)

            return wrapper

        @endpoint.decorator
        def deco2(f: Callable[P, R]) -> Callable[P, R]:
            @wraps(f)
            def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                return f(*args, **kwargs)

            return wrapper

        # All above (baseline)
        @deco1
        @deco2
        @endpoint.get("/v1/something")
        def all_above(self: Any) -> RestResponse: ...

        # All below (method decorator outermost)
        @endpoint.get("/v1/something")
        @deco1
        @deco2
        def all_below(self: Any) -> RestResponse: ...

        # Mixed (method decorator in the middle)
        @deco1
        @endpoint.get("/v1/something")
        @deco2
        def mixed(self: Any) -> RestResponse: ...

        expected_order = [deco2.__wrapped__, deco1.__wrapped__]
        assert all_above.decorators == expected_order
        assert all_below.decorators == expected_order
        assert mixed.decorators == expected_order

    def test_endpoint_decorator_with_args_below_method_decorator(self) -> None:
        """Test that @endpoint.decorator-wrapped decorators with args work when placed below the method decorator"""

        @endpoint.decorator
        def decorator_with_args(*deco_args: Any, **deco_kwargs: Any) -> Callable[[Callable[P, R]], Callable[P, R]]:
            def decorator(f: Callable[P, R]) -> Callable[P, R]:
                @wraps(f)
                def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                    return f(*args, **kwargs)

                return wrapper

            return decorator

        @endpoint.get("/v1/something")
        @decorator_with_args("x", key="val")
        def do_something(self: Any) -> RestResponse: ...

        assert isinstance(do_something, EndpointHandler)
        assert len(do_something.decorators) == 1
        registered = do_something.decorators[0]
        assert isinstance(registered, partial)
        assert registered.func is decorator_with_args.__wrapped__

    def test_missing_method_decorator_produces_pending_handler(self) -> None:
        """Test that applying a flag decorator without @endpoint.<method>() produces a PendingHandler"""

        @endpoint.is_public
        def do_something(self: Any) -> RestResponse: ...

        assert isinstance(do_something, PendingHandler)
        assert not isinstance(do_something, EndpointHandler)

    def test_multiple_flags_without_method_decorator_accumulate_in_pending(self) -> None:
        """Test that multiple flag decorators without @endpoint.<method>() accumulate operations in PendingHandler"""

        @endpoint.is_public
        @endpoint.is_deprecated
        def do_something(self: Any) -> RestResponse: ...

        assert isinstance(do_something, PendingHandler)
        assert len(do_something.deferred_operations) == 2


class TestUnregisteredDecoratorDetection:
    """Tests for fail-fast detection of unregistered endpoint decorators in APIBase.__init_subclass__"""

    @pytest.mark.parametrize("use_wraps", [True, False])
    def test_unregistered_decorator_above_method_decorator_raises(self, use_wraps: bool) -> None:
        """Test that an unregistered decorator with/without functools.wraps() applied above @endpoint.<method>()
        raises
        """

        def regular_decorator(f: Callable[P, R]) -> Callable[P, R]:
            def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                return f(*args, **kwargs)

            return wraps(f)(wrapper) if use_wraps else wrapper

        with pytest.raises(RuntimeError, match=r"@endpoint\.decorator"):

            class BadAPI(APIBase):
                @regular_decorator
                @endpoint.get("/v1/something")
                def do_something(self: Any) -> RestResponse: ...

    @pytest.mark.parametrize("use_wraps", [True, False])
    def test_unregistered_stacked_decorators_raises(self, use_wraps: bool) -> None:
        """Test that multiple regular decorators stacked above @endpoint.<method>() raises"""

        def regular_decorator(f: Callable[P, R]) -> Callable[P, R]:
            def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                return f(*args, **kwargs)

            return wraps(f)(wrapper) if use_wraps else wrapper

        with pytest.raises(RuntimeError, match=r"@endpoint\.decorator"):

            class BadAPI(APIBase):
                @regular_decorator
                @regular_decorator
                @endpoint.get("/v1/something")
                def do_something(self: Any) -> RestResponse: ...

    @pytest.mark.parametrize("num_decorators", [1, 2], ids=["single", "stacked"])
    @pytest.mark.parametrize("use_wraps", [True, False])
    def test_unregistered_decorator_in_middle_of_endpoint_decorators_raises(
        self, use_wraps: bool, num_decorators: int
    ) -> None:
        """Test that regular decorator(s) between @endpoint.* flag decorators and @endpoint.<method>() raises"""

        def regular_decorator(f: Callable[P, R]) -> Callable[P, R]:
            def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                return f(*args, **kwargs)

            return wraps(f)(wrapper) if use_wraps else wrapper

        with pytest.raises(RuntimeError, match=r"@endpoint\.decorator"):
            decos = regular_decorator if num_decorators == 1 else lambda f: regular_decorator(regular_decorator(f))

            class BadAPI(APIBase):
                @endpoint.is_public
                @decos
                @endpoint.get("/v1/something")
                def do_something(self: Any) -> RestResponse: ...

    def test_pending_handler_attribute_raises(self) -> None:
        """Test that an endpoint decorator stack without @endpoint.<method>() still raises at class-definition time"""
        with pytest.raises(RuntimeError, match=r"@endpoint\.<method>\(\)"):

            class BadAPI(APIBase):
                @endpoint.is_public
                def do_something(self: Any) -> RestResponse: ...

    def test_pending_decorator_call_stored_as_class_attribute_raises(self) -> None:
        """Test that storing a configured registered-decorator call as a class attribute raises a message covering
        both possible causes, distinct from the generic PendingHandler message
        """

        @endpoint.decorator
        def deco_with_callable_arg(callback: Callable[[Any], Any]) -> Callable[[Callable[P, R]], Callable[P, R]]:
            def decorator(f: Callable[P, R]) -> Callable[P, R]:
                @wraps(f)
                def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                    return callback(f(*args, **kwargs))

                return wrapper

            return decorator

        def my_callback(x: Any) -> Any:
            return x

        with pytest.raises(RuntimeError, match=r"configured decorator"):

            class BadAPI(APIBase):
                _log = deco_with_callable_arg(my_callback)

                @_log
                @endpoint.get("/v1/something")
                def do_something(self: Any) -> RestResponse: ...

    def test_unregistered_decorator_wrapping_bare_registered_decorator_raises(self) -> None:
        """Test that an unregistered decorator wrapping a bare-applied registered decorator (a PendingDecoratorCall)
        without @endpoint.<method>() still raises, instead of silently passing class definition
        """

        @endpoint.decorator
        def registered_decorator(f: Callable[P, R]) -> Callable[P, R]:
            @wraps(f)
            def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                return f(*args, **kwargs)

            return wrapper

        def regular_decorator(f: Callable[P, R]) -> Callable[P, R]:
            @wraps(f)
            def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                return f(*args, **kwargs)

            return wrapper

        with pytest.raises(RuntimeError, match=r"@endpoint\.decorator"):

            class BadAPI(APIBase):
                @regular_decorator
                @registered_decorator
                def do_something(self: Any) -> RestResponse: ...

    def test_registered_decorator_above_unregistered_decorator_raises(self) -> None:
        """Test that a bare-applied registered decorator (a PendingDecoratorCall) above an unregistered decorator
        that hides @endpoint.<method>() still raises, instead of misreporting a missing method decorator
        """

        @endpoint.decorator
        def registered_decorator(f: Callable[P, R]) -> Callable[P, R]:
            @wraps(f)
            def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                return f(*args, **kwargs)

            return wrapper

        def regular_decorator(f: Callable[P, R]) -> Callable[P, R]:
            @wraps(f)
            def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                return f(*args, **kwargs)

            return wrapper

        with pytest.raises(RuntimeError, match=r"@endpoint\.decorator"):

            class BadAPI(APIBase):
                @registered_decorator
                @regular_decorator
                @endpoint.get("/v1/something")
                def do_something(self: Any) -> RestResponse: ...

    def test_registered_decorator_does_not_raise(self) -> None:
        """Test that a properly @endpoint.decorator-registered decorator does not raise"""

        @endpoint.decorator
        def my_decorator(f: Callable[P, R]) -> Callable[P, R]:
            @wraps(f)
            def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                return f(*args, **kwargs)

            return wrapper

        class GoodAPI(APIBase):
            @my_decorator
            @endpoint.get("/v1/something")
            def do_something(self: Any) -> RestResponse: ...

    def test_regular_decorator_below_method_decorator_does_not_raise(self) -> None:
        """Test that a regular decorator below @endpoint.<method>() does not raise (it wraps the original func body)"""

        def regular_decorator(f: Callable[P, R]) -> Callable[P, R]:
            @wraps(f)
            def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                return f(*args, **kwargs)

            return wrapper

        class GoodAPI(APIBase):
            @endpoint.get("/v1/something")
            @regular_decorator
            def do_something(self: Any) -> RestResponse: ...

    def test_flag_decorators_and_bare_endpoint_do_not_raise(self) -> None:
        """Test that flag decorators and endpoints without custom decorators do not raise"""

        class GoodAPI(APIBase):
            @endpoint.is_public
            @endpoint.get("/v1/something")
            def with_flags(self: Any) -> RestResponse: ...

            @endpoint.get("/v1/something-else")
            def bare(self: Any) -> RestResponse: ...

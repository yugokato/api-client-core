from __future__ import annotations

import inspect
from collections.abc import Callable, Coroutine
from functools import partial, wraps
from typing import TYPE_CHECKING, Any, Concatenate, ParamSpec, TypeAlias, TypeVar, cast, overload

from ..types import RestResponse
from .endpoint_handler import DeferredOperation, EndpointHandler, PendingHandler, PendingOperations

if TYPE_CHECKING:
    from ..base import BaseAPI


T = TypeVar("T", bound="BaseAPI[Any]")
P = ParamSpec("P")
R = TypeVar("R", bound=RestResponse)
_OrigFunc: TypeAlias = Callable[Concatenate[T, P], R | Coroutine[Any, Any, R]]
_HandlerOrPending: TypeAlias = EndpointHandler[P] | PendingOperations[P]
_OrigFuncOrPending: TypeAlias = _OrigFunc[T, P, R] | PendingOperations[P]

__all__ = ["endpoint"]


class endpoint:
    """An endpoint factory that converts a wrapped API class function to an EndpointHandler instance that returns a
    dynamically-created EndpointFunc instance when accessed

    An EndpointFunc instance can be accessed by the following two ways:
    - class-level:    <API Class>.<API class function>
    - instance-level: <API Class instance>.<API class function>

    Example:
        >>> from typing import Unpack
        >>>
        >>> from myproject.clients.my_app.my_app_client import MyAppAPIClient
        >>> from myproject.clients.my_app.api.base.my_app_api import MyAppBaseAPI
        >>> from api_client_core.endpoints.endpoint_func import EndpointFunc
        >>> from api_client_core.types import Kwargs, Unset
        >>>
        >>> class AuthAPI(MyAppBaseAPI):
        >>>     @endpoint.post("/v1/auth/login")
        >>>     def login(
        >>>         self, *, username: str = Unset, password: str = Unset, **kwargs: Unpack[Kwargs]
        >>>     ) -> RestResponse:
        >>>         ...
        >>>
        >>> client = MyAppAPIClient()
        >>> type(client.Auth.login)
        <class 'AuthAPILoginEndpointFunc'>
        >>> type(AuthAPI.login)
        <class 'AuthAPILoginEndpointFunc'>
        >>> from api_client_core import EndpointFunc
        >>> isinstance(client.Auth.login, EndpointFunc) and isinstance(AuthAPI.login, EndpointFunc)
        True
        >>> client.Auth.login.endpoint
        Endpoint(api_class=<class 'myproject.clients.my_app.api.auth.AuthAPI'>, method='post', path='/v1/auth/login', func_name='login', model=<class 'AuthAPILoginEndpointModel'>, url='https://api.my-app.com/v1/auth/login', content_type=None, is_public=False, is_documented=True, is_deprecated=False)
        >>> AuthAPI.login.endpoint
        Endpoint(api_class=<class 'myproject.clients.my_app.api.auth.AuthAPI'>, method='post', path='/v1/auth/login', func_name='login', model=<class 'AuthAPILoginEndpointModel'>, url=None, content_type=None, is_public=False, is_documented=True, is_deprecated=False)
        >>> str(client.Auth.login.endpoint)
        'POST /v1/auth/login'
        >>> str(AuthAPI.login.endpoint)
        'POST /v1/auth/login'
        >>> client.Auth.login.endpoint.path
        '/v1/auth/login'
        >>> client.Auth.login.endpoint.url
        'https://api.my-app.com/v1/auth/login'

    """  # noqa: E501

    @staticmethod
    def get(path: str, **default_raw_options: Any) -> Callable[[_OrigFunc[T, P, R]], EndpointHandler[P]]:
        """Returns a decorator that generates an endpoint handler for a GET API function

        :param path: The endpoint path
        :param default_raw_options: Raw request options passed to the underlying HTTP library
        """
        return endpoint._create("get", path, use_query_string=True, **default_raw_options)

    @staticmethod
    def post(
        path: str, use_query_string: bool = False, **default_raw_options: Any
    ) -> Callable[[_OrigFunc[T, P, R]], EndpointHandler[P]]:
        """Returns a decorator that generates an endpoint handler for a POST API function

        :param path: The endpoint path
        :param use_query_string: Force send all parameters as query strings instead of request body
                                 NOTE: Parameters annotated with Annotated[type, "query"] will always be sent as query
                                       strings regardless of this option
        :param default_raw_options: Raw request options passed to the underlying HTTP library
        """
        return endpoint._create("post", path, use_query_string=use_query_string, **default_raw_options)

    @staticmethod
    def delete(
        path: str, use_query_string: bool = False, **default_raw_options: Any
    ) -> Callable[[_OrigFunc[T, P, R]], EndpointHandler[P]]:
        """Returns a decorator that generates an endpoint handler for a DELETE API function

        :param path: The endpoint path
        :param use_query_string: Force send all parameters as query strings instead of request body
                                 NOTE: Parameters annotated with Annotated[type, "query"] will always be sent as query
                                       strings regardless of this option
        :param default_raw_options: Raw request options passed to the underlying HTTP library
        """
        return endpoint._create("delete", path, use_query_string=use_query_string, **default_raw_options)

    @staticmethod
    def put(
        path: str, use_query_string: bool = False, **default_raw_options: Any
    ) -> Callable[[_OrigFunc[T, P, R]], EndpointHandler[P]]:
        """Returns a decorator that generates an endpoint handler for a PUT API function

        :param path: The endpoint path
        :param use_query_string: Force send all parameters as query strings instead of request body
                                 NOTE: Parameters annotated with Annotated[type, "query"] will always be sent as query
                                       strings regardless of this option
        :param default_raw_options: Raw request options passed to the underlying HTTP library
        """
        return endpoint._create("put", path, use_query_string=use_query_string, **default_raw_options)

    @staticmethod
    def patch(
        path: str, use_query_string: bool = False, **default_raw_options: Any
    ) -> Callable[[_OrigFunc[T, P, R]], EndpointHandler[P]]:
        """Returns a decorator that generates an endpoint handler for a PATCH API function

        :param path: The endpoint path
        :param use_query_string: Force send all parameters as query strings instead of request body
                                 NOTE: Parameters annotated with Annotated[type, "query"] will always be sent as query
                                       strings regardless of this option
        :param default_raw_options: Raw request options passed to the underlying HTTP library
        """
        return endpoint._create("patch", path, use_query_string=use_query_string, **default_raw_options)

    @staticmethod
    def options(
        path: str, use_query_string: bool = False, **default_raw_options: Any
    ) -> Callable[[_OrigFunc[T, P, R]], EndpointHandler[P]]:
        """Returns a decorator that generates an endpoint handler for an OPTIONS API function

        :param path: The endpoint path
        :param use_query_string: Force send all parameters as query strings instead of request body
                                 NOTE: Parameters annotated with Annotated[type, "query"] will always be sent as query
                                       strings regardless of this option
        :param default_raw_options: Raw request options passed to the underlying HTTP library
        """
        return endpoint._create("options", path, use_query_string=use_query_string, **default_raw_options)

    @staticmethod
    def head(
        path: str, use_query_string: bool = False, **default_raw_options: Any
    ) -> Callable[[_OrigFunc[T, P, R]], EndpointHandler[P]]:
        """Returns a decorator that generates an endpoint handler for a HEAD API function

        :param path: The endpoint path
        :param use_query_string: Force send all parameters as query strings instead of request body
                                 NOTE: Parameters annotated with Annotated[type, "query"] will always be sent as query
                                       strings regardless of this option
        :param default_raw_options: Raw request options passed to the underlying HTTP library
        """
        return endpoint._create("head", path, use_query_string=use_query_string, **default_raw_options)

    @staticmethod
    def trace(
        path: str, use_query_string: bool = False, **default_raw_options: Any
    ) -> Callable[[_OrigFunc[T, P, R]], EndpointHandler[P]]:
        """Returns a decorator that generates an endpoint handler for a TRACE API function

        :param path: The endpoint path
        :param use_query_string: Force send all parameters as query strings instead of request body
                                 NOTE: Parameters annotated with Annotated[type, "query"] will always be sent as query
                                       strings regardless of this option
        :param default_raw_options: Raw request options passed to the underlying HTTP library
        """
        return endpoint._create("trace", path, use_query_string=use_query_string, **default_raw_options)

    @overload
    @staticmethod
    def undocumented(obj: type[T]) -> type[T]: ...
    @overload
    @staticmethod
    def undocumented(obj: EndpointHandler[P]) -> EndpointHandler[P]: ...
    @overload
    @staticmethod
    def undocumented(obj: _OrigFuncOrPending[T, P, R]) -> PendingOperations[P]: ...
    @staticmethod
    def undocumented(
        obj: _HandlerOrPending[P] | _OrigFunc[T, P, R] | type[T],
    ) -> _HandlerOrPending[P] | type[T]:
        """Mark an endpoint as undocumented. If an API class is decorated, all endpoints on the class will be
        automatically marked as undocumented.
        The flag value is available with an Endpoint object's is_documented attribute

        :param obj: Endpoint handler, API class, or API function
        """
        from ..base import BaseAPI

        if inspect.isclass(obj) and issubclass(obj, BaseAPI):
            obj.is_documented = False
            return cast(type[T], obj)
        return endpoint._apply_operations(obj, lambda h: setattr(h, "is_documented", False))

    @overload
    @staticmethod
    def is_public(obj: EndpointHandler[P]) -> EndpointHandler[P]: ...
    @overload
    @staticmethod
    def is_public(obj: _OrigFuncOrPending[T, P, R]) -> PendingOperations[P]: ...
    @staticmethod
    def is_public(
        obj: _HandlerOrPending[P] | _OrigFunc[T, P, R],
    ) -> _HandlerOrPending[P]:
        """Mark an endpoint as a public API that does not require authentication.
        The flag value is available with an Endpoint object's is_public attribute

        :param obj: Endpoint handler or API function
        """
        return endpoint._apply_operations(obj, lambda h: setattr(h, "is_public", True))

    @overload
    @staticmethod
    def is_deprecated(obj: type[T]) -> type[T]: ...
    @overload
    @staticmethod
    def is_deprecated(obj: EndpointHandler[P]) -> EndpointHandler[P]: ...
    @overload
    @staticmethod
    def is_deprecated(obj: _OrigFuncOrPending[T, P, R]) -> PendingOperations[P]: ...
    @staticmethod
    def is_deprecated(
        obj: _HandlerOrPending[P] | _OrigFunc[T, P, R] | type[T],
    ) -> _HandlerOrPending[P] | type[T]:
        """Mark an endpoint as a deprecated API. If an API class is decorated, all endpoints on the class will be
        automatically marked as deprecated.

        :param obj: Endpoint handler, API class, or API function
        """
        from ..base import BaseAPI

        if inspect.isclass(obj) and issubclass(obj, BaseAPI):
            obj.is_deprecated = True
            return cast(type[T], obj)
        return endpoint._apply_operations(obj, lambda h: setattr(h, "is_deprecated", True))

    @staticmethod
    def content_type(
        content_type: str,
    ) -> Callable[[_HandlerOrPending[P]], _HandlerOrPending[P]]:
        """Explicitly set Content-Type for this endpoint

        :param content_type: Content type to explicitly set
        """

        def wrapper(obj: _HandlerOrPending[P]) -> _HandlerOrPending[P]:
            return endpoint._apply_operations(obj, lambda h: setattr(h, "content_type", content_type))

        return wrapper

    @staticmethod
    def decorator(f: Callable[..., Any]) -> Callable[..., Any]:
        """Convert a regular decorator to be usable on API functions. This supports both regular decorators and
        decorators with arguments

        Due to the way we encapsulate an API class function, the first argument of a regular decorator applied on our
        API function will be an EndpointHandler object instead of the decorated function. Decorating the decorator with
        this `endpoint.decorator` will make it usable on an API class function

        >>> # The decorator definition
        >>> @endpoint.decorator # This is what you need to register
        >>> def my_decorator(f):
        >>>     @wraps(f)
        >>>     def wrapper(*args, **kwargs):
        >>>         return f(*args, **kwargs)
        >>>     return wrapper

        >>> # Apply the decorator on an API function
        >>> @my_decorator   # This can be also done as @endpoint.decorator(my_decorator) instead
        >>> @endpoint.get("foo/bar")
        >>> def get_foo_bar(self):
        >>>    ...
        """

        @wraps(f)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not kwargs and len(args) == 1:
                if isinstance(args[0], (EndpointHandler, PendingOperations)):
                    return endpoint._apply_operations(args[0], lambda h: h.register_decorator(f))
                if inspect.isfunction(args[0]):
                    # We can't differentiate a bare application on a plain API function and a factory call whose only
                    # argument happens to be a callable at this timing. Resolution is deferred until this is consumed
                    # as a pending operation or called
                    return PendingDecoratorCall(f, args[0])

            # Decorator with arguments: @my_decorator(arg1, arg2, ...)
            @wraps(f)
            def _wrapper(obj: Any) -> Any:
                return endpoint._apply_operations(obj, lambda h: h.register_decorator(partial(f, *args, **kwargs)))

            return _wrapper

        return wrapper

    @staticmethod
    def _create(
        method: str, path: str, use_query_string: bool = False, **default_raw_options: Any
    ) -> Callable[[_OrigFunc[T, P, R]], EndpointHandler[P]]:
        """Returns an endpoint factory that creates an endpoint handler object, which will return an
        EndpointFunc object when accessing the associated API class function
        """

        def endpoint_factory(f: _OrigFuncOrPending[T, P, R]) -> EndpointHandler[P]:
            if isinstance(f, PendingOperations):
                handler: EndpointHandler[P] = EndpointHandler(
                    f.func, method, path, use_query_string=use_query_string, **default_raw_options
                )
                for modifier in f.deferred_operations:
                    modifier(handler)
                return handler
            return EndpointHandler(f, method, path, use_query_string=use_query_string, **default_raw_options)

        return endpoint_factory

    @staticmethod
    def _apply_operations(
        obj: _HandlerOrPending[P] | Callable[P, RestResponse], operation: DeferredOperation[P]
    ) -> Any:
        """Apply an endpoint operation immediately (EndpointHandler) or defer it (function / PendingOperations)

        :param obj: An EndpointHandler, PendingOperations, or a plain API function
        :param operation: An operation to apply to the final EndpointHandler
        """
        if isinstance(obj, EndpointHandler):
            operation(obj)
            return obj
        elif isinstance(obj, PendingOperations):
            obj.deferred_operations.append(operation)
            return obj
        elif inspect.isfunction(obj):
            pending_handler: PendingHandler[P] = PendingHandler(obj)
            pending_handler.deferred_operations.append(operation)
            return pending_handler
        else:
            raise TypeError(
                f"Expected an {EndpointHandler.__name__} or API function, got {type(obj).__name__}. "
                "Ensure @endpoint.<method>() is present in the decorator stack."
            )


class PendingDecoratorCall(PendingOperations[P]):
    """Result of calling an `@endpoint.decorator`-registered decorator with a single bare callable and no keyword
    arguments.

    Such a call is ambiguous: it is either bare decorator application on an API function, or a decorator-factory
    call whose only argument happens to be a callable. The two are indistinguishable at call time, so resolution is
    deferred to the next use:

    - Consumed as a `PendingOperations` object by `@endpoint.<method>()` or another endpoint operation -> it was
      bare application (the decorator registration is pre-seeded in `deferred_operations`)
    - Called with a decoration target -> it was a factory call (registers `partial(decorator, arg)` on the target)

    :param decorator: The registered decorator (the original function wrapped by `endpoint.decorator`)
    :param arg: The single callable the decorator was called with (the API function if bare application, or the
                factory's configuration argument)
    """

    def __init__(self, decorator: Callable[..., Any], arg: Callable[..., Any]) -> None:
        super().__init__(arg)
        self._decorator = decorator
        self.deferred_operations.append(lambda h: h.register_decorator(decorator))

    def __call__(self, obj: Any) -> Any:
        """Resolve this call as a decorator-factory call by registering `partial(decorator, arg)` on `obj`

        :param obj: The `EndpointHandler`, `PendingOperations`, or API function this call is applied to
        """
        # __init__ seeds exactly one deferred operation (the bare-application registration). More than that
        # means an enclosing registered decorator appended to it, i.e. this call result was nested.
        if len(self.deferred_operations) > 1:
            raise TypeError(f"{self._decorator.__name__!r}: decorators cannot be nested. Use a plain callable.")
        return endpoint._apply_operations(obj, lambda h: h.register_decorator(partial(self._decorator, self.func)))

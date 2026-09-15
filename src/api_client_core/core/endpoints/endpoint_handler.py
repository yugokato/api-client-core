from __future__ import annotations

from collections.abc import Callable
from functools import update_wrapper
from threading import RLock
from typing import TYPE_CHECKING, Any, Generic, ParamSpec, TypeAlias, cast
from weakref import WeakKeyDictionary

from ..types import RestResponse
from .endpoint_func import AsyncEndpointFunc, EndpointFunc, SyncEndpointFunc
from .utils import endpoint_call as endpoint_call_util

if TYPE_CHECKING:
    from ..base import BaseAPI


__all__ = ["EndpointHandler"]

P = ParamSpec("P")
EndpointDecorator: TypeAlias = Callable[[Callable[..., Any]], Callable[..., Any]]
DeferredOperation = Callable[["EndpointHandler[P]"], None]


class PendingOperations(Generic[P]):
    """Base for objects that carry a func together with a queue of endpoint operations deferred until the object
    is resolved onto a real EndpointHandler.
    """

    def __init__(self, f: Callable[P, RestResponse]) -> None:
        self.func = f
        self.deferred_operations: list[DeferredOperation[P]] = []


class PendingHandler(PendingOperations[P]):
    """Carries endpoint operations applied before the @endpoint.<method>() factory decorator.

    Enables @endpoint.<method>() to work at any position in the decorator stack, not just immediately
    above the function definition. Once the factory decorator runs, it drains all pending operations onto the
    newly created EndpointHandler.
    """


class EndpointHandler(Generic[P]):
    """A class to encapsulate each API class function (original function) inside a dynamically generated
    EndpointFunc class

    An instance of EndpointHandler class works like a proxy to an EndpointFunc object when an API class function is
    called, which makes the original API function to behave as an EndpointFunc class object instead.
    Each EndpointFunc class will be named in the format of: <APIClassName><APIFunctionName>EndpointFunc

    eg: Accessing <class AuthAPI>.login will return AuthAPILoginEndpointFunc class object
    """

    def __init__(
        self,
        original_func: Callable[..., RestResponse],
        method: str,
        path: str,
        use_query_string: bool = False,
        **default_raw_options: Any,
    ) -> None:
        endpoint_call_util.validate_raw_options(default_raw_options)
        self.original_func = original_func
        self.method = method
        self.path = path
        self.use_query_string = use_query_string
        self.default_raw_options = default_raw_options

        # Will be set via @endpoint.<decorator_name>
        self.content_type: str | None = None  # application/json by default
        self.is_public = False
        self.is_documented = True
        self.is_deprecated = False
        self._lock = RLock()
        self._cache: WeakKeyDictionary[type[BaseAPI[Any]], SyncEndpointFunc[P]] = WeakKeyDictionary()
        self.__decorators: list[EndpointDecorator] = []

    def __get__(
        self, instance: BaseAPI[Any] | None, owner: type[BaseAPI[Any]]
    ) -> SyncEndpointFunc[P] | AsyncEndpointFunc[P]:
        """Return an EndpointFunc object"""
        from ..base.api_class import BaseAPI as BaseAPIClass

        if not (isinstance(owner, type) and issubclass(owner, BaseAPIClass)):
            raise NotImplementedError(f"Unsupported API class: {owner}")

        with self._lock:
            endpoint_func: SyncEndpointFunc[P] | AsyncEndpointFunc[P] | None
            if instance is None:
                # Class-level access: used by BaseAPI.init() to populate endpoints.
                # Cache in a handler-level WeakKeyDictionary keyed by the owner class (always sync).
                endpoint_func = self._cache.get(owner)
                if endpoint_func is None:
                    endpoint_func = cast(SyncEndpointFunc[P], self._build_endpoint_func(None, owner, is_async=False))
                    self._cache[owner] = endpoint_func
            else:
                # Per-instance caching via __dict__ self-replacement: the built EndpointFunc is stored
                # directly on the instance so subsequent attribute access bypasses this descriptor.
                # This ensures the EndpointFunc lifetime is tied to the instance, not to the handler.
                func_name = self.original_func.__name__
                endpoint_func = instance.__dict__.get(func_name)
                if endpoint_func is None:
                    endpoint_func = self._build_endpoint_func(instance, owner, instance.api_client.async_mode)
                    instance.__dict__[func_name] = endpoint_func
            return endpoint_func

    @property
    def decorators(self) -> list[EndpointDecorator]:
        """Returns decorators that should be applied on an endpoint function"""
        return self.__decorators

    def register_decorator(self, *decorators: EndpointDecorator) -> None:
        """Register a decorator that will be applied on an endpoint function"""
        self.__decorators.extend(decorators)

    def _build_endpoint_func(
        self, instance: BaseAPI[Any] | None, owner: type[BaseAPI[Any]], is_async: bool
    ) -> SyncEndpointFunc[P] | AsyncEndpointFunc[P]:
        """Create, wrap, and return a new EndpointFunc for the given instance/owner."""
        endpoint_func_class = EndpointFunc._create(owner, self.original_func, is_async)
        endpoint_func = endpoint_func_class(self, instance, owner)
        update_wrapper(endpoint_func, self.original_func)
        return endpoint_func

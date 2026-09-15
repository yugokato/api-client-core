from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import TYPE_CHECKING, Any, Concatenate, ParamSpec, TypeVar, cast

if TYPE_CHECKING:
    from .call_wrappers import CallWrapperMixin
    from .endpoint_func import EndpointFunc


_P = ParamSpec("_P")
_R = TypeVar("_R")
_F = TypeVar("_F", bound=Callable[..., Any])
# _TCallWrapperMixin is intentionally unparameterized: bound="CallWrapperMixin[Any]" widens the class-scoped P to
# Any in the return type of requires_instance-decorated methods that return Callable[P, R], which breaks the
# propagation of P
_TCallWrapperMixin = TypeVar("_TCallWrapperMixin", bound="CallWrapperMixin")  # type: ignore[type-arg]
# _TEndpointFunc is intentionally unparameterized: requires_sync_def is applied across differently-parameterized
# EndpointFunc subclasses, and bound="EndpointFunc[Any]" would require binding a concrete P here
_TEndpointFunc = TypeVar("_TEndpointFunc", bound="EndpointFunc")  # type: ignore[type-arg]


def requires_instance(
    f: Callable[Concatenate[_TCallWrapperMixin, _P], _R],
) -> Callable[Concatenate[_TCallWrapperMixin, _P], _R]:
    """Raise if the wrapped method is accessed through the API class rather than an instance."""

    @wraps(f)
    def wrapper(self: _TCallWrapperMixin, *args: _P.args, **kwargs: _P.kwargs) -> _R:
        if self._instance is None:
            func_name = self._original_func.__name__ if f.__name__ == "__call__" else f.__name__
            raise TypeError(f"You cannot access {func_name}() directly through the {self._owner.__name__} class.")
        return f(self, *args, **kwargs)

    return wrapper


def requires_sync_def(
    f: Callable[Concatenate[_TEndpointFunc, _P], _R],
) -> Callable[Concatenate[_TEndpointFunc, _P], _R]:
    """Raise if the API method or any request hook is defined with `async def`."""

    @wraps(f)
    def wrapper(self: _TEndpointFunc, *args: _P.args, **kwargs: _P.kwargs) -> _R:
        for name, is_async in (
            (self._original_func.__name__, self._is_async_func),
            (self._owner.pre_request_hook.__name__, self._is_async_pre_request_hook),
            (self._owner.post_request_hook.__name__, self._is_async_post_request_hook),
        ):
            if is_async:
                raise RuntimeError(
                    f"`{self._owner.__name__}.{name}` is defined with `async def` but called by a sync client. "
                    f"Either use an async client (async_mode=True), or define the method with `def`."
                )
        return f(self, *args, **kwargs)

    return wrapper


def terminal(f: _F) -> _F:
    """Mark a `with_xxx()` wrapper method as terminal.

    Sets `._terminal_wrapper` on the returned `EndpointFunc` copy so that `_with_wrapper` can raise if any further
    wrapper is chained after this one.
    """

    @wraps(f)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        wrapped = f(self, *args, **kwargs)
        wrapped._terminal_wrapper = f.__name__
        return wrapped

    return cast(_F, wrapper)

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, ParamSpec, cast

from ...types import RestResponse

if TYPE_CHECKING:
    from ...types import _ResponseStream


_P = ParamSpec("_P")


def _as_response(f: Callable[_P, Awaitable[RestResponse]]) -> Callable[_P, RestResponse]:
    """Retype an async callable as a plain callable returning RestResponse.

    Applied to AsyncEndpointFunc.__call__ so that the SyncEndpointFunc | AsyncEndpointFunc union appears as a single
    non-coroutine callable type to the type checker.
    At runtime this is a no-op.
    """
    return cast(Callable[_P, RestResponse], f)


def _as_response_stream(f: Callable[_P, object]) -> Callable[_P, _ResponseStream]:
    """Retype a context-manager callable as returning the dual _ResponseStream.

    Applied to both SyncEndpointFunc.stream() and AsyncEndpointFunc.stream() so the
    SyncEndpointFunc | AsyncEndpointFunc union presents a single type that supports both `with` and `async with`.
    At runtime this is a no-op.
    """
    return cast(Callable[_P, "_ResponseStream"], f)

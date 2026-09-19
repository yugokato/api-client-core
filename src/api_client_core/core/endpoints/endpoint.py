from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator, Mapping
from dataclasses import MISSING, Field, dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Generic, NamedTuple, ParamSpec

from ..types import EndpointModel, RestResponse, Unset
from .utils import endpoint_call as endpoint_call_util
from .utils import param_type as param_type_util
from .utils.docstring import get_first_doc_line, split_param_docs
from .utils.endpoint_model import resolve_signature_name

if TYPE_CHECKING:
    from ..base.api_class import APIClientT, BaseAPI
    from .endpoint_func import EndpointFunc

P = ParamSpec("P")

__all__ = ["Endpoint", "EndpointIntrospection", "EndpointParam"]


@dataclass(frozen=True, slots=True)
class Endpoint(Generic[P]):
    """An Endpoint class to hold various endpoint data associated to an API class function

    This is accessible via an EndpointFunc object (see docstrings of the `endpoint` class below).

    The original API class function's signature, docstring, and parameters are exposed via `introspection`.
    """

    api_class: type[BaseAPI[Any]]
    method: str
    path: str
    func_name: str
    model: type[EndpointModel]
    url: str | None = None  # Available only for an endpoint object accessed via an API client instance
    content_type: str | None = None
    use_query_string: bool = False
    is_public: bool = False
    is_documented: bool = True
    is_deprecated: bool = False

    def __str__(self) -> str:
        return f"{self.method.upper()} {self.path}"

    def __eq__(self, obj: Any) -> bool:
        return isinstance(obj, Endpoint) and self.api_class is obj.api_class and str(self) == str(obj)

    def __hash__(self) -> int:
        return hash((self.api_class, str(self)))

    def __call__(self, api_client: APIClientT, *args: P.args, **kwargs: P.kwargs) -> RestResponse:
        """Make an API call directly from this endpoint obj to the associated endpoint using the given API client

        NOTE: If the provided API client is in async mode, the returned value is a coroutine that needs be awaited by
        the caller

        Parameters can be passed either positionally or as keyword arguments — same flexible convention as calling
        the endpoint function directly (see EndpointFunc.__call__).

        :param api_client: API client to use for the call
        :param args: Endpoint parameters provided as positional arguments (path and/or body/query parameters)
        :param kwargs: Endpoint parameters provided as keyword arguments (path and/or body/query parameters)

        Example:
            >>> from myproject.clients.my_app.my_app_client import MyAppAPIClient
            >>>
            >>> client = MyAppAPIClient()
            >>> r = client.auth.login(username="foo", password="bar")
            >>> # Above API call can be also done directly from the endpoint object, if you need to:
            >>> endpoint = client.auth.login.endpoint
            >>> r2 = endpoint(client, username="foo", password="bar")
        """
        return self._call(api_client, *args, **kwargs)  # type: ignore[arg-type]

    @property
    def introspection(self) -> EndpointIntrospection:
        """A read-only view onto this endpoint's original function, such as its signature, docstring-derived
        description/summary/param_docs.
        """
        return EndpointIntrospection(self)

    def bind(self, api_client: APIClientT) -> EndpointFunc[P]:
        """Return the endpoint function bound to the given API client.

        :param api_client: API client to bind the endpoint to
        """
        api_class = self.api_class(api_client)
        return getattr(api_class, self.func_name)

    def _call(
        self,
        api_client: APIClientT,
        *args: Any,
        quiet: bool | None = None,
        with_hooks: bool = True,
        raw_options: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> RestResponse:
        """Make an API call directly from this endpoint obj to the associated endpoint (implementation)

        :param api_client: API client to use for the call
        :param args: Endpoint parameters provided as positional arguments (path and/or body/query parameters)
        :param quiet: Suppress request/response logs for this call, reducing a failure to one line. Not given
                      (`None`) defers to the client's own `log_requests` default. An explicit `quiet=False`
                      overrides a `log_requests=False` client for this call
        :param with_hooks: Invoke pre/post request hooks
        :param raw_options: Raw request options passed to the underlying HTTP library
        :param kwargs: Endpoint parameters provided as keyword arguments (path and/or body/query parameters)
        """
        endpoint_func: Any = self.bind(api_client)
        return endpoint_func(*args, quiet=quiet, with_hooks=with_hooks, raw_options=raw_options, **kwargs)


@dataclass(frozen=True, slots=True)
class EndpointIntrospection:
    """A read-only view onto one `Endpoint`'s original function: its signature, docstring-derived
    description/summary/param_docs, and its parameters resolved back to the call they're dispatched by.

    Reached via `Endpoint.introspection`, kept off `Endpoint` itself so its own dataclass fields and
    dispatch methods (`__call__`, `bind()`) aren't crowded by this purely descriptive surface.
    """

    endpoint: Endpoint[Any]

    @property
    def original_func(self) -> Callable[..., Any]:
        """The original API class function this endpoint's parameter model was generated from."""
        return self.endpoint.model.endpoint_func._original_func

    @property
    def signature(self) -> inspect.Signature:
        """The original function's signature, with `self` removed."""
        return endpoint_call_util.get_params_signature(self.original_func)

    @property
    def description(self) -> str:
        """The original function's docstring prose, with any `:param` entries split out."""
        prose, _ = split_param_docs(self.original_func.__doc__)
        return prose

    @property
    def summary(self) -> str | None:
        """The first non-blank line of `description`, or `None` if it has none."""
        return get_first_doc_line(self.description)

    @property
    def param_docs(self) -> Mapping[str, str]:
        """Each parameter's `:param <name>: <description>` entry from the original function's docstring,
        keyed by parameter name.
        """
        _, param_docs = split_param_docs(self.original_func.__doc__)
        return MappingProxyType(param_docs)

    def iter_params(self) -> Iterator[EndpointParam]:
        """Yield each of the endpoint's parameters, resolved from its model back to the call it is
        dispatched by.

        Filters nothing: a caller that needs to skip reserved names, resolve flag collisions, or apply
        any other selection is responsible for its own filtering over the full set yielded here.
        """
        sig = self.signature
        for name, field in self.endpoint.model.__dataclass_fields__.items():
            param_name = resolve_signature_name(name, field.type, sig)
            sig_param = sig.parameters.get(param_name)
            required = sig_param is not None and sig_param.default is inspect.Parameter.empty
            yield EndpointParam(name=param_name, field=field, required=required, endpoint=self.endpoint)


class EndpointParam(NamedTuple):
    """One endpoint parameter, resolved from its model field back to the call it is dispatched by.

    :param name: The original signature parameter name (post-`Alias` resolution)
    :param field: The generated model's dataclass field, carrying the resolved type annotation
    :param required: Whether the original signature declares no default for it
    :param endpoint: The endpoint this parameter belongs to, used to compute `location` on demand
    """

    name: str
    field: Field[Any]
    required: bool
    endpoint: Endpoint[Any]

    @property
    def annotation(self) -> Any:
        """The parameter's resolved type annotation."""
        return self.field.type

    @property
    def default(self) -> Any:
        """The parameter's raw default value, `dataclasses.MISSING` if the field declares none, or `Unset`
        if the original signature default is `Unset`.
        """
        return self.field.default

    @property
    def has_default(self) -> bool:
        """Whether `default` is an actual value, rather than `dataclasses.MISSING` or `Unset`."""
        return self.default not in (Unset, MISSING)

    @property
    def deprecated(self) -> bool:
        """Whether the parameter is annotated with `deprecated` metadata."""
        return param_type_util.is_deprecated_param(self.annotation)

    @property
    def location(self) -> str:
        """Where this parameter's value goes in the request (`path`, `query`, or `body`)."""
        return endpoint_call_util.get_param_location(self.endpoint, self.field)

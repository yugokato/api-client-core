API Client Core — A Framework for Building Python API Clients
=================================================================

[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![test](https://github.com/yugokato/api-client-core/actions/workflows/test.yml/badge.svg)](https://github.com/yugokato/api-client-core/actions/workflows/test.yml)
[![Code style ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://docs.astral.sh/ruff/)

**API Client Core** is a framework for building Python API clients with decorator-based endpoint definitions. The `@endpoint` decorators turn plain class methods into fully managed endpoint functions that automatically build HTTP requests, support both sync and async execution, and provide extensible capabilities such as request hooks, execution wrappers, retries, and call statistics.

The framework uses the `httpx`-based REST client from [common-libs](https://github.com/yugokato/common-libs/tree/main/src/common_libs/clients/rest_client) as the underlying HTTP client.


# Table of Contents

- [Design Goals](#design-goals)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Building an API Client](#building-an-api-client)
- [Core Concepts](#core-concepts)
  - [Endpoint Factory (`endpoint`)](#endpoint-factory-endpoint)
  - [Endpoint Functions (`EndpointFunc`)](#endpoint-functions-endpointfunc)
  - [API Client (`APIClient`)](#api-client-apiclient)
  - [API Class (`BaseAPI`)](#api-class-baseapi)
  - [Endpoint Object (`Endpoint`)](#endpoint-object-endpoint)
  - [API Statistics (`Stats`)](#api-statistics-stats)
  - [Automatic Discovery (`BaseAPI.init()`)](#automatic-discovery-baseapiinit)
- [Sync vs Async](#sync-vs-async)
- [Logging](#logging)
- [Type and Response Reference](#type-and-response-reference)
- [Extending Core](#extending-core)


# Design Goals

- **Decorator-based endpoint definition** — decorate a plain method with `@endpoint.<method>("/path")` and the framework handles the rest.
- **Sync/async dual-mode** from the same source code — one endpoint definition works with both `sync` and `async` clients.
- **Batteries-included** for common needs: automatic retries, distributed locking, concurrent execution, streaming responses, and API call statistics.
- **Extensible** via request/response hooks and decorators.


# Installation

```bash
pip install git+https://github.com/yugokato/api-client-core
```

> [!NOTE]
> This project and its upstream dependency `common-libs` are not currently versioned. To pick up upstream changes into your existing installation, add `--force-reinstall` to install the latest version.


# Quick Start

Define an API endpoint by decorating a class method with `@endpoint.<method>("/path")`:

```python
from api_client_core import BaseAPI, endpoint
from api_client_core.types import RestResponse


class UsersAPI(BaseAPI):
    """User APIs"""
    
    @endpoint.get("/users/{user_id}")
    def get_user(self, user_id: int, include_posts: bool = False) -> RestResponse:
        """Get a user by ID"""
        ...
```

Call it like a regular Python method through your [API client](#building-an-api-client). The framework automatically builds and sends the HTTP request using the provided arguments and returns a `RestResponse`.  
The same endpoint definition works in both `sync` and `async` mode. See [Sync vs Async](#sync-vs-async) for details.

<details open>
<summary><b>Sync</b></summary>

```pycon
>>> client = MyAppAPIClient()
>>> r = client.Users.get_user(user_id=42, include_posts=True)
>>> r.status_code
200
>>> r.response
{'id': 42, 'name': 'Jane Doe', 'email': 'jane@example.com', 'posts': [{'id': 1, 'title': 'Hello World'}, {'id': 2, 'title': 'API Design Notes'}]}
```

</details>

<details>
<summary><b>Async</b></summary>

```pycon
# NOTE: This example uses asyncio REPL (python -m asyncio)
>>> client = MyAppAPIClient(async_mode=True)
>>> r = await client.Users.get_user(user_id=42, include_posts=True)
>>> r.status_code
200
>>> r.response
{'id': 42, 'name': 'Jane Doe', 'email': 'jane@example.com', 'posts': [{'id': 1, 'title': 'Hello World'}, {'id': 2, 'title': 'API Design Notes'}]}
```

</details>


# Building an API Client

This walkthrough builds a minimal API client from scratch by organizing endpoints into API classes, exposing them through an API client, and calling the endpoints.  
The example uses a fictional "my-app" API service at `https://api.example.com`.

### 1. Define each API class and its endpoints

Define one API class for each logical group (e.g. OpenAPI tag) by subclassing `BaseAPI`, and add methods using the `@endpoint.<method>("/path")` endpoint factory decorator. The framework automatically maps function parameters to path parameters, query parameters, or the request body based on the endpoint definition.

<details open>
<summary><code>auth.py</code></summary>

```python
# myproject/clients/my_app/api/auth.py

from typing import Annotated, Unpack

from api_client_core import BaseAPI, endpoint
from api_client_core.types import RestResponse, Kwargs, Query, Unset


class AuthAPI(BaseAPI):
    """Auth APIs"""

    @endpoint.is_public
    @endpoint.post("/auth/login")
    def login(self, username: str, password: str, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Log in"""
        ...

    @endpoint.post("/auth/logout")
    def logout(self, redirect_to: Annotated[str, Query()] = Unset, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Log out"""
        ...

    @endpoint.is_public
    @endpoint.post("/auth/sessions/{session_id}/refresh")
    def refresh_session(
        self, session_id: str, refresh_token: str, expires_in: int = 3600, scopes: list[str] = Unset
    ) -> RestResponse:
        """Refresh an existing session"""
        ...
```

</details>

<details>
<summary><code>users.py</code></summary>

```python
# myproject/clients/my_app/api/users.py

from typing import Unpack

from api_client_core import BaseAPI, endpoint
from api_client_core.types import RestResponse, Kwargs, Unset


class UsersAPI(BaseAPI):
    """User APIs"""

    @endpoint.post("/users")
    def create_user(self, username: str, email: str, role: str = Unset, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Create a user"""
        ...

    @endpoint.get("/users/{user_id}")
    def get_user(self, user_id: int, include_posts: bool = Unset, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """Get a user by ID"""
        ...

    @endpoint.get("/users")
    def list_users(self, page: int = Unset, page_size: int = Unset, **kwargs: Unpack[Kwargs]) -> RestResponse:
        """List users"""
        ...
```

</details>

> [!NOTE]
>- In most cases, the function body should be empty (`...`, `pass`, etc.). The framework automatically handles the HTTP request using the provided parameters.
> - Use the `Unset` sentinel (not `None`) as the default when a parameter should be omitted unless explicitly set. See [`Unset` and default values](#unset-and-default-values).
> - `**kwargs` takes framework-level request control options and raw `httpx` options.

> [!TIP]
> - Consider creating an app-level base class instead of subclassing `BaseAPI` directly. See [API Class (`BaseAPI`)](#api-class-baseapi) for details.
> - If your use case is async-only, you can define the endpoint with `async def` instead of `def`. See [Sync vs Async](#sync-vs-async) for details.

### 2. Define the API client

Define the API client for your application by subclassing `APIClient`, and expose the API classes you created via `@cached_property` so they are created lazily and reused for the lifetime of the client.

```python
# myproject/clients/my_app/my_app_client.py

from functools import cached_property
from typing import Any

from api_client_core import APIClient

from .api.auth import AuthAPI
from .api.users import UsersAPI


class MyAppAPIClient(APIClient):
    """API client for the my-app service"""

    def __init__(self, *, base_url: str = "https://api.example.com", async_mode: bool = False, **kwargs: Any) -> None:
        super().__init__("my-app", base_url=base_url, async_mode=async_mode, **kwargs)

    @cached_property
    def Auth(self) -> AuthAPI:
        return AuthAPI(self)

    @cached_property
    def Users(self) -> UsersAPI:
        return UsersAPI(self)
```

That's it. At this point, your API client is ready to use.


### Using the client

```pycon
>>> # Instantiate your client
>>> from myproject.clients.my_app.my_app_client import MyAppAPIClient
>>> client = MyAppAPIClient()
>>> # Make an API call
>>> r = client.Auth.login(username="foo", password="bar")
2024-01-01T00:00:00.100-0800 - request: POST https://api.example.com/auth/login
2024-01-01T00:00:00.115-0800 - response: 200 (OK)
- request_id: a2b20acf-22d5-4131-ac0d-6796bf19d2af
- request: POST https://api.example.com/auth/login
- payload: {"username": "foo", "password": "***"}
- status_code: 200 (OK)
- response: {
    "token": "eyJ1c2VySWQiOjQyLCJyb2xlIjoiYWRtaW4ifQ.d8f3Kx91LmQa7P2v",
    "refresh_token": "rft_91LmQa7P2vXk82",
    "token_type": "Bearer",
    "expires_in": 3600
}
>>> r.status_code
200
>>> r.response
{'token': 'eyJ1c2VySWQiOjQyLCJyb2xlIjoiYWRtaW4ifQ.d8f3Kx91LmQa7P2v', 'refresh_token': 'rft_91LmQa7P2vXk82', 'token_type': 'Bearer', 'expires_in': 3600}
```

> [!NOTE]
> The request/response logging shown above is disabled by default. See [Logging](#logging) for how to enable it.

> [!TIP]
> The recommended way to use a client is as a context manager, which ensures HTTP connections are cleaned up on exit:
> ```python
> # sync
> with MyAppAPIClient() as client:
>     r = client.Auth.login(username="foo", password="bar")
>
> # async
> async with MyAppAPIClient(async_mode=True) as client:
>     r = await client.Auth.logout()
> ```


# Core Concepts

## Endpoint Factory (`endpoint`)

The `endpoint` class is a decorator factory providing decorators that convert a plain API class method into a fully managed endpoint function (`EndpointFunc` instance) at runtime.

### HTTP method decorators

`endpoint` provides one decorator for each HTTP method (`get`, `post`, `put`, `patch`, `delete`, `options`, `head`, `trace`), each binding the method to that HTTP method and the given path:

```python
@endpoint.get("/users/{user_id}")
def get_user(self, user_id: int) -> RestResponse:
    ...
```

- `@endpoint.get(path)` always sends parameters as a query string.
- All other verbs send parameters as the request body by default. Pass `use_query_string=True` to route every parameter to the query string, or annotate individual params with [`Query`](#query) to target specific ones.

All HTTP-method decorators also accept `**default_raw_options`, forwarded to the underlying HTTP library (`httpx`) for every call to that endpoint (e.g., `timeout=30`).

### Metadata decorators

| Decorator                       | Applies to           | Description                                                               |
|---------------------------------|----------------------|---------------------------------------------------------------------------|
| `@endpoint.is_public`           | function             | Marks the endpoint as not requiring authentication (`is_public=True`).    |
| `@endpoint.is_deprecated`       | function or class    | Marks the endpoint (or all endpoints on a class) as deprecated.           |
| `@endpoint.undocumented`        | function or class    | Marks the endpoint as not part of the documented public API.              |
| `@endpoint.content_type("...")` | function             | Explicitly sets the `Content-Type` header for this endpoint.              |
| `@endpoint.decorator`           | decorator definition | Registers a user-written decorator so it can be applied to API functions. |

### Stacking decorators

`@endpoint.<method>("/path")` can appear anywhere in the decorator stack. The framework resolves them in the right order at class definition time:

```python
@my_decorator   # Your custom decorator — must be registered with @endpoint.decorator
@endpoint.is_deprecated
@endpoint.get("/v1/items")
def list_items(self, *, page: int = Unset, page_size: int = Unset, **kwargs: Unpack[Kwargs]) -> RestResponse:
    """List items (deprecated)"""
    ...
```

## Endpoint Functions (`EndpointFunc`)

`EndpointFunc` is the heart of the framework. When you decorate a method with the `@endpoint.<method>("/path")` endpoint factory decorator, two things happen:

1. **At class definition time** — the decorator replaces the method on the class with an `EndpointHandler` descriptor.
2. **At runtime access** — the `EndpointHandler` descriptor returns a dynamically created (and cached) `EndpointFunc` instance, making the method a fully managed endpoint function.

```pycon
# instance-level access
>>> client.Auth.login
<AuthAPILoginEndpointFunc object at 0x10f5abcd0>
  endpoint: POST /auth/login
  mapped to: <function AuthAPI.login at 0x10f4d1360>

# class-level access
>>> AuthAPI.login
<AuthAPILoginEndpointFunc object at 0x10f3c2ab0>
  endpoint: POST /auth/login
  mapped to: <function AuthAPI.login at 0x10f4d1360>
```

### Calling an endpoint function

Call an endpoint function just like a regular method to make an API request. The framework generates the request payload, performs the HTTP request, and returns the response as a [`RestResponse`](#restresponse) object:

```python
# sync
r = client.Auth.login(username="foo", password="bar")

# async
r = await client.Auth.login(username="foo", password="bar")
```

Beyond the endpoint's own parameters, the function also accepts framework-level control options and `httpx` raw options as `**kwargs`. See [`Kwargs`](#kwargs-and-unpack).

### Function parameter signatures

The framework classifies each parameter by name, not by position in the signature:

- **Path parameters** — any parameter whose name matches a `{placeholder}` token in the endpoint path. The framework substitutes it into the URL.
- **Body/query parameters** — every other parameter.

Both kinds can be defined as required (no default) or optional (with a default value). They may appear anywhere in the function signature.

```python
@endpoint.get('/v1/users/{user_id}/orders/{order_id}')
def get_order(self, user_id: int, order_id: int, include_items: bool = Unset, **kwargs: Unpack[Kwargs]) -> RestResponse:
    #               ^^^^^^^^^^^^^^^^^^^^^^^^^^^  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    #               path params (name matches    body/query param (any other name)
    #               placeholder in path)
    ...
```

For path placeholders that are not valid Python identifiers (e.g. `{order-id}`), name the parameter using underscores instead of hyphens — the framework maps them back to the original placeholder:

```python
@endpoint.get("/v1/users/{user_id}/orders/{order-id}")
def get_order(self, user_id: int, order_id: int, **kwargs: Unpack[Kwargs]) -> RestResponse:
    #                             order_id  ↑ matches {order-id}
    ...
```

### `Unset` and default values

`Unset` is a sentinel default value for optional parameters. A parameter whose value is `Unset` is **excluded from the request entirely**, unlike `None`, which is still sent to the server — as `null` in the request body, or as an empty value in the query string.

```python
r = client.Auth.logout()                              # query string: N/A
r = client.Auth.logout(redirect_to=None)              # query string: ?redirect_to=
r = client.Auth.logout(redirect_to="/dashboard")      # query string: ?redirect_to=/dashboard
```

Default values other than `Unset` are always included in the request when the caller omits the argument. Use `Unset` when a parameter should be absent unless explicitly provided:

```python
# page always defaults to 1 if not given. per_page is omitted unless the caller sets it
@endpoint.get("/v1/items")
def list_items(self, *, page: int = 1, per_page: int = Unset, **kwargs: Unpack[Kwargs]) -> RestResponse: ...

r = client.Items.list_items()                     # sent: {"page": 1} 
r = client.Items.list_items(page=2)               # sent: {"page": 2} 
r = client.Items.list_items(per_page=50)          # sent: {"page": 1, "per_page": 50} 
r = client.Items.list_items(page=2, per_page=50)  # sent: {"page": 2, "per_page": 50}
```

### Streaming

Use `stream()` when you need a streaming response. It executes the same request hooks before and after the request as a regular call and honors the class-level `stream_wrapper` (the streaming counterpart of `request_wrapper`).

```python
# sync
with client.Events.subscribe.stream(topic="updates") as r:
    for chunk in r.stream():
        print(chunk)

# async
async with client.Events.subscribe.stream(topic="updates") as r:
    async for chunk in r.astream():
        print(chunk)
```

### Configurable execution wrappers

In addition to `__call__`, every endpoint function also provides the following execution wrappers:

| Method                                                                            | Returns    | Description                                                                                                                                                                                                                                     |
|-----------------------------------------------------------------------------------|------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `with_retry(condition, *, num_retries=1, retry_after=5, safe_methods_only=False)` | `Self`     | Retries when `condition` is met. `condition` defaults to retrying any non-OK response, and also accepts status code(s), exception class(es), or a callable. `retry_after`: seconds, a callable, or a `BackoffStrategy` for exponential backoff. |
| `with_lock(lock_name=None)`                                                       | `Self`     | Executes the call under a distributed lock.                                                                                                                                                                                                     |
| `with_expected_status(*status_codes)`                                             | `Self`     | Asserts that the response status is one of the given codes.                                                                                                                                                                                     |
| `with_max_response_time(threshold_msecs)`                                         | `Self`     | Asserts that the response time does not exceed the threshold.                                                                                                                                                                                  |
| `with_polling(until, *, interval=5, timeout=60)`                                  | `Self`     | Polls until `until(response)` is `True`, raises `TimeoutError` otherwise.                                                                                                                                                                       |
| `with_stats()`                                                                    | `Self`     | Prints a scoped stats report after the call. See [API Statistics](#api-statistics-stats).                                                                                                                                                       |
| `with_concurrency(num=2, *, max_connections=None, return_exceptions=False)`       | `Callable` | Executes `num` concurrent calls, returning `list[RestResponse]`.                                                                                                                                                                                |
| `with_repeat(num=2, *, return_exceptions=False)`                                  | `Callable` | Executes `num` sequential calls, returning `list[RestResponse]`.                                                                                                                                                                                |
| `with_pagination(get_next, *, limit=None)`                                        | `Callable` | Iterates paginated responses. `get_next(response)` returns the next page's params, or `None` to stop.                                                                                                                                           |

> [!IMPORTANT]
> Each wrapper is **curried**: it takes only its own config and returns a configured callable. Call the returned object with the endpoint parameters. Wrappers returning `Self` can be **chained**. Those returning `Callable` are terminal and must be called last.

**Examples:**

With automatic retries:

```python
r = client.Auth.login.with_retry(condition=429, num_retries=3, retry_after=2)(username="foo", password="bar")
```

Chaining wrappers:

```python
# Apply a lock, retry on transient failures, and validate the status code
r = client.Auth.login.with_lock().with_retry(condition=429).with_expected_status(200)(username="foo", password="bar")
```

> [!TIP]
> Wrappers compose left-to-right — the first wrapper applied becomes the outermost layer, so the example above is conceptually equivalent to:
> 
> ```python
> with lock():
>     with retry(condition=429):
>         r = client.Auth.login(username="foo", password="bar")
>         assert r.status_code == 200
> ```


## API Client (`APIClient`)

`APIClient` is the base class for all API clients. It owns the HTTP transport and determines whether endpoint calls execute synchronously or asynchronously.

```python
class APIClient:
    def __init__(
        self,
        app_name: str,
        /,
        *,
        env: str | None = None,
        base_url: str | None = None,
        rest_client: RestClient | AsyncRestClient | None = None,
        async_mode: bool = False,
        raise_on_error: bool = False,
        **kwargs: Any,
    ) -> None: ...
```

| Parameter        | Description                                                                                                                                                          |
|------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `app_name`       | Logical name for the application. Must match `app_name` on any associated API class that sets one.                                                                   |
| `env`            | Optional target environment label (e.g., `"dev"`, `"prod"`). Accessible on API class instances via `self.env`.                                                      |
| `base_url`       | Base URL prepended to every endpoint path. Mutually exclusive with `rest_client`.                                                                                    |
| `rest_client`    | Pre-configured `RestClient` or `AsyncRestClient` to inject. Use this when you need full control over transport-level settings (TLS, proxies, session cookies, etc.). |
| `async_mode`     | Set to `True` to enable async mode. All endpoint calls must then be awaited.                                                                                         |
| `raise_on_error` | Set to `True` to raise an exception on any non-2xx response.                                                                                                         |
| `**kwargs`       | Additional keyword arguments forwarded to the underlying REST client constructor (e.g., `retry_policy`, `headers`, `timeout`, `verify`).                              |


## API Class (`BaseAPI`)

`BaseAPI` is the abstract base class for all API classes:

```python
class AuthAPI(BaseAPI):

    @endpoint.post("/auth/login")
    def login(self, username: str, password: str, **kwargs: Unpack[Kwargs]) -> RestResponse:
        ...
```

Create a dedicated app-level base class by subclassing `BaseAPI` if your project contains multiple API clients. This gives each client its own base class where you can define client-specific configuration and behavior, such as `app_name`, request hooks, and other extension points.


```python
# App-level base — one per application
class MyAppBaseAPI(BaseAPI):
    app_name = "my-app"   # must match api_client.app_name


# Concrete API classes then inherit from the app-level base instead of BaseAPI directly
class AuthAPI(MyAppBaseAPI):

    @endpoint.post("/auth/login")
    def login(self, username: str, password: str, **kwargs: Unpack[Kwargs]) -> RestResponse:
        ...
```

### Class attributes

| Attribute       | Type                     | Description                                                                 |
|-----------------|--------------------------|-----------------------------------------------------------------------------|
| `app_name`      | `str \| None`            | Optional. If set, must match `api_client.app_name`                          |
| `is_documented` | `bool`                   | Marks every endpoint in the class as documented (default `True`).           |
| `is_deprecated` | `bool`                   | Marks every endpoint in the class as deprecated (default `False`).          |
| `endpoints`     | `list[Endpoint] \| None` | Populated by `BaseAPI.init()`. Lists all `Endpoint` objects for this class. |

The class-level `is_documented`/`is_deprecated` flags can also be controlled per-endpoint via the `endpoint` factory decorators (see [Endpoint Factory](#endpoint-factory-endpoint) above).

### Request hooks

Override these methods on your API class to customize request and response behavior. To share the same hooks across multiple API classes, define them once on a dedicated app-level base class instead (see [API Class (`BaseAPI`)](#api-class-baseapi) above).

#### `pre_request_hook`

Called immediately before each request is made.

```python
def pre_request_hook(self, endpoint: Endpoint, *path_params: Any, **params: Any) -> None: ...
```

#### `post_request_hook`

Called immediately after each request completes (or raises an HTTP error).

```python
def post_request_hook(
    self,
    endpoint: Endpoint,
    response: RestResponse | None,
    exception: HTTPError | None,
    *path_params: Any,
    **params: Any,
) -> None: ...
```

> [!NOTE]
> If your use case is async-only, `pre_request_hook` and `post_request_hook` may also be defined with `async def` instead of `def`. See [Sync vs Async](#sync-vs-async) for details.

#### `request_wrapper`

Returns a list of callables that each wrap `EndpointFunc.__call__`. Each callable receives the `EndpointFunc` instance as its first positional argument. Useful for behavior that must see both the call and its result at the class level (e.g., activating a validation mode, adding timing).

```python
def request_wrapper(self) -> list[Callable[..., Any]]:
    return [my_wrapper]
```

> [!NOTE]
> If multiple wrappers are returned, they are applied in reverse order — the first element ends up as the outermost wrapper (processed first).

#### `stream_wrapper`

Analogous to `request_wrapper` but applied to `stream()` calls.

#### Execution order

When both decorators and hooks are configured, the full request lifecycle runs in this order:

1. Endpoint decorators applied with `@endpoint.decorator` (before-call)
2. `request_wrapper` callable (before-call)
3. `pre_request_hook`
4. Request (HTTP request execution)
5. `post_request_hook`
6. `request_wrapper` callable (after-call)
7. Endpoint decorators (after-call)

**Example — Automatically attach/detach a token after a successful login/logout:**

```python
from collections.abc import Callable
from typing import Any

from httpx import HTTPError

from api_client_core import BaseAPI, Endpoint
from api_client_core.types import RestResponse


class MyAppBaseAPI(BaseAPI):
    app_name = "my-app"

    def post_request_hook(
        self,
        endpoint: Endpoint,
        response: RestResponse | None,
        exception: HTTPError | None,
        *path_params: Any,
        **params: Any,
    ) -> None:
        if response and response.ok:
            if endpoint == self.api_client.Auth.login.endpoint:
                self.api_client.rest_client.set_bearer_token(response.response["token"])
            elif endpoint == self.api_client.Auth.logout.endpoint:
                self.api_client.rest_client.unset_bearer_token()
```


## Endpoint Object (`Endpoint`)

`Endpoint` is a frozen `dataclass` holding all metadata for a single endpoint. It is exposed on every endpoint function as `.endpoint` and to each API class via its `.endpoints` list.

| Field           | Type                  | Description                                                                    |
|-----------------|-----------------------|--------------------------------------------------------------------------------|
| `api_class`     | `type[BaseAPI]`       | The API class that owns this endpoint.                                         |
| `method`        | `str`                 | HTTP method in lowercase (e.g., `"get"`, `"post"`).                            |
| `path`          | `str`                 | Endpoint path (e.g., `"/auth/login"`).                                         |
| `func_name`     | `str`                 | Name of the original API class function.                                       |
| `model`         | `type[EndpointModel]` | Dynamically generated `dataclass` model describing this endpoint's parameters. |
| `url`           | `str \| None`         | Full URL. Only set when accessed via a client instance (not via the class).    |
| `content_type`  | `str \| None`         | Explicitly set Content-Type, or `None` to auto-detect.                         |
| `is_public`     | `bool`                | `True` if the endpoint does not require authentication.                        |
| `is_documented` | `bool`                | `True` by default. `False` if the endpoint is marked `@endpoint.undocumented`. |
| `is_deprecated` | `bool`                | `True` if the endpoint was marked `@endpoint.is_deprecated`.                   |

`str(endpoint)` returns `"METHOD /path"` (e.g., `"POST /auth/login"`).

```pycon
>>> print(client.Auth.login.endpoint)
POST /auth/login
>>> pprint(client.Auth.login.endpoint)
Endpoint(api_class=<class 'myproject.clients.my_app.api.auth.AuthAPI'>,
         method='post',
         path='/auth/login',
         func_name='login',
         model=<class 'AuthAPILoginEndpointModel'>,
         url='https://api.example.com/auth/login',
         content_type=None,
         is_public=True,
         is_documented=True,
         is_deprecated=False)
```

The `Endpoint` object is also callable. This lets you dispatch a request directly from an endpoint object, if needed:

```pycon
>>> endpoint = client.Auth.login.endpoint
>>> r = endpoint(client, username="foo", password="bar")   # equivalent to client.Auth.login(username="foo", password="bar")
```

### `EndpointModel`

Each `Endpoint` object exposes a `model` attribute containing a dynamically generated frozen `dataclass` that describes the endpoint's parameters.

```pycon
>>> model = client.Auth.login.endpoint.model
>>> print(model)
<class 'AuthAPILoginEndpointModel'>
>>> pprint(model.__dataclass_fields__, sort_dicts=False)
{'username': Field(name='username',type=<class 'str'>,default=Unset,default_factory=<dataclasses._MISSING_TYPE object at 0x1049bc440>,init=True,repr=True,hash=None,compare=True,metadata=mappingproxy({}),kw_only=True,doc=None,_field_type=_FIELD),
 'password': Field(name='password',type=<class 'str'>,default=Unset,default_factory=<dataclasses._MISSING_TYPE object at 0x1049bc440>,init=True,repr=True,hash=None,compare=True,metadata=mappingproxy({}),kw_only=True,doc=None,_field_type=_FIELD)}
```


## API Statistics (`Stats`)

The framework automatically records per-endpoint metrics including call counts, status-code distributions (`1xx`–`5xx`), errors, response times (`min` / `avg` / `max`), and estimated latency percentiles (`p50` / `p95` / `p99`) using DDSketch (≤1% relative error). Calls made via `stream()` are not included.

### View statistics

Call `Stats.show()` to display a formatted summary of recorded endpoint activity:

```pycon
>>> from api_client_core.endpoints import Stats
>>> client.Auth.login.with_concurrency(num=10)(username="foo", password="bar")
>>> client.Users.get_user(user_id=42)
>>> Stats.show()
                                                                                   Latency (ms)             
                                                                    ----------------------------------------
Endpoint             | Calls | 1xx | 2xx | 3xx | 4xx | 5xx | Error | min  | avg  | max  | p50  | p95  | p99 
---------------------+-------+-----+-----+-----+-----+-----+-------+------+------+------+------+------+-----
POST /auth/login     |    10 |   0 |  10 |   0 |   0 |   0 |     0 | 3.21 | 4.80 | 6.57 | 4.30 | 6.54 | 6.54
GET /users/{user_id} |     1 |   0 |   1 |   0 |   0 |   0 |     0 | 0.68 | 0.68 | 0.68 | 0.68 | 0.68 | 0.68
```

Pass `sort_by` to sort results by `"calls"` (default), `"slowest"`, `"errors"`, or `"endpoint"`. Pass `reverse=False` to sort ascending instead of descending.

### Programmatic access

Use `Stats.get()` to retrieve a single endpoint's stat record, or `Stats.all()` to get a snapshot list of all recorded stats. Both return independent snapshots, so reading them concurrently with ongoing calls is safe.

```python
stat = Stats.get("POST /auth/login")
assert stat.num_2xx == 10
```

`Stats.dump(path)` serializes the global collector to an indented JSON file (complementing `aggregate()`, which file-locks and merges rather than overwrites):

```python
Stats.dump("run_stats.json")
```

### Scoped collection

Use `Stats.collect()` context manager to measure metrics inside a specific block of code. Calls made within the block count toward **both** the yielded scoped collector and the global total:

```python
with Stats.collect("login-flow") as stats:
    r = client.Auth.login(username="foo", password="bar")

stats.show()  # only the calls inside the `with` block
Stats.show()  # all calls ever made
```

Scopes can be nested: an inner `collect()` block sees only its own calls, while the outer scope accumulates both.

For a one-off scoped report on a single endpoint, the `with_stats()` wrapper is a shortcut for the above: it opens a scoped collector around the call and prints the report (filtered to that endpoint) once the call completes:

```python
r = client.Auth.login.with_stats().with_concurrency(num=10)(username="foo", password="bar")
```

### Cross-process aggregation

`Stats.aggregate(path)` merges the current process's snapshot into a shared JSON file using a file lock, making it safe for parallel workers to accumulate into one place.

### Reset statistics

Call `Stats.reset()` to clear all recorded stats.

### Collection control

Set `API_CLIENT_STATS_DISABLED` to `1`, `true`, or `yes` (case-insensitive) before import to disable collection process-wide, or call `Stats.disable()` at runtime. Call `Stats.enable()` to re-enable it. Existing data is retained in both cases. Call `Stats.reset()` to clear it.


## Automatic Discovery (`BaseAPI.init()`)

Call `<YourBaseAPIClass>.init()` from the `__init__.py` of your API class directory. It scans all `.py` files in that directory, discovers every subclass of the specified base class, and populates each class's `.endpoints` list. `<YourBaseAPIClass>` is `BaseAPI` itself if your concrete API classes subclass it directly, or your app-level base class if you're using that pattern.

```python
# myproject/clients/my_app/api/__init__.py

from api_client_core import BaseAPI

API_CLASSES = BaseAPI.init()
```

After this runs, `API_CLASSES` is a `list[type[BaseAPI]]` — one entry per discovered API class:

```pycon
>>> from myproject.clients.my_app.api import API_CLASSES
>>> for cls in API_CLASSES:
...     for ep in cls.endpoints:
...         # ep is an Endpoint object
...         print(ep)
...
POST /auth/login
POST /auth/logout
POST /auth/sessions/{session_id}/refresh
POST /users
GET /users/{user_id}
GET /users
```

> [!NOTE]
> `BaseAPI.init()` must be called from an `__init__.py` file. Calling it from any other module raises a `RuntimeError`.


# Sync vs Async

The framework supports both `sync` and `async` execution **from the same endpoint definition**. The execution mode is determined by how your API client is instantiated.

| Mode           | Constructor                       | Calling an endpoint            |
|----------------|-----------------------------------|--------------------------------|
| Sync (default) | `MyAppAPIClient()`                | Returns a `RestResponse`       |
| Async          | `MyAppAPIClient(async_mode=True)` | Returns a coroutine to `await` |

## Choosing between `def` and `async def`

Endpoint functions and request hooks may be defined with either `def` or `async def`. The choice determines which client modes the definition supports.

### `def` (dual-mode)

A regular `def` works with **both** `sync` and `async` clients. The same endpoint or hook definition can be called from either mode without modification.  
This is recommended when the endpoint function body is empty (the common case), or when your custom function logic or hook is entirely synchronous.

### `async def` (async-only)

An `async def` works **only** with an async client (`async_mode=True`). Use it when your custom function logic or request hook needs to await other coroutines (for example, making additional async requests or performing other asynchronous work).  
Calling an `async def` endpoint or hook from a sync client raises a `RuntimeError`.

Here is a quick summary:

| Definition  | Sync client | Async client | Can `await` inside body? |
|-------------|-------------|--------------|--------------------------|
| `def`       | ✅           | ✅         | ❌                        |
| `async def` | ❌           | ✅         | ✅                        |

The framework does not favor one style over the other. Choose the one that matches your application's requirements.

> [!NOTE]
> With an async client, a `def` endpoint body or request hook runs directly on the event loop, not offloaded to a thread. If it performs blocking I/O (such as `time.sleep()`, synchronous HTTP requests, or blocking disk/database access), it will block the event loop and stall other concurrent tasks (for example, `with_concurrency()` or `asyncio.gather()`). If the body or hook needs to perform I/O under an async client, define it with `async def` and await asynchronous operations instead.


# Logging

The framework is silent by default and does not configure logging automatically. To enable logging, call `setup_logging()` once during your application's startup:

```python
import api_client_core

api_client_core.setup_logging()
```

This installs the default logging configuration, enabling colored console output at the `INFO` level, including API request and response logs.  
All of the package's logs are emitted under the `api_client_core` logger name.
To customize the configuration, pass `config` (a `dict` to replace the default logging config) and/or `delta_config` (a `dict` to merge changes into the base config). See the [default logging configuration](src/api_client_core/cfg/logging.yaml) for the default settings.


# Type and Response Reference

## `RestResponse`

The object returned by every endpoint call. Key attributes:

| Attribute       | Type             | Description                                                                                                 |
|-----------------|------------------|-------------------------------------------------------------------------------------------------------------|
| `status_code`   | `int`            | HTTP status code.                                                                                           |
| `response`      | `JSONType`       | Decoded response body (dict, list, str, or `None`).                                                         |
| `ok`            | `bool`           | `True` if `200 <= status_code < 300`.                                                                       |
| `request`       | `Request`        | The underlying `httpx` request object, extended with `request_id`, `start_time`, `end_time`, and `retried`. |
| `_response`     | `httpx.Response` | Raw `httpx` response. Provides access for streaming and low-level response details.                         |
| `is_stream`     | `bool`           | `True` if this is a streaming response.                                                                     |
| `request_id`    | `str`            | UUID set per request in the `X-Request-ID` header.                                                          |
| `response_time` | `float \| None`  | Seconds between request dispatch and response received (`None` for streaming responses).                    |

## `Kwargs` and `Unpack`

`Kwargs` is a `TypedDict` that captures the three built-in keyword options accepted by every endpoint function:

```python
class Kwargs(TypedDict, total=False):
    quiet: bool                 # suppress request/response log output
    with_hooks: bool            # set to False to skip pre/post hooks
    raw_options: dict[str, Any] # raw httpx client options (timeout, headers, ...)
```

Always include `**kwargs: Unpack[Kwargs]` in your endpoint function signatures so callers can use these options without triggering an "unexpected keyword argument" error:

```python
from typing import Unpack
from api_client_core.types import Kwargs, RestResponse

@endpoint.get("/v1/items")
def list_items(self, *, page: int = Unset, **kwargs: Unpack[Kwargs]) -> RestResponse:
    ...
```

## `Query`

Use `Query` inside `Annotated` to send an individual parameter as a URL query string on non-GET endpoints. By default, non-GET endpoints place parameters in the request body. `Query` overrides this on a per-parameter basis.

Three equivalent forms are accepted:

| Form                                         | Example                                 |
|----------------------------------------------|-----------------------------------------|
| `Query()` — canonical instance (recommended) | `mode: Annotated[str, Query()] = Unset` |
| `Query` — bare class (no parentheses)        | `mode: Annotated[str, Query] = Unset`   |
| `"query"` — string                           | `mode: Annotated[str, "query"] = Unset` |

```python
from typing import Annotated, Unpack
from api_client_core.types import Kwargs, Query, RestResponse, Unset

@endpoint.post("/v1/items/{item_id}")
def update_item(
    self,
    item_id: int,
    *,
    payload: str = Unset,
    mode: Annotated[str, Query()] = Unset,   # sent as ?mode=<value> in the URL
    **kwargs: Unpack[Kwargs],
) -> RestResponse:
    ...
```

`Query` provides a per-parameter override. It is complementary to the endpoint-level `use_query_string=True` flag (which routes *every* parameter to the query string), and has no effect on `@endpoint.get(...)` endpoints (all GET parameters already go to the query string).

## `File`

Use `File` to upload files via `multipart/form-data`. Pass each uploaded file as a separate named parameter:

```python
from api_client_core.types import File

r = client.Users.upload_documents(
    avatar=File("avatar.png", b"<png bytes>", "image/png"),
    resume=File("resume.pdf", b"<pdf bytes>", "application/pdf"),
)
```

## `Alias`

Use `Alias` inside `Annotated` when the API requires a parameter key name that is not a valid Python identifier (e.g., it contains hyphens or collides with a keyword):

```python
from typing import Annotated, Unpack

from api_client_core import endpoint
from api_client_core.types import Alias, Kwargs, RestResponse


@endpoint.post("/v1/sessions")
def create_session(
    self, *, user_id: Annotated[str, Alias("user-id")], **kwargs: Unpack[Kwargs]
) -> RestResponse:
    ...
```

The framework sends `"user-id"` as the actual key in the request payload while the Python parameter is named `user_id`.


# Extending Core

## Implement custom function logic

By default, an API function body should be just a stub (`...`), and the framework automatically generates the HTTP request from the parameters passed by the caller. In most cases, the function body can remain a stub:

```python
@endpoint.post("/auth/login")
def login(self, username: str, password: str, **kwargs: Unpack[Kwargs]) -> RestResponse:
    """Log in"""
    ...
```

If an endpoint requires custom request logic, replace the stub with your own code. The body must return a `RestResponse` (the object the underlying REST client returns). Returning `None` (or leaving the default stub implementation) falls back to the framework's automatically generated request.

> [!NOTE]
> Returning anything other than a `RestResponse` or `None` from a custom function body raises a `RuntimeError`.

> [!TIP]
> - If you only need to add behavior before or after the request, use a [registered decorator](#add-a-custom-registered-decorator) or [request hooks](#request-hooks) instead.
> - If your use case is async-only, define the function body as `async def` instead of `def` if you need to perform async operations in your custom logic. See [Sync vs Async](#sync-vs-async) for details.


## Add a custom registered decorator

Create a decorator, register it with `@endpoint.decorator`, and apply it to an API function:

```python
from collections.abc import Callable
from functools import wraps
from typing import Concatenate, ParamSpec, TypeVar

from api_client_core import BaseAPI, endpoint
from api_client_core.types import RestResponse

P = ParamSpec("P")
R = TypeVar("R", bound=RestResponse)


@endpoint.decorator
def no_prod(f: Callable[Concatenate[BaseAPI, P], R]) -> Callable[Concatenate[BaseAPI, P], R]:
    """Raise if called against a production environment."""

    @wraps(f)
    def wrapper(self: BaseAPI, *args: P.args, **kwargs: P.kwargs) -> R:
        if self.env == "prod":
            raise RuntimeError(f"{f.__name__!r} must not be called against production")
        return f(self, *args, **kwargs)

    return wrapper
```

**Decorator with arguments:**

```python
import warnings
from collections.abc import Callable
from functools import wraps
from typing import ParamSpec, TypeVar

from api_client_core import endpoint
from api_client_core.types import RestResponse

P = ParamSpec("P")
R = TypeVar("R", bound=RestResponse)


@endpoint.decorator
def warn_if_slow(threshold_ms: float) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Emit a warning when the response time exceeds the given threshold (ms)."""

    def decorator(f: Callable[P, R]) -> Callable[P, R]:
        @wraps(f)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            r = f(*args, **kwargs)
            elapsed = r.response_time * 1000
            if elapsed > threshold_ms:
                warnings.warn(f"{r.request.method} {r.request.url} took {elapsed:.0f}ms (threshold: {threshold_ms}ms)")
            return r

        return wrapper

    return decorator
```

> [!TIP]
> Custom decorators can appear at any position relative to `@endpoint.<method>("/path")` — above or below it. The framework resolves the stack correctly either way.

## Override `request_wrapper` for class-level cross-cutting behavior

`request_wrapper` is the right place for class-level behavior that must wrap the entire request lifecycle (pre/post hooks and the HTTP call). Unlike a [registered decorator](#add-a-custom-registered-decorator), which must be applied to each endpoint function individually, a `request_wrapper` is applied automatically to every endpoint on the class. Note that endpoint decorators applied with `@endpoint.decorator` run *outside* the request wrapper — decorators are the outermost layer. Return a list of plain callables. Each receives the `EndpointFunc` instance as its first argument, so it has access to `.method`, `.path`, and `.endpoint` (the `Endpoint` metadata object):

```python
import time
from collections.abc import Callable
from functools import wraps
from typing import Any

from api_client_core import BaseAPI, EndpointFunc
from api_client_core.types import RestResponse


def timing_wrapper(call: Callable[..., RestResponse]) -> Callable[..., RestResponse]:
    """Log how long each endpoint call takes."""

    @wraps(call)
    def wrapper(endpoint_func: EndpointFunc, *args: Any, **kwargs: Any) -> RestResponse:
        start = time.perf_counter()
        response = call(endpoint_func, *args, **kwargs)
        print(f"{endpoint_func.method} {endpoint_func.path} took {time.perf_counter() - start:.2f}s")
        return response

    return wrapper


class MyAppBaseAPI(BaseAPI):
    app_name = "my-app"

    def request_wrapper(self) -> list[Callable[..., Any]]:
        return [timing_wrapper]
```

> [!NOTE]
> This assumes a sync client. For an async client, `EndpointFunc.__call__` is a coroutine, so `wrapper` would need to be `async def` and `await call(...)`.

## Plug in custom `Endpoint` / `EndpointFunc` subclasses

`BaseAPI` exposes three class-level attributes that control which concrete classes are instantiated at runtime:

| Attribute                    | Default             | Purpose                                                                   |
|------------------------------|---------------------|---------------------------------------------------------------------------|
| `_endpoint_class`            | `Endpoint`          | The `Endpoint` dataclass subclass to use when building endpoint metadata. |
| `_sync_endpoint_func_class`  | `SyncEndpointFunc`  | The sync endpoint function subclass.                                      |
| `_async_endpoint_func_class` | `AsyncEndpointFunc` | The async endpoint function subclass.                                     |

Override any of them on your app-level base class to inject custom behavior into the endpoint lifecycle without modifying framework code:

```python
from api_client_core import BaseAPI
from api_client_core.endpoints.endpoint_func import SyncEndpointFunc, AsyncEndpointFunc


class MyEndpointFunc(SyncEndpointFunc):
    """Add a .docs() helper to every sync endpoint."""

    def docs(self) -> None:
        print(f"Endpoint: {self.endpoint}")


class MyAsyncEndpointFunc(AsyncEndpointFunc):
    """Async counterpart."""

    def docs(self) -> None:
        print(f"Endpoint: {self.endpoint}")


class MyAppBaseAPI(BaseAPI):
    app_name = "my-app"
    _sync_endpoint_func_class = MyEndpointFunc
    _async_endpoint_func_class = MyAsyncEndpointFunc
```

## Real-world example: OpenAPI Test Client

[OpenAPI Test Client](https://github.com/yugokato/openapi-test-client) is a client-generation tool for QA engineers built on top of this framework. It consumes an OpenAPI 3.x spec and generates the API classes, endpoint functions, and parameter models described above, using the framework's extension points (auto-discovery, decorators, request hooks, and pluggable `Endpoint`/`EndpointFunc`/`EndpointModel` classes).

In addition to the features provided by API Client Core, it adds the following user-facing features:

- **`openapi-client generate`/`update` CLI** — generates a complete, ready-to-use API client (client class, API classes, fully typed endpoint functions with spec-derived docstrings, and parameter models) directly from an OpenAPI spec URL, and later updates it in place as the spec evolves, without touching the function bodies, decorators, or hooks you've added.
- **Auto-generated parameter models (`ParamModel`)** — dataclasses generated from OpenAPI object schemas that behave as both a dataclass and a dict at once, so nested request bodies can be read, built, and mutated either way.
- **Validation mode** — an opt-in mode that converts the generated dataclass models into strict-mode Pydantic models and validates request payloads client-side before sending.
- **Schema-derived type annotations** — `Constraint` and `Format` annotations (e.g. length/range limits, `email` or `uri` string formats) generated directly from the OpenAPI schema and carried through to both the dataclass and Pydantic models.
- **API tags** — endpoint functions and API classes carry the OpenAPI `tags` metadata, mirroring how the spec organizes endpoints.

See the [OpenAPI Test Client README](https://github.com/yugokato/openapi-test-client) for the full walkthrough.

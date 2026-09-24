API Client Core — MCP Server
============================

Installing [api-client-core](../../../README.md) with the `mcp` extra also installs the `api-client-mcp` command. It inspects your project, discovers your API clients, and turns their endpoint definitions into MCP tools, allowing AI agents to discover and call your API directly.

This guide covers the key concepts beyond `-h`/`--help`, which documents every option.

All examples use the [DummyJSON example client](https://github.com/yugokato/api-client-core/tree/main/examples/dummyjson), a small client included in this repository. It connects to the public `DummyJSON` (https://dummyjson.com) sandbox API, requires no API key, and every command shown here is safe to run.


# Table of Contents

- [Quick Start](#quick-start)
- [How Your Code Becomes MCP Tools](#how-your-code-becomes-mcp-tools)
- [Exposure Modes](#exposure-modes)
- [The Dynamic Meta-Tools](#the-dynamic-meta-tools)
- [Filtering What's Exposed](#filtering-whats-exposed)
- [Safety Annotations](#safety-annotations)
- [Tool Results and Errors](#tool-results-and-errors)
- [Parameters and Types](#parameters-and-types)
- [Call Wrappers](#call-wrappers)
- [Authentication](#authentication)


# Quick Start

### 1. Install the `mcp` extra
Install `api-client-core` with its `mcp` extra:

```bash
pip install "api-client-core[mcp] @ git+https://github.com/yugokato/api-client-core.git"
```

### 2. Configure your MCP client

Configure your MCP client to use the MCP server. For example, with Claude Code, add the server with:

```bash
claude mcp add dummyjson -- api-client-mcp DummyJSON
```

Or configure it in `.mcp.json`:

```json
{
  "mcpServers": {
    "dummyjson": {
      "command": "api-client-mcp",
      "args": ["DummyJSON"]
    }
  }
}
```

Start Claude Code and run `/mcp`. Confirm that the server is connected and its tools are available.

### 3. Use the API in natural language

Ask your MCP client to perform API operations in natural language.

For example:

```text
❯ Add 3 "Cat Food" products to Emily Johnson's cart

  ...
  
⏺ Done. Created cart #209 for Emily Johnson (user id 1) with 3x Cat Food (product id 18) — total $26.97, discounted to $24.00.
```

The MCP client discovers the tools exposed by the server, and Claude uses those tools to fulfill the request.

For the above example, Claude made the following API calls in order:

1. GET https://dummyjson.com/products/search?q=Cat+Food  
   → Found "Cat Food" (product ID 18)
2. GET https://dummyjson.com/users/search?q=Emily+Johnson  
   → No results
3. GET https://dummyjson.com/users/search?q=Emily  
   → Found Emily Johnson (user ID 1)
4. POST https://dummyjson.com/carts/add  
   Body: {"userId": 1, "products": [{"id": 18, "quantity": 3}]}  
   → Cart created


# How Your Code Becomes MCP Tools

The MCP tools are generated directly from your Python API client. There is no separate MCP tool definition to maintain:

| Your code                         | Becomes            | Where it comes from                                     |
|------------------------------------|---------------------|-----------------------------------------------------------|
| API client (`APIClient` subclass) | server identity     | The client's `app_name`                                 |
| API class exposed on the client   | resource            | The attribute name                                      |
| Endpoint method                   | tool name           | `{resource}__{func_name}`, e.g. `products__get_product` |
| Method parameters                 | tool input schema   | The parameter's type annotation, as JSON Schema         |
| The function's docstring          | tool description    | The prose above any `:param:` lines                     |

Discovery walks your project from its root, finds every `APIClient` subclass and the API classes it exposes, and skips anything that doesn't look like a discoverable resource. Running `api-client-mcp` therefore imports your project's code.

> [!WARNING]
> Never point `api-client-mcp` at an untrusted project. An MCP host typically runs the server unattended, so any import-time code in your project executes with no one watching.


# Exposure Modes

A server can expose endpoints in two ways:

- **`static`**: one MCP tool per endpoint, with a full JSON Schema input, generated from your code at startup. The model sees every parameter up front and gets real schema validation per tool. Best accuracy, but doesn't scale: a large client publishing hundreds of tools blows out the model's context before it even reads your message.
- **`dynamic`**: four fixed tools regardless of client size: `list_resources`, `search_endpoints`, `describe_endpoint`, `call_endpoint`. Constant footprint at any scale, at the cost of an extra round trip (search/describe, then call) and no per-tool schema validation on `call_endpoint`'s free-form `arguments` object.

`--mode auto` (the default) picks between them by endpoint count, after filters are applied: `static` below `--threshold` (default `50`), `dynamic` at or above it. `--mode static` or `--mode dynamic` forces one regardless of count.

The DummyJSON example client has more than 50 endpoints, so by default it starts in dynamic mode, publishing only `list_resources`, `search_endpoints`, `describe_endpoint`, and `call_endpoint`:

```bash
api-client-mcp DummyJSON
```

Filtering down first can flip the resolved mode, since filters apply before the threshold check. Limiting to the `products` resource brings the count under the threshold, so this starts in static mode instead, publishing one tool per endpoint (`products__list_products`, `products__get_product`, and so on):

```bash
api-client-mcp DummyJSON --resource products
```


# The Dynamic Meta-Tools

Dynamic mode provides four tools.

- **`list_resources()`**: Lists the available API resources and their endpoint counts. A good first call to get oriented.
- **`search_endpoints({query?, resource?, method?, limit?, offset?})`**: Searches an `endpoint_id` by keyword, resource, or HTTP method. Ranks an exact `endpoint_id` match first, then a substring match in the id, then in the path, then in the summary.
- **`describe_endpoint({endpoint_id})`**: Returns the complete metadata for one endpoint, including its `input_schema` (the same schema a static-mode tool would publish for that endpoint, minus the `call_wrappers` property, which sits on `call_endpoint` itself in dynamic mode). Each property's request location (`path`/`query`/`body`) is embedded in its schema as `"x-location"`.
- **`call_endpoint({endpoint_id, arguments})`**: Dispatches the endpoint call. `arguments` is the same object `describe_endpoint`'s `input_schema` describes.

A typical model workflow: `search_endpoints({"query": "product"})` → `describe_endpoint({"endpoint_id": "products__create_product"})` → `call_endpoint({"endpoint_id": "products__create_product", "arguments": {"title": "Widget", "price": 9.99}})`. The server's MCP `instructions` describe this workflow to the model.


# Filtering What's Exposed

Every filter narrows the exposed endpoint set, applied in this order:

| Flag                   | Effect                                                                        |
|------------------------|-------------------------------------------------------------------------------|
| `--resource NAME`      | Keep only this resource (repeatable)                                          |
| `--method METHOD`      | Keep only this HTTP method (repeatable)                                       |
| `--read-only`          | Keep only GET/HEAD/OPTIONS/TRACE. Combined with `--method`, the two intersect |
| `--include GLOB`       | Keep only tool names matching this glob (repeatable)                          |
| `--exclude GLOB`       | Drop tool names matching this glob (repeatable, applied last, always wins)    |

For example:

```bash
api-client-mcp DummyJSON --resource products --resource users --read-only
```

Filters are applied before `--mode auto` chooses static or dynamic mode. Glob matching (`--include`/`--exclude`) is against each endpoint's un-prefixed tool name, so a pattern like `products__*` behaves the same whether or not `--tool-prefix` is also given.

`--tool-prefix TOKEN` prepends an extra token ahead of every endpoint's own name (a static-mode tool name, or a dynamic-mode `endpoint_id`), producing `{token}__{resource}__{func_name}`. Use it when a host flattens tool names from multiple MCP servers into one namespace.


# Safety Annotations

Every endpoint is exposed by default, including write paths. Each tool instead carries MCP annotations derived from its HTTP method, so the MCP host's approval UI can gate a write the same way it gates any other tool:

| Method                         | `readOnlyHint` | `destructiveHint` | `idempotentHint` |
|--------------------------------|:--------------:|:-----------------:|:----------------:|
| `GET`/`HEAD`/`OPTIONS`/`TRACE` |       ✓       |                   |        ✓        |
| `PUT`/`DELETE`                 |                |        ✓         |        ✓        |
| `PATCH`                        |                |        ✓         |                  |
| `POST`                         |                |                   |                  |

`openWorldHint` is true for `call_endpoint` and every static-mode endpoint tool, since each one reaches an external HTTP API. The three read-only meta-tools (`list_resources`, `search_endpoints`, `describe_endpoint`) set it false instead, since they only read the server's own catalog. In dynamic mode, `call_endpoint`'s annotations are computed from whichever endpoints actually survived filtering, so `--read-only` makes it advertise itself as read-only too, rather than a fixed worst case.

Use `--read-only`/`--method` to actually restrict what's callable, not just what's advertised.

File parameters are the one place exposure is narrowed by default. A `File` parameter's schema only ever offers inline base64 content (`{"filename": ..., "content_base64": ..., "content_type": ...}`) unless `--allow-file-paths` is given, which adds a `{"path": "..."}` alternative accepting a local filesystem path. `--max-file-bytes` (default 10 MiB) caps the decoded size either way.

> [!WARNING]
> `--allow-file-paths` lets a model have this process read any file it can read and send the content out over HTTP. Leave it off unless you need it.


# Tool Results and Errors

A successful call returns the HTTP response as a JSON object:

```json
{"status_code": 200, "headers": {...}, "body": {...}}
```

`--response-format` controls what a successful call's result contains:

| Value             | Result content                                                    |
|-------------------|----------------------------------------------------------------------|
| `full` (default)  | `{status_code, headers, body}` for the call                          |
| `json`            | just the decoded response body                                       |
| `raw`             | the undecoded response text, exactly as returned by the server       |

`full` and `json` also carry the same value as `structuredContent`, alongside the text content, except when a `json` body isn't itself a JSON object (a list, a string, a bare number, ...), in which case it's only ever shown as text. `raw` never carries `structuredContent`. A failed call (see below) always carries the same envelope as `structuredContent` too, matching the text content.

Response headers are limited to a fixed allowlist by default, since a result reaches a model's context rather than a human's terminal, and most response headers (CDN, caching, tracing) are noise it can't act on. The allowlist:

`Content-Type`, `Content-Length`, `Location`, `Retry-After`, `ETag`, `Last-Modified`, `WWW-Authenticate`, `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset` (and their un-prefixed `RateLimit-*` equivalents), and `X-Request-Id`.

`WWW-Authenticate` is a challenge (scheme/realm/error), not a credential, so it's included even on a failed auth attempt. `--all-headers` opts out and returns every header, including any sensitive one (`Set-Cookie`, `Authorization`, ...).

A response body that's binary content, rather than JSON or UTF-8 text, is shown as `{"content_base64": "...", "encoding": "base64"}` instead of being embedded directly, since raw bytes can't be placed into a JSON structure.

A result larger than 256 KiB once rendered (headers included) is never sent whole. The oversized part (a single call's `body`, or a `with_repeat`/`with_concurrency` group's whole `results` array) is replaced with `{"truncated": true, "bytes": ..., "preview": "..."}`, where `preview` holds roughly the first 2000 characters so the model can still see the response's shape. For an oversized binary body, `bytes` is the raw body's own size rather than a rendered size, since that's the more meaningful number for binary content. This is a fixed limit with no flag to raise it, since an oversized result can break the connection.

A non-2xx response, a bad argument (an unrecognized key, a missing required one, a malformed file), and an unexpected error (a network failure) are all reported as `isError: true`, never as a raised protocol error. A bad argument or an unexpected error is a one-line detail. A non-2xx response also includes the API's `{status_code, headers, body}` envelope, since that body is often what lets the model correct its arguments and retry.


# Parameters and Types

Each parameter's Python annotation maps to a JSON Schema fragment:

| Parameter type                                                  | JSON Schema                                                             |
|---------------------------------------------------------------------|-------------------------------------------------------------------------|
| `str`/`int`/`float`/`bool`                                       | `string`/`integer`/`number`/`boolean`                                   |
| `datetime`/`date`/`time`/`UUID`                                  | `string`, with a `format`                                               |
| `Decimal`                                                        | `string` (precision)                                                    |
| `Enum`                                                           | `string` with `enum` of member names                                    |
| `Literal[...]`                                                   | `enum` of the literal values, plus a shared `type` when every literal is the same scalar type |
| `list[X]`/`set[X]`/`frozenset[X]`/`tuple[X, ...]`/`Sequence[X]`  | `array`                                                                  |
| A fixed-length `tuple[X, Y]`                                     | `array` with `prefixItems`, and `minItems`/`maxItems` set to its length |
| `dict[K, V]`                                                     | `object`, with `additionalProperties` for `V`                           |
| A bare `dict` (no type arguments)                                | `object`, with no `additionalProperties` constraint                     |
| `T \| None`                                                      | `anyOf` of `T`'s schema and `{"type": "null"}`                          |
| A genuine union (`int \| str`)                                   | `anyOf`                                                                  |
| `File`                                                           | see [Safety Annotations](#safety-annotations)                           |
| A `str` subclass                                                 | `string`                                                                 |
| Other / missing annotation                                       | an unconstrained schema (`{}`)                                          |

A required parameter (no default in the function signature) is listed in the schema's `required` array. A parameter with a concrete (non-`Unset`) default shows it under the property's `"default"` key. A deprecated parameter carries `"deprecated": true` and a note in its description. Every property also carries its own request location as `"x-location"` (`path`/`query`/`body`), a non-standard JSON Schema key.


# Call Wrappers

Every call accepts an optional `call_wrappers` object, exposing the framework's chainable [call wrappers](../../../README.md#chainable-call-wrappers). In static mode it sits beside the endpoint's own parameters. In dynamic mode it sits beside `endpoint_id`/`arguments` on `call_endpoint`.

```json
{
  "endpoint_id": "products__get_product",
  "arguments": {"product_id": 1},
  "call_wrappers": {"with_retry": {"condition": 429, "num_retries": 3}, "with_repeat": {"num": 5}}
}
```

Each key is a wrapper name mapped to its own keyword parameters:

| Wrapper                  | Options                                                                              |
|--------------------------|---------------------------------------------------------------------------------------|
| `with_retry`             | `condition` (a status code, or an array of them), `num_retries`, `retry_after` (seconds), `safe_methods_only` |
| `with_rate_limit`        | `max_requests` (required), `interval` (seconds)                                     |
| `with_expected_status`   | `status_codes` (a required, non-empty array)                                        |
| `with_max_response_time` | `threshold_msecs` (required)                                                        |
| `with_lock`              | `lock_name` (letters, digits, `_`, `-` only)                                        |
| `with_stats`             | *(none)*                                                                            |
| `with_repeat`            | `num`, `return_exceptions`                                                          |
| `with_concurrency`       | `num`, `max_connections`, `return_exceptions`                                       |

`with_polling` and `with_pagination` are not exposed, since both need a Python callable no JSON value can supply.

Rules that differ from a direct Python chain, all because the caller is a model:

- **Order is fixed.** Wrappers apply in a canonical order (`with_retry` → `with_rate_limit` → `with_expected_status` → `with_max_response_time` → `with_lock` → `with_repeat`/`with_concurrency`), not the order the JSON keys happen to appear, so a model can never author an invalid chain. One consequence: without `with_repeat`/`with_concurrency` in the same call, `with_lock` composes *inside* `with_retry`, so the lock is acquired and released fresh on every retry attempt rather than held for the whole sequence. Add `with_repeat`/`with_concurrency` and it flips: `with_lock` moves outside the whole group, holding the lock once across every repeated or concurrent call (and every retry within each one).
- **`with_repeat` and `with_concurrency` are mutually exclusive**, and each returns `{"results": [...]}` with one `{status_code, headers, body}` envelope (or `{"error": ...}`) per call. The result is `isError` if any call failed. Their `num`/`max_connections` are passed straight through to the framework's own `with_repeat()`/`with_concurrency()`, with no MCP-imposed limit.
- **`return_exceptions` defaults to `true`** for `with_repeat`/`with_concurrency`, so an N-call group always runs every call and returns a full list, instead of the first failure collapsing the result. Pass `{"return_exceptions": false}` for fail-fast.
- **`with_stats` returns its report in the result**, as a `stats` array on the envelope, rather than the printed table it produces on the command line (which, over stdio, would reach only the server's log). It's carried only under the default `full` response format: `--response-format json`/`raw` omit it, whether the call succeeded or failed. The one exception is a single endpoint call (not `with_repeat`/`with_concurrency`) that fails: that always includes the report, regardless of `--response-format`, since a failed single call is rendered through the same envelope the `full` format uses either way.
- **`with_lock`'s `lock_name` is restricted to a plain token**, since it becomes a filesystem path component behind the scenes.


# Authentication

There is no built-in login flow. The credential belongs in the client, not in the server's command line: `auth` is a plain constructor option forwarded to the underlying REST client (see the main [README's Authentication section](../../../README.md#authentication)), and `api-client-mcp` applies whatever auth a client installs for itself to every tool call, with no `-H` needed.

Read the credential from the process environment so it never has to be written into a config file:

```python
import os
from typing import Any

from api_client_core import APIClient
from api_client_core.auth import BearerAuth


class MyAppClient(APIClient):
    app_name = "my-app"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(base_url="https://api.example.com", auth=BearerAuth(os.environ["TOKEN"]), **kwargs)
```

Then pass that variable through the server config's `env` block:

```json
{
  "mcpServers": {
    "myapp": {
      "command": "api-client-mcp",
      "args": ["my-app"],
      "env": {"TOKEN": "${TOKEN}"}
    }
  }
}
```

For a token that must be fetched dynamically, use [`TokenProviderAuth`](../../../README.md#tokenproviderauth), which caches the token and refreshes it on expiry or a 401. This is particularly important for MCP because an MCP server is a long-lived process. A token that was valid when the server started may expire while the MCP session is still running. Because the MCP server runs clients in async mode, a token provider that performs authentication through the same client must be an async callable.

API Client Core CLI
====================

Installing [api-client-core](../../../README.md) also installs the `api-client` command. It inspects your project, discovers your API clients, and automatically turns their endpoint definitions into fully featured command-line interfaces.

This guide covers the key concepts beyond `-h`/`--help`, which provides detailed documentation for every command and option.

All examples use the [DummyJSON example client](https://github.com/yugokato/api-client-core/tree/main/examples/dummyjson), a small client included in this repository. It connects to the public `DummyJSON` (https://dummyjson.com) sandbox API, requires no API key, and every command shown here is safe to run.


# Table of Contents

- [Quick Start](#quick-start)
- [How Your Code Becomes a CLI](#how-your-code-becomes-a-cli)
- [How Discovery Works](#how-discovery-works)
- [Exploring Commands](#exploring-commands)
- [Command Parameters](#command-parameters)
- [Output, Piping, and Exit Codes](#output-piping-and-exit-codes)
- [Authentication](#authentication)
- [Call Wrappers](#call-wrappers)
- [Tab Completion](#tab-completion)


# Quick Start

Every CLI command follows this general structure:

```bash
api-client <app-name> <resource-group> <command> [OPTIONS]
```

For example, this Python call:

```pycon
>>> from examples.dummyjson.client import DummyJSONClient
>>> client = DummyJSONClient()
>>> client.users.get_user(user_id=1)
```

becomes:

```bash
api-client DummyJSON users get-user --user-id 1
```


# How Your Code Becomes a CLI

The CLI is generated directly from your Python API client. There is no separate CLI definition to maintain. The structure and names of your client determine the corresponding CLI structure.

| Your code                         | CLI token      | Where the name comes from        |
|-----------------------------------|----------------|----------------------------------|
| API client (`APIClient` subclass) | app-name       | The client's `app_name`          |
| API class exposed on the client   | resource-group | The attribute name (kebab-cased) |
| Endpoint method                   | command        | The method name (kebab-cased)    |
| Method parameters                 | options        | The parameter name (kebab-cased) |


# How Discovery Works

When you run `api-client`, it first determines which project your command belongs to. Starting from the current directory, it walks upward until it finds the nearest directory containing `pyproject.toml`, `setup.py`, `setup.cfg`, or `.git`. That directory is treated as the project root.  
`api-client` then imports the project's top-level modules and packages, including those under `src/` when present, and searches them for API clients and resources. 

> [!WARNING]
> Discovery imports your project's Python code. Import-time code can therefore execute every time `api-client` runs, including when you press TAB for shell completion. Avoid running `api-client` against untrusted source trees.

A directory or top-level module is skipped, and never imported, if:
- its name is `tests`, `test`, `build`, `dist`, `node_modules`, `venv`, `.venv`, `__pycache__`, or `site-packages`
- its name starts with `.` or `_`
- it's a virtual environment's own root

An `APIClient` subclass is discovered only if it is a leaf class (no subclass of its own), its own name doesn't start with `_`, and it declares a non-empty `app_name`. A resource must be exposed as a `@cached_property`/`@property` whose return type annotation is a `BaseAPI` subclass.


# Exploring Commands

`-h` and `--help` work at every level:

```bash
api-client -h
api-client DummyJSON -h
api-client DummyJSON users -h
api-client DummyJSON users get-user -h
```

On a leaf command, `-h` and `--help` provide different levels of detail:

- `-h`: Shows a compact summary. Some options may be omitted, and a documented parameter's description is clamped to one line.
- `--help`: Shows the complete command help.


# Command Parameters

A leaf command's help lists every endpoint parameter as a separate option. Each option shows where the value is sent, what type of value the CLI expects, and whether the option is required, etc., as well as the parameter's docstring description.

For example:

```
$ api-client DummyJSON carts create-cart -h
usage: api-client DummyJSON carts create-cart --user-id USER_ID --products VALUE [VALUE ...] [OPTIONS]

┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ POST /carts/add:                                                                                                         │
│   Create a cart for a user from a list of products                                                                       │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘

request parameters:
  --user-id USER_ID                  [body]  int    *required
                                       ID of the user to create the cart for
  --products VALUE [VALUE ...]       [body]  json[] *required
                                       Products to add to the cart, each as `{"id": <product_id>, "quantity": <quantity>}`
```

The output above is generated from the following endpoint definition:

```python
@endpoint.post("/carts/add")
def create_cart(
    self, user_id: Annotated[int, Alias("userId")], products: list[dict[str, int]], **kwargs: Unpack[Kwargs]
) -> RestResponse:
    """Create a cart for a user from a list of products

    :param user_id: ID of the user to create the cart for
    :param products: Products to add to the cart, each as `{"id": <product_id>, "quantity": <quantity>}`
    """
    ...
```

### Parameter type mapping

The CLI derives the expected value type from the parameter's Python annotation:

| Parameter type                                                                               | CLI form                                                                                         |
|----------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------|
| `str`/`int`/`float`, and date/time/UUID/`Decimal`-like types                                 | A single value                                                                                   |
| `bool`                                                                                       | A paired `--<name>`/`--no-<name>`. A parameter already named `no_<name>` gets only `--no-<name>` |
| `Enum` / `Literal[...]`                                                                      | A value restricted to `choices`, shown as `{a,b,c}`                                              |
| `list[X]`, `set[X]`, `tuple[X, ...]`, `Sequence[X]`, when `X` can be passed as a single value | One or more space-separated values, each of type `X`                                             |
| The same collection types as above, when `X` requires JSON (e.g. `list[dict]`)           | One or more JSON values, one per element                                                         |
| `File`                                                                                       | A path to an existing, readable file                                                             |
| Anything else (`dict`, an unmapped union, etc.)                                              | A single JSON value                                                                              |
| No annotation                                                                                | A JSON value, or a plain string when the value isn't valid JSON. No type is shown                |


### Passing JSON values
JSON-typed options accept values in three input forms:

#### Inline JSON

For a repeatable option such as `--products`, each argument is parsed as one element:

```bash
api-client DummyJSON carts create-cart --user-id 1 --products '{"id": 1, "quantity": 2}' '{"id": 2, "quantity": 4}'
```

#### From a file

Prefix a path with `@` to read the JSON value from a file:

```bash
api-client DummyJSON carts create-cart --user-id 1 --products @products.json
```
When tab completion is enabled, typing `@` also enables filesystem path completion.

#### From stdin

Use `-` to read the JSON value from standard input:

```bash
cat products.json | api-client DummyJSON carts create-cart --user-id 1 --products -
```

> [!NOTE]
> - `@<path>` and `-` provide the entire option value at once. For example, when `--products` expects a list, `@products.json` or `-` must contain the complete list as a JSON array, not a single list element.
> - `@<path>` and `-` cannot be combined with another value for the same option.
> - Only one `-` is allowed per command.


# Output, Piping, and Exit Codes

`api-client` keeps stdout suitable for scripting. By default, stdout contains only explicitly requested output, such as help text, `--version`, or an `--output` payload. Everything else, including request/response logs and other diagnostic messages, is written to stderr. 

### Output formats

Use `-o`/`--output` to control what the command writes to stdout:

| Value            | Writes                                                                 |
|------------------|------------------------------------------------------------------------|
| `none` (default) | nothing                                                                |
| `json`           | the decoded response body                                              |
| `raw`            | the undecoded response body as text, exactly as returned by the server |
| `full`           | `{status_code, headers, body}` for the call                            |

Selecting any value other than `none` also suppresses request/response logging, equivalent to passing `-q`/`--quiet`.  
This makes commands with `--output json` convenient to compose with tools such as `jq`.

Exit codes:

| Code  | Meaning                                                                            |
|-------|------------------------------------------------------------------------------------|
| `0`   | The call succeeded (a 2xx response, or a status given to `--with-expected-status`) |
| `1`   | The request was made but failed                                                    |
| `2`   | A usage or setup error                                                             |
| `130` | Interrupted (Ctrl-C)                                                               |
| `141` | Broken pipe                                                                        |

For example:

```bash
$ api-client DummyJSON products get-product --product-id 999999 --output json
{"message": "Product with id '999999' not found"}
$ echo $?
1
```

With `-q`/`--quiet` or a non-`none` `--output` mode, a failed request still writes a single diagnostic line to stderr:

```
error: GET https://dummyjson.com/products/999999 - 404 Not Found (request_id: ...)
```

This guarantees that a non-zero exit code is accompanied by at least one line explaining the failure.


# Authentication

There is no built-in login flow. The common pattern is to call your login endpoint, extract the token with `--output json` and `jq`, and pass it to subsequent commands with `-H`/`--header`.

For example when authenticating with a bearer token:

```bash
TOKEN=$(api-client DummyJSON auth login --username emilys --password emilyspass --output json | jq -r '.accessToken')
api-client DummyJSON auth get-current-user -H "Authorization: Bearer $TOKEN"
```

> [!TIP]
> `-H`/`--header` also supports `@<path>` (or `-` for stdin) to read one or more `NAME:VALUE` header lines from a file or stdin instead. This can help keep sensitive values out of shell history.

A client may set up its own auth (see the main [README's Authentication section](../../../README.md#authentication)), applied to every command automatically with no `-H` needed. An explicit `-H "Authorization: ..."` still overrides it for that one run. `--raw-option auth=null` instead sends a single command unauthenticated, bypassing whatever auth the client installed for itself.


# Call Wrappers

Most of the framework's chainable [call wrappers](../../../README.md#chainable-call-wrappers) are available from the CLI through `--with-*` options.

For example:

```bash
api-client DummyJSON products list-products --limit 100 \
    --with-stats \
    --with-retry condition=429,num_retries=3 \
    --with-concurrency 10
```

The CLI form follows these rules:
- Wrappers are applied in the order they appear on the command line, just as they are when chained in Python.
- Option values can be provided as a comma-separated `key=value` spec. e.g. `--with-retry condition=429,num_retries=3`. 
- A bare value is shorthand for the wrapper's primary option. `--with-retry 429` is equivalent to `--with-retry condition=429`.

Use `--help` on any leaf command for the exact wrapper options and syntax supported.


# Tab Completion

Tab completion is powered by [argcomplete](https://github.com/kislyuk/argcomplete). To enable tab completion, first install the optional dependency:

```bash
pip install api-client-core[cli-completion]
```

Then add the following to your shell startup file (`~/.bashrc` or `~/.bash_profile` for bash, `~/.zshrc` for zsh):

```bash
eval "$(register-python-argcomplete --no-defaults api-client)"
```

Open a new shell, or source the startup file after making the change.

> [!NOTE]
> - **zsh**: `compinit` must have run before the `eval` line. Most zsh frameworks already initialize it. A minimum configuration should include `autoload -Uz compinit && compinit` before the `eval` line.
> - **bash**: If completing an `@<path>` JSON-file value drops the leading `@` instead of completing the path, your `$COMP_WORDBREAKS` contains `@`. Add the following line before the `eval` line to remove it:
>   ```bash
>   COMP_WORDBREAKS=${COMP_WORDBREAKS//@}
>   ```

Completions are served from an on-disk cache (under `$XDG_CACHE_HOME`, or `~/.cache/api-client-core` if unset, as one `completion-<hash>.json` file per project). It's keyed on your project's source files, the CLI generator's own version, and the active Python environment (`sys.prefix`), so it invalidates automatically whenever any of those change and you do not need to clear it manually. If completions still look stale for some other reason (e.g. a dependency was upgraded in place, without changing the environment's own path), delete the cache directory, or set `_ARC_DEBUG=1` for one completion request to force a fresh rebuild, enable debug logging, and re-raise instead of silently giving up on failure.

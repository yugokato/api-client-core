"""Discovery layer: turns an `APIClient` subclass into a filtered, named, mode-resolved catalog of
endpoints ready to become MCP tools.

Reuses the shared client/resource/endpoint walk as-is, so the CLI and the MCP server can never disagree
about what a client exposes.
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from common_libs.logging import get_logger

from .._common.discovery import discover_resources, endpoints_for
from ._constants import (
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_THRESHOLD,
    MAX_TOOL_NAME_LEN,
    READ_ONLY_METHODS,
    TOOL_NAME_SEP,
    Mode,
    ResponseFormat,
)

if TYPE_CHECKING:
    from api_client_core.core.base import APIClient, BaseAPI
    from api_client_core.core.endpoints import Endpoint

logger = get_logger(__name__)

_NON_NAME_CHARS_RE = re.compile(r"[^a-z0-9_]+")
_UNDERSCORE_RUN_RE = re.compile(r"_+")


@dataclass(frozen=True)
class ServerOptions:
    """Every operator-facing option the MCP server accepts, threaded through every layer of the pipeline
    as one typed bag built once from argv.
    """

    mode: Mode = Mode.AUTO
    threshold: int = DEFAULT_THRESHOLD
    resources: tuple[str, ...] = ()
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    methods: tuple[str, ...] = ()
    read_only: bool = False
    tool_prefix: str | None = None
    allow_file_paths: bool = False
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    all_headers: bool = False
    response_format: ResponseFormat = ResponseFormat.FULL
    base_url: str | None = None
    headers: tuple[tuple[str, str], ...] = ()
    log_requests: bool = False

    def __post_init__(self) -> None:
        """Coerce `mode`/`response_format` to their real enum members.

        Downstream comparisons against them use `is`, which silently never matches a plain string, so
        coercing here once keeps every comparison correct regardless of what a caller passed.
        """
        object.__setattr__(self, "mode", Mode(self.mode))
        object.__setattr__(self, "response_format", ResponseFormat(self.response_format))


@dataclass(frozen=True)
class CatalogEntry:
    """One endpoint that survived filtering, together with its resolved tool name and the resource it belongs to."""

    tool_name: str
    resource: str
    api_class: type[BaseAPI[Any]]
    endpoint: Endpoint[Any]


@dataclass(frozen=True)
class EndpointCatalog:
    """The full set of endpoints this server will expose, and the exposure mode resolved for them.

    :param app_name: The served client's `app_name`
    :param entries: Every surviving endpoint, in deterministic (resource, func_name) order
    :param by_name: `entries` keyed by `tool_name` (== the dynamic-mode `endpoint_id`), for O(1) dispatch
    :param resources: `entries` grouped by resource attribute name, in the same deterministic order
    :param mode: The resolved exposure mode (`static` or `dynamic` - never `auto`, which only ever
                 names what was requested before resolution)
    """

    app_name: str
    entries: tuple[CatalogEntry, ...]
    by_name: dict[str, CatalogEntry]
    resources: dict[str, tuple[CatalogEntry, ...]]
    mode: Mode
    # Per-endpoint built input schema, keyed by tool name, populated lazily on first use. Kept off
    # equality/repr since it's a cache, not part of the catalog's identity.
    input_schema_cache: dict[str, dict[str, Any]] = field(default_factory=dict, compare=False, repr=False)


def build_catalog(client_class: type[APIClient], options: ServerOptions) -> EndpointCatalog:
    """Discover, filter, name, and mode-resolve every endpoint `client_class` exposes.

    Filters are applied before the `auto`-mode threshold check, so a filtered-down client can land on
    `static` even when the unfiltered one wouldn't. Raises `RuntimeError` if nothing is discovered, if a
    filter combination excludes every endpoint, or if the client's resources define no endpoints at all -
    each case gets a distinct message, so an operator isn't told to loosen filters they never set.

    :param client_class: Concrete `APIClient` subclass to build a catalog for
    :param options: Resolved server options
    """
    app_name = client_class.app_name
    if not app_name:
        raise RuntimeError(f"{client_class.__name__} has no 'app_name' class attribute set.")

    resources = discover_resources(client_class)
    if not resources:
        raise RuntimeError(
            f"No API classes discovered on {client_class.__name__}. A resource must be exposed as a "
            f"@cached_property/@property whose return type annotation is a BaseAPI subclass. If a resource module "
            f"failed to import instead, re-run with --log-level DEBUG to see why."
        )

    if options.tool_prefix and not _normalize_name_part(options.tool_prefix):
        logger.warning(
            f"--tool-prefix {options.tool_prefix!r} normalizes to an empty token and has no effect on any tool "
            f"name. Use a value with letters, digits, or underscores."
        )

    effective_methods = _effective_methods(options)

    all_methods: set[str] = set()
    candidates: list[tuple[str, type[BaseAPI[Any]], Endpoint[Any]]] = []
    for attr_name, api_class in sorted(resources.items()):
        for endpoint in sorted(endpoints_for(api_class), key=lambda e: e.func_name):
            all_methods.add(endpoint.method)
            if _matches_filters(attr_name, endpoint, options, effective_methods):
                candidates.append((attr_name, api_class, endpoint))

    entries: list[CatalogEntry] = []
    seen_names: dict[str, CatalogEntry] = {}
    for attr_name, api_class, endpoint in candidates:
        tool_name = _tool_name_for(attr_name, endpoint.func_name, prefix=options.tool_prefix)
        if tool_name in seen_names:
            logger.warning(
                f"Multiple endpoints resolve to tool name {tool_name!r}: keeping "
                f"{seen_names[tool_name].endpoint}, ignoring {endpoint}"
            )
            continue
        entry = CatalogEntry(tool_name=tool_name, resource=attr_name, api_class=api_class, endpoint=endpoint)
        seen_names[tool_name] = entry
        entries.append(entry)

    if not entries:
        raise _empty_catalog_error(client_class, resources, all_methods, options)

    resources_grouped: dict[str, list[CatalogEntry]] = {}
    for entry in entries:
        resources_grouped.setdefault(entry.resource, []).append(entry)

    mode = _resolve_mode(options.mode, len(entries), options.threshold)
    return EndpointCatalog(
        app_name=app_name,
        entries=tuple(entries),
        by_name={entry.tool_name: entry for entry in entries},
        resources={name: tuple(group) for name, group in resources_grouped.items()},
        mode=mode,
    )


def _empty_catalog_error(
    client_class: type[APIClient],
    resources: dict[str, type[BaseAPI[Any]]],
    all_methods: set[str],
    options: ServerOptions,
) -> RuntimeError:
    """Build the `RuntimeError` for a catalog that ended up with no endpoints.

    Two distinct causes, two messages: a filter combination that excluded everything (loosen the
    filters), or a client whose resource classes define no endpoints at all (nothing to loosen). The
    `Methods present:` clause is dropped when no endpoint was seen, so it never renders an empty list.

    :param client_class: The `APIClient` subclass a catalog was being built for
    :param resources: The discovered resources (attribute name to API class)
    :param all_methods: Every HTTP method seen across the discovered endpoints, before filtering
    :param options: Resolved server options
    """
    resource_names = ", ".join(sorted(resources))
    has_filters = bool(options.resources or options.include or options.exclude or options.methods or options.read_only)
    if not has_filters:
        return RuntimeError(
            f"{client_class.__name__} exposes {len(resources)} resource(s) ({resource_names}) but none of them "
            f"define any endpoints."
        )
    methods_clause = f" Methods present: {', '.join(sorted(all_methods))}." if all_methods else ""
    return RuntimeError(
        f"No endpoints matched the given filters. Loosen --resource/--include/--exclude/--method/--read-only, or "
        f"drop them entirely to expose every discovered endpoint. Discovered resources: {resource_names}."
        f"{methods_clause}"
    )


def _resolve_mode(requested: Mode, endpoint_count: int, threshold: int) -> Mode:
    """Resolve `auto` to a concrete exposure mode based on the already-filtered endpoint count.

    `static` below `threshold`, `dynamic` at or above it - strictly less-than, so a count exactly equal
    to `threshold` lands on `dynamic`, matching "at or above the threshold". An explicitly requested
    (non-`auto`) mode is always honored as given. Forcing `static` at or above `threshold` is allowed
    (it's the operator's call) but logged, since it's the one combination likely to be a surprise later.

    :param requested: The `--mode` value given (or its default, `auto`)
    :param endpoint_count: Number of endpoints remaining after filtering
    :param threshold: The `--threshold` value given (or its default)
    """
    if requested is not Mode.AUTO:
        if requested is Mode.STATIC and endpoint_count >= threshold:
            logger.warning(
                f"--mode static forced with {endpoint_count} endpoints (threshold: {threshold}). This publishes "
                f"one MCP tool per endpoint regardless of count."
            )
        return requested
    return Mode.STATIC if endpoint_count < threshold else Mode.DYNAMIC


def _matches_filters(
    resource: str, endpoint: Endpoint[Any], options: ServerOptions, effective_methods: frozenset[str] | None
) -> bool:
    """Return whether one endpoint survives every filter, in precedence order.

    Order: `--resource`, the combined `--method`/`--read-only` filter, `--include` (glob against the
    endpoint's own un-prefixed tool name), then `--exclude`, which always wins last.

    Matched with `fnmatch.fnmatchcase()`, not the case-normalizing `fnmatch.fnmatch()`, since a tool name
    is always lowercase and case-folding is platform-dependent (a no-op on POSIX, lowercasing on
    Windows).

    :param resource: Resource attribute name the endpoint was discovered on
    :param endpoint: Endpoint under test
    :param options: Resolved server options
    :param effective_methods: The combined method filter, or `None` for no method restriction
    """
    if options.resources and resource not in options.resources:
        return False
    if effective_methods is not None and endpoint.method not in effective_methods:
        return False
    name = _tool_name_for(resource, endpoint.func_name)
    if options.include and not any(fnmatch.fnmatchcase(name, pattern) for pattern in options.include):
        return False
    if options.exclude and any(fnmatch.fnmatchcase(name, pattern) for pattern in options.exclude):
        return False
    return True


def _tool_name_for(resource: str, func_name: str, *, prefix: str | None = None) -> str:
    """Derive an MCP tool name (and, identically, a dynamic-mode `endpoint_id`) from a resource
    attribute name and an endpoint function name.

    `{resource}__{func_name}` (an optional `{prefix}__` ahead of it), snake_case, mirroring the Python
    call it names (`users__get_user` <-> `client.users.get_user`). Each part is normalized independently
    before joining, so a stray underscore on either side can never widen the `__` separator into
    something ambiguous. A name over `MAX_TOOL_NAME_LEN` is truncated with a deterministic hash suffix
    rather than silently dropped or left to collide.

    :param resource: Resource attribute name
    :param func_name: Endpoint function name
    :param prefix: Optional extra token prepended ahead of `resource`, from `--tool-prefix`
    """
    parts = [_normalize_name_part(p) for p in (prefix, resource, func_name) if p]
    name = TOOL_NAME_SEP.join(p for p in parts if p)
    if len(name) <= MAX_TOOL_NAME_LEN:
        return name
    digest = hashlib.sha256(name.encode()).hexdigest()[:6]
    keep = MAX_TOOL_NAME_LEN - len(digest) - 1
    return f"{name[:keep]}_{digest}"


def _normalize_name_part(part: str) -> str:
    """Normalize one component of a tool name: lowercase, replace anything outside `[a-z0-9_]` with
    `_`, collapse repeated `_`, and strip leading/trailing `_`.

    :param part: One raw component (a resource attribute name, a function name, or a `--tool-prefix`)
    """
    cleaned = _NON_NAME_CHARS_RE.sub("_", part.lower())
    return _UNDERSCORE_RUN_RE.sub("_", cleaned).strip("_")


def _effective_methods(options: ServerOptions) -> frozenset[str] | None:
    """Combine `--method` and `--read-only` into one method filter, or `None` for no restriction.

    `--read-only` alone is sugar for `READ_ONLY_METHODS`. `--method` alone is used as given. Given
    together, the two intersect, since both are meant as restrictions layered onto each other rather
    than alternatives - an empty intersection (e.g. `--read-only --method post`) is a usage error, not
    a silently empty server.

    :param options: Resolved server options
    """
    methods = frozenset(m.lower() for m in options.methods) if options.methods else None
    read_only = frozenset(READ_ONLY_METHODS) if options.read_only else None
    if methods is None and read_only is None:
        return None
    if methods is None:
        return read_only
    if read_only is None:
        return methods
    combined = methods & read_only
    if not combined:
        raise RuntimeError(
            f"--method {sorted(methods)} and --read-only don't overlap (read-only methods: "
            f"{sorted(READ_ONLY_METHODS)}). Nothing would be exposed."
        )
    return combined

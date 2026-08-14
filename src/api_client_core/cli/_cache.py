"""On-disk shell-completion cache: cache-key computation, atomic load/save, and opportunistic pruning of stale cache
files left behind by other projects.

Stays stdlib-only, since it's imported at module scope on every shell-completion request.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path

from ._completion_schema import CompletionTree
from ._paths import find_project_root, is_own_package_dir, is_skipped_dir, is_venv_dir

_STALE_CACHE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
_PRUNE_CHECK_INTERVAL_SECONDS = 24 * 60 * 60
_PRUNE_SENTINEL_NAME = ".last-prune"
_REGISTERED_SENTINEL_NAME = ".completion-registered"


def cache_key(roots: list[Path]) -> str:
    """Compute a cache key from the active Python environment, this CLI package's own signature, and every project
    `.py` file's path, modification time, and size.

    The key changes whenever a project source file is added, removed, or edited, when the CLI generator itself changes,
    or when `sys.prefix` (the active virtual environment's own root) changes, so a cache stored under it is safe to
    treat as fresh only while the key still matches.

    :param roots: Project roots to scan
    """
    hasher = hashlib.sha256()
    hasher.update(f"env:{sys.prefix}".encode())
    hasher.update(f"cli:{_cli_package_signature()}".encode())
    for file_path in _iter_source_files(roots):
        try:
            stat = file_path.stat()
        except OSError:
            continue
        hasher.update(f"{file_path}:{stat.st_mtime_ns}:{stat.st_size}".encode())
    return hasher.hexdigest()


def mark_completion_registered() -> None:
    """Record that a real shell-completion request has been served, e.g. so the tab-completion setup tip can
    stop suggesting the `eval "$(register-python-argcomplete ...)"` step once it's confirmed done.

    Global rather than scoped to any one project: whether that `eval` line is active in the current shell
    is a property of the shell, not of whichever project is being completed in. Silently gives up on any
    `OSError` (e.g. a read-only cache directory), matching every other write in this module.
    """
    try:
        marker = _cache_dir() / _REGISTERED_SENTINEL_NAME
        if not marker.exists():
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
    except OSError:
        pass


def is_completion_registered() -> bool:
    """Whether a real shell-completion request has ever been served, i.e. whether
    `eval "$(register-python-argcomplete ...)"` is already active in some shell.
    """
    return (_cache_dir() / _REGISTERED_SENTINEL_NAME).exists()


def load_cache(key: str) -> CompletionTree | None:
    """Load the cached completion tree if it's still fresh for `key`.

    Returns `None` on a missing, unreadable, corrupt, or stale (key-mismatched) cache. An empty tree (a
    project with no discoverable clients) is a valid hit, distinct from a miss.

    :param key: Current cache key to validate the stored cache against
    """
    try:
        data = json.loads(_cache_file().read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("key") != key:
        return None
    tree = data.get("tree")
    return tree if isinstance(tree, dict) else None


def save_cache(key: str, tree: CompletionTree) -> None:
    """Write the completion tree to the cache file under `key`, atomically.

    Silently gives up if the cache directory can't be created or written to, or if `tree` holds something
    that can't be JSON-serialized, since either failure just means the next completion request rebuilds
    instead of reading a stale or missing cache. A per-process temp filename avoids clobbering a concurrent
    completion request's write.

    Also opportunistically prunes stale cache files left behind by other projects.

    :param key: Cache key the cache is valid for
    :param tree: Completion tree to cache
    """
    try:
        cache_file = _cache_file()
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = cache_file.with_name(f"{cache_file.name}.{os.getpid()}.tmp")
        tmp_file.write_text(json.dumps({"key": key, "tree": tree}))
        tmp_file.replace(cache_file)
    except (OSError, TypeError, ValueError):
        return
    _prune_stale_caches(cache_file.parent)


def _cli_package_signature() -> str:
    """Signature of the whole `api_client_core` package's own source files (path, modification time, and size).

    Mixed into the completion cache key so an upgrade or edit of the framework itself invalidates a cache
    built under the previous code, without waiting for a project source file to change. Covers the whole
    package, not just `cli/`, since a change to e.g. `endpoints/utils/param_type.py` or `endpoint_model.py`
    changes how a parameter maps to a CLI flag just as directly as a change to `cli/params.py` itself would.
    """
    hasher = hashlib.sha256()
    package_root = Path(__file__).parent.parent
    for path in sorted(package_root.rglob("*.py")):
        try:
            stat = path.stat()
        except OSError:
            continue
        hasher.update(f"{path.relative_to(package_root)}:{stat.st_mtime_ns}:{stat.st_size}".encode())
    return hasher.hexdigest()


def _iter_source_files(roots: list[Path]) -> Iterator[Path]:
    """Yield every project `.py` file's path reachable from `roots`, each exactly once.

    Skips a directory already visited under an earlier root (the project root and its `src/` subdirectory
    can overlap), any excluded directory name at every depth, this framework's own installed package
    directory specifically (by resolved path, not name), and a virtual environment's own root regardless of
    its name, so a venv named e.g. `env/` doesn't get its whole `site-packages` tree walked and stat'd on
    every discovery pass.

    :param roots: Project roots to scan
    """
    visited: set[Path] = set()
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            resolved = Path(dirpath).resolve()
            if resolved in visited:
                dirnames[:] = []
                continue
            visited.add(resolved)
            dirnames[:] = sorted(
                d
                for d in dirnames
                if not is_skipped_dir(d)
                and not is_own_package_dir(Path(dirpath, d))
                and not is_venv_dir(Path(dirpath, d))
            )
            for filename in sorted(filenames):
                if filename.endswith(".py"):
                    yield Path(dirpath, filename)


def _cache_dir() -> Path:
    """Return this package's cache directory under the user's cache home, shared by the per-project completion
    cache and the global completion-registered marker.

    `$XDG_CACHE_HOME` (or `~/.cache` if unset) on every platform this project's own CI matrix and `sys.platform`
    checks treat as POSIX. On Windows, `%LOCALAPPDATA%` (or `~\\AppData\\Local` if unset) instead, matching that
    platform's own convention rather than the XDG one, which Windows has no notion of.
    """
    if sys.platform == "win32":
        cache_home = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        cache_home = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return cache_home / "api-client-core"


def _cache_file() -> Path:
    """Return the current project's completion cache file path, under the user's cache directory.

    Keyed on the resolved project root rather than the raw current directory, so a completion request from a
    subdirectory of the project resolves to the same cache file as one from the project root.
    """
    root = find_project_root(Path.cwd()) or Path.cwd()
    project_hash = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
    return _cache_dir() / f"completion-{project_hash}.json"


def _prune_stale_caches(cache_dir: Path) -> None:
    """Delete every completion cache file (and orphaned temp file) under `cache_dir` last written more than
    `_STALE_CACHE_MAX_AGE_SECONDS` ago, e.g. one left behind by a project that no longer exists, or a
    `.tmp` file `save_cache()` never got to `replace()` (a crash between the write and the rename).

    A no-op unless the prune sentinel's own mtime is missing or older than `_PRUNE_CHECK_INTERVAL_SECONDS`,
    so this scan runs at most roughly once per day. Each file's own `stat()`/`unlink()` is wrapped
    individually so one vanished file (removed by a concurrent prune elsewhere) can't stop the sweep partway
    through and leave `sentinel` untouched, which would otherwise force the same scan to re-run on every
    subsequent cache miss instead of once a day.

    :param cache_dir: The cache directory to prune
    """
    sentinel = cache_dir / _PRUNE_SENTINEL_NAME
    try:
        if time.time() - sentinel.stat().st_mtime < _PRUNE_CHECK_INTERVAL_SECONDS:
            return
    except OSError:
        pass

    now = time.time()
    for cache_file in (*cache_dir.glob("completion-*.json"), *cache_dir.glob("completion-*.json.*.tmp")):
        try:
            if now - cache_file.stat().st_mtime > _STALE_CACHE_MAX_AGE_SECONDS:
                cache_file.unlink(missing_ok=True)
        except OSError:
            continue
    try:
        sentinel.touch()
    except OSError:
        pass

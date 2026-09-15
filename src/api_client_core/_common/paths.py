"""Project-path helpers shared by the shell-completion hot path and project discovery.

Kept stdlib-only so both stay importable without pulling in `httpx2` on the completion hot path.
"""

from __future__ import annotations

from pathlib import Path

_SKIP_DIRS = frozenset(
    ("tests", "test", "build", "dist", "venv", ".venv", "node_modules", "__pycache__", "site-packages")
)
_PROJECT_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", ".git")
# This installed api_client_core package's own root directory, resolved once at import time: the src/
# directory of a checkout for an editable install (as this repository's own dogfooding scan hits), or a
# site-packages directory for a real one.
_OWN_PACKAGE_DIR = Path(__file__).parent.parent.resolve()


def is_skipped_dir(name: str) -> bool:
    """Return whether a directory (or top-level module) name should be excluded from project source discovery.

    :param name: A single path component (directory or top-level module name) to test
    """
    return name in _SKIP_DIRS or name.startswith((".", "_"))


def is_own_package_dir(path: Path) -> bool:
    """Return whether `path` resolves to this installed `api_client_core` package's own root directory.

    Checked by resolved path rather than by name, so scanning a project that vendors or embeds a copy of
    this framework under a differently-named directory doesn't skip it, and a user's own unrelated
    directory that happens to be named `api_client_core` isn't skipped either.

    :param path: Directory path to test
    """
    try:
        return path.resolve() == _OWN_PACKAGE_DIR
    except OSError:
        return False


def is_venv_dir(path: Path) -> bool:
    """Return whether `path` is a Python virtual environment's own root directory.

    Checked via `pyvenv.cfg`, the marker every standard-library/third-party venv tool (`venv`, `virtualenv`,
    `uv venv`) writes there regardless of the directory's own name, rather than a name in `_SKIP_DIRS`: a venv
    named `env/` (or anything else not already on that list) would otherwise have its whole `site-packages`
    tree scanned on every discovery pass.

    :param path: Directory path to test
    """
    return (path / "pyvenv.cfg").is_file()


def find_project_root(start: Path) -> Path | None:
    """Walk `start` and its parents for the nearest directory containing a project marker
    (`pyproject.toml`, `setup.py`, `setup.cfg`, or `.git`), or return `None` if none is found.

    :param start: Directory to start the upward search from (typically `Path.cwd()`)
    """
    for candidate in (start, *start.parents):
        if any((candidate / marker).exists() for marker in _PROJECT_MARKERS):
            return candidate
    return None


def project_roots() -> list[Path]:
    """Return the project roots to scan for the completion cache key.

    Anchored to the nearest enclosing project root, falling back to the current working directory when no
    project marker is found.
    """
    root = find_project_root(Path.cwd()) or Path.cwd()
    roots = [root]
    src_dir = root / "src"
    if src_dir.is_dir():
        roots.append(src_dir)
    return roots

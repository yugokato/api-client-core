"""The API-client framework itself: base classes, the endpoint system, and the types they use.

This subpackage holds everything that makes up the framework, kept apart from the package's public
surface (`api_client_core/__init__.py`, `types.py`, `auth.py`, `logging.py`) and from the two code
generators (`cli/`, `mcp/`). Import nothing here at module scope: a bare `import api_client_core` and the
generators' stdlib-only entry-point fast paths must not pull in `httpx2` (via `common_libs`), and an eager
re-export here would defeat that.
"""

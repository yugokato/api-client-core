"""Unit tests for the public `api_client_core.types` re-export shim (`types.py`)."""

from __future__ import annotations

import inspect
import subprocess
import sys
import textwrap

from common_libs.clients.rest_client.rate_limit import RateLimit, RateLimiter
from common_libs.clients.rest_client.retry import BackoffStrategy, RetryPolicy

import api_client_core.core.types as core_types
import api_client_core.types as public_types
from api_client_core.types import DataclassModel, ParamAnnotationType

# Names the shim re-exports straight from the REST client library rather than from `core/types.py`.
_CONFIG_REEXPORTS = {
    "BackoffStrategy": BackoffStrategy,
    "RateLimit": RateLimit,
    "RateLimiter": RateLimiter,
    "RetryPolicy": RetryPolicy,
}

# The model/annotation base classes a downstream reserved-name scan is expected to pick up by filtering
# the module namespace for `ParamAnnotationType` / `DataclassModel` subclasses.
_INTROSPECTABLE_MODEL_TYPES = {"Alias", "DataclassModel", "EndpointModel", "File", "ParamAnnotationType", "Query"}


class TestPublicSurface:
    """Tests for the set of names `api_client_core.types` exports"""

    def test_all_matches_module_namespace(self) -> None:
        """Test that every `__all__` entry is bound on the module and every public binding is in `__all__`"""
        exported = set(public_types.__all__)
        bound = {name for name in vars(public_types) if not name.startswith("_")}
        assert exported == bound

    def test_all_is_sorted(self) -> None:
        """Test that `__all__` stays in the isort-style order ruff enforces"""
        assert public_types.__all__ == sorted(public_types.__all__)

    def test_public_surface_is_core_plus_config_reexports(self) -> None:
        """Test that the surface is exactly `core/types.py`'s exports plus the client-config re-exports"""
        assert set(public_types.__all__) == set(core_types.__all__) | set(_CONFIG_REEXPORTS)

    def test_config_types_are_the_rest_client_originals(self) -> None:
        """Test that the client-config names resolve to the objects defined in the REST client library"""
        for name, obj in _CONFIG_REEXPORTS.items():
            assert getattr(public_types, name) is obj

    def test_framework_types_are_shared_with_core(self) -> None:
        """Test that each framework type is the same object whether reached via the shim or via `core`"""
        for name in core_types.__all__:
            assert getattr(public_types, name) is getattr(core_types, name)


class TestEagerBinding:
    """Tests that the shim binds its names at import time rather than lazily"""

    def test_shim_has_no_module_getattr(self) -> None:
        """Test that the shim defines no PEP 562 module `__getattr__`

        A downstream project filters `vars(api_client_core.types).values()` directly for its reserved-name
        scan, which a lazy module would leave empty until each name is first accessed.
        """
        assert "__getattr__" not in vars(public_types)

    def test_names_are_bound_without_attribute_access(self) -> None:
        """Test that a cold import of the shim already has every public name in its namespace

        Runs in a subprocess so the check observes a module dict built from scratch. By the time any other
        test runs the shim is already imported, so an in-process check on a lazy variant would pass
        vacuously against names a prior access had cached.
        """
        script = textwrap.dedent("""
            import api_client_core.types as t

            missing = [name for name in t.__all__ if name not in vars(t)]
            assert not missing, missing
            assert "__getattr__" not in vars(t)
        """)
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr


class TestDownstreamIntrospectionContract:
    """Tests for the namespace shape a downstream reserved-name scan depends on"""

    def test_model_type_subclasses_in_namespace(self) -> None:
        """Test that filtering the namespace for model/annotation base classes yields exactly the known set

        A downstream project iterates `vars(module).values()`, keeps the classes that subclass
        `ParamAnnotationType` or `DataclassModel`, and treats their names as reserved. Widening the module
        with the client-config re-exports must not change this set, since none of them are such a subclass.
        """
        found = {
            obj.__name__
            for obj in vars(public_types).values()
            if inspect.isclass(obj) and issubclass(obj, ParamAnnotationType | DataclassModel)
        }
        assert found == _INTROSPECTABLE_MODEL_TYPES

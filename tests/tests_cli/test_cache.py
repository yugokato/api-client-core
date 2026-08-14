"""Unit tests for `api_client_core.cli._cache` (the on-disk shell-completion cache).

Exercises cache-key computation, atomic load/save, and stale-cache pruning in isolation from
`_entrypoint.py`'s own completion-request/argparse-rebuilding concerns. See `_cache`'s module docstring
for why this stays a separate module from `_entrypoint.py`.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pytest_mock import MockerFixture

from api_client_core.cli import _cache, _paths

_SAMPLE_TREE: dict[str, Any] = {
    "cli-test": {
        "opts": [{"opts": ["--base-url"], "choices": None, "nargs": None}],
        "resources": {
            "widgets": {
                "opts": [{"opts": ["--base-url"], "choices": None, "nargs": None}],
                "commands": {
                    "get-widget": [
                        {"opts": ["--widget-id"], "choices": None, "nargs": None},
                        {"opts": ["--quiet"], "choices": None, "nargs": 0},
                    ],
                },
            }
        },
    }
}
"""A tree matching `build_completion_tree()`'s schema, standing in for a real cached tree."""


class TestCacheDir:
    """Tests for `_cache_dir()`'s per-platform cache directory resolution"""

    def test_uses_local_appdata_on_windows(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Test that on Windows, the cache directory resolves under %LOCALAPPDATA%, not $XDG_CACHE_HOME,
        even when the latter is also set
        """
        monkeypatch.setattr(_cache.sys, "platform", "win32")
        local_appdata = tmp_path / "local-appdata"
        monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
        assert _cache._cache_dir() == local_appdata / "api-client-core"

    def test_uses_xdg_cache_home_on_a_non_windows_platform(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Test that off Windows, the cache directory resolves under $XDG_CACHE_HOME, not %LOCALAPPDATA%,
        even when the latter is also set
        """
        monkeypatch.setattr(_cache.sys, "platform", "linux")
        xdg_cache_home = tmp_path / "xdg-cache"
        monkeypatch.setenv("XDG_CACHE_HOME", str(xdg_cache_home))
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-appdata"))
        assert _cache._cache_dir() == xdg_cache_home / "api-client-core"


class TestCacheKey:
    """Tests for `cache_key()`, the on-disk cache's freshness/invalidation signal"""

    def test_stable_across_repeated_calls(self, project_dir: Path) -> None:
        """Test that the key is stable when nothing under the project has changed"""
        roots = _paths.project_roots()
        assert _cache.cache_key(roots) == _cache.cache_key(roots)

    @pytest.mark.parametrize(
        ("pre_mutate", "mutate"),
        [
            pytest.param(lambda p: None, lambda p: (p / "app.py").write_text("x = 2\n"), id="edited"),
            pytest.param(lambda p: None, lambda p: (p / "new_module.py").write_text("y = 1\n"), id="added"),
            pytest.param(
                lambda p: (p / "extra.py").write_text("y = 1\n"), lambda p: (p / "extra.py").unlink(), id="removed"
            ),
        ],
    )
    def test_changes_when_a_file_is_edited_added_or_removed(
        self, project_dir: Path, pre_mutate: Callable[[Path], None], mutate: Callable[[Path], None]
    ) -> None:
        """Test that editing a project .py file's content, adding a new one, or removing an existing one
        each changes the key
        """
        pre_mutate(project_dir)
        roots = _paths.project_roots()
        before = _cache.cache_key(roots)
        mutate(project_dir)
        assert _cache.cache_key(roots) != before

    def test_changes_when_the_cli_package_signature_changes(
        self, project_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that the key changes when `_cli_package_signature()` changes, so upgrading or editing
        `api_client_core.cli` itself (which can change the completion-tree schema) invalidates a
        cache produced by the previous code, rather than waiting for a project source file to change
        """
        roots = _paths.project_roots()
        before = _cache.cache_key(roots)
        monkeypatch.setattr(_cache, "_cli_package_signature", lambda: "a-different-signature")
        assert _cache.cache_key(roots) != before

    def test_changes_when_sys_prefix_changes(self, project_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that the key changes when `sys.prefix` (the active virtual environment's own root) changes,
        so switching to a different environment invalidates a cache built under another one, even when this
        package's own files happen to be byte-for-byte identical between the two (e.g. the same editable
        checkout linked from two different venvs) and a third-party dependency's own version is what actually
        differs
        """
        roots = _paths.project_roots()
        before = _cache.cache_key(roots)
        monkeypatch.setattr(_cache.sys, "prefix", "/some/other/venv")
        assert _cache.cache_key(roots) != before

    def test_catches_a_same_mtime_edit_via_file_size(self, project_dir: Path) -> None:
        """Test that an edit which happens to preserve mtime is still caught, via file size"""
        target = project_dir / "app.py"
        roots = _paths.project_roots()
        before = _cache.cache_key(roots)
        mtime_ns = target.stat().st_mtime_ns
        target.write_text("x = 22\n")
        os.utime(target, ns=(mtime_ns, mtime_ns))
        assert _cache.cache_key(roots) != before

    def test_ignores_files_under_a_skipped_directory(self, project_dir: Path) -> None:
        """Test that churn inside a skipped directory (e.g. .venv) never changes the key, so
        unrelated tooling activity (a pip install, a rebuild) can't force a spurious rebuild
        """
        roots = _paths.project_roots()
        before = _cache.cache_key(roots)
        venv_dir = project_dir / ".venv" / "lib"
        venv_dir.mkdir(parents=True)
        (venv_dir / "somepkg.py").write_text("z = 1\n")
        assert _cache.cache_key(roots) == before

    def test_ignores_a_venv_directory_regardless_of_its_name(self, project_dir: Path) -> None:
        """Test that a virtual environment is skipped by its `pyvenv.cfg` marker, not just by a name on
        the hardcoded `_SKIP_DIRS` list, so a venv named e.g. `env/` doesn't have its whole
        `site-packages` tree walked and stat'd on every discovery pass
        """
        roots = _paths.project_roots()
        before = _cache.cache_key(roots)
        venv_dir = project_dir / "env"
        site_packages = venv_dir / "lib" / "python3.x" / "site-packages"
        site_packages.mkdir(parents=True)
        (venv_dir / "pyvenv.cfg").write_text("home = /usr/bin\n")
        (site_packages / "somepkg.py").write_text("z = 1\n")
        assert _cache.cache_key(roots) == before

    def test_does_not_ignore_a_lookalike_directory_named_api_client_core(self, project_dir: Path) -> None:
        """Test that a project's own directory that happens to be named `api_client_core`, but isn't
        this framework's own installed package, still contributes to the key: `is_own_package_dir()`
        excludes this framework's real installed package by resolved path, not by that bare directory
        name, so a coincidentally-named project directory isn't silently skipped too
        """
        roots = _paths.project_roots()
        before = _cache.cache_key(roots)
        lookalike_dir = project_dir / "api_client_core"
        lookalike_dir.mkdir()
        (lookalike_dir / "somepkg.py").write_text("z = 1\n")
        assert _cache.cache_key(roots) != before


class TestIterSourceFiles:
    """Tests for `_iter_source_files()`, the deduplicating walk `cache_key()` builds on"""

    def test_does_not_enumerate_a_src_layout_file_twice(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that a file under `src/` is yielded once, not once per root, even though
        `project_roots()` returns both the project root and its `src/` subdirectory (and the
        project root's own walk already descends into `src/`)
        """
        project = tmp_path / "project"
        (project / "src" / "pkg").mkdir(parents=True)
        (project / "src" / "pkg" / "mod.py").write_text("x = 1\n")
        monkeypatch.chdir(project)

        paths = list(_cache._iter_source_files(_paths.project_roots()))
        assert paths.count(project / "src" / "pkg" / "mod.py") == 1


class TestCacheRoundtrip:
    """Tests for `save_cache()`/`load_cache()`"""

    def test_roundtrips_a_saved_tree(self, project_dir: Path, cache_home: Path) -> None:
        """Test that a tree saved under a key is returned by loading with the same key"""
        _cache.save_cache("some-key", _SAMPLE_TREE)
        assert _cache.load_cache("some-key") == _SAMPLE_TREE

    def test_empty_tree_is_a_valid_hit(self, project_dir: Path, cache_home: Path) -> None:
        """Test that an empty tree (a project with no discoverable clients) is a hit, not a miss"""
        _cache.save_cache("some-key", {})
        assert _cache.load_cache("some-key") == {}

    def test_missing_cache_file_is_a_miss(self, project_dir: Path, cache_home: Path) -> None:
        """Test that no cache file at all is reported as a miss"""
        assert _cache.load_cache("some-key") is None

    def test_key_mismatch_is_a_miss(self, project_dir: Path, cache_home: Path) -> None:
        """Test that a cache saved under a different (stale) key is reported as a miss"""
        _cache.save_cache("old-key", _SAMPLE_TREE)
        assert _cache.load_cache("new-key") is None

    def test_corrupt_cache_file_is_a_miss(self, project_dir: Path, cache_home: Path) -> None:
        """Test that unreadable/corrupt JSON is reported as a miss, not raised"""
        cache_file = _cache._cache_file()
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text("not json")
        assert _cache.load_cache("some-key") is None

    def test_isolates_by_project_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cache_home: Path
    ) -> None:
        """Test that two different project directories get distinct cache files, so completion in
        one project never serves another project's cache
        """
        project_a = tmp_path / "a"
        project_b = tmp_path / "b"
        project_a.mkdir()
        project_b.mkdir()

        monkeypatch.chdir(project_a)
        _cache.save_cache("k", _SAMPLE_TREE)

        monkeypatch.chdir(project_b)
        assert _cache.load_cache("k") is None

    def test_reuses_the_project_root_cache_from_a_nested_subdirectory(
        self, project_dir: Path, monkeypatch: pytest.MonkeyPatch, cache_home: Path
    ) -> None:
        """Test that completion from a subdirectory of the same project (e.g. `examples/dummyjson/`) resolves
        to the exact same cache file as the project root itself, so a save from one location is a hit from
        the other, rather than each cwd paying its own rebuild and storing its own duplicate copy.

        Regression test: `_cache_file()` used to key on the raw `Path.cwd()` while `cache_key()`/
        `project_roots()` were anchored to `find_project_root()`, so the two disagreed for any cwd other
        than the project root itself
        """
        root_cache_file = _cache._cache_file()
        _cache.save_cache("k", _SAMPLE_TREE)

        nested = project_dir / "examples" / "dummyjson"
        nested.mkdir(parents=True)
        monkeypatch.chdir(nested)

        assert _cache._cache_file() == root_cache_file
        assert _cache.load_cache("k") == _SAMPLE_TREE

    def test_unserializable_tree_does_not_raise(self, project_dir: Path, cache_home: Path) -> None:
        """Test that a tree `json.dumps` can't serialize is swallowed rather than raised, leaving the
        completion request to still complete from the in-memory tree it was called with instead of
        aborting before `argcomplete.autocomplete()` ever runs (see `_entrypoint._complete()`). The cache
        file is left unwritten, so the next request rebuilds instead of reading a corrupt one
        """
        _cache.save_cache("some-key", {"cli-test": object()})
        assert _cache.load_cache("some-key") is None


class TestPruneStaleCaches:
    """Tests for `_prune_stale_caches()`, the opportunistic sweep `save_cache()` runs to remove a cache file
    left behind by a project that hasn't had a completion request in a long time (e.g. one that no longer
    exists)
    """

    @pytest.mark.parametrize(
        ("age_seconds", "should_survive"),
        [
            pytest.param(_cache._STALE_CACHE_MAX_AGE_SECONDS + 1, False, id="older_than_max_age"),
            pytest.param(0, True, id="within_max_age"),
        ],
    )
    def test_prunes_based_on_the_cache_files_own_age(
        self, project_dir: Path, cache_home: Path, age_seconds: float, should_survive: bool
    ) -> None:
        """Test that a cache file last written more than `_STALE_CACHE_MAX_AGE_SECONDS` ago is removed, while
        one written more recently is kept
        """
        cache_dir = _cache._cache_file().parent
        cache_dir.mkdir(parents=True)
        cache_file = cache_dir / "completion-testproject0000.json"
        cache_file.write_text("{}")
        if age_seconds:
            old_time = time.time() - age_seconds
            os.utime(cache_file, (old_time, old_time))

        _cache._prune_stale_caches(cache_dir)

        assert cache_file.exists() is should_survive

    def test_skips_scanning_when_the_prune_sentinel_is_still_fresh(self, project_dir: Path, cache_home: Path) -> None:
        """Test that a stale cache file is left alone (the whole directory scan is skipped) when the prune
        sentinel's own mtime shows a scan already ran within `_PRUNE_CHECK_INTERVAL_SECONDS`, so this sweep
        runs at most roughly once per day rather than on every single completion request
        """
        cache_dir = _cache._cache_file().parent
        cache_dir.mkdir(parents=True)
        stale_file = cache_dir / "completion-abandoned0000000.json"
        stale_file.write_text("{}")
        old_time = time.time() - _cache._STALE_CACHE_MAX_AGE_SECONDS - 1
        os.utime(stale_file, (old_time, old_time))
        (cache_dir / _cache._PRUNE_SENTINEL_NAME).touch()

        _cache._prune_stale_caches(cache_dir)

        assert stale_file.exists()

    def test_save_cache_triggers_a_prune_sweep(self, project_dir: Path, cache_home: Path) -> None:
        """Test that `save_cache()` itself runs the prune sweep after a successful write, end-to-end"""
        cache_dir = _cache._cache_file().parent
        cache_dir.mkdir(parents=True)
        stale_file = cache_dir / "completion-abandoned0000000.json"
        stale_file.write_text("{}")
        old_time = time.time() - _cache._STALE_CACHE_MAX_AGE_SECONDS - 1
        os.utime(stale_file, (old_time, old_time))

        _cache.save_cache("some-key", _SAMPLE_TREE)

        assert not stale_file.exists()
        assert _cache.load_cache("some-key") == _SAMPLE_TREE

    def test_prunes_an_orphaned_tmp_file_left_by_an_interrupted_save(self, project_dir: Path, cache_home: Path) -> None:
        """Test that a `.tmp` file left behind by a `save_cache()` that never reached `replace()` (e.g. the
        process was killed between the write and the rename) is swept the same as a stale `.json` cache,
        since it can never be renamed into place after the fact and would otherwise accumulate forever
        """
        cache_dir = _cache._cache_file().parent
        cache_dir.mkdir(parents=True)
        orphaned_tmp = cache_dir / "completion-testproject0000.json.12345.tmp"
        orphaned_tmp.write_text("{}")
        old_time = time.time() - _cache._STALE_CACHE_MAX_AGE_SECONDS - 1
        os.utime(orphaned_tmp, (old_time, old_time))

        _cache._prune_stale_caches(cache_dir)

        assert not orphaned_tmp.exists()

    def test_sentinel_is_still_touched_when_one_file_vanishes_mid_scan(
        self, project_dir: Path, cache_home: Path, mocker: MockerFixture
    ) -> None:
        """Test that the prune sentinel is still touched (so the daily sweep doesn't re-run on every cache
        miss) even when one file's own `stat()` raises mid-scan, e.g. because a concurrent process already
        removed it
        """
        cache_dir = _cache._cache_file().parent
        cache_dir.mkdir(parents=True)
        vanished = cache_dir / "completion-vanished000000.json"
        vanished.write_text("{}")
        survivor = cache_dir / "completion-survivor00000.json"
        survivor.write_text("{}")

        real_stat = Path.stat

        def flaky_stat(self: Path, *args: Any, **kwargs: Any) -> os.stat_result:
            if self == vanished:
                raise OSError("No such file or directory")
            return real_stat(self, *args, **kwargs)

        mocker.patch.object(Path, "stat", flaky_stat)

        _cache._prune_stale_caches(cache_dir)

        assert (cache_dir / _cache._PRUNE_SENTINEL_NAME).exists()


class TestCompletionRegisteredMarker:
    """Tests for `mark_completion_registered()`/`is_completion_registered()`, the global marker recording
    that a real shell-completion request has been served at least once
    """

    def test_not_registered_before_the_marker_is_ever_touched(self, cache_home: Path) -> None:
        """Test that registration is reported as not yet done when the marker file doesn't exist"""
        assert not _cache.is_completion_registered()

    def test_registered_once_the_marker_is_touched(self, cache_home: Path) -> None:
        """Test that registration is reported as done immediately after `mark_completion_registered()`"""
        _cache.mark_completion_registered()
        assert _cache.is_completion_registered()

    def test_marker_is_not_scoped_to_a_project(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that the marker is a single global file, not one per project (unlike the completion cache
        itself), since whether the `eval` line is active in the current shell doesn't depend on which
        project is being completed in
        """
        cache_home = tmp_path / "cache-home"
        monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
        project_a, project_b = tmp_path / "a", tmp_path / "b"
        project_a.mkdir()
        project_b.mkdir()

        monkeypatch.chdir(project_a)
        _cache.mark_completion_registered()

        monkeypatch.chdir(project_b)
        assert _cache.is_completion_registered()

    def test_silently_gives_up_when_the_cache_directory_cant_be_created(
        self, cache_home: Path, mocker: MockerFixture
    ) -> None:
        """Test that a failure creating the cache directory (e.g. read-only) is swallowed rather than
        raised, matching every other write in this module
        """
        mocker.patch.object(Path, "mkdir", side_effect=OSError("Permission denied"))
        _cache.mark_completion_registered()
        assert not _cache.is_completion_registered()

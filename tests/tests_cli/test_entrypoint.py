"""Unit tests for `api_client_core.cli._entrypoint` (the `api-client` console entry point).

Exercises the shell-completion hot path in isolation from `api_client_core`'s heavier submodules
(`.base`/`.endpoints`, which pull in `httpx2`): completion-request routing, the parser rebuilt from a cached
tree, and `main()`'s own top-level exit-code handling. See `_entrypoint`'s module docstring for why this hot
path must never import that heavy chain. The on-disk completion cache itself (key computation, load/save,
pruning) is tested separately in `test_cache.py`.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from common_libs.ansi_colors import remove_color_code
from pytest_mock import MockerFixture

import api_client_core
from api_client_core.cli import _cache as cache
from api_client_core.cli import _entrypoint as entrypoint
from api_client_core.cli import _paths
from api_client_core.cli.builder import _serialize_options, build_client_parser, build_completion_entry

from .conftest import CliTestClient, get_subparsers_action

# `argcomplete` is only pulled in via the optional `cli-completion` extra (see pyproject.toml), so this whole
# module is skipped, not a collection error, when it isn't installed. Must run before the import below, the
# only reason it can't sit with the rest of the module's imports above.
pytest.importorskip("argcomplete", reason="requires the 'cli-completion' extra")
from argcomplete.finders import CompletionFinder


class _SafeCompletionFinder(CompletionFinder):
    """A `CompletionFinder` that skips argcomplete's fd-9 debug stream.

    `CompletionFinder._init_debug_stream()` unconditionally opens file descriptor 9 for debug
    output (falling back to `stderr` only if that open fails), a descriptor pytest's own capturing
    can leave in a state that later gets reused and closed out from under it. Its own docstring
    calls out overriding it as the sanctioned way to avoid exactly this clash when testing under
    pytest; debug output isn't needed here since `_ARC_DEBUG` isn't set in the test environment.
    """

    def _init_debug_stream(self) -> None:
        pass


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
                        {"opts": ["--with-expected-status"], "choices": None, "nargs": "+"},
                    ],
                    "create-widget": [
                        {"opts": ["--active", "--no-active"], "choices": None, "nargs": 0},
                        {"opts": ["--priority"], "choices": [1, 2, 3], "nargs": None},
                        {"opts": ["--metadata"], "choices": None, "nargs": None, "is_json_file": True},
                    ],
                    "upload-avatar": [
                        {"opts": ["--avatar"], "choices": None, "nargs": None, "is_file": True},
                    ],
                },
            }
        },
    }
}
"""A tree matching `build_completion_tree()`'s schema, standing in for a real cached tree."""


def _complete(tree: dict[str, Any], line: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    """Return argcomplete's completions for `line` (a full `COMP_LINE`, cursor at the end).

    Drives the real `argcomplete.autocomplete()` entry point (the same one `entrypoint._complete()`
    calls, including its `default_completer=` override) against a fresh parser
    rebuilt from `tree`, with `exit_method` swapped for a no-op so completion doesn't terminate the
    test process, and output captured to a temp file instead of the shell's fd 8.

    :param tree: Completion tree to rebuild a parser from, via `_build_parser_from_tree()`
    :param line: Full command line typed so far
    :param monkeypatch: pytest fixture, used to set the completion-request env vars
    :param tmp_path: Temp directory to write the completion output file under
    """
    monkeypatch.setenv("_ARGCOMPLETE", "1")
    monkeypatch.setenv("COMP_LINE", line)
    monkeypatch.setenv("COMP_POINT", str(len(line)))
    out_file = tmp_path / "completions.out"
    parser = entrypoint._build_parser_from_tree(tree)
    with out_file.open("w") as stream:
        _SafeCompletionFinder()(
            parser, exit_method=lambda code: None, output_stream=stream, default_completer=lambda **kwargs: []
        )
    raw = out_file.read_text()
    return raw.split("\x0b") if raw else []


def _completion_surface(parser: argparse.ArgumentParser, prefix: str = "") -> dict[str, list[dict[str, Any]]]:
    """Recursively extract every parser/subparser's own flags (as `OptSpec`-shaped dicts, minus
    `is_file`/`is_json_file`), keyed by dotted path from `parser`, for comparing two differently-built
    parser trees by their actual completion surface (option strings and choices) rather than by object
    identity.

    `is_file`/`is_json_file` are excluded from the comparison on purpose: a parser rebuilt by
    `_build_parser_from_tree()` records that information as a live `argcomplete` completer
    (`_at_file_completer`/`FilesCompleter`), not as the marker attribute `_serialize_options()` reads back,
    so it can never round-trip back out of a rebuilt parser the same way it went in. That's by design, not
    a gap: the information only ever needs to flow one way, into the rebuilt parser.

    :param parser: App-level, resource-level, or leaf command parser to extract flags from
    :param prefix: Dotted path already walked to reach `parser`, used as this level's own key
    """
    result: dict[str, list[dict[str, Any]]] = {
        prefix: [
            {k: v for k, v in spec.items() if k not in ("is_file", "is_json_file")}
            for spec in _serialize_options(parser)
        ]
    }
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub_parser in action.choices.items():
                result.update(_completion_surface(sub_parser, f"{prefix}.{name}" if prefix else name))
    return result


class TestFindProjectRoot:
    """Tests for `find_project_root()`, which anchors `project_roots()` to the project root instead of
    the raw current working directory
    """

    def test_resolves_the_project_root_itself(self, project_dir: Path) -> None:
        """Test that a directory holding a project marker (pyproject.toml) resolves to itself"""
        assert entrypoint.find_project_root(project_dir) == project_dir

    def test_resolves_from_a_nested_subdirectory(self, project_dir: Path) -> None:
        """Test that the search walks upward, so a deeply nested subdirectory (e.g. examples/dummyjson/)
        still resolves to the enclosing project root rather than finding nothing
        """
        nested = project_dir / "examples" / "dummyjson"
        nested.mkdir(parents=True)
        assert entrypoint.find_project_root(nested) == project_dir

    def test_returns_none_when_no_marker_is_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that a directory tree with no project marker anywhere above it resolves to None, so
        `project_roots()` falls back to cwd instead of finding an unrelated ancestor project by
        accident.

        Patches the marker list to a filename no real directory could plausibly contain, rather than
        relying on the test machine's actual filesystem never having e.g. a stray `.git` above `tmp_path`
        """
        monkeypatch.setattr(_paths, "_PROJECT_MARKERS", ("__no-such-marker-ever__",))
        unrelated = tmp_path / "unrelated" / "nested"
        unrelated.mkdir(parents=True)
        assert entrypoint.find_project_root(unrelated) is None


class TestIsOwnPackageDir:
    """Tests for `is_own_package_dir()`, which excludes this installed `api_client_core` package from
    project discovery by its resolved path rather than by its bare directory name (unlike the generic
    names in `_SKIP_DIRS`), so a project's own differently-purposed directory that happens to share that
    literal name isn't silently excluded too.
    """

    def test_matches_this_installed_package_s_own_root_directory(self) -> None:
        """Test that the real, currently-running `api_client_core` package's own root directory is
        recognized as itself
        """
        own_root = Path(api_client_core.__file__).parent
        assert _paths.is_own_package_dir(own_root)

    def test_does_not_match_a_lookalike_directory_of_the_same_name(self, tmp_path: Path) -> None:
        """Test that a project's own directory named `api_client_core`, but not this installed package,
        isn't mistaken for it: the check is by resolved path, not by name
        """
        lookalike = tmp_path / "api_client_core"
        lookalike.mkdir()
        assert not _paths.is_own_package_dir(lookalike)


class TestIsVenvDir:
    """Tests for `is_venv_dir()`, which identifies a virtual environment by its `pyvenv.cfg` marker rather
    than by a name on `_SKIP_DIRS`, so a venv named e.g. `env/` is still recognized.
    """

    def test_matches_a_directory_holding_pyvenv_cfg(self, tmp_path: Path) -> None:
        """Test that a directory containing `pyvenv.cfg` is recognized as a venv root, regardless of its
        own name
        """
        venv = tmp_path / "env"
        venv.mkdir()
        (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
        assert _paths.is_venv_dir(venv)

    def test_does_not_match_an_ordinary_directory(self, tmp_path: Path) -> None:
        """Test that a directory with no `pyvenv.cfg` is not mistaken for a venv root"""
        ordinary = tmp_path / "src"
        ordinary.mkdir()
        assert not _paths.is_venv_dir(ordinary)


class TestBuildParserFromTree:
    """Tests for `_build_parser_from_tree()`, the exact inverse of `builder.build_completion_tree()`"""

    def test_completes_app_names(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Test that the top level completes discovered app names"""
        assert "cli-test" in _complete(_SAMPLE_TREE, "api-client ", monkeypatch, tmp_path)

    def test_completes_version_flag_at_the_top_level(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Test that --version completes at the top level even though it isn't part of the tree at all
        (unlike every other flag, it belongs to `api-client` itself, not to any discovered client, so
        `build_completion_tree()` has nothing to serialize it into - see `_build_parser_from_tree()`'s own
        docstring). An empty tree is used deliberately, so this only passes if --version was added directly
        rather than happening to come from some other flag's own serialized entry
        """
        assert "--version" in _complete({}, "api-client --", monkeypatch, tmp_path)

    def test_completes_resource_names_and_app_level_opts(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Test that after an app name, both its resources and its app-level --base-url flag complete
        (--base-url is added directly to the app parser, not nested under a subparser)
        """
        completions = _complete(_SAMPLE_TREE, "api-client cli-test ", monkeypatch, tmp_path)
        assert "widgets" in completions
        assert "--base-url" in completions

    def test_completes_command_names(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Test that after a resource name, its commands complete"""
        completions = _complete(_SAMPLE_TREE, "api-client cli-test widgets ", monkeypatch, tmp_path)
        assert {"get-widget", "create-widget"} <= set(completions)

    def test_completes_resource_level_opts(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Test that after a resource name, its own --base-url flag completes too (added directly to the
        resource parser, distinct from each command's own flags, see `builder._add_global_arguments()`)
        """
        completions = _complete(_SAMPLE_TREE, "api-client cli-test widgets --", monkeypatch, tmp_path)
        assert "--base-url" in completions

    def test_completes_command_flags(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Test that after a command name, its flags complete"""
        completions = _complete(_SAMPLE_TREE, "api-client cli-test widgets get-widget --", monkeypatch, tmp_path)
        assert "--widget-id" in completions
        assert "--quiet" in completions

    def test_boolean_flag_does_not_consume_the_next_token(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Test that a zero-arg (nargs=0) flag doesn't swallow the next word as its value, so the
        flag that follows it still completes as a flag rather than falling through to file
        completion
        """
        line = "api-client cli-test widgets create-widget --active --"
        completions = _complete(_SAMPLE_TREE, line, monkeypatch, tmp_path)
        assert "--priority" in completions

    def test_repeatable_flag_keeps_its_own_nargs_rather_than_becoming_single_valued(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Test that a repeatable flag's rebuilt action keeps its own real nargs ("+", here), rather than
        every value-taking flag degrading to a plain single value (nargs=None) regardless of its own arity

        Regression test: before OptSpec tracked the real nargs, a flag like --with-expected-status (which
        accepts one or more CODE values in the same occurrence) was rebuilt as a plain single-value flag,
        unable to accept a second value in the same occurrence at all
        """
        parser = entrypoint._build_parser_from_tree(_SAMPLE_TREE)
        widgets_parser = get_subparsers_action(parser).choices["cli-test"]
        resource_parser = get_subparsers_action(widgets_parser).choices["widgets"]
        command_parser = get_subparsers_action(resource_parser).choices["get-widget"]
        action = next(a for a in command_parser._actions if "--with-expected-status" in a.option_strings)
        assert action.nargs == "+"

    def test_literal_choices_complete_as_values(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Test that a flag's cached Literal choices complete as its value"""
        line = "api-client cli-test widgets create-widget --priority "
        assert set(_complete(_SAMPLE_TREE, line, monkeypatch, tmp_path)) == {"1", "2", "3"}

    def test_plain_value_flag_offers_no_completions(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Test that a flag with no choices and no is_file marker (an ordinary str/int/etc. param)
        gets no value completions, rather than argcomplete's default fallback to path completion
        (which is misleading for a flag like --widget-id or --username)
        """
        monkeypatch.chdir(tmp_path)
        (tmp_path / "should-not-appear.txt").write_text("x")
        line = "api-client cli-test widgets get-widget --widget-id "
        assert _complete(_SAMPLE_TREE, line, monkeypatch, tmp_path) == []

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="argcomplete's FilesCompleter shells out to bash's compgen, unreliable on native Windows CI runners",
    )
    def test_is_file_flag_still_completes_paths(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Test that an is_file-marked flag (a File-typed endpoint param) keeps real path
        completion, the one case where completing to a filesystem path is actually correct
        """
        monkeypatch.chdir(tmp_path)
        (tmp_path / "avatar.png").write_text("x")
        line = "api-client cli-test widgets upload-avatar --avatar "
        assert "avatar.png" in _complete(_SAMPLE_TREE, line, monkeypatch, tmp_path)

    def test_is_json_file_flag_offers_no_completion_without_at_prefix(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Test that an is_json_file-marked flag (a JSON-typed endpoint param) offers no path completion for
        an ordinary, non-`@`-prefixed value, since that's inline JSON or `-` for stdin, not a path
        """
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data.json").write_text("{}")
        line = "api-client cli-test widgets create-widget --metadata "
        assert _complete(_SAMPLE_TREE, line, monkeypatch, tmp_path) == []

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="argcomplete's FilesCompleter shells out to bash's compgen, unreliable on native Windows CI runners",
    )
    def test_is_json_file_flag_completes_paths_once_at_is_typed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Test that once `@` is typed, an is_json_file-marked flag completes to real filesystem paths,
        each still prefixed with `@` so the completed token stays a valid `@<path>` value

        Matches against two candidate files rather than one, so the assertion doesn't depend on
        argcomplete's own trailing-space-on-a-unique-match behavior.
        """
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data.json").write_text("{}")
        (tmp_path / "data2.json").write_text("{}")
        line = "api-client cli-test widgets create-widget --metadata @data"
        assert {"@data.json", "@data2.json"} <= set(_complete(_SAMPLE_TREE, line, monkeypatch, tmp_path))


class TestBuildParserFromTreePruning:
    """Tests for `_build_parser_from_tree()`'s `prune_to` parameter, which skips fully materializing a branch
    a completion request can't possibly need: `_complete()`'s own real caller resolves it from the tokens
    already typed (see `TestTypedAppAndResource`), keeping a warm completion's rebuild cost independent of a
    client's total command count rather than linear in it.
    """

    def test_prune_to_none_builds_the_full_tree(self) -> None:
        """Test that the default prune_to=None builds every app's own flags, resources, and commands in
        full, exactly as before pruning existed
        """
        parser = entrypoint._build_parser_from_tree(_SAMPLE_TREE)
        app_parser = get_subparsers_action(parser).choices["cli-test"]
        assert "--base-url" in {o for a in app_parser._actions for o in a.option_strings}
        resource_parser = get_subparsers_action(app_parser).choices["widgets"]
        assert "get-widget" in get_subparsers_action(resource_parser).choices

    def test_prune_to_with_nothing_typed_registers_only_bare_app_names(self) -> None:
        """Test that prune_to=(None, None) - a completion request before any app name has been typed -
        registers every app's own name as a valid choice, but builds none of its flags or resources, since
        argcomplete can never walk into a subparser the input hasn't selected yet
        """
        parser = entrypoint._build_parser_from_tree(_SAMPLE_TREE, prune_to=(None, None))
        app_subparsers = get_subparsers_action(parser)
        assert "cli-test" in app_subparsers.choices
        app_parser = app_subparsers.choices["cli-test"]
        assert "--base-url" not in {o for a in app_parser._actions for o in a.option_strings}
        assert not any(isinstance(a, argparse._SubParsersAction) for a in app_parser._actions)

    def test_prune_to_with_app_typed_builds_that_app_but_not_its_resources(self) -> None:
        """Test that prune_to=("cli-test", None) builds the matching app's own flags and registers every one
        of its resources' bare names, but doesn't build any resource's own flags or commands yet
        """
        parser = entrypoint._build_parser_from_tree(_SAMPLE_TREE, prune_to=("cli-test", None))
        app_parser = get_subparsers_action(parser).choices["cli-test"]
        assert "--base-url" in {o for a in app_parser._actions for o in a.option_strings}
        resource_subparsers = get_subparsers_action(app_parser)
        assert "widgets" in resource_subparsers.choices
        resource_parser = resource_subparsers.choices["widgets"]
        assert not any(isinstance(a, argparse._SubParsersAction) for a in resource_parser._actions)

    def test_prune_to_with_app_and_resource_typed_builds_the_full_branch(self) -> None:
        """Test that prune_to=("cli-test", "widgets") builds the matching resource's own flags and every one
        of its commands in full, exactly as the unpruned build does for that one branch
        """
        parser = entrypoint._build_parser_from_tree(_SAMPLE_TREE, prune_to=("cli-test", "widgets"))
        app_parser = get_subparsers_action(parser).choices["cli-test"]
        resource_parser = get_subparsers_action(app_parser).choices["widgets"]
        command_subparsers = get_subparsers_action(resource_parser)
        assert {"get-widget", "create-widget", "upload-avatar"} <= set(command_subparsers.choices)
        command_parser = command_subparsers.choices["get-widget"]
        assert "--widget-id" in {o for a in command_parser._actions for o in a.option_strings}

    def test_prune_to_with_an_untyped_app_still_registers_its_bare_name(self) -> None:
        """Test that an app not matching prune_to's own app name still gets its bare name registered as a
        valid completion choice, even though none of its own flags or resources are built - the case that
        keeps every sibling app choosable while only the one actually being completed costs anything to build
        """
        parser = entrypoint._build_parser_from_tree(_SAMPLE_TREE, prune_to=("some-other-app", None))
        app_subparsers = get_subparsers_action(parser)
        assert "cli-test" in app_subparsers.choices
        app_parser = app_subparsers.choices["cli-test"]
        assert not any(isinstance(a, argparse._SubParsersAction) for a in app_parser._actions)


class TestTypedAppAndResource:
    """Tests for `_typed_app_and_resource()`, the best-effort `COMP_LINE` peek `_complete()` resolves
    `_build_parser_from_tree()`'s own `prune_to` from.
    """

    def test_returns_none_and_none_when_nothing_is_typed_yet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that a completion request right after the program name resolves to (None, None), so the
        rebuild only registers bare app names
        """
        line = "api-client "
        monkeypatch.setenv("_ARGCOMPLETE", "1")
        monkeypatch.setenv("COMP_LINE", line)
        monkeypatch.setenv("COMP_POINT", str(len(line)))
        assert entrypoint._typed_app_and_resource() == (None, None)

    def test_returns_the_app_name_once_typed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that an app name already typed, with the resource name still in progress, resolves the app
        name alone
        """
        line = "api-client cli-test wid"
        monkeypatch.setenv("_ARGCOMPLETE", "1")
        monkeypatch.setenv("COMP_LINE", line)
        monkeypatch.setenv("COMP_POINT", str(len(line)))
        assert entrypoint._typed_app_and_resource() == ("cli-test", None)

    def test_returns_the_app_and_resource_name_once_both_are_typed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that both an app and resource name already typed resolve together, regardless of how much
        more follows (a command name being completed, or already complete)
        """
        line = "api-client cli-test widgets get-widget --"
        monkeypatch.setenv("_ARGCOMPLETE", "1")
        monkeypatch.setenv("COMP_LINE", line)
        monkeypatch.setenv("COMP_POINT", str(len(line)))
        assert entrypoint._typed_app_and_resource() == ("cli-test", "widgets")

    def test_skips_a_global_value_flag_given_ahead_of_the_app_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that --base-url/--log-level (and its value) given ahead of the app name is skipped over,
        mirroring dispatch.py's own _peek_app_name(), rather than being mistaken for the app name itself
        """
        line = "api-client --log-level DEBUG cli-test wid"
        monkeypatch.setenv("_ARGCOMPLETE", "1")
        monkeypatch.setenv("COMP_LINE", line)
        monkeypatch.setenv("COMP_POINT", str(len(line)))
        assert entrypoint._typed_app_and_resource() == ("cli-test", None)

    def test_stops_at_an_unrecognized_flag_rather_than_guessing_further(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that an unrecognized flag ahead of the app name stops the peek rather than misreading a
        later token as the app name, so the caller falls back to building more of the tree than strictly
        needed instead of guessing wrong
        """
        line = "api-client --verbose cli-test "
        monkeypatch.setenv("_ARGCOMPLETE", "1")
        monkeypatch.setenv("COMP_LINE", line)
        monkeypatch.setenv("COMP_POINT", str(len(line)))
        assert entrypoint._typed_app_and_resource() == (None, None)

    def test_returns_none_on_malformed_completion_request_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that a missing env var this function relies on - which should never happen inside a real
        completion request, but this is a best-effort peek that must never raise - falls back to None, so
        the caller builds the whole tree unpruned rather than risking an incorrect prune
        """
        monkeypatch.setenv("_ARGCOMPLETE", "1")
        monkeypatch.setenv("COMP_LINE", "api-client ")
        monkeypatch.delenv("COMP_POINT", raising=False)
        assert entrypoint._typed_app_and_resource() is None


class TestBuildCompletionTreeRoundTrip:
    """Tests that `builder.build_completion_tree()` and `_build_parser_from_tree()` are exact inverses of
    each other for a real client's own real parser, not just for the hand-written `_SAMPLE_TREE` every
    other test in this module uses as a stand-in. `_SAMPLE_TREE` is maintained by hand and could silently
    drift from what `build_completion_tree()` actually emits; this test instead builds a completion tree
    entry the same way the real cache does, and checks the parser rebuilt from it against the real parser
    `build_client_parser()` builds directly for that same client.
    """

    def test_a_rebuilt_parser_exposes_the_same_flags_and_choices_as_the_real_one(
        self, cli_client_class: type[CliTestClient]
    ) -> None:
        """Test that a parser rebuilt from `build_completion_entry()`'s own tree entry for a real client
        exposes the same option strings and choices, at every level (app, each resource, each command),
        as the real parser `build_client_parser()` produces directly for that same client
        """
        real_parser = build_client_parser(cli_client_class)
        entry = build_completion_entry(cli_client_class)

        rebuilt_top = entrypoint._build_parser_from_tree({"cli-test": entry})
        rebuilt_app_parser = get_subparsers_action(rebuilt_top).choices["cli-test"]

        assert _completion_surface(rebuilt_app_parser) == _completion_surface(real_parser)


class TestHotPathAvoidsHeavyImports:
    """Tests that a cache-hit completion never imports `api_client_core`'s heavy chain (`.base`/`.endpoints`, and
    therefore `httpx2`), which is the whole point of `entry` staying stdlib+argcomplete-only on the hot path
    """

    def test_cache_hit_never_imports_the_heavy_chain(self, tmp_path: Path) -> None:
        """Test that reaching a cache hit, the common case, never triggers `api_client_core.base`,
        `api_client_core.endpoints`, or `httpx2`'s import in a fresh process

        Must run in a subprocess: by the time any other test runs, `api_client_core.base` and
        `.endpoints` are already imported (via conftest.py), so there is no in-process way to observe
        this.
        """
        script = textwrap.dedent(f"""
            import os
            import sys

            os.chdir({str(tmp_path)!r})
            os.environ["XDG_CACHE_HOME"] = {str(tmp_path / "cache-home")!r}

            import api_client_core.cli._entrypoint as entrypoint

            key = entrypoint.cache_key(entrypoint.project_roots())
            entrypoint.save_cache(key, {{"cli-test": {{"opts": [], "resources": {{}}}}}})

            tree = entrypoint.load_cache(key)
            assert tree is not None, "expected a cache hit"
            entrypoint._build_parser_from_tree(tree)

            assert "httpx2" not in sys.modules, sorted(sys.modules)
            assert "api_client_core.base" not in sys.modules, sorted(sys.modules)
            assert "api_client_core.endpoints" not in sys.modules, sorted(sys.modules)
            print("OK")
            """)
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout

    def test_complete_on_a_cache_hit_never_imports_the_heavy_chain(self, tmp_path: Path) -> None:
        """Test that `_complete()` itself, not just `load_cache()`/`_build_parser_from_tree()` in
        isolation, stays on the light path for an ordinary cache-hit request.

        Must run in a subprocess: by the time any other test runs, `api_client_core.base` and `.endpoints`
        are already imported (via conftest.py), so there is no in-process way to observe this.
        """
        script = textwrap.dedent(f"""
            import os
            import sys
            from unittest.mock import patch

            os.chdir({str(tmp_path)!r})
            os.environ["XDG_CACHE_HOME"] = {str(tmp_path / "cache-home")!r}
            os.environ["_ARGCOMPLETE"] = "1"
            os.environ["COMP_LINE"] = "api-client cli-test "
            os.environ["COMP_POINT"] = "19"

            import api_client_core.cli._entrypoint as entrypoint
            import argcomplete

            key = entrypoint.cache_key(entrypoint.project_roots())
            entrypoint.save_cache(key, {{"cli-test": {{"opts": [], "resources": {{}}}}}})

            with patch.object(argcomplete, "autocomplete"):
                entrypoint._complete()

            assert "httpx2" not in sys.modules, sorted(sys.modules)
            assert "api_client_core.base" not in sys.modules, sorted(sys.modules)
            assert "api_client_core.endpoints" not in sys.modules, sorted(sys.modules)
            print("OK")
            """)
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout


class TestSilenceStreams:
    """Tests for `_silence_streams()`, used around a completion cache-miss rebuild so a project module's own
    import-time output never reaches the terminal mid-completion.
    """

    def test_suppresses_writes_to_the_real_stdout_and_stderr_file_descriptors(
        self, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """Test that a write to the real fd 1/2 - not just the `sys.stdout`/`sys.stderr` objects, which
        `reserve_stdout()` may have already repointed elsewhere - is discarded for the duration of the block,
        and that both descriptors write normally again once it ends
        """
        with entrypoint._silence_streams():
            os.write(1, b"stdout during silence\n")
            os.write(2, b"stderr during silence\n")
        os.write(1, b"stdout after silence\n")
        os.write(2, b"stderr after silence\n")

        captured = capfd.readouterr()
        assert "during silence" not in captured.out
        assert "during silence" not in captured.err
        assert "stdout after silence" in captured.out
        assert "stderr after silence" in captured.err


class TestComplete:
    """Tests for `_complete()`'s top-level exception guard"""

    def test_silences_import_time_output_from_a_cache_miss_rebuild(
        self,
        project_dir: Path,
        cache_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        mocker: MockerFixture,
        capfd: pytest.CaptureFixture[str],
    ) -> None:
        """Test that a project module's own import-time output (a bare `print()`, here) never reaches the
        terminal during a completion cache-miss rebuild, which imports every discoverable project module.

        Regression test: `reserve_stdout()` only repoints the `sys.stdout` object, so a bare `print()`
        during discovery still landed on the real stderr - visible on the user's terminal mid-completion -
        until the rebuild started redirecting the real file descriptors too, via `_silence_streams()`
        """
        (project_dir / "noisy.py").write_text("print('NOISY STDOUT AT IMPORT')\n")
        monkeypatch.setenv("_ARGCOMPLETE", "1")
        monkeypatch.setenv("COMP_LINE", "api-client ")
        monkeypatch.setenv("COMP_POINT", "11")
        mocker.patch("argcomplete.autocomplete")

        entrypoint._complete()

        assert "NOISY STDOUT AT IMPORT" not in capfd.readouterr().out

    def test_is_a_no_op_when_cwd_is_outside_any_project(
        self,
        tmp_path: Path,
        cache_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        mocker: MockerFixture,
    ) -> None:
        """Test that a completion request from a directory with no project marker anywhere above it
        returns immediately without ever building (or trying to cache) a completion tree, rather than
        walking that directory's entire tree just to compute a cache key for it.

        Regression test: before this check, a completion request from e.g. the user's home directory
        walked every file beneath it (tens of thousands on a real machine) on every single TAB press
        """
        monkeypatch.setattr(_paths, "_PROJECT_MARKERS", ("__no-such-marker-ever__",))
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("_ARGCOMPLETE", "1")
        monkeypatch.setenv("COMP_LINE", "api-client ")
        monkeypatch.setenv("COMP_POINT", "11")
        mock_build_tree = mocker.patch("api_client_core.cli.builder.build_completion_tree")

        entrypoint._complete()  # must not raise

        mock_build_tree.assert_not_called()

    def test_marks_completion_as_registered_even_outside_a_project(
        self, tmp_path: Path, cache_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that the completion-registered marker is still touched even when the current directory is
        outside any project, since reaching this point with `_ARGCOMPLETE` set and `argcomplete` importable
        is itself already proof that `eval "$(register-python-argcomplete ...)"` is active in this shell,
        regardless of whether this particular directory happens to hold a project
        """
        monkeypatch.setattr(_paths, "_PROJECT_MARKERS", ("__no-such-marker-ever__",))
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("_ARGCOMPLETE", "1")
        monkeypatch.setenv("COMP_LINE", "api-client ")
        monkeypatch.setenv("COMP_POINT", "11")

        entrypoint._complete()

        assert cache.is_completion_registered()

    def test_marks_completion_as_registered_on_a_real_completion_request(
        self, project_dir: Path, cache_home: Path, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture
    ) -> None:
        """Test that a real, successful completion request marks completion as registered, so
        the tab-completion setup tip stops suggesting the `eval "$(register-python-argcomplete ...)"` setup step

        `argcomplete.autocomplete()` itself is mocked out: by default it writes to real fd 8 and exits the
        process, which is unsafe under pytest's own fd capturing (see `_SafeCompletionFinder`'s docstring
        above) - this test only needs `_complete()` to reach that call successfully, not what it does
        """
        monkeypatch.setenv("_ARGCOMPLETE", "1")
        monkeypatch.setenv("COMP_LINE", "api-client ")
        monkeypatch.setenv("COMP_POINT", "11")
        mocker.patch("argcomplete.autocomplete")
        assert not cache.is_completion_registered()

        entrypoint._complete()

        assert cache.is_completion_registered()

    def test_does_not_mark_completion_as_registered_when_argcomplete_is_missing(
        self, project_dir: Path, cache_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that the marker is left untouched when `argcomplete` isn't importable, since reaching
        `_complete()` with `_ARGCOMPLETE` set proves nothing about a real shell registration in that case
        """
        monkeypatch.setenv("_ARGCOMPLETE", "1")
        monkeypatch.setitem(sys.modules, "argcomplete", None)

        entrypoint._complete()

        assert not cache.is_completion_registered()

    def test_swallows_an_exception_from_building_the_completion_tree(
        self,
        project_dir: Path,
        cache_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        mocker: MockerFixture,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Test that a completion request never lets an exception escape to a traceback on the user's
        terminal: a shell polls this on every keystroke, so anything `build_completion_tree()` itself
        doesn't already tolerate (e.g. `discover_clients()` raising directly, rather than one of the
        per-candidate failures it already catches) must still be a silent no-op here, not a crash
        """
        monkeypatch.setenv("_ARGCOMPLETE", "1")
        monkeypatch.setenv("COMP_LINE", "api-client ")
        monkeypatch.setenv("COMP_POINT", "11")
        mocker.patch("api_client_core.cli.builder.build_completion_tree", side_effect=RuntimeError("simulated failure"))

        entrypoint._complete()  # must not raise

        assert "simulated failure" not in capsys.readouterr().err

    def test_reraises_under_arc_debug_instead_of_swallowing(
        self,
        project_dir: Path,
        cache_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        mocker: MockerFixture,
    ) -> None:
        """Test that the same failure a plain completion request swallows silently is instead re-raised
        (a real traceback) once `_ARC_DEBUG` is set, since that env var's whole purpose is letting a user
        who set it specifically to debug a missing completion actually see why, rather than the request
        completing silently with no completions offered regardless of what went wrong.

        Regression test: `_complete()` used to swallow every exception unconditionally, defeating
        `_ARC_DEBUG` for the specific case of an outright bug in the rebuild path
        """
        monkeypatch.setenv("_ARGCOMPLETE", "1")
        monkeypatch.setenv("_ARC_DEBUG", "1")
        monkeypatch.setenv("COMP_LINE", "api-client ")
        monkeypatch.setenv("COMP_POINT", "11")
        mocker.patch("api_client_core.logging.setup_logging")
        mocker.patch("api_client_core.cli.builder.build_completion_tree", side_effect=RuntimeError("simulated failure"))

        with pytest.raises(RuntimeError, match="simulated failure"):
            entrypoint._complete()

    def test_prunes_the_rebuilt_parser_using_the_tokens_already_typed(
        self, project_dir: Path, cache_home: Path, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture
    ) -> None:
        """Test that a real completion request resolves `prune_to` from the tokens already typed
        (`_typed_app_and_resource()`) and threads it through to `_build_parser_from_tree()`, rather than
        always building the whole tree unpruned. This is the wiring that keeps a large client's warm
        completion latency independent of its total command count rather than linear in it
        """
        line = "api-client cli-test "
        monkeypatch.setenv("_ARGCOMPLETE", "1")
        monkeypatch.setenv("COMP_LINE", line)
        monkeypatch.setenv("COMP_POINT", str(len(line)))
        mocker.patch("argcomplete.autocomplete")
        spy = mocker.spy(entrypoint, "_build_parser_from_tree")

        entrypoint._complete()

        spy.assert_called_once()
        assert spy.call_args.kwargs["prune_to"] == ("cli-test", None)

    def test_does_not_prune_under_arc_debug(
        self, project_dir: Path, cache_home: Path, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture
    ) -> None:
        """Test that `_ARC_DEBUG` disables pruning, since a user debugging a missing completion needs the
        whole tree available to inspect, not just the branch this module's own best-effort peek considers
        relevant
        """
        line = "api-client cli-test "
        monkeypatch.setenv("_ARGCOMPLETE", "1")
        monkeypatch.setenv("_ARC_DEBUG", "1")
        monkeypatch.setenv("COMP_LINE", line)
        monkeypatch.setenv("COMP_POINT", str(len(line)))
        mocker.patch("api_client_core.logging.setup_logging")
        mocker.patch("argcomplete.autocomplete")
        spy = mocker.spy(entrypoint, "_build_parser_from_tree")

        entrypoint._complete()

        assert spy.call_args.kwargs["prune_to"] is None


class TestCompleteDebugLogging:
    """Tests for `_complete()`'s `_ARC_DEBUG`-gated `setup_logging()` call and forced rebuild"""

    @pytest.mark.parametrize(
        ("arc_debug", "pre_populate_cache", "expect_setup_logging", "expect_rebuild"),
        [
            (True, False, True, True),
            (False, False, False, True),
            (True, True, True, True),
            (False, True, False, False),
        ],
        ids=["debug-no-cache", "no-debug-no-cache", "debug-fresh-cache", "no-debug-fresh-cache"],
    )
    def test_arc_debug_controls_debug_logging_and_forced_rebuild(
        self,
        arc_debug: bool,
        pre_populate_cache: bool,
        expect_setup_logging: bool,
        expect_rebuild: bool,
        project_dir: Path,
        cache_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        mocker: MockerFixture,
    ) -> None:
        """Test that `_ARC_DEBUG=1` (argcomplete's own debug switch) makes `_complete()` call
        `setup_logging(level="DEBUG")` before rebuilding, regardless of whether the cache is already fresh,
        while omitting it never enables debug logging, so a discovery-time `DEBUG` log (normally silenced by
        the default `NullHandler`) becomes visible only once asked for. Also covers that `_ARC_DEBUG=1`
        forces a rebuild even when an otherwise-fresh cache would normally be served as-is, while omitting it
        preserves the fast, no-rebuild common path for a fresh cache: before this behavior existed, a fresh
        cache skipped both the rebuild and the `setup_logging()` call meant to explain it, silently defeating
        the whole point of setting `_ARC_DEBUG`
        """
        if pre_populate_cache:
            key = entrypoint.cache_key(entrypoint.project_roots())
            entrypoint.save_cache(key, {})
        monkeypatch.setenv("_ARGCOMPLETE", "1")
        if arc_debug:
            monkeypatch.setenv("_ARC_DEBUG", "1")
        else:
            monkeypatch.delenv("_ARC_DEBUG", raising=False)
        monkeypatch.setenv("COMP_LINE", "api-client ")
        monkeypatch.setenv("COMP_POINT", "11")
        mock_setup_logging = mocker.patch("api_client_core.logging.setup_logging")
        mock_build = mocker.patch("api_client_core.cli.builder.build_completion_tree", return_value={})
        # A real argcomplete.autocomplete() call, given genuine COMP_LINE/COMP_POINT env vars, prints
        # completions and hard-exits the process (its real behavior for an actual shell completion request).
        mocker.patch("argcomplete.autocomplete")

        entrypoint._complete()

        if expect_setup_logging:
            mock_setup_logging.assert_called_once_with(level="DEBUG")
        else:
            mock_setup_logging.assert_not_called()
        if expect_rebuild:
            mock_build.assert_called_once()
        else:
            mock_build.assert_not_called()


class TestMain:
    """Tests for `main()`'s completion-vs-real-run routing and top-level `KeyboardInterrupt`/`BrokenPipeError`
    handling.
    """

    def test_completion_request_never_dispatches_a_real_run_when_argcomplete_is_missing(
        self, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture
    ) -> None:
        """Test that a completion request (`_ARGCOMPLETE` set) never falls through to a real dispatch when
        `argcomplete` isn't importable, since a completion request must never execute a real, potentially
        side-effecting command
        """
        monkeypatch.setenv("_ARGCOMPLETE", "1")
        monkeypatch.setitem(sys.modules, "argcomplete", None)
        mock_dispatch = mocker.patch("api_client_core.cli.dispatch.dispatch")

        rc = entrypoint.main(["some-app", "some-resource", "some-command"])

        assert rc == 0
        mock_dispatch.assert_not_called()

    def test_keyboard_interrupt_during_a_real_run_exits_130(self, mocker: MockerFixture) -> None:
        """Test that Ctrl-C during a real (non-completion) run exits 130, the usual shell convention for
        SIGINT, rather than an uncaught KeyboardInterrupt traceback
        """
        mocker.patch("api_client_core.cli.dispatch.dispatch", side_effect=KeyboardInterrupt)

        rc = entrypoint.main(["some-app", "some-resource", "some-command"])

        assert rc == 130

    def test_broken_pipe_during_a_real_run_exits_141_and_redirects_stdout_to_devnull(
        self, mocker: MockerFixture
    ) -> None:
        """Test that a `BrokenPipeError` during a real (non-completion) run - e.g. `--output json` writing
        into a `| head` that already exited - exits `128 + SIGPIPE` (141), the usual shell convention,
        rather than an uncaught traceback. `stdout` is redirected to `os.devnull` first: without it, Python
        still re-reports the failure while flushing stdout at shutdown regardless of the returned exit code
        """
        mocker.patch("api_client_core.cli.dispatch.dispatch", side_effect=BrokenPipeError)
        mock_dup2 = mocker.patch("api_client_core.cli._entrypoint.os.dup2")
        mock_close = mocker.patch("api_client_core.cli._entrypoint.os.close")
        devnull_fd = object()
        mock_open = mocker.patch("api_client_core.cli._entrypoint.os.open", return_value=devnull_fd)

        rc = entrypoint.main(["some-app", "some-resource", "some-command"])

        assert rc == 128 + getattr(signal, "SIGPIPE", 13)
        mock_open.assert_called_once_with(os.devnull, os.O_WRONLY)
        mock_dup2.assert_called_once_with(devnull_fd, sys.stdout.fileno())
        mock_close.assert_called_once_with(devnull_fd)

    def test_broken_pipe_falls_back_to_13_when_sigpipe_is_unavailable(self, mocker: MockerFixture) -> None:
        """Test that the exit code falls back to `128 + 13` (13 being `SIGPIPE`'s universal POSIX value) when
        `signal.SIGPIPE` doesn't exist, e.g. on Windows, rather than the handler itself raising `AttributeError`
        """
        mocker.patch("api_client_core.cli.dispatch.dispatch", side_effect=BrokenPipeError)
        mocker.patch("api_client_core.cli._entrypoint.os.dup2")
        mocker.patch("api_client_core.cli._entrypoint.os.close")
        mocker.patch("api_client_core.cli._entrypoint.os.open", return_value=object())
        mocker.patch("api_client_core.cli._entrypoint.signal", spec=[])

        rc = entrypoint.main(["some-app", "some-resource", "some-command"])

        assert rc == 128 + 13

    def test_a_successful_run_flushes_stdout_before_returning(self, mocker: MockerFixture) -> None:
        """Test that `main()` flushes stdout after a successful `dispatch()` call, so a broken pipe from a
        downstream reader that already exited (e.g. `| head`) surfaces here - where it's caught - rather
        than later, at interpreter shutdown, as an uncaught `Exception ignored` on stderr
        """
        mocker.patch("api_client_core.cli.dispatch.dispatch", return_value=0)
        mock_flush = mocker.patch.object(sys.stdout, "flush")

        rc = entrypoint.main(["some-app", "some-resource", "some-command"])

        assert rc == 0
        mock_flush.assert_called_once()

    def test_a_broken_pipe_at_flush_time_is_handled_the_same_as_one_from_dispatch(self, mocker: MockerFixture) -> None:
        """Test that a `BrokenPipeError` raised by the explicit flush (rather than by `dispatch()` itself,
        which is the more common case since a pipe write is block-buffered) is caught and handled
        identically: exit `128 + SIGPIPE`, real stdout redirected to `os.devnull`
        """
        mocker.patch("api_client_core.cli.dispatch.dispatch", return_value=0)
        raised = False

        def fail_once() -> None:
            nonlocal raised
            if not raised:
                raised = True
                raise BrokenPipeError

        # Only the first call raises: a later flush (e.g. pytest's own capture teardown, on the same real
        # stdout object) must not blow up outside the test itself.
        mocker.patch.object(sys.stdout, "flush", side_effect=fail_once)
        mock_dup2 = mocker.patch("api_client_core.cli._entrypoint.os.dup2")
        mock_close = mocker.patch("api_client_core.cli._entrypoint.os.close")
        devnull_fd = object()
        mock_open = mocker.patch("api_client_core.cli._entrypoint.os.open", return_value=devnull_fd)

        rc = entrypoint.main(["some-app", "some-resource", "some-command"])

        assert rc == 128 + getattr(signal, "SIGPIPE", 13)
        mock_open.assert_called_once_with(os.devnull, os.O_WRONLY)
        mock_dup2.assert_called_once_with(devnull_fd, sys.stdout.fileno())
        mock_close.assert_called_once_with(devnull_fd)

    def test_a_dispatch_that_exits_via_systemexit_still_flushes_stdout(self, mocker: MockerFixture) -> None:
        """Test that `main()` still flushes stdout when `dispatch()` exits via `SystemExit` (e.g. `--help`/a
        usage error below the top level) rather than a normal return, since the flush lives in a `finally`
        around the `dispatch()` call rather than only after it returns
        """
        mocker.patch("api_client_core.cli.dispatch.dispatch", side_effect=SystemExit(0))
        mock_flush = mocker.patch.object(sys.stdout, "flush")

        with pytest.raises(SystemExit) as exc_info:
            entrypoint.main(["some-app", "some-resource", "some-command"])

        assert exc_info.value.code == 0
        mock_flush.assert_called_once()

    def test_a_broken_pipe_at_flush_time_during_a_systemexit_unwind_is_handled_the_same_way(
        self, mocker: MockerFixture
    ) -> None:
        """Test that a `BrokenPipeError` raised by the explicit flush while a `SystemExit` from `dispatch()`
        is unwinding is still caught and handled identically to one raised on the normal-return path: exit
        `128 + SIGPIPE`, real stdout redirected to `os.devnull`, rather than the `BrokenPipeError` escaping
        underneath the in-flight `SystemExit`
        """
        mocker.patch("api_client_core.cli.dispatch.dispatch", side_effect=SystemExit(0))
        raised = False

        def fail_once() -> None:
            nonlocal raised
            if not raised:
                raised = True
                raise BrokenPipeError

        mocker.patch.object(sys.stdout, "flush", side_effect=fail_once)
        mock_dup2 = mocker.patch("api_client_core.cli._entrypoint.os.dup2")
        mock_close = mocker.patch("api_client_core.cli._entrypoint.os.close")
        devnull_fd = object()
        mock_open = mocker.patch("api_client_core.cli._entrypoint.os.open", return_value=devnull_fd)

        rc = entrypoint.main(["some-app", "some-resource", "some-command"])

        assert rc == 128 + getattr(signal, "SIGPIPE", 13)
        mock_open.assert_called_once_with(os.devnull, os.O_WRONLY)
        mock_dup2.assert_called_once_with(devnull_fd, sys.stdout.fileno())
        mock_close.assert_called_once_with(devnull_fd)


class TestInstalledConsoleScript:
    """Tests that actually invoke the `api-client` executable pip/uv installs alongside the interpreter,
    per `[project.scripts]` in `pyproject.toml` (`api-client = "api_client_core.cli._entrypoint:main"`).

    Every other test in this module drives `entrypoint.main()` either in-process or via `sys.executable -c
    <script>`, neither of which touches the console-script wrapper the build backend actually generates on
    install. A typo in the `[project.scripts]` target, or a `main()` that stops returning a plain int, would
    pass every one of those tests while leaving the real, installed command broken.
    """

    @staticmethod
    def _script_path() -> Path:
        """The `api-client` executable installed alongside the current interpreter, e.g. `.venv/bin/api-client`
        (`.venv\\Scripts\\api-client.exe` on Windows): where `pip`/`uv` puts a console script it generates for
        the environment currently running these tests.
        """
        name = "api-client.exe" if sys.platform == "win32" else "api-client"
        return Path(sys.executable).parent / name

    def test_version_runs_through_the_real_generated_script(self) -> None:
        """Test that `api-client --version`, run as the actual installed executable rather than through
        `entrypoint.main()` directly, resolves the `[project.scripts]` entry point and exits `0`
        """
        script_path = self._script_path()
        if not script_path.is_file():
            pytest.skip(f"{script_path} not found: package isn't installed with console scripts here")

        result = subprocess.run([str(script_path), "--version"], capture_output=True, text=True, check=False)

        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith("api-client "), result.stdout

    def test_help_runs_through_the_real_generated_script(self) -> None:
        """Test that `api-client -h`, run as the actual installed executable, exercises the full
        `main()` -> `dispatch()` -> discovery path (not just `--version`'s early short-circuit) and exits `0`
        """
        script_path = self._script_path()
        if not script_path.is_file():
            pytest.skip(f"{script_path} not found: package isn't installed with console scripts here")

        result = subprocess.run([str(script_path), "-h"], capture_output=True, text=True, check=False)

        assert result.returncode == 0, result.stderr
        assert "usage: api-client" in remove_color_code(result.stdout)


class TestStdoutReservation:
    """Tests pinning `main()`'s own `reserve_stdout()` policy: the real stdout is reserved for the whole
    process, around both a real run and a completion request, so only the CLI's own output ever reaches
    it
    """

    def test_a_real_run_is_dispatched_with_the_reservation_open(self, mocker: MockerFixture) -> None:
        """Test that `dispatch()` runs with `sys.stdout` pointed at `sys.stderr`, so any downstream code
        it reaches (discovery, a client constructor, the call itself) can't write to the real stdout
        """
        real_stdout = sys.stdout
        seen: list[bool] = []

        def fake_dispatch(argv: list[str] | None) -> int:
            seen.append(sys.stdout is real_stdout)
            return 0

        mocker.patch("api_client_core.cli.dispatch.dispatch", side_effect=fake_dispatch)

        rc = entrypoint.main(["some-app", "some-resource", "some-command"])

        assert rc == 0
        assert seen == [False]
        assert sys.stdout is real_stdout

    def test_a_completion_request_runs_with_the_reservation_open(
        self, project_dir: Path, cache_home: Path, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture
    ) -> None:
        """Test that `_complete()` also runs with the reservation open, not just a real dispatch, since a
        cache-miss completion request can import project modules and construct clients exactly like a real
        run can
        """
        real_stdout = sys.stdout
        monkeypatch.setenv("_ARGCOMPLETE", "1")
        monkeypatch.setenv("COMP_LINE", "api-client ")
        monkeypatch.setenv("COMP_POINT", "11")
        key = entrypoint.cache_key(entrypoint.project_roots())
        entrypoint.save_cache(key, {})
        seen: list[bool] = []
        mocker.patch("argcomplete.autocomplete", side_effect=lambda *a, **k: seen.append(sys.stdout is real_stdout))

        rc = entrypoint.main(["some-app"])

        assert rc == 0
        assert seen == [False]
        assert sys.stdout is real_stdout

    def test_version_and_help_still_reach_the_real_stdout(
        self, project_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Test that `--version` and `-h`, dispatched through the real entry point rather than `dispatch()`
        directly, still reach the real stdout despite `main()`'s own reservation
        """
        rc = entrypoint.main(["--version"])
        assert rc == 0
        assert capsys.readouterr().out.startswith("api-client ")

        rc = entrypoint.main(["-h"])
        assert rc == 0
        assert "usage: api-client" in remove_color_code(capsys.readouterr().out)

    def test_a_downstream_logging_leak_stays_off_stdout_end_to_end(
        self,
        downstream_setup_logging_project: Path,
        capsys: pytest.CaptureFixture[str],
        _restore_logging_state: None,
    ) -> None:
        """End-to-end regression test for the reported bug, driven through the real console-script entry
        point rather than `dispatch()` directly: a downstream project's own import-time logging setup,
        bound straight to `ext://sys.stdout`, still lands on stderr once `main()` has opened its own
        reservation, and stays there for a line logged after discovery has already returned
        """
        rc = entrypoint.main(["-h"])

        assert rc == 0
        out, err = capsys.readouterr()
        assert "Skipping NoAppNameClient" not in out
        assert "usage: api-client [-h]" in remove_color_code(out)
        assert "Skipping NoAppNameClient: no 'app_name' class attribute is set" in err

"""Typed schema for the on-disk shell-completion tree.

Stays stdlib-only, since it's imported at module scope on the shell-completion hot path.
"""

from __future__ import annotations

from typing import TypeAlias, TypedDict


class OptSpec(TypedDict):
    """One serialized `argparse` flag."""

    opts: list[str]
    choices: list[str | int | float] | None
    nargs: str | int | None
    is_file: bool
    is_json_file: bool


class ResourceSpec(TypedDict):
    """One resource's own flags, plus its `command_name -> [OptSpec, ...]` map.

    `opts` covers the `--base-url`/`--log-level` flags registered directly on the resource parser, distinct
    from each of its commands' own flags.
    """

    opts: list[OptSpec]
    commands: dict[str, list[OptSpec]]


class AppSpec(TypedDict):
    """One app's own flags, plus its `resource_name -> ResourceSpec` map."""

    opts: list[OptSpec]
    resources: dict[str, ResourceSpec]


CompletionTree: TypeAlias = dict[str, AppSpec]

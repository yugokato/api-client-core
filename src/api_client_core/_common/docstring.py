from __future__ import annotations

import inspect
import re
from functools import cache

_PARAM_DOC_RE = re.compile(r"^:param\s+(\S+):\s*(.*)$")


@cache
def split_param_docs(doc: str | None) -> tuple[str, dict[str, str]]:
    """Split an endpoint function's own docstring into its prose (everything but `:param` entries) and a
    `dict` of `:param <name>: <description>` entries keyed by parameter name.

    A description that continues onto the following non-blank line(s) not themselves starting a new
    `:field:` - this project's own convention for one too long to fit a single line - is joined back into
    one line. Runs of source lines that belong to no `:param` entry accumulate into the prose, in the order
    they appear, with consecutive blank lines collapsed to one so a `:param` block's own removal never
    leaves a stray gap behind. `cleandoc()` normalizes indentation first, so this works the same regardless
    of how deep the enclosing function body sits and matches Python's own C-level docstring cleanup (3.13+)
    on every supported version.

    Cached by the raw docstring text: a docstring is static for the life of the process, and the MCP
    server generator (unlike the CLI, whose own process exits after one command) re-derives this same
    split repeatedly per endpoint over a long-running server's lifetime. The returned `dict` must never
    be mutated by a caller, since a cache hit hands back the exact same object every subsequent call.

    :param doc: The endpoint function's own docstring, if any
    """
    if not doc or not doc.strip():
        return "", {}
    params: dict[str, list[str]] = {}
    prose: list[str] = []
    current: str | None = None
    for line in inspect.cleandoc(doc).splitlines():
        match = _PARAM_DOC_RE.match(line.strip())
        if match:
            name, text = match.groups()
            current = name
            params[current] = [text] if text else []
            continue
        if current is not None:
            stripped = line.strip()
            if stripped and not stripped.startswith(":"):
                params[current].append(stripped)
                continue
            current = None
            if not stripped:
                continue
        if line.strip() or (prose and prose[-1].strip()):
            prose.append(line)
    while prose and not prose[-1].strip():
        prose.pop()
    return "\n".join(prose), {name: " ".join(parts) for name, parts in params.items()}


def first_doc_line(doc: str | None) -> str | None:
    """Return the first non-blank line of a docstring, or `None` if it has none.

    :param doc: Docstring to summarize
    """
    return doc.strip().splitlines()[0] if doc and doc.strip() else None

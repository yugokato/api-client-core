"""Custom exceptions raised by the MCP server generator."""

from __future__ import annotations


class ToolArgumentError(ValueError):
    """A tool call's arguments don't match its endpoint's schema: an unknown key, a missing required
    parameter, a rejected `File` form, or a malformed `call_wrappers` object. Always caught within
    `dispatch_endpoint_call()` and turned into an `isError` `ToolResult`, never propagated.
    """

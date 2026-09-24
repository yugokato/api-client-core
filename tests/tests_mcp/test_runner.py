"""Unit tests for `api_client_core.mcp.runner`"""

from __future__ import annotations

import base64
import json
import textwrap
from functools import cached_property
from pathlib import Path
from typing import Any

import httpx2
import pytest
from common_libs.clients.rest_client import AsyncRestClient
from common_libs.clients.rest_client.utils import set_request_to_exception
from pytest_mock import MockerFixture

from api_client_core import APIClient, BaseAPI, endpoint
from api_client_core._common.discovery import discover_resources
from api_client_core.core.endpoints import Endpoint
from api_client_core.mcp._constants import (
    MAX_HEADER_VALUE_CHARS,
    MAX_RESULT_BYTES,
    MAX_SUMMARY_FAILURE_DETAILS,
    TRUNCATION_PREVIEW_CHARS,
    Mode,
    ResponseFormat,
)
from api_client_core.mcp.catalog import CatalogEntry, ServerOptions, _tool_name_for
from api_client_core.mcp.errors import ToolArgumentError
from api_client_core.mcp.runner import (
    _base64_encoded_len,
    _bound_text,
    _bounded_headers,
    _coerce_arguments,
    _coerce_value,
    _connection_failure_detail,
    _json_safe_body,
    _list_summary,
    _render_bounded_body,
    _render_full_payload,
    dispatch_endpoint_call,
)
from api_client_core.types import File, RestResponse

from ..tests_cli.conftest import (
    CliTestClient,
    Status,
    WidgetsAPI,
    make_httpx_response,
    make_rest_response,
)


def _entry(client_class: type, resource: str, func_name: str) -> CatalogEntry:
    """Build a single `CatalogEntry` for one endpoint directly, without going through
    `build_catalog()`'s full filter/name/collision pipeline - these tests care about dispatch, not
    catalog construction.
    """
    api_class = discover_resources(client_class)[resource]
    endpoint = getattr(api_class, func_name).endpoint
    return CatalogEntry(
        tool_name=_tool_name_for(resource, func_name), resource=resource, api_class=api_class, endpoint=endpoint
    )


class _NoKwargsAPI(BaseAPI):
    """A synthetic API class whose endpoint omits `**kwargs: Unpack[Kwargs]`, mirroring a
    hand-maintained/undocumented endpoint the framework explicitly tolerates (see
    `EndpointFunc._call()`'s own comment on why `quiet` is only ever forwarded when not `None`).
    """

    app_name = "no-kwargs-test"

    @endpoint.get("/items/{item_id}")
    def get_item(self, item_id: int) -> RestResponse:
        """Get an item"""
        ...


class _NoKwargsClient(APIClient):
    """A synthetic client exposing `_NoKwargsAPI`, for testing that dispatch doesn't force an explicit
    `quiet` value onto an endpoint that can't accept one.
    """

    app_name = "no-kwargs-test"

    @cached_property
    def items(self) -> _NoKwargsAPI:
        return _NoKwargsAPI(self)


class TestCoerceArgumentsValidation:
    """Tests for `_coerce_arguments()`'s up-front unknown/missing-argument rejection"""

    def test_unknown_argument_raises_naming_it(self) -> None:
        """Test that an argument not on the endpoint's schema raises, naming the offending key"""
        with pytest.raises(ToolArgumentError, match="bogus"):
            _coerce_arguments(WidgetsAPI.get_widget.endpoint, {"widget_id": 1, "bogus": "x"}, ServerOptions())

    def test_unknown_argument_hint_names_only_describe_endpoint_in_dynamic_mode(self) -> None:
        """Test that mode=Mode.DYNAMIC points only at describe_endpoint, not also at a static-mode
        tool's own input schema that doesn't exist under this mode
        """
        with pytest.raises(ToolArgumentError, match="describe_endpoint") as exc_info:
            _coerce_arguments(
                WidgetsAPI.get_widget.endpoint, {"widget_id": 1, "bogus": "x"}, ServerOptions(), mode=Mode.DYNAMIC
            )
        assert "this tool's own input schema" not in str(exc_info.value)

    def test_call_wrappers_nested_here_by_mistake_gets_a_dedicated_hint_in_dynamic_mode(self) -> None:
        """Test that `call_wrappers` given as one of these arguments - the mistake of nesting it inside
        `call_endpoint`'s `arguments` instead of beside it - raises a hint naming that mistake, not the
        generic unknown-argument message
        """
        with pytest.raises(ToolArgumentError, match=r"call_endpoint\(\{endpoint_id, arguments, call_wrappers\}\)"):
            _coerce_arguments(
                WidgetsAPI.get_widget.endpoint,
                {"widget_id": 1, "call_wrappers": {"with_repeat": {"num": 2}}},
                ServerOptions(),
                mode=Mode.DYNAMIC,
            )

    def test_call_wrappers_nested_here_is_a_plain_unknown_argument_outside_dynamic_mode(self) -> None:
        """Test that the dedicated `call_wrappers` hint only fires for dynamic mode: in static mode (and
        with no mode given), `call_wrappers` given here is just another unknown key, since static mode's
        own tool schema has no separate `arguments` object for it to be misplaced relative to
        """
        with pytest.raises(ToolArgumentError, match="Unknown argument") as exc_info:
            _coerce_arguments(
                WidgetsAPI.get_widget.endpoint,
                {"widget_id": 1, "call_wrappers": {"with_repeat": {"num": 2}}},
                ServerOptions(),
                mode=Mode.STATIC,
            )
        assert "call_endpoint(" not in str(exc_info.value)

    def test_unknown_argument_hint_names_only_input_schema_in_static_mode(self) -> None:
        """Test that mode=Mode.STATIC points only at this tool's own input schema, not also at
        describe_endpoint, which doesn't exist as a tool under this mode
        """
        with pytest.raises(ToolArgumentError, match="this tool's input schema") as exc_info:
            _coerce_arguments(
                WidgetsAPI.get_widget.endpoint, {"widget_id": 1, "bogus": "x"}, ServerOptions(), mode=Mode.STATIC
            )
        assert "describe_endpoint" not in str(exc_info.value)

    def test_unknown_argument_hint_hedges_between_both_when_mode_is_unknown(self) -> None:
        """Test that omitting mode falls back to naming both possible paths, matching the historical
        message a caller with no catalog context (e.g. a direct unit test) still gets
        """
        with pytest.raises(ToolArgumentError, match="describe_endpoint") as exc_info:
            _coerce_arguments(WidgetsAPI.get_widget.endpoint, {"widget_id": 1, "bogus": "x"}, ServerOptions())
        assert "input schema" in str(exc_info.value)

    def test_missing_required_argument_raises_naming_it(self) -> None:
        """Test that a missing required argument raises, naming it"""
        with pytest.raises(ToolArgumentError, match="widget_id"):
            _coerce_arguments(WidgetsAPI.get_widget.endpoint, {}, ServerOptions())

    def test_missing_multiple_required_arguments_names_all(self) -> None:
        """Test that multiple missing required arguments are all named in the error"""
        with pytest.raises(ToolArgumentError, match=r"name.*owner_id|owner_id.*name"):
            _coerce_arguments(WidgetsAPI.create_widget.endpoint, {}, ServerOptions())

    def test_all_required_arguments_given_succeeds(self) -> None:
        """Test that providing every required argument succeeds without raising"""
        result = _coerce_arguments(WidgetsAPI.create_widget.endpoint, {"name": "x", "owner_id": 1}, ServerOptions())
        assert result == {"name": "x", "owner_id": 1}

    def test_optional_arguments_may_be_omitted(self) -> None:
        """Test that omitting an optional argument doesn't raise and isn't defaulted into the result"""
        result = _coerce_arguments(WidgetsAPI.get_widget.endpoint, {"widget_id": 1}, ServerOptions())
        assert result == {"widget_id": 1}


class TestCoerceArgumentsValues:
    """Tests for `_coerce_arguments()`'s per-value conversion (Enum, File, list elements)"""

    def test_enum_value_converts_from_its_member_name(self) -> None:
        """Test that a string matching an enum member's name converts to that member's own wire value"""
        result = _coerce_arguments(
            WidgetsAPI.create_widget.endpoint, {"name": "x", "owner_id": 1, "status": "ACTIVE"}, ServerOptions()
        )
        assert result["status"] == Status.ACTIVE.value

    def test_invalid_enum_value_raises_naming_valid_choices(self) -> None:
        """Test that a string not matching any member name raises, naming the valid choices"""
        with pytest.raises(ToolArgumentError, match="ACTIVE"):
            _coerce_arguments(
                WidgetsAPI.create_widget.endpoint,
                {"name": "x", "owner_id": 1, "status": "bogus"},
                ServerOptions(),
            )

    def test_plain_values_pass_through_unchanged(self) -> None:
        """Test that ordinary JSON-native values (str, int, bool, list, dict) pass through as-is"""
        result = _coerce_arguments(
            WidgetsAPI.create_widget.endpoint,
            {"name": "x", "owner_id": 1, "active": False, "tags": ["a", "b"], "metadata": {"k": "v"}},
            ServerOptions(),
        )
        assert result["active"] is False
        assert result["tags"] == ["a", "b"]
        assert result["metadata"] == {"k": "v"}

    def test_none_value_passes_through(self) -> None:
        """Test that an explicit JSON null passes through as None rather than being coerced further"""
        result = _coerce_arguments(WidgetsAPI.create_widget.endpoint, {"name": "x", "owner_id": None}, ServerOptions())
        assert result["owner_id"] is None


class TestCoerceValueContainers:
    """Tests for `_coerce_value()`'s recursion into a dict's values and a fixed-length tuple's
    elements, the same way it already recurses into a list's elements
    """

    def test_dict_values_are_coerced_by_their_declared_value_type(self) -> None:
        """Test that dict[str, Enum]'s values convert from their member names to their own wire values,
        matching the additionalProperties schema published for the same type
        """
        result = _coerce_value({"a": "ACTIVE", "b": "INACTIVE"}, dict[str, Status], ServerOptions())
        assert result == {"a": Status.ACTIVE.value, "b": Status.INACTIVE.value}

    def test_fixed_tuple_elements_are_coerced_elementwise(self) -> None:
        """Test that a fixed-length tuple[str, Enum]'s elements convert by position to their own wire
        values, matching the prefixItems schema published for the same type
        """
        result = _coerce_value(["owner", "ACTIVE"], tuple[str, Status], ServerOptions())
        assert result == ["owner", Status.ACTIVE.value]

    def test_variable_length_tuple_still_uses_the_list_branch(self) -> None:
        """Test that a variable-length tuple[Enum, ...] still converts every element to its own wire
        value, unaffected by the fixed-tuple branch
        """
        result = _coerce_value(["ACTIVE", "INACTIVE"], tuple[Status, ...], ServerOptions())
        assert result == [Status.ACTIVE.value, Status.INACTIVE.value]

    def test_too_long_fixed_tuple_value_is_left_uncoerced(self) -> None:
        """Test that an array longer than a fixed-length tuple[str, int]'s arity is passed through
        unchanged rather than silently truncated to the declared length
        """
        result = _coerce_value(["a", 1, "extra"], tuple[str, int], ServerOptions())
        assert result == ["a", 1, "extra"]

    def test_too_short_fixed_tuple_value_is_left_uncoerced(self) -> None:
        """Test that an array shorter than a fixed-length tuple[str, int]'s arity is passed through
        unchanged rather than silently accepted with the missing element dropped
        """
        result = _coerce_value(["a"], tuple[str, int], ServerOptions())
        assert result == ["a"]


class TestCoerceValueUnions:
    """Tests for `_coerce_value()`'s handling of a genuine multi-member union (as opposed to the
    single-non-`None`-member case `unwrap_annotation()` already collapses on its own), matching the
    per-member schema `_leaf_schema()` publishes for the same annotation
    """

    def test_enum_member_in_a_union_converts_from_its_member_name(self) -> None:
        """Test that a value matching an Enum member's name converts to that member's own wire value,
        not just the first non-union member schema.py happens to declare
        """
        assert _coerce_value("ACTIVE", Status | str, ServerOptions()) == Status.ACTIVE.value

    def test_non_member_string_in_an_enum_union_falls_back_to_the_other_member(self) -> None:
        """Test that a value not matching any Enum member name falls through to the union's other
        (plain str) member instead of raising, since some other member could still accept it
        """
        assert _coerce_value("not-a-member", Status | str, ServerOptions()) == "not-a-member"

    def test_nullable_enum_still_converts_from_its_member_name(self) -> None:
        """Test that Status | None (a single-non-None-member union, already collapsed by
        unwrap_annotation()) still converts by member name to its own wire value, unaffected by the new
        union branch
        """
        assert _coerce_value("ACTIVE", Status | None, ServerOptions()) == Status.ACTIVE.value

    def test_file_form_in_a_union_converts_to_a_file(self) -> None:
        """Test that a File-shaped object matching the union's File member converts to a File"""
        value = {"filename": "a.txt", "content_base64": base64.b64encode(b"hi").decode(), "content_type": "text/plain"}
        result = _coerce_value(value, File | str, ServerOptions())
        assert isinstance(result, File)
        assert result["content"] == b"hi"

    def test_string_in_a_file_union_falls_back_to_the_other_member(self) -> None:
        """Test that a plain string given for a File | str union reaches the str member instead of
        being force-coerced by File's own is_type_of() union-aware check, which would otherwise make
        this union's str branch permanently unreachable
        """
        assert _coerce_value("just-a-string", File | str, ServerOptions()) == "just-a-string"

    def test_enum_member_converts_regardless_of_its_position_in_the_union(self) -> None:
        """Test that str | Status (str declared first) still converts a matching member name to the
        Enum member's own wire value, not the raw string a naive declaration-order-only walk would
        return - str's own coercion always succeeds, so trying it before Status would otherwise win by
        declaration order alone and leave the value never actually converted
        """
        assert _coerce_value("ACTIVE", str | Status, ServerOptions()) == Status.ACTIVE.value

    def test_file_form_converts_regardless_of_its_position_in_the_union(self) -> None:
        """Test that str | File (str declared first) still converts a File-shaped object to a File, not
        the raw dict a naive declaration-order-only walk would return
        """
        value = {"filename": "a.txt", "content_base64": base64.b64encode(b"hi").decode()}
        result = _coerce_value(value, str | File, ServerOptions())
        assert isinstance(result, File)
        assert result["content"] == b"hi"


class TestCoerceArgumentsFile:
    """Tests for `_coerce_arguments()`'s `File`-typed argument handling"""

    def test_base64_form_converts_to_a_file(self) -> None:
        """Test that the {filename, content_base64, content_type} form converts to a File with the
        given content decoded and content_type preserved
        """
        content_b64 = base64.b64encode(b"binary content").decode()
        result = _coerce_arguments(
            WidgetsAPI.upload_avatar.endpoint,
            {
                "widget_id": 1,
                "avatar": {"filename": "a.png", "content_base64": content_b64, "content_type": "image/png"},
            },
            ServerOptions(),
        )
        file = result["avatar"]
        assert isinstance(file, File)
        assert file["content"] == b"binary content"
        assert file["filename"] == "a.png"
        assert file["content_type"] == "image/png"

    def test_content_type_is_guessed_when_omitted(self) -> None:
        """Test that omitting content_type falls back to a mimetypes guess from the filename"""
        content_b64 = base64.b64encode(b"x").decode()
        result = _coerce_arguments(
            WidgetsAPI.upload_avatar.endpoint,
            {"widget_id": 1, "avatar": {"filename": "a.png", "content_base64": content_b64}},
            ServerOptions(),
        )
        assert result["avatar"]["content_type"] == "image/png"

    def test_line_wrapped_base64_still_decodes(self) -> None:
        """Test that a base64 literal wrapped across multiple lines (e.g. from the base64 CLI tool or
        a PEM-style source) still decodes, rather than rejecting the embedded newlines as invalid
        """
        content = b"binary content" * 5
        wrapped = "\n".join(textwrap.wrap(base64.b64encode(content).decode(), 20))
        result = _coerce_arguments(
            WidgetsAPI.upload_avatar.endpoint,
            {"widget_id": 1, "avatar": {"filename": "a.png", "content_base64": wrapped}},
            ServerOptions(),
        )
        assert result["avatar"]["content"] == content

    def test_invalid_base64_raises(self) -> None:
        """Test that malformed base64 content raises a ToolArgumentError rather than an unhandled
        binascii.Error
        """
        with pytest.raises(ToolArgumentError, match="base64"):
            _coerce_arguments(
                WidgetsAPI.upload_avatar.endpoint,
                {"widget_id": 1, "avatar": {"filename": "a.png", "content_base64": "not-valid-base64!!!"}},
                ServerOptions(),
            )

    def test_missing_content_base64_raises(self) -> None:
        """Test that a file object missing content_base64 (and no path) raises"""
        with pytest.raises(ToolArgumentError, match="content_base64"):
            _coerce_arguments(
                WidgetsAPI.upload_avatar.endpoint, {"widget_id": 1, "avatar": {"filename": "a.png"}}, ServerOptions()
            )

    def test_path_form_rejected_by_default(self) -> None:
        """Test that the {path} form is rejected when --allow-file-paths isn't set, even though the
        server otherwise never restricts endpoint exposure by default
        """
        with pytest.raises(ToolArgumentError, match="local file paths"):
            _coerce_arguments(
                WidgetsAPI.upload_avatar.endpoint, {"widget_id": 1, "avatar": {"path": "/etc/passwd"}}, ServerOptions()
            )

    def test_path_form_accepted_with_allow_file_paths(self, tmp_path: Any) -> None:
        """Test that the {path} form is accepted, and the file actually read, once --allow-file-paths
        is set
        """
        file_path = tmp_path / "avatar.png"
        file_path.write_bytes(b"file content")
        result = _coerce_arguments(
            WidgetsAPI.upload_avatar.endpoint,
            {"widget_id": 1, "avatar": {"path": str(file_path)}},
            ServerOptions(allow_file_paths=True),
        )
        assert result["avatar"]["content"] == b"file content"
        assert result["avatar"]["filename"] == "avatar.png"

    def test_nonexistent_path_raises_even_when_allowed(self, tmp_path: Any) -> None:
        """Test that a nonexistent path still raises cleanly, even with --allow-file-paths set"""
        with pytest.raises(ToolArgumentError, match="No such file"):
            _coerce_arguments(
                WidgetsAPI.upload_avatar.endpoint,
                {"widget_id": 1, "avatar": {"path": str(tmp_path / "missing.png")}},
                ServerOptions(allow_file_paths=True),
            )

    def test_oversized_base64_content_raises(self) -> None:
        """Test that base64 content decoding to more than --max-file-bytes raises, naming the limit"""
        content_b64 = base64.b64encode(b"x" * 100).decode()
        with pytest.raises(ToolArgumentError, match="exceeds the 10-byte limit"):
            _coerce_arguments(
                WidgetsAPI.upload_avatar.endpoint,
                {"widget_id": 1, "avatar": {"filename": "a.png", "content_base64": content_b64}},
                ServerOptions(max_file_bytes=10),
            )

    def test_oversized_path_content_raises(self, tmp_path: Any) -> None:
        """Test that a local file exceeding --max-file-bytes raises, naming the limit"""
        file_path = tmp_path / "big.bin"
        file_path.write_bytes(b"x" * 100)
        with pytest.raises(ToolArgumentError, match="exceeds the 10-byte limit"):
            _coerce_arguments(
                WidgetsAPI.upload_avatar.endpoint,
                {"widget_id": 1, "avatar": {"path": str(file_path)}},
                ServerOptions(allow_file_paths=True, max_file_bytes=10),
            )

    def test_oversized_path_content_is_rejected_without_reading_the_file(
        self, tmp_path: Any, mocker: MockerFixture
    ) -> None:
        """Test that an oversized local file is rejected from its own stat() size alone, never actually
        read into memory - a model naming a multi-GB path shouldn't make this process buffer all of it
        just to then reject it
        """
        file_path = tmp_path / "big.bin"
        file_path.write_bytes(b"x" * 100)
        read_bytes = mocker.patch.object(Path, "read_bytes")
        with pytest.raises(ToolArgumentError, match="exceeds the 10-byte limit"):
            _coerce_arguments(
                WidgetsAPI.upload_avatar.endpoint,
                {"widget_id": 1, "avatar": {"path": str(file_path)}},
                ServerOptions(allow_file_paths=True, max_file_bytes=10),
            )
        read_bytes.assert_not_called()

    def test_non_string_content_base64_raises(self) -> None:
        """Test that a non-string content_base64 value raises cleanly instead of an unhandled
        AttributeError from calling .split() on it
        """
        with pytest.raises(ToolArgumentError, match="'content_base64' must be a string"):
            _coerce_arguments(
                WidgetsAPI.upload_avatar.endpoint,
                {"widget_id": 1, "avatar": {"filename": "a.png", "content_base64": 123}},
                ServerOptions(),
            )

    def test_non_string_filename_raises(self) -> None:
        """Test that a non-string filename value raises cleanly instead of an unhandled TypeError from
        mimetypes.guess_type()
        """
        with pytest.raises(ToolArgumentError, match="'filename' must be a string"):
            _coerce_arguments(
                WidgetsAPI.upload_avatar.endpoint,
                {"widget_id": 1, "avatar": {"filename": 5, "content_base64": base64.b64encode(b"hi").decode()}},
                ServerOptions(),
            )

    def test_non_string_content_type_raises(self) -> None:
        """Test that a non-string content_type value raises cleanly instead of being accepted verbatim"""
        with pytest.raises(ToolArgumentError, match="'content_type' must be a string"):
            _coerce_arguments(
                WidgetsAPI.upload_avatar.endpoint,
                {
                    "widget_id": 1,
                    "avatar": {
                        "filename": "a.png",
                        "content_base64": base64.b64encode(b"hi").decode(),
                        "content_type": 7,
                    },
                },
                ServerOptions(),
            )

    def test_non_string_path_raises(self) -> None:
        """Test that a non-string path value raises cleanly instead of an unhandled TypeError from
        pathlib.Path()
        """
        with pytest.raises(ToolArgumentError, match="'path' must be a string"):
            _coerce_arguments(
                WidgetsAPI.upload_avatar.endpoint,
                {"widget_id": 1, "avatar": {"path": 123}},
                ServerOptions(allow_file_paths=True),
            )

    def test_non_object_file_value_raises(self) -> None:
        """Test that a File-typed argument given as a non-object JSON value raises cleanly"""
        with pytest.raises(ToolArgumentError, match="Expected a file object"):
            _coerce_arguments(
                WidgetsAPI.upload_avatar.endpoint, {"widget_id": 1, "avatar": "not-an-object"}, ServerOptions()
            )


class TestFullPayload:
    """Tests for `_render_full_payload()`'s `{status_code, headers, body}` envelope and header
    allowlisting
    """

    def test_envelope_shape(self, mocker: MockerFixture) -> None:
        """Test that the envelope carries status_code, headers, and the decoded body"""
        response = make_rest_response(mocker, 200, json_body={"id": 1})
        payload = json.loads(_render_full_payload(response, ServerOptions()))
        assert payload == {"status_code": 200, "headers": {}, "body": {"id": 1}}

    def test_non_allowlisted_headers_are_dropped_by_default(self, mocker: MockerFixture) -> None:
        """Test that both a sensitive header (Set-Cookie/Authorization) and a merely noisy one (a
        CDN/tracing header a model can't act on) are stripped from the envelope by default, keeping
        only the fixed allowlist, since it reaches a model's context rather than a human's terminal
        """
        raw = make_httpx_response(mocker, 200, json_body={})
        raw.headers = {
            "Set-Cookie": "session=abc",
            "Authorization": "Bearer xyz",
            "X-Powered-By": "Express",
            "Content-Type": "application/json",
        }
        response = RestResponse(_response=raw)
        payload = json.loads(_render_full_payload(response, ServerOptions()))
        assert "Set-Cookie" not in payload["headers"]
        assert "Authorization" not in payload["headers"]
        assert "X-Powered-By" not in payload["headers"]
        assert payload["headers"] == {"Content-Type": "application/json"}

    def test_www_authenticate_survives_the_default_allowlist(self, mocker: MockerFixture) -> None:
        """Test that WWW-Authenticate, a header that looks auth-related but, unlike a credential such
        as Authorization/Set-Cookie, is safe (and useful) to keep even on a failed auth attempt,
        survives the default header allowlist - a model debugging a 401/403 needs to see the
        scheme/realm it names to correct the failure
        """
        raw = make_httpx_response(mocker, 401, json_body={})
        raw.headers = {"WWW-Authenticate": 'Bearer realm="api"'}
        response = RestResponse(_response=raw)
        payload = json.loads(_render_full_payload(response, ServerOptions()))
        assert payload["headers"] == {"WWW-Authenticate": 'Bearer realm="api"'}

    def test_all_headers_option_keeps_every_header(self, mocker: MockerFixture) -> None:
        """Test that --all-headers (all_headers=True) keeps every header, including a non-allowlisted one"""
        raw = make_httpx_response(mocker, 200, json_body={})
        raw.headers = {"Set-Cookie": "session=abc"}
        response = RestResponse(_response=raw)
        payload = json.loads(_render_full_payload(response, ServerOptions(all_headers=True)))
        assert payload["headers"] == {"Set-Cookie": "session=abc"}

    def test_binary_body_is_base64_encoded(self, mocker: MockerFixture) -> None:
        """Test that a body that didn't decode as JSON and isn't valid UTF-8 text (raw bytes, common_libs'
        fallback for a binary response) is base64-encoded in the envelope rather than embedded as-is,
        since bytes can't be placed directly into a JSON structure or an MCP result's structuredContent
        """
        raw = make_httpx_response(mocker, 200, content=b"\x89PNG\r\n\x1a\n\xff\xfe")
        response = RestResponse(_response=raw)
        payload = json.loads(_render_full_payload(response, ServerOptions()))
        assert payload["body"] == {"content_base64": base64.b64encode(raw.content).decode(), "encoding": "base64"}

    def test_oversized_body_is_replaced_by_a_truncation_marker(self, mocker: MockerFixture) -> None:
        """Test that a body whose rendered JSON exceeds MAX_RESULT_BYTES is replaced by a truncation
        marker in the envelope, while status_code/headers are untouched
        """
        oversized = {"items": ["x" * 100] * (MAX_RESULT_BYTES // 50)}
        response = make_rest_response(mocker, 200, json_body=oversized)
        payload = json.loads(_render_full_payload(response, ServerOptions()))
        assert payload["status_code"] == 200
        assert payload["body"]["truncated"] is True
        assert payload["body"]["bytes"] > MAX_RESULT_BYTES
        assert isinstance(payload["body"]["preview"], str)

    def test_under_limit_body_is_untouched(self, mocker: MockerFixture) -> None:
        """Test that a body well under MAX_RESULT_BYTES round-trips unchanged"""
        response = make_rest_response(mocker, 200, json_body={"id": 1, "title": "widget"})
        payload = json.loads(_render_full_payload(response, ServerOptions()))
        assert payload["body"] == {"id": 1, "title": "widget"}

    def test_oversized_headers_trigger_truncation_of_both_body_and_headers(self, mocker: MockerFixture) -> None:
        """Test that the whole envelope - not body in isolation - is what's checked against
        MAX_RESULT_BYTES: when --all-headers makes the headers dict alone push the rendered envelope
        over the cap, a tiny body's own truncation marker doesn't shrink it enough on its own (the
        marker is larger than the tiny body it replaces), so headers are bounded too, and the final
        rendered result never exceeds the cap regardless of how large the upstream response's own
        headers were.

        Regression test: this used to leave headers untouched even when they alone were the reason
        the envelope was oversized, so the rendered result could still exceed MAX_RESULT_BYTES.
        """
        raw = make_httpx_response(mocker, 200, json_body={"id": 1})
        raw.headers = {f"x-custom-header-{i}": "v" * 200 for i in range(MAX_RESULT_BYTES // 200)}
        response = RestResponse(_response=raw)
        content = _render_full_payload(response, ServerOptions(all_headers=True))
        payload = json.loads(content)
        assert payload["body"]["truncated"] is True
        assert payload["headers"] == {"x-mcp-headers-truncated": "true"}
        assert len(content.encode()) <= MAX_RESULT_BYTES

    def test_truncation_preview_is_sliced_from_the_body_not_the_leading_headers(self, mocker: MockerFixture) -> None:
        """Test that the truncation preview reflects the body's own content, not whatever precedes it
        in the rendered envelope.

        Regression test: `status_code`/`headers` lead `body` in the envelope's own key order, so slicing
        the preview from the whole rendered envelope (rather than from `body` alone) could show none of
        the body at all once a large `--all-headers` header block pushed it past the preview's own
        character budget - exactly what the preview exists to convey
        """
        raw = make_httpx_response(mocker, 200, json_body={"id": 1})
        raw.headers = {f"x-custom-header-{i}": "v" * 200 for i in range(MAX_RESULT_BYTES // 200)}
        response = RestResponse(_response=raw)
        payload = json.loads(_render_full_payload(response, ServerOptions(all_headers=True)))
        assert '"id": 1' in payload["body"]["preview"]

    def test_oversized_binary_body_is_truncated_without_full_encoding(self, mocker: MockerFixture) -> None:
        """Test that a binary body whose base64 form alone would already exceed MAX_RESULT_BYTES is
        truncated directly. Only a preview-sized prefix is ever encoded, not the whole body, and the
        preview is identical to what encoding the whole body and slicing it would have produced.
        `bytes` reports the raw body's byte count rather than a rendered envelope size.
        """
        raw_content = bytes(range(256)) * (MAX_RESULT_BYTES // 256 + 10)
        raw = make_httpx_response(mocker, 200, content=raw_content)
        response = RestResponse(_response=raw)
        spy = mocker.spy(base64, "b64encode")
        payload = json.loads(_render_full_payload(response, ServerOptions()))
        calls_during_render = list(spy.call_args_list)
        assert calls_during_render == [mocker.call(raw_content[:TRUNCATION_PREVIEW_CHARS])]
        assert payload["body"]["truncated"] is True
        assert payload["body"]["bytes"] == len(raw_content)
        full_preview = json.dumps(
            {"content_base64": base64.b64encode(raw_content).decode("ascii"), "encoding": "base64"},
            default=str,
            indent=2,
        )[:TRUNCATION_PREVIEW_CHARS]
        assert payload["body"]["preview"] == full_preview

    def test_oversized_binary_body_plus_oversized_headers_does_not_nest_the_marker(self, mocker: MockerFixture) -> None:
        """Test that an oversized binary body and an oversized (--all-headers) header block together
        still bound the headers, with a single, non-nested body truncation marker
        """
        raw_content = bytes(range(256)) * (MAX_RESULT_BYTES // 256 + 10)
        raw = make_httpx_response(mocker, 200, content=raw_content)
        raw.headers = {f"x-custom-header-{i}": "v" * 200 for i in range(MAX_RESULT_BYTES // 200)}
        response = RestResponse(_response=raw)
        content = _render_full_payload(response, ServerOptions(all_headers=True))
        payload = json.loads(content)
        assert payload["body"]["truncated"] is True
        assert payload["body"]["bytes"] == len(raw_content)
        assert payload["headers"] == {"x-mcp-headers-truncated": "true"}
        assert len(content.encode()) <= MAX_RESULT_BYTES


class TestJsonSafeBody:
    """Tests for `_json_safe_body()`'s oversized-binary fast path and its `_base64_encoded_len()` trigger"""

    def test_base64_encoded_len_matches_a_real_encoding(self) -> None:
        """Test that the analytic base64 length formula matches an actual encoding, for both a
        3-byte-aligned length and one that needs padding
        """
        for n in (0, 1, 2, 3, 4, 100, 3 * 1000, 3 * 1000 + 1, 3 * 1000 + 2):
            assert _base64_encoded_len(n) == len(base64.b64encode(bytes(n)))

    def test_body_at_the_exact_threshold_is_not_truncated(self) -> None:
        """Test that a body whose base64 form is exactly MAX_RESULT_BYTES long is still encoded in full,
        since the fast path only triggers once encoding would exceed (not merely reach) the cap
        """
        n = 3 * (MAX_RESULT_BYTES // 4)
        assert _base64_encoded_len(n) == MAX_RESULT_BYTES
        body = bytes(n)
        result = _json_safe_body(body)
        assert result == {"content_base64": base64.b64encode(body).decode("ascii"), "encoding": "base64"}

    def test_body_one_byte_over_the_threshold_is_truncated(self) -> None:
        """Test that a body one byte past the exact threshold already triggers the fast, non-full-encode path"""
        n = 3 * (MAX_RESULT_BYTES // 4) + 1
        assert _base64_encoded_len(n) > MAX_RESULT_BYTES
        result = _json_safe_body(bytes(n))
        assert result["truncated"] is True
        assert result["bytes"] == n


class TestBoundedHeaders:
    """Tests for `_bounded_headers()`, `_render_full_payload()`'s fallback for a headers dict that
    alone keeps the envelope over MAX_RESULT_BYTES even after `body` was already truncated
    """

    def test_drops_non_allowlisted_headers(self) -> None:
        """Test that a header outside the default allowlist is dropped, the same as
        `_filtered_headers()` without --all-headers, since the allowlist's small fixed entry count is
        what bounds the result regardless of how many headers the response actually carried
        """
        headers = {"X-Powered-By": "Express", "Content-Type": "application/json"}
        assert _bounded_headers(headers) == {"Content-Type": "application/json", "x-mcp-headers-truncated": "true"}

    def test_truncates_an_oversized_allowlisted_value(self) -> None:
        """Test that an allowlisted header whose own value exceeds MAX_HEADER_VALUE_CHARS is
        truncated in place, so a single pathologically large header value can't defeat the bound
        """
        headers = {"Content-Type": "v" * (MAX_HEADER_VALUE_CHARS + 100)}
        bounded = _bounded_headers(headers)
        assert bounded["Content-Type"] == f"{'v' * MAX_HEADER_VALUE_CHARS}...(truncated)"

    def test_always_marks_that_headers_were_truncated(self) -> None:
        """Test that the marker key is always present, even when every header happened to survive
        unchanged, since a model has no other signal that --all-headers was overridden here
        """
        assert _bounded_headers({})["x-mcp-headers-truncated"] == "true"


class TestRenderBoundedBody:
    """Tests for `_render_bounded_body()`'s size cap on a `--response-format json` body, the direct
    counterpart to `_render_full_payload()`'s envelope-wide cap
    """

    def test_body_at_or_under_the_limit_passes_through_unchanged(self) -> None:
        """Test that a body whose rendered JSON size is within MAX_RESULT_BYTES is returned as-is"""
        body = {"id": 1, "values": list(range(10))}
        assert json.loads(_render_bounded_body(body)) == body

    def test_oversized_body_becomes_a_truncation_marker(self) -> None:
        """Test that an oversized body is replaced by a {truncated, bytes, preview} marker naming the
        real rendered size and carrying a text preview
        """
        body = {"data": "x" * (MAX_RESULT_BYTES + 1000)}
        bounded = json.loads(_render_bounded_body(body))
        assert bounded["truncated"] is True
        assert bounded["bytes"] > MAX_RESULT_BYTES
        assert bounded["preview"].startswith("{")
        assert '"data": "xxx' in bounded["preview"]

    def test_truncation_marker_is_always_a_dict_regardless_of_the_original_bodys_shape(self) -> None:
        """Test that an oversized non-object body (a list) still becomes an object marker, so it stays
        eligible for structuredContent even though the original body wouldn't have been
        """
        body = ["x" * 100] * (MAX_RESULT_BYTES // 50)
        bounded = json.loads(_render_bounded_body(body))
        assert isinstance(bounded, dict)
        assert bounded["truncated"] is True

    def test_a_body_compact_enough_to_pass_but_oversized_once_indented_is_still_truncated(self) -> None:
        """Test that the size check accounts for the indent=2 rendering this function actually emits,
        not a smaller compact rendering that would otherwise let an over-cap body through
        """
        item = {"id": 1, "name": "widget"}
        n = 1
        while True:
            body = [item] * n
            compact_size = len(json.dumps(body, default=str).encode())
            indented_size = len(json.dumps(body, default=str, indent=2).encode())
            if compact_size < MAX_RESULT_BYTES < indented_size:
                break
            n += 200
        bounded = json.loads(_render_bounded_body(body))
        assert bounded["truncated"] is True
        assert bounded["bytes"] == indented_size


class TestBoundText:
    """Tests for `_bound_text()`'s size cap on raw-format response text"""

    def test_text_at_or_under_the_limit_passes_through_unchanged(self) -> None:
        """Test that text within MAX_RESULT_BYTES is returned as-is"""
        assert _bound_text("hello") == "hello"

    def test_oversized_text_is_truncated_with_a_trailing_marker(self) -> None:
        """Test that oversized text is cut to a preview with a trailing marker naming the real size"""
        text = "x" * (MAX_RESULT_BYTES + 1000)
        bounded = _bound_text(text)
        assert bounded.startswith("x" * 100)
        assert "[truncated:" in bounded
        assert str(len(text.encode())) in bounded


class TestConnectionFailureDetail:
    """Tests for `_connection_failure_detail()`'s one-line rendering of a transport error that
    produced no response
    """

    def test_names_method_and_url_when_the_request_is_attached(self) -> None:
        """Test that a recoverable request turns into a `<METHOD> <url> failed - <type>: <msg>` line"""
        exc = httpx2.ConnectError("connection refused")
        set_request_to_exception(exc, httpx2.Request("POST", "https://api.example.com/widgets"))
        detail = _connection_failure_detail(exc)
        assert detail == "POST https://api.example.com/widgets failed - ConnectError: connection refused"

    def test_falls_back_to_the_bare_exception_when_no_request_is_attached(self) -> None:
        """Test that an exception carrying no recoverable request still yields a usable one-line detail"""
        assert _connection_failure_detail(httpx2.ConnectError("boom")) == "ConnectError: boom"


class TestListSummary:
    """Tests for `_list_summary()`'s deduplication and capping of a multi-call failure summary"""

    def test_captured_http_status_errors_with_the_same_status_dedupe_despite_different_request_ids(
        self, mocker: MockerFixture
    ) -> None:
        """Test that several captured `HTTPStatusError` items sharing one status code collapse to a
        single failure detail, rather than one per item, since each carries its own `request_id`
        """
        items = []
        for i in range(5):
            response = make_httpx_response(mocker, 500)
            response.request.request_id = f"request-{i}"
            items.append(httpx2.HTTPStatusError("simulated failure", request=response.request, response=response))
        summary = _list_summary(items, [False] * len(items))
        assert summary == "5 of 5 call(s) failed: HTTP 500"

    def test_plain_failed_responses_and_other_exceptions_dedupe(self, mocker: MockerFixture) -> None:
        """Test that a plain failed `RestResponse` and a non-HTTP exception both dedupe by their own
        failure detail
        """
        responses = [make_rest_response(mocker, 500) for _ in range(3)]
        items: list[Any] = [*responses, ValueError("boom"), ValueError("boom")]
        ok_flags = [False] * len(items)
        summary = _list_summary(items, ok_flags)
        assert summary == "5 of 5 call(s) failed: HTTP 500; ValueError: boom"

    def test_distinct_details_at_the_cap_show_no_suffix(self) -> None:
        """Test that exactly `MAX_SUMMARY_FAILURE_DETAILS` distinct failures are all shown, with no
        "and N more" suffix
        """
        items = [AssertionError(f"boom {i}") for i in range(MAX_SUMMARY_FAILURE_DETAILS)]
        summary = _list_summary(items, [False] * len(items))
        assert summary.count(";") == MAX_SUMMARY_FAILURE_DETAILS - 1
        assert "more" not in summary

    def test_distinct_details_over_the_cap_are_capped_with_a_count_of_the_rest(self) -> None:
        """Test that more than `MAX_SUMMARY_FAILURE_DETAILS` distinct failures show only the first
        `MAX_SUMMARY_FAILURE_DETAILS`, plus a count of how many more distinct ones were dropped
        """
        items = [AssertionError(f"boom {i}") for i in range(10)]
        summary = _list_summary(items, [False] * len(items))
        assert summary == (
            "10 of 10 call(s) failed: AssertionError: boom 0; AssertionError: boom 1; AssertionError: boom 2; "
            "AssertionError: boom 3; AssertionError: boom 4; and 5 more distinct failure(s)"
        )


class TestCallEndpoint:
    """End-to-end tests for `dispatch_endpoint_call()`'s dispatch and result shaping, against a real (mocked)
    async client
    """

    @pytest.fixture
    def client(self, async_request_mock: Any) -> Any:
        return CliTestClient(base_url="https://example.com/api", async_mode=True)

    async def test_successful_call_is_not_an_error_and_envelopes_the_body(
        self, client: Any, async_request_mock: Any, mocker: MockerFixture
    ) -> None:
        """Test that a 2xx response produces a non-error ToolResult whose structured content is the
        {status_code, headers, body} envelope
        """
        async_request_mock.return_value = make_httpx_response(mocker, 200, json_body={"id": 1, "name": "Foo"})
        catalog_entry = _entry(CliTestClient, "widgets", "get_widget")
        result = await dispatch_endpoint_call(client, catalog_entry, {"widget_id": 1}, ServerOptions())
        assert result.is_error is False
        assert result.structured == {"status_code": 200, "headers": {}, "body": {"id": 1, "name": "Foo"}}

    async def test_failed_call_is_an_error_with_a_one_line_detail(
        self, client: Any, async_request_mock: Any, mocker: MockerFixture
    ) -> None:
        """Test that a non-2xx response produces an isError ToolResult whose content leads with the same
        one-line failure detail the CLI's format_request_failure() produces
        """
        async_request_mock.return_value = make_httpx_response(mocker, 404, json_body={"error": "not found"})
        catalog_entry = _entry(CliTestClient, "widgets", "get_widget")
        result = await dispatch_endpoint_call(client, catalog_entry, {"widget_id": 999}, ServerOptions())
        assert result.is_error is True
        assert "404" in result.content

    async def test_failed_call_includes_the_response_body(
        self, client: Any, async_request_mock: Any, mocker: MockerFixture
    ) -> None:
        """Test that a non-2xx response's decoded body is included in the error detail, not just the
        one-line summary: unlike the CLI (where a failed body still reaches the user via --output), this
        text is the model's only channel, and an API's error body is what lets it correct its arguments
        and retry
        """
        async_request_mock.return_value = make_httpx_response(
            mocker, 400, json_body={"error": "widget_id must be positive"}
        )
        catalog_entry = _entry(CliTestClient, "widgets", "get_widget")
        result = await dispatch_endpoint_call(client, catalog_entry, {"widget_id": 999}, ServerOptions())
        assert result.is_error is True
        assert "widget_id must be positive" in result.content

    async def test_failed_call_carries_structured_content_even_without_with_stats(
        self, client: Any, async_request_mock: Any, mocker: MockerFixture
    ) -> None:
        """Test that a plain (non-`with_stats`) failed call still carries `structuredContent`, matching
        the same envelope shown in the text content, rather than only text.

        Regression test: this used to be `None` unless `with_stats` was requested, even though nothing
        about the payload prevented it from being structured - the MCP SDK only validates
        `structuredContent` against a tool's `output_schema` on a successful result, so there was no
        schema-safety reason to withhold it on a failure, and a failed call's body is often exactly
        what a model needs to parse and self-correct its next call.
        """
        async_request_mock.return_value = make_httpx_response(
            mocker, 400, json_body={"error": "widget_id must be positive"}
        )
        catalog_entry = _entry(CliTestClient, "widgets", "get_widget")
        result = await dispatch_endpoint_call(client, catalog_entry, {"widget_id": 999}, ServerOptions())
        assert result.is_error is True
        assert result.structured is not None
        assert result.structured["body"] == {"error": "widget_id must be positive"}

    async def test_raise_on_error_client_reports_the_identical_error_result(
        self, async_request_mock: Any, mocker: MockerFixture
    ) -> None:
        """Test that a `raise_on_error=True` client - which raises `HTTPStatusError` instead of
        returning the failed response - is re-wrapped into the same `isError` result a non-raising
        client would produce for the identical response, including the response body in both the
        text detail and structuredContent
        """
        async_request_mock.return_value = make_httpx_response(
            mocker, 404, json_body={"error": "widget_id must be positive"}
        )
        raising_client = CliTestClient(base_url="https://example.com/api", async_mode=True, raise_on_error=True)
        catalog_entry = _entry(CliTestClient, "widgets", "get_widget")
        result = await dispatch_endpoint_call(raising_client, catalog_entry, {"widget_id": 999}, ServerOptions())
        assert result.is_error is True
        assert "404" in result.content
        assert "widget_id must be positive" in result.content
        assert result.structured["body"] == {"error": "widget_id must be positive"}

    async def test_binary_body_produces_json_safe_structured_content(
        self, client: Any, async_request_mock: Any, mocker: MockerFixture
    ) -> None:
        """Test that a binary (non-JSON, non-UTF-8) response body never leaves raw bytes in
        structuredContent under any --response-format, since bytes can't be JSON-serialized
        """
        response = make_httpx_response(mocker, 200, content=b"\x89PNG\r\n\x1a\n\xff\xfe")
        response.text = "<binary>"
        async_request_mock.return_value = response
        catalog_entry = _entry(CliTestClient, "widgets", "get_widget")
        for response_format in ResponseFormat:
            options = ServerOptions(response_format=response_format)
            result = await dispatch_endpoint_call(client, catalog_entry, {"widget_id": 1}, options)
            assert result.is_error is False
            if result.structured is not None:
                json.dumps(result.structured)  # must not raise

    @pytest.mark.parametrize(
        "status_code,response_format",
        [(200, ResponseFormat.FULL), (500, ResponseFormat.FULL), (200, ResponseFormat.JSON)],
    )
    async def test_oversized_binary_body_is_truncated(
        self,
        client: Any,
        async_request_mock: Any,
        mocker: MockerFixture,
        status_code: int,
        response_format: ResponseFormat,
    ) -> None:
        """Test that an oversized binary response body is truncated the same way for a successful call,
        a failed call, and --response-format json, with `bytes` reporting the raw body's byte count
        """
        raw_content = bytes(range(256)) * (MAX_RESULT_BYTES // 256 + 10)
        async_request_mock.return_value = make_httpx_response(mocker, status_code, content=raw_content)
        catalog_entry = _entry(CliTestClient, "widgets", "get_widget")
        options = ServerOptions(response_format=response_format)
        result = await dispatch_endpoint_call(client, catalog_entry, {"widget_id": 1}, options)
        assert result.is_error is (status_code != 200)
        body = result.structured if response_format is ResponseFormat.JSON else result.structured["body"]
        assert body["truncated"] is True
        assert body["bytes"] == len(raw_content)

    async def test_enum_body_param_reaches_the_wire_as_its_value(self) -> None:
        """Test that an Enum-typed body parameter reaches the real request as its own wire value, not
        the member object.

        Goes through a real `httpx2.MockTransport`, not `async_request_mock`: that fixture patches
        `AsyncClient.request` at the method level, bypassing the exact JSON-encoding path (inside that
        same method) where a raw Enum member would raise `TypeError: Object of type ... is not JSON
        serializable`.
        """
        captured: dict[str, Any] = {}

        def handler(request: httpx2.Request) -> httpx2.Response:
            captured["body"] = json.loads(request.content)
            # Stream-backed, not preloaded via json=, so the client reads it the same way a real
            # transport does and populates .elapsed the way the logging hook needs.
            return httpx2.Response(
                200,
                stream=httpx2.ByteStream(json.dumps({"ok": True}).encode()),
                headers={"Content-Type": "application/json"},
            )

        rest_client = AsyncRestClient("https://example.com/api", transport=httpx2.MockTransport(handler))
        client = CliTestClient(async_mode=True, rest_client=rest_client)
        catalog_entry = _entry(CliTestClient, "widgets", "create_widget")
        result = await dispatch_endpoint_call(
            client, catalog_entry, {"name": "x", "owner_id": 1, "status": "ACTIVE"}, ServerOptions()
        )
        assert result.is_error is False
        assert captured["body"]["status"] == Status.ACTIVE.value

    async def test_bad_arguments_are_an_error_without_ever_reaching_the_network(
        self, client: Any, async_request_mock: Any
    ) -> None:
        """Test that an argument error is caught inside dispatch_endpoint_call() itself, returning an isError
        result without ever dispatching a request
        """
        catalog_entry = _entry(CliTestClient, "widgets", "get_widget")
        result = await dispatch_endpoint_call(client, catalog_entry, {}, ServerOptions())
        assert result.is_error is True
        assert "widget_id" in result.content
        async_request_mock.assert_not_called()

    async def test_connection_level_failure_is_returned_as_an_error_not_raised(
        self, client: Any, async_request_mock: Any
    ) -> None:
        """Test that a transport failure, which never produces a response to shape, is returned as an
        isError ToolResult (the model's only channel) rather than propagating out of
        dispatch_endpoint_call() as a raised exception
        """
        async_request_mock.side_effect = httpx2.ConnectError("connection refused")
        catalog_entry = _entry(CliTestClient, "widgets", "get_widget")
        result = await dispatch_endpoint_call(client, catalog_entry, {"widget_id": 1}, ServerOptions())
        assert result.is_error is True
        assert result.structured is None
        assert "ConnectError" in result.content

    async def test_connection_level_failure_names_the_request_when_recoverable(
        self, client: Any, async_request_mock: Any
    ) -> None:
        """Test that when the failed request is recoverable from the exception, the detail names its
        method and URL, so a model sees what the call was reaching for, not just a transport-error class
        """
        exc = httpx2.ReadTimeout("timed out")
        set_request_to_exception(exc, httpx2.Request("GET", "https://example.com/api/widgets/1"))
        async_request_mock.side_effect = exc
        catalog_entry = _entry(CliTestClient, "widgets", "get_widget")
        result = await dispatch_endpoint_call(client, catalog_entry, {"widget_id": 1}, ServerOptions())
        assert result.is_error is True
        assert "GET https://example.com/api/widgets/1" in result.content
        assert "ReadTimeout" in result.content

    async def test_response_format_json_returns_the_bare_decoded_body(
        self, client: Any, async_request_mock: Any, mocker: MockerFixture
    ) -> None:
        """Test that --response-format json returns just the decoded body, not the full envelope"""
        async_request_mock.return_value = make_httpx_response(mocker, 200, json_body={"id": 1})
        catalog_entry = _entry(CliTestClient, "widgets", "get_widget")
        options = ServerOptions(response_format=ResponseFormat.JSON)
        result = await dispatch_endpoint_call(client, catalog_entry, {"widget_id": 1}, options)
        assert result.structured == {"id": 1}

    async def test_response_format_raw_returns_undecoded_text(
        self, client: Any, async_request_mock: Any, mocker: MockerFixture
    ) -> None:
        """Test that --response-format raw returns the response's raw text, with no structured content"""
        async_request_mock.return_value = make_httpx_response(mocker, 200, json_body={"id": 1}, text="raw body text")
        catalog_entry = _entry(CliTestClient, "widgets", "get_widget")
        options = ServerOptions(response_format=ResponseFormat.RAW)
        result = await dispatch_endpoint_call(client, catalog_entry, {"widget_id": 1}, options)
        assert result.content == "raw body text"
        assert result.structured is None

    async def test_response_format_json_with_a_non_object_body_has_no_structured_content(
        self, client: Any, async_request_mock: Any, mocker: MockerFixture
    ) -> None:
        """Test that --response-format json with a body that doesn't decode to a JSON object (a bare
        list here) omits structuredContent rather than publishing a non-object value, which the MCP spec
        requires to be an object. The full body is still shown as text content.
        """
        async_request_mock.return_value = make_httpx_response(mocker, 200, json_body=[1, 2, 3])
        catalog_entry = _entry(CliTestClient, "widgets", "get_widget")
        options = ServerOptions(response_format=ResponseFormat.JSON)
        result = await dispatch_endpoint_call(client, catalog_entry, {"widget_id": 1}, options)
        assert result.structured is None
        assert "[" in result.content and "1" in result.content

    async def test_oversized_body_is_truncated_and_content_still_matches_structured(
        self, client: Any, async_request_mock: Any, mocker: MockerFixture
    ) -> None:
        """Test that an oversized response body is truncated end-to-end through dispatch_endpoint_call(), and
        that the text content and structuredContent still agree with each other, matching every other
        (non-truncated) result
        """
        oversized = {"items": ["x" * 100] * (MAX_RESULT_BYTES // 50)}
        async_request_mock.return_value = make_httpx_response(mocker, 200, json_body=oversized)
        catalog_entry = _entry(CliTestClient, "widgets", "get_widget")
        result = await dispatch_endpoint_call(client, catalog_entry, {"widget_id": 1}, ServerOptions())
        assert result.is_error is False
        assert result.structured["body"]["truncated"] is True
        assert json.loads(result.content) == result.structured

    async def test_oversized_raw_body_is_truncated(
        self, client: Any, async_request_mock: Any, mocker: MockerFixture
    ) -> None:
        """Test that --response-format raw truncates oversized undecoded text instead of returning it
        whole
        """
        oversized_text = "x" * (MAX_RESULT_BYTES + 1000)
        async_request_mock.return_value = make_httpx_response(mocker, 200, json_body={}, text=oversized_text)
        catalog_entry = _entry(CliTestClient, "widgets", "get_widget")
        options = ServerOptions(response_format=ResponseFormat.RAW)
        result = await dispatch_endpoint_call(client, catalog_entry, {"widget_id": 1}, options)
        assert len(result.content) < len(oversized_text)
        assert "[truncated:" in result.content

    async def test_quiet_is_always_none_regardless_of_log_requests(
        self, client: Any, async_request_mock: Any, mocker: MockerFixture
    ) -> None:
        """Test that dispatch always passes quiet=None, both by default and with --log-requests,
        deferring in both cases to the client-level log_requests flag _construct_client() already sets
        from that same option, rather than overriding it with an explicit True/False here
        """
        async_request_mock.return_value = make_httpx_response(mocker, 200, json_body={"id": 1})
        catalog_entry = _entry(CliTestClient, "widgets", "get_widget")
        spy = mocker.spy(Endpoint, "_call")

        await dispatch_endpoint_call(client, catalog_entry, {"widget_id": 1}, ServerOptions())
        assert spy.call_args.kwargs["quiet"] is None

        await dispatch_endpoint_call(client, catalog_entry, {"widget_id": 1}, ServerOptions(log_requests=True))
        assert spy.call_args.kwargs["quiet"] is None

    async def test_dispatch_succeeds_for_an_endpoint_without_kwargs(
        self, async_request_mock: Any, mocker: MockerFixture
    ) -> None:
        """Test that a call to an endpoint without `**kwargs: Unpack[Kwargs]` still succeeds.

        Regression test: an explicit `quiet=True` (the default --log-requests-off behavior before
        `quiet` was made unconditionally `None`) is forwarded to the original endpoint function whenever
        it accepts `**kwargs`, but raises `TypeError` for one that doesn't - the same class of failure
        `EndpointFunc._call()`'s own comment on `quiet` exists to avoid for a direct Python call.
        """
        async_request_mock.return_value = make_httpx_response(mocker, 200, json_body={"id": 1})
        client = _NoKwargsClient(base_url="https://example.com/api", async_mode=True)
        catalog_entry = _entry(_NoKwargsClient, "items", "get_item")
        result = await dispatch_endpoint_call(client, catalog_entry, {"item_id": 1}, ServerOptions())
        assert result.is_error is False
        assert result.structured == {"status_code": 200, "headers": {}, "body": {"id": 1}}


class TestCallWrappers:
    """Tests for `dispatch_endpoint_call()`'s `call_wrappers` handling: the dispatch fork through the
    bound `EndpointFunc`, list-result shaping, expected-status resolution, and the attached stats block.

    A wrapper routes the call through the bound `EndpointFunc`, bypassing the `Endpoint` facade, so these
    assert on the mocked transport (`async_request_mock`) rather than spying `Endpoint._call`.
    """

    @pytest.fixture
    def client(self, async_request_mock: Any) -> Any:
        return CliTestClient(base_url="https://example.com/api", async_mode=True)

    async def test_bad_spec_is_an_error_without_reaching_the_network(
        self, client: Any, async_request_mock: Any
    ) -> None:
        """Test that a malformed `call_wrappers` object is caught before dispatch, returning an isError
        result and never issuing a request
        """
        entry = _entry(CliTestClient, "widgets", "get_widget")
        result = await dispatch_endpoint_call(
            client, entry, {"widget_id": 1}, ServerOptions(), call_wrappers={"with_bogus": {}}
        )
        assert result.is_error is True
        assert "Unknown call wrapper" in result.content

    async def test_a_chain_construction_failure_is_a_clean_error_not_an_unhandled_exception(
        self, client: Any, async_request_mock: Any, mocker: MockerFixture
    ) -> None:
        """Test that a failure while folding the wrapper chain onto the bound endpoint func - anything
        `parse_call_wrappers()` didn't already reject up front - is reported as a clean isError result
        rather than propagating to the caller's generic unhandled-error catch and logging a traceback.

        `apply_wrappers()` is mocked to raise directly, rather than relying on a specific wrapper spec to
        trigger the failure, so this stays a regression test for the dispatcher's own boundary regardless
        of which wrapper misconfigurations `parse_call_wrappers()` currently catches.
        """
        mocker.patch("api_client_core.mcp.runner.apply_wrappers", side_effect=ValueError("boom"))
        entry = _entry(CliTestClient, "widgets", "get_widget")
        result = await dispatch_endpoint_call(
            client, entry, {"widget_id": 1}, ServerOptions(), call_wrappers={"with_stats": {}, "with_retry": {}}
        )
        assert result.is_error is True
        assert result.content == "boom"
        async_request_mock.assert_not_called()

    async def test_with_repeat_issues_n_requests_and_returns_a_results_list(
        self, client: Any, async_request_mock: Any, mocker: MockerFixture
    ) -> None:
        """Test that `with_repeat` dispatches the call N times and shapes the result as a
        `{"results": [...]}` object with one envelope per call
        """
        async_request_mock.return_value = make_httpx_response(mocker, 200, json_body={"ok": True})
        entry = _entry(CliTestClient, "widgets", "list_widgets")
        result = await dispatch_endpoint_call(
            client, entry, {}, ServerOptions(), call_wrappers={"with_repeat": {"num": 3}}
        )
        assert async_request_mock.call_count == 3
        assert result.is_error is False
        assert len(result.structured["results"]) == 3
        assert result.structured["results"][0]["status_code"] == 200

    async def test_a_list_result_with_one_failed_call_is_an_error_but_keeps_every_item(
        self, client: Any, async_request_mock: Any, mocker: MockerFixture
    ) -> None:
        """Test that `return_exceptions` (defaulted true for MCP) keeps all N items when one call fails,
        and the overall result is flagged isError
        """
        async_request_mock.side_effect = [
            make_httpx_response(mocker, 200, json_body={"n": 1}),
            make_httpx_response(mocker, 200, json_body={"n": 2}),
            make_httpx_response(mocker, 500, json_body={"error": "boom"}),
        ]
        entry = _entry(CliTestClient, "widgets", "list_widgets")
        result = await dispatch_endpoint_call(
            client, entry, {}, ServerOptions(), call_wrappers={"with_concurrency": {"num": 3}}
        )
        assert result.is_error is True
        assert len(result.structured["results"]) == 3
        assert {item["status_code"] for item in result.structured["results"]} == {200, 500}

    async def test_with_retry_retries_until_the_response_is_ok(
        self, client: Any, async_request_mock: Any, mocker: MockerFixture
    ) -> None:
        """Test that `with_retry` re-dispatches a non-OK response and returns the first OK one, with no
        list shaping (retry is not a multi-call wrapper)
        """
        async_request_mock.side_effect = [
            make_httpx_response(mocker, 503, json_body={"error": "unavailable"}),
            make_httpx_response(mocker, 200, json_body={"recovered": True}),
        ]
        entry = _entry(CliTestClient, "widgets", "list_widgets")
        result = await dispatch_endpoint_call(
            client, entry, {}, ServerOptions(), call_wrappers={"with_retry": {"num_retries": 2, "retry_after": 0}}
        )
        assert async_request_mock.call_count == 2
        assert result.is_error is False
        assert result.structured["body"] == {"recovered": True}

    async def test_with_expected_status_makes_a_declared_non_2xx_a_success(
        self, client: Any, async_request_mock: Any, mocker: MockerFixture
    ) -> None:
        """Test that a status listed in `with_expected_status` is reported as a non-error result that
        still carries the response body, since the model asked for exactly that status
        """
        async_request_mock.return_value = make_httpx_response(mocker, 404, json_body={"detail": "gone"})
        entry = _entry(CliTestClient, "widgets", "get_widget")
        result = await dispatch_endpoint_call(
            client,
            entry,
            {"widget_id": 9},
            ServerOptions(),
            call_wrappers={"with_expected_status": {"status_codes": [404]}},
        )
        assert result.is_error is False
        assert result.structured == {"status_code": 404, "headers": {}, "body": {"detail": "gone"}}

    async def test_a_failed_wrapper_assertion_is_a_clean_error(
        self, client: Any, async_request_mock: Any, mocker: MockerFixture
    ) -> None:
        """Test that an `AssertionError` from `with_expected_status` (the actual status not among the
        declared ones) is caught and returned as a one-line isError result, not left to propagate
        """
        async_request_mock.return_value = make_httpx_response(mocker, 500, json_body={"error": "boom"})
        entry = _entry(CliTestClient, "widgets", "get_widget")
        result = await dispatch_endpoint_call(
            client,
            entry,
            {"widget_id": 9},
            ServerOptions(),
            call_wrappers={"with_expected_status": {"status_codes": [200]}},
        )
        assert result.is_error is True
        assert "assertion failed" in result.content.lower()
        assert "500" in result.content

    async def test_with_stats_attaches_a_stats_block_on_a_successful_call(
        self, client: Any, async_request_mock: Any, mocker: MockerFixture
    ) -> None:
        """Test that `with_stats` folds a `stats` array into the result envelope, since its printed
        table would otherwise reach only the server's stderr
        """
        async_request_mock.return_value = make_httpx_response(mocker, 200, json_body={"ok": True})
        entry = _entry(CliTestClient, "widgets", "list_widgets")
        result = await dispatch_endpoint_call(client, entry, {}, ServerOptions(), call_wrappers={"with_stats": {}})
        assert result.is_error is False
        assert result.structured["stats"][0]["num_calls"] == 1
        assert result.structured["stats"][0]["num_2xx"] == 1

    async def test_with_stats_attaches_a_stats_block_on_a_failed_call(
        self, client: Any, async_request_mock: Any, mocker: MockerFixture
    ) -> None:
        """Test that a `with_stats` request on a call that then fails still returns its report, folded
        into the failure envelope
        """
        async_request_mock.return_value = make_httpx_response(mocker, 404, json_body={"error": "nope"})
        entry = _entry(CliTestClient, "widgets", "get_widget")
        result = await dispatch_endpoint_call(
            client, entry, {"widget_id": 9}, ServerOptions(), call_wrappers={"with_stats": {}}
        )
        assert result.is_error is True
        assert result.structured["stats"][0]["num_4xx"] == 1

    async def test_an_oversized_list_result_truncates_the_results_array(
        self, client: Any, async_request_mock: Any, mocker: MockerFixture
    ) -> None:
        """Test that a list result over `MAX_RESULT_BYTES` replaces `results` with a truncation marker
        dict, which is why the output schema leaves `results` unconstrained
        """
        big = "x" * (MAX_RESULT_BYTES // 2)
        async_request_mock.return_value = make_httpx_response(mocker, 200, json_body={"blob": big})
        entry = _entry(CliTestClient, "widgets", "list_widgets")
        result = await dispatch_endpoint_call(
            client, entry, {}, ServerOptions(), call_wrappers={"with_repeat": {"num": 3}}
        )
        assert result.structured["results"]["truncated"] is True

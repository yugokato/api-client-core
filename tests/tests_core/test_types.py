"""Unit tests for the types defined in `core/types.py`."""

from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import MISSING, field
from typing import Any

import pytest

from api_client_core.core.types import Alias, DataclassModelField, File, MultipartFormData


class TestFile:
    """Tests for the File dataclass"""

    def test_creation(self) -> None:
        """Test that File stores filename, content, and content_type"""
        f = File("logo.png", b"\x89PNG", "image/png")
        assert f.filename == "logo.png"
        assert f.content == b"\x89PNG"
        assert f.content_type == "image/png"

    def test_to_tuple(self) -> None:
        """Test that to_tuple() returns (filename, content, content_type)"""
        f = File("doc.pdf", b"%PDF", "application/pdf")
        assert f.to_tuple() == ("doc.pdf", b"%PDF", "application/pdf")

    def test_dict_behavior(self) -> None:
        """Test that File also behaves as a dict (filename/content/content_type are keys)"""
        f = File("img.jpg", b"JFIF", "image/jpeg")
        assert f["filename"] == "img.jpg"
        assert f["content"] == b"JFIF"
        assert f["content_type"] == "image/jpeg"

    def test_string_content(self) -> None:
        """Test that File can hold string content, not just bytes"""
        f = File("notes.txt", "Hello World", "text/plain")
        assert f.content == "Hello World"
        assert f.to_tuple() == ("notes.txt", "Hello World", "text/plain")


class TestMultipartFormData:
    """Tests for the MultipartFormData MutableMapping wrapper"""

    @pytest.fixture(scope="class")
    @classmethod
    def logo(cls) -> File:
        return File("logo.png", b"logo-content", "image/png")

    @pytest.fixture(scope="class")
    @classmethod
    def favicon(cls) -> File:
        return File("favicon.ico", b"fav-content", "image/x-icon")

    @pytest.fixture(scope="class")
    @classmethod
    def dict_file(cls) -> dict[str, Any]:
        return {"filename": "data.csv", "content": b"col1,col2", "content_type": "text/csv"}

    def test_init_with_file_objects(self, logo: File, favicon: File) -> None:
        """Test that File objects are stored as tuples via File.to_tuple()"""
        files = MultipartFormData(logo=logo, favicon=favicon)
        assert files["logo"] == logo.to_tuple()
        assert files["favicon"] == favicon.to_tuple()

    def test_init_with_dict_objects(self, dict_file: dict[str, Any]) -> None:
        """Test that dict values are stored as a tuple of their values"""
        files = MultipartFormData(data=dict_file)
        assert files["data"] == tuple(dict_file.values())

    def test_init_skips_falsy_values(self, logo: File) -> None:
        """Test that entries with falsy file values (None, empty string, etc.) are excluded"""
        files = MultipartFormData(logo=logo, empty=None)
        assert "logo" in files
        assert "empty" not in files
        assert len(files) == 1

    def test_getitem(self, logo: File) -> None:
        """Test that __getitem__ returns the tuple stored under the given key"""
        files = MultipartFormData(logo=logo)
        assert files["logo"] == ("logo.png", b"logo-content", "image/png")

    def test_setitem_file(self, logo: File) -> None:
        """Test that assigning a File object converts it to a tuple"""
        files = MultipartFormData(logo=logo)
        new_logo = File("new-logo.png", b"new-content", "image/png")
        files["logo"] = new_logo
        assert files["logo"] == new_logo.to_tuple()

    def test_setitem_dict(self, dict_file: dict[str, Any]) -> None:
        """Test that assigning a dict converts it to a tuple of its values"""
        files = MultipartFormData(data=dict_file)
        new_dict = {"filename": "updated.csv", "content": b"a,b", "content_type": "text/csv"}
        files["data"] = new_dict
        assert files["data"] == tuple(new_dict.values())

    def test_setitem_raw(self, logo: File) -> None:
        """Test that assigning a raw (non-File, non-dict) value stores it as-is"""
        files = MultipartFormData(logo=logo)
        raw_tuple = ("raw.png", b"raw", "image/png")
        files["logo"] = raw_tuple
        assert files["logo"] == raw_tuple

    def test_delitem(self, logo: File, favicon: File) -> None:
        """Test that deleting a key removes it and subsequent access raises KeyError"""
        files = MultipartFormData(logo=logo, favicon=favicon)
        del files["logo"]
        assert "logo" not in files
        with pytest.raises(KeyError):
            _ = files["logo"]

    def test_iter(self, logo: File, favicon: File) -> None:
        """Test that iterating over MultipartFormData yields the key names"""
        files = MultipartFormData(logo=logo, favicon=favicon)
        keys = list(files)
        assert set(keys) == {"logo", "favicon"}

    def test_len(self, logo: File, favicon: File) -> None:
        """Test that len() returns the number of stored files"""
        files = MultipartFormData(logo=logo, favicon=favicon)
        assert len(files) == 2

    def test_to_dict(self, logo: File) -> None:
        """Test that to_dict() returns a plain dict of {name: tuple} pairs"""
        files = MultipartFormData(logo=logo)
        result = files.to_dict()
        assert isinstance(result, dict)
        assert result == {"logo": logo.to_tuple()}

    def test_mutable_mapping_protocol(self, logo: File, favicon: File) -> None:
        """Test that MultipartFormData is a MutableMapping whose standard mapping methods work"""
        files = MultipartFormData(logo=logo, favicon=favicon)
        assert isinstance(files, MutableMapping)
        assert set(files.keys()) == {"logo", "favicon"}
        assert len(list(files.values())) == 2
        assert len(list(files.items())) == 2
        for key, value in files.items():
            assert value == files[key]

    def test_string_content(self) -> None:
        """Test that a File with string (non-bytes) content is handled without error"""
        text_file = File("notes.txt", "plain text content", "text/plain")
        files = MultipartFormData(notes=text_file)
        assert files["notes"] == text_file.to_tuple()

    def test_attribute_access(self, logo: File, favicon: File) -> None:
        """Test that attribute-style access reads the correct value from each instance"""
        files = MultipartFormData(logo=logo, favicon=favicon)
        assert files.logo == logo.to_tuple()
        assert files.favicon == favicon.to_tuple()

    def test_attribute_missing_raises(self, logo: File) -> None:
        """Test that accessing a non-existent key via attribute raises AttributeError"""
        files = MultipartFormData(logo=logo)
        assert not hasattr(files, "nonexistent")
        with pytest.raises(AttributeError):
            _ = files.nonexistent


class TestAlias:
    """Tests for the Alias frozen dataclass"""

    def test_creation(self) -> None:
        """Test that Alias stores its value string"""
        v = "x-param-name"
        a = Alias(v)
        assert a.value == v

    def test_frozen(self) -> None:
        """Test that Alias is frozen so assignment raises FrozenInstanceError"""
        a = Alias("original")
        with pytest.raises(Exception):  # FrozenInstanceError (AttributeError in older Python)
            # noinspection PyDataclass
            a.value = "changed"


class TestDataclassModelField:
    """Tests for the DataclassModelField NamedTuple"""

    def test_creation(self) -> None:
        """Test that DataclassModelField stores name and type with a MISSING default"""
        f = DataclassModelField(name="param1", type=str)
        assert f.name == "param1"
        assert f.type is str
        assert f.default is MISSING

    def test_with_default(self) -> None:
        """Test that DataclassModelField stores an explicit default value"""
        default_field = field(default=None)
        f = DataclassModelField(name="optional_param", type=int, default=default_field)
        assert f.name == "optional_param"
        assert f.type is int
        assert f.default is default_field

    def test_namedtuple_unpacking(self) -> None:
        """Test that DataclassModelField supports tuple unpacking via NamedTuple"""
        f = DataclassModelField(name="x", type=float)
        name, tp, default = f
        assert name == "x"
        assert tp is float
        assert default is MISSING

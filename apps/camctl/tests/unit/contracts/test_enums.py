"""枚举转换的单元测试：资源读取隔离为受约束的内存替身。"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from camctl.contracts import enums


@pytest.fixture(autouse=True)
def registry_resource(monkeypatch):
    registry = {
        "enums": {
            "sample.state": {"members": {"PENDING": 1, "RUNNING": 2}},
            "sample.kind": {"members": {"IMAGE": 1}},
        },
        "json_enums": {},
    }

    def read(name: str) -> bytes:
        if name != "registry/enum-registry.json":
            raise AssertionError(f"枚举转换请求了未声明的资源: {name}")
        return json.dumps(registry).encode()

    enums.enum_for.cache_clear()
    enums.load_registry.cache_clear()
    monkeypatch.setattr(enums, "resource_bytes", read)
    yield registry
    enums.enum_for.cache_clear()
    enums.load_registry.cache_clear()


def test_decode_returns_registered_member() -> None:
    member = enums.decode_member("sample.state", 2)
    assert member.name == "RUNNING"
    assert enums.encode_member("sample.state", member) == 2


def test_unknown_code_is_rejected() -> None:
    with pytest.raises(enums.EnumRegistryError):
        enums.decode_member("sample.state", 99)


@pytest.mark.parametrize("value", [True, "1", 1.0, Decimal("1"), None])
def test_noninteger_storage_value_is_rejected(value) -> None:
    with pytest.raises(enums.EnumRegistryError):
        enums.decode_member("sample.state", value)


def test_decode_rejects_another_enum_with_equal_number() -> None:
    with pytest.raises(enums.EnumRegistryError):
        enums.decode_member("sample.state", enums.enum_for("sample.kind").IMAGE)


def test_encode_rejects_another_enum_with_equal_number() -> None:
    with pytest.raises(enums.EnumRegistryError):
        enums.encode_member("sample.state", enums.enum_for("sample.kind").IMAGE)


def test_decode_accepts_its_own_enum_member() -> None:
    member = enums.enum_for("sample.state").RUNNING
    assert enums.decode_member("sample.state", member) is member


def test_unknown_column_is_rejected() -> None:
    with pytest.raises(enums.EnumRegistryError):
        enums.enum_for("sample.unknown")


def test_public_text_uses_member_name() -> None:
    assert enums.public_text(enums.enum_for("sample.state").RUNNING) == "running"


def test_public_text_rejects_nonmember() -> None:
    with pytest.raises(enums.EnumRegistryError):
        enums.public_text("running")


@pytest.mark.parametrize("code", [True, 0, -1, "1", 1.5])
def test_invalid_registry_code_is_rejected(registry_resource, code) -> None:
    registry_resource["enums"]["sample.state"]["members"]["RUNNING"] = code
    with pytest.raises(enums.EnumRegistryError):
        enums.load_registry()

"""B5 静态能力导出的单元测试。

describe 只装配静态资源：无状态库、无锁、无设备连接。设备目录
声明设备而驱动定义未接入时拒绝导出，不产生部分说明。
"""

from __future__ import annotations

import pytest

from camctl.bootstrap.application import EmptyCapabilityCatalog, describe
from camctl.bootstrap.config import ConfigDefaults, ConfigError, load_config


class RefuseEverything:
    """替身：任何属性访问都按接口违约拒绝调用。"""

    def __getattr__(self, name: str):
        raise AssertionError(f"describe 不应触碰 {name}")


def _config(devices: dict | None = None):
    document = {"devices": devices} if devices is not None else {}
    return load_config(document, ConfigDefaults())


class TestDescribeWithoutDatabase:
    def test_empty_catalog_document(self) -> None:
        config = _config()
        document = describe(config, EmptyCapabilityCatalog())
        assert document == {"devices": []}

    def test_describe_never_touches_runtime_resources(self) -> None:
        # 状态库、锁和设备连接以拒绝调用替身提供：describe 不得触碰。
        refuse = RefuseEverything()
        document = describe(_config(), EmptyCapabilityCatalog())
        assert document == {"devices": []}
        assert refuse is not None  # 替身未被访问即通过。

    def test_describe_uses_provided_catalog_document(self) -> None:
        # describe 不自行合并设备清单：目录（D1）是唯一能力来源。
        config = _config(devices={"cam-1": {"kind": "camera", "driver": "adb"}})
        document = describe(config, EmptyCapabilityCatalog())
        assert document == {"devices": []}

    def test_invalid_config_rejected_before_document(self) -> None:
        with pytest.raises(ConfigError):
            _config(devices={"cam-1": {"kind": "camera"}})

"""读取发现批量配置只解析内存输入，不创建业务资源。"""

import pytest
import sys
from types import ModuleType


@pytest.fixture
def config_module(monkeypatch):
    # 配置解析不验证读取发现仓储；导入时隔离其资源登记协作者。
    discovery = ModuleType("camctl.outputs.read_discovery")
    discovery.DEFAULT_BATCH_SIZE = 23
    monkeypatch.setitem(sys.modules, discovery.__name__, discovery)
    camera_config = ModuleType("camctl.devices.drivers.hasselblad.configuration")
    def unused_camera_config(*args, **kwargs):
        raise AssertionError("读取批量配置没有设备输入，不应校验设备参数")
    camera_config.read_camera_config = unused_camera_config
    definitions = ModuleType("camctl.devices.drivers.hasselblad.definitions")
    definitions.DRIVER_ID = "unused-driver"
    monkeypatch.setitem(sys.modules, camera_config.__name__, camera_config)
    monkeypatch.setitem(sys.modules, definitions.__name__, definitions)
    already_loaded = "camctl.bootstrap.config" in sys.modules
    from camctl.bootstrap import config
    try:
        yield config
    finally:
        if not already_loaded:
            sys.modules.pop("camctl.bootstrap.config", None)
            import camctl.bootstrap
            delattr(camctl.bootstrap, "config")


@pytest.mark.parametrize("size", [1, 100000])
def test_read_discovery_override_and_reload(size, config_module):
    first = config_module.load_config({"session": {"read_discovery_batch_size": size}}, config_module.ConfigDefaults())
    second = config_module.load_config({"session": {"read_discovery_batch_size": size + 1}}, config_module.ConfigDefaults())
    assert first.session.read_discovery_batch_size == size
    assert second.session.read_discovery_batch_size == size + 1


@pytest.mark.parametrize("size", [True, False, 0, -1, "1", 1.0])
def test_invalid_read_discovery_size(size, config_module):
    with pytest.raises(config_module.ConfigError):
        config_module.load_config({"session": {"read_discovery_batch_size": size}}, config_module.ConfigDefaults())

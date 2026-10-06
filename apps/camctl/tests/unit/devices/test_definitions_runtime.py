"""已部署驱动定义进程启动登记点的单元测试。

登记是部署装配接入面：重复身份拒绝且不产生部分登记；快照不受
后续登记影响；默认定义来源只反映登记结果，reset 仅用于隔离。
"""

from __future__ import annotations

import pytest

from camctl.devices.catalog import (
    ActionCapability,
    default_driver_definitions,
)
from camctl.devices.definitions_runtime import (
    current_driver_definitions,
    register_driver_definitions,
    reset_driver_definitions,
)

_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {"type": {"const": "single_shot"}},
    "required": ["type"],
    "additionalProperties": False,
}


def _definition(driver_id: str):
    return ActionCapability(
        action_type="camera_take_photo", parameter_type="single_shot",
        name="单张拍摄", description="登记点测试定义",
        preview_supported=False, schema=_SCHEMA, defaults={},
    )


def _entry(driver_id: str):
    from camctl.devices.catalog import DriverDefinition

    return DriverDefinition(
        driver_id=driver_id,
        actions={"camera_take_photo": (_definition(driver_id),)},
    )


@pytest.fixture(autouse=True)
def _isolated():
    reset_driver_definitions()
    yield
    reset_driver_definitions()


def test_empty_registration_is_visible_as_empty_default():
    assert default_driver_definitions().drivers == {}
    assert current_driver_definitions().drivers == {}


def test_registered_definition_appears_in_default_source():
    register_driver_definitions(_entry("deployed-a"))
    assert set(default_driver_definitions().drivers) == {"deployed-a"}


def test_duplicate_registration_is_rejected_without_partial_effect():
    register_driver_definitions(_entry("deployed-a"))
    with pytest.raises(ValueError, match="重复登记"):
        register_driver_definitions(_entry("deployed-a"))
    assert set(current_driver_definitions().drivers) == {"deployed-a"}


def test_snapshot_does_not_reflect_later_registrations():
    register_driver_definitions(_entry("deployed-a"))
    snapshot = current_driver_definitions()
    register_driver_definitions(_entry("deployed-b"))
    assert set(snapshot.drivers) == {"deployed-a"}
    assert set(current_driver_definitions().drivers) == {"deployed-a", "deployed-b"}

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
from device_driver_fixtures import (
    synthetic_driver_declaration,
    synthetic_source_storage_resolver,
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
        declaration=synthetic_driver_declaration(),
        source_storage_resolver=synthetic_source_storage_resolver,
    )


def _runtime_entry(driver_id: str, declaration):
    from camctl.devices.drivers.registry import DriverEntry, DriverStatus
    from camctl.devices.evidence import EvidenceRegistry

    return DriverEntry(
        driver_id=driver_id,
        driver=object(),
        declaration=declaration,
        evidence=EvidenceRegistry(()),
        status=DriverStatus.DEVICE_VERIFICATION_PENDING,
    )


@pytest.fixture(autouse=True)
def _isolated():
    from camctl.devices.drivers.runtime import reset_drivers

    reset_driver_definitions()
    reset_drivers()
    yield
    reset_driver_definitions()
    reset_drivers()


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


def test_duplicate_ids_in_definition_batch_are_rejected_without_partial_effect():
    with pytest.raises(ValueError, match="重复登记"):
        register_driver_definitions(_entry("deployed-a"), _entry("deployed-a"))

    assert current_driver_definitions().drivers == {}


def test_conflicting_runtime_declaration_rejects_definition_batch_before_write():
    from dataclasses import replace
    from camctl.devices.drivers.runtime import register_drivers

    preserved_definition = _entry("preserved")
    register_driver_definitions(preserved_definition)
    runtime_declaration = synthetic_driver_declaration()
    conflicting_declaration = replace(runtime_declaration, delete_supported=True)
    register_drivers(_runtime_entry("deployed-b", runtime_declaration))

    with pytest.raises(ValueError, match="声明冲突"):
        register_driver_definitions(
            _entry("deployed-a"),
            replace(_entry("deployed-b"), declaration=conflicting_declaration),
        )

    assert current_driver_definitions().drivers == {"preserved": preserved_definition}


def test_conflicting_static_declaration_rejects_runtime_batch_before_write():
    from dataclasses import replace
    from camctl.devices.drivers.runtime import current_registry, register_drivers

    static_declaration = synthetic_driver_declaration()
    conflicting_declaration = replace(static_declaration, delete_supported=True)
    register_driver_definitions(_entry("deployed-b"))
    preserved_entry = _runtime_entry("preserved", static_declaration)
    register_drivers(preserved_entry)

    with pytest.raises(ValueError, match="声明冲突"):
        register_drivers(
            _runtime_entry("deployed-a", static_declaration),
            _runtime_entry("deployed-b", conflicting_declaration),
        )

    assert current_registry().entry("deployed-a") is None
    assert current_registry().entry("deployed-b") is None
    assert current_registry().entry("preserved") is preserved_entry


def test_duplicate_ids_in_runtime_batch_are_rejected_without_partial_effect():
    from camctl.devices.drivers.runtime import current_registry, register_drivers

    declaration = synthetic_driver_declaration()
    with pytest.raises(ValueError, match="重复登记"):
        register_drivers(
            _runtime_entry("deployed-a", declaration),
            _runtime_entry("deployed-a", declaration),
        )

    assert current_registry().entry("deployed-a") is None


def test_snapshot_does_not_reflect_later_registrations():
    register_driver_definitions(_entry("deployed-a"))
    snapshot = current_driver_definitions()
    register_driver_definitions(_entry("deployed-b"))
    assert set(snapshot.drivers) == {"deployed-a"}
    assert set(current_driver_definitions().drivers) == {"deployed-a", "deployed-b"}

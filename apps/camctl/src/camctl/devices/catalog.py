"""同源静态能力与参数规则。

已部署驱动定义是唯一静态来源：同一份定义供 describe 导出与受
理校验使用，不复制第二份 Schema 或默认值清单。只导出已部署且设
备实际声明的能力，不把预留名称当实现。
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from camctl.acceptance.ports import ParameterDefinition
from camctl.bootstrap.config import ConfigSnapshot

__all__ = [
    "ActionCapability",
    "DriverDefinition",
    "DriverDefinitions",
    "apply_defaults",
    "build_catalog",
]

#: 第一版按设备种类声明的能力（camera 专属；MCU 仅预留）。
_KIND_ACTION_TYPES = {
    "camera": frozenset(
        {"camera_take_photo", "camera_record", "camera_timelapse"}
    ),
}


@dataclass(frozen=True)
class ActionCapability:
    """一种设备动作的参数规则与导出说明（同源）。"""

    action_type: str
    parameter_type: str
    name: str
    description: str
    preview_supported: bool
    schema: Mapping[str, Any]
    defaults: Mapping[str, Any]


@dataclass(frozen=True)
class DriverDefinition:
    """一个已部署驱动的静态能力定义。"""

    driver_id: str
    actions: Mapping[str, ActionCapability]


@dataclass(frozen=True)
class DriverDefinitions:
    """已部署驱动的单一静态来源。"""

    drivers: Mapping[str, DriverDefinition]


def apply_defaults(
    raw: Mapping[str, Any], defaults: Mapping[str, Any]
) -> dict[str, Any]:
    """应用默认值生成生效参数；原输入保持不变。

    显式提供的键（包括显式 null）不被默认值覆盖；省略的合法可默
    认字段由定义补齐。
    """
    effective = dict(defaults)
    effective.update(raw)
    return effective


class Catalog:
    """从配置设备声明与驱动定义构建的静态能力目录。"""

    def __init__(self, config: ConfigSnapshot, definitions: DriverDefinitions) -> None:
        self._devices: dict[str, Mapping[str, Any]] = dict(config.devices)
        self._definitions = definitions
        self._capabilities = self._resolve_capabilities()

    def _resolve_capabilities(self) -> dict[str, tuple[str, DriverDefinition, frozenset[str]]]:
        resolved: dict[str, tuple[str, DriverDefinition, frozenset[str]]] = {}
        for device_id, declaration in self._devices.items():
            driver_id = declaration.get("driver")
            definition = self._definitions.drivers.get(driver_id)
            if definition is None:
                raise ValueError(f"设备 {device_id} 声明的驱动未部署: {driver_id!r}")
            kind = declaration.get("kind")
            declared = _KIND_ACTION_TYPES.get(kind, frozenset())
            resolved[device_id] = (driver_id, definition, declared)
        return resolved

    def action_types(self) -> frozenset[str]:
        return frozenset(
            action_type
            for _, (_, definition, declared) in self._capabilities.items()
            for action_type in definition.actions
            if action_type in declared
        ) | {
            "obtain_action_outputs",
            "delete_action_outputs",
            "cancel_task",
            "report_status",
        }

    def device_exists(self, device_id: str) -> bool:
        return device_id in self._capabilities

    def driver_id(self, device_id: str) -> str | None:
        entry = self._capabilities.get(device_id)
        return entry[0] if entry is not None else None

    def parameter_definition(
        self, device_id: str, action_type: str
    ) -> ParameterDefinition | None:
        entry = self._capabilities.get(device_id)
        if entry is None:
            return None
        _, definition, declared = entry
        if action_type not in declared:
            return None
        capability = definition.actions.get(action_type)
        if capability is None:
            return None
        return ParameterDefinition(
            schema=dict(capability.schema), defaults=dict(capability.defaults)
        )

    def document(self) -> dict[str, Any]:
        """能力说明文档（CapabilityCatalog 端口入口）。"""
        return self.describe_document()

    def describe_document(self) -> dict[str, Any]:
        """生成完整能力说明文档（与受理使用同一定义）。"""
        devices = []
        for device_id, (driver_id, definition, declared) in sorted(
            self._capabilities.items()
        ):
            actions = [
                {
                    "type": capability.action_type,
                    "parameter_types": [
                        {
                            "type": capability.parameter_type,
                            "name": capability.name,
                            "description": capability.description,
                            "preview_supported": capability.preview_supported,
                            "schema": dict(capability.schema),
                        }
                    ],
                }
                for action_type, capability in sorted(definition.actions.items())
                if action_type in declared
            ]
            devices.append(
                {
                    "device_id": device_id,
                    "driver_id": driver_id,
                    "actions": actions,
                }
            )
        return {"devices": devices}


def build_catalog(config: ConfigSnapshot, definitions: DriverDefinitions) -> Catalog:
    """按生效配置与已部署驱动定义构建静态能力目录。

    声明的驱动缺失定义属于部署错误（规则处理错误），不接受部分
    目录；空设备目录合法。
    """
    return Catalog(config, definitions)

def default_driver_definitions() -> DriverDefinitions:
    """当前进程内置的已部署驱动定义。

    第一版相机驱动定义随驱动接入登记于此；未部署的驱动不出现，
    声明了未部署驱动的设备在目录构建时报部署错误。
    """
    return DriverDefinitions(drivers={})

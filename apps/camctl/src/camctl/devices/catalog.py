"""同源静态能力与参数规则。

已部署驱动定义是唯一静态来源：同一份定义供 describe 导出与受
理校验使用，不复制第二份 Schema 或默认值清单。只导出已部署且设
备实际声明的能力，不把预留名称当实现。
"""

from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
from types import MappingProxyType
from typing import Any, Mapping

from camctl.acceptance.ports import ParameterDefinition
from camctl.acceptance.schema import RuleError
from camctl.bootstrap.config import ConfigSnapshot
from camctl.devices.tasks import CaptureTaskFactory
from camctl.devices.parameter_schemas import validate_parameter_schema, schema_with_defaults

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
    task_factory: CaptureTaskFactory | None = None


@dataclass(frozen=True)
class DriverDefinition:
    """一个已部署驱动的静态能力定义。"""

    driver_id: str
    actions: Mapping[str, tuple[ActionCapability, ...]]


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
        for driver_id, definition in definitions.drivers.items():
            if definition.driver_id != driver_id:
                raise RuleError("驱动身份与登记键矛盾")
            for action_type, capabilities in definition.actions.items():
                if not capabilities:
                    raise RuleError("已登记动作必须至少具有一种完整参数类型")
                seen = set()
                for capability in capabilities:
                    if (capability.action_type != action_type or not capability.parameter_type
                            or capability.parameter_type in seen or not isinstance(capability.preview_supported, bool)):
                        raise RuleError("驱动动作及参数类型定义矛盾")
                    seen.add(capability.parameter_type)
        self._implemented = frozenset(
            action_type for definition in definitions.drivers.values() for action_type in definition.actions
        ) | {"obtain_action_outputs", "delete_action_outputs", "cancel_task", "report_status"}
        self._capabilities = self._resolve_capabilities()

    def _resolve_capabilities(self) -> dict[str, tuple[str, DriverDefinition, frozenset[str]]]:
        resolved: dict[str, tuple[str, DriverDefinition, frozenset[str]]] = {}
        for device_id, declaration in self._devices.items():
            driver_id = declaration.get("driver")
            definition = self._definitions.drivers.get(driver_id)
            if definition is None:
                raise RuleError(f"设备 {device_id} 声明的驱动未部署: {driver_id!r}")
            kind = declaration.get("kind")
            declared = _KIND_ACTION_TYPES.get(kind, frozenset())
            resolved[device_id] = (driver_id, definition, declared)
        return resolved

    def action_types(self) -> frozenset[str]:
        return self._implemented

    def device_supports(self, device_id: str, action_type: str) -> bool:
        entry = self._capabilities.get(device_id)
        return entry is not None and action_type in entry[2] and action_type in entry[1].actions

    def device_exists(self, device_id: str) -> bool:
        return device_id in self._capabilities

    def driver_id(self, device_id: str) -> str | None:
        entry = self._capabilities.get(device_id)
        return entry[0] if entry is not None else None

    def parameter_definition(
        self, device_id: str, action_type: str, parameter_type: str
    ) -> ParameterDefinition | None:
        if not self.device_supports(device_id, action_type):
            return None
        _, definition, _ = self._capabilities[device_id]
        capabilities = definition.actions[action_type]
        seen: set[str] = set()
        found = None
        for capability in capabilities:
            if capability.action_type != action_type or capability.parameter_type in seen:
                raise RuleError("驱动能力与参数类型登记矛盾")
            seen.add(capability.parameter_type)
            if capability.parameter_type == parameter_type:
                found = capability
        if found is None:
            return None
        if not isinstance(found.preview_supported, bool):
            raise RuleError("驱动必须明确声明参数类型的预览支持")
        schema = deepcopy(found.schema)
        if not isinstance(schema, dict):
            raise RuleError("参数 Schema 必须是对象")
        validate_parameter_schema(parameter_type, schema)
        schema = schema_with_defaults(schema, found.defaults)
        return ParameterDefinition(schema=schema, defaults=deepcopy(found.defaults),
                                   preview_supported=found.preview_supported, task_factory=found.task_factory)

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
                    "type": action_type,
                    "parameter_types": [
                        {
                            "type": parameter.parameter_type,
                            "name": parameter.name,
                            "description": parameter.description,
                            "preview_supported": parameter.preview_supported,
                            "schema": dict(self.parameter_definition(device_id, action_type, parameter.parameter_type).schema),
                        } for parameter in capabilities
                    ],
                }
                for action_type, capabilities in sorted(definition.actions.items())
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
    """当前进程可见的已部署驱动定义。

    第一版没有内置厂商映射；部署适配（或集成测试的受约束替身）
    在进程启动阶段经定义登记点接入，登记结果与将来内置的厂商映
    射合并。未登记定义的驱动不出现，声明了未登记驱动定义的设备
    在目录构建时报部署错误。
    """
    from camctl.devices.definitions_runtime import current_driver_definitions

    return current_driver_definitions()

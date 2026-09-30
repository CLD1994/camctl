"""受理使用的窄端口与静态目录接口。

静态驱动目录（D1）提供动作类型、设备与参数定义；仓储端口由
A4 的原子输入用例消费。接口受真实契约约束，不复制驱动规则。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from camctl.contracts.json_values import JsonValue

__all__ = ["ParameterDefinition", "StaticActionCatalog"]


@dataclass(frozen=True)
class ParameterDefinition:
    """一种拍摄动作的参数规则：公共 Schema 片段与同源默认值。

    schema 为 Draft 2020-12 的对象 Schema（经精确数字适配校验）；
    defaults 保存字段的权威默认值，应用时原输入保持不变。
    """

    schema: JsonValue
    defaults: Mapping[str, JsonValue]


class StaticActionCatalog(Protocol):
    """静态驱动目录端口：不连接设备，回答支持范围与参数规则。"""

    def action_types(self) -> frozenset[str]: ...

    def device_exists(self, device_id: str) -> bool: ...

    def parameter_definition(
        self, device_id: str, action_type: str
    ) -> ParameterDefinition | None: ...

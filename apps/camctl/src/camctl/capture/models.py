"""拍摄专属定义（第一版：拍照、录像、延时摄影）。

定义由首次受理事实构建并只读：动作类型、精确目标时长（毫秒）
与完成声明。公共管理模型设备无关；专属参数仅本模块解释。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Mapping

from camctl.contracts.values import seconds_to_duration_ms

__all__ = [
    "CaptureCompletion",
    "CaptureDefinition",
    "CaptureInput",
]

_CAMERA_TYPES = frozenset(
    {"camera_take_photo", "camera_record", "camera_timelapse"}
)


class CaptureCompletion(enum.Enum):
    """动作完成依据的声明。"""

    DEVICE_EVIDENCE = "device_evidence"
    TIME_AND_OUTPUTS = "time_and_outputs"


@dataclass(frozen=True)
class CaptureInput:
    """已校验的动作输入：类型与生效参数（首次受理事实）。"""

    action_type: str
    effective_params: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.action_type not in _CAMERA_TYPES:
            raise ValueError(f"非拍摄动作不构造拍摄定义: {self.action_type!r}")
        object.__setattr__(self, "effective_params", MappingProxyType(dict(self.effective_params)))


@dataclass(frozen=True)
class CaptureDefinition:
    """一次拍摄动作的只读执行定义。

    target_duration_ms 仅录像/延时需要（正整数毫秒，精确换算）；
    定义由首次受理构建，驱动默认值变化不影响旧动作。
    """

    action_type: str
    target_duration_ms: int | None
    completion: CaptureCompletion

    @classmethod
    def build(cls, source: CaptureInput) -> "CaptureDefinition":
        """从生效参数构造定义；参数不完整或不精确时拒绝。"""
        if source.action_type == "camera_take_photo":
            return cls(
                action_type=source.action_type,
                target_duration_ms=None,
                completion=CaptureCompletion.DEVICE_EVIDENCE,
            )
        duration_raw = source.effective_params.get("target_duration_s")
        if duration_raw is None:
            raise ValueError(f"{source.action_type} 需要 target_duration_s")
        duration_ms = seconds_to_duration_ms(duration_raw)
        if source.action_type == "camera_record":
            if not source.effective_params.get("stop_supported", False):
                raise ValueError("camera_record 需要停止能力声明 stop_supported")
            completion = CaptureCompletion.DEVICE_EVIDENCE
        else:
            completion = CaptureCompletion.TIME_AND_OUTPUTS
        return cls(
            action_type=source.action_type,
            target_duration_ms=duration_ms,
            completion=completion,
        )

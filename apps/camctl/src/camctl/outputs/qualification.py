"""读取和删除资格的共同事务类型。

资格授予在一个事务内核对全部可靠限制与业务顺序：候选按计划时
间排序、同时间取回优先；授予时依赖、交付、拷贝、目标文件及读取
流程共同建档，缺一即整笔拒绝。清理限制与删除处理者的拒绝是逐项
最终失败；排序阻挡与设备占用是可等待的暂时拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from camctl.contracts.values import MAX_OBJECT_ID, ObjectId, UtcMicros
from camctl.host_files.paths import validate_file_extension

__all__ = [
    "FileCandidate",
    "FileQualification",
    "OperationConfig",
    "QualificationOutcome",
]


class QualificationOutcome(Enum):
    """一次资格申请的结果分类。"""

    GRANTED = "granted"
    #: 同设备存在排序更早的合格候选，或设备读取机会被占用：等待，
    #: 不落库为失败。
    REJECTED = "rejected"
    #: 清理限制、删除处理者或产物不可用：逐项保存最终失败。
    REJECTED_FINAL = "rejected_final"


@dataclass(frozen=True)
class OperationConfig:
    """读取流程建档采用的本次配置。"""

    max_attempts: int
    timeout_s: Decimal
    retry_interval_s: Decimal

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or not 1 <= self.max_attempts <= MAX_OBJECT_ID
        ):
            raise ValueError(f"尝试上限必须是可保存的正整数: {self.max_attempts!r}")
        for name in ("timeout_s", "retry_interval_s"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, Decimal)
                or not value.is_finite()
                or value < 0
                or (name == "timeout_s" and value == 0)
            ):
                domain = "正" if name == "timeout_s" else "非负"
                raise ValueError(f"{name} 必须是有限{domain} Decimal 秒数: {value!r}")


@dataclass(frozen=True)
class FileQualification:
    """一次资格申请的实际结果。

    GRANTED 时 copy_id、run_id、target_file_id 与（取回分支的）
    delivery_id 指向首次共同建档或已核实的原记录。该结果不替代
    实际读取的流程、尝试及派发资格检查；拒绝时身份均为 None 并携带原因。
    """

    outcome: QualificationOutcome
    copy_id: int | None
    run_id: int | None
    delivery_id: int | None
    target_file_id: int | None
    reason: str | None


@dataclass(frozen=True)
class FileCandidate:
    """一次文件资格申请：目标产物、源文件与建档所需身份。

    取回路径携带 item_id（已固定取回项，首次建档须为 SELECTED）；内部检查/修复路径携
    带 processing_id（录像处理责任），共用设备单文件读取机会且不
    创建交付。主机产物使用 source_intermediate_file_id，config 为
    None 表示不适用设备配置；本地读取的次数上限固定为 1。
    调用方只提供扩展名，事务分配身份后生成目标路径及交付文件名；
    delivery_display_name 仅用于取回交付的可读名称。
    """

    action_id: int
    item_id: int | None
    processing_id: int | None
    output_id: int | None
    source_device_file_id: int | None
    target_extension: str | None
    delivery_extension: str | None
    delivery_display_name: str | None
    config: OperationConfig | None
    occurred_at: int
    source_intermediate_file_id: int | None = None

    def __post_init__(self) -> None:
        ObjectId(self.action_id)
        for value in (self.item_id, self.processing_id, self.output_id,
                      self.source_device_file_id, self.source_intermediate_file_id):
            if value is not None:
                ObjectId(value)
        if (self.item_id is None) == (self.processing_id is None):
            raise ValueError(
                "候选必须恰属于取回项或录像处理之一:"
                f" item={self.item_id!r} processing={self.processing_id!r}"
            )
        if (self.item_id is None) != (self.output_id is None):
            raise ValueError("取回候选必须填写正式产物，内部处理候选不填写正式产物")
        if (self.source_device_file_id is None) == (self.source_intermediate_file_id is None):
            raise ValueError("读取源必须恰为设备文件或主机文件之一")
        if self.source_device_file_id is not None:
            if not isinstance(self.config, OperationConfig):
                raise ValueError("设备读取候选必须使用已校验的读取配置")
        elif self.config is not None or self.processing_id is not None:
            raise ValueError("主机产物取回不采用设备配置，内部原片必须来自设备")
        validate_file_extension(self.target_extension)
        if self.item_id is not None:
            validate_file_extension(self.delivery_extension, required=True)
            if not isinstance(self.delivery_display_name, str) or not self.delivery_display_name:
                raise ValueError("交付可读名称必须是非空字符串")
        elif self.delivery_extension is not None or self.delivery_display_name is not None:
            raise ValueError("内部处理不填写交付扩展名或可读名称")
        timestamp = UtcMicros(self.occurred_at)
        if not -MAX_OBJECT_ID - 1 <= timestamp <= MAX_OBJECT_ID:
            raise ValueError(f"事件时间超出 SQLite 整数微秒范围: {self.occurred_at!r}")

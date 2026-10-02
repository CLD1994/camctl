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

    授予时 copy_id、run_id、target_file_id 与（取回分支的）
    delivery_id 指向同事务建档的行；拒绝时均为 None 并携带原因。
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

    取回路径携带 item_id（SELECTED 取回项）；内部检查/修复路径携
    带 processing_id（录像处理责任），共用设备单文件读取机会且不
    创建交付。
    """

    action_id: int
    item_id: int | None
    processing_id: int | None
    output_id: int | None
    source_device_file_id: int
    target_relative_path: str
    delivery_file_name: str
    delivery_display_name: str
    config: OperationConfig
    occurred_at: int

    def __post_init__(self) -> None:
        for value in (self.action_id, self.source_device_file_id):
            ObjectId(value)
        for value in (self.item_id, self.processing_id, self.output_id):
            if value is not None:
                ObjectId(value)
        if (self.item_id is None) == (self.processing_id is None):
            raise ValueError(
                "候选必须恰属于取回项或录像处理之一:"
                f" item={self.item_id!r} processing={self.processing_id!r}"
            )
        if (self.item_id is None) != (self.output_id is None):
            raise ValueError("取回候选必须填写正式产物，内部处理候选不填写正式产物")
        if not isinstance(self.config, OperationConfig):
            raise ValueError("候选必须使用已校验的读取配置")
        if not self.target_relative_path or not self.delivery_file_name:
            raise ValueError("目标相对路径与交付文件名不能为空")
        timestamp = UtcMicros(self.occurred_at)
        if not -MAX_OBJECT_ID - 1 <= timestamp <= MAX_OBJECT_ID:
            raise ValueError(f"事件时间超出 SQLite 整数微秒范围: {self.occurred_at!r}")

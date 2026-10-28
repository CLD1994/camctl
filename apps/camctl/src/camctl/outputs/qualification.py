"""读取和删除资格的共同事务类型。

资格授予在一个事务内核对同产物的可靠限制与业务顺序：候选按计划
时间排序、同时间取回优先；依赖、交付、拷贝、目标文件及读取流程
共同建档，缺一即整笔拒绝。清理限制与产物不可用保存逐项最终失败；
排序阻挡只读等待。相机读取机会由独立事务取得。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Mapping

from camctl.contracts.values import MAX_OBJECT_ID, ObjectId, UtcMicros
from camctl.devices.bindings import BindingResult, BindingStatus
from camctl.host_files.paths import validate_file_extension
from camctl.logging_runtime.diagnostics import DiagnosticState

__all__ = [
    "FileCandidate",
    "FileQualification",
    "OperationConfig",
    "QualificationOutcome",
]


class QualificationOutcome(Enum):
    """一次资格申请的结果分类。"""

    GRANTED = "granted"
    #: 时间、来源或同产物候选条件尚未满足：等待，不落库为失败。
    REJECTED = "rejected"
    #: 清理限制、删除处理者或产物不可用：逐项保存最终失败。
    REJECTED_FINAL = "rejected_final"


@dataclass(frozen=True)
class OperationConfig:
    """读取流程建档采用的本次配置。"""

    max_attempts: int
    timeout_s: Decimal
    retry_interval_s: Decimal

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> "OperationConfig":
        """恢复持久化的读取预算，将 JSON 整数秒规范成精确 Decimal。

        JSON 保存 Decimal 数值时不保留 Python 类型；比如 `10` 和 `0`
        重开后会分别成为 `int`。次数仍必须是整数，秒数字段只接受
        Decimal 或整数，之后统一交给构造器执行原有合法性校验。
        """
        fields = {"max_attempts", "timeout_s", "retry_interval_s"}
        if not isinstance(document, Mapping) or set(document) != fields:
            raise ValueError("持久化读取预算字段不完整或包含未知字段")

        def decode_seconds(name: str) -> Decimal:
            value = document[name]
            if isinstance(value, Decimal):
                return value
            if type(value) is int:
                return Decimal(value)
            raise ValueError(f"持久化读取预算 {name} 必须是 JSON 数值: {value!r}")

        return cls(
            document["max_attempts"],
            decode_seconds("timeout_s"),
            decode_seconds("retry_interval_s"),
        )

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
    error_code: int | None = None
    error_details: Mapping | None = None
    diagnostic: DiagnosticState = field(default_factory=DiagnosticState, compare=False, repr=False)


@dataclass(frozen=True)
class FileCandidate:
    """一次文件资格申请：目标产物、源文件与建档所需身份。

    取回路径携带 item_id（已固定取回项，首次建档须为 SELECTED）；单原片处理携带
    processing_id，分段检查携带 inspection_id，两类内部路径都共用设备单文件读取机会且不
    创建交付。一般主机产物使用 source_intermediate_file_id 且不采用设备配置；正式取回
    可复用的检查副本时，允许把原设备读取配置冻结到同一 READ_FILE 流程，供失败回退沿用。
    调用方只提供扩展名，事务分配身份后生成目标路径及交付文件名；
    delivery_display_name 仅用于取回交付的可读名称。
    occurred_at 是正常执行入口实际取得的 UTC 微秒读数，用于核对
    首次建档是否已到发起动作的计划时间；调用方仍负责会话墙钟检查。
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
    #: 设备源按原观察者身份核对本次配置；异常时不建立读取配置或流程。
    binding_result: BindingResult | None = None
    #: 独立分段检查责任；与取回项、单原片处理责任互斥。
    inspection_id: int | None = None
    #: 取回项已绑定检查副本的复用凭据；与 inspection_id 的责任归属不同。
    reuse_inspection_id: int | None = None
    #: 照片来源缓存的原 RESULTS 角色页；不是视频检查身份。
    reuse_photo_page_event_id: int | None = None

    def __post_init__(self) -> None:
        ObjectId(self.action_id)
        for value in (self.item_id, self.processing_id, self.inspection_id,
                      self.reuse_inspection_id, self.reuse_photo_page_event_id, self.output_id,
                      self.source_device_file_id, self.source_intermediate_file_id):
            if value is not None:
                ObjectId(value)
        if sum(value is not None for value in
               (self.item_id, self.processing_id, self.inspection_id)) != 1:
            raise ValueError(
                "候选必须恰属于取回项、录像处理或分段检查之一:"
                f" item={self.item_id!r} processing={self.processing_id!r}"
                f" inspection={self.inspection_id!r}"
            )
        if (self.item_id is None) != (self.output_id is None):
            raise ValueError("取回候选必须填写正式产物，内部处理候选不填写正式产物")
        if (self.source_device_file_id is None) == (self.source_intermediate_file_id is None):
            raise ValueError("读取源必须恰为设备文件或主机文件之一")
        if self.source_device_file_id is not None:
            if self.binding_result is not None and not isinstance(self.binding_result, BindingResult):
                raise TypeError("设备绑定核对必须使用 BindingResult")
            failed_binding = self.binding_result is not None and self.binding_result.status in (
                BindingStatus.DEVICE_MISSING, BindingStatus.DRIVER_MISMATCH)
            if not failed_binding and not isinstance(self.config, OperationConfig):
                raise ValueError("设备读取候选必须使用已校验的读取配置")
            if self.binding_result is not None and self.binding_result.status is BindingStatus.UNAVAILABLE:
                raise ValueError("无法可靠取得的配置不能作为普通设备绑定异常")
        elif (self.processing_id is not None or self.inspection_id is not None
              or self.binding_result is not None):
            raise ValueError("内部原片必须来自设备，不能携带取回绑定失败")
        if self.reuse_inspection_id is not None and (
                self.item_id is None or self.output_id is None
                or self.source_device_file_id is not None
                or self.source_intermediate_file_id is None
                or self.processing_id is not None or self.inspection_id is not None):
            raise ValueError("检查副本复用只适用于主机源的正式取回项")
        if self.reuse_photo_page_event_id is not None and (
                self.reuse_inspection_id is not None or self.item_id is None or self.output_id is None
                or self.source_device_file_id is not None or self.source_intermediate_file_id is None
                or self.processing_id is not None or self.inspection_id is not None):
            raise ValueError("照片缓存复用只适用于原角色的正式取回项")
        if (self.source_intermediate_file_id is not None and self.config is not None
                and (self.reuse_inspection_id is None and self.reuse_photo_page_event_id is None
                     or not isinstance(self.config, OperationConfig))):
            raise ValueError("设备读取配置只可冻结在正式取回的检查副本复用流程")
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

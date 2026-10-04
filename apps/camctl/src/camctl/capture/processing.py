"""录像内部处理的状态推进命令与检查结果分类。

RECORDING_DECIDED 固定检查决定、修复决定及可靠原片关联；
RECORDING_PROCESSED 保存检查、修复与取消收场的实际阶段。媒体观
察采用公共 media 结构：时长与判定依据分开表达，未知不以零代替，
检查完成与时长不足分别表达。命令只固定事实和阶段，不携带采集
成功结论；修复成品资格由文件与完整性事实表达。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

from camctl.contracts.enums import enum_for
from camctl.contracts.values import ObjectId

__all__ = [
    "CheckBasis",
    "CheckDecisionChoice",
    "CheckDecisionSave",
    "CheckDurationClass",
    "CheckPhase",
    "CheckReason",
    "CheckResultSave",
    "DiscardPhase",
    "DiscardProgressSave",
    "MediaObservation",
    "ProcessingDisposition",
    "ProcessingError",
    "ProcessingOutcome",
    "RepairBasis",
    "RepairDecisionChoice",
    "RepairDecisionSave",
    "RepairOutcome",
    "RepairReason",
    "RepairResultSave",
    "SourceFileSave",
    "classify_check_duration",
    "repair_basis_from_check",
]

_CHECK_DECISION = enum_for("recording_processing.check_decision")
_CHECK_STATE = enum_for("recording_processing.check_state")
_REPAIR_STATE = enum_for("recording_processing.repair_state")
_DISCARD_STATE = enum_for("recording_processing.discard_state")

_STATUS_NAMES = {
    2: "running",
    3: "completed",
    4: "failed",
    5: "unconfirmed",
}


class CheckDecisionChoice(Enum):
    """原片检查决定；初始 UNDETERMINED 不可显式保存。"""

    NOT_NEEDED = int(_CHECK_DECISION.NOT_NEEDED)
    REQUIRED = int(_CHECK_DECISION.REQUIRED)


class CheckReason(Enum):
    """检查依据的来源分类。"""

    CONTINUOUS_CONTROL_COMPLETE = 1
    INSUFFICIENT_TIMING = 2
    EXCESS_DURATION_CHECK = 3


class RepairDecisionChoice(Enum):
    """修复决定；PENDING 表示已确认多录、等待执行。"""

    NOT_NEEDED = int(_REPAIR_STATE.NOT_NEEDED)
    PENDING = int(_REPAIR_STATE.PENDING)
    CANCELED = int(_REPAIR_STATE.CANCELED)


class RepairReason(Enum):
    """修复依据的来源分类。"""

    BELOW_THRESHOLD = 1
    THRESHOLD_REACHED = 2
    NO_USABLE_INPUT = 3
    CANCELED = 4


class CheckPhase(Enum):
    """检查执行阶段；FAILED 与 UNCONFIRMED 为终态。"""

    RUNNING = int(_CHECK_STATE.RUNNING)
    COMPLETED = int(_CHECK_STATE.COMPLETED)
    FAILED = int(_CHECK_STATE.FAILED)
    UNCONFIRMED = int(_CHECK_STATE.UNCONFIRMED)


class RepairOutcome(Enum):
    """修复执行阶段；成功只能自 RUNNING 进入。"""

    RUNNING = int(_REPAIR_STATE.RUNNING)
    SUCCEEDED = int(_REPAIR_STATE.SUCCEEDED)
    FAILED = int(_REPAIR_STATE.FAILED)
    CANCELED = int(_REPAIR_STATE.CANCELED)


class DiscardPhase(Enum):
    """取消后文件处理的收场进度。"""

    PENDING = int(_DISCARD_STATE.PENDING)
    RUNNING = int(_DISCARD_STATE.RUNNING)
    COMPLETED = int(_DISCARD_STATE.COMPLETED)
    FAILED = int(_DISCARD_STATE.FAILED)
    UNKNOWN = int(_DISCARD_STATE.UNKNOWN)


class CheckDurationClass(Enum):
    """可靠视频时长与修复门槛比较的三分区。"""

    SHORT = "short"
    WITHIN = "within"
    OVER = "over"


class ProcessingDisposition(Enum):
    """处理事务的保存结果：新保存或原键恢复首次响应。"""

    SAVED = "saved"
    ALREADY = "already"


def _positive_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} 必须是整数: {value!r}")
    if value <= 0:
        raise ValueError(f"{name} 必须是正整数: {value!r}")
    return value


def _optional_non_negative_int(name: str, value: Any) -> int:
    if value is None:
        return value
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} 必须是整数: {value!r}")
    if value < 0:
        raise ValueError(f"{name} 必须是非负整数: {value!r}")
    return value


def _seconds(name: str, value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal):
        if isinstance(value, int):
            value = Decimal(value)
        else:
            raise TypeError(f"{name} 必须是精确十进制数: {value!r}")
    if not value.is_finite() or value < 0:
        raise ValueError(f"{name} 必须是非负有限秒数: {value!r}")
    return value


def _optional_seconds(name: str, value: Any) -> Decimal | None:
    return None if value is None else _seconds(name, value)


@dataclass(frozen=True)
class ProcessingError:
    """协议 error 结构的处理诊断；三键完整，details 保持映射。"""

    code: str
    stage: str
    details: Mapping[str, Any]

    def __post_init__(self) -> None:
        for name in ("code", "stage"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} 必须是非空文本: {value!r}")
        if not isinstance(self.details, Mapping):
            raise TypeError(f"details 必须是对象: {self.details!r}")

    def as_json(self) -> dict[str, Any]:
        return {"code": self.code, "stage": self.stage,
                "details": dict(self.details)}


@dataclass(frozen=True)
class CheckBasis:
    """检查决定的依据；未知成员省略，不默认为零。"""

    reason: CheckReason
    target_duration_ms: int
    control_elapsed_ns: int | None = None
    continuity_evidence: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.reason, CheckReason):
            raise TypeError(f"检查依据必须是 CheckReason: {self.reason!r}")
        _positive_int("目标时长毫秒", self.target_duration_ms)
        _optional_non_negative_int("控制耗时纳秒", self.control_elapsed_ns)
        evidence = self.continuity_evidence
        if evidence is not None:
            if not isinstance(evidence, tuple):
                raise TypeError(f"连续性证据必须是元组: {evidence!r}")
            if not evidence:
                raise ValueError("连续性证据不能为空")
            for item in evidence:
                if not isinstance(item, str) or not item:
                    raise ValueError(f"连续性证据成员必须是非空文本: {item!r}")

    def as_json(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "reason": self.reason.value,
            "target_duration_ms": self.target_duration_ms,
        }
        if self.control_elapsed_ns is not None:
            document["control_elapsed_ns"] = self.control_elapsed_ns
        if self.continuity_evidence is not None:
            document["continuity_evidence"] = list(self.continuity_evidence)
        return document


@dataclass(frozen=True)
class RepairBasis:
    """修复决定的依据；门槛秒数随比较理由必填或省略。"""

    reason: RepairReason
    target_duration_ms: int
    threshold_s: Decimal | None = None
    actual_duration_s: Decimal | None = None
    control_elapsed_ns: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.reason, RepairReason):
            raise TypeError(f"修复依据必须是 RepairReason: {self.reason!r}")
        _positive_int("目标时长毫秒", self.target_duration_ms)
        needs_threshold = self.reason in (
            RepairReason.BELOW_THRESHOLD, RepairReason.THRESHOLD_REACHED)
        if needs_threshold and self.threshold_s is None:
            raise ValueError("门槛比较理由必须提供实际比较门槛秒数")
        if not needs_threshold and self.threshold_s is not None:
            raise ValueError("该理由不适用门槛秒数，应省略")
        if self.threshold_s is not None:
            _seconds("门槛秒数", self.threshold_s)
        _optional_seconds("实测时长秒", self.actual_duration_s)
        _optional_non_negative_int("控制耗时纳秒", self.control_elapsed_ns)

    def as_json(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "reason": self.reason.value,
            "target_duration_ms": self.target_duration_ms,
        }
        if self.threshold_s is not None:
            document["threshold_s"] = self.threshold_s
        if self.actual_duration_s is not None:
            document["actual_duration_s"] = self.actual_duration_s
        if self.control_elapsed_ns is not None:
            document["control_elapsed_ns"] = self.control_elapsed_ns
        return document


@dataclass(frozen=True)
class MediaObservation:
    """公共 media 结构的检查观察；阶段与结论互相对应。

    completed 表示检查流程完成并取得可靠视频时长，error 必须省略；
    failed 与 unconfirmed 保留实际错误，时长可为已知观察或未知；
    running 不携带结论。issues 表达已发现的明确媒体错误。
    """

    phase: CheckPhase
    duration_s: Decimal | None = None
    issues: tuple[ProcessingError, ...] = ()
    error: ProcessingError | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.phase, CheckPhase):
            raise TypeError(f"检查阶段必须是 CheckPhase: {self.phase!r}")
        _optional_seconds("视频时长秒", self.duration_s)
        if not isinstance(self.issues, tuple):
            raise TypeError(f"媒体问题必须是元组: {self.issues!r}")
        for issue in self.issues:
            if not isinstance(issue, ProcessingError):
                raise TypeError(f"媒体问题必须是 ProcessingError: {issue!r}")
        if self.phase is CheckPhase.COMPLETED:
            if self.duration_s is None:
                raise ValueError("检查完成必须保存可靠视频时长")
            if self.error is not None:
                raise ValueError("检查完成不能同时携带错误")
        elif self.phase in (CheckPhase.FAILED, CheckPhase.UNCONFIRMED):
            if self.error is None:
                raise ValueError("检查失败或未确认必须保存实际错误")
        else:
            if self.duration_s is not None:
                raise ValueError("检查进行中不能携带时长结论")
            if self.error is not None:
                raise ValueError("检查进行中不能携带错误结论")

    def as_json(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "check_status": _STATUS_NAMES[self.phase.value],
            "duration": (
                {"status": "unknown"} if self.duration_s is None
                else {"status": "available", "seconds": self.duration_s}
            ),
        }
        if self.issues:
            document["issues"] = [issue.as_json() for issue in self.issues]
        if self.error is not None:
            document["error"] = self.error.as_json()
        return document


@dataclass(frozen=True)
class ProcessingOutcome:
    """处理事务的保存结果。"""

    disposition: ProcessingDisposition


#: 检查决定与依据来源的固定配对：只有计时不足才需要媒体检查。
_CHECK_REASON_FOR_CHOICE = {
    CheckDecisionChoice.REQUIRED: frozenset({CheckReason.INSUFFICIENT_TIMING}),
    CheckDecisionChoice.NOT_NEEDED: frozenset({
        CheckReason.CONTINUOUS_CONTROL_COMPLETE,
        CheckReason.EXCESS_DURATION_CHECK,
    }),
}

#: 修复决定与依据来源的固定配对。
_REPAIR_REASON_FOR_CHOICE = {
    RepairDecisionChoice.PENDING: frozenset({RepairReason.THRESHOLD_REACHED}),
    RepairDecisionChoice.NOT_NEEDED: frozenset({
        RepairReason.BELOW_THRESHOLD, RepairReason.NO_USABLE_INPUT}),
    RepairDecisionChoice.CANCELED: frozenset({RepairReason.CANCELED}),
}


@dataclass(frozen=True)
class CheckDecisionSave:
    """固定原片检查决定的事务输入。"""

    processing_id: int
    decision: CheckDecisionChoice
    basis: CheckBasis
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.processing_id)
        if not isinstance(self.decision, CheckDecisionChoice):
            raise TypeError(f"检查决定必须是 CheckDecisionChoice: {self.decision!r}")
        if not isinstance(self.basis, CheckBasis):
            raise TypeError(f"检查依据必须是 CheckBasis: {self.basis!r}")
        if self.basis.reason not in _CHECK_REASON_FOR_CHOICE[self.decision]:
            raise ValueError(
                f"检查决定与依据来源不符: {self.decision!r}"
                f" / {self.basis.reason!r}")


@dataclass(frozen=True)
class RepairDecisionSave:
    """固定修复决定的事务输入。"""

    processing_id: int
    decision: RepairDecisionChoice
    basis: RepairBasis
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.processing_id)
        if not isinstance(self.decision, RepairDecisionChoice):
            raise TypeError(f"修复决定必须是 RepairDecisionChoice: {self.decision!r}")
        if not isinstance(self.basis, RepairBasis):
            raise TypeError(f"修复依据必须是 RepairBasis: {self.basis!r}")
        if self.basis.reason not in _REPAIR_REASON_FOR_CHOICE[self.decision]:
            raise ValueError(
                f"修复决定与依据来源不符: {self.decision!r}"
                f" / {self.basis.reason!r}")


@dataclass(frozen=True)
class SourceFileSave:
    """首次关联可靠原片的事务输入。"""

    processing_id: int
    source_device_file_id: int
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.processing_id)
        ObjectId(self.source_device_file_id)


@dataclass(frozen=True)
class CheckResultSave:
    """保存检查执行阶段及媒体观察的事务输入。"""

    processing_id: int
    media: MediaObservation
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.processing_id)
        if not isinstance(self.media, MediaObservation):
            raise TypeError(f"媒体观察必须是 MediaObservation: {self.media!r}")

    @property
    def phase(self) -> CheckPhase:
        return self.media.phase


@dataclass(frozen=True)
class RepairResultSave:
    """保存修复执行阶段及结果的事务输入。

    SUCCEEDED 必须指向已完整登记的修复输出；FAILED 保存实际错
    误；RUNNING 与 CANCELED 不携带文件或错误事实。
    """

    processing_id: int
    phase: RepairOutcome
    occurred_at: int
    output_file_id: int | None = None
    error: ProcessingError | None = None

    def __post_init__(self) -> None:
        ObjectId(self.processing_id)
        if not isinstance(self.phase, RepairOutcome):
            raise TypeError(f"修复阶段必须是 RepairOutcome: {self.phase!r}")
        if self.output_file_id is not None:
            ObjectId(self.output_file_id)
        if self.error is not None and not isinstance(self.error, ProcessingError):
            raise TypeError(f"修复错误必须是 ProcessingError: {self.error!r}")
        if self.phase is RepairOutcome.SUCCEEDED:
            if self.output_file_id is None:
                raise ValueError("修复成功必须指向修复输出文件")
            if self.error is not None:
                raise ValueError("修复成功不能同时携带错误")
        elif self.phase is RepairOutcome.FAILED:
            if self.error is None:
                raise ValueError("修复失败必须保存实际错误")
            if self.output_file_id is not None:
                raise ValueError("修复失败不携带成品文件身份")
        else:
            if self.output_file_id is not None:
                raise ValueError("该修复阶段不携带成品文件身份")
            if self.error is not None:
                raise ValueError("该修复阶段不携带错误")


@dataclass(frozen=True)
class DiscardProgressSave:
    """保存取消后文件处理收场进度的事务输入。"""

    processing_id: int
    phase: DiscardPhase
    occurred_at: int
    error: ProcessingError | None = None

    def __post_init__(self) -> None:
        ObjectId(self.processing_id)
        if not isinstance(self.phase, DiscardPhase):
            raise TypeError(f"收场阶段必须是 DiscardPhase: {self.phase!r}")
        if self.error is not None and not isinstance(self.error, ProcessingError):
            raise TypeError(f"收场错误必须是 ProcessingError: {self.error!r}")
        failed = self.phase in (DiscardPhase.FAILED, DiscardPhase.UNKNOWN)
        if failed and self.error is None:
            raise ValueError("收场失败或未知必须保存实际错误")
        if not failed and self.error is not None:
            raise ValueError("该收场阶段不携带错误")


def classify_check_duration(
    duration_s: Decimal, repair_margin_s: Decimal, observed_s: Decimal,
) -> CheckDurationClass:
    """按目标时长与修复门槛三分可靠视频时长。

    区间端点属于 WITHIN：恰好达到目标即满足，恰好达到门槛不
    触发修复；严格超过门槛才确认多录。
    """
    threshold = _seconds("目标时长", duration_s) + _seconds("修复余量", repair_margin_s)
    observed = _seconds("实测时长", observed_s)
    if observed < duration_s:
        return CheckDurationClass.SHORT
    if observed > threshold:
        return CheckDurationClass.OVER
    return CheckDurationClass.WITHIN


def repair_basis_from_check(
    duration_s: Decimal, repair_margin_s: Decimal, observed_s: Decimal,
    target_duration_ms: int,
) -> RepairBasis:
    """按检查时长构造修复决定依据；分类规则与检查表一致。"""
    classification = classify_check_duration(
        duration_s, repair_margin_s, observed_s)
    reason = (
        RepairReason.THRESHOLD_REACHED
        if classification is CheckDurationClass.OVER
        else RepairReason.BELOW_THRESHOLD
    )
    return RepairBasis(
        reason=reason,
        target_duration_ms=target_duration_ms,
        threshold_s=duration_s + repair_margin_s,
        actual_duration_s=observed_s,
    )

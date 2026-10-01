"""拍摄文件归属、集合核实与占用释放判定。

文件归属、单文件写完与集合齐备分别判断，任何一项不由另一项推
导；读取错误不解释为缺失或空集合；集合已确定仍缺必需类别是明
确不满足，不再当作暂时未知反复核实。活动结束与占用释放是不同
事实，ENDED+HELD 不表示仍拍摄，文件归属未定时同范围新拍摄不
得放行。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "ActivityFacts",
    "ActivityState",
    "CaptureAssessment",
    "CaptureFile",
    "CaptureFileSet",
    "FileKind",
    "OccupancyState",
    "ProductRequirements",
    "ReleaseDecision",
    "assess_capture_files",
    "decide_release",
]


class FileKind(Enum):
    """产物文件的类别；必需集合由任务参数声明。"""

    PHOTO = "photo"
    VIDEO = "video"
    OTHER = "other"


@dataclass(frozen=True)
class CaptureFile:
    """一份已观察的候选产物文件。"""

    file_id: str
    kind: FileKind
    complete: bool
    ownership_confirmed: bool
    read_error: bool = False


@dataclass(frozen=True)
class CaptureFileSet:
    """一轮核实取得的文件集合。

    set_finalized 表示归属范围已经确定（采集结束且基准核对完成）；
    尚未确定时缺类别按暂未齐处理。
    """

    files: tuple[CaptureFile, ...]
    set_finalized: bool


@dataclass(frozen=True)
class ProductRequirements:
    """任务声明的产物要求：必需类别集合；空集合表示无必需产物。"""

    required_kinds: frozenset[FileKind]


@dataclass(frozen=True)
class CaptureAssessment:
    """集合核实的判定：各类事实分别表达。"""

    is_complete: bool
    explicitly_unmet: bool
    missing_kinds: tuple[FileKind, ...]
    incomplete_files: tuple[str, ...]
    ownership_pending: tuple[str, ...]
    read_errors: tuple[str, ...]


def assess_capture_files(
    files: CaptureFileSet, requirements: ProductRequirements
) -> CaptureAssessment:
    """按归属、写完与集合要求分别判定产物状态。"""
    read_errors = tuple(f.file_id for f in files.files if f.read_error)
    ownership_pending = tuple(
        f.file_id for f in files.files if not f.ownership_confirmed
    )
    incomplete = tuple(f.file_id for f in files.files if not f.complete)
    confirmed_kinds = {f.kind for f in files.files if f.ownership_confirmed}
    missing = tuple(
        kind
        for kind in sorted(requirements.required_kinds, key=lambda k: k.value)
        if kind not in confirmed_kinds
    )
    complete = (
        not read_errors
        and not ownership_pending
        and not incomplete
        and not missing
    )
    explicitly_unmet = bool(missing) and files.set_finalized
    return CaptureAssessment(
        is_complete=complete,
        explicitly_unmet=explicitly_unmet,
        missing_kinds=missing,
        incomplete_files=incomplete,
        ownership_pending=ownership_pending,
        read_errors=read_errors,
    )


class ActivityState(Enum):
    """设备活动状态。"""

    UNKNOWN = "unknown"
    ACTIVE = "active"
    ENDED = "ended"


class OccupancyState(Enum):
    """占用状态。"""

    HELD = "held"
    RELEASED = "released"


class ReleaseDecision(Enum):
    """占用释放的判定分区。"""

    RELEASE = "release"
    KEEP_HELD_ACTIVE = "keep_held_active"
    KEEP_HELD_CALLS = "keep_held_calls"
    KEEP_HELD_OWNERSHIP = "keep_held_ownership"
    KEEP_HELD_UNKNOWN = "keep_held_unknown"


@dataclass(frozen=True)
class ActivityFacts:
    """占用判定的已保存事实。"""

    activity_state: ActivityState
    occupancy_state: OccupancyState
    completion_evidence: bool
    unresolved_calls: bool
    file_ownership_resolved: bool


def decide_release(state: ActivityFacts) -> ReleaseDecision:
    """按活动、调用、归属与依据分别核对后决定占用释放。

    释放要求结束或完成依据可靠、无未收场调用且文件归属已定；
    归属未定独立阻挡放行（同范围新拍摄不得开始）；ENDED+HELD
    但依据不足保留为未知，不判仍在拍摄。
    """
    if state.unresolved_calls:
        return ReleaseDecision.KEEP_HELD_CALLS
    if not state.file_ownership_resolved:
        return ReleaseDecision.KEEP_HELD_OWNERSHIP
    if state.activity_state is ActivityState.ACTIVE:
        return ReleaseDecision.KEEP_HELD_ACTIVE
    if not state.completion_evidence:
        return ReleaseDecision.KEEP_HELD_UNKNOWN
    return ReleaseDecision.RELEASE

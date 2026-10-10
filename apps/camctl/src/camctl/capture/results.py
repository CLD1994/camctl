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
from collections.abc import Iterable

__all__ = [
    "ActivityFacts",
    "ActivityState",
    "CaptureAssessment",
    "CaptureFile",
    "CaptureFileSet",
    "FileKind",
    "OccupancyState",
    "ProductRequirements",
    "ProductRule",
    "RuleState",
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
    format_id: str | None = None
    pairing_confirmed: bool | None = None


@dataclass(frozen=True)
class CaptureFileSet:
    """一轮核实取得的文件集合。

    set_finalized 表示归属范围已经确定（采集结束且基准核对完成）；
    尚未确定时缺类别按暂未齐处理。
    """

    files: Iterable[CaptureFile]
    set_finalized: bool


@dataclass(frozen=True)
class ProductRule:
    """任务明确要求的类别、格式、数量和适用配对；不推算数量。"""

    kind: FileKind
    format_id: str | None = None
    min_count: int = 1
    exact_count: int | None = None
    require_pairing: bool = False

    def __post_init__(self):
        if not isinstance(self.kind, FileKind):
            raise ValueError("产物规则必须声明文件类别")
        if self.format_id is not None and (not isinstance(self.format_id, str) or not self.format_id):
            raise ValueError("产物格式必须是非空标识或不限制")
        if type(self.min_count) is not int or self.min_count < 0:
            raise ValueError("最低产物数必须是非负整数")
        if self.exact_count is not None and (type(self.exact_count) is not int or self.exact_count < self.min_count):
            raise ValueError("明确产物数必须是不低于最低数量的整数")
        if type(self.require_pairing) is not bool:
            raise ValueError("适用配对要求必须是布尔值")

    def as_json(self) -> dict:
        return {"kind": self.kind.value, "format_id": self.format_id,
                "min_count": self.min_count, "exact_count": self.exact_count,
                "require_pairing": self.require_pairing}

    @classmethod
    def from_json(cls, value) -> "ProductRule":
        from camctl.contracts.json_values import is_json_integer
        from camctl.contracts.values import MAX_OBJECT_ID
        if not isinstance(value, dict) or set(value) != {"kind", "format_id", "min_count", "exact_count", "require_pairing"}:
            raise ValueError("必要产物规则必须具有完整登记成员")
        counts = {}
        for name in ("min_count", "exact_count"):
            raw = value[name]
            if raw is None and name == "exact_count":
                counts[name] = None
            elif not is_json_integer(raw) or not 0 <= raw <= MAX_OBJECT_ID:
                raise ValueError("必要产物数量必须是安全非负 JSON 整数")
            else:
                counts[name] = int(raw)
        return cls(FileKind(value["kind"]), value["format_id"], **counts,
                   require_pairing=value["require_pairing"])


class RuleState(Enum):
    PASSED = "passed"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProductRuleResult:
    rule: ProductRule
    count: int
    state: RuleState


@dataclass(frozen=True)
class ProductRequirements:
    """任务声明的产物要求：必需类别集合；空集合表示无必需产物。"""

    required_kinds: frozenset[FileKind]
    rules: tuple[ProductRule, ...] = ()


@dataclass(frozen=True)
class CaptureAssessment:
    """集合核实的判定：各类事实分别表达。"""

    is_complete: bool
    explicitly_unmet: bool
    missing_kinds: tuple[FileKind, ...]
    incomplete_files: tuple[str, ...]
    ownership_pending: tuple[str, ...]
    read_errors: tuple[str, ...]
    rules: tuple[ProductRuleResult, ...] = ()
    file_count: int = 0
    incomplete_count: int = 0
    ownership_pending_count: int = 0
    read_error_count: int = 0


def assess_capture_files(
    files: CaptureFileSet, requirements: ProductRequirements
) -> CaptureAssessment:
    """按归属、写完与集合要求分别判定产物状态。"""
    read_errors, ownership_pending, incomplete = [], [], []
    file_count = incomplete_count = ownership_count = error_count = 0
    confirmed_kinds = set()
    counts = [0] * len(requirements.rules)
    unknown_rules, failed_pairing = set(), set()
    for file in files.files:
        file_count += 1
        if file.read_error:
            error_count += 1
            if len(read_errors) < 128:
                read_errors.append(file.file_id)
        if not file.ownership_confirmed:
            ownership_count += 1
            if len(ownership_pending) < 128:
                ownership_pending.append(file.file_id)
        if not file.complete:
            incomplete_count += 1
            if len(incomplete) < 128:
                incomplete.append(file.file_id)
        if file.ownership_confirmed:
            confirmed_kinds.add(file.kind)
        for index, rule in enumerate(requirements.rules):
            if file.kind is not rule.kind:
                continue
            if rule.format_id is not None:
                if file.format_id is None:
                    unknown_rules.add(index)
                    continue
                if file.format_id != rule.format_id:
                    continue
            counts[index] += 1
            if rule.require_pairing:
                if file.pairing_confirmed is None:
                    unknown_rules.add(index)
                elif not file.pairing_confirmed:
                    failed_pairing.add(index)
    missing = tuple(
        kind
        for kind in sorted(requirements.required_kinds, key=lambda k: k.value)
        if kind not in confirmed_kinds
    )
    unresolved = not files.set_finalized or bool(error_count or ownership_count or incomplete_count)
    rules = []
    for index, rule in enumerate(requirements.rules):
        if unresolved or index in unknown_rules:
            state = RuleState.UNKNOWN
        elif (counts[index] < rule.min_count
                or (rule.exact_count is not None and counts[index] != rule.exact_count)
                or index in failed_pairing):
            state = RuleState.FAILED
        else:
            state = RuleState.PASSED
        rules.append(ProductRuleResult(rule, counts[index], state))
    unresolved = unresolved or any(rule.state is RuleState.UNKNOWN for rule in rules)
    unmet = bool(missing) or any(rule.state is RuleState.FAILED for rule in rules)
    complete = not unresolved and not unmet
    explicitly_unmet = not unresolved and unmet
    return CaptureAssessment(
        is_complete=complete,
        explicitly_unmet=explicitly_unmet,
        missing_kinds=missing,
        incomplete_files=tuple(incomplete),
        ownership_pending=tuple(ownership_pending),
        read_errors=tuple(read_errors),
        rules=tuple(rules), file_count=file_count,
        incomplete_count=incomplete_count, ownership_pending_count=ownership_count,
        read_error_count=error_count,
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

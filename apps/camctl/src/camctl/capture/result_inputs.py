"""RESULTS v1 的原文件输入；实际返回与本地恢复使用同一解释规则。"""

from dataclasses import dataclass
from typing import Any, Mapping

from camctl.capture.results import FileKind
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.devices.evidence import DeviceObservation, EvidenceContract
from camctl.operations.models import (
    AttemptStatus, AttemptTicket, CallInfo, CallOutcome, EffectState, ErrorValue,
    EvidenceValue, Settlement, SettlementBasis,
)

RESULT_LISTED_TYPE = "result_files_listed"
RESULT_LISTED_VERSION = 1
RESULT_FILES_CONTRACT = EvidenceContract(
    RESULT_LISTED_TYPE, RESULT_LISTED_VERSION, "result",
    frozenset({"activity_id", "entries"}), identity_field="activity_id")


@dataclass(frozen=True)
class ObservedFile:
    """候选文件的原定位与完成观察；归属、配对及正式产物各自保存。"""

    identity: str
    locator: Mapping[str, Any]
    evidence: Mapping[str, Any]
    complete: bool
    size_bytes: int | None
    kind: FileKind = FileKind.OTHER
    original_name: str | None = None
    media_type: str | None = None
    paired_identity: str | None = None


@dataclass(frozen=True)
class ListedResult:
    """原文件输入与完整实际调用结果，二者使用同一来源。"""

    entries: tuple[ObservedFile, ...]
    outcome: CallOutcome


def observed_file(entry: Any) -> ObservedFile:
    """解释 v1 列举条目；非法结构不变成空文件或完成文件。"""
    if not isinstance(entry, Mapping) or not isinstance(entry.get("identity"), str) or not entry["identity"]:
        raise ValueError(f"列举条目缺少稳定文件身份: {entry!r}")
    locator = entry.get("locator")
    if not isinstance(locator, Mapping):
        raise ValueError(f"列举条目缺少定位结构: {entry!r}")
    size = entry.get("size_bytes")
    if size is not None and (isinstance(size, bool) or not isinstance(size, int)):
        raise ValueError(f"列举条目大小不是整数或空: {entry!r}")
    complete = entry.get("complete")
    if not isinstance(complete, bool):
        raise ValueError(f"列举条目未声明完整与否: {entry!r}")
    try:
        kind = FileKind(entry.get("kind", "other"))
    except ValueError:
        kind = FileKind.OTHER
    original, media, paired = entry.get("original_name"), entry.get("media_type"), entry.get("paired_identity")
    if original is not None and not isinstance(original, str):
        raise ValueError(f"列举条目原始文件名不是文本: {entry!r}")
    if media is not None and not isinstance(media, str):
        raise ValueError(f"列举条目媒体类型不是文本: {entry!r}")
    if paired is not None and (not isinstance(paired, str) or not paired):
        raise ValueError(f"列举条目配对身份不是非空文本或空: {entry!r}")
    return ObservedFile(entry["identity"], dict(locator), dict(entry), complete, size,
                        kind, original, media, paired)


def files_from_outcome(ticket: AttemptTicket, outcome: CallOutcome) -> tuple[ObservedFile, ...]:
    """读取原登记 v1 观察，不依赖当前驱动或推定集合已经确定。"""
    listed = [value for value in outcome.observations if value.type == RESULT_LISTED_TYPE]
    if not listed:
        raise ConsistencyError("已保存 RESULTS 缺少完整本地文件输入")
    entries = []
    for value in listed:
        if (value.version != RESULT_LISTED_VERSION
                or set(value.data) != {"activity_id", "entries"}
                or value.data["activity_id"] != ticket.target_id
                or not isinstance(value.data["entries"], (list, tuple))):
            raise ConsistencyError("已保存 RESULTS 文件观察版本、目标或结构不可解释")
        entries.extend(observed_file(entry) for entry in value.data["entries"])
    return tuple(entries)


def saved_outcome(status: int, effect: int, result: Mapping, error: Mapping | None) -> CallOutcome:
    """恢复原外层结果；状态与错误仍来自原尝试，不生成成功结果。"""
    try:
        if result["format_version"] != 1:
            raise ValueError("结果外层版本不支持")
        settlement = result["settlement"]
        evidence = settlement["evidence"]
        info = result.get("call_info")
        call_info = None
        if info is not None:
            local = info.get("local_exit", {})
            call_info = CallInfo(local_exit_code=local.get("exit_code"),
                                 local_signal=local.get("signal"),
                                 remote_exit_code=info.get("remote_exit_code"))
        return CallOutcome(
            status=AttemptStatus(enum_for("operation_attempts.status")(status).name.lower()),
            effect=EffectState(enum_for("operation_attempts.effect_state")(effect).name.lower()),
            error=None if error is None else ErrorValue(error["code"], error["stage"], error.get("details", {})),
            settlement=Settlement(SettlementBasis(settlement["basis"]),
                EvidenceValue(evidence["type"], evidence["version"], evidence["data"])),
            observations=tuple(DeviceObservation(value["type"], value["version"], value["data"])
                               for value in result["observations"]),
            call_info=call_info,
        )
    except (KeyError, TypeError, ValueError) as failure:
        raise ConsistencyError("已保存 RESULTS 完整结果不可解释") from failure

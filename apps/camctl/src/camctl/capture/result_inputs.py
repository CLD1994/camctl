"""RESULTS 文件输入和分页登记；实际返回与本地恢复共用解释规则。"""

from dataclasses import dataclass
from typing import Any, Mapping

from camctl.capture.results import FileKind
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.devices.evidence import DeviceObservation, EvidenceContract, validate_observation
from camctl.devices.bindings import DeviceBinding
from camctl.devices.directory import DirectoryCursor
from camctl.operations.models import (
    AttemptStatus, AttemptTicket, CallInfo, CallOutcome, EffectState, ErrorValue,
    EvidenceValue, Settlement, SettlementBasis,
)

from camctl.operations.result_format import read_result_document

RESULT_LISTED_TYPE = "result_files_listed"
RESULT_LISTED_VERSION = 1
RESULT_FILES_CONTRACT = EvidenceContract(
    RESULT_LISTED_TYPE, RESULT_LISTED_VERSION, "result",
    frozenset({"activity_id", "entries"}), identity_field="activity_id")
RESULT_PAGE_CONTRACT = EvidenceContract(
    RESULT_LISTED_TYPE, 2, "result",
    frozenset({"activity_id", "entries", "cursor", "next_cursor", "set_finalized", "completion_evidence"}),
    identity_field="activity_id")


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
    format_id: str | None = None


@dataclass(frozen=True)
class ListedResult:
    """原文件输入与完整实际调用结果，二者使用同一来源。"""

    entries: tuple[ObservedFile, ...]
    outcome: CallOutcome


@dataclass(frozen=True)
class ResultPage:
    """一批实际返回；完整扫描、集合和设备完成是独立依据。"""

    entries: tuple[ObservedFile, ...]
    cursor: DirectoryCursor | None
    next_cursor: DirectoryCursor | None
    set_finalized: bool
    completion_evidence: DeviceObservation | None
    outcome: CallOutcome
    scan_complete: bool


def page_from_outcome(ticket: AttemptTicket, outcome: CallOutcome, *,
                      cursor: DirectoryCursor | None = None,
                      binding: DeviceBinding | None = None) -> ResultPage:
    """解码原实际观察；没有观察的错误保留，非法观察不会变成空目录。"""
    if ticket.operation != "result" or ticket.responsibility_key != f"results/{ticket.target_id}":
        raise ConsistencyError("结果页要求原 RESULTS 活动票据")
    listed = [value for value in outcome.observations if value.type == RESULT_LISTED_TYPE]
    if not listed:
        if outcome.error is None:
            raise ConsistencyError("RESULTS 缺少完整文件观察")
        return ResultPage((), cursor, None, False, None, outcome, False)
    if len(listed) != 1:
        raise ConsistencyError("一次结果页必须具有一份文件观察")
    value = listed[0]
    if type(value.version) is not int:
        raise ConsistencyError("结果文件观察版本必须是登记整数")
    contract = RESULT_FILES_CONTRACT if value.version == 1 else RESULT_PAGE_CONTRACT
    validate_observation(value, contract, expected_identity=ticket.target_id)
    if set(value.data) != contract.fields or not isinstance(value.data["entries"], (list, tuple)):
        raise ConsistencyError("RESULTS 文件观察缺少完整登记成员")
    entries = tuple(observed_file(entry) for entry in value.data["entries"])
    if value.version == 1:
        if cursor is not None:
            raise ConsistencyError("旧 v1 无法响应后续分页游标")
        return ResultPage(entries, None, None, False, None, outcome, False)
    if len(entries) > 128:
        raise ConsistencyError("结果页超过 128 个条目的上限")
    input_cursor = None if value.data["cursor"] is None else DirectoryCursor.from_json(value.data["cursor"])
    next_cursor = None if value.data["next_cursor"] is None else DirectoryCursor.from_json(value.data["next_cursor"])
    if input_cursor != cursor:
        raise ConsistencyError("结果页没有响应原请求游标")
    for item in (input_cursor, next_cursor):
        if item is not None and binding is not None and item.binding != binding:
            raise ConsistencyError("结果页游标改变原设备绑定")
    if next_cursor is not None and input_cursor is not None:
        if (next_cursor.binding != input_cursor.binding or next_cursor.directories != input_cursor.directories
                or (next_cursor.directory_index, next_cursor.after_path or "")
                   <= (input_cursor.directory_index, input_cursor.after_path or "")):
            raise ConsistencyError("结果页游标未沿原范围推进")
    finalized = value.data["set_finalized"]
    if type(finalized) is not bool or (finalized and next_cursor is not None):
        raise ConsistencyError("结果页集合保证类型或扫描阶段不合法")
    completion = value.data["completion_evidence"]
    if completion is not None:
        if (not isinstance(completion, Mapping) or set(completion) != {"type", "version", "data"}
                or not isinstance(completion["type"], str) or not completion["type"]
                or type(completion["version"]) is not int or completion["version"] < 1
                or not isinstance(completion["data"], Mapping)):
            raise ConsistencyError("完成依据必须是完整类型化实际观察")
        completion = DeviceObservation(completion["type"], completion["version"], completion["data"])
        identity_contract = EvidenceContract(completion.type, completion.version, "result",
                                            frozenset(completion.data), identity_field="activity_id")
        validate_observation(completion, identity_contract, expected_identity=ticket.target_id)
    return ResultPage(entries, input_cursor, next_cursor, finalized, completion, outcome,
                      next_cursor is None and outcome.error is None)


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
    format_id = entry.get("format_id")
    if format_id is not None and (not isinstance(format_id, str) or not format_id):
        raise ValueError("列举条目的格式必须是非空标识或未知")
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
                        kind, original, media, paired, format_id)


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
        return read_result_document(status, effect, result, error)
    except (KeyError, TypeError, ValueError) as failure:
        raise ConsistencyError("已保存 RESULTS 完整结果不可解释") from failure

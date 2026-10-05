"""报告专属文件规则与发布编排的真实消费者。

状态报告不经中间文件登记：文件名由报告身份和首次确定字节构
成，staging 内使用报告专属直接子目录。本模块提供文件名规则、
交接目录的恢复观察、补投资格决策，以及"写入 staging → 登记
字节 → 保存意图 → 撤下旧 ready 报告 → 原子移动 → 保存发布 →
同步本地完成"的发布编排；失败分类保留各步实际证据，不把未知
结果解释为成功或缺失。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import NamedTuple, Protocol, Sequence

from camctl.contracts.values import ObjectId, new_operation_key
from camctl.host_files.handoff import (
    HandoffDirectories,
    HandoffIdentity,
    PublishResult,
    PublishStage,
    ReadyName,
    WithdrawResult,
    WithdrawStage,
    publish_staged_file,
    withdraw_ready_file,
)
from camctl.host_files.io import DirectorySyncStage
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.runtime import OwnedConnection
from camctl.reporting import policy
from camctl.reporting.ack import AckReport
from camctl.reporting.models import FrozenReport, ReportBytes

__all__ = [
    "DeliveryOutcome",
    "DeliveryResult",
    "DbPublicationSession",
    "PublicationSession",
    "ReportDirectories",
    "ReportFileDecision",
    "ReportFileIdentity",
    "ReportLocations",
    "REPORTS_DIRECTORY",
    "RepublishDecision",
    "Sighting",
    "StagedReport",
    "decide_republish",
    "deliver_report",
    "observe_report_locations",
    "parse_report_file_name",
    "recover_report_files",
    "report_file_name",
]

#: staging 根下报告文件的专属直接子目录；不与中间文件用途目录重叠。
REPORTS_DIRECTORY = "reports"

_BINARY = getattr(os, "O_BINARY", 0)

_NAME_PATTERN = re.compile(
    r"\Astatus-report-([1-9][0-9]*)-([0-9a-f]{64})\.json\Z"
)


class ReportFileIdentity(NamedTuple):
    """从文件名解析出的报告身份：报告编号与文件自称摘要。"""

    report_id: int
    sha256: str


def report_file_name(report_id: int, sha256: str) -> str:
    """按报告身份与字节摘要构造唯一文件名。"""
    ObjectId(report_id)
    if (not isinstance(sha256, str) or len(sha256) != 64
            or any(c not in "0123456789abcdef" for c in sha256)):
        raise ValueError(f"报告摘要必须是 64 位小写十六进制: {sha256!r}")
    return f"status-report-{report_id}-{sha256}.json"


def parse_report_file_name(name: str) -> ReportFileIdentity | None:
    """解析报告文件名；不符合命名规则的名字不属于状态报告。"""
    if not isinstance(name, str):
        return None
    match = _NAME_PATTERN.fullmatch(name)
    if match is None:
        return None
    try:
        ObjectId(int(match.group(1)))
    except ValueError:
        return None
    return ReportFileIdentity(int(match.group(1)), match.group(2))


@dataclass(frozen=True)
class Sighting:
    """一个交接目录中可靠识别到的报告文件；错误保留实际原因。"""

    files: tuple[ReportFileIdentity, ...]
    error: str | None


@dataclass(frozen=True)
class ReportLocations:
    """三处目录的恢复观察结果。"""

    ready: Sighting
    processing: Sighting
    staging: Sighting


@dataclass(frozen=True)
class ReportDirectories:
    """报告文件涉及的绑定目录。"""

    staging: Path
    ready: Path
    processing: Path


class ReportFileDecision(Enum):
    """恢复观察对一份已登记报告的位置事实分类。"""

    PRESENT_ELSEWHERE = "present_elsewhere"
    STAGING_RESIDUE = "staging_residue"
    NOT_PRESENT = "not_present"
    UNRELIABLE = "unreliable"


def recover_report_files(
    report: FrozenReport, locations: ReportLocations
) -> ReportFileDecision:
    """按目录观察判定一份报告的文件位置事实。

    ready 或 processing 中已有该报告的文件时保留不动（processing
    中的报告绝不修改）；仅 staging 残留时需要发布则从头生成；三处
    均可靠确认为空时沿原身份重建。任一目录观察失败都不能当作文
    件不存在。
    """
    for sighting in (locations.ready, locations.processing, locations.staging):
        if sighting.error is not None:
            return ReportFileDecision.UNRELIABLE
    delivered = locations.ready.files + locations.processing.files
    for identity in delivered:
        if identity.report_id == report.report_id:
            return ReportFileDecision.PRESENT_ELSEWHERE
    for identity in locations.staging.files:
        if identity.report_id == report.report_id:
            return ReportFileDecision.STAGING_RESIDUE
    return ReportFileDecision.NOT_PRESENT


class RepublishDecision(Enum):
    """报告补投资格的决策分类。"""

    NOT_NEEDED = "not_needed"
    KEEP_EXISTING = "keep_existing"
    REPUBLISH_EXISTING = "republish_existing"
    CREATE_NEW = "create_new"
    UNRELIABLE = "unreliable"


def decide_republish(
    responsibility_open: bool,
    covering: AckReport | None,
    locations: ReportLocations,
) -> RepublishDecision:
    """判断是否补投及补投身份。

    责任判断优先：无未确认变化且无待确认同步时不依赖目录观察。
    已登记的满足全部要求的报告（covering）存在于 ready 或
    processing 时保留；可靠确认不在两目录时沿原身份同字节重建；
    没有满足要求的已登记报告时创建新报告。covering 存在而目录观
    察不可靠时不能断定文件缺失。
    """
    if not responsibility_open:
        return RepublishDecision.NOT_NEEDED
    if covering is None:
        return RepublishDecision.CREATE_NEW
    for sighting in (locations.ready, locations.processing):
        if sighting.error is not None:
            return RepublishDecision.UNRELIABLE
    delivered = locations.ready.files + locations.processing.files
    for identity in delivered:
        if identity.report_id == covering.report_id:
            return RepublishDecision.KEEP_EXISTING
    return RepublishDecision.REPUBLISH_EXISTING


class StagedReport:
    """staging 中完整写入后的报告文件事实。"""

    __slots__ = ("name", "size_bytes", "sha256")

    def __init__(self, *, name: str, size_bytes: int, sha256: str) -> None:
        self.name = name
        self.size_bytes = size_bytes
        self.sha256 = sha256


class DeliveryOutcome(Enum):
    """一次发布编排的实际结果分类。"""

    PUBLISHED = "published"
    WRITE_FAILED = "write_failed"
    WITHDRAW_FAILED = "withdraw_failed"
    HANDOFF_FAILED = "handoff_failed"
    STORE_FAILED = "store_failed"
    UNKNOWN = "unknown"


class DeliveryResult:
    """发布编排结果；成功时携带最终文件事实。"""

    __slots__ = ("outcome", "report", "error")

    def __init__(self, outcome: DeliveryOutcome, report: StagedReport | None = None,
                 error: str | None = None) -> None:
        self.outcome = outcome
        self.report = report
        self.error = error


class PublicationSession(Protocol):
    """发布编排所需的状态库管理端口。"""

    def prepare(self, report_id: int, contents: ReportBytes) -> DbOutcome: ...

    def record_intent(self, report_id: int) -> DbOutcome: ...

    def save_publication(self, report_id: int,
                         file_result: PublishResult) -> DbOutcome: ...

    def record_failure(self, report_id: int, error: dict) -> DbOutcome: ...

    def settle_local(self, action_id: int, report_id: int) -> DbOutcome: ...


class DbPublicationSession:
    """把发布端口适配到报告管理入口的真实会话。

    每次管理事实都是独立事务：各步分别生成操作键，重放语义由
    入口的幂等分支承担。
    """

    def __init__(self, owned: OwnedConnection) -> None:
        self._owned = owned

    def prepare(self, report_id: int, contents: ReportBytes) -> DbOutcome:
        return policy.record_report_bytes(
            new_operation_key(), self._owned, report_id, contents)

    def record_intent(self, report_id: int) -> DbOutcome:
        return policy.record_report_publish_intent(
            new_operation_key(), self._owned, report_id)

    def save_publication(self, report_id: int,
                         file_result: PublishResult) -> DbOutcome:
        return policy.publish_report(
            new_operation_key(), self._owned, report_id, file_result)

    def record_failure(self, report_id: int, error: dict) -> DbOutcome:
        return policy.record_report_failure(
            new_operation_key(), self._owned, report_id, error)

    def settle_local(self, action_id: int, report_id: int) -> DbOutcome:
        return policy.record_local_report(
            new_operation_key(), self._owned, action_id=action_id,
            local_report_id=report_id, occurred_at=0)


def _report_staging_directory(staging_root: Path) -> Path:
    return staging_root / REPORTS_DIRECTORY


def _describe(kind: str, failure: Exception) -> str:
    return f"{kind}: {type(failure).__name__}: {failure}"


# 窄注入点：真实文件操作的系统调用边界；仅测试替换。
def _stage_payload(staging_root: Path, name: str, payload: bytes) -> str | None:
    """在报告专属 staging 子目录写入完整文件并同步。

    同一报告的既有残留（不完整或摘要不符的旧文件）由本次完整重
    新生成的内容替代；替代失败保留原残留并阻断发布。
    """
    directory = _report_staging_directory(staging_root)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        report_id = parse_report_file_name(name).report_id
        for entry in os.listdir(directory):
            identity = parse_report_file_name(entry)
            if (identity is not None and identity.report_id == report_id
                    and entry != name):
                os.unlink(directory / entry)
        fd = os.open(directory / name,
                     os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _BINARY, 0o600)
    except OSError as failure:
        return _describe("open_failed", failure)
    error: str | None = None
    try:
        view = memoryview(payload)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    except OSError as failure:
        error = _describe("stage_failed", failure)
    finally:
        try:
            os.close(fd)
        except OSError as failure:
            close_error = _describe("close_failed", failure)
            error = close_error if error is None else f"{error}; {close_error}"
    return error


def _list_stale_ready(ready_dir: Path,
                      current: str) -> tuple[tuple[str, ...], str | None]:
    """列出 ready 中待替换的旧状态报告文件名。"""
    try:
        names = os.listdir(ready_dir)
    except OSError as failure:
        return (), _describe("list_failed", failure)
    return tuple(
        name for name in names
        if name != current and parse_report_file_name(name) is not None
    ), None


def _withdraw_stale(ready_dir: Path, name: str) -> WithdrawResult:
    return withdraw_ready_file(HandoffIdentity(ready_dir, name))


async def _move_staged(staging_root: Path, ready_dir: Path,
                       name: str) -> PublishResult:
    return await publish_staged_file(
        _report_staging_directory(staging_root) / name,
        HandoffDirectories(staging_root, ready_dir), ReadyName(name))


def _sighting(directory: Path, *, missing_as_empty: bool = False) -> Sighting:
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        # staging 的报告子目录由首次写入创建：尚不存在表示无残留。
        # ready 与 processing 是部署绑定目录，缺失仍是观察错误。
        if missing_as_empty:
            return Sighting((), None)
        return Sighting((), _describe("missing_directory", FileNotFoundError()))
    except OSError as failure:
        return Sighting((), _describe("list_failed", failure))
    files = []
    for name in names:
        identity = parse_report_file_name(name)
        if identity is not None:
            files.append(identity)
    return Sighting(tuple(files), None)


def observe_report_locations(directories: ReportDirectories) -> ReportLocations:
    """观察三处目录中的报告文件；观察失败不视为文件不存在。"""
    return ReportLocations(
        ready=_sighting(directories.ready),
        processing=_sighting(directories.processing),
        staging=_sighting(_report_staging_directory(directories.staging),
                          missing_as_empty=True),
    )


def _store_failure(outcome: DbOutcome) -> DeliveryResult:
    if outcome.kind is DbOutcomeKind.ROLLED_BACK:
        return DeliveryResult(
            DeliveryOutcome.STORE_FAILED,
            error=f"rolled_back: {outcome.error}")
    return DeliveryResult(
        DeliveryOutcome.UNKNOWN, error=f"unknown: {outcome.error}")


async def deliver_report(
    report_id: int,
    payload: bytes,
    directories: HandoffDirectories,
    session: PublicationSession,
    *,
    local_actions: Sequence[int] = (),
) -> DeliveryResult:
    """执行一次报告发布的完整编排。

    staging 写入与字节登记、发布意图、旧报告撤下、原子移动和发
    布记录按证据顺序推进；任何一步失败或结果未知都停止后续步
    骤并保留实际分类。发布成功后对关联的同步动作保存本地完成，
    该步失败不撤销已确认的发布。
    """
    digest = hashlib.sha256(payload).hexdigest()
    staged = StagedReport(
        name=report_file_name(report_id, digest),
        size_bytes=len(payload), sha256=digest)
    stage_error = await asyncio.to_thread(
        _stage_payload, directories.staging, staged.name, payload)
    if stage_error is not None:
        return DeliveryResult(DeliveryOutcome.WRITE_FAILED, error=stage_error)

    prepare = session.prepare(
        report_id, ReportBytes(staged.size_bytes, staged.sha256))
    if prepare.kind is not DbOutcomeKind.COMPLETED:
        return _store_failure(prepare)
    intent = session.record_intent(report_id)
    if intent.kind is not DbOutcomeKind.COMPLETED:
        return _store_failure(intent)

    stale, stale_error = await asyncio.to_thread(
        _list_stale_ready, directories.ready, staged.name)
    if stale_error is not None:
        return DeliveryResult(DeliveryOutcome.WITHDRAW_FAILED,
                              error=stale_error)
    for name in stale:
        withdrawn = await asyncio.to_thread(
            _withdraw_stale, directories.ready, name)
        if withdrawn.stage in (WithdrawStage.FAILED, WithdrawStage.UNKNOWN):
            return DeliveryResult(
                DeliveryOutcome.WITHDRAW_FAILED,
                error=withdrawn.error or withdrawn.stage.value)

    file_result = await _move_staged(
        directories.staging, directories.ready, staged.name)
    if file_result.stage is PublishStage.UNKNOWN:
        return DeliveryResult(
            DeliveryOutcome.UNKNOWN, error=file_result.error)
    if file_result.stage is not PublishStage.MOVED:
        reason = file_result.error or "not moved"
        session.record_failure(report_id, {"error": reason})
        return DeliveryResult(DeliveryOutcome.HANDOFF_FAILED, error=reason)
    try:
        policy.validate_report_publication_result(file_result)
    except Exception as failure:
        # 移动已发生但交接不完整（同步失败、源未移除等）：保存实
        # 际文件处理错误，不解释为成功。
        detail = file_result.error or str(failure)
        session.record_failure(report_id, {"error": detail})
        return DeliveryResult(DeliveryOutcome.HANDOFF_FAILED, error=detail)

    saved = session.save_publication(report_id, file_result)
    if saved.kind is not DbOutcomeKind.COMPLETED:
        return _store_failure(saved)

    settlement_error = None
    for action_id in local_actions:
        settled = session.settle_local(action_id, report_id)
        if settled.kind is not DbOutcomeKind.COMPLETED:
            # 发布已确认成功；本地完成责任保留，由后续机会再保存。
            settlement_error = (
                f"local action {action_id}: {settled.kind.value}"
                + (f": {settled.error}" if settled.error else ""))
            break
    return DeliveryResult(DeliveryOutcome.PUBLISHED, staged,
                          settlement_error)

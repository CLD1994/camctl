"""录像媒体链调用方：D4 读取会话绑定与检查修复组合。

把内部输入取得、检查与修复编排接到真实仓储和设备读取端口：
DriverReadSessions 按设备文件的完成事实经驱动 open_read 打开可停
止读取会话；RecordingInputCopies 组合资格、续传与完整性事务；
CaptureProcessingSaves 适配处理事务。run_recording_media 串联输入
取得、检查执行与修复执行，各失败分区原样透传，不在本层重试。
"""

from __future__ import annotations

from contextlib import closing
from collections.abc import Awaitable
from copy import deepcopy
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import Enum
from pathlib import Path
from time import monotonic_ns as _default_monotonic_ns
from types import SimpleNamespace
from typing import Any, Callable
from camctl.logging_runtime.diagnostics import DiagnosticState, record_error

from camctl.capture.files import FileChecksumSave
from camctl.capture.media_facts import probe_document, saved_source_probe
from camctl.capture.recovery import RecoveryBoundary, RecoveryDiagnostic
from camctl.capture.input_copy import (
    InputContext,
    InputPhase,
    InputStep,
    obtain_recording_input,
)
from camctl.capture.media import (
    CheckContext,
    CheckExecutionPhase,
    CheckStep,
    MediaPolicy,
    MediaTools,
    ProcessingSaves,
    ProcessingStatus,
    RepairContext,
    RepairStep,
    SaveDisposition,
    SaveReceipt,
    execute_check,
    execute_repair,
    require_saved_media_result,
)
from camctl.capture.processing import (
    CheckResultSave, ProcessingError, RepairDecisionSave, RepairOutputFile, RepairResultSave,
    RepairStart, RepairSuccess, saved_check_duration, saved_target_duration_ms,
)
from camctl.contracts.json_values import json_equal, parse_exact_json
from camctl.contracts.values import ConsistencyError, OperationKey, new_operation_key
from camctl.devices.ports import ReadDriver
from camctl.devices.read_session import SourceFile
from camctl.host_files.models import BoundDirectories
from camctl.host_files.tasks import AsyncFileTask, FileTaskExecutor, FileTaskId
from camctl.outputs.copy import (
    CompletionContext,
    CopyContext,
    SegmentContext,
    SourceDigestReader,
    complete_copy,
    copy_next_segment,
    prepare_copy,
)
from camctl.outputs.qualification import FileCandidate, OperationConfig, QualificationOutcome
from camctl.outputs.database_reads import read_one as _read_one, read_all as _read_all
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
from camctl.operations.attempts import RetryWaitGate
from camctl.operations.attempts import (
    AttemptConfig, AttemptFinish, AttemptIntent, AttemptTarget, OperationKind,
    RunFinish, RunOutcome,
)
from camctl.operations.models import (
    AttemptStatus, CallOutcome, EffectState, ErrorValue, EvidenceValue,
    Settlement, SettlementBasis,
)
from camctl.operations.validation import validate_outcome
from camctl.outputs.read_attempts import (
    PendingReadResult, PendingStoppedRead, acquire_read_attempt, forget_read_result, hold_read_result,
    retry_read_business, run_owned_read, save_read_result, read_failure_details,
)
from camctl.outputs.slots import SlotOutcome, SlotRequest
from camctl.persistence.models import db_failure, db_outcome_errors, DbOutcomeKind as _DbOutcomeKind, db_outcome_for
from camctl.persistence.business import BusinessDatabase, settle_required
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.runtime import OwnedConnection
from camctl.session.supervision import Supervisor

__all__ = [
    "CaptureProcessingSaves",
    "DriverReadSessions",
    "MediaFlow",
    "RecordingInputCopies",
    "load_confirmed_source",
    "load_processing_status",
    "run_recording_media",
]

#: 独立装配的读取默认值；正式会话采用本次设备配置与公共段大小。
_SEGMENT_SIZE = 4 * 1024 * 1024
_READ_MAX_ATTEMPTS = 3
_READ_TIMEOUT_S = Decimal("10")


def load_confirmed_source(owned: OwnedConnection, file_id: int) -> SourceFile:
    """按身份事实装载已确认写完的设备源文件；未完成不可读取。"""
    with closing(owned.connection.execute(
        "SELECT identity_key, locator_json, size_bytes, completion_state, sha256"
        " FROM device_files WHERE id = ?", (file_id,)
    )) as cursor:
        row = cursor.fetchone()
    if row is None:
        raise ConsistencyError(f"设备文件不存在: {file_id}")
    identity_key, locator_raw, size_bytes, completion, sha256 = row
    if completion != 3 or size_bytes is None:
        raise ConsistencyError(
            f"设备文件尚未确认写完，不能读取: {file_id}")
    try:
        locator = parse_exact_json(locator_raw)
        identity_parts = parse_exact_json(identity_key)
        from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
        if (isinstance(identity_parts, list) and len(identity_parts) == 3
                and identity_parts[1] == DRIVER_ID):
            from camctl.devices.drivers.hasselblad.files import CameraFileIdentity
            identity = CameraFileIdentity.from_source(identity_key, locator)
            if identity.size_bytes != size_bytes or identity.sha256 != sha256:
                raise ValueError("源长度或摘要与固定身份不一致")
        return SourceFile(identity_key, locator, int(size_bytes))
    except (TypeError, ValueError, KeyError) as error:
        raise ConsistencyError(f"设备文件的固定身份或定位无效: {file_id}") from error


class DriverReadSessions:
    """D4 读取会话绑定：按设备文件完成事实经驱动打开可停止会话。

    源身份使用全库唯一的身份键与驱动定位结构；未完成或缺少完整
    大小的文件不可读。读取尝试票据由装配层按实际尝试提供。
    """

    def __init__(self, database: BusinessDatabase, driver: ReadDriver,
                 ticket: Any, *, idle_timeout_s: Decimal = _READ_TIMEOUT_S, control=None) -> None:
        self._database = database
        self._driver = driver
        self._ticket = ticket
        self._control = control
        self.idle_timeout_s = idle_timeout_s

    def for_attempt(self, ticket, *, idle_timeout_s: Decimal, control=None):
        """为已可靠保存的原尝试构造本次读取会话入口。"""
        return DriverReadSessions(self._database, self._driver, ticket,
                                  idle_timeout_s=idle_timeout_s, control=control)

    async def open_session(self, source_device_file_id: int, offset: int, *, on_start=None):
        from camctl.outputs.read_attempts import read_dispatch_qualification, read_dispatch_gate

        if self._ticket is None:
            raise ConsistencyError("设备读取会话缺少已保存的尝试票据")
        def read(owned):
            return (load_confirmed_source(owned, source_device_file_id),
                read_dispatch_qualification(owned, self._ticket, source_device_file_id, device_source=True))
        try:
            source, qualification = await self._database.read(read, description="媒体读取源事实")
        except Exception as error:
            from camctl.devices.read_session import ReadPreparationError, ReadPreparationSettlement
            raise ReadPreparationError("设备读取源事实未取得，原尝试尚未调用",
                settlement=ReadPreparationSettlement.UNCONFIRMED) from error
        prevented = read_dispatch_gate(qualification, self._control)
        if prevented is not None:
            return prevented
        if on_start is not None:
            on_start()
        return await self._driver.open_read(source, offset, self._ticket,
                                           idle_timeout_s=self.idle_timeout_s)


class RecordingInputCopies:
    """内部输入拷贝端口适配：资格、状态、续传、分段与完整性事务。"""

    def __init__(self, database: BusinessDatabase, roots: BoundDirectories,
                 occurred_at: Callable[[], int], *,
                 max_recopies: int = 1,
                 file_executor: FileTaskExecutor | None = None,
                 read_discovery_batch_size: int | None = None) -> None:
        self.database = database
        self.roots = roots
        self.occurred_at = occurred_at
        self.max_recopies = max_recopies
        self.file_executor = file_executor
        self.repository = OutputsRepository() if read_discovery_batch_size is None else OutputsRepository(read_discovery_batch_size)

    @staticmethod
    def _receipt(outcome) -> SaveReceipt:
        if outcome.kind is _DbOutcomeKind.COMPLETED:
            return SaveReceipt(SaveDisposition.SAVED, value=outcome.value)
        if outcome.kind is _DbOutcomeKind.ROLLED_BACK:
            return SaveReceipt(SaveDisposition.REJECTED, error=outcome.error)
        return SaveReceipt(SaveDisposition.UNKNOWN, error=outcome.error)

    async def qualify(self, candidate: FileCandidate) -> SaveReceipt:
        from camctl.outputs.diagnostics import record_qualification

        key = new_operation_key()
        receipt = self._receipt(await self.database.write(
            lambda owned: self.repository.grant_file(candidate, key, owned),
            key=key, description="内部输入资格"))
        if receipt.disposition is SaveDisposition.SAVED:
            record_qualification(receipt.value, candidate)
        return receipt

    async def copy_state(self, copy_id: int):
        return await self.database.read(
            lambda owned: self.repository.load_copy_state(copy_id, owned),
            description="内部输入副本状态")

    async def prepare(self, copy_id: int):
        return await prepare_copy(copy_id, CopyContext(
            repository=self.repository, database=self.database, roots=self.roots,
            occurred_at=self.occurred_at(), executor=self.file_executor))

    async def transfer(self, copy_id: int, session, segment_size: int):
        return await copy_next_segment(copy_id, SegmentContext(
            repository=self.repository, database=self.database, roots=self.roots,
            occurred_at=self.occurred_at(), segment_size=segment_size,
            session=session, executor=self.file_executor))

    async def complete(self, copy_id: int, digest):
        return await complete_copy(copy_id, CompletionContext(
            repository=self.repository, database=self.database, roots=self.roots,
            occurred_at=self.occurred_at(), digest=digest,
            max_recopies=self.max_recopies, executor=self.file_executor))


class MediaSaveStage(Enum):
    """原媒体申请所属的仓储操作。"""

    CHECK = "save_check_result"
    DECISION = "save_repair_decision"
    REPAIR = "save_repair_result"
    START = "start_repair_output"
    COMPLETE = "complete_repair_output"


@dataclass(frozen=True)
class PendingMediaRequest:
    """已取得的原申请；action_id 为空时只表示拥有者尚待数据库解析。"""

    stage: MediaSaveStage
    command: CheckResultSave | RepairDecisionSave | RepairResultSave | RepairStart | RepairSuccess
    key: OperationKey
    action_id: int | None
    file_ids: tuple[int, ...]
    save_error: BaseException | None = None
    diagnostic: DiagnosticState = field(default_factory=DiagnosticState, compare=False, repr=False)


def _media_request_values(command):
    """沿类型化申请的业务编码核实完整原输入，区分 JSON 布尔与数字。"""
    values = {"processing_id": command.processing_id, "occurred_at": command.occurred_at}
    if isinstance(command, CheckResultSave):
        values["media"] = command.media.as_json()
    elif isinstance(command, RepairDecisionSave):
        values.update(decision=command.decision.value, basis=command.basis.as_json())
    elif isinstance(command, RepairResultSave):
        values.update(phase=command.phase.value, output_file_id=command.output_file_id,
            error=None if command.error is None else command.error.as_json())
    elif isinstance(command, RepairStart):
        values["extension"] = command.extension
    elif isinstance(command, RepairSuccess):
        values.update(output_file_id=command.output_file_id,
            size_bytes=command.size_bytes, sha256=command.sha256)
    else:
        raise TypeError("媒体保存必须使用已定义的类型化申请")
    if isinstance(command, (CheckResultSave, RepairStart)) and command.source_probe is not None:
        values["source_probe"] = probe_document(command.source_probe)
    if isinstance(command, (RepairSuccess, RepairResultSave)) and command.output_probe is not None:
        values["output_probe"] = probe_document(command.output_probe)
    return values


class CaptureProcessingSaves:
    """媒体申请在首次写入前持有原输入与原键，确认保存才解除。"""

    def __init__(self, database: BusinessDatabase, pending=None, *, file_ids=()) -> None:
        self.database = database
        self.repository = CaptureRepository()
        self.pending = {} if pending is None else pending
        self.file_ids = file_ids

    @staticmethod
    def _commit(outcome) -> SaveReceipt:
        if outcome.kind is _DbOutcomeKind.COMPLETED:
            return SaveReceipt(SaveDisposition.SAVED, value=outcome.value)
        errors = db_outcome_errors(outcome)
        error = (errors[0] if len(errors) == 1 else
            db_failure(ConsistencyError, "原媒体申请保存未可靠完成", outcome)) if errors else None
        if error is not None and not isinstance(error, Exception):
            raise error
        if outcome.kind is _DbOutcomeKind.ROLLED_BACK:
            return SaveReceipt(SaveDisposition.REJECTED, error=error)
        return SaveReceipt(SaveDisposition.UNKNOWN, error=error)

    async def save_check_result(self, command):
        return await self._save(MediaSaveStage.CHECK, command)

    async def save_repair_decision(self, command):
        return await self._save(MediaSaveStage.DECISION, command)

    async def save_repair_result(self, command):
        return await self._save(MediaSaveStage.REPAIR, command)

    async def start_repair_output(self, command):
        return await self._save(MediaSaveStage.START, command)

    async def complete_repair_output(self, command):
        return await self._save(MediaSaveStage.COMPLETE, command)

    async def _save(self, stage, command):
        return await settle_required(self._save_held(stage, command))

    async def _save_held(self, stage, command):
        identity = (stage, command.processing_id)
        held = self.pending.get(identity)
        if held is None:
            files = self.file_ids
            if isinstance(command, (RepairSuccess, RepairResultSave)) and command.output_file_id is not None:
                files = tuple(dict.fromkeys((*files, command.output_file_id)))
            held = PendingMediaRequest(stage, deepcopy(command), new_operation_key(), None, files)
            self.pending[identity] = held
        elif (type(command) is not type(held.command)
                or not json_equal(_media_request_values(command), _media_request_values(held.command))):
            raise ConsistencyError("尚未保存的原媒体申请不能由后续申请替换")
        held = await self._resolve_owner(held)
        actual = held.command
        if isinstance(actual, CheckResultSave) and (actual.media.error is not None or actual.media.issues):
            tool = None if actual.source_probe is None else actual.source_probe.tool_diagnostics
            if tool is None or not tool.control_only or actual.media.issues:
                record_error(actual.media.error or actual.media.issues[0], stage="capture.media_check",
                    context={"action_id": held.action_id, "processing_id": actual.processing_id},
                    details={"issues": actual.media.issues, "source_probe": actual.source_probe},
                    state=held.diagnostic)
        elif isinstance(actual, RepairResultSave) and actual.error is not None:
            tool = (actual.failure_probe.tool_diagnostics if actual.failure_probe is not None else
                    actual.artifact.tool_diagnostics if actual.artifact is not None else None)
            artifact = actual.artifact
            control = tool is not None and tool.control_only
            # 原输入探测已由 CHECK 或 START 消费；这里仅从保存事实派生修复失败。
            # 缺少新工具事实的旧历史仍保留原消费行为。
            source_failure = (artifact is None and actual.failure_probe is not None
                and actual.failure_probe.error is not None and tool is not None)
            if artifact is not None:
                control |= artifact.postprocessing_stopped
                if (artifact.observation.error is not None
                        or artifact.checksum is not None and artifact.checksum.error is not None
                        or artifact.synchronization is not None and artifact.synchronization.error is not None):
                    control = False
            if not control and not source_failure:
                record_error(actual.error, stage="capture.media_repair",
                    context={"action_id": held.action_id, "processing_id": actual.processing_id},
                    details={"artifact": artifact, "failure_probe": actual.failure_probe},
                    state=held.diagnostic)
        elif isinstance(actual, RepairStart) and actual.source_probe is not None and actual.source_probe.error is not None:
            probe = actual.source_probe
            if probe.tool_diagnostics is None or not probe.tool_diagnostics.control_only:
                record_error(probe.error, stage="capture.media_repair_probe",
                    context={"action_id": held.action_id, "processing_id": actual.processing_id},
                    details={"source_probe": probe}, state=held.diagnostic)
        try:
            outcome = await self.database.write(
                lambda owned: getattr(self.repository, stage.value)(held.command, held.key, owned),
                key=held.key, description="原媒体申请保存")
        except BaseException as error:
            outcome = db_outcome_for(error)
            if outcome is not None:
                self._apply_receipt(identity, held, self._commit(outcome))
            raise
        receipt = self._commit(outcome)
        self._apply_receipt(identity, held, receipt)
        return receipt

    async def _resolve_owner(self, held):
        if held.action_id is None:
            try:
                owner = await _read_one(self.database,
                    "SELECT action_id FROM recording_processing WHERE id=?", (held.command.processing_id,))
                if owner is None:
                    raise ConsistencyError("原媒体申请的处理拥有者不存在")
            except BaseException as error:
                self.pending[(held.stage, held.command.processing_id)] = replace(held, save_error=error)
                raise
            held = replace(held, action_id=owner[0])
            self.pending[(held.stage, held.command.processing_id)] = held
        return held

    def _apply_receipt(self, identity, held, receipt):
        if receipt.disposition is SaveDisposition.SAVED:
            del self.pending[identity]
            if held.stage is MediaSaveStage.START and isinstance(receipt.value, RepairOutputFile):
                self.file_ids = tuple(dict.fromkeys((*self.file_ids, receipt.value.file_id)))
        else:
            self.pending[identity] = replace(held, save_error=receipt.error)


async def resume_media_saves(database, pending, executor=None, *, action_id=None):
    """只核实已经取得的原申请；不启动工具、读取或重算决定。"""
    saves = CaptureProcessingSaves(database, pending)
    owner = executor if executor is not None else FileTaskExecutor(Supervisor())
    for held in tuple(pending.values()):
        held = await saves._resolve_owner(held)
        if action_id is not None and held.action_id != action_id:
            continue

        async def save_original(_control=None):
            receipt = await saves._save(held.stage, held.command)
            if receipt.disposition is not SaveDisposition.SAVED:
                raise db_failure(ConsistencyError, f"原媒体结果未可靠保存: {receipt.error}", receipt)
            return receipt

        if held.file_ids:
            await owner.run_owned_async_file_task(AsyncFileTask(
                FileTaskId(f"media_save/{held.key}"), held.file_ids, "media_save",
                "原媒体申请与原操作键核实", save_original, resources=("state_db",)))
        else:
            await save_original()


def load_processing_status(owned: OwnedConnection, processing_id: int) -> ProcessingStatus:
    """从当前投影装载编排事实；修复执行中的登记输出按归属查询。"""
    with closing(owned.connection.execute(
        "SELECT check_decision, check_state, media_json, check_basis_json,"
        " repair_state, repair_output_file_id, action_id"
        " FROM recording_processing WHERE id=?", (processing_id,)
    )) as cursor:
        row = cursor.fetchone()
    if row is None:
        raise ConsistencyError(f"录像处理记录不存在: {processing_id}")
    (check_decision, check_state, media_raw, basis_raw, repair_state,
     output_id, action_id) = row
    media = parse_exact_json(media_raw)
    duration = (
        saved_check_duration(media)
        if check_state == 3 else None)
    target = saved_target_duration_ms(parse_exact_json(basis_raw))
    if output_id is None and repair_state == 4:
        with closing(owned.connection.execute(
            "SELECT id FROM intermediate_files"
            " WHERE owner_action_id=? AND purpose=4 ORDER BY id LIMIT 2", (action_id,)
        )) as cursor:
            registered = cursor.fetchall()
        if not registered:
            output_id = None
        elif len(registered) > 1:
            raise ConsistencyError(
                f"动作 {action_id} 登记了多个修复输出文件")
        else:
            output_id = registered[0][0]
    path = None
    if output_id is not None:
        with closing(owned.connection.execute(
            "SELECT relative_path FROM intermediate_files WHERE id=?",
            (output_id,)
        )) as cursor:
            path = cursor.fetchone()[0]
    return ProcessingStatus(
        processing_id=processing_id,
        check_decision=check_decision,
        check_state=check_state,
        check_duration_s=duration,
        target_duration_ms=target,
        repair_state=repair_state,
        repair_output_file_id=output_id,
        repair_output_path=path,
        source_probe=saved_source_probe(owned.connection, processing_id),
        check_issues=tuple(ProcessingError(item["code"], item["stage"], item["details"])
            for item in media.get("issues", ())),
    )



@dataclass
class MediaFlow:
    """媒体链调用方的端口集合；工具与摘要读取由装配层注入。

    digest_supported 是读取绑定的驱动摘要能力声明；首次媒体处理
    前把来源文件的摘要能力从未判定一次固定。digest_for 按源设备
    文件构造源端摘要读取端口，装配层持有驱动端口时提供；固定的
    digest 端口仍适用于单一源的简单装配。

    retry_interval_s 是本设备文件读取的重试间隔
    （configuration.md#通信重试间隔），读取失败后按它在会话单调钟
    上等待再次读取；retry_gate 由装配层会话共享。
    """

    database: BusinessDatabase
    roots: BoundDirectories
    sessions: DriverReadSessions
    tools: MediaTools
    policy: MediaPolicy
    occurred_at: Callable[[], int]
    digest_supported: bool
    digest: Any = None
    digest_for: Callable[[int], Awaitable[SourceDigestReader | None]] | None = None
    repair_extension: str | None = None
    retry_interval_s: Decimal = Decimal("3")
    monotonic_ns: Callable[[], int] = _default_monotonic_ns
    retry_gate: RetryWaitGate = field(default_factory=RetryWaitGate)
    max_read_attempts: int = _READ_MAX_ATTEMPTS
    read_idle_timeout_s: Decimal = _READ_TIMEOUT_S
    max_recopies: int = 1
    segment_size: int = _SEGMENT_SIZE
    evidence: Any = field(default_factory=lambda: EvidenceRegistry((
        EvidenceContract("read_returned", 1, "read", frozenset()),)))
    operations: OperationRepository = field(default_factory=OperationRepository)
    recovery_boundary: RecoveryBoundary = RecoveryBoundary.UNCONFIRMED
    recovery_max_event_id: int | None = None
    recovery_evidence_for: Callable | None = None
    on_recovery_diagnostic: Callable[[RecoveryDiagnostic], None] | None = None
    last_recovery_diagnostic: RecoveryDiagnostic | None = None
    continuing_read_tickets: dict = field(default_factory=dict)
    pending_read_results: dict = field(default_factory=dict)
    file_executor: FileTaskExecutor | None = None
    pending_read_business: dict = field(default_factory=dict)
    pending_read_ends: dict = field(default_factory=dict)
    pending_media_results: dict = field(default_factory=dict)
    pending_segment_inspection_results: dict = field(default_factory=dict)
    read_discovery_batch_size: int | None = None
    capture_selection: Callable[[int], Any] | None = None

    def saves(self, *, file_ids=()) -> CaptureProcessingSaves:
        return CaptureProcessingSaves(self.database, self.pending_media_results, file_ids=file_ids)

    def copies(self) -> RecordingInputCopies:
        return RecordingInputCopies(
            self.database, self.roots, self.occurred_at, max_recopies=self.max_recopies,
            file_executor=self.file_executor, read_discovery_batch_size=self.read_discovery_batch_size)


async def _ensure_checksum_support(
    flow: MediaFlow, source_device_file_id: int) -> None:
    """按绑定声明固定来源文件的摘要能力；已决定的能力不重复声明。"""
    row = await _read_one(flow.database,
        "SELECT checksum_support FROM device_files WHERE id = ?", (source_device_file_id,))
    if row is None:
        raise ConsistencyError(f"设备文件不存在: {source_device_file_id}")
    if row[0] != 1:
        return
    request = FileChecksumSave(file_id=source_device_file_id,
        support=2 if flow.digest_supported else 3, occurred_at=flow.occurred_at())
    from camctl.outputs.read_attempts import save_read_business
    await save_read_business(flow, source_device_file_id, "source_checksum_support",
        request, CaptureRepository().save_file_checksum)


async def run_recording_media(
    flow: MediaFlow, action_id: int, processing_id: int,
    source_device_file_id: int, *, target_extension: str | None = "mp4",
) -> InputStep | CheckStep | RepairStep:
    """串联输入取得、检查执行与修复执行的一次推进。

    输入未就绪、检查未终态或修复不待执行时返回对应步骤；保存被
    拒或未知的分区原样透传，由下一次推进按已保存事实续跑。

    原读取明确失败保存了重试等待时，按本次配置等待；原 RUNNING
    尝试沿原身份恢复，重拷只登记轮次，不增加读取次数。
    """
    from camctl.outputs.read_attempts import retry_read_business, retry_read_ends

    await resume_media_saves(flow.database, flow.pending_media_results, flow.file_executor, action_id=action_id)
    await retry_read_business(flow)
    for pending in tuple(flow.pending_read_results.values()):
        await _save_internal_read_and_settle(flow, pending, retry=True)
    await retry_read_ends(flow)
    original_failure = await _read_all(flow.database, "SELECT c.id FROM file_copies c JOIN operation_runs r ON r.copy_id=c.id"
        " WHERE c.processing_id=? AND r.kind=3 AND r.status=4 LIMIT 2", (processing_id,))
    if len(original_failure) > 1:
        raise ConsistencyError("同一内部处理有多个终态失败读取")
    if original_failure:
        copy_id = original_failure[0][0]
        await _fail_internal_read_input(flow, copy_id=copy_id)
        return InputStep(InputPhase.WAITING, copy_id=copy_id, reason="run_ended")
    await _ensure_checksum_support(flow, source_device_file_id)
    status = await flow.database.read(
        lambda owned: load_processing_status(owned, processing_id), description="录像处理状态")
    digest = (await flow.digest_for(source_device_file_id)
              if flow.digest_for is not None else flow.digest)
    copies = flow.copies()
    config = OperationConfig(flow.max_read_attempts, flow.read_idle_timeout_s,
                             flow.retry_interval_s)
    qualified = await copies.qualify(FileCandidate(
        action_id, None, processing_id, None, source_device_file_id,
        target_extension, None, None, config, flow.occurred_at()))
    if qualified.disposition is not SaveDisposition.SAVED:
        return InputStep(InputPhase.QUALIFY_UNKNOWN if qualified.disposition is SaveDisposition.UNKNOWN
                         else InputPhase.QUALIFY_REJECTED, error=qualified.error)
    qualification = qualified.value
    if qualification.outcome is not QualificationOutcome.GRANTED:
        return InputStep(InputPhase.REJECTED_FINAL if qualification.outcome is QualificationOutcome.REJECTED_FINAL
                         else InputPhase.WAITING, reason=qualification.reason)
    copy_id = qualification.copy_id
    state = await copies.copy_state(copy_id)
    ticket = None
    wait_key = f"read/{copy_id}"
    if state.verification_state not in (3, 5) or state.target_sha256 is None:
        run = await _read_one(flow.database, "SELECT attempts_used,retry_wait_required FROM operation_runs WHERE copy_id=?", (copy_id,))
        if run is None:
            raise ConsistencyError("内部输入缺少原读取流程")
        if flow.retry_gate.remaining(wait_key, attempts_used=run[0], retry_wait_required=run[1] == 1,
                max_attempts_used=flow.max_read_attempts, interval_s=flow.retry_interval_s,
                now_ns=flow.monotonic_ns()) is not None:
            return InputStep(InputPhase.RETRY_WAITING, copy_id=copy_id)
        current = flow.occurred_at()
        selection = None if flow.capture_selection is None else flow.capture_selection(current)
        request = SlotRequest(copy_id, current, capture_selection=selection)
        from camctl.outputs.read_attempts import save_read_business
        slot = await save_read_business(flow, copy_id, "grant_read_slot", request,
            copies.repository.grant_read_slot)
        if slot.value.outcome in (SlotOutcome.WAIT, SlotOutcome.FINISHED):
            return InputStep(InputPhase.WAITING, copy_id=copy_id)
        current = flow.occurred_at()
        selection = None if flow.capture_selection is None else flow.capture_selection(current)
        ticket, reason = await acquire_read_attempt(flow, AttemptIntent(
            "read", action_id, OperationKind.READ_FILE, AttemptTarget(copy_id=copy_id), None,
            AttemptConfig(flow.max_read_attempts, flow.read_idle_timeout_s, flow.retry_interval_s),
            current, copy_round=state.round, capture_selection=selection))
        if ticket is None:
            if reason == "budget_exhausted":
                await _fail_internal_read_input(flow, copy_id=copy_id)
            return InputStep(InputPhase.WAITING, copy_id=copy_id, reason=reason)
    from camctl.outputs.read_attempts import hold_read_end

    sessions = flow.sessions if ticket is None else flow.sessions.for_attempt(
        ticket, idle_timeout_s=flow.read_idle_timeout_s)
    context = InputContext(
        action_id=action_id,
        processing_id=processing_id,
        source_device_file_id=source_device_file_id,
        target_extension=target_extension,
        config=config,
        segment_size=flow.segment_size,
        staging=flow.roots.staging,
        copies=copies,
        sessions=sessions,
        occurred_at=flow.occurred_at(),
        digest=digest,
        read_end=(flow.pending_read_ends[copy_id].end if copy_id in flow.pending_read_ends else None),
        on_read_end=(None if ticket is None else
            lambda end, complete: hold_read_end(flow, ticket, end, complete,
                resume=lambda current, held: _resume_local_internal_end(current, held, flow, context),
                evidence=flow.evidence)),
        on_read_start=lambda: _read_dispatched(flow, copy_id),
    )

    async def read_and_save(control):
        current = replace(context, sessions=(sessions if ticket is None else
            sessions.for_attempt(ticket, idle_timeout_s=flow.read_idle_timeout_s, control=control)))
        step = await obtain_recording_input(current)
        if ticket is not None:
            await _finish_internal_read(flow, ticket, step, diagnostic_context={
                "action_id": action_id, "processing_id": processing_id,
                "source_device_file_id": source_device_file_id})
        return step

    input_step = await run_owned_read(flow, copy_id, read_and_save) if ticket is not None else await read_and_save(None)
    if input_step.phase is InputPhase.SEGMENT_FAILED and input_step.source_failed:
        # 读取通信失败：登记锚点，间隔内不再次读取。
        flow.retry_gate.established(wait_key, flow.monotonic_ns())
    elif input_step.phase in (InputPhase.INPUT_READY,
                              InputPhase.RECOPY_PENDING):
        flow.retry_gate.cleared(wait_key)
    if input_step.input_file is None:
        return input_step
    check = await execute_check(CheckContext(
        processing=status,
        input_file=input_step.input_file,
        policy=flow.policy,
        tools=flow.tools,
        saves=flow.saves(file_ids=(input_step.input_file.file_id,)),
        occurred_at=flow.occurred_at(),
        executor=flow.file_executor,
    ))
    require_saved_media_result(check)
    if check.phase not in (CheckExecutionPhase.CHECK_COMPLETED,
            CheckExecutionPhase.NOT_REQUIRED, CheckExecutionPhase.ALREADY_FINISHED,
            CheckExecutionPhase.FINISHED_DECISION_SAVED):
        return check
    # 检查完成或检查不适用：修复决定待执行才继续（计时判定的异常
    # 多录不经检查直接修复），无需修复时到此为止。
    status = await flow.database.read(
        lambda owned: load_processing_status(owned, processing_id), description="录像处理状态")
    if status.repair_state not in (3, 4):
        return check
    repair_files = (input_step.input_file.file_id,)
    if status.repair_output_file_id is not None:
        repair_files = tuple(dict.fromkeys((*repair_files, status.repair_output_file_id)))
    repair = await execute_repair(RepairContext(
        processing=status,
        input_file=input_step.input_file,
        # OSV 由修复入口固定独立 MP4；其他来源继续原扩展名规则。
        extension=(flow.repair_extension
                   if flow.repair_extension is not None
                   else target_extension),
        tools=flow.tools,
        saves=flow.saves(file_ids=repair_files),
        occurred_at=flow.occurred_at(),
        executor=flow.file_executor,
    ))
    return require_saved_media_result(repair)


async def _resume_local_internal_end(current, held, original_flow, original_context):
    """原完整 End 的本地校验与原结果保存；不重开源或推进媒体处理。"""
    from camctl.capture.input_copy import _complete_input
    from camctl.outputs.read_attempts import run_owned_read

    flow = replace(original_flow, database=current.database)
    copies = flow.copies()
    context = replace(original_context, copies=copies, digest=None, read_end=held.end)
    copy_id = int(held.ticket.target_id)

    async def complete_and_save(_control):
        step = replace(await _complete_input(context, copy_id, await copies.copy_state(copy_id)),
                       read_end=held.end, stop_requested=True, content_complete=True)
        await _finish_internal_read(flow, held.ticket, step, diagnostic_context={
            "action_id": context.action_id, "processing_id": context.processing_id,
            "inspection_id": context.inspection_id,
            "source_device_file_id": context.source_device_file_id})

    await run_owned_read(flow, copy_id, complete_and_save)


def _read_dispatched(flow, copy_id):
    flow.pending_read_ends.pop(copy_id, None)
    from camctl.outputs.read_attempts import ReadContinuation
    previous = flow.continuing_read_tickets.get(copy_id)
    if previous is not None:
        flow.continuing_read_tickets[copy_id] = ReadContinuation(previous.ticket, False)


async def _finish_internal_read(flow: MediaFlow, ticket, step: InputStep, *,
                          max_attempts: int | None = None, diagnostic_context=None) -> None:
    """连接已经关闭后保存读取结果；重拷继续原尝试。"""
    await settle_required(_finish_internal_read_result(flow, ticket, step,
        max_attempts=max_attempts, diagnostic_context=diagnostic_context))


async def _finish_internal_read_result(flow, ticket, step, *, max_attempts, diagnostic_context=None):
    from camctl.outputs.read_attempts import require_preparation_settled

    if step.phase is InputPhase.READ_NOT_STARTED:
        from camctl.outputs.read_attempts import prepare_prevented_read_result
        pending = await prepare_prevented_read_result(flow, ticket, step.dispatch_prevented, flow.evidence)
        if pending is not None:
            await _save_internal_read_and_settle(flow, pending)
        return

    if not require_preparation_settled(step):
        return
    if step.phase is InputPhase.RECOPY_PENDING:
        from camctl.outputs.read_attempts import ReadContinuation
        flow.continuing_read_tickets[step.copy_id] = ReadContinuation(ticket, False, step, flow.occurred_at(), evidence=flow.evidence)
        return
    from camctl.outputs.read_attempts import stopped_read_result

    stopped = stopped_read_result(flow, ticket, step, flow.evidence, flow.occurred_at())
    if stopped is not None:
        await _save_internal_read_and_settle(flow, stopped)
        return
    from camctl.outputs.read_attempts import canceled_complete_read_result

    completed_cancel = await canceled_complete_read_result(flow, ticket, flow.evidence)
    if completed_cancel is not None:
        await _save_internal_read_and_settle(flow, completed_cancel)
        return
    from camctl.outputs.read_attempts import source_verification_run_finish

    verification_failed = step.phase is InputPhase.VERIFICATION_FAILED
    verification_finish = (await source_verification_run_finish(
        flow, ticket, step)
        if verification_failed else None)
    checksum_failed = step.phase is InputPhase.CHECKSUM_EXHAUSTED
    if (step.phase is not InputPhase.INPUT_READY and not step.source_failed
            and not checksum_failed and not verification_failed):
        raise ConsistencyError(f"内部读取的本地或保存前提未完成，保留原尝试: {step.phase.value}: {step.error}") from step.error
    succeeded = (step.phase is InputPhase.INPUT_READY
                 or checksum_failed or verification_failed)
    # 原 ticket 的连续编号已由意图／恢复事务核实，实际返回后先持有结果。
    exhausted = ticket.attempt_id >= (max_attempts or flow.max_read_attempts)
    outcome = CallOutcome(status=AttemptStatus.SUCCEEDED if succeeded else AttemptStatus.FAILED,
        effect=EffectState.UNKNOWN,
        error=None if succeeded else ErrorValue("device_error", "read", read_failure_details(step)),
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("read_returned", 1, {})))
    if verification_failed:
        run_finish = verification_finish
    elif checksum_failed:
        run_finish = RunFinish(RunOutcome.FAILED, ErrorValue("checksum_mismatch", "source_read", {
            "max_recopies": step.error.max_recopies, "recopies_used": step.error.recopies_used}))
    elif succeeded:
        run_finish = RunFinish(RunOutcome.SUCCEEDED)
    elif exhausted:
        run_finish = RunFinish(RunOutcome.FAILED, ErrorValue("read_attempts_exhausted", "source_read"))
    else:
        run_finish = None
    pending = hold_read_result(flow, AttemptFinish(
        ticket, validate_outcome(ticket, outcome, flow.evidence), flow.occurred_at(),
        retry_wait=not succeeded and not exhausted, run_finish=run_finish),
        context=diagnostic_context,
        diagnostic_error=step.error if checksum_failed or verification_failed else None,
        diagnostic_stage=step.phase.value)
    await _save_internal_read_and_settle(flow, pending)


async def resume_prepared_internal_reads(
    database: BusinessDatabase, *, pending_read_results: dict,
    pending_read_business: dict, pending_read_ends: dict,
    continuing_read_tickets: dict,
) -> None:
    """默认入口只核实原完整 READ 申请，不取得新设备或媒体执行资格。"""
    prepared = tuple(pending for pending in pending_read_results.values()
                     if isinstance(pending, (PendingReadResult, PendingStoppedRead)))
    if not prepared and not pending_read_business:
        return
    scope = SimpleNamespace(
        database=database, operations=OperationRepository(),
        pending_read_results=pending_read_results, pending_read_business=pending_read_business,
        pending_read_ends=pending_read_ends, continuing_read_tickets=continuing_read_tickets)
    for pending in prepared:
        # 完整输入首次确定的原时刻承担其独立收场，重送不读取当前墙钟。
        original_time = (pending.occurred_at if isinstance(pending, PendingStoppedRead)
                         else pending.finish.occurred_at)
        scope.occurred_at = lambda original=original_time: original
        await _save_internal_read_and_settle(scope, pending, retry=True)
    await retry_read_business(scope)


async def resume_binding_internal_read_ends(
    database: BusinessDatabase, *, pending_read_results: dict,
    pending_read_business: dict, pending_read_ends: dict,
    continuing_read_tickets: dict, binding_check: Callable, occurred_at: Callable,
) -> None:
    """普通与残留入口保存已实际读完、却缺必要源摘要的绑定失败。"""
    from camctl.capture.media import RecordingFailure
    from camctl.contracts.enums import enum_for
    from camctl.devices.bindings import DeviceBinding, binding_failure_details
    from camctl.outputs.copy import SourceChecksumSupport
    from camctl.outputs.read_attempts import PendingReadBusiness, held_binding_read_result
    from camctl.persistence.repositories.capture import FinishBindingFailure

    # 原完整申请的核实由 prepared 前置负责；失败传播后不会到达本分类。
    if pending_read_results or pending_read_business:
        return
    action_status = enum_for("actions.status")
    run_status = enum_for("operation_runs.status")
    run_kind = enum_for("operation_runs.kind")
    query_purpose = enum_for("operation_runs.query_purpose")
    scope = SimpleNamespace(
        database=database, operations=OperationRepository(),
        pending_read_results=pending_read_results, pending_read_business=pending_read_business,
        pending_read_ends=pending_read_ends, continuing_read_tickets=continuing_read_tickets)
    for copy_id, held in tuple(pending_read_ends.items()):
        row = await _read_one(database, "SELECT a.id,a.device_id,a.driver_id,a.status,a.cancel_requested,r.id,r.status,"
            "r.kind,c.delivery_id,f.source_action_id,ob.device_id,ob.driver_id FROM file_copies c"
            " JOIN recording_processing p ON p.id=c.processing_id"
            " JOIN actions a ON a.id=p.action_id JOIN operation_runs r ON r.copy_id=c.id"
            " JOIN device_files f ON f.id=c.source_device_file_id"
            " JOIN actions ob ON ob.id=f.observer_action_id WHERE c.id=?", (copy_id,))
        if row is None:
            raise ConsistencyError("原内部读取结束缺少处理、源观察者与动作归属")
        (action_id, device_id, driver_id, status, canceled, run_id, read_status,
         kind, delivery_id, source_action_id, observer_device_id, observer_driver_id) = row
        # 不给取消、已有终态或其他原责任建立新的业务决定。
        if status != int(action_status.RUNNING) or canceled or read_status != int(run_status.ACTIVE):
            continue
        if (run_id != held.ticket.run_id or kind != int(run_kind.READ_FILE)
                or delivery_id is not None or source_action_id != action_id
                or (observer_device_id, observer_driver_id) != (device_id, driver_id)):
            raise ConsistencyError("原内部读取结束与拍摄处理责任不符")
        facts = await database.read(
            lambda owned: OutputsRepository().load_copy_state(copy_id, owned),
            description="原内部读取的完整副本事实")
        if (facts.committed_bytes != facts.source_size
                or facts.source_support is not SourceChecksumSupport.SUPPORTED
                or facts.source_sha256 is not None):
            continue
        unfinished = await _read_one(database, "SELECT 1 FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
            " WHERE r.action_id=? AND r.id!=? AND (t.status=1 OR t.result_json IS NULL) LIMIT 1", (action_id, run_id))
        if unfinished is not None:
            continue
        binding = binding_check(DeviceBinding(device_id, driver_id))
        details = binding_failure_details(binding)
        if details is None:
            continue
        responsibilities = tuple(row[0] for row in await _read_all(database,
            "SELECT responsibility_key FROM operation_runs WHERE action_id=? AND id!=?"
            " AND kind IN (?,?,?,?,?) AND status IN (?,?)"
            " AND (kind!=? OR query_purpose!=?) ORDER BY id",
            (action_id, run_id, int(run_kind.START), int(run_kind.STOP), int(run_kind.READ_FILE),
             int(run_kind.QUERY_ACTIVITY), int(run_kind.CHECK_CAPTURE_RESULTS),
             int(run_status.PENDING), int(run_status.ACTIVE), int(run_kind.QUERY_ACTIVITY),
             int(query_purpose.RESIDUAL_STOP_CONFIRMATION))))
        formed_at = occurred_at()
        scope.occurred_at = lambda original=formed_at: original
        # 未取消的收场不会建立新的 STOP/RESULTS，故不取得其新配置或设备资格。
        request = FinishBindingFailure(action_id, formed_at,
            RecordingFailure("device_binding_unavailable", details), responsibility_keys=responsibilities)
        business = PendingReadBusiness(CaptureRepository().finish_binding_failure, request, new_operation_key())
        pending = await held_binding_read_result(scope, copy_id, binding, business)
        if pending is None:
            raise ConsistencyError("已分类的必要摘要绑定失败缺少原完整读取结果")
        await _save_internal_read_and_settle(scope, pending)


async def _save_internal_read_and_settle(flow: MediaFlow, pending, *, retry=False) -> None:
    if retry:
        pending.settlement.retry_failed()
        pending.result_save.retry_failed()
    await pending.settlement.run(lambda: _save_internal_read_result(flow, pending, retry=retry))


async def _save_internal_read_result(flow, pending, *, retry=False):
    pending = await save_read_result(flow, pending)
    if pending is None:
        return
    if pending.preserved_run_terminal:
        forget_read_result(flow, pending)
        return
    if pending.business is not None:
        from camctl.outputs.read_attempts import hold_prepared_read_business, save_prepared_read_business

        hold_prepared_read_business(flow, pending.business.request.action_id, "capture_binding_failure", pending.business)
        forget_read_result(flow, pending)
        await save_prepared_read_business(flow, pending.business.request.action_id, "capture_binding_failure", pending.business)
        return
    ticket = pending.finish.ticket
    succeeded = pending.finish.outcome.outcome.status is AttemptStatus.SUCCEEDED
    exhausted = (pending.finish.run_finish is not None
                 and pending.finish.run_finish.status is RunOutcome.FAILED)
    row = await _read_one(flow.database, "SELECT processing_id,inspection_id FROM file_copies WHERE id=?", (int(ticket.target_id),))
    if row is None:
        raise ConsistencyError("已结束读取尝试缺少对应拷贝")
    notification_error = None
    if exhausted:
        if row[0] is not None:
            try:
                await _fail_internal_read_input(flow, copy_id=int(ticket.target_id),
                    business_saves=pending.business_saves, retry=retry)
            except BaseException as error:
                actual = db_outcome_for(error)
                if actual is None or actual.kind is not _DbOutcomeKind.COMPLETED:
                    raise
                notification_error = error
    if row[1] is not None:
        run_status = await _read_one(flow.database, "SELECT status FROM operation_runs WHERE copy_id=?", (int(ticket.target_id),))
        if run_status is None:
            raise ConsistencyError("检查读取缺少原流程状态")
        from camctl.contracts.enums import enum_for

        if run_status[0] == int(enum_for("operation_runs.status").FAILED):
            from camctl.capture.segment_inspections import (
                settle_inspection_read_failure, settle_terminal_inspection_read_slot,
            )

            copy_id = int(ticket.target_id)
            if not await settle_inspection_read_failure(flow, copy_id, pending.finish.occurred_at):
                return
            # 读取尝试先可靠保存，检查结果再可靠保存；只有这两项都完成后
            # 才可解除检查读取机会。结果未知时保留同一 AttemptFinish 与结果键。
            forget_read_result(flow, pending)
            await settle_terminal_inspection_read_slot(flow, copy_id, pending.finish.occurred_at)
            return
    if succeeded and not exhausted:
        from camctl.outputs.read_attempts import save_read_business

        # 原尝试已经可靠保存；后续独立释放由原完整申请和键承担。
        # 不继续持有 Finish，以免该释放重送后再次生成另一申请。
        forget_read_result(flow, pending)
        await save_read_business(flow, int(ticket.target_id), "release_read_slot",
            SlotRequest(int(ticket.target_id), pending.finish.occurred_at), OutputsRepository().release_read_slot)
        return
    forget_read_result(flow, pending)
    if notification_error is not None:
        raise notification_error


async def _fail_internal_read_input(flow: MediaFlow, *, copy_id: int,
                                    business_saves=None, retry=False) -> None:
    """原读取可靠耗尽后结束所需检查或修复，再释放原读取机会。"""
    from camctl.capture.processing import (
        CheckPhase, CheckResultSave, MediaObservation, ProcessingError, RepairBasis,
        RepairDecisionChoice, RepairDecisionSave, RepairOutcome, RepairReason, RepairResultSave,
    )

    row = await _read_one(flow.database, "SELECT c.processing_id,r.attempts_used,r.max_attempts_used,r.status FROM file_copies c"
        " JOIN operation_runs r ON r.copy_id=c.id WHERE c.id=?", (copy_id,))
    if row is None or row[0] is None or row[3] != 4:
        raise ConsistencyError("内部输入失败必须对应已可靠失败的原读取流程")
    processing_id, used, maximum, _status = row
    unfinished = await _read_one(flow.database, "SELECT 1 FROM operation_attempts WHERE run_id=(SELECT id FROM operation_runs WHERE copy_id=?)"
        " AND (status=1 OR result_json IS NULL) LIMIT 1", (copy_id,))
    if unfinished is not None:
        raise ConsistencyError("原内部读取尚有未结束调用，不能收场检查或释放保护")
    error_raw = (await _read_one(flow.database, "SELECT error_json FROM operation_runs WHERE copy_id=?", (copy_id,)))[0]
    original_error = parse_exact_json(error_raw)
    status = await flow.database.read(
        lambda owned: load_processing_status(owned, processing_id), description="录像处理状态")
    if original_error["code"] == "checksum_mismatch":
        failure = ProcessingError("checksum_mismatch", "source_read", original_error["details"])
    elif original_error["code"] == "read_attempts_exhausted":
        failure = ProcessingError("read_attempts_exhausted", "source_read",
                                  {"attempts_used": used, "max_read_attempts": maximum})
    elif original_error["code"] == "source_file_unconfirmed":
        failure = ProcessingError("source_file_unconfirmed", "source_read",
                                  original_error["details"])
    else:
        raise ConsistencyError("原内部读取失败的所属错误不可解释")
    repository = CaptureRepository()
    requests = []
    if status.check_decision == 3 and status.check_state in (1, 2):
        requests.append((repository.save_check_result, CheckResultSave(
            processing_id, MediaObservation(CheckPhase.FAILED, error=failure), flow.occurred_at())))
    if status.repair_state == 1:
        requests.append((repository.save_repair_decision, RepairDecisionSave(
            processing_id, RepairDecisionChoice.NOT_NEEDED,
            RepairBasis(RepairReason.NO_USABLE_INPUT, status.target_duration_ms), flow.occurred_at())))
    elif status.repair_state in (3, 4):
        requests.append((repository.save_repair_result, RepairResultSave(
            processing_id, RepairOutcome.FAILED, flow.occurred_at(), error=failure)))
    from camctl.outputs.read_attempts import save_read_business

    held = [] if business_saves is None else [
        pending for (identity, operation), pending in business_saves.items()
        if identity == copy_id and operation != "release_read_slot"]
    held_operations = {pending.save.__name__ for pending in held}
    requests = [(pending.save, pending.request) for pending in held] + [
        (save, request) for save, request in requests if save.__name__ not in held_operations]
    notification_error = None
    async def save_stage(operation, request, save):
        nonlocal notification_error
        try:
            await save_read_business(flow, copy_id, operation, request, save,
                business_saves=business_saves, retry=retry)
        except BaseException as error:
            actual = db_outcome_for(error)
            if actual is None or actual.kind is not _DbOutcomeKind.COMPLETED:
                raise
            if notification_error is None:
                notification_error = error

    for save, request in requests:
        await save_stage(save.__name__, request, save)
    if ((business_saves is not None and (copy_id, "release_read_slot") in business_saves)
            or (await _read_one(flow.database, "SELECT slot_device_id FROM file_copies WHERE id=?", (copy_id,)))[0] is not None):
        await save_stage("release_read_slot", SlotRequest(copy_id, flow.occurred_at()),
                         OutputsRepository().release_read_slot)
    if notification_error is not None:
        raise notification_error

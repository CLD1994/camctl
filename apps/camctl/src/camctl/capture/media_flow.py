"""录像媒体链调用方：D4 读取会话绑定与检查修复组合。

把内部输入取得、检查与修复编排接到真实仓储和设备读取端口：
DriverReadSessions 按设备文件的完成事实经驱动 open_read 打开可停
止读取会话；RecordingInputCopies 组合资格、续传与完整性事务；
CaptureProcessingSaves 适配处理事务。run_recording_media 串联输入
取得、检查执行与修复执行，各失败分区原样透传，不在本层重试。
"""

from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import Enum
from pathlib import Path
from time import monotonic_ns as _default_monotonic_ns
from types import SimpleNamespace
from typing import Any, Callable

from camctl.capture.files import FileChecksumSave
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
    CheckResultSave, RepairDecisionSave, RepairOutputFile, RepairResultSave,
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
    complete_copy,
    copy_next_segment,
    prepare_copy,
)
from camctl.outputs.qualification import FileCandidate, OperationConfig, QualificationOutcome
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
    PendingReadResult, acquire_read_attempt, forget_read_result, hold_read_result,
    retry_read_business, run_owned_read, save_read_result,
)
from camctl.outputs.slots import SlotOutcome, SlotRequest
from camctl.persistence.models import DbOutcomeKind as _DbOutcomeKind
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
        "SELECT identity_key, locator_json, size_bytes, completion_state"
        " FROM device_files WHERE id = ?", (file_id,)
    )) as cursor:
        row = cursor.fetchone()
    if row is None:
        raise ConsistencyError(f"设备文件不存在: {file_id}")
    identity_key, locator_raw, size_bytes, completion = row
    if completion != 3 or size_bytes is None:
        raise ConsistencyError(
            f"设备文件尚未确认写完，不能读取: {file_id}")
    return SourceFile(
        identity_key, parse_exact_json(locator_raw), int(size_bytes))


class DriverReadSessions:
    """D4 读取会话绑定：按设备文件完成事实经驱动打开可停止会话。

    源身份使用全库唯一的身份键与驱动定位结构；未完成或缺少完整
    大小的文件不可读。读取尝试票据由装配层按实际尝试提供。
    """

    def __init__(self, owned: OwnedConnection, driver: ReadDriver,
                 ticket: Any, *, idle_timeout_s: Decimal = _READ_TIMEOUT_S) -> None:
        self._owned = owned
        self._driver = driver
        self._ticket = ticket
        self.idle_timeout_s = idle_timeout_s

    def for_attempt(self, ticket, *, idle_timeout_s: Decimal):
        """为已可靠保存的原尝试构造本次读取会话入口。"""
        return DriverReadSessions(self._owned, self._driver, ticket,
                                  idle_timeout_s=idle_timeout_s)

    async def open_session(self, source_device_file_id: int, offset: int):
        source = load_confirmed_source(self._owned, source_device_file_id)
        if self._ticket is None:
            raise ConsistencyError("设备读取会话缺少已保存的尝试票据")
        return await self._driver.open_read(source, offset, self._ticket,
                                           idle_timeout_s=self.idle_timeout_s)


class RecordingInputCopies:
    """内部输入拷贝端口适配：资格、状态、续传、分段与完整性事务。"""

    def __init__(self, owned: OwnedConnection, roots: BoundDirectories,
                 occurred_at: Callable[[], int], *,
                 max_recopies: int = 1,
                 file_executor: FileTaskExecutor | None = None) -> None:
        self.owned = owned
        self.roots = roots
        self.occurred_at = occurred_at
        self.max_recopies = max_recopies
        self.file_executor = file_executor
        self.repository = OutputsRepository()

    @staticmethod
    def _receipt(outcome) -> SaveReceipt:
        if outcome.kind is _DbOutcomeKind.COMPLETED:
            return SaveReceipt(SaveDisposition.SAVED, value=outcome.value)
        if outcome.kind is _DbOutcomeKind.ROLLED_BACK:
            return SaveReceipt(SaveDisposition.REJECTED, error=outcome.error)
        return SaveReceipt(SaveDisposition.UNKNOWN, error=outcome.error)

    def qualify(self, candidate: FileCandidate) -> SaveReceipt:
        return self._receipt(self.repository.grant_file(
            candidate, new_operation_key(), self.owned))

    def copy_state(self, copy_id: int):
        return self.repository.load_copy_state(copy_id, self.owned)

    async def prepare(self, copy_id: int):
        return await prepare_copy(copy_id, CopyContext(
            repository=self.repository, owned=self.owned, roots=self.roots,
            occurred_at=self.occurred_at(), executor=self.file_executor))

    async def transfer(self, copy_id: int, session, segment_size: int):
        return await copy_next_segment(copy_id, SegmentContext(
            repository=self.repository, owned=self.owned, roots=self.roots,
            occurred_at=self.occurred_at(), segment_size=segment_size,
            session=session, executor=self.file_executor))

    async def complete(self, copy_id: int, digest):
        return await complete_copy(copy_id, CompletionContext(
            repository=self.repository, owned=self.owned, roots=self.roots,
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
    """已取得但未可靠确认保存的完整原申请和本地文件责任。"""

    stage: MediaSaveStage
    command: CheckResultSave | RepairDecisionSave | RepairResultSave | RepairStart | RepairSuccess
    key: OperationKey
    action_id: int
    file_ids: tuple[int, ...]
    save_error: BaseException | None = None


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
    return values


class CaptureProcessingSaves:
    """媒体申请在首次写入前持有原输入与原键，确认保存才解除。"""

    def __init__(self, owned: OwnedConnection, pending=None, *, file_ids=()) -> None:
        self.owned = owned
        self.repository = CaptureRepository()
        self.pending = {} if pending is None else pending
        self.file_ids = file_ids

    @staticmethod
    def _commit(outcome) -> SaveReceipt:
        if outcome.kind is _DbOutcomeKind.COMPLETED:
            return SaveReceipt(SaveDisposition.SAVED, value=outcome.value)
        if outcome.kind is _DbOutcomeKind.ROLLED_BACK:
            return SaveReceipt(SaveDisposition.REJECTED, error=outcome.error)
        return SaveReceipt(SaveDisposition.UNKNOWN, error=outcome.error)

    def save_check_result(self, command):
        return self._save(MediaSaveStage.CHECK, command)

    def save_repair_decision(self, command):
        return self._save(MediaSaveStage.DECISION, command)

    def save_repair_result(self, command):
        return self._save(MediaSaveStage.REPAIR, command)

    def start_repair_output(self, command):
        receipt = self._save(MediaSaveStage.START, command)
        if receipt.disposition is SaveDisposition.SAVED and isinstance(receipt.value, RepairOutputFile):
            self.file_ids = tuple(dict.fromkeys((*self.file_ids, receipt.value.file_id)))
        return receipt

    def complete_repair_output(self, command):
        return self._save(MediaSaveStage.COMPLETE, command)

    def _save(self, stage, command):
        identity = (stage, command.processing_id)
        held = self.pending.get(identity)
        if held is None:
            with closing(self.owned.connection.execute(
                "SELECT action_id FROM recording_processing WHERE id=?", (command.processing_id,))) as cursor:
                owner = cursor.fetchone()
            if owner is None:
                raise ConsistencyError("原媒体申请的处理拥有者不存在")
            files = self.file_ids
            if isinstance(command, RepairSuccess):
                files = tuple(dict.fromkeys((*files, command.output_file_id)))
            held = PendingMediaRequest(stage, deepcopy(command), new_operation_key(), owner[0], files)
            self.pending[identity] = held
        elif (type(command) is not type(held.command)
                or not json_equal(_media_request_values(command), _media_request_values(held.command))):
            raise ConsistencyError("尚未保存的原媒体申请不能由后续申请替换")
        receipt = self._commit(getattr(self.repository, stage.value)(held.command, held.key, self.owned))
        if receipt.disposition is SaveDisposition.SAVED:
            del self.pending[identity]
        else:
            self.pending[identity] = replace(held, save_error=receipt.error)
        return receipt


async def resume_media_saves(owned, pending, executor=None, *, action_id=None):
    """只核实已经取得的原申请；不启动工具、读取或重算决定。"""
    saves = CaptureProcessingSaves(owned, pending)
    owner = executor if executor is not None else FileTaskExecutor(Supervisor())
    for held in tuple(pending.values()):
        if action_id is not None and held.action_id != action_id:
            continue

        async def save_original(_control=None):
            receipt = saves._save(held.stage, held.command)
            if receipt.disposition is not SaveDisposition.SAVED:
                raise ConsistencyError(f"原媒体结果未可靠保存: {receipt.error}") from receipt.error
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
    duration = (
        saved_check_duration(parse_exact_json(media_raw))
        if check_state == 3 else None)
    target = saved_target_duration_ms(parse_exact_json(basis_raw))
    if output_id is None and repair_state == 4:
        with closing(owned.connection.execute(
            "SELECT id FROM intermediate_files"
            " WHERE owner_action_id=? AND purpose=4 ORDER BY id", (action_id,)
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

    owned: OwnedConnection
    roots: BoundDirectories
    sessions: DriverReadSessions
    tools: MediaTools
    policy: MediaPolicy
    occurred_at: Callable[[], int]
    digest_supported: bool
    digest: Any = None
    digest_for: Callable[[int], Any] | None = None
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

    def saves(self, *, file_ids=()) -> CaptureProcessingSaves:
        return CaptureProcessingSaves(self.owned, self.pending_media_results, file_ids=file_ids)

    def copies(self) -> RecordingInputCopies:
        return RecordingInputCopies(
            self.owned, self.roots, self.occurred_at, max_recopies=self.max_recopies,
            file_executor=self.file_executor)


def _ensure_checksum_support(
    flow: MediaFlow, source_device_file_id: int) -> None:
    """按绑定声明固定来源文件的摘要能力；已决定的能力不重复声明。"""
    with closing(flow.owned.connection.execute(
        "SELECT checksum_support FROM device_files WHERE id = ?",
        (source_device_file_id,),
    )) as cursor:
        row = cursor.fetchone()
    if row is None:
        raise ConsistencyError(f"设备文件不存在: {source_device_file_id}")
    if row[0] != 1:
        return
    support = 2 if flow.digest_supported else 3
    outcome = CaptureRepository().save_file_checksum(
        FileChecksumSave(
            file_id=source_device_file_id,
            support=support,
            occurred_at=flow.occurred_at()),
        new_operation_key(), flow.owned)
    if outcome.kind is not _DbOutcomeKind.COMPLETED:
        raise ConsistencyError(f"摘要能力声明未完成: {outcome.error}")


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

    await resume_media_saves(flow.owned, flow.pending_media_results, flow.file_executor, action_id=action_id)
    retry_read_business(flow)
    for pending in tuple(flow.pending_read_results.values()):
        _save_internal_read_and_settle(flow, pending)
    await retry_read_ends(flow)
    original_failure = flow.owned.connection.execute(
        "SELECT c.id FROM file_copies c JOIN operation_runs r ON r.copy_id=c.id"
        " WHERE c.processing_id=? AND r.kind=3 AND r.status=4", (processing_id,)).fetchall()
    if len(original_failure) > 1:
        raise ConsistencyError("同一内部处理有多个终态失败读取")
    if original_failure:
        copy_id = original_failure[0][0]
        _fail_internal_read_input(flow, copy_id=copy_id)
        return InputStep(InputPhase.WAITING, copy_id=copy_id, reason="run_ended")
    _ensure_checksum_support(flow, source_device_file_id)
    status = load_processing_status(flow.owned, processing_id)
    digest = (flow.digest_for(source_device_file_id)
              if flow.digest_for is not None else flow.digest)
    copies = flow.copies()
    config = OperationConfig(flow.max_read_attempts, flow.read_idle_timeout_s,
                             flow.retry_interval_s)
    qualified = copies.qualify(FileCandidate(
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
    state = copies.copy_state(copy_id)
    ticket = None
    wait_key = f"read/{copy_id}"
    if state.verification_state not in (3, 5) or state.target_sha256 is None:
        run = flow.owned.connection.execute(
            "SELECT attempts_used,retry_wait_required FROM operation_runs WHERE copy_id=?", (copy_id,)).fetchone()
        if run is None:
            raise ConsistencyError("内部输入缺少原读取流程")
        if flow.retry_gate.remaining(wait_key, attempts_used=run[0], retry_wait_required=run[1] == 1,
                max_attempts_used=flow.max_read_attempts, interval_s=flow.retry_interval_s,
                now_ns=flow.monotonic_ns()) is not None:
            return InputStep(InputPhase.RETRY_WAITING, copy_id=copy_id)
        slot = copies.repository.grant_read_slot(SlotRequest(copy_id, flow.occurred_at()),
                                                  new_operation_key(), flow.owned)
        if slot.kind is not _DbOutcomeKind.COMPLETED:
            raise ConsistencyError(f"内部读取机会未可靠保存: {slot.error}")
        if slot.value.outcome in (SlotOutcome.WAIT, SlotOutcome.FINISHED):
            return InputStep(InputPhase.WAITING, copy_id=copy_id)
        ticket, reason = acquire_read_attempt(flow, AttemptIntent(
            "read", action_id, OperationKind.READ_FILE, AttemptTarget(copy_id=copy_id), None,
            AttemptConfig(flow.max_read_attempts, flow.read_idle_timeout_s, flow.retry_interval_s),
            flow.occurred_at(), copy_round=state.round))
        if ticket is None:
            if reason == "budget_exhausted":
                _fail_internal_read_input(flow, copy_id=copy_id)
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
        on_read_start=lambda: flow.pending_read_ends.pop(copy_id, None),
    )

    async def read_and_save():
        step = await obtain_recording_input(context)
        if ticket is not None:
            _finish_internal_read(flow, ticket, step)
        return step

    input_step = await run_owned_read(flow, copy_id, read_and_save) if ticket is not None else await read_and_save()
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
    status = load_processing_status(flow.owned, processing_id)
    if status.repair_state not in (3, 4):
        return check
    repair_files = (input_step.input_file.file_id,)
    if status.repair_output_file_id is not None:
        repair_files = tuple(dict.fromkeys((*repair_files, status.repair_output_file_id)))
    repair = await execute_repair(RepairContext(
        processing=status,
        input_file=input_step.input_file,
        # 修复成品与输入副本同容器：无重编码流复制沿用源容器的封
        # 装格式，登记扩展名默认与输入副本一致，装配可用
        # repair_extension 显式覆盖（camera-recovery.md 裁剪约束）。
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

    flow = replace(original_flow, owned=current.owned)
    copies = flow.copies()
    context = replace(original_context, copies=copies, digest=None, read_end=held.end)
    copy_id = int(held.ticket.target_id)

    async def complete_and_save():
        step = replace(await _complete_input(context, copy_id, copies.copy_state(copy_id)),
                       read_end=held.end, stop_requested=True, content_complete=True)
        _finish_internal_read(flow, held.ticket, step)

    await run_owned_read(flow, copy_id, complete_and_save)


def _finish_internal_read(flow: MediaFlow, ticket, step: InputStep) -> None:
    """连接已经关闭后保存读取结果；重拷继续原尝试。"""
    if step.phase is InputPhase.RECOPY_PENDING:
        flow.continuing_read_tickets[step.copy_id] = ticket
        return
    from camctl.outputs.read_attempts import stopped_read_result

    stopped = stopped_read_result(flow, ticket, step, flow.evidence, flow.occurred_at())
    if stopped is not None:
        _save_internal_read_and_settle(flow, stopped)
        return
    from camctl.outputs.read_attempts import canceled_complete_read_result

    completed_cancel = canceled_complete_read_result(flow, ticket, flow.evidence)
    if completed_cancel is not None:
        _save_internal_read_and_settle(flow, completed_cancel)
        return
    checksum_failed = step.phase is InputPhase.CHECKSUM_EXHAUSTED
    if step.phase is not InputPhase.INPUT_READY and not step.source_failed and not checksum_failed:
        raise ConsistencyError(f"内部读取的本地或保存前提未完成，保留原尝试: {step.phase.value}: {step.error}")
    succeeded = step.phase is InputPhase.INPUT_READY or checksum_failed
    # 原 ticket 的连续编号已由意图／恢复事务核实，实际返回后先持有结果。
    exhausted = ticket.attempt_id >= flow.max_read_attempts
    outcome = CallOutcome(status=AttemptStatus.SUCCEEDED if succeeded else AttemptStatus.FAILED,
        effect=EffectState.UNKNOWN,
        error=None if succeeded else ErrorValue("device_error", "read"),
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("read_returned", 1, {})))
    run_finish = RunFinish(RunOutcome.SUCCEEDED) if succeeded else (
        RunFinish(RunOutcome.FAILED, ErrorValue("read_attempts_exhausted", "source_read")) if exhausted else None)
    if checksum_failed:
        run_finish = RunFinish(RunOutcome.FAILED, ErrorValue("checksum_mismatch", "source_read", {
            "max_recopies": step.error.max_recopies, "recopies_used": step.error.recopies_used}))
    pending = hold_read_result(flow, AttemptFinish(
        ticket, validate_outcome(ticket, outcome, flow.evidence), flow.occurred_at(),
        retry_wait=not succeeded and not exhausted, run_finish=run_finish))
    _save_internal_read_and_settle(flow, pending)


def resume_prepared_internal_reads(
    owned: OwnedConnection, *, pending_read_results: dict,
    pending_read_business: dict, pending_read_ends: dict,
    continuing_read_tickets: dict,
) -> None:
    """默认入口只核实原完整 READ 申请，不取得新设备或媒体执行资格。"""
    prepared = tuple(pending for pending in pending_read_results.values()
                     if isinstance(pending, PendingReadResult))
    if not prepared and not pending_read_business:
        return
    scope = SimpleNamespace(
        owned=owned, operations=OperationRepository(),
        pending_read_results=pending_read_results, pending_read_business=pending_read_business,
        pending_read_ends=pending_read_ends, continuing_read_tickets=continuing_read_tickets)
    for pending in prepared:
        # 完整输入首次确定的原时刻承担其独立收场，重送不读取当前墙钟。
        scope.occurred_at = lambda original=pending.finish.occurred_at: original
        _save_internal_read_and_settle(scope, pending)
    retry_read_business(scope)


def _save_internal_read_and_settle(flow: MediaFlow, pending) -> None:
    pending = save_read_result(flow, pending)
    if pending is None:
        return
    if pending.business is not None:
        from camctl.outputs.read_attempts import save_prepared_read_business

        forget_read_result(flow, pending)
        save_prepared_read_business(flow, pending.business.request.action_id, "capture_binding_failure", pending.business)
        return
    ticket = pending.finish.ticket
    succeeded = pending.finish.outcome.outcome.status is AttemptStatus.SUCCEEDED
    exhausted = (pending.finish.run_finish is not None
                 and pending.finish.run_finish.status is RunOutcome.FAILED)
    if exhausted:
        _fail_internal_read_input(flow, copy_id=int(ticket.target_id))
    if succeeded and not exhausted:
        from camctl.outputs.read_attempts import save_read_business

        # 原尝试已经可靠保存；后续独立释放由原完整申请和键承担。
        # 不继续持有 Finish，以免该释放重送后再次生成另一申请。
        forget_read_result(flow, pending)
        save_read_business(flow, int(ticket.target_id), "release_read_slot",
            SlotRequest(int(ticket.target_id), pending.finish.occurred_at), OutputsRepository().release_read_slot)
        return
    forget_read_result(flow, pending)


def _fail_internal_read_input(flow: MediaFlow, *, copy_id: int) -> None:
    """原读取可靠耗尽后结束所需检查或修复，再释放原读取机会。"""
    from camctl.capture.processing import (
        CheckPhase, CheckResultSave, MediaObservation, ProcessingError, RepairBasis,
        RepairDecisionChoice, RepairDecisionSave, RepairOutcome, RepairReason, RepairResultSave,
    )

    row = flow.owned.connection.execute(
        "SELECT c.processing_id,r.attempts_used,r.max_attempts_used,r.status FROM file_copies c"
        " JOIN operation_runs r ON r.copy_id=c.id WHERE c.id=?", (copy_id,)).fetchone()
    if row is None or row[0] is None or row[3] != 4:
        raise ConsistencyError("内部输入失败必须对应已可靠失败的原读取流程")
    processing_id, used, maximum, _status = row
    unfinished = flow.owned.connection.execute(
        "SELECT 1 FROM operation_attempts WHERE run_id=(SELECT id FROM operation_runs WHERE copy_id=?)"
        " AND (status=1 OR result_json IS NULL) LIMIT 1", (copy_id,)).fetchone()
    if unfinished is not None:
        raise ConsistencyError("原内部读取尚有未结束调用，不能收场检查或释放保护")
    error_raw = flow.owned.connection.execute(
        "SELECT error_json FROM operation_runs WHERE copy_id=?", (copy_id,)).fetchone()[0]
    original_error = parse_exact_json(error_raw)
    status = load_processing_status(flow.owned, processing_id)
    if original_error["code"] == "checksum_mismatch":
        failure = ProcessingError("checksum_mismatch", "source_read", original_error["details"])
    elif original_error["code"] == "read_attempts_exhausted":
        failure = ProcessingError("read_attempts_exhausted", "source_read",
                                  {"attempts_used": used, "max_read_attempts": maximum})
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

    for save, request in requests:
        save_read_business(flow, copy_id, save.__name__, request, save)
    if flow.owned.connection.execute(
            "SELECT slot_device_id FROM file_copies WHERE id=?", (copy_id,)).fetchone()[0] is not None:
        save_read_business(flow, copy_id, "release_read_slot", SlotRequest(copy_id, flow.occurred_at()),
                           OutputsRepository().release_read_slot)

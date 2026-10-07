"""录像媒体链调用方：D4 读取会话绑定与检查修复组合。

把内部输入取得、检查与修复编排接到真实仓储和设备读取端口：
DriverReadSessions 按设备文件的完成事实经驱动 open_read 打开可停
止读取会话；RecordingInputCopies 组合资格、续传与完整性事务；
CaptureProcessingSaves 适配处理事务。run_recording_media 串联输入
取得、检查执行与修复执行，各失败分区原样透传，不在本层重试。
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from time import monotonic_ns as _default_monotonic_ns
from typing import Any, Callable

from camctl.capture.files import FileChecksumSave
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
)
from camctl.capture.processing import saved_check_duration, saved_target_duration_ms
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.ports import ReadDriver
from camctl.devices.read_session import SourceFile
from camctl.host_files.models import BoundDirectories
from camctl.outputs.copy import (
    CompletionContext,
    CopyContext,
    SegmentContext,
    complete_copy,
    copy_next_segment,
    prepare_copy,
)
from camctl.outputs.qualification import FileCandidate, OperationConfig
from camctl.operations.attempts import RetryWaitGate
from camctl.persistence.models import DbOutcomeKind as _DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.runtime import OwnedConnection

__all__ = [
    "CaptureProcessingSaves",
    "DriverReadSessions",
    "MediaFlow",
    "RecordingInputCopies",
    "load_confirmed_source",
    "load_processing_status",
    "run_recording_media",
]

#: 内部读取采用的段大小与次数、调用时限（第一版固定值，重试间隔
#: 随设备声明 devices.<id>.copy.retry_interval_s 接入）。
_SEGMENT_SIZE = 4 * 1024 * 1024
_READ_MAX_ATTEMPTS = 3
_READ_TIMEOUT_S = Decimal("60")


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
                 ticket: Any) -> None:
        self._owned = owned
        self._driver = driver
        self._ticket = ticket

    async def open_session(self, source_device_file_id: int, offset: int):
        source = load_confirmed_source(self._owned, source_device_file_id)
        return await self._driver.open_read(source, offset, self._ticket)


class RecordingInputCopies:
    """内部输入拷贝端口适配：资格、状态、续传、分段与完整性事务。"""

    def __init__(self, owned: OwnedConnection, roots: BoundDirectories,
                 occurred_at: Callable[[], int], *,
                 max_recopies: int = 1) -> None:
        self.owned = owned
        self.roots = roots
        self.occurred_at = occurred_at
        self.max_recopies = max_recopies
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
            occurred_at=self.occurred_at()))

    async def transfer(self, copy_id: int, session, segment_size: int):
        return await copy_next_segment(copy_id, SegmentContext(
            repository=self.repository, owned=self.owned, roots=self.roots,
            occurred_at=self.occurred_at(), segment_size=segment_size,
            session=session))

    async def complete(self, copy_id: int, digest):
        return await complete_copy(copy_id, CompletionContext(
            repository=self.repository, owned=self.owned, roots=self.roots,
            occurred_at=self.occurred_at(), digest=digest,
            max_recopies=self.max_recopies))


class CaptureProcessingSaves:
    """处理事务端口适配；每次保存独立提交并使用新操作键。"""

    def __init__(self, owned: OwnedConnection) -> None:
        self.owned = owned
        self.repository = CaptureRepository()

    @staticmethod
    def _commit(outcome) -> SaveReceipt:
        if outcome.kind is _DbOutcomeKind.COMPLETED:
            return SaveReceipt(SaveDisposition.SAVED, value=outcome.value)
        if outcome.kind is _DbOutcomeKind.ROLLED_BACK:
            return SaveReceipt(SaveDisposition.REJECTED, error=outcome.error)
        return SaveReceipt(SaveDisposition.UNKNOWN, error=outcome.error)

    def save_check_result(self, command):
        return self._commit(self.repository.save_check_result(
            command, new_operation_key(), self.owned))

    def save_repair_decision(self, command):
        return self._commit(self.repository.save_repair_decision(
            command, new_operation_key(), self.owned))

    def save_repair_result(self, command):
        return self._commit(self.repository.save_repair_result(
            command, new_operation_key(), self.owned))

    def start_repair_output(self, command):
        return self._commit(self.repository.start_repair_output(
            command, new_operation_key(), self.owned))

    def complete_repair_output(self, command):
        return self._commit(self.repository.complete_repair_output(
            command, new_operation_key(), self.owned))


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

    def saves(self) -> CaptureProcessingSaves:
        return CaptureProcessingSaves(self.owned)

    def copies(self) -> RecordingInputCopies:
        return RecordingInputCopies(
            self.owned, self.roots, self.occurred_at)


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

    本会话上一次段传输或完整性收尾的通信失败保存了重试等待时，
    间隔未到不开始新的读取（不开会话、不触设备），返回等待分区；
    副本就绪或登记重拷清除等待，重拷流程的首次读取不预等待。
    """
    wait_key = f"media-input/{processing_id}"
    if flow.retry_gate.pending(
            wait_key, interval_s=flow.retry_interval_s,
            now_ns=flow.monotonic_ns()) is not None:
        return InputStep(InputPhase.RETRY_WAITING)
    _ensure_checksum_support(flow, source_device_file_id)
    status = load_processing_status(flow.owned, processing_id)
    digest = (flow.digest_for(source_device_file_id)
              if flow.digest_for is not None else flow.digest)
    input_step = await obtain_recording_input(InputContext(
        action_id=action_id,
        processing_id=processing_id,
        source_device_file_id=source_device_file_id,
        target_extension=target_extension,
        config=OperationConfig(
            max_attempts=_READ_MAX_ATTEMPTS, timeout_s=_READ_TIMEOUT_S,
            retry_interval_s=flow.retry_interval_s),
        segment_size=_SEGMENT_SIZE,
        staging=flow.roots.staging,
        copies=flow.copies(),
        sessions=flow.sessions,
        occurred_at=flow.occurred_at(),
        digest=digest,
    ))
    if input_step.phase in (InputPhase.SEGMENT_FAILED,
                            InputPhase.COMPLETION_FAILED):
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
        saves=flow.saves(),
        occurred_at=flow.occurred_at(),
    ))
    if (check.phase is not CheckExecutionPhase.CHECK_COMPLETED
            and check.phase is not CheckExecutionPhase.NOT_REQUIRED):
        return check
    # 检查完成或检查不适用：修复决定待执行才继续（计时判定的异常
    # 多录不经检查直接修复），无需修复时到此为止。
    status = load_processing_status(flow.owned, processing_id)
    if status.repair_state not in (3, 4):
        return check
    return await execute_repair(RepairContext(
        processing=status,
        input_file=input_step.input_file,
        # 修复成品与输入副本同容器：无重编码流复制沿用源容器的封
        # 装格式，登记扩展名默认与输入副本一致，装配可用
        # repair_extension 显式覆盖（camera-recovery.md 裁剪约束）。
        extension=(flow.repair_extension
                   if flow.repair_extension is not None
                   else target_extension),
        tools=flow.tools,
        saves=flow.saves(),
        occurred_at=flow.occurred_at(),
    ))

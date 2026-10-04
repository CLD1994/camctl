"""C8 检查与修复执行编排的组件集成测试。

真实 SQLite、P3 事务内核与真实受管子进程组合：编排经适配层驱动
保存端口与媒体端口，验证检查决定后的执行链事实共同提交——检查
阶段与媒体观察、修复决定、修复输出登记与字节事实、修复终态；失
败与恢复入口按分区验证。工具替身不证明真实视频结论。
"""

from __future__ import annotations

import hashlib
import json
import sys
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.capture.media import (
    CheckContext,
    CheckExecutionPhase,
    MediaPolicy,
    ProcessingStatus,
    RepairContext,
    RepairExecutionPhase,
    SaveDisposition,
    SaveReceipt,
    execute_check,
    execute_repair,
)
from camctl.capture.processing import (
    CheckPhase,
    CheckResultSave,
    MediaObservation,
    ProcessingDisposition,
    RepairBasis,
    RepairDecisionChoice,
    RepairDecisionSave,
    RepairReason,
    RepairStart,
    RepairSuccess,
    saved_check_duration,
    saved_target_duration_ms,
)
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.host_files import media as real_media
from camctl.host_files.media import ProbeRequest, RepairRequest
from camctl.host_files.models import BoundDirectories, FilePurpose, FileRef
from camctl.host_files.tasks import FileTaskExecutor, FileTaskId
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import (
    CaptureRepository,
    register_capture_guards,
)
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.session.supervision import Supervisor

from ..persistence.test_runtime import _create_valid_database

register_capture_guards()
register_outputs_guards()

_NOW = 1_750_000_000_000_000
_POLICY = MediaPolicy(repair_margin_s=Decimal("10"))

_PROBE_BODY = """
import json, sys
print(json.dumps({"format": {"duration": "75.125"}}))
"""

_FAILING_PROBE_BODY = """
import sys
sys.exit(3)
"""

_REPAIR_BODY = """
import sys
args = sys.argv[1:]
input_path = args[args.index("-i") + 1]
output_path = args[-1]
with open(input_path, "rb") as source:
    data = source.read()
if "--fail" in args:
    with open(output_path, "wb") as target:
        target.write(data[: len(data) // 2])
    sys.exit(1)
with open(output_path, "wb") as target:
    target.write(b"repaired:" + data)
"""


class _Owner:
    async def take_over(self, task):
        raise AssertionError("编排用例的等待者没有取消，不应产生接手")


def _tool(directory: Path, name: str, body: str) -> str:
    helper = directory / f"{name}.py"
    helper.write_text(body, encoding="utf-8")
    if sys.platform == "win32":
        launcher = directory / f"{name}.cmd"
        launcher.write_text(
            f'@{sys.executable} "{helper}" %*\n', encoding="utf-8")
        return str(launcher)
    launcher = directory / f"{name}.sh"
    launcher.write_text(
        f'#!/bin/sh\nexec {sys.executable} "{helper}" "$@"\n', encoding="utf-8")
    launcher.chmod(0o755)
    return str(launcher)


@pytest.fixture
def pipeline(tmp_path: Path):
    """检查决定已固定为需要检查的处理行、输入副本与真实工具。"""
    target = tmp_path / "state.db"
    _create_valid_database(target)
    owned = open_existing(target, DbOpenMode.EXISTING_RW, DbConfig())
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "INSERT INTO history_transactions (id, operation_key, first_event_id, last_event_id)"
        " VALUES (1, ?, 1, 1)",
        ("f" * 32,),
    )
    connection.execute(
        "INSERT INTO history_events (id, transaction_id, event_type, event_version,"
        " occurred_at, clock_status, change_seq, body_json)"
        " VALUES (1, 1, 2, 1, ?, 2, NULL, ?)",
        (_NOW, json.dumps({"reason": 1, "evidence": {}, "rows": []})),
    )
    _seed_plan(connection, 1)
    _seed_action(connection, 1, 1)
    _seed_device_file(connection, 11)
    _seed_intermediate(
        connection, 701, action=1, purpose=2, path="recording-inputs/701.mp4",
        size=250, sha256="b" * 64)
    _seed_processing(connection, 1, 1)
    connection.commit()

    staging = tmp_path / "staging"
    (staging / "recording-inputs").mkdir(parents=True)
    (staging / "derived").mkdir()
    (staging / "recording-inputs" / "701.mp4").write_bytes(b"x" * 250)
    tools = _SubprocessTools(
        staging,
        probe_tool=_tool(tmp_path, "probe", _PROBE_BODY),
        repair_tool=_tool(tmp_path, "repair", _REPAIR_BODY),
    )
    return owned, staging, tools


def _seed_plan(connection, plan_id: int) -> None:
    connection.execute(
        "INSERT INTO plans (id, request_id, name, created_at, status,"
        " created_event_id, last_event_id, change_count)"
        " VALUES (?, ?, 'seed', ?, 1, 1, 1, 1)",
        (plan_id, 4241 + plan_id, _NOW),
    )


def _seed_action(connection, action_id: int, plan_id: int) -> None:
    connection.execute(
        "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
        " scheduled_at, group_name, input_fields_json, effective_params_json,"
        " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
        " cancel_requested, error_code, error_details_json, first_window_observed_at,"
        " expiration_reason, source_resolution_state, resolved_source_plan_id,"
        " target_selection_state, created_event_id, last_event_id, change_count)"
        " VALUES (?, ?, 0, 'rec', 2, 'cam-1', ?, NULL, '{}',"
        " '{\"target_duration_s\": 60}', 'camctl-adb', 1000, '{}', 2, 1, 0, NULL,"
        " NULL, NULL, NULL, NULL, NULL, NULL, 1, 1, 1)",
        (action_id, plan_id, _NOW),
    )


def _seed_device_file(connection, file_id: int) -> None:
    connection.execute(
        "INSERT INTO device_files (id, observer_action_id, source_action_id,"
        " identity_key, locator_json, ownership_evidence_json, original_name,"
        " media_type, role, original_device_file_id, pairing_evidence_json,"
        " presence_state, completion_state, completion_evidence_json, size_bytes,"
        " checksum_support, sha256, last_error_json, created_event_id,"
        " last_event_id, change_count)"
        " VALUES (?, 1, 1, ?, '{}', '{}', 'video.mp4', 'video/mp4', 2, NULL, NULL,"
        " 2, 3, '{}', 250, 3, NULL, NULL, 1, 1, 1)",
        (file_id, f"file-{file_id:04d}"),
    )


def _seed_intermediate(connection, file_id: int, *, action: int, purpose: int,
                       path: str, size: int | None, sha256: str | None) -> None:
    connection.execute(
        "INSERT INTO intermediate_files (id, owner_action_id, owner_delivery_id,"
        " purpose, relative_path, retention_state, cleanup_state, size_bytes,"
        " sha256, created_event_id, last_event_id, change_count)"
        " VALUES (?, ?, NULL, ?, ?, 1, 1, ?, ?, 1, 1, 1)",
        (file_id, action, purpose, path, size, sha256),
    )


def _seed_processing(connection, processing_id: int, action_id: int) -> None:
    """检查决定固定为需要检查：目标 60 秒，修复决定未判定。"""
    connection.execute(
        "INSERT INTO recording_processing (id, action_id, source_device_file_id,"
        " check_state, check_decision, check_basis_json, media_json, repair_state,"
        " repair_basis_json, repair_output_file_id, repair_error_json,"
        " discard_state, discard_error_json)"
        " VALUES (?, ?, 11, 1, 3, ?, '{}', 1, NULL, NULL, NULL, 1, NULL)",
        (processing_id, action_id,
         '{"reason": 2, "target_duration_ms": 60000}'),
    )


def _row(owned, sql: str, *params):
    with closing(owned.connection.execute(sql, params)) as cursor:
        return cursor.fetchone()


def _json_value(raw):
    return None if raw is None else parse_exact_json(raw)


def _status(owned, processing_id: int = 1) -> ProcessingStatus:
    """从当前投影装载编排事实；解码走生产读取规则。

    修复处于执行中时，repair_output_file_id 尚未由成功事务固定，
    已登记输出按归属动作与修复输出用途查询。
    """
    row = _row(
        owned,
        "SELECT check_decision, check_state, media_json, check_basis_json,"
        " repair_state, repair_output_file_id, action_id"
        " FROM recording_processing WHERE id=?",
        processing_id)
    (check_decision, check_state, media_raw, basis_raw, repair_state,
     output_id, action_id) = row
    duration = (
        saved_check_duration(parse_exact_json(media_raw))
        if check_state == 3 else None)
    target = saved_target_duration_ms(parse_exact_json(basis_raw))
    if output_id is None and repair_state == 4:
        output_id = _registered_repair_output(owned, action_id)
    path = None
    if output_id is not None:
        path = _row(
            owned, "SELECT relative_path FROM intermediate_files WHERE id=?",
            output_id)[0]
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


def _registered_repair_output(owned, action_id: int) -> int | None:
    """查找本动作已登记的修复输出文件；多个登记属于不可解释状态。"""
    with closing(owned.connection.execute(
        "SELECT id FROM intermediate_files"
        " WHERE owner_action_id=? AND purpose=4 ORDER BY id", (action_id,)
    )) as cursor:
        rows = cursor.fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        raise ValueError(f"动作 {action_id} 登记了多个修复输出文件")
    return rows[0][0]


def _input_ref(staging: Path) -> FileRef:
    return FileRef(701, FilePurpose.RECORDING_INPUT,
                   "recording-inputs/701.mp4", staging)


class _RepositorySaves:
    """把真实仓储适配成编排保存端口；每次保存使用新操作键。"""

    def __init__(self, owned) -> None:
        self.owned = owned
        self.repository = CaptureRepository()

    def _commit(self, outcome) -> SaveReceipt:
        if outcome.kind is DbOutcomeKind.COMPLETED:
            return SaveReceipt(SaveDisposition.SAVED, value=outcome.value)
        if outcome.kind is DbOutcomeKind.ROLLED_BACK:
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


class _SubprocessTools:
    """把受管媒体入口适配成编排工具端口。"""

    def __init__(self, staging: Path, *, probe_tool: str, repair_tool: str) -> None:
        self.roots = BoundDirectories(staging=staging)
        self.probe_request = ProbeRequest(ffprobe=probe_tool)
        self.repair_request = RepairRequest(ffmpeg=repair_tool)
        self._counter = 0

    async def probe(self, input: FileRef):
        self._counter += 1
        return await real_media.probe_media(
            input, self.roots, self.probe_request,
            executor=FileTaskExecutor(Supervisor()),
            task_id=FileTaskId(f"probe-{self._counter}"),
            owner=_Owner())

    async def repair(self, input: FileRef, output: FileRef):
        self._counter += 1
        return await real_media.repair_media(
            input, output, self.roots, self.repair_request,
            executor=FileTaskExecutor(Supervisor()),
            task_id=FileTaskId(f"repair-{self._counter}"),
            owner=_Owner())


def _events(owned, event_type: int, reason: int) -> int:
    return _row(
        owned,
        "SELECT COUNT(*) FROM history_events WHERE event_type=?"
        " AND json_extract(body_json, '$.reason')=?",
        event_type, reason)[0]


def _event_types(owned, txn_id: int) -> list[tuple[int, int]]:
    with closing(owned.connection.execute(
        "SELECT event_type, json_extract(body_json, '$.reason')"
        " FROM history_events WHERE transaction_id=? ORDER BY id", (txn_id,)
    )) as cursor:
        return [(row[0], row[1]) for row in cursor]


# ---- 检查编排 ----


@pytest.mark.asyncio
async def test_check_pipeline_completes_with_decision(pipeline) -> None:
    """决定需要检查后：运行阶段、完成观察与修复决定按序共同保存。"""
    owned, staging, tools = pipeline
    step = await execute_check(CheckContext(
        processing=_status(owned), input_file=_input_ref(staging),
        policy=_POLICY, tools=tools, saves=_RepositorySaves(owned),
        occurred_at=_NOW + 10))
    assert step.phase is CheckExecutionPhase.CHECK_COMPLETED, step.error
    row = _row(
        owned,
        "SELECT check_state, repair_state, media_json, repair_basis_json"
        " FROM recording_processing WHERE id=1")
    assert row[0] == 3
    assert row[1] == 3
    assert _json_value(row[2]) == {
        "check_status": "completed",
        "duration": {"status": "available", "seconds": Decimal("75.125")},
    }
    assert _json_value(row[3]) == {
        "reason": RepairReason.THRESHOLD_REACHED.value,
        "target_duration_ms": 60_000,
        "threshold_s": Decimal("70"),
        "actual_duration_s": Decimal("75.125"),
    }
    assert _events(owned, 19, 1) == 2
    assert _events(owned, 18, 2) == 1


@pytest.mark.asyncio
async def test_check_pipeline_tool_failure_is_terminal(pipeline, tmp_path) -> None:
    """检查工具失败：保存失败终态与错误结构，不固定修复决定。"""
    owned, staging, tools = pipeline
    tools.probe_request = ProbeRequest(
        ffprobe=_tool(tmp_path, "probe-fail", _FAILING_PROBE_BODY))
    step = await execute_check(CheckContext(
        processing=_status(owned), input_file=_input_ref(staging),
        policy=_POLICY, tools=tools, saves=_RepositorySaves(owned),
        occurred_at=_NOW + 10))
    assert step.phase is CheckExecutionPhase.CHECK_TERMINAL
    row = _row(
        owned,
        "SELECT check_state, repair_state, media_json FROM recording_processing"
        " WHERE id=1")
    assert row[0] == 4
    assert row[1] == 1
    media = _json_value(row[2])
    assert media["check_status"] == "failed"
    assert media["duration"] == {"status": "unknown"}
    assert media["error"]["code"] == "tool_failed"
    assert media["error"]["stage"] == "probe"
    assert _events(owned, 18, 2) == 0


@pytest.mark.asyncio
async def test_check_recovery_fixes_decision_from_saved_media(pipeline) -> None:
    """检查完成已保存而决定未固定：恢复入口只补固定修复决定。"""
    owned, staging, tools = pipeline
    repository = CaptureRepository()
    for command in (
        CheckResultSave(1, MediaObservation(CheckPhase.RUNNING), occurred_at=_NOW + 1),
        CheckResultSave(
            1, MediaObservation(CheckPhase.COMPLETED, duration_s=Decimal("75.125")),
            occurred_at=_NOW + 2),
    ):
        outcome = repository.save_check_result(command, new_operation_key(), owned)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    step = await execute_check(CheckContext(
        processing=_status(owned), input_file=_input_ref(staging),
        policy=_POLICY, tools=tools, saves=_RepositorySaves(owned),
        occurred_at=_NOW + 10))
    assert step.phase is CheckExecutionPhase.FINISHED_DECISION_SAVED, step.error
    assert _row(
        owned, "SELECT repair_state FROM recording_processing WHERE id=1")[0] == 3
    assert _events(owned, 19, 1) == 2


# ---- 修复编排 ----


async def _seed_pending_repair(owned) -> None:
    repository = CaptureRepository()
    commands = (
        CheckResultSave(1, MediaObservation(CheckPhase.RUNNING), occurred_at=_NOW + 1),
        CheckResultSave(
            1, MediaObservation(CheckPhase.COMPLETED, duration_s=Decimal("75.125")),
            occurred_at=_NOW + 2),
        RepairDecisionSave(
            1, RepairDecisionChoice.PENDING,
            basis=RepairBasis(
                reason=RepairReason.THRESHOLD_REACHED,
                target_duration_ms=60_000,
                threshold_s=Decimal("70"),
                actual_duration_s=Decimal("75.125")),
            occurred_at=_NOW + 3),
    )
    for command in commands:
        outcome = (
            repository.save_check_result(command, new_operation_key(), owned)
            if isinstance(command, CheckResultSave)
            else repository.save_repair_decision(command, new_operation_key(), owned))
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error


@pytest.mark.asyncio
async def test_repair_pipeline_registers_output_and_succeeds(pipeline) -> None:
    """修复执行：输出登记与运行同事务，字节事实与成功同事务。"""
    owned, staging, tools = pipeline
    await _seed_pending_repair(owned)
    step = await execute_repair(RepairContext(
        processing=_status(owned), input_file=_input_ref(staging),
        extension="mp4", tools=tools, saves=_RepositorySaves(owned),
        occurred_at=_NOW + 20))
    assert step.phase is RepairExecutionPhase.SUCCEEDED, step.error
    output_id = _row(
        owned, "SELECT repair_output_file_id FROM recording_processing"
        " WHERE id=1")[0]
    assert output_id == 702
    output_path = staging / "derived" / "702.mp4"
    content = output_path.read_bytes()
    assert content == b"repaired:" + b"x" * 250
    row = _row(
        owned,
        "SELECT purpose, owner_action_id, retention_state, cleanup_state,"
        " size_bytes, sha256 FROM intermediate_files WHERE id=?",
        output_id)
    assert row == (4, 1, 1, 1, len(content),
                   hashlib.sha256(content).hexdigest())
    assert _row(
        owned, "SELECT repair_state FROM recording_processing WHERE id=1")[0] == 5
    assert _events(owned, 26, 1) == 1
    assert _events(owned, 26, 2) == 1
    assert _events(owned, 19, 2) == 2


@pytest.mark.asyncio
async def test_repair_pipeline_resumes_after_start(pipeline) -> None:
    """启动事务已提交后的恢复入口：不重复登记，续执行并完成。"""
    owned, staging, tools = pipeline
    await _seed_pending_repair(owned)
    repository = CaptureRepository()
    started = repository.start_repair_output(
        RepairStart(1, "mp4", _NOW + 10), new_operation_key(), owned)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    assert started.value.file_id == 702
    step = await execute_repair(RepairContext(
        processing=_status(owned), input_file=_input_ref(staging),
        extension="mp4", tools=tools, saves=_RepositorySaves(owned),
        occurred_at=_NOW + 20))
    assert step.phase is RepairExecutionPhase.SUCCEEDED, step.error
    assert _row(
        owned, "SELECT COUNT(*) FROM intermediate_files WHERE purpose=4")[0] == 1
    assert _events(owned, 26, 1) == 1
    assert _row(
        owned, "SELECT repair_state FROM recording_processing WHERE id=1")[0] == 5


@pytest.mark.asyncio
async def test_repair_pipeline_failure_saves_error(pipeline, tmp_path) -> None:
    """工具失败：修复失败终态与错误结构保存，登记的输出行保留。"""
    owned, staging, tools = pipeline
    await _seed_pending_repair(owned)
    tools.repair_request = RepairRequest(
        ffmpeg=_tool(tmp_path, "repair-fail", _REPAIR_BODY),
        output_args=("--fail",))
    step = await execute_repair(RepairContext(
        processing=_status(owned), input_file=_input_ref(staging),
        extension="mp4", tools=tools, saves=_RepositorySaves(owned),
        occurred_at=_NOW + 20))
    assert step.phase is RepairExecutionPhase.FAILED_SAVED, step.error
    row = _row(
        owned,
        "SELECT repair_state, repair_error_json, repair_output_file_id"
        " FROM recording_processing WHERE id=1")
    assert row[0] == 6
    assert row[2] is None
    error = _json_value(row[1])
    assert error["code"] == "tool_failed"
    assert error["stage"] == "repair"
    assert error["details"]
    assert _row(
        owned, "SELECT COUNT(*) FROM intermediate_files WHERE purpose=4")[0] == 1


# ---- 仓储命令：修复输出登记与完成 ----


@pytest.mark.asyncio
async def test_start_repair_output_registers_file_and_running(pipeline) -> None:
    owned, staging, tools = pipeline
    await _seed_pending_repair(owned)
    repository = CaptureRepository()
    key = new_operation_key()
    outcome = repository.start_repair_output(
        RepairStart(1, "mp4", _NOW + 10), key, owned)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    assert outcome.value.disposition is ProcessingDisposition.SAVED
    assert (outcome.value.file_id, outcome.value.relative_path) == (
        702, "derived/702.mp4")
    row = _row(
        owned,
        "SELECT purpose, owner_action_id, relative_path, retention_state,"
        " cleanup_state, size_bytes, sha256 FROM intermediate_files WHERE id=702")
    assert row == (4, 1, "derived/702.mp4", 1, 1, None, None)
    assert _row(
        owned, "SELECT repair_state FROM recording_processing WHERE id=1")[0] == 4
    assert _event_types(owned, _txn_of(owned, key)) == [
        (26, 1), (19, 2)]

    reuse = repository.start_repair_output(
        RepairStart(1, "mp4", _NOW + 10), key, owned)
    assert reuse.kind is DbOutcomeKind.COMPLETED, reuse.error
    assert reuse.value.disposition is ProcessingDisposition.ALREADY
    assert (reuse.value.file_id, reuse.value.relative_path) == (
        702, "derived/702.mp4")


def _txn_of(owned, key) -> int:
    return _row(
        owned, "SELECT id FROM history_transactions WHERE operation_key=?",
        str(key))[0]


def test_start_repair_output_rejects_wrong_state(pipeline) -> None:
    """修复决定未固定时不能登记输出；拒绝不改变数据库事实。"""
    owned, staging, tools = pipeline
    before = tuple(owned.connection.iterdump())
    repository = CaptureRepository()
    outcome = repository.start_repair_output(
        RepairStart(1, "mp4", _NOW + 10), new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(outcome.error, ConsistencyError)
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.asyncio
async def test_complete_repair_output_saves_bytes_and_success(pipeline) -> None:
    owned, staging, tools = pipeline
    await _seed_pending_repair(owned)
    repository = CaptureRepository()
    started = repository.start_repair_output(
        RepairStart(1, "mp4", _NOW + 10), new_operation_key(), owned)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    content = b"repaired:" + b"x" * 250
    (staging / "derived" / "702.mp4").write_bytes(content)
    key = new_operation_key()
    outcome = repository.complete_repair_output(
        RepairSuccess(1, 702, len(content),
                      hashlib.sha256(content).hexdigest(), _NOW + 20),
        key, owned)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    row = _row(
        owned,
        "SELECT repair_state, repair_output_file_id, size_bytes, sha256"
        " FROM recording_processing, intermediate_files"
        " WHERE recording_processing.id=1 AND intermediate_files.id=702")
    assert row == (5, 702, len(content), hashlib.sha256(content).hexdigest())
    assert _event_types(owned, _txn_of(owned, key)) == [(26, 2), (19, 2)]

    reuse = repository.complete_repair_output(
        RepairSuccess(1, 702, len(content),
                      hashlib.sha256(content).hexdigest(), _NOW + 20),
        key, owned)
    assert reuse.kind is DbOutcomeKind.COMPLETED, reuse.error
    assert reuse.value.disposition is ProcessingDisposition.ALREADY


@pytest.mark.asyncio
async def test_complete_repair_output_rejects_mismatched_file(pipeline) -> None:
    """字节与成功只属于本动作的修复输出用途；指向输入副本被拒绝。"""
    owned, staging, tools = pipeline
    await _seed_pending_repair(owned)
    repository = CaptureRepository()
    started = repository.start_repair_output(
        RepairStart(1, "mp4", _NOW + 10), new_operation_key(), owned)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    before = tuple(owned.connection.iterdump())
    outcome = repository.complete_repair_output(
        RepairSuccess(1, 701, 250, "b" * 64, _NOW + 20),
        new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(outcome.error, ConsistencyError)
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.asyncio
async def test_complete_repair_output_rejects_without_start(pipeline) -> None:
    """修复仍待执行：字节事实与成功不能越过启动事务。"""
    owned, staging, tools = pipeline
    await _seed_pending_repair(owned)
    repository = CaptureRepository()
    before = tuple(owned.connection.iterdump())
    outcome = repository.complete_repair_output(
        RepairSuccess(1, 702, 250, "b" * 64, _NOW + 20),
        new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(outcome.error, ConsistencyError)
    assert tuple(owned.connection.iterdump()) == before

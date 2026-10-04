"""C8 取消收场与动作失败登记的组件集成测试。

真实 SQLite、真实清理事务与真实文件验证取消后的处理收场：动作归
属中间文件按 X11 定向清理释放并删除，删除失败保存登记错误详情；
必要检查结束后无成功依据的动作以公共错误码登记失败终态，原片与
失败事实同事务保存。
"""

from __future__ import annotations

import json
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.capture.media import (
    DiscardContext,
    DiscardExecutionPhase,
    RecordingFailure,
    SaveDisposition,
    SaveReceipt,
    execute_discard,
)
from camctl.capture.processing import DiscardPhase, DiscardProgressSave, ProcessingDisposition
from camctl.contracts.values import new_operation_key
from camctl.host_files.models import BoundDirectories
from camctl.outputs.catalog import (
    FileReference,
    OutputCatalogFacts,
    OutputDraft,
    OutputKind,
)
from camctl.outputs.work_files import (
    WorkFileContext,
    WorkFileLimits,
    clean_one_work_file,
)
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import (
    CaptureRepository,
    FinishCapture,
    register_capture_guards,
)
from camctl.persistence.repositories.operations import register_operation_guards
from camctl.persistence.repositories.outputs import (
    OutputsRepository,
    register_outputs_guards,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..persistence.test_runtime import _create_valid_database

register_capture_guards()
register_operation_guards()
register_outputs_guards()

_NOW = 1_750_000_000_000_000


class _Saves:
    """把真实仓储适配成取消收场保存端口。"""

    def __init__(self, owned) -> None:
        self.owned = owned
        self.repository = CaptureRepository()

    def _commit(self, outcome) -> SaveReceipt:
        if outcome.kind is DbOutcomeKind.COMPLETED:
            return SaveReceipt(SaveDisposition.SAVED, value=outcome.value)
        if outcome.kind is DbOutcomeKind.ROLLED_BACK:
            return SaveReceipt(SaveDisposition.REJECTED, error=outcome.error)
        return SaveReceipt(SaveDisposition.UNKNOWN, error=outcome.error)

    def save_discard_progress(self, command):
        return self._commit(self.repository.save_discard_progress(
            command, new_operation_key(), self.owned))


class _Cleaning:
    """把 X11 定向清理适配成编排端口。"""

    def __init__(self, owned, roots: BoundDirectories) -> None:
        self.owned = owned
        self.roots = roots

    def action_work_files(self, action_id: int) -> tuple[int, ...]:
        with closing(self.owned.connection.execute(
            "SELECT id FROM intermediate_files"
            " WHERE owner_action_id=? AND purpose IN (2, 4) ORDER BY id",
            (action_id,),
        )) as cursor:
            return tuple(row[0] for row in cursor)

    async def clean(self, file_id: int):
        return await clean_one_work_file(file_id, WorkFileContext(
            repository=OutputsRepository(), owned=self.owned,
            staging=self.roots.staging, occurred_at=_NOW + 30,
            limits=WorkFileLimits(batch_size=32, limit_per_run=128)))


@pytest.fixture
def environment(tmp_path: Path):
    """已取消的录像动作：处理行挂起收场，两个动作归属中间文件在位。"""
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
    _seed_processing(connection, 1, 1)
    _seed_intermediate(
        connection, 701, purpose=2, path="recording-inputs/701.mp4")
    _seed_intermediate(
        connection, 702, purpose=4, path="derived/702.mp4")
    connection.commit()

    staging = tmp_path / "staging"
    for name in ("recording-inputs", "derived"):
        (staging / name).mkdir(parents=True)
    (staging / "recording-inputs" / "701.mp4").write_bytes(b"x" * 32)
    (staging / "derived" / "702.mp4").write_bytes(b"y" * 64)
    roots = BoundDirectories(staging=staging)
    return owned, roots


def _seed_plan(connection, plan_id: int) -> None:
    connection.execute(
        "INSERT INTO plans (id, request_id, name, created_at, status,"
        " created_event_id, last_event_id, change_count)"
        " VALUES (?, ?, 'seed', ?, 1, 1, 1, 1)",
        (plan_id, 4241 + plan_id, _NOW),
    )


def _seed_action(connection, action_id: int, plan_id: int, *,
                 status: int = 2, canceled: int = 0) -> None:
    connection.execute(
        "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
        " scheduled_at, group_name, input_fields_json, effective_params_json,"
        " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
        " cancel_requested, error_code, error_details_json, first_window_observed_at,"
        " expiration_reason, source_resolution_state, resolved_source_plan_id,"
        " target_selection_state, created_event_id, last_event_id, change_count)"
        " VALUES (?, ?, 0, 'rec', 2, 'cam-1', ?, NULL, '{}',"
        " '{\"target_duration_s\": 60}', 'camctl-adb', 1000, '{}', ?, 1, ?, NULL,"
        " NULL, NULL, NULL, NULL, NULL, NULL, 1, 1, 1)",
        (action_id, plan_id, _NOW, status, canceled),
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


def _seed_processing(connection, processing_id: int, action_id: int, *,
                     discard_state: int = 2) -> None:
    connection.execute(
        "INSERT INTO recording_processing (id, action_id, source_device_file_id,"
        " check_state, check_decision, check_basis_json, media_json, repair_state,"
        " repair_basis_json, repair_output_file_id, repair_error_json,"
        " discard_state, discard_error_json)"
        " VALUES (?, ?, 11, 1, 3, ?, '{}', 1, NULL, NULL, NULL, ?, NULL)",
        (processing_id, action_id,
         '{"reason": 2, "target_duration_ms": 60000}', discard_state),
    )


def _seed_intermediate(connection, file_id: int, *, purpose: int,
                       path: str) -> None:
    connection.execute(
        "INSERT INTO intermediate_files (id, owner_action_id, owner_delivery_id,"
        " purpose, relative_path, retention_state, cleanup_state, size_bytes,"
        " sha256, created_event_id, last_event_id, change_count)"
        " VALUES (?, 1, NULL, ?, ?, 1, 1, NULL, NULL, 1, 1, 1)",
        (file_id, purpose, path),
    )


def _row(owned, sql: str, *params):
    with closing(owned.connection.execute(sql, params)) as cursor:
        return cursor.fetchone()


def _cancel_action(owned) -> None:
    """测试准备：动作取消收场完成（清理判定要求归属终态）。"""
    owned.connection.execute(
        "UPDATE actions SET status=6, cancel_requested=1 WHERE id=1")
    owned.connection.commit()


def _discard_context(owned, roots, *, discard_state: int = 2) -> DiscardContext:
    return DiscardContext(
        processing_id=1, action_id=1, discard_state=discard_state,
        saves=_Saves(owned), cleaning=_Cleaning(owned, roots),
        occurred_at=_NOW + 30)


# ---- 取消收场 ----


@pytest.mark.asyncio
async def test_discard_releases_and_deletes_action_files(environment) -> None:
    owned, roots = environment
    _cancel_action(owned)
    step = await execute_discard(_discard_context(owned, roots))
    assert step.phase is DiscardExecutionPhase.CLEANED, step.error
    assert (step.cleaned, step.failed) == (2, 0)
    assert not (roots.staging / "recording-inputs" / "701.mp4").exists()
    assert not (roots.staging / "derived" / "702.mp4").exists()
    for file_id in (701, 702):
        assert _row(
            owned,
            "SELECT retention_state, cleanup_state, last_error_json"
            " FROM intermediate_files WHERE id=?", file_id) == (2, 4, None)
    assert _row(
        owned, "SELECT discard_state, discard_error_json"
        " FROM recording_processing WHERE id=1") == (4, None)


@pytest.mark.asyncio
async def test_discard_delete_failure_saves_registered_error(
        environment) -> None:
    """删除失败：文件保存登记错误详情，收场保存失败终态。"""
    owned, roots = environment
    _cancel_action(owned)
    # 打开句柄使 Windows 拒绝删除；清理观察正常、删除明确失败。
    handle = open(roots.staging / "recording-inputs" / "701.mp4", "rb")
    try:
        step = await execute_discard(_discard_context(owned, roots))
    finally:
        handle.close()
    assert step.phase is DiscardExecutionPhase.CLEANUP_FAILED
    assert (step.cleaned, step.failed) == (1, 1)
    assert (roots.staging / "derived" / "702.mp4").exists() is False
    retention, cleanup, error = _row(
        owned,
        "SELECT retention_state, cleanup_state, last_error_json"
        " FROM intermediate_files WHERE id=701")
    assert (retention, cleanup) == (2, 5)
    document = json.loads(error)
    assert document["code"] == "action_work_file_delete_failed"
    assert document["details"] == {"file_id": "701"}
    state, error_json = _row(
        owned, "SELECT discard_state, discard_error_json"
        " FROM recording_processing WHERE id=1")
    assert state == 5
    discard_error = json.loads(error_json)
    assert discard_error["code"] == "action_work_file_delete_failed"
    assert discard_error["stage"] == "discard"
    assert discard_error["details"]["file_id"] == "701"


@pytest.mark.asyncio
async def test_discard_resumes_and_finishes(environment) -> None:
    """执行中恢复入口：沿用既有进度重新核实并完成。"""
    owned, roots = environment
    _cancel_action(owned)
    repository = CaptureRepository()
    started = repository.save_discard_progress(
        DiscardProgressSave(1, DiscardPhase.RUNNING, _NOW + 20),
        new_operation_key(), owned)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    step = await execute_discard(
        _discard_context(owned, roots, discard_state=3))
    assert step.phase is DiscardExecutionPhase.CLEANED, step.error
    assert _row(
        owned, "SELECT discard_state FROM recording_processing"
        " WHERE id=1")[0] == 4


# ---- 动作失败登记 ----


def _finish(failure: RecordingFailure) -> FinishCapture:
    return FinishCapture(
        action_id=1,
        drafts=(OutputDraft(
            kind=OutputKind.ORIGINAL,
            file=FileReference(device_file_id=11),
            file_complete=True,
            sha256=None,
        ),),
        catalog_facts=OutputCatalogFacts(action_id=1, ownership_confirmed=True),
        occurred_at=_NOW + 40,
        failure=failure,
    )


def test_processing_failure_finishes_action_with_error(environment) -> None:
    owned, roots = environment
    outcome = CaptureRepository().finish_capture(
        _finish(RecordingFailure(
            code="recording_processing_failed",
            details={"processing_id": "1", "reason": "check_failed"})),
        new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    result = outcome.value
    assert (result.action_status, result.plan_status) == (4, 3)
    row = _row(
        owned,
        "SELECT status, error_code, error_details_json FROM actions WHERE id=1")
    assert row[0] == 4
    assert row[1] == 16
    assert json.loads(row[2]) == {
        "processing_id": "1", "reason": "check_failed"}
    output = _row(
        owned, "SELECT kind, source_action_id FROM outputs WHERE id=?",
        result.output_ids[0])
    assert output == (1, 1)


def test_short_duration_finishes_with_too_short(environment) -> None:
    owned, roots = environment
    outcome = CaptureRepository().finish_capture(
        _finish(RecordingFailure(
            code="recording_too_short", details={"processing_id": "1"})),
        new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    row = _row(
        owned,
        "SELECT status, error_code, error_details_json FROM actions WHERE id=1")
    assert (row[0], row[1]) == (4, 15)
    assert json.loads(row[2]) == {"processing_id": "1"}


def test_invalid_failure_details_are_rejected(environment) -> None:
    """details 不满足登记结构：整组拒绝，不改写动作事实。"""
    owned, roots = environment
    before = tuple(owned.connection.iterdump())
    outcome = CaptureRepository().finish_capture(
        _finish(RecordingFailure(
            code="recording_processing_failed",
            details={"processing_id": "1"})),
        new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert outcome.error is not None
    assert tuple(owned.connection.iterdump()) == before


def test_canceled_action_cannot_fail_finish(environment) -> None:
    """取消请求已生效的动作不走失败终态（按取消收场规则处理）。"""
    owned, roots = environment
    owned.connection.execute(
        "UPDATE actions SET cancel_requested=1 WHERE id=1")
    owned.connection.commit()
    before = tuple(owned.connection.iterdump())
    outcome = CaptureRepository().finish_capture(
        _finish(RecordingFailure(
            code="recording_too_short", details={"processing_id": "1"})),
        new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.ROLLED_BACK
    assert tuple(owned.connection.iterdump()) == before

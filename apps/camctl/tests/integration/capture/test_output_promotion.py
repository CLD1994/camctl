"""修复成品提升与正式登记同事务的组件集成测试。

真实 SQLite 与 P3 事务内核组合：REPAIRED 产物登记时承载中间文
件经 LIFECYCLE 推进到 PROMOTED，元信息取自实际文件事实；缺修
复成功依据或缺完整字节时整组回滚。提升只因同事务登记发生。
"""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from camctl.contracts.history_values import TransactionRange
from camctl.contracts.values import new_operation_key
from camctl.outputs.catalog import FileReference, OutputDraft, OutputKind
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import (
    CaptureRepository, FinishCapture, register_capture_guards,
)
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.persistence.transaction import TransactionScope
from camctl.history.changes import event_report_targets
from camctl.history.validators import EventContext, EventValidationError, validate_event

from .test_recording_finish import (
    _environment, _finish, _original_draft, _seed_device_file,
)

register_capture_guards()
register_outputs_guards()

_NOW = 1_750_000_000_000_000

_LIFECYCLE_EVENT = 26
_LIFECYCLE_REASON = 2


def promotion_environment(tmp_path: Path):
    """原片 11 与未提升的修复输出 21；处理记录声明修复成功。"""
    owned = _environment(tmp_path)
    connection = owned.connection
    connection.execute(
        "INSERT INTO intermediate_files (id, owner_action_id, owner_delivery_id,"
        " purpose, relative_path, retention_state, cleanup_state, size_bytes,"
        " sha256, created_event_id, last_event_id, change_count)"
        " VALUES (21, 1, NULL, 4, 'derived/21.mp4', 1, 1, 2048, ?, 1, 1, 1)",
        ("a" * 64,),
    )
    connection.execute(
        "INSERT INTO recording_processing (id, action_id, source_device_file_id,"
        " check_state, check_decision, check_basis_json, media_json, repair_state,"
        " repair_basis_json, repair_output_file_id, repair_error_json,"
        " discard_state, discard_error_json)"
        " VALUES (1, 1, 11, 3, 3, '{}', '{}', 5, '{}', 21, NULL, 1, NULL)"
    )
    connection.commit()
    return owned


def _repaired_draft(file_id: int = 21, *, batch: int = 11) -> OutputDraft:
    return OutputDraft(
        kind=OutputKind.REPAIRED,
        file=FileReference(intermediate_file_id=file_id),
        file_complete=True,
        sha256="a" * 64,
        original_batch_file_id=batch,
    )


def _row(owned, sql: str, *params):
    with closing(owned.connection.execute(sql, params)) as cursor:
        return cursor.fetchone()


def _finish_with_repair(owned, drafts=None):
    repository = CaptureRepository()
    if drafts is None:
        drafts = (_repaired_draft(), _original_draft(11))
    return repository.finish_capture(_finish(drafts=drafts), new_operation_key(), owned)


def test_repaired_registration_promotes_in_same_transaction(tmp_path: Path) -> None:
    """登记与提升同事务保存；元信息取自原片文件与未检查事实。"""
    owned = promotion_environment(tmp_path)
    try:
        outcome = _finish_with_repair(owned)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        repaired_id, original_id = outcome.value.output_ids
        output = _row(
            owned,
            "SELECT kind, intermediate_file_id, original_name, media_type,"
            " availability, media_json FROM outputs WHERE id = ?",
            repaired_id)
        assert output[0] == 2 and output[1] == 21
        assert output[2] == "video.mp4" and output[3] == "video/mp4"
        assert output[4] == 1
        assert json.loads(output[5]) == {
            "check_status": "not_performed",
            "duration": {"status": "unknown"},
        }
        file = _row(
            owned,
            "SELECT retention_state, cleanup_state, size_bytes, sha256"
            " FROM intermediate_files WHERE id = 21")
        assert file == (3, 1, 2048, "a" * 64)
        # 提升事件存在且排在登记事件之后：提升只因登记发生。
        registered = _row(
            owned,
            "SELECT id FROM history_events WHERE event_type=20"
            " AND json_extract(body_json, '$.reason')=2")
        promoted = _row(
            owned,
            "SELECT id FROM history_events WHERE event_type=?"
            " AND json_extract(body_json, '$.reason')=?",
            _LIFECYCLE_EVENT, _LIFECYCLE_REASON)
        assert registered is not None and promoted is not None
        assert promoted[0] > registered[0]
    finally:
        owned.connection.close()


def test_device_output_metadata_comes_from_device_file(tmp_path: Path) -> None:
    """原片登记的名称、类型与未检查媒体结构来自设备文件行。"""
    owned = promotion_environment(tmp_path)
    try:
        outcome = _finish_with_repair(owned, drafts=(_original_draft(11),))
        assert outcome.kind is DbOutcomeKind.COMPLETED
        output = _row(
            owned,
            "SELECT original_name, media_type, media_json FROM outputs WHERE kind = 1")
        assert output[0] == "video.mp4" and output[1] == "video/mp4"
        assert json.loads(output[2]) == {
            "check_status": "not_performed",
            "duration": {"status": "unknown"},
        }
    finally:
        owned.connection.close()


def test_repaired_metadata_falls_back_to_unknown(tmp_path: Path) -> None:
    """原片文件未保存可读元信息时，产物保留未知，不冒充实测。"""
    owned = promotion_environment(tmp_path)
    connection = owned.connection
    try:
        connection.execute(
            "UPDATE device_files SET original_name=NULL, media_type=NULL WHERE id = 11")
        connection.commit()
        outcome = _finish_with_repair(owned)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        output = _row(
            owned, "SELECT original_name, media_type FROM outputs WHERE kind = 2")
        assert output == (None, None)
    finally:
        connection.close()


@pytest.mark.parametrize("case", [
    "running_repair", "other_output_file", "missing_processing",
    "missing_bytes", "input_purpose", "releasable_file", "promoted_file",
])
def test_promotion_without_repair_success_rolls_back(
    tmp_path: Path, case: str,
) -> None:
    """修复未成功、字节不完整或文件不属修复输出：整组回滚。"""
    owned = promotion_environment(tmp_path)
    connection = owned.connection
    try:
        if case == "running_repair":
            connection.execute(
                "UPDATE recording_processing SET repair_state=4, repair_output_file_id=NULL"
                " WHERE id = 1")
        elif case == "other_output_file":
            connection.execute(
                "UPDATE recording_processing SET repair_output_file_id=999 WHERE id = 1")
        elif case == "missing_processing":
            connection.execute("DELETE FROM recording_processing WHERE id = 1")
        elif case == "missing_bytes":
            connection.execute(
                "UPDATE intermediate_files SET sha256=NULL WHERE id = 21")
        elif case == "input_purpose":
            connection.execute(
                "UPDATE intermediate_files SET purpose=2 WHERE id = 21")
        elif case == "releasable_file":
            connection.execute(
                "UPDATE intermediate_files SET retention_state=2, cleanup_state=2"
                " WHERE id = 21")
        else:
            connection.execute(
                "UPDATE intermediate_files SET retention_state=3 WHERE id = 21")
        connection.commit()
        before = tuple(connection.iterdump())
        outcome = _finish_with_repair(owned)
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK, outcome.value
        assert tuple(connection.iterdump()) == before
    finally:
        connection.close()


# ---- 守卫直接核对：提升不能脱离先行登记 ----


def _proposal(owned, drafts=None):
    """取出完成事务的事件提案而不提交。"""
    connection = owned.connection
    connection.execute("BEGIN")
    try:
        from camctl.persistence.repositories.capture import FinishCaptureCommand
        plan = FinishCaptureCommand(
            _finish(drafts=drafts if drafts is not None
                    else (_repaired_draft(), _original_draft(11))),
            new_operation_key(),
        ).plan(TransactionScope(connection, 1, 1))
    finally:
        connection.rollback()
    assert plan.events, "提案必须产生事件"
    return plan


def _validate_without_registration(owned, plan):
    """剔除 REPAIRED 登记事件后逐事件校验：提升事件必须被拒绝。"""
    filtered = tuple(
        event for event in plan.events
        if not (event.event_type == 20 and event.reason == 2)
    )
    assert len(filtered) < len(plan.events), "提案中必须存在 REPAIRED 登记事件"
    working = {
        table: {row_id: dict(rows) for row_id, rows in table_rows.items()}
        for table, table_rows in plan.state_rows.items()
    }
    seq = 0
    for event in filtered:
        if event_report_targets(event, working):
            seq += 1
            event = replace(event, change_seq=seq)
        context = EventContext(
            TransactionRange(
                filtered[0].transaction_id, filtered[0].event_id,
                filtered[-1].event_id),
            dict(plan.owners), working, read_coverage=plan.read_coverage)
        validate_event(event, context)
        for row in event.rows:
            if row.after.exists:
                working.setdefault(row.table, {}).setdefault(
                    row.row_id, {}).update(row.after.values)


def test_guard_rejects_promotion_without_registration(tmp_path: Path) -> None:
    """去掉登记事件的提升事务被守卫拒绝。"""
    owned = promotion_environment(tmp_path)
    try:
        plan = _proposal(owned)
        with pytest.raises(EventValidationError):
            _validate_without_registration(owned, plan)
    finally:
        owned.connection.close()

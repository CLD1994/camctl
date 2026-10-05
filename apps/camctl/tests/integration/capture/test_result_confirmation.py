"""C6 结果集合核实与延时完成释放的组件集成测试。

真实 SQLite 与 P3 事务内核组合：等待完成事实按 CAPTURE_WAIT_CHANGED
完成分支保存并引用自身事件；结果集合核实按 RESULT_SET_CONFIRMED 四
分支推进，时间与产物完成依据要求固定完成方式、可靠发送、等待完成
与满足的完整集合共同成立；依据成立后占用按统一释放判定解除，明确
不满足与无法确认不补造设备结束事实，不解除占用。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from camctl.capture.models import (
    ActivityReleaseSave,
    ResultSetPhase,
    ResultSetSave,
    WaitCompletedSave,
)
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import (
    CaptureRepository,
    ReleaseOutcome,
    register_capture_guards,
)
from camctl.persistence.repositories.timelapse import (
    TimelapseRepository,
    register_timelapse_guards,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..persistence.test_runtime import _create_valid_database
from ..scheduling.test_resources import _seed_activity, _seed_plan, _seed_record_action

register_capture_guards()
register_timelapse_guards()

pytestmark = pytest.mark.asyncio

_NOW = 1_750_000_000_000_000

_RESULT_CONTRACT = "task_scope_files"


def _environment(
    tmp_path: Path,
    *,
    action_type: int = 3,
    completion_mode: int = 2,
    scheduled: bool = True,
) -> object:
    """一个延时拍摄活动的最小事实：已成功发送并安排等待。"""
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
    _seed_record_action(connection, 1, 1)
    if action_type != 2:
        connection.execute("UPDATE actions SET type = ? WHERE id = 1", (action_type,))
    _seed_activity(connection, 1)
    connection.execute(
        "UPDATE device_activities SET completion_mode = ?, dispatch_state = 3,"
        " sent_at = ?, completion_basis = ?"
        + (", result_wait_margin_ms = 0, extra_wait_ms_used = 0,"
           " expected_check_at = ?" if scheduled else "")
        + " WHERE id = 1",
        ((completion_mode, _NOW, None if action_type == 2 else 1,
          _NOW + 600_000_000) if scheduled
         else (completion_mode, _NOW, None if action_type == 2 else 1)),
    )
    connection.commit()
    return owned


def _value(owned, sql: str, *params):
    row = owned.connection.execute(sql, params).fetchone()
    assert row is not None, f"查询无结果: {sql}"
    return row


def _complete_wait(owned) -> int:
    receipt = TimelapseRepository().complete_wait(
        WaitCompletedSave(action_id=1, occurred_at=_NOW),
        new_operation_key(), owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    return receipt.value.event_id


def _confirm(owned, command: ResultSetSave):
    return CaptureRepository().confirm_result_set(
        command, new_operation_key(), owned)


def _satisfied(evidence_wait_id: int) -> ResultSetSave:
    return ResultSetSave(
        action_id=1,
        occurred_at=_NOW,
        phase=ResultSetPhase.COMPLETE,
        contract=_RESULT_CONTRACT,
        observation={"files": ["clip-1"]},
        capture={"status": "completed"},
        evidence={
            "method": "time_and_outputs",
            "wait_completed_event_id": evidence_wait_id,
            "observation": {"files": ["clip-1"]},
        },
    )


class TestWaitCompleted:
    async def test_saves_self_referencing_event_once(self, tmp_path: Path) -> None:
        """等待完成引用自身事件；同键重送恢复首次结果，新键拒绝。"""
        owned = _environment(tmp_path)
        try:
            command = WaitCompletedSave(action_id=1, occurred_at=_NOW)
            key = new_operation_key()
            first = TimelapseRepository().complete_wait(
                command, key, owned)
            assert first.kind is DbOutcomeKind.COMPLETED
            event_id = first.value.event_id
            row = _value(
                owned,
                "SELECT wait_completed_event_id FROM device_activities WHERE id = 1")
            assert row[0] == event_id
            event = _value(
                owned,
                "SELECT event_type, json_extract(body_json, '$.reason')"
                " FROM history_events WHERE id = ?", event_id)
            assert event == (15, 3)
            resent = TimelapseRepository().complete_wait(command, key, owned)
            assert resent.kind is DbOutcomeKind.COMPLETED
            assert resent.value.event_id == event_id
            again = TimelapseRepository().complete_wait(
                command, new_operation_key(), owned)
            assert again.kind is DbOutcomeKind.ROLLED_BACK
            assert "已保存" in str(again.error)
        finally:
            owned.connection.close()

    async def test_requires_saved_schedule(self, tmp_path: Path) -> None:
        """没有预计检查时间的活动不能保存等待完成。"""
        owned = _environment(tmp_path, scheduled=False)
        try:
            outcome = TimelapseRepository().complete_wait(
                WaitCompletedSave(action_id=1, occurred_at=_NOW),
                new_operation_key(), owned)
            assert outcome.kind is DbOutcomeKind.ROLLED_BACK
            assert _value(
                owned,
                "SELECT wait_completed_event_id FROM device_activities"
                " WHERE id = 1")[0] is None
        finally:
            owned.connection.close()


class TestResultSetConfirmation:
    async def test_begin_moves_unexamined_to_checking(self, tmp_path: Path) -> None:
        """开始分支只推进核实状态，不携带结论事实。"""
        owned = _environment(tmp_path)
        try:
            outcome = _confirm(owned, ResultSetSave(
                action_id=1, occurred_at=_NOW, phase=ResultSetPhase.BEGIN))
            assert outcome.kind is DbOutcomeKind.COMPLETED
            assert outcome.value.result_set_state == 2
            row = _value(
                owned,
                "SELECT result_set_state, result_check_json, capture_json,"
                " completion_basis FROM device_activities WHERE id = 1")
            assert row == (2, None, None, 1)
            event = _value(
                owned,
                "SELECT event_type, json_extract(body_json, '$.reason'),"
                " body_json FROM history_events"
                " WHERE id = (SELECT MAX(id) FROM history_events)")
            assert (event[0], event[1]) == (16, 4)
            body = json.loads(event[2])
            assert set(body["rows"][0]["after"]["values"]) == {"result_set_state"}
        finally:
            owned.connection.close()

    async def test_satisfied_saves_time_and_outputs_basis_then_releases(
            self, tmp_path: Path) -> None:
        """满足结论保存完整依据；随后统一释放判定解除占用。"""
        owned = _environment(tmp_path)
        try:
            wait_id = _complete_wait(owned)
            outcome = _confirm(owned, _satisfied(wait_id))
            assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
            assert outcome.value.result_set_state == 3
            assert outcome.value.completion_basis == 3
            row = _value(
                owned,
                "SELECT result_set_state, result_check_json, capture_json,"
                " completion_basis, completion_evidence_json, occupancy_state"
                " FROM device_activities WHERE id = 1")
            assert row[0] == 3
            assert json.loads(row[1]) == {
                "contract": _RESULT_CONTRACT, "outcome": 1,
                "observation": {"files": ["clip-1"]}}
            assert json.loads(row[2]) == {"status": "completed"}
            assert row[3] == 3
            evidence = json.loads(row[4])
            assert evidence["method"] == "time_and_outputs"
            assert evidence["wait_completed_event_id"] == wait_id
            assert row[5] == 1  # 核实事务本身不释放占用
            release = CaptureRepository().release_occupancy(
                ActivityReleaseSave(action_id=1, occurred_at=_NOW),
                new_operation_key(), owned)
            assert release.kind is DbOutcomeKind.COMPLETED
            assert release.value.outcome is ReleaseOutcome.RELEASED
            assert _value(
                owned,
                "SELECT occupancy_state FROM device_activities WHERE id = 1")[0] == 2
        finally:
            owned.connection.close()

    async def test_satisfied_from_checking_round(self, tmp_path: Path) -> None:
        """已开始核实（CHECKING）的轮次可以直接保存满足结论。"""
        owned = _environment(tmp_path)
        try:
            assert _confirm(owned, ResultSetSave(
                action_id=1, occurred_at=_NOW,
                phase=ResultSetPhase.BEGIN)).kind is DbOutcomeKind.COMPLETED
            wait_id = _complete_wait(owned)
            outcome = _confirm(owned, _satisfied(wait_id))
            assert outcome.kind is DbOutcomeKind.COMPLETED
            assert _value(
                owned,
                "SELECT result_set_state FROM device_activities"
                " WHERE id = 1")[0] == 3
        finally:
            owned.connection.close()

    async def test_time_and_outputs_requires_wait_fact(self, tmp_path: Path) -> None:
        """缺少等待完成事实时不能满足结论不得保存完成依据。"""
        owned = _environment(tmp_path)
        try:
            outcome = _confirm(owned, _satisfied(1))
            assert outcome.kind is DbOutcomeKind.ROLLED_BACK
            row = _value(
                owned,
                "SELECT result_set_state, completion_basis, capture_json"
                " FROM device_activities WHERE id = 1")
            assert row == (1, 1, None)
        finally:
            owned.connection.close()

    async def test_time_and_outputs_requires_matching_wait_reference(
            self, tmp_path: Path) -> None:
        """完成依据引用的等待事件必须与本活动保存的事实一致。"""
        owned = _environment(tmp_path)
        try:
            _complete_wait(owned)
            outcome = _confirm(owned, _satisfied(1))
            assert outcome.kind is DbOutcomeKind.ROLLED_BACK
        finally:
            owned.connection.close()

    async def test_time_and_outputs_rejects_device_evidence_mode(
            self, tmp_path: Path) -> None:
        """固定完成方式为设备证据的活动不能按时间与产物判定。"""
        owned = _environment(tmp_path, completion_mode=1)
        try:
            wait_id = _complete_wait(owned)
            outcome = _confirm(owned, _satisfied(wait_id))
            assert outcome.kind is DbOutcomeKind.ROLLED_BACK
        finally:
            owned.connection.close()

    async def test_unsatisfied_saves_known_failure_and_keeps_occupancy(
            self, tmp_path: Path) -> None:
        """明确不满足保存已知失败判定；没有设备结束事实不解除占用。"""
        owned = _environment(tmp_path)
        try:
            _complete_wait(owned)
            outcome = _confirm(owned, ResultSetSave(
                action_id=1, occurred_at=_NOW, phase=ResultSetPhase.UNSATISFIED,
                contract=_RESULT_CONTRACT,
                observation={"missing": ["video"]},
                capture={"status": "failed",
                         "error": {"code": "capture_unsatisfied"}},
                evidence={
                    "method": "known_failure",
                    "observation": {"missing": ["video"]},
                }))
            assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
            row = _value(
                owned,
                "SELECT result_set_state, result_check_json, capture_json,"
                " completion_basis, occupancy_state FROM device_activities"
                " WHERE id = 1")
            assert row[0] == 3
            assert json.loads(row[1])["outcome"] == 2
            assert json.loads(row[2])["status"] == "failed"
            assert row[3] == 4
            release = CaptureRepository().release_occupancy(
                ActivityReleaseSave(action_id=1, occurred_at=_NOW),
                new_operation_key(), owned)
            assert release.kind is DbOutcomeKind.COMPLETED
            assert release.value.outcome is ReleaseOutcome.REJECTED
            assert release.value.reason == "conditions_unmet"
            assert row[4] == 1
        finally:
            owned.connection.close()

    async def test_unconfirmed_marks_unexamined_capture(
            self, tmp_path: Path) -> None:
        """无法确认保存结论与未知采集事实，判定保持待定。"""
        owned = _environment(tmp_path)
        try:
            _complete_wait(owned)
            outcome = _confirm(owned, ResultSetSave(
                action_id=1, occurred_at=_NOW,
                phase=ResultSetPhase.UNCONFIRMED,
                contract=_RESULT_CONTRACT,
                observation={"reason": "listing_failed"},
                capture={"status": "unconfirmed",
                         "error": {"code": "result_unconfirmed"}},
                error={"code": "result_unconfirmed"},
            ))
            assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
            row = _value(
                owned,
                "SELECT result_set_state, result_check_json, capture_json,"
                " completion_basis, completion_evidence_json, last_error_json"
                " FROM device_activities WHERE id = 1")
            assert row[0] == 4
            assert json.loads(row[1])["outcome"] == 3
            assert json.loads(row[2])["status"] == "unconfirmed"
            assert row[3] == 1
            assert row[4] is None
            assert json.loads(row[5]) == {"code": "result_unconfirmed"}
        finally:
            owned.connection.close()

    async def test_recording_action_not_applicable(self, tmp_path: Path) -> None:
        """录像活动不适用结果集合核实与采集判定列。"""
        owned = _environment(tmp_path, action_type=2)
        try:
            outcome = _confirm(owned, ResultSetSave(
                action_id=1, occurred_at=_NOW, phase=ResultSetPhase.BEGIN))
            assert outcome.kind is DbOutcomeKind.ROLLED_BACK
            assert "录像" in str(outcome.error)
        finally:
            owned.connection.close()

    async def test_terminal_result_set_rejects_further_rounds(
            self, tmp_path: Path) -> None:
        """已确定（COMPLETE/UNCONFIRMED）的集合不再接受新分支。"""
        owned = _environment(tmp_path)
        try:
            wait_id = _complete_wait(owned)
            assert _confirm(owned, _satisfied(wait_id)).kind is DbOutcomeKind.COMPLETED
            again = _confirm(owned, _satisfied(wait_id))
            assert again.kind is DbOutcomeKind.ROLLED_BACK
        finally:
            owned.connection.close()

    async def test_same_key_resend_recovers_first_result(
            self, tmp_path: Path) -> None:
        """同键重送核实原事务输入后恢复首次结果。"""
        owned = _environment(tmp_path)
        repository = CaptureRepository()
        try:
            wait_id = _complete_wait(owned)
            command = _satisfied(wait_id)
            key = new_operation_key()
            first = repository.confirm_result_set(command, key, owned)
            assert first.kind is DbOutcomeKind.COMPLETED
            resent = repository.confirm_result_set(command, key, owned)
            assert resent.kind is DbOutcomeKind.COMPLETED
            assert resent.value.result_set_state == 3
            assert resent.value.completion_basis == 3
            mismatched = repository.confirm_result_set(
                ResultSetSave(
                    action_id=1, occurred_at=_NOW,
                    phase=ResultSetPhase.UNSATISFIED,
                    contract=_RESULT_CONTRACT, observation={},
                    evidence={"method": "known_failure",
                              "observation": {}}),
                key, owned)
            assert mismatched.kind is DbOutcomeKind.ROLLED_BACK
            assert "操作身份" in str(mismatched.error)
        finally:
            owned.connection.close()

    async def test_capture_status_must_match_phase(self, tmp_path: Path) -> None:
        """采集状态与结论分支不符属于输入错误。"""
        owned = _environment(tmp_path)
        try:
            wait_id = _complete_wait(owned)
            with pytest.raises(ValueError):
                _confirm(owned, ResultSetSave(
                    action_id=1, occurred_at=_NOW, phase=ResultSetPhase.COMPLETE,
                    contract=_RESULT_CONTRACT, observation={},
                    capture={"status": "failed",
                             "error": {"code": "capture_unsatisfied"}},
                    evidence={"method": "time_and_outputs",
                              "wait_completed_event_id": wait_id,
                              "observation": {}}))
        finally:
            owned.connection.close()

    async def test_conclusion_requires_contract_and_observation(
            self, tmp_path: Path) -> None:
        """结论分支必须携带驱动结果规则标识与结构化依据。"""
        owned = _environment(tmp_path)
        try:
            with pytest.raises(ValueError):
                _confirm(owned, ResultSetSave(
                    action_id=1, occurred_at=_NOW, phase=ResultSetPhase.COMPLETE,
                    evidence={"method": "time_and_outputs",
                              "wait_completed_event_id": 1,
                              "observation": {}}))
        finally:
            owned.connection.close()

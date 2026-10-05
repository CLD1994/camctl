"""C6 结果集合核实与延时完成释放的组件集成测试。

真实 SQLite 与 P3 事务内核组合：等待完成事实按 CAPTURE_WAIT_CHANGED
完成分支保存并引用自身事件；结果集合核实按 RESULT_SET_CONFIRMED 四
分支推进，时间与产物完成依据要求固定完成方式、可靠发送、等待完成
与满足的完整集合共同成立；依据成立后占用按统一释放判定解除，明确
不满足与无法确认不补造设备结束事实，不解除占用。
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.capture.models import (
    ActivityReleaseSave,
    ResultSetPhase,
    ResultSetSave,
    WaitCompletedSave,
)
from camctl.capture.handlers import CaptureRuntime, capture_handler
from camctl.contracts.values import new_operation_key
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
from camctl.operations.attempts import (
    AttemptConfig,
    AttemptFinish,
    AttemptIntent,
    AttemptTarget,
    FinishDisposition,
    OperationKind,
    RunFinish,
    RunOutcome,
)
from camctl.operations.models import (
    AttemptStatus,
    CallOutcome,
    EffectState,
    EvidenceValue,
    Settlement,
    SettlementBasis,
)
from camctl.operations.validation import validate_outcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import (
    CaptureRepository,
    ReleaseOutcome,
    register_capture_guards,
)
from camctl.persistence.repositories.operations import (
    OperationRepository,
    register_operation_guards,
)
from camctl.persistence.repositories.scheduling import SchedulingRepository
from camctl.persistence.repositories.timelapse import (
    TimelapseRepository,
    register_timelapse_guards,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from .test_capture_contract import ResultsDouble, _entry
from ..persistence.test_runtime import _create_valid_database
from ..scheduling.test_resources import _seed_activity, _seed_plan, _seed_record_action

register_capture_guards()
register_operation_guards()
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


#: 核实轮次尝试结果的证据登记：result 操作的可靠返回契约。
_CHECK_EVIDENCE = EvidenceRegistry(
    (EvidenceContract(type="results_returned", version=1, operation="result",
                      fields=frozenset()),))

_CHECK_CONFIG = AttemptConfig(
    max_attempts=3, timeout_s=Decimal("10"), retry_interval_s=Decimal("3"))


def _check_ticket(owned):
    """授予一轮结果核实尝试并返回票据。"""
    outcome = OperationRepository().begin_attempt(
        AttemptIntent(
            operation="result", action_id=1,
            kind=OperationKind.CHECK_CAPTURE_RESULTS,
            target=AttemptTarget(activity_id=1),
            query_purpose=None, config=_CHECK_CONFIG, occurred_at=_NOW),
        new_operation_key(), owned)
    assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
    assert outcome.value.disposition.value == "granted"
    return outcome.value.ticket


def _round_finish(ticket, *, retry_wait: bool = False) -> AttemptFinish:
    outcome = validate_outcome(ticket, CallOutcome(
        status=AttemptStatus.SUCCEEDED, error=None, effect=EffectState.UNKNOWN,
        settlement=Settlement(
            basis=SettlementBasis.OBSERVED,
            evidence=EvidenceValue(type="results_returned", version=1, data={}),
        )), _CHECK_EVIDENCE)
    return AttemptFinish(
        ticket=ticket, outcome=outcome, occurred_at=_NOW,
        retry_wait=retry_wait,
        run_finish=None if retry_wait else RunFinish(status=RunOutcome.SUCCEEDED))


class TestFinishResultCheck:
    async def test_conclusion_commits_with_attempt_and_run_end(
            self, tmp_path: Path) -> None:
        """结论与尝试结果、流程结束同一事务提交。"""
        owned = _environment(tmp_path)
        try:
            wait_id = _complete_wait(owned)
            ticket = _check_ticket(owned)
            outcome = CaptureRepository().finish_result_check(
                _round_finish(ticket), _satisfied(wait_id),
                new_operation_key(), owned)
            assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
            assert outcome.value.finish.disposition is FinishDisposition.SAVED
            assert outcome.value.finish.attempt_status is AttemptStatus.SUCCEEDED
            assert outcome.value.result_set.result_set_state == 3
            assert outcome.value.result_set.completion_basis == 3
            run = _value(
                owned,
                "SELECT status, attempts_used, retry_wait_required"
                " FROM operation_runs WHERE responsibility_key = 'results/1'")
            assert run == (3, 1, 0)
            events = owned.connection.execute(
                "SELECT transaction_id, event_type,"
                " json_extract(body_json, '$.reason') FROM history_events"
                " WHERE event_type IN (12, 10, 16) ORDER BY id").fetchall()
            assert [row[1] for row in events] == [12, 10, 16]
            assert len({row[0] for row in events}) == 1
            assert events[2][2] == 1  # RESULT_SET_CONFIRMED.COMPLETE
        finally:
            owned.connection.close()

    async def test_atomic_rejection_keeps_attempt_running(
            self, tmp_path: Path) -> None:
        """结论输入被拒时尝试与流程保持原状，不留半提交事实。"""
        owned = _environment(tmp_path)
        try:
            ticket = _check_ticket(owned)
            outcome = CaptureRepository().finish_result_check(
                _round_finish(ticket), _satisfied(1),
                new_operation_key(), owned)
            assert outcome.kind is DbOutcomeKind.ROLLED_BACK
            row = _value(
                owned,
                "SELECT r.status, r.attempts_used, a.status"
                " FROM operation_runs r JOIN operation_attempts a"
                " ON a.run_id = r.id"
                " WHERE r.responsibility_key = 'results/1'")
            assert row == (2, 1, 1)
            assert _value(
                owned,
                "SELECT result_set_state FROM device_activities"
                " WHERE id = 1")[0] == 1
        finally:
            owned.connection.close()

    async def test_same_key_resend_recovers_first_result(
            self, tmp_path: Path) -> None:
        """同键重送恢复首次的尝试结束与核实结论。"""
        owned = _environment(tmp_path)
        repository = CaptureRepository()
        try:
            wait_id = _complete_wait(owned)
            ticket = _check_ticket(owned)
            finish = _round_finish(ticket)
            confirm = _satisfied(wait_id)
            key = new_operation_key()
            first = repository.finish_result_check(finish, confirm, key, owned)
            assert first.kind is DbOutcomeKind.COMPLETED
            resent = repository.finish_result_check(finish, confirm, key, owned)
            assert resent.kind is DbOutcomeKind.COMPLETED
            assert resent.value.result_set.result_set_state == 3
            mismatched = repository.finish_result_check(
                finish,
                ResultSetSave(
                    action_id=1, occurred_at=_NOW, phase=ResultSetPhase.COMPLETE,
                    contract=_RESULT_CONTRACT,
                    observation={"files": ["other-clip"]},
                    capture={"status": "completed"},
                    evidence={
                        "method": "time_and_outputs",
                        "wait_completed_event_id": wait_id,
                        "observation": {"files": ["other-clip"]},
                    }),
                key, owned)
            assert mismatched.kind is DbOutcomeKind.ROLLED_BACK
            assert "重送" in str(mismatched.error)
        finally:
            owned.connection.close()

    async def test_rejects_when_attempt_already_ended(
            self, tmp_path: Path) -> None:
        """已结束的尝试不能再携带结论提交。"""
        owned = _environment(tmp_path)
        try:
            ticket = _check_ticket(owned)
            assert OperationRepository().finish_attempt(
                _round_finish(ticket, retry_wait=True),
                new_operation_key(), owned).kind is DbOutcomeKind.COMPLETED
            wait_id = _complete_wait(owned)
            outcome = CaptureRepository().finish_result_check(
                _round_finish(ticket), _satisfied(wait_id),
                new_operation_key(), owned)
            assert outcome.kind is DbOutcomeKind.ROLLED_BACK
        finally:
            owned.connection.close()


class TestCloseResultCheckUnconfirmed:
    def _pending_round(self, owned) -> None:
        """一轮暂不齐备的核实：尝试成功结束并建立重试等待。"""
        ticket = _check_ticket(owned)
        assert OperationRepository().finish_attempt(
            _round_finish(ticket, retry_wait=True),
            new_operation_key(), owned).kind is DbOutcomeKind.COMPLETED

    def _unconfirmed(self) -> ResultSetSave:
        return ResultSetSave(
            action_id=1, occurred_at=_NOW, phase=ResultSetPhase.UNCONFIRMED,
            contract=_RESULT_CONTRACT,
            observation={"reason": "attempts_exhausted"},
            capture={"status": "unconfirmed",
                     "error": {"code": "result_unconfirmed"}},
            error={"code": "result_unconfirmed"},
        )

    async def test_budget_exhausted_closes_run_and_result_set(
            self, tmp_path: Path) -> None:
        """预算耗尽的收场把流程与集合结论同事务置为无法确认。"""
        owned = _environment(tmp_path)
        try:
            _complete_wait(owned)
            self._pending_round(owned)
            outcome = CaptureRepository().close_result_check_unconfirmed(
                self._unconfirmed(), new_operation_key(), owned)
            assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
            run = _value(
                owned,
                "SELECT status, retry_wait_required, error_json IS NOT NULL"
                " FROM operation_runs WHERE responsibility_key = 'results/1'")
            assert run == (6, 0, 1)
            row = _value(
                owned,
                "SELECT result_set_state, completion_basis,"
                " completion_evidence_json, json_extract(capture_json, '$.status'),"
                " json_extract(last_error_json, '$.code')"
                " FROM device_activities WHERE id = 1")
            assert row == (4, 1, None, "unconfirmed", "result_unconfirmed")
            events = owned.connection.execute(
                "SELECT transaction_id, event_type FROM history_events"
                " WHERE id > 1 ORDER BY id").fetchall()
            assert [row[1] for row in events[-2:]] == [10, 16]
            assert events[-1][0] == events[-2][0]
        finally:
            owned.connection.close()

    async def test_same_key_resend_recovers_close(
            self, tmp_path: Path) -> None:
        """同键重送恢复首次的收场结果，不同输入拒绝。"""
        owned = _environment(tmp_path)
        repository = CaptureRepository()
        try:
            _complete_wait(owned)
            self._pending_round(owned)
            command = self._unconfirmed()
            key = new_operation_key()
            first = repository.close_result_check_unconfirmed(
                command, key, owned)
            assert first.kind is DbOutcomeKind.COMPLETED
            resent = repository.close_result_check_unconfirmed(
                command, key, owned)
            assert resent.kind is DbOutcomeKind.COMPLETED
            assert resent.value.result_set_state == 4
            other = repository.close_result_check_unconfirmed(
                ResultSetSave(
                    action_id=1, occurred_at=_NOW,
                    phase=ResultSetPhase.UNCONFIRMED,
                    contract=_RESULT_CONTRACT,
                    observation={"reason": "other"},
                    capture={"status": "unconfirmed",
                             "error": {"code": "result_unconfirmed"}},
                    error={"code": "result_unconfirmed"},
                ), key, owned)
            assert other.kind is DbOutcomeKind.ROLLED_BACK
            assert "重送" in str(other.error)
        finally:
            owned.connection.close()


class TestConclusionRecovery:
    """结论已保存而动作未收场的中断窗口：用原结果完成收尾。"""

    def _concluded(self, tmp_path: Path, *, result_set_state: int,
                   completion_basis: int, capture: str) -> object:
        owned = _environment(tmp_path)
        outcome = 1 if result_set_state == 3 else 3
        evidence = (json.dumps({
            "method": "time_and_outputs",
            "observation": {"files": ["sequence-1"]},
        }) if completion_basis == 3 else None)
        owned.connection.execute(
            "UPDATE device_activities SET result_set_state = ?,"
            " completion_basis = ?, capture_json = ?, result_check_json = ?,"
            " completion_evidence_json = ?, wait_completed_event_id = 1"
            " WHERE id = 1",
            (result_set_state, completion_basis, capture,
             json.dumps({"contract": _RESULT_CONTRACT, "outcome": outcome,
                         "observation": {"files": ["sequence-1"]}}),
             evidence))
        # 已确认的启动事实：处理器不再发起启动调用。
        owned.connection.execute(
            "INSERT INTO operation_runs (id, action_id, delivery_id, kind,"
            " query_purpose, responsibility_key, activity_id, copy_id,"
            " cleanup_item_id, session_key, status, attempts_used,"
            " max_attempts_used, timeout_s_json, retry_interval_s_json,"
            " retry_wait_required, error_json)"
            " VALUES (50, 1, NULL, 1, NULL, 'start/1', 1, NULL, NULL, NULL,"
            " 3, 1, 1, '10', '1', 0, NULL)")
        owned.connection.execute(
            "INSERT INTO operation_attempts (id, run_id, attempt_no, status,"
            " intent_event_id, result_event_id, max_attempts_used, effect_state,"
            " result_json) VALUES (51, 50, 1, 2, 1, 1, 1, 3, '{}')")
        owned.connection.commit()
        return owned

    async def _advance(self, owned, files: dict) -> None:
        from camctl.capture.timelapse import CaptureWaitConfig
        from camctl.scheduling.rules import LaunchWindow

        runtime = CaptureRuntime(
            owned=owned,
            scheduling=SchedulingRepository(),
            operations=OperationRepository(),
            capture=CaptureRepository(),
            timelapse=TimelapseRepository(),
            driver=None,
            results=ResultsDouble(files),
            evidence=EvidenceRegistry(()),
            wall_us=lambda: _NOW + 700_000_000,
            monotonic_ns=lambda: 5_000_000_000,
            window_of=lambda action: LaunchWindow(
                scheduled_at=action["scheduled_at"],
                window_end=action["scheduled_at"] + action["max_delay_ms"] * 1000),
            wait_config=lambda params: CaptureWaitConfig(
                target_duration_ms=600_000, driver_margin_ms=0),
        )
        await capture_handler("camera_timelapse")(1, runtime)

    async def test_satisfied_conclusion_finishes_without_new_round(
            self, tmp_path: Path) -> None:
        """满足结论保存后的中断：直接收尾，不重开核实轮次。"""
        owned = self._concluded(
            tmp_path, result_set_state=3, completion_basis=3,
            capture='{"status": "completed"}')
        try:
            await self._advance(owned, {1: (_entry("sequence-1"),)})
            assert _value(
                owned, "SELECT status FROM actions WHERE id = 1") == (3,)
            assert _value(
                owned, "SELECT occupancy_state FROM device_activities"
                " WHERE id = 1") == (2,)
            assert _value(
                owned, "SELECT COUNT(*) FROM outputs"
                " WHERE source_action_id = 1") == (1,)
            assert _value(
                owned, "SELECT COUNT(*) FROM operation_runs"
                " WHERE responsibility_key = 'results/1'") == (0,)
        finally:
            owned.connection.close()

    async def test_unconfirmed_conclusion_fails_with_registered_error(
            self, tmp_path: Path) -> None:
        """无法确认结论保存后的中断：按登记错误收场失败终态。"""
        owned = self._concluded(
            tmp_path, result_set_state=4, completion_basis=1,
            capture='{"status": "unconfirmed",'
                    ' "error": {"code": "result_unconfirmed"}}')
        try:
            await self._advance(owned, {1: (_entry("sequence-1"),)})
            failure = _value(
                owned,
                "SELECT status, error_code,"
                " json_extract(error_details_json, '$.reason')"
                " FROM actions WHERE id = 1")
            assert failure == (4, 12, "outputs_unknown")
            # 失败仍保留已列举的完整且归属明确的文件。
            assert _value(
                owned, "SELECT COUNT(*) FROM outputs") == (1,)
            assert _value(
                owned, "SELECT occupancy_state FROM device_activities"
                " WHERE id = 1") == (1,)
        finally:
            owned.connection.close()


class TestInterruptedRoundParks:
    """中断遗留的在途核实轮次：停等跨会话恢复，不提交新意图。"""

    async def test_running_round_waits_without_new_attempt(
            self, tmp_path: Path) -> None:
        owned = _environment(tmp_path)
        # 已确认的启动事实：处理器不再发起启动调用。
        owned.connection.execute(
            "INSERT INTO operation_runs (id, action_id, delivery_id, kind,"
            " query_purpose, responsibility_key, activity_id, copy_id,"
            " cleanup_item_id, session_key, status, attempts_used,"
            " max_attempts_used, timeout_s_json, retry_interval_s_json,"
            " retry_wait_required, error_json)"
            " VALUES (50, 1, NULL, 1, NULL, 'start/1', 1, NULL, NULL, NULL,"
            " 3, 1, 1, '10', '1', 0, NULL)")
        owned.connection.execute(
            "INSERT INTO operation_attempts (id, run_id, attempt_no, status,"
            " intent_event_id, result_event_id, max_attempts_used, effect_state,"
            " result_json) VALUES (51, 50, 1, 2, 1, 1, 1, 3, '{}')")
        owned.connection.execute(
            "INSERT INTO operation_runs (id, action_id, delivery_id, kind,"
            " query_purpose, responsibility_key, activity_id, copy_id,"
            " cleanup_item_id, session_key, status, attempts_used,"
            " max_attempts_used, timeout_s_json, retry_interval_s_json,"
            " retry_wait_required, error_json)"
            " VALUES (60, 1, NULL, 7, NULL, 'results/1', 1, NULL, NULL, NULL,"
            " 2, 1, 3, '10', '3', 0, NULL)")
        owned.connection.execute(
            "INSERT INTO operation_attempts (id, run_id, attempt_no, status,"
            " intent_event_id, result_event_id, max_attempts_used, effect_state,"
            " result_json) VALUES (61, 60, 1, 1, 1, NULL, 3, 1, NULL)")
        owned.connection.commit()
        recovery = TestConclusionRecovery()
        try:
            await recovery._advance(owned, {1: (_entry("sequence-1"),)})
            # 停等不产生新尝试与新事件，动作与集合保持原状。
            assert _value(
                owned, "SELECT status FROM actions WHERE id = 1") == (2,)
            assert _value(
                owned,
                "SELECT result_set_state FROM device_activities"
                " WHERE id = 1") == (1,)
            assert _value(
                owned,
                "SELECT attempts_used FROM operation_runs"
                " WHERE responsibility_key = 'results/1'") == (1,)
            assert _value(
                owned, "SELECT COUNT(*) FROM operation_attempts"
                " WHERE run_id = 60") == (1,)
        finally:
            owned.connection.close()

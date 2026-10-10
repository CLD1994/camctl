"""C7 应急最终补记的组件集成测试。

真实 SQLite 与 P3 事务内核组合：零尝试已知/未知配置、有尝试已
知配置按正式组合保存；有尝试未知上限、超限、普通意图引用与重
复补记均拒绝；同键重送按原事务核实不重开流程。
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.capture.recovery import (
    EmergencyBudget,
    EmergencyOutcome,
    EmergencyRecord,
    RecordStatus,
)
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import (
    ActivityReleaseSave,
    CaptureRepository,
    ReleaseOutcome,
    register_capture_guards,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import saved_transaction_events

from ..persistence.test_runtime import _create_valid_database
from ..scheduling.test_resources import _seed_activity, _seed_plan, _seed_record_action

register_capture_guards()

_NOW = 1_750_000_000_000_000
_SESSION = "a" * 32
_NOT_ATTEMPTED_REASON = "未可靠取得停止配置，未开始应急尝试"
_UNCONFIRMED_REASON = "有限停止处理已经结束，设备停止仍未确认"


def _environment(tmp_path: Path):
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
    _seed_activity(connection, 1, dispatch_state=2, activity_state=2)
    connection.commit()
    return owned


def _value(owned, sql: str, *params):
    row = owned.connection.execute(sql, params).fetchone()
    assert row is not None, f"查询无结果: {sql}"
    return row


def _attempt(status: int = 3, *, error=None, result=None) -> dict:
    if status == 3 and error is None:
        error = {"code": "stop_rejected", "stage": "driver"}
    return {
        "status": status,
        "effect_state": 2,
        "result_json": result if result is not None else {
            "format_version": 1,
            "settlement": {
                "basis": "observed",
                "evidence": {"type": "stop_confirmed", "version": 1, "data": {}},
            },
            "observations": [],
        },
        "error_json": error,
    }


def _save(owned, record, attempts, *, timeout=Decimal("10"), interval=Decimal("1"),
          key=None):
    return CaptureRepository().save_emergency(
        session_key=_SESSION,
        action_id=1,
        activity_id=1,
        record=record,
        attempts=attempts,
        occurred_at=_NOW,
        key=key if key is not None else new_operation_key(),
        owned=owned,
        timeout_s=timeout,
        retry_interval_s=interval,
    )


def _stopped_result(activity_id: int = 1) -> dict:
    """携带指向目标活动停止观察的尝试结果；停止证据优先保存在结果中。"""
    return {
        "format_version": 1,
        "settlement": {
            "basis": "observed",
            "evidence": {"type": "stop_confirmed", "version": 1, "data": {}},
        },
        "observations": [{
            "type": "stop_confirmed",
            "version": 1,
            "data": {"activity_id": str(activity_id)},
        }],
    }


def _stop_attempt(**overrides) -> dict:
    attempt = _attempt(result=_stopped_result())
    attempt.update(overrides)
    return attempt


def _stop_observation(activity_id: int = 1) -> dict:
    return {"type": "stop_confirmed", "version": 1,
            "data": {"activity_id": str(activity_id)}}


pytestmark = pytest.mark.asyncio


async def test_stopped_with_attempts_records_final_flow(tmp_path: Path) -> None:
    """有尝试已知配置的停止成功：终态流程+尝试+活动结束共同保存。"""
    owned = _environment(tmp_path)
    try:
        record = EmergencyRecord(
            outcome=EmergencyOutcome.STOPPED,
            attempts_used=2,
            max_attempts=3,
        )
        outcome = _save(
            owned, record, (_stop_attempt(), _stop_attempt())
        )
        assert outcome.kind is DbOutcomeKind.COMPLETED
        assert outcome.value.record_status is RecordStatus.RECORDED

        run = _value(
            owned,
            "SELECT kind, status, attempts_used, max_attempts_used, error_json,"
            " responsibility_key FROM operation_runs WHERE id = ?",
            outcome.value.run_id,
        )
        assert run[0] == 9 and run[1] == 3 and run[2] == 2
        assert run[3] == 3 and run[4] is None
        assert run[5] == f"emergency/{_SESSION}/1"
        rows = owned.connection.execute(
            "SELECT attempt_no, intent_event_id, result_event_id FROM"
            " operation_attempts WHERE run_id = ? ORDER BY attempt_no",
            (outcome.value.run_id,),
        ).fetchall()
        assert [row[0] for row in rows] == [1, 2]
        assert all(row[1] is None for row in rows)
        assert all(row[2] is not None for row in rows)
        activity = _value(
            owned, "SELECT activity_state, occupancy_state FROM device_activities WHERE id = 1"
        )
        assert activity[0] == 3  # ENDED
        assert activity[1] == 1  # 应急不自动释放占用
    finally:
        owned.connection.close()


async def test_zero_attempts_unknown_config_saves_not_attempted(tmp_path: Path) -> None:
    """零尝试且配置未知：FAILED + not_attempted 原因，配置允许为空。"""
    owned = _environment(tmp_path)
    try:
        record = EmergencyRecord(
            outcome=EmergencyOutcome.NOT_ATTEMPTED,
            attempts_used=0,
            max_attempts=3,
            reason=_NOT_ATTEMPTED_REASON,
        )
        outcome = CaptureRepository().save_emergency(
            session_key=_SESSION,
            action_id=1,
            activity_id=1,
            record=record,
            attempts=(),
            occurred_at=_NOW,
            key=new_operation_key(),
            owned=owned,
            timeout_s=None,
            retry_interval_s=None,
        )
        assert outcome.kind is DbOutcomeKind.COMPLETED
        run = _value(
            owned,
            "SELECT status, attempts_used, max_attempts_used, error_json"
            " FROM operation_runs WHERE id = ?",
            outcome.value.run_id,
        )
        assert run[0] == 4 and run[1] == 0 and run[2] == 3
        assert json.loads(run[3])["code"] == "emergency_not_attempted"
        activity = _value(
            owned, "SELECT activity_state FROM device_activities WHERE id = 1"
        )
        assert activity[0] == 2  # 未尝试不结束活动
    finally:
        owned.connection.close()


async def test_attempted_unknown_config_is_rejected(tmp_path: Path) -> None:
    """有尝试但配置未知：拒绝。"""
    owned = _environment(tmp_path)
    try:
        record = EmergencyRecord(
            outcome=EmergencyOutcome.STOPPED, attempts_used=1, max_attempts=3
        )
        outcome = CaptureRepository().save_emergency(
            session_key=_SESSION,
            action_id=1,
            activity_id=1,
            record=record,
            attempts=(_attempt(),),
            occurred_at=_NOW,
            key=new_operation_key(),
            owned=owned,
            timeout_s=None,
            retry_interval_s=None,
        )
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK
        assert _value(owned, "SELECT COUNT(*) FROM operation_runs")[0] == 0
    finally:
        owned.connection.close()


async def test_over_limit_and_count_mismatch_are_rejected(tmp_path: Path) -> None:
    """超限与行数不符：整组拒绝。"""
    owned = _environment(tmp_path)
    try:
        over = EmergencyRecord(
            outcome=EmergencyOutcome.STOPPED, attempts_used=4, max_attempts=3
        )
        assert _save(owned, over, (_attempt(),) * 4).kind is DbOutcomeKind.ROLLED_BACK
        mismatch = EmergencyRecord(
            outcome=EmergencyOutcome.STOPPED, attempts_used=1, max_attempts=3
        )
        assert _save(owned, mismatch, (_attempt(), _attempt())).kind is DbOutcomeKind.ROLLED_BACK
    finally:
        owned.connection.close()


async def test_duplicate_session_flow_is_rejected(tmp_path: Path) -> None:
    """同一会话同一活动只有一条应急流程；重复补记拒绝不重开。"""
    owned = _environment(tmp_path)
    try:
        record = EmergencyRecord(
            outcome=EmergencyOutcome.STOPPED, attempts_used=1, max_attempts=3
        )
        first = _save(owned, record, (_stop_attempt(),))
        assert first.kind is DbOutcomeKind.COMPLETED
        again = _save(owned, record, (_stop_attempt(),))
        assert again.kind is DbOutcomeKind.ROLLED_BACK
        assert _value(owned, "SELECT COUNT(*) FROM operation_runs WHERE kind = 9")[0] == 1
    finally:
        owned.connection.close()


@pytest.mark.parametrize("second_outcome", [EmergencyOutcome.NOT_ATTEMPTED, EmergencyOutcome.UNCONFIRMED])
async def test_later_session_preserves_exact_old_error_and_omits_unchanged_activity(tmp_path, second_outcome):
    from camctl.contracts.json_values import parse_exact_json

    owned = _environment(tmp_path)
    try:
        first = _save(owned, EmergencyRecord(
            EmergencyOutcome.NOT_ATTEMPTED, 0, 3, reason=_NOT_ATTEMPTED_REASON), ())
        assert first.kind is DbOutcomeKind.COMPLETED, first.error
        attempts = () if second_outcome is EmergencyOutcome.NOT_ATTEMPTED else (
            _attempt(status=4, error={"code": "timeout", "stage": "transport"}),)
        second = CaptureRepository().save_emergency(
            session_key="b" * 32, action_id=1, activity_id=1,
            record=EmergencyRecord(second_outcome, len(attempts), 3,
                reason=_NOT_ATTEMPTED_REASON if second_outcome is EmergencyOutcome.NOT_ATTEMPTED
                else _UNCONFIRMED_REASON), attempts=attempts,
            occurred_at=_NOW, key=new_operation_key(), owned=owned,
            timeout_s=Decimal("10"), retry_interval_s=Decimal("1"))
        assert second.kind is DbOutcomeKind.COMPLETED, second.error
        body = parse_exact_json(_value(owned, "SELECT body_json FROM history_events ORDER BY id DESC LIMIT 1")[0])
        activities = [row for row in body["rows"] if row["table"] == "device_activities"]
        assert _value(owned, "SELECT COUNT(*) FROM operation_runs WHERE kind = 9")[0] == 2
        if second_outcome is EmergencyOutcome.NOT_ATTEMPTED:
            assert activities == []
        else:
            assert len(activities) == 1
            assert activities[0]["before"]["values"] == {
                "last_error_json": {"code": "emergency_not_attempted", "stage": "emergency",
                    "details": {"activity_id": "1", "reason": _NOT_ATTEMPTED_REASON}}}
            assert activities[0]["after"]["values"] == {
                "last_error_json": {"code": "emergency_stop_unconfirmed", "stage": "emergency",
                    "details": {"activity_id": "1", "reason": _UNCONFIRMED_REASON}}}
    finally:
        owned.connection.close()


async def test_zero_attempt_stopped_requires_stop_observation(tmp_path: Path) -> None:
    """零尝试停止（首次发令前已确认）必须携带可靠停止依据。"""
    from camctl.contracts.json_values import parse_exact_json

    owned = _environment(tmp_path)
    try:
        missing = _save(
            owned,
            EmergencyRecord(EmergencyOutcome.STOPPED, 0, 3),
            (),
        )
        assert missing.kind is DbOutcomeKind.ROLLED_BACK
        assert _value(owned, "SELECT COUNT(*) FROM operation_runs WHERE kind = 9")[0] == 0

        wrong_target = _save(
            owned,
            EmergencyRecord(
                EmergencyOutcome.STOPPED, 0, 3,
                stop_observation=_stop_observation(activity_id=9)),
            (),
        )
        assert wrong_target.kind is DbOutcomeKind.ROLLED_BACK
        assert _value(owned, "SELECT COUNT(*) FROM operation_runs WHERE kind = 9")[0] == 0

        saved = _save(
            owned,
            EmergencyRecord(
                EmergencyOutcome.STOPPED, 0, 3,
                stop_observation=_stop_observation()),
            (),
        )
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        run = _value(
            owned, "SELECT status, attempts_used FROM operation_runs WHERE id = ?",
            saved.value.run_id)
        assert (run[0], run[1]) == (3, 0)
        body = parse_exact_json(_value(
            owned, "SELECT body_json FROM history_events ORDER BY id DESC LIMIT 1")[0])
        assert body["evidence"] == {"observation": _stop_observation()}
        activity = _value(owned, "SELECT activity_state FROM device_activities WHERE id = 1")
        assert activity[0] == 3  # 可靠停止依据支持活动结束
    finally:
        owned.connection.close()


async def test_stop_observation_only_with_stopped_outcome(tmp_path: Path) -> None:
    """停止依据只伴随停止成功；其他结果的补记拒绝携带。"""
    owned = _environment(tmp_path)
    try:
        outcome = _save(
            owned,
            EmergencyRecord(
                EmergencyOutcome.NOT_ATTEMPTED, 0, 3,
                stop_observation=_stop_observation(), reason=_NOT_ATTEMPTED_REASON),
            (),
        )
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK
        assert _value(owned, "SELECT COUNT(*) FROM operation_runs WHERE kind = 9")[0] == 0
    finally:
        owned.connection.close()


async def test_stopped_with_attempts_requires_stop_evidence_in_results(
        tmp_path: Path) -> None:
    """有尝试的停止成功：可靠停止证据保存在尝试结果中并指向目标活动。

    只保存调用成功或流程 SUCCEEDED 不能代替停止证据；结果观察指向
    其他活动同样不构成依据。
    """
    owned = _environment(tmp_path)
    try:
        record = EmergencyRecord(
            outcome=EmergencyOutcome.STOPPED, attempts_used=2, max_attempts=3
        )
        without_evidence = _save(owned, record, (_attempt(), _attempt(status=3)))
        assert without_evidence.kind is DbOutcomeKind.ROLLED_BACK
        assert _value(owned, "SELECT COUNT(*) FROM operation_runs WHERE kind = 9")[0] == 0

        wrong_target = _save(
            owned, record,
            (_attempt(result=_stopped_result(activity_id=9)), _attempt(status=3)),
        )
        assert wrong_target.kind is DbOutcomeKind.ROLLED_BACK
        assert _value(owned, "SELECT COUNT(*) FROM operation_runs WHERE kind = 9")[0] == 0
    finally:
        owned.connection.close()


async def test_saved_stop_observation_preserves_exact_decimal(
        tmp_path: Path) -> None:
    """补记事件的依据成员经真实 SQLite 保存再读出，精确小数不折叠。"""
    owned = _environment(tmp_path)
    try:
        observation = {
            "type": "stop_confirmed",
            "version": 1,
            "data": {
                "activity_id": "1",
                "measured": Decimal("0.10000000000000001"),
            },
        }
        key = new_operation_key()
        outcome = _save(
            owned,
            EmergencyRecord(
                EmergencyOutcome.STOPPED, 0, 3,
                stop_observation=observation),
            (),
            key=key,
        )
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        saved = saved_transaction_events(owned.connection, key)
        assert saved[0]["body"]["evidence"]["observation"]["data"][
            "measured"] == Decimal("0.10000000000000001")
    finally:
        owned.connection.close()


async def test_unconfirmed_with_attempts_saves_unconfirmed(tmp_path: Path) -> None:
    owned = _environment(tmp_path)
    try:
        record = EmergencyRecord(
            outcome=EmergencyOutcome.UNCONFIRMED, attempts_used=2, max_attempts=2,
            reason=_UNCONFIRMED_REASON,
        )
        outcome = _save(
            owned,
            record,
            (_attempt(status=4, error={"code": "timeout", "stage": "transport"}),) * 2,
        )
        assert outcome.kind is DbOutcomeKind.COMPLETED
        run = _value(
            owned,
            "SELECT status, error_json FROM operation_runs WHERE id = ?",
            outcome.value.run_id,
        )
        assert run[0] == 6
        assert json.loads(run[1])["code"] == "emergency_stop_unconfirmed"
        activity = _value(
            owned, "SELECT activity_state FROM device_activities WHERE id = 1"
        )
        assert activity[0] == 2  # 未确认停止不结束活动
    finally:
        owned.connection.close()


async def test_recorded_emergency_stop_qualifies_for_release(
        tmp_path: Path) -> None:
    """应急补记入口的释放组合：停止可靠补记后按统一判定放行。

    应急只证明停止事实：活动结束但占用保持；释放经统一判定核对
    结束依据后放行，不因应急路径单独放行或扣留。
    """
    owned = _environment(tmp_path)
    try:
        record = EmergencyRecord(
            outcome=EmergencyOutcome.STOPPED,
            attempts_used=2,
            max_attempts=3,
        )
        outcome = _save(owned, record, (_stop_attempt(), _stop_attempt()))
        assert outcome.kind is DbOutcomeKind.COMPLETED
        assert _value(
            owned, "SELECT activity_state, occupancy_state"
            " FROM device_activities WHERE id = 1") == (3, 1)

        released = CaptureRepository().release_occupancy(
            ActivityReleaseSave(action_id=1, occurred_at=_NOW),
            new_operation_key(), owned,
        )
        assert released.kind is DbOutcomeKind.COMPLETED, released.error
        assert released.value.outcome is ReleaseOutcome.RELEASED
        assert _value(
            owned, "SELECT occupancy_state FROM device_activities"
            " WHERE id = 1") == (2,)
    finally:
        owned.connection.close()


async def test_unrecorded_emergency_does_not_release(tmp_path: Path) -> None:
    """停止未确认的应急结果不能凭内存事实推进释放。"""
    owned = _environment(tmp_path)
    try:
        record = EmergencyRecord(
            outcome=EmergencyOutcome.UNCONFIRMED, attempts_used=2, max_attempts=2,
            reason=_UNCONFIRMED_REASON,
        )
        outcome = _save(
            owned, record,
            (_attempt(status=4, error={"code": "timeout", "stage": "transport"}),) * 2,
        )
        assert outcome.kind is DbOutcomeKind.COMPLETED
        assert _value(
            owned, "SELECT activity_state, occupancy_state"
            " FROM device_activities WHERE id = 1") == (2, 1)

        rejected = CaptureRepository().release_occupancy(
            ActivityReleaseSave(action_id=1, occurred_at=_NOW),
            new_operation_key(), owned,
        )
        assert rejected.kind is DbOutcomeKind.COMPLETED, rejected.error
        assert rejected.value.outcome is ReleaseOutcome.REJECTED
        assert _value(
            owned, "SELECT occupancy_state FROM device_activities"
            " WHERE id = 1") == (1,)
    finally:
        owned.connection.close()

"""应急实际原因经 SQLite 保存，失败补记整组回滚并保留占用。

使用真实受理、START 保存、补记事务、错误登记与活动释放判定，
START 的设备返回由受接口约束的替身提供。正常停止端口到生产会话
的连接由各自集成测试验证。
"""

from decimal import Decimal
from pathlib import Path

import pytest

from camctl.capture.handlers import capture_handler
from camctl.capture.recovery import EmergencyOutcome, EmergencyRecord, RecordStatus
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import (
    ActivityReleaseSave, CaptureRepository, ReleaseOutcome,
)
from camctl.persistence.transaction import TransactionError, saved_transaction_events

from .result_consumer_fixtures import consumer_world
from .test_emergency import _NOW, _SESSION, _attempt, _stop_observation

pytestmark = pytest.mark.asyncio

_ACTION_ID = 2
_ACTIVITY_ID = 1
_REASON = '原设备返回：“不能停止”\n含引号"、反斜线\\与空格  '
_ATTEMPT_ERROR = {
    "code": "vendor_stop_timeout", "stage": "transport",
    "details": {"message": '驱动原始响应\n"另一个原因"', "received": False},
}


async def _world(tmp_path: Path):
    """目标活动与动作的身份不同，错误详情必须采用活动身份。"""
    owned, runtime, action_id, _ = await consumer_world(
        tmp_path, "record", independent_activity=True, start=False)
    try:
        assert action_id == _ACTION_ID
        activity_id, = owned.connection.execute(
            "SELECT id FROM device_activities WHERE action_id=?", (action_id,)).fetchone()
        assert activity_id == _ACTIVITY_ID
        assert activity_id != action_id
        await capture_handler("camera_record")(action_id, runtime)
        assert owned.connection.execute(
            "SELECT activity_state,occupancy_state,started_at IS NOT NULL"
            " FROM device_activities WHERE id=?", (activity_id,)).fetchone() == (2, 1, 1)
        return owned
    except BaseException:
        owned.connection.close()
        raise


def _save(owned, record, attempts=(), *, key=None, session_key=_SESSION,
          timeout_s=Decimal("10"), retry_interval_s=Decimal("1")):
    return CaptureRepository().save_emergency(
        session_key=session_key, action_id=_ACTION_ID, activity_id=_ACTIVITY_ID,
        record=record, attempts=attempts, occurred_at=_NOW,
        key=new_operation_key() if key is None else key, owned=owned,
        timeout_s=timeout_s, retry_interval_s=retry_interval_s,
    )


def _facts(owned):
    """保存全部表事实，避免只用计数漏掉原历史或投影被修改。"""
    tables = owned.connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    return {
        name: tuple(sorted(owned.connection.execute(
            'SELECT * FROM "' + name.replace('"', '""') + '"').fetchall(), key=repr))
        for name, in tables
    }


@pytest.mark.parametrize("outcome, attempts, status, code", [
    (EmergencyOutcome.NOT_ATTEMPTED, (), 4, "emergency_not_attempted"),
    (EmergencyOutcome.UNCONFIRMED,
     (_attempt(status=4, error=_ATTEMPT_ERROR),), 6, "emergency_stop_unconfirmed"),
])
async def test_save_keeps_real_target_and_exact_actual_reason(
        tmp_path, outcome, attempts, status, code):
    owned = await _world(tmp_path)
    try:
        key = new_operation_key()
        record = EmergencyRecord(outcome, len(attempts), 3, reason=_REASON)
        saved = _save(owned, record, attempts, key=key)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        assert saved.value.record_status is RecordStatus.RECORDED
        expected = {"code": code, "stage": "emergency", "details": {
            "activity_id": "1", "reason": _REASON,
        }}
        run = owned.connection.execute(
            "SELECT action_id,activity_id,status,attempts_used,error_json"
            " FROM operation_runs WHERE id=?", (saved.value.run_id,)).fetchone()
        assert run[:4] == (2, 1, status, len(attempts))
        assert parse_exact_json(run[4]) == expected
        activity = owned.connection.execute(
            "SELECT activity_state,occupancy_state,last_error_json"
            " FROM device_activities WHERE id=1").fetchone()
        assert activity[:2] == (2, 1)
        assert parse_exact_json(activity[2]) == expected
        events = saved_transaction_events(owned.connection, key)
        assert len(events) == 1
        rows = events[0]["body"]["rows"]
        run_row, = (row for row in rows if row["table"] == "operation_runs")
        assert run_row["after"]["values"]["error_json"] == expected
        if attempts:
            attempt_error, = owned.connection.execute(
                "SELECT error_json FROM operation_attempts WHERE run_id=?",
                (saved.value.run_id,)).fetchone()
            assert parse_exact_json(attempt_error) == _ATTEMPT_ERROR
        released = CaptureRepository().release_occupancy(
            ActivityReleaseSave(_ACTION_ID, _NOW), new_operation_key(), owned)
        assert released.kind is DbOutcomeKind.COMPLETED, released.error
        assert released.value.outcome is ReleaseOutcome.REJECTED
        assert owned.connection.execute(
            "SELECT occupancy_state FROM device_activities WHERE id=1").fetchone() == (1,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("outcome, attempts", [
    (EmergencyOutcome.NOT_ATTEMPTED, ()),
    (EmergencyOutcome.UNCONFIRMED, (_attempt(status=4, error=_ATTEMPT_ERROR),)),
])
@pytest.mark.parametrize("reason", [None, "", 7, False, []], ids=[
    "missing", "empty", "integer", "boolean", "list",
])
async def test_invalid_failure_reason_rolls_back_all_facts(tmp_path, outcome, attempts, reason):
    owned = await _world(tmp_path)
    try:
        before = _facts(owned)
        saved = _save(owned, EmergencyRecord(outcome, len(attempts), 3, reason=reason), attempts)
        assert saved.kind is DbOutcomeKind.ROLLED_BACK
        assert saved.error is not None
        assert owned.connection.in_transaction is False
        assert _facts(owned) == before
    finally:
        owned.connection.close()


async def test_successful_stop_cannot_save_failure_reason(tmp_path):
    owned = await _world(tmp_path)
    try:
        before = _facts(owned)
        record = EmergencyRecord(
            EmergencyOutcome.STOPPED, 0, 3,
            stop_observation=_stop_observation(activity_id=1), reason=_REASON)
        saved = _save(owned, record)
        assert saved.kind is DbOutcomeKind.ROLLED_BACK
        assert _facts(owned) == before
    finally:
        owned.connection.close()


@pytest.mark.parametrize("outcome, status", [
    (EmergencyOutcome.NOT_ATTEMPTED, 4),
    (EmergencyOutcome.STOPPED, 3),
])
async def test_zero_attempt_unknown_limit_keeps_actual_final_facts(tmp_path, outcome, status):
    owned = await _world(tmp_path)
    try:
        record = EmergencyRecord(
            outcome, 0, None,
            reason=_REASON if outcome is EmergencyOutcome.NOT_ATTEMPTED else None,
            stop_observation=_stop_observation(activity_id=1)
            if outcome is EmergencyOutcome.STOPPED else None)
        saved = CaptureRepository().save_emergency(
            session_key=_SESSION, action_id=2, activity_id=1,
            record=record,
            attempts=(), occurred_at=_NOW, key=new_operation_key(), owned=owned,
            timeout_s=None, retry_interval_s=None)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        run = owned.connection.execute(
            "SELECT status,attempts_used,max_attempts_used,timeout_s_json,"
            " retry_interval_s_json,error_json FROM operation_runs WHERE id=?",
            (saved.value.run_id,)).fetchone()
        assert run[:5] == (status, 0, None, None, None)
        if outcome is EmergencyOutcome.NOT_ATTEMPTED:
            assert parse_exact_json(run[5]) == {
                "code": "emergency_not_attempted", "stage": "emergency",
                "details": {"activity_id": "1", "reason": _REASON},
            }
        else:
            assert run[5] is None
    finally:
        owned.connection.close()


@pytest.mark.parametrize("maximum", [None, 0, -1, True, "3", Decimal("1.5")], ids=[
    "unknown", "zero", "negative", "boolean", "string", "fraction",
])
async def test_attempted_invalid_limit_rolls_back_all_facts(tmp_path, maximum):
    owned = await _world(tmp_path)
    try:
        before = _facts(owned)
        saved = _save(owned, EmergencyRecord(
            EmergencyOutcome.UNCONFIRMED, 1, maximum, reason=_REASON),
            (_attempt(status=4, error=_ATTEMPT_ERROR),))
        assert saved.kind is DbOutcomeKind.ROLLED_BACK
        assert _facts(owned) == before
    finally:
        owned.connection.close()


@pytest.mark.parametrize("maximum", [0, -1, False, "3", Decimal("1.5")], ids=[
    "zero", "negative", "boolean", "string", "fraction",
])
async def test_zero_attempt_invalid_known_limit_rolls_back_all_facts(tmp_path, maximum):
    owned = await _world(tmp_path)
    try:
        before = _facts(owned)
        saved = _save(owned, EmergencyRecord(
            EmergencyOutcome.NOT_ATTEMPTED, 0, maximum, reason=_REASON))
        assert saved.kind is DbOutcomeKind.ROLLED_BACK
        assert _facts(owned) == before
    finally:
        owned.connection.close()


@pytest.mark.parametrize("outcome, used, attempts", [
    (EmergencyOutcome.NOT_ATTEMPTED, -1, ()),
    (EmergencyOutcome.NOT_ATTEMPTED, False, ()),
    (EmergencyOutcome.NOT_ATTEMPTED, "0", ()),
    (EmergencyOutcome.NOT_ATTEMPTED, None, ()),
    (EmergencyOutcome.UNCONFIRMED, True, (_attempt(status=4, error=_ATTEMPT_ERROR),)),
    (EmergencyOutcome.UNCONFIRMED, Decimal("1.5"), (_attempt(status=4, error=_ATTEMPT_ERROR),)),
])
async def test_invalid_actual_count_rolls_back_all_facts(tmp_path, outcome, used, attempts):
    owned = await _world(tmp_path)
    try:
        before = _facts(owned)
        saved = _save(owned, EmergencyRecord(outcome, used, 3, reason=_REASON), attempts)
        assert saved.kind is DbOutcomeKind.ROLLED_BACK
        assert _facts(owned) == before
    finally:
        owned.connection.close()


@pytest.mark.parametrize("outcome, used, maximum, attempts", [
    (EmergencyOutcome.NOT_ATTEMPTED, 0, Decimal("3.0"), ()),
    (EmergencyOutcome.NOT_ATTEMPTED, Decimal("0.0"), 3, ()),
    (EmergencyOutcome.UNCONFIRMED, Decimal("1.0"), 3,
     (_attempt(status=4, error=_ATTEMPT_ERROR),)),
], ids=["decimal-limit", "decimal-zero-count", "decimal-positive-count"])
async def test_record_integer_type_errors_are_rejected_before_sql_binding(
        tmp_path, outcome, used, maximum, attempts):
    owned = await _world(tmp_path)
    try:
        before = _facts(owned)
        saved = _save(owned, EmergencyRecord(outcome, used, maximum, reason=_REASON), attempts)
        assert saved.kind is DbOutcomeKind.ROLLED_BACK
        assert isinstance(saved.error, TransactionError)
        assert owned.connection.in_transaction is False
        assert _facts(owned) == before
    finally:
        owned.connection.close()


@pytest.mark.parametrize("timeout_s, retry_interval_s", [
    (None, Decimal("1")),
    (Decimal("10"), None),
])
async def test_attempted_partial_config_rolls_back_all_facts(
        tmp_path, timeout_s, retry_interval_s):
    owned = await _world(tmp_path)
    try:
        before = _facts(owned)
        saved = _save(owned, EmergencyRecord(
            EmergencyOutcome.UNCONFIRMED, 1, 3, reason=_REASON),
            (_attempt(status=4, error=_ATTEMPT_ERROR),),
            timeout_s=timeout_s, retry_interval_s=retry_interval_s)
        assert saved.kind is DbOutcomeKind.ROLLED_BACK
        assert _facts(owned) == before
    finally:
        owned.connection.close()


@pytest.mark.parametrize("outcome, attempts", [
    (EmergencyOutcome.NOT_ATTEMPTED, ()),
    (EmergencyOutcome.UNCONFIRMED, (_attempt(status=4, error=_ATTEMPT_ERROR),)),
])
@pytest.mark.parametrize("timeout_s, retry_interval_s", [
    (Decimal("0"), Decimal("1")),
    (Decimal("-1"), Decimal("1")),
    (Decimal("10"), Decimal("-1")),
    (Decimal("NaN"), Decimal("1")),
    (Decimal("Infinity"), Decimal("1")),
    (Decimal("-Infinity"), Decimal("1")),
    (Decimal("10"), Decimal("NaN")),
    (Decimal("10"), Decimal("Infinity")),
    (Decimal("10"), Decimal("-Infinity")),
], ids=["zero-timeout", "negative-timeout", "negative-interval",
        "nan-timeout", "infinite-timeout", "negative-infinite-timeout",
        "nan-interval", "infinite-interval", "negative-infinite-interval"])
async def test_invalid_known_config_rolls_back_all_facts(
        tmp_path, outcome, attempts, timeout_s, retry_interval_s):
    owned = await _world(tmp_path)
    try:
        before = _facts(owned)
        saved = _save(owned, EmergencyRecord(
            outcome, len(attempts), 3, reason=_REASON), attempts,
            timeout_s=timeout_s, retry_interval_s=retry_interval_s)
        assert saved.kind is DbOutcomeKind.ROLLED_BACK
        assert saved.error is not None
        assert owned.connection.in_transaction is False
        assert _facts(owned) == before
    finally:
        owned.connection.close()


async def test_same_key_with_changed_reason_cannot_replace_original_facts(tmp_path):
    """原键核实读取原事件；再次提交不同事实整组拒绝。"""
    owned = await _world(tmp_path)
    try:
        key = new_operation_key()
        first = _save(owned, EmergencyRecord(
            EmergencyOutcome.NOT_ATTEMPTED, 0, 3, reason=_REASON), key=key)
        assert first.kind is DbOutcomeKind.COMPLETED, first.error
        before = _facts(owned)
        original = saved_transaction_events(owned.connection, key)
        again = _save(owned, EmergencyRecord(
            EmergencyOutcome.NOT_ATTEMPTED, 0, 3, reason="不得替换的另一原因"), key=key)
        assert again.kind is DbOutcomeKind.ROLLED_BACK
        assert _facts(owned) == before
        assert saved_transaction_events(owned.connection, key) == original
        run_row, = (row for row in original[0]["body"]["rows"]
                    if row["table"] == "operation_runs")
        assert run_row["after"]["values"]["error_json"]["details"]["reason"] == _REASON
    finally:
        owned.connection.close()


@pytest.mark.parametrize("outcome, attempts, code", [
    (EmergencyOutcome.NOT_ATTEMPTED, (), "emergency_not_attempted"),
    (EmergencyOutcome.UNCONFIRMED, (_attempt(status=4, error=_ATTEMPT_ERROR),),
     "emergency_stop_unconfirmed"),
    (EmergencyOutcome.STOPPED, (), None),
])
async def test_unchanged_activity_keeps_valid_new_flow_error(tmp_path, outcome, attempts, code):
    owned = await _world(tmp_path)
    try:
        record = EmergencyRecord(
            outcome, len(attempts), 3,
            reason=None if outcome is EmergencyOutcome.STOPPED else _REASON,
            stop_observation=_stop_observation(activity_id=1)
            if outcome is EmergencyOutcome.STOPPED else None)
        first = _save(owned, record, attempts)
        assert first.kind is DbOutcomeKind.COMPLETED, first.error
        error = owned.connection.execute(
            "SELECT last_error_json FROM device_activities WHERE id=1").fetchone()[0]
        expected = None if code is None else {
            "code": code, "stage": "emergency",
            "details": {"activity_id": "1", "reason": _REASON},
        }
        assert (None if error is None else parse_exact_json(error)) == expected
        key = new_operation_key()
        second = _save(owned, record, attempts, key=key, session_key="b" * 32)
        assert second.kind is DbOutcomeKind.COMPLETED, second.error
        rows = saved_transaction_events(owned.connection, key)[0]["body"]["rows"]
        assert [row["table"] for row in rows] == ["operation_runs"] + [
            "operation_attempts" for _ in attempts]
        assert rows[0]["after"]["values"]["error_json"] == expected
    finally:
        owned.connection.close()


async def test_error_registry_resource_failure_rolls_back_without_becoming_bad_instance(
        tmp_path, monkeypatch):
    from camctl.contracts import workflow_errors
    from camctl.contracts.schemas import SchemaRuleError
    from camctl.resources import ResourceError

    owned = await _world(tmp_path)
    try:
        before = _facts(owned)
        original = workflow_errors.resource_bytes
        cause = ResourceError("测试控制的公共错误登记读取失败")

        def read(name):
            if name == "protocol/workflow-codes.json":
                assert owned.connection.in_transaction
                raise cause
            return original(name)

        workflow_errors._registry.cache_clear()
        monkeypatch.setattr(workflow_errors, "resource_bytes", read)
        with pytest.raises(SchemaRuleError) as raised:
            _save(owned, EmergencyRecord(
                EmergencyOutcome.NOT_ATTEMPTED, 0, 3, reason=_REASON))
        assert raised.value.__cause__ is cause
        assert not owned.connection.in_transaction
        assert _facts(owned) == before
    finally:
        workflow_errors._registry.cache_clear()
        owned.connection.close()

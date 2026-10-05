"""拍摄动作开始事务的组件集成测试。

受理后的 pending 拍摄动作取得时间资格后，同一事务保存开始事实
（ACTION_STARTED）并登记设备活动身份与固定能力（DEVICE_OBSERVED
.CREATE）；录像动作同时建立处理责任。同键重送与已开始动作按原
事实幂等返回，未到时间、窗口结束、取消与终态分别拒绝。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from camctl.acceptance.input import parse_input, read_input
from camctl.acceptance.service import AcceptanceContext, CommandMode, accept_input
from camctl.contracts.values import new_operation_key, to_utc_micros
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import (
    AcceptanceRepository,
    register_acceptance_guards,
)
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.persistence.repositories.scheduling import (
    SchedulingRepository,
    StartActionRequest,
    StartOutcome,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, OwnedConnection, open_existing
from camctl.reporting.policy import register_report_guards, register_sync_guard

from ..acceptance.test_acceptance import (
    Catalog,
    RealFileReader,
    _create_valid_database,
    _plan_body,
)

register_acceptance_guards()
register_report_guards()
register_sync_guard()
register_outputs_guards()
register_capture_guards()

_NOW = 1_750_000_000_000_000
_SCHEDULED = to_utc_micros("2026-01-15 09:00:00")


@pytest.fixture()
def owned(tmp_path: Path):
    target = tmp_path / "state.db"
    _create_valid_database(target)
    connection = open_existing(target, DbOpenMode.EXISTING_RW, DbConfig())
    yield connection
    connection.connection.close()


async def _accept_plan(owned: OwnedConnection, tmp_path: Path, body: dict,
                       catalog: Catalog) -> int:
    target = tmp_path / "plan.json"
    target.write_text(__import__("json").dumps(body), encoding="utf-8")
    read = await read_input(str(target), RealFileReader())
    result = await accept_input(
        parse_input(read),
        AcceptanceContext(
            mode=CommandMode.RUN,
            catalog=catalog,
            repository=AcceptanceRepository(),
            clock=type("C", (), {"utc_micros": staticmethod(lambda: _NOW)})(),
        ),
        new_operation_key(), owned,
    )
    assert result.plan_id is not None
    return result.plan_id


def _start(owned: OwnedConnection, action_id: int, *, now: int, key=None):
    return SchedulingRepository().start_action(
        StartActionRequest(
            action_id=action_id, trusted_wall_now=now, occurred_at=now),
        key if key is not None else new_operation_key(), owned)


def _value(owned: OwnedConnection, sql: str, *params):
    row = owned.connection.execute(sql, params).fetchone()
    assert row is not None, f"查询无结果: {sql}"
    return row[0]


def _events(owned: OwnedConnection, event_type: int) -> int:
    return _value(
        owned, "SELECT COUNT(*) FROM history_events WHERE event_type = ?",
        event_type)


pytestmark = pytest.mark.asyncio


class TestStartGranted:
    async def test_photo_start_saves_running_and_activity(self, owned, tmp_path):
        await _accept_plan(owned, tmp_path, _plan_body(), Catalog())
        outcome = _start(owned, 1, now=_SCHEDULED)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        result = outcome.value
        assert result.outcome is StartOutcome.STARTED
        assert result.activity_id == 1

        row = owned.connection.execute(
            "SELECT status, execution_started, cancel_requested FROM actions"
            " WHERE id = 1").fetchone()
        assert row == (2, 1, 0)
        activity = owned.connection.execute(
            "SELECT task_key, state_query_supported, stop_supported,"
            " safe_repeat_stop, start_return_meaning, completion_mode,"
            " ownership_mode, output_scope_json, baseline_state, dispatch_state,"
            " activity_state, occupancy_state, result_set_state"
            " FROM device_activities WHERE action_id = 1").fetchone()
        task_key = activity[0]
        assert len(task_key) == 32 and task_key == task_key.lower()
        # 单张拍摄：返回即完成、任务独立输出范围、无需基准。
        assert activity[1:8] == (0, 0, 0, 3, 1, 1, "{}")
        assert activity[8:13] == (1, 1, 1, 1, 1)
        assert _value(owned, "SELECT COUNT(*) FROM recording_processing") == 0
        # 开始事实与活动登记在同一次提交中各保存一个事件。
        assert _events(owned, 5) == 1
        assert _events(owned, 13) == 1

    async def test_record_start_creates_processing(self, owned, tmp_path):
        from ..acceptance.test_definitions import TaskCatalog

        body = _plan_body()
        body["actions"][0]["type"] = "camera_record"
        await _accept_plan(owned, tmp_path, body, TaskCatalog())
        outcome = _start(owned, 1, now=_SCHEDULED)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        assert outcome.value.outcome is StartOutcome.STARTED
        processing = owned.connection.execute(
            "SELECT check_state, check_decision, repair_state, discard_state,"
            " media_json, check_basis_json FROM recording_processing"
            " WHERE action_id = 1").fetchone()
        # 录像处理责任以尚未决定状态建立，检查决定由后续事务推进。
        assert processing == (1, 1, 1, 1, "{}", None)
        activity = owned.connection.execute(
            "SELECT stop_supported, safe_repeat_stop, start_return_meaning,"
            " completion_mode FROM device_activities WHERE action_id = 1").fetchone()
        assert activity == (1, 1, 2, 2)

    async def test_timelapse_capabilities_follow_fixed_spec(self, owned, tmp_path):
        from ..acceptance.test_definitions import TaskCatalog

        body = _plan_body()
        body["actions"][0]["type"] = "camera_timelapse"
        await _accept_plan(owned, tmp_path, body, TaskCatalog())
        outcome = _start(owned, 1, now=_SCHEDULED)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        activity = owned.connection.execute(
            "SELECT stop_supported, safe_repeat_stop, start_return_meaning,"
            " completion_mode FROM device_activities WHERE action_id = 1").fetchone()
        # 延时能力与受理时固定任务契约一致；无停止能力则无安全重复停止。
        assert activity == (0, 0, 1, 2)


class TestStartIdempotent:
    async def test_same_key_replay_returns_already(self, owned, tmp_path):
        await _accept_plan(owned, tmp_path, _plan_body(), Catalog())
        key = new_operation_key()
        first = _start(owned, 1, now=_SCHEDULED, key=key)
        assert first.value.outcome is StartOutcome.STARTED
        replay = _start(owned, 1, now=_SCHEDULED, key=key)
        assert replay.kind is DbOutcomeKind.COMPLETED, replay.error
        assert replay.value.outcome is StartOutcome.ALREADY
        assert replay.value.activity_id == 1
        assert _events(owned, 5) == 1
        assert _events(owned, 13) == 1

    async def test_new_key_after_start_returns_already(self, owned, tmp_path):
        await _accept_plan(owned, tmp_path, _plan_body(), Catalog())
        assert _start(owned, 1, now=_SCHEDULED).value.outcome is StartOutcome.STARTED
        again = _start(owned, 1, now=_SCHEDULED)
        assert again.kind is DbOutcomeKind.COMPLETED, again.error
        assert again.value.outcome is StartOutcome.ALREADY
        assert _events(owned, 5) == 1
        assert _value(owned, "SELECT COUNT(*) FROM device_activities") == 1


class TestStartRejected:
    async def test_before_schedule_is_rejected(self, owned, tmp_path):
        await _accept_plan(owned, tmp_path, _plan_body(), Catalog())
        outcome = _start(owned, 1, now=_SCHEDULED - 1)
        assert outcome.value.outcome is StartOutcome.REJECTED
        assert outcome.value.reason == "too_early"
        assert _value(owned, "SELECT status FROM actions WHERE id = 1") == 1
        assert _value(owned, "SELECT COUNT(*) FROM device_activities") == 0

    async def test_after_window_is_rejected(self, owned, tmp_path):
        await _accept_plan(owned, tmp_path, _plan_body(), Catalog())
        outcome = _start(owned, 1, now=_SCHEDULED + 1_000_001)
        assert outcome.value.outcome is StartOutcome.REJECTED
        assert outcome.value.reason == "window_ended"
        assert _value(owned, "SELECT status FROM actions WHERE id = 1") == 1

    async def test_cancel_requested_is_rejected(self, owned, tmp_path):
        await _accept_plan(owned, tmp_path, _plan_body(), Catalog())
        # 执行前取消直接保存终态；已取消动作不再开始。
        owned.connection.execute(
            "UPDATE actions SET status = 6, cancel_requested = 1 WHERE id = 1")
        outcome = _start(owned, 1, now=_SCHEDULED)
        assert outcome.value.outcome is StartOutcome.REJECTED
        assert outcome.value.reason == "terminal"

    async def test_terminal_action_is_rejected(self, owned, tmp_path):
        await _accept_plan(owned, tmp_path, _plan_body(), Catalog())
        owned.connection.execute(
            "UPDATE actions SET status = 3, execution_started = 1 WHERE id = 1")
        outcome = _start(owned, 1, now=_SCHEDULED)
        assert outcome.value.outcome is StartOutcome.REJECTED
        assert outcome.value.reason == "terminal"

    async def test_non_camera_action_is_rolled_back(self, owned, tmp_path):
        body = _plan_body()
        body["actions"][0] = {
            "name": "sync", "type": "report_status",
            "scheduled_at": "2026-01-15 09:00:00",
            "params": {"scope": "full"},
        }
        await _accept_plan(owned, tmp_path, body, Catalog())
        outcome = _start(owned, 1, now=_SCHEDULED)
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK
        assert "拍摄动作" in str(outcome.error)

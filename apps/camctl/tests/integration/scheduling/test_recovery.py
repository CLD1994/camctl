"""Q6 释放与重启的全入口组件集成测试。

设备活动占用的释放采用统一判定：活动已结束、可靠未派发或无效果
拒绝、适用的等待与产物完成依据三者居一，且输出范围归属限制已解
除；ENDED 与 HELD 可并存，主机后处理不维持占用。活动结束的可靠
停止事实是 start 责任的成功终态流程行，不由路径、超时或本地退出
补造。迟到观察只修改旧活动自身事实，不清除下一动作的占用；已释
放重复释放按原事实幂等。照片成功链在同一推进中收场活动并释放，
释放后同设备下一动作继续推进。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import (
    ActivityConcludeSave,
    ActivityObservationSave,
    ActivityReleaseSave,
    CaptureRepository,
    ConcludeOutcome,
    ReleaseOutcome,
    register_capture_guards,
)
from camctl.persistence.repositories.operations import register_operation_guards
from camctl.persistence.repositories.scheduling import register_window_guard
from camctl.persistence.repositories.timelapse import register_timelapse_guards
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..persistence.test_runtime import _create_valid_database
from .test_resources import (
    _seed_activity,
    _seed_plan,
    _seed_record_action,
)

register_operation_guards()
register_capture_guards()
register_timelapse_guards()
register_window_guard()

_NOW = 1_750_000_000_000_000


@pytest.fixture()
def owned(tmp_path: Path):
    target = tmp_path / "state.db"
    _create_valid_database(target)
    handle = open_existing(target, DbOpenMode.EXISTING_RW, DbConfig())
    connection = handle.connection
    connection.execute("BEGIN IMMEDIATE")
    _seed_plan(connection, 1)
    connection.commit()
    yield handle
    handle.connection.close()


def _seed_running_action(
    connection, action_id: int, *, input_index: int = 0,
) -> None:
    _seed_record_action(connection, action_id, 1, input_index=input_index)


def _seed_terminal_run(connection, action_id: int, *, status: int = 3) -> None:
    """start 责任的终态流程行；status=3 是活动结束的可靠停止事实。"""
    connection.execute(
        "INSERT INTO operation_runs (id, action_id, delivery_id, kind, query_purpose,"
        " responsibility_key, activity_id, copy_id, cleanup_item_id, session_key,"
        " status, attempts_used, max_attempts_used, timeout_s_json,"
        " retry_interval_s_json, retry_wait_required, error_json)"
        " VALUES (?, ?, NULL, 1, NULL, ?, ?, NULL, NULL, NULL, ?, 1, 1, '30', '1',"
        " 0, ?)",
        (action_id + 50, action_id, f"start/{action_id}", action_id, status,
         None if status == 3 else '{"code": "call_failed"}'))


def _release(owned, action_id: int, *, key=None):
    return CaptureRepository().release_occupancy(
        ActivityReleaseSave(action_id=action_id, occurred_at=_NOW),
        key if key is not None else new_operation_key(), owned)


def _conclude(owned, action_id: int):
    return CaptureRepository().conclude_activity(
        ActivityConcludeSave(action_id=action_id, occurred_at=_NOW),
        new_operation_key(), owned)


def _release_events(owned) -> int:
    return int(_value(
        owned, "SELECT COUNT(*) FROM history_events WHERE event_type = 13"
        " AND json_extract(body_json, '$.reason') = 3")[0])


def _value(owned, sql: str, *params):
    row = owned.connection.execute(sql, params).fetchone()
    assert row is not None, f"查询无结果: {sql}"
    return row


pytestmark = pytest.mark.asyncio


class TestReleaseOccupancy:
    async def test_not_dispatched_activity_releases(self, owned):
        connection = owned.connection
        connection.execute("BEGIN IMMEDIATE")
        _seed_running_action(connection, 1)
        _seed_activity(connection, 1, dispatch_state=1, activity_state=1)
        connection.commit()

        outcome = _release(owned, 1)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        assert outcome.value.outcome is ReleaseOutcome.RELEASED

        # 可靠未派发：活动保持未知，占用释放并有事件事实。
        row = _value(
            owned, "SELECT dispatch_state, activity_state, occupancy_state"
            " FROM device_activities WHERE id = 1")
        assert row == (1, 1, 2)
        assert _release_events(owned) == 1

    async def test_ended_activity_releases(self, owned):
        connection = owned.connection
        connection.execute("BEGIN IMMEDIATE")
        _seed_running_action(connection, 1)
        _seed_activity(connection, 1, dispatch_state=3, activity_state=3)
        connection.commit()

        outcome = _release(owned, 1)
        assert outcome.value.outcome is ReleaseOutcome.RELEASED
        assert _value(
            owned, "SELECT occupancy_state FROM device_activities"
            " WHERE id = 1") == (2,)

    async def test_rejected_without_effect_releases(self, owned):
        connection = owned.connection
        connection.execute("BEGIN IMMEDIATE")
        _seed_running_action(connection, 1)
        _seed_activity(connection, 1, dispatch_state=4, activity_state=1)
        connection.commit()

        outcome = _release(owned, 1)
        assert outcome.value.outcome is ReleaseOutcome.RELEASED
        assert _value(
            owned, "SELECT occupancy_state FROM device_activities"
            " WHERE id = 1") == (2,)

    async def test_running_activity_rejects_release(self, owned):
        connection = owned.connection
        connection.execute("BEGIN IMMEDIATE")
        _seed_running_action(connection, 1)
        # 已派发且活动进行中：三个释放依据都不成立。
        _seed_activity(connection, 1, dispatch_state=2, activity_state=2)
        connection.commit()

        outcome = _release(owned, 1)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        assert outcome.value.outcome is ReleaseOutcome.REJECTED
        assert outcome.value.reason == "conditions_unmet"
        assert _value(
            owned, "SELECT occupancy_state FROM device_activities"
            " WHERE id = 1") == (1,)
        assert _release_events(owned) == 0

    async def test_baseline_scope_limited_rejects_release(self, owned):
        connection = owned.connection
        connection.execute("BEGIN IMMEDIATE")
        _seed_running_action(connection, 1)
        _seed_activity(connection, 1, dispatch_state=3, activity_state=3)
        # 基准比较归属尚未固定：输出范围限制仍然存在。
        connection.execute(
            "UPDATE device_activities SET ownership_mode = 2, baseline_state = 2"
            " WHERE id = 1")
        connection.commit()

        outcome = _release(owned, 1)
        assert outcome.value.outcome is ReleaseOutcome.REJECTED
        assert outcome.value.reason == "scope_limited"
        assert _value(
            owned, "SELECT occupancy_state FROM device_activities"
            " WHERE id = 1") == (1,)

        # 归属固定后同一入口可以释放。
        connection.execute(
            "UPDATE device_activities SET baseline_state = 3 WHERE id = 1")
        connection.commit()
        again = _release(owned, 1)
        assert again.value.outcome is ReleaseOutcome.RELEASED

    async def test_already_released_is_idempotent(self, owned):
        connection = owned.connection
        connection.execute("BEGIN IMMEDIATE")
        _seed_running_action(connection, 1)
        _seed_activity(connection, 1, dispatch_state=1, activity_state=1)
        connection.commit()
        first = _release(owned, 1)
        assert first.value.outcome is ReleaseOutcome.RELEASED
        again = _release(owned, 1)
        assert again.kind is DbOutcomeKind.COMPLETED, again.error
        assert again.value.outcome is ReleaseOutcome.ALREADY
        assert _release_events(owned) == 1

    async def test_same_key_replay_restores_release(self, owned):
        connection = owned.connection
        connection.execute("BEGIN IMMEDIATE")
        _seed_running_action(connection, 1)
        _seed_activity(connection, 1, dispatch_state=1, activity_state=1)
        connection.commit()
        key = new_operation_key()
        first = _release(owned, 1, key=key)
        assert first.value.outcome is ReleaseOutcome.RELEASED
        replay = _release(owned, 1, key=key)
        assert replay.kind is DbOutcomeKind.COMPLETED, replay.error
        assert replay.value.outcome is ReleaseOutcome.RELEASED
        assert _release_events(owned) == 1

    async def test_missing_activity_rejected(self, owned):
        connection = owned.connection
        connection.execute("BEGIN IMMEDIATE")
        _seed_running_action(connection, 1)
        connection.commit()
        outcome = _release(owned, 1)
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK


class TestReleaseNeverErasesNewOwner:
    async def test_late_observation_keeps_new_owner_occupancy(self, owned):
        """计划点名用例：迟到观察属于旧活动，不清除新活动占用。"""
        connection = owned.connection
        connection.execute("BEGIN IMMEDIATE")
        _seed_running_action(connection, 1, input_index=0)
        _seed_running_action(connection, 2, input_index=1)
        _seed_activity(connection, 1, dispatch_state=3, activity_state=3)
        # 同设备下一动作的新活动：持有占用。
        _seed_activity(connection, 2, dispatch_state=1, activity_state=1)
        connection.commit()

        released = _release(owned, 1)
        assert released.value.outcome is ReleaseOutcome.RELEASED
        late = CaptureRepository().save_activity_observation(
            ActivityObservationSave(
                action_id=1, occurred_at=_NOW, started_at=_NOW - 1),
            new_operation_key(), owned)
        assert late.kind is DbOutcomeKind.COMPLETED, late.error
        # 旧活动迟到观察落库，新活动占用不受影响。
        assert _value(
            owned, "SELECT started_at, activity_state, occupancy_state"
            " FROM device_activities WHERE id = 1") == (_NOW - 1, 3, 2)
        assert _value(
            owned, "SELECT occupancy_state FROM device_activities"
            " WHERE id = 2") == (1,)
        # 已释放的旧活动不能借迟到观察再次改变释放事实。
        repeat = _release(owned, 1)
        assert repeat.value.outcome is ReleaseOutcome.ALREADY
        assert _value(
            owned, "SELECT occupancy_state FROM device_activities"
            " WHERE id = 2") == (1,)


class TestConcludeRequiresStopFact:
    async def test_conclude_requires_terminal_success_run(self, owned):
        connection = owned.connection
        connection.execute("BEGIN IMMEDIATE")
        _seed_running_action(connection, 1)
        _seed_activity(connection, 1, dispatch_state=3, activity_state=2)
        connection.commit()

        # 没有任何 start 责任成功终态流程行：活动结束缺少可靠停止
        # 事实，整组拒绝且不保存任何收场事实。
        outcome = _conclude(owned, 1)
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK
        assert _value(
            owned, "SELECT activity_state, occupancy_state"
            " FROM device_activities WHERE id = 1") == (2, 1)

        connection.execute("BEGIN IMMEDIATE")
        _seed_terminal_run(connection, 1, status=3)
        connection.commit()
        saved = _conclude(owned, 1)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        assert saved.value.outcome is ConcludeOutcome.CONCLUDED
        assert _value(
            owned, "SELECT activity_state, occupancy_state"
            " FROM device_activities WHERE id = 1") == (3, 2)

    async def test_failed_run_is_not_stop_fact(self, owned):
        connection = owned.connection
        connection.execute("BEGIN IMMEDIATE")
        _seed_running_action(connection, 1)
        _seed_activity(connection, 1, dispatch_state=3, activity_state=2)
        _seed_terminal_run(connection, 1, status=4)
        connection.commit()

        outcome = _conclude(owned, 1)
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK

    async def test_unknown_activity_rejects_conclude(self, owned):
        connection = owned.connection
        connection.execute("BEGIN IMMEDIATE")
        _seed_running_action(connection, 1)
        _seed_activity(connection, 1, dispatch_state=3, activity_state=1)
        connection.commit()

        outcome = _conclude(owned, 1)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        assert outcome.value.outcome is ConcludeOutcome.REJECTED
        assert outcome.value.reason == "not_active"

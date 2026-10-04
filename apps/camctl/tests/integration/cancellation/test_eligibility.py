"""N2 取消资格与启动竞争的组件集成测试。

真实 SQLite 装配资格事实（活动派发阶段与首次固定停止能力），并组
合真实启动授予核对同步点：取消先可靠成立则无启动；启动先成立按已
启动或可能启动分区判定；可靠未启动允许取消且不要求停止能力。
"""

from __future__ import annotations

import json

import pytest

from camctl.cancellation.rules import (
    CancelEligibility,
    decide_cancel_eligibility,
    load_eligibility_facts,
)
from camctl.contracts.values import new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.scheduling import (
    GrantOutcome,
    SchedulingRepository,
)

from ..scheduling.test_resources import (
    _grant_request,
    _seed_activity,
    _seed_environment,
    _seed_plan,
    _seed_record_action,
)


@pytest.fixture
def environment(tmp_path):
    owned = _seed_environment(tmp_path)
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    _seed_plan(connection, 1)
    _seed_record_action(connection, 11, 1)
    connection.commit()
    try:
        yield owned
    finally:
        connection.close()


_NOW = 1_750_000_000_000_000


def _seed_timelapse(connection, action_id: int, *, spec):
    connection.execute(
        "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
        " scheduled_at, group_name, input_fields_json, effective_params_json,"
        " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
        " cancel_requested, error_code, error_details_json,"
        " first_window_observed_at, expiration_reason, source_resolution_state,"
        " resolved_source_plan_id, target_selection_state, created_event_id,"
        " last_event_id, change_count)"
        " VALUES (?, 1, ?, ?, 3, 'cam-1', ?, NULL, '{}', '{}', 'camctl-adb',"
        " 1000, ?, 1, 0, 0, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 1, 1, 1)",
        (action_id, action_id - 11, f"tl-{action_id}", _NOW, json.dumps(spec)))


class TestEligibilityAssembly:
    def test_started_partition_uses_activity_capability(self, environment):
        owned = environment
        connection = owned.connection
        # 已启动（启动已成功返回）：能力取活动行；支持停止允许取消。
        _seed_activity(connection, 11, dispatch_state=3)
        connection.execute(
            "UPDATE device_activities SET stop_supported = 1 WHERE action_id = 11")
        connection.commit()
        assert decide_cancel_eligibility(
            load_eligibility_facts(connection, 11)
        ) is CancelEligibility.ALLOW_WITH_STOP

    def test_may_have_dispatched_without_stop_is_rejected(self, environment):
        owned = environment
        connection = owned.connection
        # 启动发送未知且无停止能力：拒绝，原任务继续。
        _seed_activity(connection, 11, dispatch_state=2)
        connection.execute(
            "UPDATE device_activities SET stop_supported = 0,"
            " safe_repeat_stop = 0 WHERE action_id = 11")
        connection.commit()
        assert decide_cancel_eligibility(
            load_eligibility_facts(connection, 11)
        ) is CancelEligibility.REJECT_UNSUPPORTED

    def test_not_started_allows_even_without_stop(self, environment):
        owned = environment
        connection = owned.connection
        # 已建档但未派发且动作未进入执行：可靠未启动允许取消。
        _seed_activity(connection, 11, dispatch_state=1)
        connection.execute(
            "UPDATE actions SET status = 1, execution_started = 0"
            " WHERE id = 11")
        connection.execute(
            "UPDATE device_activities SET stop_supported = 0,"
            " safe_repeat_stop = 0 WHERE action_id = 11")
        connection.commit()
        assert decide_cancel_eligibility(
            load_eligibility_facts(connection, 11)
        ) is CancelEligibility.ALLOW_PRE_START

    def test_terminal_keeps_state(self, environment):
        owned = environment
        connection = owned.connection
        connection.execute("UPDATE actions SET status = 3 WHERE id = 11")
        connection.commit()
        assert decide_cancel_eligibility(
            load_eligibility_facts(connection, 11)
        ) is CancelEligibility.TERMINAL

    def test_applied_cancel_reuses_responsibility(self, environment):
        owned = environment
        connection = owned.connection
        connection.execute(
            "UPDATE actions SET cancel_requested = 1 WHERE id = 11")
        connection.commit()
        assert decide_cancel_eligibility(
            load_eligibility_facts(connection, 11)
        ) is CancelEligibility.ALREADY_CANCELED

    def test_timelapse_capability_comes_from_saved_spec(self, environment):
        owned = environment
        connection = owned.connection
        connection.execute("BEGIN IMMEDIATE")
        _seed_timelapse(connection, 12, spec={
            "end_control": 1, "stop_supported": False})
        connection.commit()
        facts = load_eligibility_facts(connection, 12)
        assert facts.stop_supported is False
        # 未启动分区：能力不参与判定。
        assert decide_cancel_eligibility(facts) \
            is CancelEligibility.ALLOW_PRE_START
        from camctl.contracts.values import ConsistencyError

        connection.execute("BEGIN IMMEDIATE")
        _seed_timelapse(connection, 13, spec={"end_control": 1})
        connection.commit()
        with pytest.raises(ConsistencyError):
            load_eligibility_facts(connection, 13)


class TestStartCancelRace:
    def test_cancel_first_prevents_start(self, environment):
        """取消先可靠成立：同一协调边界内不再发出启动。"""
        owned = environment
        connection = owned.connection
        connection.execute(
            "UPDATE actions SET cancel_requested = 1 WHERE id = 11")
        connection.commit()
        outcome = SchedulingRepository().grant_start(
            _grant_request(11), new_operation_key(), owned)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        assert outcome.value.outcome is GrantOutcome.REJECTED
        assert outcome.value.reason == "canceled"
        assert outcome.value.ticket is None
        assert connection.execute(
            "SELECT COUNT(*) FROM device_activities").fetchone() == (0,)

    def test_start_first_competes_in_started_partition(self, environment):
        """启动先成立：资格按已启动分区判定，沿用活动固定能力。"""
        owned = environment
        connection = owned.connection
        connection.execute("BEGIN IMMEDIATE")
        _seed_activity(connection, 11)
        connection.commit()
        granted = SchedulingRepository().grant_start(
            _grant_request(11), new_operation_key(), owned)
        assert granted.kind is DbOutcomeKind.COMPLETED, granted.error
        assert granted.value.outcome is GrantOutcome.GRANTED
        facts = load_eligibility_facts(connection, 11)
        assert facts.dispatch.value in ("start_pending", "started")
        assert decide_cancel_eligibility(facts) is CancelEligibility.ALLOW_WITH_STOP

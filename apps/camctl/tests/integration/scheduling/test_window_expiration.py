"""窗口观察与未派发动作过期的组件集成测试。

调度侧在拍摄动作的启动窗口内检查到动作时，先以独立事务保存首
次观察（WINDOW_OBSERVED），再进入开始资格判断；窗口两端包含，
观察不可覆盖。窗口外仍未派发的动作在同一事务保存过期
终态与原因（WINDOW_MISSED／WINDOW_EXHAUSTED）及计划完成事实；
已开始的动作不因窗口结束直接过期，在途处理归属尝试与核实流程。
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
    ExpireActionRequest,
    ExpireOutcome,
    ObserveOutcome,
    ObserveWindowRequest,
    SchedulingRepository,
    StartActionRequest,
    StartOutcome,
    register_window_guard,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, OwnedConnection, open_existing
from camctl.reporting.policy import register_report_guards, register_sync_guard

from ..acceptance.test_acceptance import (
    Catalog,
    RealFileReader,
    _create_valid_database,
)

register_acceptance_guards()
register_report_guards()
register_sync_guard()
register_outputs_guards()
register_capture_guards()
register_window_guard()

_NOW = 1_750_000_000_000_000
_SCHEDULED = to_utc_micros("2026-01-15 09:00:00")
#: ACTION_FINISHED 的事件类型与 EXPIRE 分支编号。
_FINISHED_EVENT = 8
#: max_delay_ms = 1000：窗口终点（含）与终点之后 1 微秒的判定边界。
_WINDOW_END = _SCHEDULED + 1_000 * 1000
_AFTER_WINDOW = _WINDOW_END + 1


@pytest.fixture()
def owned(tmp_path: Path):
    target = tmp_path / "state.db"
    _create_valid_database(target)
    connection = open_existing(target, DbOpenMode.EXISTING_RW, DbConfig())
    yield connection
    connection.connection.close()


def _photo_body(*, request_id: str = "42", count: int = 1) -> dict:
    return {
        "request_id": request_id,
        "created_at": "2026-01-15 08:00:00",
        "name": "plan",
        "actions": [
            {
                "name": f"shoot-{index}",
                "type": "camera_take_photo",
                "device_id": "cam-1",
                "scheduled_at": "2026-01-15 09:00:00",
                "params": {"type": "single_shot"},
                "policy": {"max_delay_ms": 1000},
            }
            for index in range(count)
        ],
    }


async def _accept_plan(owned: OwnedConnection, tmp_path: Path, body: dict,
                       catalog: Catalog = Catalog()) -> int:
    import json

    target = tmp_path / "plan.json"
    target.write_text(json.dumps(body), encoding="utf-8")
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


def _observe(owned: OwnedConnection, action_id: int, *, now: int, key=None):
    return SchedulingRepository().observe_window(
        ObserveWindowRequest(
            action_id=action_id, trusted_wall_now=now, occurred_at=now),
        key if key is not None else new_operation_key(), owned)


def _expire(owned: OwnedConnection, action_id: int, *, now: int, key=None):
    return SchedulingRepository().expire_action(
        ExpireActionRequest(
            action_id=action_id, trusted_wall_now=now, occurred_at=now),
        key if key is not None else new_operation_key(), owned)


def _row(owned: OwnedConnection, sql: str, *params):
    row = owned.connection.execute(sql, params).fetchone()
    assert row is not None, f"查询无结果: {sql}"
    return row


def _events(owned: OwnedConnection, event_type: int, reason: int | None = None) -> int:
    sql = ("SELECT COUNT(*) FROM history_events WHERE event_type = ?"
           " AND json_extract(body_json, '$.reason') = ?")
    return int(_row(owned, sql, event_type, reason)[0])


pytestmark = pytest.mark.asyncio


class TestWindowObservation:
    async def test_in_window_check_saves_first_observation(self, owned, tmp_path):
        await _accept_plan(owned, tmp_path, _photo_body())
        outcome = _observe(owned, 1, now=_SCHEDULED + 500_000)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        result = outcome.value
        assert result.outcome is ObserveOutcome.OBSERVED
        assert result.observed_at == _SCHEDULED + 500_000

        row = _row(
            owned,
            "SELECT first_window_observed_at, status, execution_started"
            " FROM actions WHERE id = 1")
        # 观察只保存事实：动作仍待执行、未开始。
        assert row == (_SCHEDULED + 500_000, 1, 0)
        assert _events(owned, 7, 1) == 1
        # 观察时间用于内部调度，不构成公开报告变化。
        assert _row(
            owned, "SELECT change_seq FROM history_events WHERE event_type = 7") == (None,)

    async def test_window_end_is_inclusive_for_observation(self, owned, tmp_path):
        await _accept_plan(owned, tmp_path, _photo_body())
        outcome = _observe(owned, 1, now=_WINDOW_END)
        assert outcome.value.outcome is ObserveOutcome.OBSERVED
        assert _row(
            owned, "SELECT first_window_observed_at FROM actions WHERE id = 1"
        ) == (_WINDOW_END,)

    async def test_second_observation_keeps_first_time(self, owned, tmp_path):
        await _accept_plan(owned, tmp_path, _photo_body())
        first = _observe(owned, 1, now=_SCHEDULED)
        assert first.value.outcome is ObserveOutcome.OBSERVED
        again = _observe(owned, 1, now=_SCHEDULED + 900_000)
        assert again.value.outcome is ObserveOutcome.ALREADY
        assert _row(
            owned, "SELECT first_window_observed_at FROM actions WHERE id = 1"
        ) == (_SCHEDULED,)
        assert _events(owned, 7, 1) == 1

    async def test_before_start_rejects_observation(self, owned, tmp_path):
        await _accept_plan(owned, tmp_path, _photo_body())
        outcome = _observe(owned, 1, now=_SCHEDULED - 1)
        assert outcome.value.outcome is ObserveOutcome.REJECTED
        assert outcome.value.reason == "too_early"
        # 提前加载不形成观察记录。
        assert _row(
            owned, "SELECT first_window_observed_at FROM actions WHERE id = 1") == (None,)
        assert _events(owned, 7, 1) == 0

    async def test_after_window_rejects_observation(self, owned, tmp_path):
        await _accept_plan(owned, tmp_path, _photo_body())
        outcome = _observe(owned, 1, now=_AFTER_WINDOW)
        assert outcome.value.outcome is ObserveOutcome.REJECTED
        assert outcome.value.reason == "window_ended"
        # 第一次有效检查时窗口已结束：保留未观察到的事实。
        assert _row(
            owned, "SELECT first_window_observed_at FROM actions WHERE id = 1") == (None,)
        assert _events(owned, 7, 1) == 0

    async def test_same_key_replay_restores_observed(self, owned, tmp_path):
        await _accept_plan(owned, tmp_path, _photo_body())
        key = new_operation_key()
        first = _observe(owned, 1, now=_SCHEDULED, key=key)
        assert first.value.outcome is ObserveOutcome.OBSERVED
        replay = _observe(owned, 1, now=_SCHEDULED, key=key)
        assert replay.kind is DbOutcomeKind.COMPLETED, replay.error
        assert replay.value.outcome is ObserveOutcome.OBSERVED
        assert replay.value.observed_at == _SCHEDULED
        assert _events(owned, 7, 1) == 1


class TestPendingExpiration:
    @pytest.mark.parametrize("fault", ["rollback", "commit_before", "commit_after"])
    async def test_unstarted_expiration_recovers_atomic_transaction(
        self, owned, tmp_path, fault,
    ):
        from dataclasses import replace
        import sqlite3

        await _accept_plan(owned, tmp_path, _photo_body())
        _observe(owned, 1, now=_SCHEDULED)
        started = SchedulingRepository().start_action(
            StartActionRequest(1, _SCHEDULED, _SCHEDULED), new_operation_key(), owned)
        assert started.kind is DbOutcomeKind.COMPLETED, started.error
        before = tuple(owned.connection.iterdump())

        class FaultConnection:
            @property
            def in_transaction(self):
                return owned.connection.in_transaction

            def execute(self, sql, parameters=()):
                if fault == "rollback" and sql.startswith("INSERT INTO entity_event_links"):
                    raise sqlite3.OperationalError("injected history link failure")
                if fault.startswith("commit_") and sql == "COMMIT":
                    if fault == "commit_after":
                        owned.connection.execute(sql, parameters)
                    raise sqlite3.OperationalError("injected commit acknowledgement failure")
                return owned.connection.execute(sql, parameters)

        key = new_operation_key()
        failed = _expire(replace(owned, connection=FaultConnection()), 1,
                         now=_AFTER_WINDOW, key=key)
        assert failed.kind is (DbOutcomeKind.ROLLED_BACK if fault == "rollback"
                               else DbOutcomeKind.UNKNOWN), failed.error
        if owned.connection.in_transaction:
            owned.connection.execute("ROLLBACK")
        if fault != "commit_after":
            assert tuple(owned.connection.iterdump()) == before
        # 重开连接后沿原键核实或执行；不能留下终态与占用相互矛盾的投影。
        reopened = open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
        try:
            result = _expire(reopened, 1, now=_AFTER_WINDOW, key=key)
            assert result.kind is DbOutcomeKind.COMPLETED, result.error
            assert result.value.outcome is ExpireOutcome.EXPIRED
            assert _row(reopened, "SELECT status, execution_started FROM actions WHERE id = 1") == (5, 1)
            assert _row(reopened, "SELECT activity_state, occupancy_state FROM device_activities WHERE action_id = 1") == (1, 2)
            assert _row(reopened, "SELECT status FROM plans WHERE id = 1") == (3,)
            assert _events(reopened, 8, 3) == 1
            assert _events(reopened, 13, 3) == 1
        finally:
            reopened.connection.close()

    async def test_unstarted_expiration_replays_and_reverses_compound_history(self, owned, tmp_path):
        from camctl.contracts.history_values import INITIAL_BOUNDARY
        from camctl.history.replay import EntityImage, RestoreSeed, restore
        from camctl.history.events import business_columns
        from camctl.persistence.repositories.history import HistoryRepository
        from ..history.test_complete_history import _entity_type_of, _validated_events

        await _accept_plan(owned, tmp_path, _photo_body())
        _observe(owned, 1, now=_SCHEDULED)
        started = SchedulingRepository().start_action(
            StartActionRequest(1, _SCHEDULED, _SCHEDULED), new_operation_key(), owned)
        assert started.kind is DbOutcomeKind.COMPLETED, started.error
        repository = HistoryRepository(tmp_path / "state.db")
        previous = repository.current_boundary()
        seeds = {entity: repository.restore_entity(entity, 1, previous)
                 for entity in ("action", "plan")}
        expired = _expire(owned, 1, now=_AFTER_WINDOW)
        assert expired.kind is DbOutcomeKind.COMPLETED, expired.error
        final = repository.current_boundary()
        unchanged = tuple(owned.connection.iterdump())
        def business_rows(rows):
            return {key: {column: value for column, value in row.items()
                          if column in business_columns(key[0])}
                    for key, row in rows.items()}
        for entity, seed in seeds.items():
            events = _validated_events(owned.connection, entity, 1, final.last_event_id)
            current = repository.restore_entity(entity, 1, final)
            initial = restore(RestoreSeed(EntityImage(_entity_type_of(entity), 1,
                                                     False, {}, 0, 0), INITIAL_BOUNDARY),
                              events, final)
            seed_image = restore(RestoreSeed(EntityImage(_entity_type_of(entity), 1,
                                                        False, {}, 0, 0), INITIAL_BOUNDARY),
                                 events, previous)
            assert seed_image.rows == business_rows(seed)
            forward = restore(RestoreSeed(seed_image, previous), events, final)
            reverse = repository.restore_entity(entity, 1, previous, event_batch_size=1)
            assert initial.rows == forward.rows == business_rows(current)
            assert business_rows(reverse) == business_rows(seed)
        assert tuple(owned.connection.iterdump()) == unchanged

    async def test_unstarted_scope_restriction_keeps_occupancy(self, owned, tmp_path):
        await _accept_plan(owned, tmp_path, _photo_body())
        SchedulingRepository().start_action(
            StartActionRequest(1, _SCHEDULED, _SCHEDULED), new_operation_key(), owned)
        # 合法范围限制分区由测试种子给出；本用例只核对过期事务的释放条件。
        owned.connection.execute("UPDATE device_activities SET ownership_mode = 2,"
                                 " baseline_state = 2 WHERE action_id = 1")
        result = _expire(owned, 1, now=_AFTER_WINDOW)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert result.value.outcome is ExpireOutcome.EXPIRED
        assert _row(owned, "SELECT occupancy_state FROM device_activities WHERE action_id = 1") == (1,)
        assert _events(owned, 13, 3) == 0

    async def test_running_missing_activity_is_not_unstarted(self, owned, tmp_path):
        await _accept_plan(owned, tmp_path, _photo_body())
        owned.connection.execute("UPDATE actions SET status = 2, execution_started = 1 WHERE id = 1")
        before = tuple(owned.connection.iterdump())
        result = _expire(owned, 1, now=_AFTER_WINDOW)
        assert result.kind is DbOutcomeKind.ROLLED_BACK
        assert tuple(owned.connection.iterdump()) == before

    async def test_known_no_effect_expires_original_start_atomically(self, owned, tmp_path):
        from decimal import Decimal
        from camctl.operations.attempts import AttemptConfig, AttemptFinish
        from camctl.operations.models import (
            AttemptStatus, CallOutcome, EffectState, ErrorValue, EvidenceValue,
            Settlement, SettlementBasis,
        )
        from camctl.operations.validation import validate_outcome
        from camctl.persistence.repositories.operations import OperationRepository
        from camctl.persistence.repositories.scheduling import GrantRequest
        from camctl.scheduling.rules import LaunchWindow
        from .test_resources import _CONTRACTS

        await _accept_plan(owned, tmp_path, _photo_body())
        _observe(owned, 1, now=_SCHEDULED)
        started = SchedulingRepository().start_action(
            StartActionRequest(1, _SCHEDULED, _SCHEDULED), new_operation_key(), owned)
        assert started.kind is DbOutcomeKind.COMPLETED, started.error
        granted = SchedulingRepository().grant_start(
            GrantRequest("cam-1", 1, LaunchWindow(_SCHEDULED, _WINDOW_END),
                         _SCHEDULED, AttemptConfig(3, Decimal("10"), Decimal("3")), _SCHEDULED),
            new_operation_key(), owned)
        assert granted.kind is DbOutcomeKind.COMPLETED, granted.error
        ticket = granted.value.ticket
        no_effect = validate_outcome(ticket, CallOutcome(
            status=AttemptStatus.FAILED, error=ErrorValue("window_ended", "dispatch"),
            effect=EffectState.NO_EFFECT,
            settlement=Settlement(SettlementBasis.NOT_DISPATCHED,
                                  EvidenceValue("dispatch_prevented", 1, {})),
            observations=()), _CONTRACTS)
        finished = OperationRepository().finish_attempt(
            AttemptFinish(ticket, no_effect, _WINDOW_END, retry_wait=True),
            new_operation_key(), owned)
        assert finished.kind is DbOutcomeKind.COMPLETED, finished.error
        before_attempt = _row(owned, "SELECT status, effect_state, result_json"
                              " FROM operation_attempts WHERE run_id = ?", ticket.run_id)
        key = new_operation_key()
        expired = _expire(owned, 1, now=_AFTER_WINDOW, key=key)
        assert expired.kind is DbOutcomeKind.COMPLETED, expired.error
        assert expired.value.outcome is ExpireOutcome.EXPIRED
        assert _row(owned, "SELECT status, attempts_used, retry_wait_required"
                    " FROM operation_runs WHERE id = ?", ticket.run_id) == (7, 1, 0)
        assert _row(owned, "SELECT dispatch_state, activity_state, occupancy_state"
                    " FROM device_activities WHERE action_id = 1") == (1, 1, 2)
        assert _row(owned, "SELECT status, effect_state, result_json"
                    " FROM operation_attempts WHERE run_id = ?", ticket.run_id) == before_attempt
        before = tuple(owned.connection.iterdump())
        again = _expire(owned, 1, now=_AFTER_WINDOW, key=key)
        assert again.kind is DbOutcomeKind.COMPLETED, again.error
        assert again.value.outcome is ExpireOutcome.EXPIRED
        assert tuple(owned.connection.iterdump()) == before

    async def test_expire_without_observation_is_window_missed(self, owned, tmp_path):
        await _accept_plan(owned, tmp_path, _photo_body())
        outcome = _expire(owned, 1, now=_AFTER_WINDOW)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        result = outcome.value
        assert result.outcome is ExpireOutcome.EXPIRED
        assert result.expiration_reason == 1

        row = _row(
            owned,
            "SELECT status, expiration_reason, first_window_observed_at,"
            " error_code, error_details_json FROM actions WHERE id = 1")
        # 错过窗口：没有观察事实，也不构造错误依据。
        assert row == (5, 1, None, None, None)
        assert _events(owned, _FINISHED_EVENT, 3) == 1
        # 过期终态是公开报告变化。
        assert _row(
            owned, "SELECT change_seq FROM history_events"
            " WHERE event_type = ?", _FINISHED_EVENT,
        ) != (None,)
        assert int(_row(
            owned, "SELECT COUNT(*) FROM report_entity_changes")[0]) >= 1

    async def test_expire_with_persisted_observation_is_window_exhausted(
        self, owned, tmp_path,
    ):
        await _accept_plan(owned, tmp_path, _photo_body())
        observed = _observe(owned, 1, now=_SCHEDULED + 200_000)
        assert observed.value.outcome is ObserveOutcome.OBSERVED
        outcome = _expire(owned, 1, now=_AFTER_WINDOW)
        assert outcome.value.outcome is ExpireOutcome.EXPIRED
        assert outcome.value.expiration_reason == 2
        # 过期不清除已保存的窗口内观察。
        assert _row(
            owned,
            "SELECT status, expiration_reason, first_window_observed_at"
            " FROM actions WHERE id = 1") == (5, 2, _SCHEDULED + 200_000)

    async def test_window_end_is_inclusive_for_expiration(self, owned, tmp_path):
        await _accept_plan(owned, tmp_path, _photo_body())
        outcome = _expire(owned, 1, now=_WINDOW_END)
        assert outcome.value.outcome is ExpireOutcome.REJECTED
        assert outcome.value.reason == "window_active"
        assert _row(owned, "SELECT status FROM actions WHERE id = 1") == (1,)

    async def test_in_window_rejects_expiration(self, owned, tmp_path):
        await _accept_plan(owned, tmp_path, _photo_body())
        outcome = _expire(owned, 1, now=_SCHEDULED + 500_000)
        assert outcome.value.outcome is ExpireOutcome.REJECTED
        assert outcome.value.reason == "window_active"
        assert _events(owned, _FINISHED_EVENT, 3) == 0

    async def test_running_without_attempt_expires_and_releases(self, owned, tmp_path):
        await _accept_plan(owned, tmp_path, _photo_body())
        _observe(owned, 1, now=_SCHEDULED)
        started = SchedulingRepository().start_action(
            StartActionRequest(
                action_id=1, trusted_wall_now=_SCHEDULED, occurred_at=_SCHEDULED),
            new_operation_key(), owned)
        assert started.value.outcome is StartOutcome.STARTED
        # RUNNING 只证明执行准备开始：没有启动意图时应过期并释放
        # 冲突占用，保留执行标记和 UNKNOWN 活动事实。
        outcome = _expire(owned, 1, now=_AFTER_WINDOW)
        assert outcome.kind is DbOutcomeKind.COMPLETED, outcome.error
        assert outcome.value.outcome is ExpireOutcome.EXPIRED
        assert outcome.value.expiration_reason == 2
        assert _row(
            owned, "SELECT status, execution_started FROM actions WHERE id = 1"
        ) == (5, 1)
        assert _row(
            owned, "SELECT dispatch_state, activity_state, occupancy_state"
            " FROM device_activities WHERE action_id = 1") == (1, 1, 2)
        assert _row(
            owned, "SELECT COUNT(*) FROM operation_attempts") == (0,)
        assert _row(
            owned, "SELECT COUNT(DISTINCT transaction_id) FROM history_events"
            " WHERE event_type = 8 OR (event_type = 13"
            " AND json_extract(body_json, '$.reason') = 3)") == (1,)

    async def test_granted_intent_is_not_expired_by_window(self, owned, tmp_path):
        from decimal import Decimal
        from camctl.operations.attempts import AttemptConfig
        from camctl.persistence.repositories.operations import register_operation_guards
        from camctl.persistence.repositories.scheduling import GrantRequest, GrantOutcome
        from camctl.scheduling.rules import LaunchWindow

        register_operation_guards()
        await _accept_plan(owned, tmp_path, _photo_body())
        _observe(owned, 1, now=_SCHEDULED)
        started = SchedulingRepository().start_action(
            StartActionRequest(1, _SCHEDULED, _SCHEDULED), new_operation_key(), owned)
        assert started.value.outcome is StartOutcome.STARTED
        granted = SchedulingRepository().grant_start(
            GrantRequest("cam-1", 1, LaunchWindow(_SCHEDULED, _WINDOW_END),
                         _SCHEDULED, AttemptConfig(1, Decimal("10")), _SCHEDULED),
            new_operation_key(), owned)
        assert granted.kind is DbOutcomeKind.COMPLETED, granted.error
        assert granted.value.outcome is GrantOutcome.GRANTED
        outcome = _expire(owned, 1, now=_AFTER_WINDOW)
        assert outcome.value.outcome is ExpireOutcome.REJECTED
        assert outcome.value.reason == "in_flight"
        assert _row(owned, "SELECT status, execution_started FROM actions"
                    " WHERE id = 1") == (2, 1)
        assert _row(owned, "SELECT dispatch_state, occupancy_state"
                    " FROM device_activities WHERE action_id = 1") == (2, 1)

    async def test_last_expired_action_completes_plan(self, owned, tmp_path):
        await _accept_plan(owned, tmp_path, _photo_body(count=2))
        first = _expire(owned, 1, now=_AFTER_WINDOW)
        assert first.value.outcome is ExpireOutcome.EXPIRED
        # 计划内仍有待执行动作：计划不完成。
        assert _row(owned, "SELECT status FROM plans WHERE id = 1") == (1,)
        assert _events(owned, 9, 2) == 0

        second = _expire(owned, 2, now=_AFTER_WINDOW)
        assert second.value.outcome is ExpireOutcome.EXPIRED
        # 计划完成事实与最后的终态同事务保存。
        assert _row(owned, "SELECT status FROM plans WHERE id = 1") == (3,)
        assert _events(owned, 9, 2) == 1

    async def test_expire_same_key_replay_restores_result(self, owned, tmp_path):
        await _accept_plan(owned, tmp_path, _photo_body())
        key = new_operation_key()
        first = _expire(owned, 1, now=_AFTER_WINDOW, key=key)
        assert first.value.outcome is ExpireOutcome.EXPIRED
        replay = _expire(owned, 1, now=_AFTER_WINDOW, key=key)
        assert replay.kind is DbOutcomeKind.COMPLETED, replay.error
        assert replay.value.outcome is ExpireOutcome.EXPIRED
        assert replay.value.expiration_reason == 1
        assert _events(owned, _FINISHED_EVENT, 3) == 1

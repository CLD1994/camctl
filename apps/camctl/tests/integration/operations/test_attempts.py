"""O2 意图、预算与完整结果事务的组件集成测试。

真实 SQLite 与 P3 事务内核组合：意图与额度先于派发提交、预算占用
不退还、失败后的重试等待、整组回滚、同键重送复用及迟到结果不覆
盖已有终态。正式操作守卫在本模块导入时注册。
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from camctl.contracts.values import new_operation_key
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.operations.attempts import (
    AttemptConfig,
    AttemptFinish,
    AttemptIntent,
    AttemptTarget,
    BeginDisposition,
    FinishDisposition,
    OperationKind,
    QueryPurpose,
    RefusalKind,
    RunFinish,
    RunOutcome,
    RunStatus,
    dispatch_decision,
)
from camctl.operations.models import (
    AttemptStatus,
    CallOutcome,
    EffectState,
    ErrorValue,
    EvidenceValue,
    Settlement,
    SettlementBasis,
)
from camctl.operations.validation import validate_outcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import (
    OperationRepository,
    register_operation_guards,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..persistence.test_runtime import _create_valid_database

register_operation_guards()

_NOW = 1_750_000_000_000_000

_CONTRACTS = EvidenceRegistry(
    (
        EvidenceContract(
            type="query_returned",
            version=1,
            operation="query",
            fields=frozenset({"terminate_grace_s"}),
        ),
        EvidenceContract(
            type="dispatch_prevented",
            version=1,
            operation="query",
            fields=frozenset(),
        ),
        EvidenceContract(
            type="file_absent",
            version=1,
            operation="delete",
            fields=frozenset({"cleanup_item_id"}),
            identity_field="cleanup_item_id",
        ),
        EvidenceContract(
            type="operation_returned",
            version=1,
            operation="delete",
            fields=frozenset({"terminate_grace_s"}),
        ),
        EvidenceContract(
            type="adb_foreground_assumption",
            version=1,
            operation="delete",
            fields=frozenset(),
        ),
    )
)


class DispatchSpy:
    """派发端替身：只记录调用，不产生副作用。"""

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, ticket) -> None:
        self.calls.append(ticket)


def _seed_environment(tmp_path: Path):
    """构造有效状态库并直接写入一条已受理动作及清理项（测试准备）。"""
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
    connection.execute(
        "INSERT INTO plans (id, request_id, name, created_at, status,"
        " created_event_id, last_event_id, change_count)"
        " VALUES (1, 4242, 'seed', ?, 1, 1, 1, 1)",
        (_NOW,),
    )
    connection.execute(
        "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
        " scheduled_at, group_name, input_fields_json, effective_params_json,"
        " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
        " cancel_requested, error_code, error_details_json, first_window_observed_at,"
        " expiration_reason, source_resolution_state, resolved_source_plan_id,"
        " target_selection_state, created_event_id, last_event_id, change_count)"
        " VALUES (1, 1, 0, 'shoot', 1, 'cam-1', ?, NULL, '{}', '{\"shots\": 1}',"
        " 'camctl-adb', 1000, '{\"action_type\": \"camera_take_photo\"}', 1, 0, 0,"
        " NULL, NULL, NULL, NULL, NULL, NULL, NULL, 1, 1, 1)",
        (_NOW,),
    )
    connection.execute(
        "INSERT INTO cleanup_items (id, action_id, requested_output_id, output_id,"
        " status, restriction_state, outcome, final_event_id, error_code,"
        " error_details_json) VALUES (1, 1, 1, NULL, 1, 1, NULL, NULL, NULL, NULL)"
    )
    connection.commit()
    return owned


def _preflight_intent(**overrides) -> AttemptIntent:
    values = dict(
        operation="query",
        action_id=1,
        kind=OperationKind.QUERY_ACTIVITY,
        target=AttemptTarget(),
        query_purpose=QueryPurpose.BEFORE_EXECUTION,
        config=AttemptConfig(
            max_attempts=2,
            timeout_s=Decimal("5"),
            retry_interval_s=Decimal("1"),
        ),
        copy_round=None,
        occurred_at=_NOW,
    )
    values.update(overrides)
    return AttemptIntent(**values)


def _delete_intent(**overrides) -> AttemptIntent:
    values = dict(
        operation="delete",
        action_id=1,
        kind=OperationKind.DELETE_FILE,
        target=AttemptTarget(cleanup_item_id=1),
        query_purpose=None,
        config=AttemptConfig(
            max_attempts=2, timeout_s=Decimal("4"), retry_interval_s=Decimal("2")
        ),
        copy_round=None,
        occurred_at=_NOW,
    )
    values.update(overrides)
    return AttemptIntent(**values)


def _outcome(
    *,
    status: AttemptStatus,
    error: ErrorValue | None,
    effect: EffectState,
    basis: SettlementBasis,
    evidence_type: str,
    observations: tuple[DeviceObservation, ...] = (),
) -> CallOutcome:
    return CallOutcome(
        status=status,
        error=error,
        effect=effect,
        settlement=Settlement(
            basis=basis, evidence=EvidenceValue(type=evidence_type, version=1, data={})
        ),
        observations=observations,
    )


def _validated(owned_ticket, outcome: CallOutcome):
    return validate_outcome(owned_ticket, outcome, _CONTRACTS)


_FILE_ABSENT = DeviceObservation(
    type="file_absent", version=1, data={"cleanup_item_id": "1"}
)


def _run_value(owned, sql: str, *params):
    row = owned.connection.execute(sql, params).fetchone()
    assert row is not None, f"查询无结果: {sql}"
    return row


def _event_count(owned) -> int:
    return int(_run_value(owned, "SELECT COUNT(*) FROM history_events")[0])


def test_first_intent_creates_run_and_attempt_together(tmp_path: Path) -> None:
    owned = _seed_environment(tmp_path)
    repository = OperationRepository()
    try:
        outcome = repository.begin_attempt(
            _preflight_intent(), new_operation_key(), owned
        )
        assert outcome.kind is DbOutcomeKind.COMPLETED
        result = outcome.value
        assert result.disposition is BeginDisposition.GRANTED
        ticket = result.ticket
        assert ticket is not None
        assert ticket.attempt_id == 1
        assert ticket.responsibility_key == "query/preflight/1"
        assert ticket.target_id is None

        run = _run_value(
            owned,
            "SELECT status, attempts_used, max_attempts_used, timeout_s_json,"
            " retry_interval_s_json, retry_wait_required, kind, query_purpose,"
            " responsibility_key FROM operation_runs WHERE id = ?",
            ticket.run_id,
        )
        assert run[0] == 2  # ACTIVE
        assert run[1] == 1
        assert run[2] == 2
        assert Decimal(run[3]) == Decimal("5")
        assert Decimal(run[4]) == Decimal("1")
        assert run[5] == 0
        assert run[6] == 6  # QUERY_ACTIVITY
        assert run[7] == 1  # BEFORE_EXECUTION
        assert run[8] == "query/preflight/1"

        attempt = _run_value(
            owned,
            "SELECT attempt_no, status, intent_event_id, result_event_id,"
            " effect_state, max_attempts_used FROM operation_attempts"
            " WHERE run_id = ? AND attempt_no = 1",
            ticket.run_id,
        )
        assert attempt[1] == 1  # RUNNING
        assert attempt[2] == _event_count(owned)  # 意图指向本次事件
        assert attempt[3] is None
        assert attempt[4] == 1  # UNKNOWN
        assert attempt[5] == 2

        # 意图事件已进入历史并与动作对象关联。
        event = _run_value(
            owned,
            "SELECT event_type, body_json FROM history_events WHERE id = ?",
            attempt[2],
        )
        assert event[0] == 11  # ATTEMPT_STARTED
        assert json.loads(event[1])["reason"] == 1  # NEW
        link = _run_value(
            owned,
            "SELECT entity_type, entity_id FROM entity_event_links WHERE event_id = ?",
            attempt[2],
        )
        assert link[0] == 1 and link[1] == 1  # action#1
    finally:
        owned.connection.close()


def test_prevented_dispatch_keeps_attempt_count(tmp_path: Path) -> None:
    owned = _seed_environment(tmp_path)
    repository = OperationRepository()
    dispatches = DispatchSpy()
    try:
        granted = repository.begin_attempt(
            _preflight_intent(), new_operation_key(), owned
        )
        assert dispatch_decision(granted).dispatched is True
        ticket = granted.value.ticket

        # 意图提交后、派发前取消生效：驱动从未被调用。
        prevented = _validated(
            ticket,
            _outcome(
                status=AttemptStatus.FAILED,
                error=ErrorValue(code="canceled_before_dispatch", stage="dispatch"),
                effect=EffectState.NO_EFFECT,
                basis=SettlementBasis.NOT_DISPATCHED,
                evidence_type="dispatch_prevented",
            ),
        )
        assert dispatches.calls == []

        finish = repository.finish_attempt(
            AttemptFinish(ticket=ticket, outcome=prevented, occurred_at=_NOW),
            new_operation_key(),
            owned,
        )
        assert finish.kind is DbOutcomeKind.COMPLETED
        assert finish.value.disposition is FinishDisposition.SAVED

        # 已提交次数保留，不因未派发退还。
        used = _run_value(
            owned,
            "SELECT attempts_used, status FROM operation_runs WHERE id = ?",
            ticket.run_id,
        )
        assert used[0] == 1
        assert used[1] == 2  # ACTIVE

        saved = _run_value(
            owned,
            "SELECT status, error_json, result_json FROM operation_attempts"
            " WHERE run_id = ? AND attempt_no = 1",
            ticket.run_id,
        )
        assert saved[0] == 3  # FAILED
        result_json = json.loads(saved[2])
        assert result_json["settlement"]["basis"] == "not_dispatched"
        assert result_json["settlement"]["evidence"]["type"] == "dispatch_prevented"
        assert result_json["observations"] == []

        # 预算用完后的再次意图被拒绝，且不派发。
        rejected = repository.begin_attempt(
            _preflight_intent(
                config=AttemptConfig(
                    max_attempts=1, timeout_s=Decimal("5"), retry_interval_s=Decimal("1")
                )
            ),
            new_operation_key(),
            owned,
        )
        assert rejected.kind is DbOutcomeKind.COMPLETED
        assert rejected.value.disposition is BeginDisposition.REJECTED
        refused = dispatch_decision(rejected)
        assert refused.dispatched is False
        assert refused.refusal is RefusalKind.REJECTED
        assert dispatches.calls == []
    finally:
        owned.connection.close()


def test_failed_attempt_with_retry_wait_then_second_attempt(tmp_path: Path) -> None:
    owned = _seed_environment(tmp_path)
    repository = OperationRepository()
    try:
        first = repository.begin_attempt(_delete_intent(), new_operation_key(), owned)
        ticket1 = first.value.ticket
        assert ticket1.target_id == "1"

        # 第一次失败并需要重试：尝试结果与重试等待共同保存。
        failed = _validated(
            ticket1,
            _outcome(
                status=AttemptStatus.FAILED,
                error=ErrorValue(code="transport_timeout", stage="transport"),
                effect=EffectState.UNKNOWN,
                basis=SettlementBasis.ASSUMED,
                evidence_type="adb_foreground_assumption",
            ),
        )
        finish1 = repository.finish_attempt(
            AttemptFinish(ticket=ticket1, outcome=failed, occurred_at=_NOW, retry_wait=True),
            new_operation_key(),
            owned,
        )
        assert finish1.kind is DbOutcomeKind.COMPLETED
        run = _run_value(
            owned,
            "SELECT status, retry_wait_required, attempts_used FROM operation_runs"
            " WHERE id = ?",
            ticket1.run_id,
        )
        assert run[0] == 2 and run[1] == 1 and run[2] == 1

        # 间隔结束后同一责任下的第二次尝试：清零等待并占用第二次预算。
        second = repository.begin_attempt(_delete_intent(), new_operation_key(), owned)
        assert second.kind is DbOutcomeKind.COMPLETED
        assert second.value.disposition is BeginDisposition.GRANTED
        ticket2 = second.value.ticket
        assert ticket2.run_id == ticket1.run_id
        assert ticket2.attempt_id == 2
        run = _run_value(
            owned,
            "SELECT retry_wait_required, attempts_used FROM operation_runs WHERE id = ?",
            ticket2.run_id,
        )
        assert run[0] == 0 and run[1] == 2

        # 第二次成功并结束整个责任。
        succeeded = _validated(
            ticket2,
            _outcome(
                status=AttemptStatus.SUCCEEDED,
                error=None,
                effect=EffectState.CONFIRMED,
                basis=SettlementBasis.OBSERVED,
                evidence_type="operation_returned",
                observations=(_FILE_ABSENT,),
            ),
        )
        finish2 = repository.finish_attempt(
            AttemptFinish(
                ticket=ticket2,
                outcome=succeeded,
                occurred_at=_NOW,
                run_finish=RunFinish(status=RunOutcome.SUCCEEDED),
            ),
            new_operation_key(),
            owned,
        )
        assert finish2.kind is DbOutcomeKind.COMPLETED
        run = _run_value(
            owned,
            "SELECT status, error_json, retry_wait_required FROM operation_runs"
            " WHERE id = ?",
            ticket2.run_id,
        )
        assert run[0] == 3  # SUCCEEDED
        assert run[1] is None
        assert run[2] == 0
    finally:
        owned.connection.close()


def test_late_result_does_not_overwrite_terminal_state(tmp_path: Path) -> None:
    owned = _seed_environment(tmp_path)
    repository = OperationRepository()
    try:
        begin = repository.begin_attempt(_delete_intent(), new_operation_key(), owned)
        ticket = begin.value.ticket
        failed = _validated(
            ticket,
            _outcome(
                status=AttemptStatus.FAILED,
                error=ErrorValue(code="transport_timeout", stage="transport"),
                effect=EffectState.UNKNOWN,
                basis=SettlementBasis.ASSUMED,
                evidence_type="adb_foreground_assumption",
            ),
        )
        repository.finish_attempt(
            AttemptFinish(
                ticket=ticket,
                outcome=failed,
                occurred_at=_NOW,
                run_finish=RunFinish(
                    status=RunOutcome.UNCONFIRMED,
                    error=ErrorValue(code="budget_exhausted", stage="operation"),
                ),
            ),
            new_operation_key(),
            owned,
        )
        events_before = _event_count(owned)

        # 迟到的成功结果不覆盖已保存的失败与终态。
        late = _validated(
            ticket,
            _outcome(
                status=AttemptStatus.SUCCEEDED,
                error=None,
                effect=EffectState.CONFIRMED,
                basis=SettlementBasis.OBSERVED,
                evidence_type="operation_returned",
                observations=(_FILE_ABSENT,),
            ),
        )
        outcome = repository.finish_attempt(
            AttemptFinish(ticket=ticket, outcome=late, occurred_at=_NOW),
            new_operation_key(),
            owned,
        )
        assert outcome.kind is DbOutcomeKind.COMPLETED
        assert outcome.value.disposition is FinishDisposition.ALREADY_ENDED
        assert outcome.value.attempt_status is AttemptStatus.FAILED

        saved = _run_value(
            owned,
            "SELECT status, result_json FROM operation_attempts"
            " WHERE run_id = ? AND attempt_no = 1",
            ticket.run_id,
        )
        assert saved[0] == 3  # 原失败保留
        assert json.loads(saved[1])["settlement"]["basis"] == "assumed"
        assert _event_count(owned) == events_before
    finally:
        owned.connection.close()


def test_invalid_run_finish_rolls_back_whole_group(tmp_path: Path) -> None:
    owned = _seed_environment(tmp_path)
    repository = OperationRepository()
    try:
        begin = repository.begin_attempt(_delete_intent(), new_operation_key(), owned)
        ticket = begin.value.ticket
        events_before = _event_count(owned)

        # 结束为 FAILED 却缺少流程错误：整组回滚，尝试保持 RUNNING。
        failed = _validated(
            ticket,
            _outcome(
                status=AttemptStatus.FAILED,
                error=ErrorValue(code="transport_timeout", stage="transport"),
                effect=EffectState.UNKNOWN,
                basis=SettlementBasis.ASSUMED,
                evidence_type="adb_foreground_assumption",
            ),
        )
        outcome = repository.finish_attempt(
            AttemptFinish(
                ticket=ticket,
                outcome=failed,
                occurred_at=_NOW,
                run_finish=RunFinish(status=RunOutcome.FAILED),
            ),
            new_operation_key(),
            owned,
        )
        assert outcome.kind is DbOutcomeKind.ROLLED_BACK

        attempt = _run_value(
            owned,
            "SELECT status, result_event_id FROM operation_attempts"
            " WHERE run_id = ? AND attempt_no = 1",
            ticket.run_id,
        )
        assert attempt[0] == 1  # RUNNING
        assert attempt[1] is None
        run = _run_value(
            owned, "SELECT status FROM operation_runs WHERE id = ?", ticket.run_id
        )
        assert run[0] == 2
        assert _event_count(owned) == events_before
    finally:
        owned.connection.close()


def test_same_key_redelivery_reuses_committed_result(tmp_path: Path) -> None:
    owned = _seed_environment(tmp_path)
    repository = OperationRepository()
    try:
        key = new_operation_key()
        first = repository.begin_attempt(_preflight_intent(), key, owned)
        assert first.kind is DbOutcomeKind.COMPLETED
        events_before = _event_count(owned)

        # 提交结果未知后的重送：按同一操作身份核实并复用原结果。
        again = repository.begin_attempt(_preflight_intent(), key, owned)
        assert again.kind is DbOutcomeKind.COMPLETED
        assert again.value.disposition is BeginDisposition.GRANTED
        assert again.value.ticket == first.value.ticket

        used = _run_value(
            owned,
            "SELECT attempts_used FROM operation_runs WHERE responsibility_key = ?",
            "query/preflight/1",
        )
        assert used[0] == 1
        assert _event_count(owned) == events_before
    finally:
        owned.connection.close()

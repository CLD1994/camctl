"""O5 原尝试恢复与取消接手的组件集成测试。

真实 SQLite 与 O2 结果事务组合：给定取消结果按原尝试保存；只登记
意图时保留原身份与预算并拒绝新尝试；迟到结果不覆盖已保存的事实。
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.contracts.values import new_operation_key
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
from camctl.operations.attempts import (
    AttemptConfig,
    AttemptFinish,
    AttemptIntent,
    AttemptTarget,
    BeginDisposition,
    FinishDisposition,
    OperationKind,
)
from camctl.operations.models import (
    AttemptStatus,
    CallInfo,
    CallOutcome,
    EffectState,
    ErrorValue,
    EvidenceValue,
    Settlement,
    SettlementBasis,
)
from camctl.operations.validation import validate_outcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..persistence.test_runtime import _create_valid_database


_NOW = 1_750_000_000_000_000

_CONTRACTS = EvidenceRegistry(
    (
        EvidenceContract(
            type="operation_returned",
            version=1,
            operation="delete",
            fields=frozenset(),
        ),
        EvidenceContract(
            type="adb_foreground_assumption",
            version=1,
            operation="delete",
            fields=frozenset(),
        ),
    )
)


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
        " VALUES (1, 1, 0, 'clean', 5, NULL, NULL, NULL, '{}', NULL, NULL, NULL,"
        " NULL, 4, 0, 0, 1, '{}', NULL, NULL, NULL, NULL, NULL, 1, 1, 1)",
    )
    connection.execute(
        "INSERT INTO cleanup_items (id, action_id, requested_output_id, output_id,"
        " status, restriction_state, outcome, final_event_id, error_code,"
        " error_details_json) VALUES (1, 1, 1, NULL, 1, 1, NULL, NULL, NULL, NULL)"
    )
    connection.commit()
    return owned


def _intent() -> AttemptIntent:
    return AttemptIntent(
        operation="delete",
        action_id=1,
        kind=OperationKind.DELETE_FILE,
        target=AttemptTarget(cleanup_item_id=1),
        query_purpose=None,
        config=AttemptConfig(
            max_attempts=2, timeout_s=Decimal("4"), retry_interval_s=Decimal("1")
        ),
        occurred_at=_NOW,
    )


def _value(owned, sql: str, *params):
    row = owned.connection.execute(sql, params).fetchone()
    assert row is not None, f"查询无结果: {sql}"
    return row


pytestmark = pytest.mark.asyncio


async def test_given_cancel_result_is_saved_on_original_attempt(tmp_path: Path) -> None:
    """给定取消结束结果，仓储按原票据保存完整未知结果。"""
    owned = _environment(tmp_path)
    repository = OperationRepository()
    try:
        granted = repository.begin_attempt(_intent(), new_operation_key(), owned)
        ticket = granted.value.ticket

        outcome = validate_outcome(
            ticket,
            CallOutcome(
                status=AttemptStatus.UNKNOWN,
                error=ErrorValue(code="canceled_terminated", stage="dispatch"),
                effect=EffectState.UNKNOWN,
                settlement=Settlement(
                    basis=SettlementBasis.OBSERVED,
                    evidence=EvidenceValue(
                        type="operation_returned", version=1, data={}
                    ),
                ),
                observations=(),
                call_info=CallInfo(local_signal=9),
            ),
            _CONTRACTS,
        )
        finish = repository.finish_attempt(
            AttemptFinish(ticket=ticket, outcome=outcome, occurred_at=_NOW),
            new_operation_key(),
            owned,
        )
        assert finish.kind is DbOutcomeKind.COMPLETED, finish.error
        assert finish.value.disposition is FinishDisposition.SAVED
        saved = _value(
            owned,
            "SELECT status, error_json, result_json, effect_state, intent_event_id, result_event_id FROM operation_attempts"
            " WHERE run_id = ? AND attempt_no = 1",
            ticket.run_id,
        )
        assert saved[0] == 4  # UNKNOWN
        assert json.loads(saved[1]) == {"code": "canceled_terminated", "stage": "dispatch", "details": {}}
        assert json.loads(saved[2]) == {
            "format_version": 1,
            "settlement": {"basis": "observed", "evidence": {
                "type": "operation_returned", "version": 1, "data": {}}},
            "observations": [], "call_info": {"local_exit": {"signal": 9}},
        }
        assert saved[3] == 1  # UNKNOWN
        assert saved[4] is not None and saved[5] is not None
    finally:
        owned.connection.close()


async def test_intent_only_attempt_reuses_key_and_rejects_new_attempt(tmp_path: Path) -> None:
    """只登记意图时，同键保留原票据，新尝试被拒绝且预算不变。"""
    owned = _environment(tmp_path)
    repository = OperationRepository()
    try:
        key = new_operation_key()
        granted = repository.begin_attempt(_intent(), key, owned)
        ticket = granted.value.ticket
        assert granted.value.disposition is BeginDisposition.GRANTED

        original = tuple(owned.connection.iterdump())
        repeated = repository.begin_attempt(_intent(), key, owned)
        assert repeated.kind is DbOutcomeKind.COMPLETED, repeated.error
        assert repeated.value == granted.value
        assert tuple(owned.connection.iterdump()) == original

        # 未核实前不新增尝试：重试协议违规被整组回滚。
        again = repository.begin_attempt(_intent(), new_operation_key(), owned)
        assert again.kind is DbOutcomeKind.ROLLED_BACK
        used = _value(
            owned,
            "SELECT attempts_used FROM operation_runs WHERE id = ?",
            ticket.run_id,
        )
        assert used[0] == 1
        assert tuple(owned.connection.iterdump()) == original
    finally:
        owned.connection.close()


async def test_settled_attempt_keeps_original_result(tmp_path: Path) -> None:
    """尝试已失败且流程仍活跃时，迟到成功不覆盖原结果或历史。"""
    owned = _environment(tmp_path)
    repository = OperationRepository()
    try:
        granted = repository.begin_attempt(_intent(), new_operation_key(), owned)
        ticket = granted.value.ticket
        failed = validate_outcome(
            ticket,
            CallOutcome(
                status=AttemptStatus.FAILED,
                error=ErrorValue(code="transport_timeout", stage="transport"),
                effect=EffectState.UNKNOWN,
                settlement=Settlement(
                    basis=SettlementBasis.ASSUMED,
                    evidence=EvidenceValue(
                        type="adb_foreground_assumption", version=1, data={}
                    ),
                ),
                observations=(),
            ),
            _CONTRACTS,
        )
        initial = repository.finish_attempt(
            AttemptFinish(ticket=ticket, outcome=failed, occurred_at=_NOW),
            new_operation_key(),
            owned,
        )
        assert initial.kind is DbOutcomeKind.COMPLETED, initial.error
        assert initial.value.disposition is FinishDisposition.SAVED
        assert _value(owned, "SELECT status FROM operation_runs WHERE id = ?", ticket.run_id) == (2,)
        original = _value(owned, "SELECT status, error_json, result_json FROM operation_attempts"
                          " WHERE run_id = ? AND attempt_no = 1", ticket.run_id)
        assert original[0] == 3
        assert json.loads(original[1])["code"] == "transport_timeout"
        history = tuple(owned.connection.iterdump())
        late = validate_outcome(
            ticket,
            CallOutcome(status=AttemptStatus.SUCCEEDED, effect=EffectState.UNKNOWN,
                settlement=Settlement(SettlementBasis.OBSERVED,
                    EvidenceValue(type="operation_returned", version=1, data={})),
                observations=(), call_info=CallInfo(local_exit_code=0)),
            _CONTRACTS,
        )
        result = repository.finish_attempt(
            AttemptFinish(ticket=ticket, outcome=late, occurred_at=_NOW),
            new_operation_key(), owned,
        )
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert result.value.disposition is FinishDisposition.ALREADY_ENDED
        assert result.value.attempt_status is AttemptStatus.FAILED
        assert _value(owned, "SELECT status, error_json, result_json FROM operation_attempts"
                      " WHERE run_id = ? AND attempt_no = 1", ticket.run_id) == original
        assert tuple(owned.connection.iterdump()) == history
    finally:
        owned.connection.close()

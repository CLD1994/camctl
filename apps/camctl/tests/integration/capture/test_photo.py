"""C4 单张拍摄流程的组件集成测试。

真实授予与结果事务、驱动替身组合：照片共用拍摄设备竞争但按自
身完成声明收场；未派发取消不创建调用；调用结果按声明保存。
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.capture.photo import (
    CaptureAssessment,
    PhotoCompletion,
    PhotoDecision,
    PhotoDispatch,
    PhotoState,
    decide_photo,
    run_photo,
)
from camctl.contracts.values import new_operation_key
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.operations.attempts import AttemptConfig, AttemptFinish
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
from camctl.persistence.repositories.operations import (
    OperationRepository,
    register_operation_guards,
)
from camctl.persistence.repositories.scheduling import (
    GrantRequest,
    SchedulingRepository,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.scheduling.rules import LaunchWindow

from ..persistence.test_runtime import _create_valid_database
from ..scheduling.test_resources import _seed_activity, _seed_plan

register_operation_guards()

_NOW = 1_750_000_000_000_000
_WINDOW = LaunchWindow(scheduled_at=_NOW - 1_000_000, window_end=_NOW + 1_000_000)

_CONTRACTS = EvidenceRegistry(
    (
        EvidenceContract(
            type="operation_returned",
            version=1,
            operation="control",
            fields=frozenset(),
        ),
        EvidenceContract(
            type="photo_taken",
            version=1,
            operation="control",
            fields=frozenset({"activity_id"}),
            identity_field="activity_id",
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
    _seed_plan(connection, 1)
    connection.execute(
        "INSERT INTO actions (id, plan_id, input_index, name, type, device_id,"
        " scheduled_at, group_name, input_fields_json, effective_params_json,"
        " driver_id, max_delay_ms, execution_spec_json, status, execution_started,"
        " cancel_requested, error_code, error_details_json, first_window_observed_at,"
        " expiration_reason, source_resolution_state, resolved_source_plan_id,"
        " target_selection_state, created_event_id, last_event_id, change_count)"
        " VALUES (1, 1, 0, 'shot', 1, 'cam-1', ?, NULL, '{}', '{\"type\": \"single_shot\"}',"
        " 'camctl-adb', 1000, '{}', 2, 1, 0, NULL, NULL, NULL, NULL, NULL, NULL,"
        " NULL, 1, 1, 1)",
        (_NOW,),
    )
    _seed_activity(connection, 1)
    connection.commit()
    return owned


def _value(owned, sql: str, *params):
    row = owned.connection.execute(sql, params).fetchone()
    assert row is not None, f"查询无结果: {sql}"
    return row


pytestmark = pytest.mark.asyncio


async def test_photo_grant_and_completion_by_return(tmp_path: Path) -> None:
    """照片共用拍摄设备竞争；完成后返回契约以响应保存成功。"""
    owned = _environment(tmp_path)
    scheduling = SchedulingRepository()
    operations = OperationRepository()
    try:
        granted = scheduling.grant_start(
            GrantRequest(
                device_id="cam-1",
                action_id=1,
                window=_WINDOW,
                trusted_wall_now=_NOW,
                config=AttemptConfig(max_attempts=1, timeout_s=Decimal("10")),
                occurred_at=_NOW,
            ),
            new_operation_key(),
            owned,
        )
        assert granted.kind is DbOutcomeKind.COMPLETED
        ticket = granted.value.ticket

        # 驱动替身：完成后返回契约的可靠完成响应（含照片观察）。
        outcome = validate_outcome(
            ticket,
            CallOutcome(
                status=AttemptStatus.SUCCEEDED,
                error=None,
                effect=EffectState.CONFIRMED,
                settlement=Settlement(
                    basis=SettlementBasis.OBSERVED,
                    evidence=EvidenceValue(
                        type="operation_returned", version=1, data={}
                    ),
                ),
                observations=(
                    DeviceObservation(
                        type="photo_taken", version=1, data={"activity_id": "1"}
                    ),
                ),
            ),
            _CONTRACTS,
        )
        finish = operations.finish_attempt(
            AttemptFinish(ticket=ticket, outcome=outcome, occurred_at=_NOW),
            new_operation_key(),
            owned,
        )
        assert finish.kind is DbOutcomeKind.COMPLETED
        saved = _value(
            owned,
            "SELECT a.status, a.effect_state FROM operation_attempts a"
            " JOIN operation_runs r ON a.run_id = r.id"
            " WHERE r.responsibility_key = 'start/1'",
        )
        assert saved[0] == 2 and saved[1] == 3

        decision = decide_photo(
            PhotoState(
                action_terminal=False,
                canceled=False,
                dispatched=True,
                response_completed=True,
                response_failed=False,
                effect_unknown=False,
                stop_supported=False,
            ),
            CaptureAssessment(complete=False),
            PhotoCompletion.COMPLETED_ON_RETURN,
        )
        assert decision is PhotoDecision.REGISTER_SUCCESS
    finally:
        owned.connection.close()


async def test_photo_cancel_before_dispatch_keeps_zero_calls(tmp_path: Path) -> None:
    """取消在派发前生效：不调用驱动，次数保留为一次未派发。"""
    owned = _environment(tmp_path)
    scheduling = SchedulingRepository()
    operations = OperationRepository()
    try:
        owned.connection.execute("BEGIN IMMEDIATE")
        owned.connection.execute("UPDATE actions SET cancel_requested = 1 WHERE id = 1")
        owned.connection.commit()

        rejected = scheduling.grant_start(
            GrantRequest(
                device_id="cam-1",
                action_id=1,
                window=_WINDOW,
                trusted_wall_now=_NOW,
                config=AttemptConfig(max_attempts=1, timeout_s=Decimal("10")),
                occurred_at=_NOW,
            ),
            new_operation_key(),
            owned,
        )
        assert rejected.value.reason == "canceled"
        calls = _value(owned, "SELECT COUNT(*) FROM operation_attempts")
        assert calls[0] == 0

        decision = decide_photo(
            PhotoState(
                action_terminal=False,
                canceled=True,
                dispatched=False,
                response_completed=False,
                response_failed=False,
                effect_unknown=False,
                stop_supported=False,
            ),
            CaptureAssessment(complete=False),
            PhotoCompletion.SENT_ONLY,
        )
        assert decision is PhotoDecision.NOT_DISPATCHED_CANCELED
    finally:
        owned.connection.close()

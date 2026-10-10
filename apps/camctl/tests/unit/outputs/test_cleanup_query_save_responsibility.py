"""存在性查询只在完整实际结果可靠保存后交付观察。

查询返回和保存完成是两个独立事实。保存回滚、提交未知或仅返回
既有终态时，不能把在场、缺席或无可靠观察交给清理消费者。
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.contracts.values import ConsistencyError
from camctl.devices.bindings import DeviceBinding
from camctl.devices.evidence import (
    DeviceObservation,
    EvidenceContract,
    EvidenceRegistry,
)
from camctl.devices.ports import DeviceCallResult, StateQueryDriver
from camctl.operations.attempts import (
    AttemptConfig,
    AttemptFinish,
    FinishAttemptResult,
    FinishDisposition,
    RetryWaitGate,
    RunStatus,
)
from camctl.operations.models import (
    AttemptStatus,
    AttemptTicket,
    CallInfo,
    CallOutcome,
    EffectState,
    ErrorValue,
    EvidenceValue,
    Settlement,
    SettlementBasis,
)
from camctl.operations.validation import OutcomeValidationError
from camctl.outputs.cleanup_flow import CleanupQueryConsumer, CleanupRuntime, _run_query
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection


pytestmark = pytest.mark.asyncio

_ITEM_ID = 91
_RETURNED_AT = 1_750_000_000_123_456
_TICKET = AttemptTicket(
    attempt_id=2,
    operation="query",
    target_id="91",
    responsibility_key="exists/91",
    run_id=7,
)
_PRESENCE = EvidenceContract(
    type="file_presence",
    version=1,
    operation="query",
    fields=frozenset({"cleanup_item_id", "present"}),
    identity_field="cleanup_item_id",
)
_SETTLEMENT = EvidenceContract(
    type="operation_returned",
    version=1,
    operation="query",
    fields=frozenset({"terminate_grace_s"}),
)
_EVIDENCE = EvidenceRegistry((_PRESENCE, _SETTLEMENT))
_OBSERVATIONS = (
    pytest.param(True, id="present"),
    pytest.param(False, id="absent"),
    pytest.param(None, id="unknown"),
)


def _actual_result(
    present: bool | None,
    *,
    status: AttemptStatus = AttemptStatus.SUCCEEDED,
    basis: SettlementBasis = SettlementBasis.OBSERVED,
) -> DeviceCallResult:
    observations = () if present is None else (
        DeviceObservation(
            type="file_presence",
            version=1,
            data={"cleanup_item_id": "91", "present": present},
        ),
    )
    error = None if status is AttemptStatus.SUCCEEDED else ErrorValue(
        code="device_error",
        stage="query",
        details={"reason": "查询通信中断\n保留实际详情", "exit_code": 7},
    )
    outcome = CallOutcome(
        status=status,
        error=error,
        effect=EffectState.UNKNOWN if present is None else EffectState.CONFIRMED,
        settlement=Settlement(
            basis=basis,
            evidence=EvidenceValue(
                type="operation_returned",
                version=1,
                data={"terminate_grace_s": Decimal("0.125")},
            ),
        ),
        observations=observations,
        call_info=CallInfo(
            local_exit_code=0 if status is AttemptStatus.SUCCEEDED else 1,
            remote_exit_code=0 if status is AttemptStatus.SUCCEEDED else 7,
        ),
    )
    return DeviceCallResult.from_outcome(outcome)


def _saved(status: AttemptStatus) -> DbOutcome[FinishAttemptResult]:
    return DbOutcome(
        DbOutcomeKind.COMPLETED,
        FinishAttemptResult(FinishDisposition.SAVED, status, RunStatus.ACTIVE),
    )


def _runtime(
    actual: DeviceCallResult,
    saved: DbOutcome[FinishAttemptResult],
) -> CleanupRuntime:
    connection = create_autospec(sqlite3.Connection, instance=True)

    def read_cursor(statement, parameters=()):
        cursor = create_autospec(sqlite3.Cursor, instance=True)
        # 已固定成员对应的设备文件定位；其他读取没有原结果证明。
        cursor.fetchone.return_value = (
            (5, "file-5", '{"path":"/DCIM/file.MP4"}')
            if "JOIN device_files f ON f.id = o.device_file_id" in statement
            else None
        )
        cursor.fetchall.return_value = []
        return cursor

    connection.execute.side_effect = read_cursor
    owned = OwnedConnection(
        connection,
        DatabaseMetadata(
            application_id="camctl",
            instance_id="1234567890abcdef1234567890abcdef",
            format_version=1,
            staging_path="/test/staging",
            ready_path="/test/ready",
            processing_path="/test/processing",
        ),
    )
    operations = create_autospec(OperationRepository, instance=True)
    operations.finish_attempt.return_value = saved
    driver = create_autospec(StateQueryDriver, instance=True)
    driver.query_state.return_value = actual
    return CleanupRuntime(
        owned=owned,
        outputs=create_autospec(OutputsRepository, instance=True),
        operations=operations,
        driver=driver,
        evidence=_EVIDENCE,
        binding_of=lambda item_id: DeviceBinding("cam-1", "camctl-adb"),
        occurred_at=lambda: _RETURNED_AT,
        delete_config=AttemptConfig(3, Decimal(10), Decimal(1)),
        query_config=AttemptConfig(3, Decimal(10), Decimal(1)),
        monotonic_ns=lambda: 300,
        retry_gate=RetryWaitGate({"exists/91": 100, "delete/91": 200, "exists/92": 250}),
    )


@pytest.mark.parametrize("present", _OBSERVATIONS)
@pytest.mark.parametrize("kind", [DbOutcomeKind.ROLLED_BACK, DbOutcomeKind.UNKNOWN])
async def test_unreliable_result_save_does_not_deliver_observation(present, kind):
    actual = _actual_result(present)
    runtime = _runtime(actual, DbOutcome(kind, error=sqlite3.OperationalError("结果事务未完成")))
    anchors = dict(runtime.retry_gate.anchors)

    with pytest.raises(ConsistencyError):
        await _run_query(runtime, _TICKET, _ITEM_ID)

    assert runtime.retry_gate.anchors == anchors
    finish, _, owned = runtime.operations.finish_attempt.call_args.args
    assert finish.ticket is _TICKET
    assert finish.outcome.outcome is actual.outcome
    assert owned is runtime.owned


@pytest.mark.parametrize("present", _OBSERVATIONS)
async def test_saved_full_actual_result_delivers_its_observation(present):
    actual = _actual_result(present)
    runtime = _runtime(actual, _saved(AttemptStatus.SUCCEEDED))

    assert await _run_query(runtime, _TICKET, _ITEM_ID) is present


@pytest.mark.parametrize("present", _OBSERVATIONS)
async def test_already_ended_without_original_full_result_does_not_deliver_observation(present):
    actual = _actual_result(present)
    # 仓储可以可靠返回此前 UNKNOWN 终态，但它没有证明本次完整申请。
    old_result = FinishAttemptResult(
        FinishDisposition.ALREADY_ENDED,
        AttemptStatus.UNKNOWN,
        RunStatus.ACTIVE,
    )
    runtime = _runtime(actual, DbOutcome(DbOutcomeKind.COMPLETED, old_result))
    anchors = dict(runtime.retry_gate.anchors)

    with pytest.raises(ConsistencyError):
        await _run_query(runtime, _TICKET, _ITEM_ID)

    assert runtime.retry_gate.anchors == anchors


@pytest.mark.parametrize("value", [
    pytest.param(None, id="missing-value"),
    pytest.param(FinishAttemptResult(FinishDisposition.SAVED, None, RunStatus.ACTIVE),
                 id="missing-attempt-status"),
    pytest.param(FinishAttemptResult(FinishDisposition.SAVED, AttemptStatus.SUCCEEDED, None),
                 id="missing-run-status"),
    pytest.param(FinishAttemptResult(FinishDisposition.SAVED, AttemptStatus.FAILED, RunStatus.ACTIVE),
                 id="contradictory-attempt-status"),
])
async def test_completed_without_consistent_finish_value_is_diagnosed(value):
    runtime = _runtime(_actual_result(True), DbOutcome(DbOutcomeKind.COMPLETED, value))
    anchors = dict(runtime.retry_gate.anchors)

    with pytest.raises(ConsistencyError):
        await _run_query(runtime, _TICKET, _ITEM_ID)

    assert runtime.retry_gate.anchors == anchors


@pytest.mark.parametrize("ticket", [
    pytest.param(replace(_TICKET, target_id="92"), id="another-target"),
    pytest.param(replace(_TICKET, responsibility_key="exists/92"), id="another-responsibility"),
])
async def test_ticket_for_another_member_cannot_deliver_query_observation(ticket):
    # 空观察本身合法，不能因没有观察身份成员而略过票据与成员的关联。
    runtime = _runtime(_actual_result(None), _saved(AttemptStatus.SUCCEEDED))

    with pytest.raises(ConsistencyError):
        await _run_query(runtime, ticket, _ITEM_ID)

    runtime.driver.query_state.assert_not_awaited()
    runtime.operations.finish_attempt.assert_not_called()


@pytest.mark.parametrize("present", _OBSERVATIONS)
@pytest.mark.parametrize("status", [AttemptStatus.FAILED, AttemptStatus.UNKNOWN])
@pytest.mark.parametrize("basis", [SettlementBasis.OBSERVED, SettlementBasis.ASSUMED])
async def test_complete_driver_result_is_saved_without_reinterpretation(present, status, basis):
    actual = _actual_result(present, status=status, basis=basis)
    runtime = _runtime(actual, _saved(status))

    assert await _run_query(runtime, _TICKET, _ITEM_ID) is present

    finish, _, owned = runtime.operations.finish_attempt.call_args.args
    assert isinstance(finish, AttemptFinish)
    assert finish.ticket is _TICKET
    assert finish.outcome.ticket is _TICKET
    assert finish.outcome.outcome is actual.outcome
    assert finish.outcome.outcome.status is status
    assert finish.outcome.outcome.error is actual.outcome.error
    assert finish.outcome.outcome.settlement is actual.outcome.settlement
    assert finish.outcome.outcome.call_info is actual.outcome.call_info
    assert finish.occurred_at == _RETURNED_AT
    assert owned is runtime.owned


@pytest.mark.parametrize("present", _OBSERVATIONS)
@pytest.mark.parametrize("kind", [DbOutcomeKind.ROLLED_BACK, DbOutcomeKind.UNKNOWN])
@pytest.mark.parametrize("fresh_connection", [False, True], ids=["same-connection", "fresh-connection"])
async def test_retry_saves_original_prepared_result_without_querying_again(present, kind, fresh_connection):
    actual = _actual_result(present, status=AttemptStatus.FAILED)
    runtime = _runtime(actual, _saved(AttemptStatus.FAILED))
    runtime.operations.finish_attempt.side_effect = (
        DbOutcome(kind, error=sqlite3.OperationalError("第一次结果保存未完成")),
        _saved(AttemptStatus.FAILED),
    )
    first_driver = runtime.driver
    with pytest.raises(ConsistencyError):
        await _run_query(runtime, _TICKET, _ITEM_ID)
    first_finish, first_key, _ = runtime.operations.finish_attempt.call_args.args

    changed = _runtime(_actual_result(None), _saved(AttemptStatus.SUCCEEDED))
    runtime.driver = changed.driver
    if fresh_connection:
        runtime.owned = changed.owned
    runtime.evidence = EvidenceRegistry(())
    runtime.query_config = AttemptConfig(9, Decimal(20), Decimal(5))
    runtime.occurred_at = lambda: _RETURNED_AT + 9_000_000
    runtime.monotonic_ns = lambda: 9_000

    assert await _run_query(runtime, _TICKET, _ITEM_ID) is present

    second_finish, second_key, owned = runtime.operations.finish_attempt.call_args.args
    assert second_finish is first_finish
    assert second_key == first_key
    assert second_finish.outcome.outcome is actual.outcome
    assert second_finish.occurred_at == _RETURNED_AT
    assert owned is runtime.owned
    first_driver.query_state.assert_awaited_once()
    changed.driver.query_state.assert_not_awaited()


@pytest.mark.parametrize("present", _OBSERVATIONS)
@pytest.mark.parametrize("first_kind", [DbOutcomeKind.ROLLED_BACK, DbOutcomeKind.UNKNOWN])
@pytest.mark.parametrize("second_kind", [DbOutcomeKind.ROLLED_BACK, DbOutcomeKind.UNKNOWN])
async def test_second_save_failure_keeps_original_result_for_later_redelivery(present, first_kind, second_kind):
    actual = _actual_result(present, status=AttemptStatus.FAILED)
    runtime = _runtime(actual, _saved(AttemptStatus.FAILED))
    runtime.operations.finish_attempt.side_effect = (
        DbOutcome(first_kind, error=sqlite3.OperationalError("第一次结果保存未完成")),
        DbOutcome(second_kind, error=sqlite3.OperationalError("再次结果保存未完成")),
        _saved(AttemptStatus.FAILED),
    )
    anchors = dict(runtime.retry_gate.anchors)
    with pytest.raises(ConsistencyError):
        await _run_query(runtime, _TICKET, _ITEM_ID)
    first_finish, first_key, _ = runtime.operations.finish_attempt.call_args.args
    runtime.occurred_at = lambda: _RETURNED_AT + 10_000_000

    with pytest.raises(ConsistencyError):
        await _run_query(runtime, _TICKET, _ITEM_ID)

    assert runtime.retry_gate.anchors == anchors
    assert await _run_query(runtime, _TICKET, _ITEM_ID) is present
    finishes = [call.args[0] for call in runtime.operations.finish_attempt.call_args_list]
    keys = [call.args[1] for call in runtime.operations.finish_attempt.call_args_list]
    assert len(finishes) == 3
    assert all(finish is first_finish for finish in finishes)
    assert all(key == first_key for key in keys)
    assert first_finish.outcome.outcome is actual.outcome
    assert first_finish.occurred_at == _RETURNED_AT
    runtime.driver.query_state.assert_awaited_once()


@pytest.mark.parametrize("present", _OBSERVATIONS)
@pytest.mark.parametrize("kind", [DbOutcomeKind.ROLLED_BACK, DbOutcomeKind.UNKNOWN])
async def test_pending_result_is_not_saved_into_another_database_instance(present, kind):
    actual = _actual_result(present, status=AttemptStatus.FAILED)
    runtime = _runtime(actual, DbOutcome(kind, error=sqlite3.OperationalError("原结果尚未保存")))
    original_owned = runtime.owned
    with pytest.raises(ConsistencyError):
        await _run_query(runtime, _TICKET, _ITEM_ID)
    first_finish, first_key, _ = runtime.operations.finish_attempt.call_args.args
    runtime.operations.finish_attempt.return_value = _saved(AttemptStatus.FAILED)
    another_owned = _runtime(_actual_result(None), _saved(AttemptStatus.SUCCEEDED)).owned
    runtime.owned = replace(
        another_owned,
        metadata=replace(another_owned.metadata, instance_id="abcdef1234567890abcdef1234567890"),
    )

    with pytest.raises(ConsistencyError):
        await _run_query(runtime, _TICKET, _ITEM_ID)

    assert runtime.operations.finish_attempt.call_count == 1
    runtime.driver.query_state.assert_awaited_once()
    runtime.owned = original_owned
    assert await _run_query(runtime, _TICKET, _ITEM_ID) is present
    restored_finish, restored_key, owned = runtime.operations.finish_attempt.call_args.args
    assert restored_finish is first_finish
    assert restored_key == first_key
    assert restored_finish.outcome.outcome is actual.outcome
    assert owned is original_owned
    runtime.driver.query_state.assert_awaited_once()


async def test_validation_failure_retains_original_diagnostic_without_querying_again():
    original = _actual_result(True)
    invalid_outcome = replace(
        original.outcome,
        settlement=Settlement(
            SettlementBasis.OBSERVED,
            EvidenceValue("unregistered_query_finish", 1, {}),
        ),
    )
    actual = DeviceCallResult.from_outcome(invalid_outcome)
    runtime = _runtime(actual, _saved(AttemptStatus.SUCCEEDED))
    first_driver = runtime.driver
    with pytest.raises(OutcomeValidationError) as first_error:
        await _run_query(runtime, _TICKET, _ITEM_ID)
    changed = _runtime(_actual_result(False), _saved(AttemptStatus.SUCCEEDED))
    runtime.driver = changed.driver
    runtime.evidence = _EVIDENCE.with_contract(EvidenceContract(
        "unregistered_query_finish", 1, "query", frozenset(),
    ))

    with pytest.raises(OutcomeValidationError) as repeated_error:
        await _run_query(runtime, _TICKET, _ITEM_ID)

    assert repeated_error.value is first_error.value
    first_driver.query_state.assert_awaited_once()
    changed.driver.query_state.assert_not_awaited()
    runtime.operations.finish_attempt.assert_not_called()


@pytest.mark.parametrize("data", [
    pytest.param({"cleanup_item_id": "91", "present": 0}, id="integer-zero"),
    pytest.param({"cleanup_item_id": "91", "present": 1}, id="integer-one"),
    pytest.param({"cleanup_item_id": "91", "present": Decimal(1)}, id="decimal-one"),
    pytest.param({"cleanup_item_id": "91", "present": "true"}, id="string-true"),
    pytest.param({"cleanup_item_id": "91", "present": "false"}, id="string-false"),
    pytest.param({"cleanup_item_id": "91", "present": None}, id="null"),
    pytest.param({"cleanup_item_id": "91", "present": []}, id="array"),
    pytest.param({"cleanup_item_id": "91", "present": {}}, id="object"),
    pytest.param({"cleanup_item_id": "91"}, id="missing-present"),
])
async def test_presence_member_must_be_a_boolean_instead_of_unknown(data):
    original = _actual_result(True)
    actual = DeviceCallResult.from_outcome(replace(
        original.outcome,
        observations=(DeviceObservation("file_presence", 1, data),),
    ))
    runtime = _runtime(actual, _saved(AttemptStatus.SUCCEEDED))
    anchors = dict(runtime.retry_gate.anchors)

    with pytest.raises(OutcomeValidationError):
        await _run_query(runtime, _TICKET, _ITEM_ID)

    assert runtime.retry_gate.anchors == anchors
    runtime.operations.finish_attempt.assert_not_called()


@pytest.mark.parametrize("observations", [
    pytest.param((DeviceObservation("file_presence", 2, {"cleanup_item_id": "91", "present": True}),),
                 id="unknown-version"),
    pytest.param((DeviceObservation("file_presence", 1, {"cleanup_item_id": "92", "present": True}),),
                 id="another-identity"),
    pytest.param((DeviceObservation("file_presence", 1, {"present": True}),),
                 id="missing-identity"),
    pytest.param((DeviceObservation("file_presence", 1, {"cleanup_item_id": "91", "present": True}),
                  DeviceObservation("file_presence", 1, {"cleanup_item_id": "91", "present": False})),
                 id="too-many-observations"),
])
async def test_invalid_presence_evidence_is_rejected_by_actual_validator(observations):
    original = _actual_result(True)
    actual = DeviceCallResult.from_outcome(replace(original.outcome, observations=observations))
    runtime = _runtime(actual, _saved(AttemptStatus.SUCCEEDED))
    anchors = dict(runtime.retry_gate.anchors)

    with pytest.raises(OutcomeValidationError):
        await _run_query(runtime, _TICKET, _ITEM_ID)

    assert runtime.retry_gate.anchors == anchors
    runtime.operations.finish_attempt.assert_not_called()


@pytest.mark.parametrize("observation", [None, {"type": "file_presence"}, "file_presence"])
async def test_invalid_observation_structure_keeps_original_query_diagnostic(observation):
    original = _actual_result(True)
    actual = DeviceCallResult.from_outcome(replace(
        original.outcome, observations=(observation,)))
    runtime = _runtime(actual, _saved(AttemptStatus.SUCCEEDED))

    for _ in range(2):
        with pytest.raises(OutcomeValidationError):
            await _run_query(runtime, _TICKET, _ITEM_ID)

    runtime.driver.query_state.assert_awaited_once()
    runtime.operations.finish_attempt.assert_not_called()


@pytest.mark.parametrize("present", [True, None], ids=["present", "unknown"])
async def test_saved_cancel_query_does_not_grant_an_ordinary_retry(present):
    runtime = _runtime(_actual_result(present), _saved(AttemptStatus.SUCCEEDED))
    anchors = dict(runtime.retry_gate.anchors)

    assert await _run_query(runtime, _TICKET, _ITEM_ID, CleanupQueryConsumer.CANCEL) is present

    assert runtime.retry_gate.anchors == anchors

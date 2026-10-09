"""取消目标与必要 RESULTS 失败的完整事务及原键核实。"""

from dataclasses import replace
from decimal import Decimal
import json

import pytest

from camctl.capture.media import RecordingFailure
from camctl.contracts.values import new_operation_key
from camctl.operations.attempts import AttemptConfig
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import (
    CaptureRepository, FinishBindingFailure, FinishCanceledCapture,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import TransactionError

from ..capture.test_capture_contract import _NOW
from ..operations.test_result_reuse import _FaultConnection
from .test_binding_canceled_results import _stopped_canceled_timelapse
from .test_binding_changes import environment
from .test_binding_transactions import _CommitFailure


async def _command(home, *, result_started):
    owned = await _stopped_canceled_timelapse(home, result_started=result_started)
    command = FinishBindingFailure(
        action_id=13, occurred_at=_NOW, canceled=True,
        failure=RecordingFailure("device_binding_unavailable", {
            "device_id": "cam-1", "expected_driver_id": "camctl-adb", "reason": "missing"}),
        responsibility_keys=("results/13",) if result_started else (),
        check_config=AttemptConfig(6, Decimal("17"), Decimal("2")))
    return owned, command


@pytest.mark.asyncio
@pytest.mark.parametrize("result_started", [False, True])
@pytest.mark.parametrize("phase", ["projection", "commit_before", "commit_after"])
async def test_canceled_results_failure_recovers_complete_transaction(
        environment, result_started, phase):
    _, _, home = environment
    directory = home / "atomic-results"
    owned, command = await _command(directory, result_started=result_started)
    repository = CaptureRepository()
    key = new_operation_key()
    before = tuple(owned.connection.iterdump())
    stop_before = owned.connection.execute(
        "SELECT * FROM operation_runs WHERE responsibility_key = 'stop/13'").fetchone()
    attempts_before = tuple(owned.connection.execute("SELECT * FROM operation_attempts"))
    faulty = (_FaultConnection(owned.connection, "UPDATE operation_runs")
              if phase == "projection" else _CommitFailure(owned.connection, phase == "commit_after"))
    try:
        result = repository.finish_binding_failure(command, key, replace(owned, connection=faulty))
        assert result.kind is (DbOutcomeKind.ROLLED_BACK if phase == "projection" else DbOutcomeKind.UNKNOWN)
        if phase == "projection":
            assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()
    reopened = open_existing(directory / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
    try:
        if phase == "commit_before":
            assert tuple(reopened.connection.iterdump()) == before
        recovered = repository.finish_binding_failure(command, key, reopened)
        assert recovered.kind is DbOutcomeKind.COMPLETED, recovered.error
        assert reopened.connection.execute(
            "SELECT status, cancel_requested FROM actions WHERE id = 13").fetchone() == (6, 1)
        assert reopened.connection.execute(
            "SELECT * FROM operation_runs WHERE responsibility_key = 'stop/13'"
        ).fetchone() == stop_before
        assert tuple(reopened.connection.execute("SELECT * FROM operation_attempts")) == attempts_before
        result_row = reopened.connection.execute(
            "SELECT status, attempts_used, max_attempts_used, timeout_s_json, retry_interval_s_json,"
            " error_json FROM operation_runs WHERE responsibility_key = 'results/13'").fetchone()
        assert result_row is not None
        assert result_row[:2] == (4, int(result_started))
        if not result_started:
            assert result_row[2:5] == (6, "17", "2")
        assert json.loads(result_row[5]) == {
            "code": "device_binding_unavailable", "stage": "execution", "details": dict(command.failure.details)}
        events = reopened.connection.execute(
            "SELECT event_type, json_extract(body_json, '$.reason') FROM history_events"
            " WHERE transaction_id = (SELECT id FROM history_transactions WHERE operation_key = ?)"
            " ORDER BY id", (str(key),)).fetchall()
        assert events[0] == (8, 4)
        flow_events = [event for event in events if event[0] == 10]
        assert flow_events == ([(10, 3)] if result_started else [(10, 1), (10, 3)])
        assert [event[0] for event in events[:-len(flow_events)]] in ([8], [8, 9])
        saved = tuple(reopened.connection.iterdump())
        repeated = repository.finish_binding_failure(command, key, reopened)
        assert repeated.kind is DbOutcomeKind.COMPLETED, repeated.error
        assert tuple(reopened.connection.iterdump()) == saved
    finally:
        reopened.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("result_started, changed", [
    (False, "time"), (False, "reason"), (False, "check_config"), (False, "responsibilities"),
    (True, "time"), (True, "reason"), (True, "responsibilities"),
])
async def test_canceled_results_original_key_rejects_changed_input(environment, result_started, changed):
    _, _, home = environment
    owned, command = await _command(home / "input-results", result_started=result_started)
    try:
        repository = CaptureRepository()
        key = new_operation_key()
        saved = repository.finish_binding_failure(command, key, owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        if changed == "time":
            different = replace(command, occurred_at=command.occurred_at + 1)
        elif changed == "reason":
            different = replace(command, failure=RecordingFailure("device_binding_unavailable", {
                "device_id": "cam-1", "expected_driver_id": "camctl-adb", "reason": "mismatch",
                "actual_driver_id": "alternate-camera"}))
        elif changed == "check_config":
            different = replace(command, check_config=AttemptConfig(7, Decimal("17"), Decimal("2")))
        else:
            different = replace(command, responsibility_keys=() if result_started else ("results/13",))
        before = tuple(owned.connection.iterdump())

        changed_result = repository.finish_binding_failure(different, key, owned)

        assert changed_result.kind is DbOutcomeKind.ROLLED_BACK
        assert isinstance(changed_result.error, TransactionError)
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_canceled_results_original_key_rejects_action_only_group(environment):
    _, _, home = environment
    owned, command = await _command(home / "partial-results", result_started=False)
    try:
        repository = CaptureRepository()
        key = new_operation_key()
        partial = repository.finish_canceled_capture(FinishCanceledCapture(13, _NOW), key, owned)
        assert partial.kind is DbOutcomeKind.COMPLETED, partial.error
        before = tuple(owned.connection.iterdump())

        result = repository.finish_binding_failure(command, key, owned)

        assert result.kind is DbOutcomeKind.ROLLED_BACK
        assert isinstance(result.error, TransactionError)
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()

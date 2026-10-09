"""拍摄绑定失败的完整事务、未知提交与原操作键输入核实。"""

from dataclasses import replace
import sqlite3

import pytest

from camctl.capture.media import RecordingFailure
from camctl.contracts.values import new_operation_key
from camctl.operations.attempts import AttemptConfig
from camctl.operations.models import (
    AttemptStatus, CallOutcome, EffectState, ErrorValue, EvidenceValue,
    Settlement, SettlementBasis,
)
from camctl.outputs.catalog import OutputCatalogFacts
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import (
    CaptureRepository, FinishBindingFailure, FinishCapture,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import TransactionError

from ..capture.test_cancel_late_start import _cancel
from ..capture.test_capture_contract import _environment, _runtime, _NOW
from ..operations.test_result_reuse import _FaultConnection
from .test_binding_changes import environment


def _unknown_owned(home, canceled):
    home.mkdir()
    owned = _environment(home, ((12, 2),))
    runtime = _runtime(owned)
    ticket, reason = runtime.grant(runtime.action(12))
    assert ticket is not None, reason
    runtime.finish(ticket, CallOutcome(
        status=AttemptStatus.UNKNOWN, effect=EffectState.UNKNOWN,
        error=ErrorValue("transport_timeout", "transport"), observations=(),
        settlement=Settlement(SettlementBasis.OBSERVED,
                              EvidenceValue("operation_returned", 1, {})),
    ))
    if canceled:
        _cancel(owned, 12)
    return owned, FinishBindingFailure(
        action_id=12, occurred_at=_NOW, canceled=canceled,
        failure=RecordingFailure("device_binding_unavailable", {
            "device_id": "cam-1", "expected_driver_id": "camctl-adb", "reason": "missing",
        }), stop_config=runtime.stop_config,
        responsibility_keys=() if canceled else ("start/12",),
    )


class _CommitFailure:
    """真实 SQLite 的 COMMIT 完成前或完成后返回错误，其他接口透传。"""

    def __init__(self, connection, after):
        self.connection = connection
        self.after = after

    def execute(self, sql, parameters=()):
        if sql == "COMMIT":
            if self.after:
                self.connection.execute(sql, parameters)
            raise sqlite3.OperationalError("injected commit response failure")
        return self.connection.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self.connection, name)


@pytest.mark.parametrize("canceled", [False, True])
@pytest.mark.parametrize("phase", ["projection", "commit_before", "commit_after"])
def test_binding_failure_recovers_whole_transaction(environment, canceled, phase):
    _, _, home = environment
    directory = home / "atomic"
    owned, command = _unknown_owned(directory, canceled)
    repository = CaptureRepository()
    key = new_operation_key()
    before = tuple(owned.connection.iterdump())
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
            "SELECT status, cancel_requested FROM actions WHERE id = 12",
        ).fetchone() == (6 if canceled else 4, int(canceled))
        assert reopened.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone() == (1,)
        assert reopened.connection.execute(
            "SELECT activity_state, occupancy_state, dispatch_state, started_at FROM device_activities WHERE id = 12",
        ).fetchone() == (1, 1, 2, None)
        assert reopened.connection.execute(
            "SELECT status, attempts_used FROM operation_runs WHERE responsibility_key = ?",
            ("stop/12" if canceled else "start/12",),
        ).fetchone() == (4, 0 if canceled else 1)
        saved = tuple(reopened.connection.iterdump())
        repeated = repository.finish_binding_failure(command, key, reopened)
        assert repeated.kind is DbOutcomeKind.COMPLETED, repeated.error
        assert tuple(reopened.connection.iterdump()) == saved
        transaction = reopened.connection.execute(
            "SELECT id, first_event_id, last_event_id FROM history_transactions WHERE operation_key = ?", (str(key),),
        ).fetchone()
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM history_events WHERE transaction_id = ?", (transaction[0],),
        ).fetchone()[0] == transaction[2] - transaction[1] + 1
    finally:
        reopened.connection.close()


def test_binding_failure_rejects_action_only_transaction_for_original_key(environment):
    _, _, home = environment
    owned, command = _unknown_owned(home / "incomplete", False)
    try:
        repository = CaptureRepository()
        key = new_operation_key()
        partial = repository.finish_capture(FinishCapture(
            action_id=12, drafts=(), catalog_facts=OutputCatalogFacts(12, True),
            occurred_at=_NOW, failure=command.failure,
        ), key, owned)
        assert partial.kind is DbOutcomeKind.COMPLETED, partial.error
        before = tuple(owned.connection.iterdump())

        result = repository.finish_binding_failure(command, key, owned)

        assert result.kind is DbOutcomeKind.ROLLED_BACK
        assert isinstance(result.error, TransactionError)
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


@pytest.mark.parametrize("changed", ["time", "canceled", "reason", "stop_config"])
def test_binding_failure_original_key_rejects_changed_input(environment, changed):
    _, _, home = environment
    owned, command = _unknown_owned(home / "input", True)
    try:
        repository = CaptureRepository()
        key = new_operation_key()
        saved = repository.finish_binding_failure(command, key, owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        if changed == "time":
            different = replace(command, occurred_at=command.occurred_at + 1)
        elif changed == "canceled":
            different = replace(command, canceled=False)
        elif changed == "stop_config":
            different = replace(command, stop_config=AttemptConfig(
                command.stop_config.max_attempts + 1, command.stop_config.timeout_s,
                command.stop_config.retry_interval_s))
        else:
            different = replace(command, failure=RecordingFailure("device_binding_unavailable", {
                "device_id": "cam-1", "expected_driver_id": "camctl-adb", "reason": "mismatch",
                "actual_driver_id": "alternate-camera",
            }))
        before = tuple(owned.connection.iterdump())

        result = repository.finish_binding_failure(different, key, owned)

        assert result.kind is DbOutcomeKind.ROLLED_BACK
        assert isinstance(result.error, TransactionError)
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


def test_binding_failure_original_key_rejects_changed_responsibility_set(environment):
    _, _, home = environment
    owned, command = _unknown_owned(home / "responsibilities", False)
    try:
        repository = CaptureRepository()
        key = new_operation_key()
        saved = repository.finish_binding_failure(command, key, owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        before = tuple(owned.connection.iterdump())

        result = repository.finish_binding_failure(replace(command, responsibility_keys=()), key, owned)

        assert result.kind is DbOutcomeKind.ROLLED_BACK
        assert isinstance(result.error, TransactionError)
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()

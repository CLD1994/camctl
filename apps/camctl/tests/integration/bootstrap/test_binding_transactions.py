"""拍摄绑定失败的完整事务、未知提交与原操作键输入核实。"""

from dataclasses import replace
import sqlite3
from decimal import Decimal

import pytest

from camctl.capture.media import RecordingFailure
from camctl.contracts.values import new_operation_key
from camctl.operations.attempts import AttemptConfig, AttemptFinish
from camctl.operations.validation import validate_outcome
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.scheduling import SchedulingRepository, GrantRequest
from camctl.scheduling.rules import LaunchWindow
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
from camctl.persistence.repositories.transactions import TransactionError

from ..capture.test_cancel_late_start import _cancel
from ..capture.test_capture_contract import _environment, _EVIDENCE, _NOW


def _unknown_owned(home, canceled):
    home.mkdir()
    owned = _environment(home, ((12, 2),))
    config = AttemptConfig(3, Decimal("10"), Decimal("3"))
    granted = SchedulingRepository().grant_start(GrantRequest(
        "cam-1", 12, LaunchWindow(_NOW, _NOW + 1_000_000), _NOW, config, _NOW),
        new_operation_key(), owned)
    assert granted.kind is DbOutcomeKind.COMPLETED, granted.error
    ticket = granted.value.ticket
    assert ticket is not None, granted.value.reason
    outcome = CallOutcome(
        status=AttemptStatus.UNKNOWN, effect=EffectState.UNKNOWN,
        error=ErrorValue("transport_timeout", "transport"), observations=(),
        settlement=Settlement(SettlementBasis.OBSERVED,
                              EvidenceValue("operation_returned", 1, {})),
    )
    saved = OperationRepository().finish_attempt(AttemptFinish(
        ticket, validate_outcome(ticket, outcome, _EVIDENCE), _NOW), new_operation_key(), owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    if canceled:
        _cancel(owned, 12)
    return owned, FinishBindingFailure(
        action_id=12, occurred_at=_NOW, canceled=canceled,
        failure=RecordingFailure("device_binding_unavailable", {
            "device_id": "cam-1", "expected_driver_id": "camctl-adb", "reason": "missing",
        }), stop_config=config,
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


def _reject_operation_projection(connection):
    """真实 SQLite 在流程投影写入时失败，不绑定 SQL 排版。"""
    connection.execute(
        "CREATE TEMP TRIGGER reject_operation_projection BEFORE UPDATE ON operation_runs"
        " BEGIN SELECT RAISE(ABORT, '流程投影写入失败'); END")
    return connection


@pytest.mark.parametrize("canceled", [False, True])
@pytest.mark.parametrize("phase", ["projection", "commit_before", "commit_after"])
def test_binding_failure_recovers_whole_transaction(tmp_path, canceled, phase):
    directory = tmp_path / "atomic"
    owned, command = _unknown_owned(directory, canceled)
    repository = CaptureRepository()
    key = new_operation_key()
    before = tuple(owned.connection.iterdump())
    faulty = (_reject_operation_projection(owned.connection)
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


def test_binding_failure_rejects_action_only_transaction_for_original_key(tmp_path):
    owned, command = _unknown_owned(tmp_path / "incomplete", False)
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


def test_binding_failure_original_key_rejects_changed_input(tmp_path):
    owned, command = _unknown_owned(tmp_path / "input", True)
    try:
        repository = CaptureRepository()
        key = new_operation_key()
        saved = repository.finish_binding_failure(command, key, owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        changes = {
            "time": replace(command, occurred_at=command.occurred_at + 1),
            "canceled": replace(command, canceled=False),
            "stop_config": replace(command, stop_config=AttemptConfig(
                command.stop_config.max_attempts + 1, command.stop_config.timeout_s,
                command.stop_config.retry_interval_s)),
            "reason": replace(command, failure=RecordingFailure("device_binding_unavailable", {
                "device_id": "cam-1", "expected_driver_id": "camctl-adb", "reason": "mismatch",
                "actual_driver_id": "alternate-camera"})),
        }
        before = tuple(owned.connection.iterdump())
        for changed, different in changes.items():
            result = repository.finish_binding_failure(different, key, owned)
            assert result.kind is DbOutcomeKind.ROLLED_BACK, changed
            assert isinstance(result.error, TransactionError), changed
            assert not owned.connection.in_transaction, changed
            assert tuple(owned.connection.iterdump()) == before, changed
    finally:
        owned.connection.close()


def test_binding_failure_original_key_rejects_changed_responsibility_set(tmp_path):
    owned, command = _unknown_owned(tmp_path / "responsibilities", False)
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

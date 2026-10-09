"""原读取尚无可靠结束结果时，绑定异常不代替收场或破坏保护。"""

from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path

import pytest

from camctl.bootstrap.obtain_assembly import obtain_flow, session_obtain_assembly
from camctl.bootstrap.application import query_work_facts
from camctl.capture.recovery import (
    RecoveryBoundary, RecoveryBlockedReason, recovery_registry)
from camctl.contracts.enums import enum_for
from camctl.contracts.values import new_operation_key
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
from camctl.operations.attempts import AttemptConfig, AttemptIntent, AttemptTarget, OperationKind
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.session.work import classify_work

from .test_output_binding_changes import _NOW, _changed, _registry, environment
from .test_output_binding_transactions import pending_read

pytestmark = pytest.mark.asyncio


def _start_unended_read(owned, copy_id):
    action_id, copy_round = owned.connection.execute(
        "SELECT r.action_id,c.round FROM operation_runs r JOIN file_copies c ON c.id=r.copy_id"
        " WHERE c.id=?", (copy_id,)).fetchone()
    started = OperationRepository().begin_attempt(AttemptIntent(
        "read", action_id, OperationKind.READ_FILE, AttemptTarget(copy_id=copy_id), None,
        AttemptConfig(3, Decimal("60"), Decimal("0")), _NOW, copy_round=copy_round),
        new_operation_key(), owned)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    assert started.value.ticket is not None
    return started.value.ticket


def _recovering_runtime(environment, command, change, *, boundary, horizon,
                        declared=True, evidence=True, lookup=True):
    cfg, owned, _context, driver = environment
    current = _changed(cfg, change)
    runtime = session_obtain_assembly(
        devices=current.devices, drivers=_registry(driver), staging=Path(cfg.paths.staging),
        ready=Path(cfg.paths.ready), processing=Path(cfg.paths.processing), segment_size=4,
        occurred_at=lambda: _NOW, monotonic_ns=lambda: 0)(owned)
    original = _registry(driver).entry("camctl-adb")
    original = replace(original,
        declaration=replace(original.declaration,
            adb_foreground_recovery_operations=frozenset({"read"}) if declared else frozenset()),
        evidence=EvidenceRegistry((EvidenceContract(
            "adb_foreground_recovery", 1, "read", frozenset()),)) if evidence else EvidenceRegistry(()))
    diagnostics = []
    # 隔离会话装配，在真实消费者边界注入其正式恢复输入。
    runtime.recovery_boundary = boundary
    runtime.recovery_max_event_id = horizon
    runtime.recovery_evidence_for = (
        lambda saved, operation: recovery_registry(original if saved.driver_id == original.driver_id else None, operation)
    ) if lookup else None
    runtime.on_recovery_diagnostic = diagnostics.append
    runtime.last_recovery_diagnostic = None
    return runtime, diagnostics


@pytest.mark.parametrize("change", ["missing", "mismatch"])
async def test_unended_original_read_keeps_responsibility_without_state_error(pending_read, environment, change):
    cfg, owned, command = pending_read
    _same_cfg, _same_owned, context, driver = environment
    ticket = _start_unended_read(owned, command.copy_id)
    assert owned.connection.execute(
        "SELECT status,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
        (ticket.run_id, ticket.attempt_id)).fetchone() == (1, None)
    before = tuple(owned.connection.iterdump())
    calls = tuple(driver.reads)
    rejected = OutputsRepository().fail_read_binding(command, new_operation_key(), owned)
    assert rejected.kind is DbOutcomeKind.ROLLED_BACK
    assert tuple(owned.connection.iterdump()) == before
    current = _changed(cfg, change)
    factory = session_obtain_assembly(
        devices=current.devices, drivers=_registry(driver), staging=Path(cfg.paths.staging),
        ready=Path(cfg.paths.ready), processing=Path(cfg.paths.processing), segment_size=4,
        occurred_at=lambda: _NOW, monotonic_ns=lambda: 0)

    await obtain_flow(factory)(context)

    assert tuple(owned.connection.iterdump()) == before
    assert tuple(driver.reads) == calls
    assert owned.connection.execute(
        "SELECT committed_bytes,slot_device_id FROM file_copies WHERE id=?", (command.copy_id,)).fetchone() == (4, "cam-a")
    assert owned.connection.execute(
        "SELECT source_dependency FROM obtain_items WHERE delivery_id="
        " (SELECT delivery_id FROM file_copies WHERE id=?)", (command.copy_id,)).fetchone() == (1,)
    assert classify_work(query_work_facts(owned.connection)).needs_driver


@pytest.mark.parametrize("change", ["missing", "mismatch"])
async def test_original_read_recovers_unknown_then_finishes_local_binding_failure(pending_read, environment, change):
    from camctl.outputs.obtain_flow import advance_obtain

    _cfg, owned, command = pending_read
    ticket = _start_unended_read(owned, command.copy_id)
    horizon = owned.connection.execute("SELECT MAX(id) FROM history_events").fetchone()[0]
    runtime, diagnostics = _recovering_runtime(environment, command, change,
        boundary=RecoveryBoundary.HOST_LOCAL_SETTLED, horizon=horizon)
    attempts_before = owned.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone()[0]
    calls = tuple(environment[3].reads)

    await advance_obtain(runtime)

    row = owned.connection.execute(
        "SELECT status,effect_state,result_json,error_json FROM operation_attempts"
        " WHERE run_id=? AND attempt_no=?", (ticket.run_id, ticket.attempt_id)).fetchone()
    assert row[:2] == (int(enum_for("operation_attempts.status").UNKNOWN),
                      int(enum_for("operation_attempts.effect_state").UNKNOWN))
    assert json.loads(row[2]) == {
        "format_version": 1, "observations": [], "settlement": {
            "basis": "assumed", "evidence": {
                "type": "adb_foreground_recovery", "version": 1, "data": {}}}}
    assert json.loads(row[3]) == {"code": "result_not_saved", "stage": "recovery", "details": {}}
    assert owned.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone()[0] == attempts_before
    assert tuple(environment[3].reads) == calls
    assert owned.connection.execute(
        "SELECT status,json_extract(error_json,'$.code') FROM deliveries WHERE id="
        " (SELECT delivery_id FROM file_copies WHERE id=?)", (command.copy_id,)).fetchone() == (6, "device_binding_unavailable")
    assert owned.connection.execute(
        "SELECT committed_bytes,slot_device_id FROM file_copies WHERE id=?", (command.copy_id,)).fetchone() == (4, None)
    assert runtime.last_recovery_diagnostic is None
    assert diagnostics == []


@pytest.mark.parametrize("change", ["missing", "mismatch"])
@pytest.mark.parametrize("missing,reason", [
    ("boundary", RecoveryBlockedReason.UNCONFIRMED_BOUNDARY),
    ("horizon", RecoveryBlockedReason.MISSING_HORIZON),
    ("lookup", RecoveryBlockedReason.MISSING_EVIDENCE_LOOKUP),
    ("outside_horizon", RecoveryBlockedReason.INTENT_OUTSIDE_HORIZON),
    ("declaration", RecoveryBlockedReason.EVIDENCE_UNAVAILABLE),
    ("evidence", RecoveryBlockedReason.EVIDENCE_UNAVAILABLE),
])
async def test_unended_read_without_one_recovery_input_keeps_facts_and_diagnostic(
        pending_read, environment, change, missing, reason):
    from camctl.outputs.obtain_flow import advance_obtain

    _cfg, owned, command = pending_read
    ticket = _start_unended_read(owned, command.copy_id)
    intent_id = owned.connection.execute(
        "SELECT intent_event_id FROM operation_attempts WHERE run_id=? AND attempt_no=?",
        (ticket.run_id, ticket.attempt_id)).fetchone()[0]
    runtime, diagnostics = _recovering_runtime(environment, command, change,
        boundary=(RecoveryBoundary.UNCONFIRMED if missing == "boundary" else RecoveryBoundary.HOST_LOCAL_SETTLED),
        horizon=(None if missing == "horizon" else intent_id - 1 if missing == "outside_horizon" else intent_id),
        declared=missing != "declaration", evidence=missing != "evidence", lookup=missing != "lookup")
    before, calls = tuple(owned.connection.iterdump()), tuple(environment[3].reads)

    await advance_obtain(runtime)

    assert tuple(owned.connection.iterdump()) == before
    assert tuple(environment[3].reads) == calls
    diagnostic = runtime.last_recovery_diagnostic
    assert diagnostic is not None
    assert (diagnostic.reason, diagnostic.run_id, diagnostic.attempt_id) == (reason, ticket.run_id, ticket.attempt_id)
    assert diagnostics == [diagnostic]
    assert classify_work(query_work_facts(owned.connection)).needs_driver


@pytest.mark.parametrize("after_commit", [False, True])
async def test_unknown_read_result_commit_retries_original_key_before_binding_failure(
        pending_read, environment, monkeypatch, after_commit):
    from camctl.contracts.values import ConsistencyError
    from camctl.outputs.obtain_flow import advance_obtain
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
    from ..cancellation.test_report_sync_lifecycle import _fault_owned

    cfg, owned, command = pending_read
    _start_unended_read(owned, command.copy_id)
    horizon = owned.connection.execute("SELECT MAX(id) FROM history_events").fetchone()[0]
    runtime, _diagnostics = _recovering_runtime(environment, command, "missing",
        boundary=RecoveryBoundary.HOST_LOCAL_SETTLED, horizon=horizon)
    original = OperationRepository.finish_attempt
    saves = []

    def lose_commit_once(repository, finish, key, connection):
        saves.append((finish, key))
        if len(saves) == 1:
            return original(repository, finish, key, _fault_owned(connection, "COMMIT", after_commit=after_commit))
        return original(repository, finish, key, connection)

    monkeypatch.setattr(OperationRepository, "finish_attempt", lose_commit_once)
    with pytest.raises(ConsistencyError):
        await advance_obtain(runtime)
    owned.connection.close()
    reopened = open_existing(Path(cfg.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig())
    runtime.owned = reopened
    try:
        await advance_obtain(runtime)
        assert len(saves) == 2
        assert saves[1] == saves[0]
        assert reopened.connection.execute(
            "SELECT status FROM deliveries WHERE id=(SELECT delivery_id FROM file_copies WHERE id=?)",
            (command.copy_id,)).fetchone() == (6,)
        assert reopened.connection.execute(
            "SELECT source_dependency FROM obtain_items WHERE delivery_id=(SELECT delivery_id FROM file_copies WHERE id=?)",
            (command.copy_id,)).fetchone() == (0,)
    finally:
        reopened.connection.close()

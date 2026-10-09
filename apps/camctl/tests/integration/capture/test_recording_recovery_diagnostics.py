"""无可靠恢复依据时保留原调用责任并提供可检查的有限诊断。"""

import pytest

from camctl.capture.recovery import RecoveryBoundary
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry

from .test_recording_start_runtime import StartDriver, _configured, _result, _world


def test_blocked_recovery_emits_original_identity_without_repeating_same_diagnostic(tmp_path):
    owned = _world(tmp_path)
    try:
        runtime = _configured(owned, StartDriver(_result(confirmed=True)))
        ticket, rejected = runtime.grant(runtime.action(12))
        assert ticket is not None, rejected
        records = []
        runtime.on_recovery_diagnostic = records.append
        before = tuple(owned.connection.iterdump())

        assert runtime.recover_attempt(ticket) is False
        assert runtime.recover_attempt(ticket) is False

        assert len(records) == 1
        assert records[0] == runtime.last_recovery_diagnostic
        assert (records[0].run_id, records[0].attempt_id) == (ticket.run_id, ticket.attempt_id)
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


def test_changed_recovery_block_emits_the_new_reason_without_rewriting_attempt(tmp_path):
    owned = _world(tmp_path)
    try:
        runtime = _configured(owned, StartDriver(_result(confirmed=True)))
        ticket, rejected = runtime.grant(runtime.action(12))
        assert ticket is not None, rejected
        records = []
        runtime.on_recovery_diagnostic = records.append
        before = tuple(owned.connection.iterdump())
        assert runtime.recover_attempt(ticket) is False
        runtime.recovery_boundary = RecoveryBoundary.HOST_LOCAL_SETTLED
        runtime.recovery_max_event_id = None

        assert runtime.recover_attempt(ticket) is False

        assert [record.reason.name for record in records] == [
            "UNCONFIRMED_BOUNDARY", "MISSING_HORIZON"]
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


@pytest.mark.parametrize("blocked,reason", [
    ("boundary", "UNCONFIRMED_BOUNDARY"),
    ("horizon", "MISSING_HORIZON"),
    ("lookup", "MISSING_EVIDENCE_LOOKUP"),
    ("evidence", "EVIDENCE_UNAVAILABLE"),
    ("new_intent", "INTENT_OUTSIDE_HORIZON"),
])
def test_recovery_keeps_responsibility_and_exact_blocked_reason(tmp_path, blocked, reason):
    owned = _world(tmp_path)
    try:
        driver = StartDriver(_result(confirmed=True))
        runtime = _configured(owned, driver)
        initial_horizon = owned.connection.execute(
            "SELECT COALESCE(MAX(id),0) FROM history_events").fetchone()[0]
        ticket, rejected = runtime.grant(runtime.action(12))
        assert ticket is not None, rejected
        horizon = owned.connection.execute("SELECT MAX(id) FROM history_events").fetchone()[0]
        recovery = EvidenceRegistry((EvidenceContract(
            "adb_foreground_recovery", 1, "control", frozenset()),))
        runtime.recovery_boundary = RecoveryBoundary.HOST_LOCAL_SETTLED
        runtime.recovery_max_event_id = horizon
        runtime.recovery_evidence_for = lambda binding, operation: recovery
        if blocked == "boundary":
            runtime.recovery_boundary = RecoveryBoundary.UNCONFIRMED
        elif blocked == "horizon":
            runtime.recovery_max_event_id = None
        elif blocked == "lookup":
            runtime.recovery_evidence_for = None
        elif blocked == "evidence":
            runtime.recovery_evidence_for = lambda binding, operation: None
        else:
            runtime.recovery_max_event_id = initial_horizon
        assert runtime.recover_attempt(ticket) is False
        diagnostic = runtime.last_recovery_diagnostic
        assert diagnostic.reason.name == reason
        assert (diagnostic.run_id, diagnostic.attempt_id) == (ticket.run_id, ticket.attempt_id)
        assert owned.connection.execute(
            "SELECT status, result_event_id FROM operation_attempts").fetchone() == (1, None)
        assert driver.calls == []
        runtime.recovery_boundary = RecoveryBoundary.HOST_LOCAL_SETTLED
        runtime.recovery_max_event_id = horizon
        runtime.recovery_evidence_for = lambda binding, operation: recovery
        assert runtime.recover_attempt(ticket) is True
        assert runtime.last_recovery_diagnostic is None
    finally:
        owned.connection.close()

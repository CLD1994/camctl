"""无可靠恢复依据时保留原调用责任并提供可检查的有限诊断。"""

import pytest
from types import SimpleNamespace

from camctl.capture.recovery import RecoveryBoundary
from camctl.capture.residual import residual_flow
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry

from .test_recording_start_runtime import StartDriver, configured, _result, _world, _NOW
from .test_recording_start_save_boundary import _grant

pytestmark = pytest.mark.asyncio


async def advance(runtime):
    async def factory(database, device_id):
        return runtime
    await residual_flow(factory)(SimpleNamespace(
        database=runtime.database, clock=SimpleNamespace(utc_micros=lambda: _NOW)))


async def test_blocked_recovery_emits_original_identity_without_repeating_same_diagnostic(tmp_path, configured):
    owned = _world(tmp_path)
    try:
        runtime = await configured(owned, StartDriver(_result(confirmed=True)))
        ticket = _grant(owned)
        records = []
        runtime.on_recovery_diagnostic = records.append
        before = tuple(owned.connection.iterdump())

        await advance(runtime)
        await advance(runtime)

        assert len(records) == 1
        assert records[0] == runtime.last_recovery_diagnostic
        assert (records[0].run_id, records[0].attempt_id) == (ticket.run_id, ticket.attempt_id)
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


async def test_changed_recovery_block_emits_the_new_reason_without_rewriting_attempt(tmp_path, configured):
    owned = _world(tmp_path)
    try:
        runtime = await configured(owned, StartDriver(_result(confirmed=True)))
        _grant(owned)
        records = []
        runtime.on_recovery_diagnostic = records.append
        before = tuple(owned.connection.iterdump())
        await advance(runtime)
        runtime.recovery_boundary = RecoveryBoundary.HOST_LOCAL_SETTLED
        runtime.recovery_max_event_id = None

        await advance(runtime)

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
async def test_recovery_keeps_responsibility_and_exact_blocked_reason(tmp_path, blocked, reason, configured):
    owned = _world(tmp_path)
    try:
        driver = StartDriver(_result(confirmed=True))
        runtime = await configured(owned, driver)
        initial_horizon = owned.connection.execute(
            "SELECT COALESCE(MAX(id),0) FROM history_events").fetchone()[0]
        ticket = _grant(owned)
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
        await advance(runtime)
        diagnostic = runtime.last_recovery_diagnostic
        assert diagnostic.reason.name == reason
        assert (diagnostic.run_id, diagnostic.attempt_id) == (ticket.run_id, ticket.attempt_id)
        assert owned.connection.execute(
            "SELECT status, result_event_id FROM operation_attempts").fetchone() == (1, None)
        assert driver.calls == []
        runtime.recovery_boundary = RecoveryBoundary.HOST_LOCAL_SETTLED
        runtime.recovery_max_event_id = horizon
        runtime.recovery_evidence_for = lambda binding, operation: recovery
        await advance(runtime)
        assert runtime.last_recovery_diagnostic is None
        assert owned.connection.execute(
            "SELECT status FROM operation_attempts").fetchone() == (4,)
    finally:
        owned.connection.close()

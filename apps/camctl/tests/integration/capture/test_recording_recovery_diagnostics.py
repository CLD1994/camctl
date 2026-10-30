"""公开C1恢复保留原责任，并通过正式回调交付有限诊断。"""

import pytest

from camctl.capture.handlers import capture_handler
from camctl.capture.recovery import RecoveryBoundary

from ..support.capture_diagnostic_cases import diagnostic_case, unfinished_start, recovery_runtime

pytestmark = pytest.mark.asyncio


async def test_blocked_recovery_emits_original_identity_without_repeating_same_diagnostic(tmp_path, diagnostic_case, monkeypatch):
    case = diagnostic_case
    ticket = await unfinished_start(case, tmp_path, monkeypatch)
    records = []
    runtime = await recovery_runtime(case, records, boundary=RecoveryBoundary.UNCONFIRMED, horizon=case.intent_horizon)
    before = tuple(case.owned.connection.iterdump())
    for _ in range(2):
        await capture_handler('camera_record')(case.action_id, runtime)
    diagnostic, = records
    assert diagnostic.reason.name == 'UNCONFIRMED_BOUNDARY'
    assert (diagnostic.run_id, diagnostic.attempt_id) == (ticket.run_id, ticket.attempt_id)
    assert tuple(case.owned.connection.iterdump()) == before
    assert case.control.calls == []


async def test_changed_recovery_block_emits_the_new_reason_without_rewriting_attempt(tmp_path, diagnostic_case, monkeypatch):
    case = diagnostic_case
    await unfinished_start(case, tmp_path, monkeypatch)
    records = []
    before = tuple(case.owned.connection.iterdump())
    runtime = await recovery_runtime(case, records, boundary=RecoveryBoundary.UNCONFIRMED, horizon=case.intent_horizon)
    await capture_handler('camera_record')(case.action_id, runtime)
    runtime = await recovery_runtime(case, records, boundary=RecoveryBoundary.HOST_LOCAL_SETTLED, horizon=None)
    await capture_handler('camera_record')(case.action_id, runtime)
    assert [record.reason.name for record in records] == ['UNCONFIRMED_BOUNDARY', 'MISSING_HORIZON']
    assert tuple(case.owned.connection.iterdump()) == before
    assert case.control.calls == []


@pytest.mark.parametrize('blocked,reason', [
    ('boundary', 'UNCONFIRMED_BOUNDARY'),
    ('horizon', 'MISSING_HORIZON'),
    ('evidence', 'EVIDENCE_UNAVAILABLE'),
    ('new_intent', 'INTENT_OUTSIDE_HORIZON'),
])
async def test_recovery_keeps_responsibility_and_exact_blocked_reason(tmp_path, diagnostic_case, monkeypatch, blocked, reason):
    case = diagnostic_case
    ticket = await unfinished_start(case, tmp_path, monkeypatch)
    records = []
    runtime = await recovery_runtime(case, records,
        boundary=RecoveryBoundary.UNCONFIRMED if blocked == 'boundary' else RecoveryBoundary.HOST_LOCAL_SETTLED,
        horizon=None if blocked == 'horizon' else case.before_intent_horizon if blocked == 'new_intent' else case.intent_horizon,
        evidence=blocked != 'evidence')
    before = tuple(case.owned.connection.iterdump())
    await capture_handler('camera_record')(case.action_id, runtime)
    diagnostic, = records
    assert diagnostic.reason.name == reason
    assert (diagnostic.run_id, diagnostic.attempt_id) == (ticket.run_id, ticket.attempt_id)
    assert tuple(case.owned.connection.iterdump()) == before
    assert case.owned.connection.execute('SELECT status,result_event_id FROM operation_attempts').fetchone() == (1, None)
    assert case.control.calls == []
    runtime = await recovery_runtime(case, records, boundary=RecoveryBoundary.HOST_LOCAL_SETTLED, horizon=case.intent_horizon)
    await capture_handler('camera_record')(case.action_id, runtime)
    assert case.owned.connection.execute('SELECT status,result_event_id IS NOT NULL FROM operation_attempts').fetchone() == (4, 1)
    assert len(records) == 1 and case.control.calls == []

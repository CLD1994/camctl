"""START 原结果保存责任、复合事务和已结束动作的旧调用恢复。"""

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
import sqlite3

import pytest

from camctl.capture.handlers import capture_handler
from camctl.capture.models import ActivityObservationSave
from camctl.capture.recovery import RecoveryBoundary
from camctl.capture.residual import residual_flow
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.evidence import EvidenceContract
from camctl.operations.attempts import (
    AttemptConfig, AttemptFinish, AttemptIntent, AttemptTarget, OperationKind,
    QueryPurpose, RunFinish, RunOutcome, StaleRunFinish,
)
from camctl.operations.models import ErrorValue
from camctl.operations.validation import validate_outcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository, FinishCanceledCapture
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.scheduling import SchedulingRepository

from .test_capture_contract import _NOW
from .test_natural_wait_time_eligibility import _request_cancel
from .test_recording_start_runtime import _ANCHOR, _START_EVIDENCE, StartDriver, StateQuery, _result, _start
from .recording_start_fixtures import start_case, StartSaveFault, observe_start_save
from ..support.result_cases import result_case

pytestmark = pytest.mark.asyncio


class ResultWriteFault:
    """真实 SQLite 写边界一次故障；保留实际提交前后分区。"""

    def __init__(self, real, driver, mode):
        self.real, self.driver, self.mode = real, driver, mode
        self.armed = True
        self.result_keys = []
        self.rolled_back_images = []

    def __getattr__(self, name):
        return getattr(self.real, name)

    def execute(self, statement, params=()):
        result_phase = bool(self.driver.calls)
        if result_phase and statement.startswith("INSERT INTO history_transactions"):
            self.result_keys.extend(value for value in params
                                    if isinstance(value, str) and len(value) == 32)
        fail = result_phase and self.armed and (
            (self.mode in ("commit_before", "commit_after") and statement == "COMMIT")
            or (self.mode == "projection" and statement.startswith("UPDATE device_activities SET")))
        if fail:
            self.armed = False
            if self.mode == "commit_after":
                self.real.execute(statement, params)
            raise sqlite3.OperationalError(f"确定注入的 {self.mode} 故障")
        result = self.real.execute(statement, params)
        if result_phase and statement == "ROLLBACK":
            self.rolled_back_images.append(tuple(self.real.iterdump()))
        return result


@pytest.mark.parametrize("kind", [DbOutcomeKind.ROLLED_BACK, DbOutcomeKind.UNKNOWN])
async def test_start_grant_state_failure_stops_before_device_call(tmp_path, start_case, monkeypatch, kind):
    owned = await start_case.world(tmp_path)
    driver = StartDriver(_result(confirmed=True, activity_id='1'))
    faults = []
    original = SchedulingRepository.grant_start
    def unreliable(repository, request, key, current):
        fault = StartSaveFault(current.connection,
            'projection' if kind is DbOutcomeKind.ROLLED_BACK else 'commit_before',
            target_table='operation_attempts', target_insert=True)
        faults.append(fault)
        receipt = original(repository, request, key, replace(current, connection=fault))
        assert receipt.kind is kind and fault.hits == 1, receipt.error
        return receipt
    monkeypatch.setattr(SchedulingRepository, 'grant_start', unreliable)
    try:
        runtime = await start_case.runtime(owned, driver)
        with pytest.raises(ConsistencyError):
            await capture_handler('camera_record')(start_case.aid, runtime)
        assert driver.calls == []
        assert owned.connection.execute('SELECT COUNT(*) FROM operation_attempts').fetchone() == (0,)
    finally:
        owned.connection.close()


async def test_unresolved_result_save_keeps_original_outcome_and_identity_for_same_session(
        tmp_path, start_case, monkeypatch):
    owned = await start_case.world(tmp_path)
    actual = _result(confirmed=True, activity_id='1')
    driver = StartDriver(actual)
    observed = observe_start_save(monkeypatch, 'persistent')
    try:
        runtime = await start_case.runtime(owned, driver)
        with pytest.raises(ConsistencyError):
            await capture_handler('camera_record')(start_case.aid, runtime)
        assert start_case.stopper.calls == []
        assert len(observed.requests) == 2 and observed.requests[0] == observed.requests[1]
        finish, observation, key, options = observed.requests[0]
        assert finish.outcome.outcome is actual
        assert finish.ticket.responsibility_key == 'start/1'
        assert observation.started_at == _NOW
        assert len(driver.calls) == 1
        observed.fault.enabled = False
        start_case.wall[0] = _NOW + 5_000_000
        start_case.mono[0] = _ANCHOR + 1_000_000_000
        await capture_handler('camera_record')(start_case.aid, runtime)
        assert len(observed.requests) == 3
        assert all(value == (finish, observation, key, options) for value in observed.requests)
        assert len(driver.calls) == 1
        assert owned.connection.execute(
            'SELECT started_at FROM device_activities WHERE action_id=1').fetchone() == (_NOW,)
        await start_case.assert_stop_deadline(runtime, _ANCHOR + 60_000_000_000)
        assert start_case.stopper.times == [_ANCHOR + 60_000_000_000]
    finally:
        owned.connection.close()


@pytest.mark.parametrize("mode", ["projection", "commit_before", "commit_after"])
async def test_actual_start_result_is_verified_and_resaved_with_same_key_without_resend(
        tmp_path, start_case, monkeypatch, mode):
    owned = await start_case.world(tmp_path)
    try:
        before_result = []
        driver = StartDriver(_result(confirmed=True, activity_id='1'), returned=lambda:
            before_result.append(tuple(owned.connection.iterdump())))
        observed = observe_start_save(monkeypatch, mode)
        runtime = await start_case.runtime(owned, driver)
        await capture_handler('camera_record')(start_case.aid, runtime)
        assert len(driver.calls) == 1
        assert _start(owned, start_case.aid)[1:] == (3, 1, 0)
        assert owned.connection.execute(
            'SELECT status, effect_state FROM operation_attempts').fetchone() == (2, 3)
        assert owned.connection.execute(
            'SELECT started_at FROM device_activities WHERE action_id=1').fetchone() == (_NOW,)
        assert len(set(observed.fault.result_keys)) == 1 and observed.fault.hits == 1
        assert owned.connection.execute('SELECT COUNT(*) FROM operation_attempts').fetchone() == (1,)
        if mode == 'projection':
            assert observed.fault.rolled_back_images == before_result
        elif mode == 'commit_before':
            assert observed.fault.reopened_images == before_result
    finally:
        owned.connection.close()


async def test_dispatch_prevented_result_and_expiration_share_transaction(tmp_path, start_case, monkeypatch):
    owned = await start_case.world(tmp_path)
    try:
        wall = [_NOW]
        driver = StartDriver(_result(confirmed=True, activity_id='1'))
        runtime = await start_case.runtime(owned, driver, wall=wall)
        original = SchedulingRepository.grant_start
        def after_grant(repository, *args, **options):
            receipt = original(repository, *args, **options)
            wall[0] += 60_000_001
            return receipt
        monkeypatch.setattr(SchedulingRepository, 'grant_start', after_grant)
        await capture_handler('camera_record')(start_case.aid, runtime)
        attempt_txn = owned.connection.execute(
            'SELECT e.transaction_id FROM operation_attempts a'
            ' JOIN history_events e ON e.id=a.result_event_id').fetchone()[0]
        expired_txn = owned.connection.execute(
            "SELECT transaction_id FROM history_events WHERE event_type=8 AND"
            " json_extract(body_json,'$.reason')=3").fetchone()[0]
        assert attempt_txn == expired_txn
        assert driver.calls == []
    finally:
        owned.connection.close()


@pytest.mark.parametrize("changed", ["result_time", "observation_time", "observation", "run_result"])
async def test_joint_start_same_key_rejects_changed_original_input(tmp_path, start_case, monkeypatch, changed):
    owned = await start_case.world(tmp_path)
    try:
        observed = observe_start_save(monkeypatch, 'observe')
        runtime = await start_case.runtime(owned, StartDriver(_result(confirmed=True, activity_id='1')))
        await capture_handler('camera_record')(start_case.aid, runtime)
        finish, observation, key, options = observed.requests[0]
        first = await runtime.database.write(lambda current: CaptureRepository().finish_start_result(
            finish, observation, key, current, **options), key=key)
        assert first.kind is DbOutcomeKind.COMPLETED, first.error
        before = tuple(owned.connection.iterdump())
        if changed == 'result_time':
            finish = replace(finish, occurred_at=_NOW + 1)
        elif changed == 'observation_time':
            observation = replace(observation, occurred_at=_NOW + 1)
        elif changed == 'observation':
            observation = replace(observation, started_at=_NOW + 1)
        else:
            finish = replace(finish, run_finish=RunFinish(
                RunOutcome.FAILED, ErrorValue('device_start_failed', 'execution')))
        again = await runtime.database.write(lambda current: CaptureRepository().finish_start_result(
            finish, observation, key, current, **options), key=key)
        assert again.kind is DbOutcomeKind.ROLLED_BACK
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


async def test_joint_query_same_key_rejects_changed_original_start_segment(tmp_path, start_case):
    """独立仓储目标：已登记QUERY结果与START片段同键核对。"""
    owned = await start_case.world(tmp_path)
    try:
        runtime = await start_case.runtime(owned, StartDriver(_result(activity_id='1')))
        await capture_handler('camera_record')(start_case.aid, runtime)
        activity_id, = owned.connection.execute('SELECT id FROM device_activities').fetchone()
        intent = AttemptIntent('query', start_case.aid, OperationKind.QUERY_ACTIVITY,
            AttemptTarget(activity_id=activity_id), QueryPurpose.START_CONFIRMATION,
            AttemptConfig(2, Decimal('0.5'), Decimal('1')), _NOW)
        key = new_operation_key()
        receipt = await runtime.database.write(lambda current:
            OperationRepository().begin_attempt(intent, key, current), key=key)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        response = await StateQuery(active=True, activity_id=str(activity_id)).query_state(None)
        finish = AttemptFinish(receipt.value.ticket,
            validate_outcome(receipt.value.ticket, response.outcome, _START_EVIDENCE),
            _NOW, run_finish=RunFinish(RunOutcome.SUCCEEDED))
        observation = ActivityObservationSave(start_case.aid, _NOW, started_at=_NOW,
            activity_state=2)
        original = StaleRunFinish(('start/1',), RunOutcome.SUCCEEDED, _NOW)
        key = new_operation_key()
        saved = await runtime.database.write(lambda current: CaptureRepository().finish_start_result(
            finish, observation, key, current, start_finish=original), key=key)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        before = tuple(owned.connection.iterdump())
        changed = replace(original, status=RunOutcome.UNCONFIRMED,
            error=ErrorValue('result_unconfirmed', 'device'))
        again = await runtime.database.write(lambda current: CaptureRepository().finish_start_result(
            finish, observation, key, current, start_finish=changed), key=key)
        assert again.kind is DbOutcomeKind.ROLLED_BACK
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


@pytest.mark.parametrize("boundary,old_h", [
    (RecoveryBoundary.HOST_LOCAL_SETTLED, True),
    (RecoveryBoundary.UNCONFIRMED, True),
    (RecoveryBoundary.HOST_LOCAL_SETTLED, False),
])
async def test_residual_consumer_finds_unfinished_old_start_under_terminal_action(
        tmp_path, monkeypatch, result_case, boundary, old_h):
    owned, runtime, aid, handler = await result_case.world(tmp_path, 'record', start=False,
        assembly_options={'control_recovery': True},
        evidence_contracts=(EvidenceContract('adb_foreground_recovery', 1, 'control', frozenset()),))
    driver = result_case.driver
    class IntentSaved(Exception):
        pass
    original = SchedulingRepository.grant_start
    def after_grant(repository, *args, **options):
        receipt = original(repository, *args, **options)
        assert receipt.kind is DbOutcomeKind.COMPLETED and receipt.value.ticket is not None
        return replace(receipt, handoff_error=IntentSaved())
    try:
        with monkeypatch.context() as patch:
            patch.setattr(SchedulingRepository, 'grant_start', after_grant)
            with pytest.raises(IntentSaved):
                await capture_handler(handler)(aid, runtime)
        horizon, = owned.connection.execute('SELECT MAX(id) FROM history_events').fetchone()
        await _request_cancel(runtime, aid, [_NOW])
        key = new_operation_key()
        ended = await runtime.database.write(lambda current: CaptureRepository().finish_canceled_capture(
            FinishCanceledCapture(aid, _NOW), key, current), key=key)
        assert ended.kind is DbOutcomeKind.COMPLETED, ended.error
        runtime = await result_case.reopen(owned, cold=True, assembly_options={
            'recovery_boundary': boundary, 'recovery_max_event_id': lambda: horizon if old_h else 1})
        await residual_flow(result_case.factory)(SimpleNamespace(database=runtime.database,
            clock=SimpleNamespace(utc_micros=lambda: _NOW)))
        assert driver.control.await_count == 0
        assert _start(owned, aid)[1:] == (5, 1, 0)
        assert owned.connection.execute('SELECT status FROM actions WHERE id=?', (aid,)).fetchone() == (6,)
        attempt = owned.connection.execute(
            'SELECT status,effect_state,result_json FROM operation_attempts').fetchone()
        if boundary is RecoveryBoundary.HOST_LOCAL_SETTLED and old_h:
            assert attempt[:2] == (4, 1)
            import json
            result = json.loads(attempt[2])
            assert result['settlement'] == {'basis': 'assumed', 'evidence': {
                'type': 'adb_foreground_recovery', 'version': 1, 'data': {}}}
            assert result['observations'] == [] and 'call_info' not in result
        else:
            assert attempt == (1, 1, None)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('late_error', [False, True])
async def test_canceled_query_storage_keeps_late_actual_observation_without_reviving_runs(
        tmp_path, start_case, late_error):
    """独立仓储合同：取消终态先保存，原QUERY实际返回后仍交付原事实。"""
    import asyncio
    from camctl.devices.bindings import DeviceBinding
    from camctl.devices.evidence import DeviceObservation
    from camctl.devices.ports import ControlRequest, DeviceCallResult
    from camctl.operations.models import AttemptStatus, EvidenceValue, Settlement, SettlementBasis

    owned = await start_case.world(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    task = None
    actual = replace(_result(confirmed=True, activity_id='1'),
        status=AttemptStatus.FAILED if late_error else AttemptStatus.SUCCEEDED,
        error=ErrorValue('transport_timeout', 'transport') if late_error else None,
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue('query_returned', 1, {})),
        observations=(DeviceObservation('activity_status', 1, {'activity_id': '1'}),))
    requests = []
    async def query(request):
        requests.append(request)
        entered.set()
        await release.wait()
        return DeviceCallResult.from_outcome(actual)
    try:
        driver = StartDriver(_result(activity_id='1'))
        runtime = await start_case.runtime(owned, driver)
        await capture_handler('camera_record')(start_case.aid, runtime)
        intent = AttemptIntent('query', start_case.aid, OperationKind.QUERY_ACTIVITY,
            AttemptTarget(activity_id=1), QueryPurpose.START_CONFIRMATION,
            AttemptConfig(2, Decimal('0.5'), Decimal('1')), _NOW)
        intent_key = new_operation_key()
        begun = await runtime.database.write(lambda current:
            OperationRepository().begin_attempt(intent, intent_key, current), key=intent_key)
        assert begun.kind is DbOutcomeKind.COMPLETED and begun.value.ticket is not None, begun.error
        ticket = begun.value.ticket
        task = asyncio.create_task(query(ControlRequest('query', DeviceBinding('cam-1', 'camctl-adb'),
            {'activity_id': '1'}, ticket=ticket, timeout_s=Decimal('0.5'))))
        await entered.wait()
        await _request_cancel(runtime, start_case.aid, start_case.wall)
        terminal_key = new_operation_key()
        ended = await runtime.database.write(lambda current: CaptureRepository().finish_canceled_capture(
            FinishCanceledCapture(start_case.aid, _NOW), terminal_key, current), key=terminal_key)
        assert ended.kind is DbOutcomeKind.COMPLETED, ended.error
        assert start_case.stopper.calls == []
        assert owned.connection.execute(
            'SELECT status FROM operation_attempts WHERE run_id=?', (ticket.run_id,)).fetchone() == (1,)
        release.set()
        response = await task
        finish = AttemptFinish(ticket, validate_outcome(ticket, response.outcome, _START_EVIDENCE),
            _NOW, run_finish=RunFinish(RunOutcome.CANCELED))
        observation = ActivityObservationSave(start_case.aid, _NOW, started_at=_NOW, activity_state=2)
        key = new_operation_key()
        saved = await runtime.database.write(lambda current: CaptureRepository().finish_start_result(
            finish, observation, key, current), key=key)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        assert owned.connection.execute(
            'SELECT started_at,activity_state FROM device_activities WHERE action_id=1').fetchone() == (_NOW, 2)
        assert owned.connection.execute('SELECT status FROM actions WHERE id=1').fetchone() == (6,)
        assert owned.connection.execute('SELECT status FROM operation_runs ORDER BY id').fetchall() == [(5,), (5,)]
        assert owned.connection.execute(
            'SELECT status,effect_state FROM operation_attempts WHERE run_id=?', (ticket.run_id,)).fetchone() == (3 if late_error else 2, 3)
        assert len(driver.calls) == len(requests) == 1
        import json
        raw, error_json = owned.connection.execute(
            'SELECT result_json,error_json FROM operation_attempts WHERE run_id=?', (ticket.run_id,)).fetchone()
        assert json.loads(raw) == {'format_version': 1,
            'settlement': {'basis': 'observed',
                'evidence': {'type': 'query_returned', 'version': 1, 'data': {}}},
            'observations': [{'type': 'activity_status', 'version': 1, 'data': {'activity_id': '1'}}],
            'call_info': {'local_exit': {'exit_code': 0}}}
        assert (None if error_json is None else json.loads(error_json)) == (
            {'code': 'transport_timeout', 'stage': 'transport', 'details': {}} if late_error else None)
        before = tuple(owned.connection.iterdump())
        repeated = await runtime.database.write(lambda current: CaptureRepository().finish_start_result(
            finish, observation, key, current), key=key)
        assert repeated.kind is DbOutcomeKind.COMPLETED and tuple(owned.connection.iterdump()) == before
    finally:
        release.set()
        if task is not None and not task.done():
            await task
        owned.connection.close()

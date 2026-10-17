"""有限设备任务取消后的原责任与自然期限。"""
from dataclasses import replace
from decimal import Decimal
import json
from unittest.mock import create_autospec

import pytest

from camctl.capture.handlers import capture_handler
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.devices.ports import DeviceCallResult, StopDriver
from camctl.operations.attempts import AttemptConfig
from camctl.operations.models import AttemptStatus, CallOutcome, EffectState, ErrorValue, EvidenceValue, Settlement, SettlementBasis

from .test_capture_contract import _TIMELAPSE, _NOW, _environment, _runtime, _entry
from .test_cancel_timelapse_exhaustion_files import _apply_public_cancel

pytestmark = pytest.mark.asyncio


async def _world(tmp_path, *, unknown=False):
    owned = _environment(tmp_path, _TIMELAPSE)
    spec = json.loads(owned.connection.execute('SELECT execution_spec_json FROM actions WHERE id=13').fetchone()[0])
    spec.update(target_duration_ms=1000, result_wait_margin_ms=200,
                cancel_completion_policy='finite_device_end')
    owned.connection.execute('UPDATE actions SET execution_spec_json=? WHERE id=13', (json.dumps(spec),))
    wall, mono = [_NOW], [5_000_000_000]
    runtime = _runtime(owned, files={13: (_entry('finite-original'),)},
        stop_config=AttemptConfig(3, Decimal('1'), Decimal('0')))
    runtime.wall_us = lambda: wall[0]
    runtime.monotonic_ns = lambda: mono[0]
    runtime.wait_config = lambda action: CaptureWaitConfig(1000, 200, 50)
    advance = capture_handler('camera_timelapse')
    await advance(13, runtime)
    _apply_public_cancel(owned, 13, _NOW+100_000)
    stopper = create_autospec(StopDriver, instance=True)
    stopper.stop.return_value = DeviceCallResult.from_outcome(CallOutcome(
        status=AttemptStatus.UNKNOWN if unknown else AttemptStatus.FAILED,
        effect=EffectState.UNKNOWN,
        error=ErrorValue('device_error', 'device'),
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue('stop_returned', 1, {}))))
    runtime.stopper = stopper
    return owned, runtime, advance, stopper, wall, mono


@pytest.mark.parametrize('unknown', [False, True])
async def test_first_failed_stop_transfers_responsibility_without_retry_or_early_listing(tmp_path, unknown):
    owned, runtime, advance, stopper, wall, mono = await _world(tmp_path, unknown=unknown)
    try:
        await advance(13, runtime)
        assert owned.connection.execute("SELECT status,attempts_used,retry_wait_required FROM operation_runs WHERE responsibility_key='stop/13'").fetchone() == (4, 1, 0)
        assert owned.connection.execute('SELECT sent_at FROM device_activities WHERE id=13').fetchone() == (_NOW,)
        assert runtime.results.calls == []
        assert owned.connection.execute('SELECT activity_state,occupancy_state FROM device_activities WHERE id=13').fetchone() == (1, 1)
        await advance(13, runtime)
        stopper.stop.assert_awaited_once()
        assert runtime.driver.calls == ['start_timelapse']
        assert runtime.results.calls == []
        assert owned.connection.execute('SELECT status FROM actions WHERE id=13').fetchone() == (2,)
        stop_txn = owned.connection.execute("SELECT e.transaction_id FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id JOIN history_events e ON e.id=a.result_event_id WHERE r.responsibility_key='stop/13'").fetchone()[0]
        failed_txn = owned.connection.execute("SELECT e.transaction_id FROM operation_runs r JOIN history_events e ON e.event_type=10 AND json_extract(e.body_json,'$.rows[0].id')=r.id WHERE r.responsibility_key='stop/13' ORDER BY e.id DESC LIMIT 1").fetchone()[0]
        assert stop_txn == failed_txn
    finally:
        owned.connection.close()


async def test_restarted_finite_cancel_waits_only_remaining_original_deadline(tmp_path):
    owned, runtime, advance, stopper, wall, mono = await _world(tmp_path)
    try:
        await advance(13, runtime)
        restarted = replace(runtime, timelapse_deadlines={}, wall_us=lambda: _NOW+1_000_000,
            monotonic_ns=lambda: 9_000_000_000)
        await advance(13, restarted)
        assert restarted.timelapse_deadlines[13] == 9_250_000_000
        assert restarted.results.calls == []
        stopper.stop.assert_awaited_once()
    finally:
        owned.connection.close()


async def test_finite_cancel_ends_activity_without_claiming_capture_or_stop_success(tmp_path):
    owned, runtime, advance, stopper, wall, mono = await _world(tmp_path)
    try:
        await advance(13, runtime)
        wall[0] += 1_250_000
        mono[0] += 1_250_000_000
        await advance(13, runtime)
        assert owned.connection.execute('SELECT status FROM actions WHERE id=13').fetchone() == (6,)
        assert owned.connection.execute('SELECT activity_state,occupancy_state,completion_basis,capture_json FROM device_activities WHERE id=13').fetchone() == (3, 2, 1, None)
        assert owned.connection.execute("SELECT status,attempts_used FROM operation_runs WHERE responsibility_key='stop/13'").fetchone() == (4, 1)
        from camctl.cancellation.settlement import TargetSettlement
        outcome = TargetSettlement(owned, None, None, lambda _: '', lambda: wall[0])._settle_capture(13, 6)
        assert outcome.complete and outcome.failed
        assert owned.connection.execute('SELECT COUNT(*) FROM outputs WHERE source_action_id=13').fetchone() == (1,)
        ended = owned.connection.execute("SELECT body_json FROM history_events WHERE event_type=13 AND json_extract(body_json,'$.reason')=4").fetchone()
        assert ended is not None
        evidence = json.loads(ended[0])['evidence']['cancel_completion']
        assert evidence['policy'] == 'finite_device_end'
        assert evidence['sent_at_us'] == _NOW
        assert evidence['expected_check_at_us'] == _NOW+1_250_000
        assert evidence['start_result_event_id'] < evidence['stop_result_event_id'] < evidence['wait_completed_event_id'] < evidence['result_page_event_id']
        stopper.stop.assert_awaited_once()
    finally:
        owned.connection.close()

"""目录调用的原始输入先保存，再供恢复和后续页使用。"""
from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.capture.baseline_models import BaselineReadIntent
from camctl.contracts.history_values import INITIAL_BOUNDARY
from camctl.contracts.values import new_operation_key
from camctl.devices.bindings import DeviceBinding
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository, register_capture_guards
from camctl.persistence.repositories.operations import register_operation_guards
from camctl.history.validators import EventContext
from camctl.persistence.transaction import BoundedCommandPlan, commit_operation

from ..capture.test_baseline_history import _activity, _fix, _ref
from ..scheduling.test_start_action import _SCHEDULED, owned

register_capture_guards()
register_operation_guards()
pytestmark = pytest.mark.asyncio


async def _intent(owned, tmp_path):
    activity = await _activity(owned, tmp_path)
    action = owned.connection.execute(
        'SELECT action_id FROM device_activities WHERE id=?', (activity,)).fetchone()[0]
    return BaselineReadIntent(action, activity, DeviceBinding('cam-1', 'dji-action6'),
        ('/mnt/media_rw/emulated/DCIM', '/mnt/media_rw/sd/DCIM'), None,
        Decimal('1.0000000000000001'),
        _ref(owned, activity), 1, new_operation_key(), new_operation_key(), _SCHEDULED)


async def test_intent_freezes_original_request_without_changing_capture(owned, tmp_path):
    # 遗漏原始输入或意图保存时提前固定基准/派发相机，均应使此测试失败。
    request = await _intent(owned, tmp_path)
    capture = CaptureRepository()
    saved = capture.save_baseline_read_intent(request, request.call_key, owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    view = saved.value
    assert view.request == request
    assert view.state.name == 'INTENDED'
    assert (view.result_event_id, view.returned_at, view.error, view.next_cursor,
            view.chunk_event_id) == (None, None, None, None, None)
    assert owned.connection.execute(
        'SELECT dispatch_state,occupancy_state,baseline_state FROM device_activities'
        ' WHERE id=?', (request.activity_id,)).fetchone() == (1, 1, 2)
    assert owned.connection.execute('SELECT COUNT(*) FROM operation_attempts').fetchone() == (0,)
    assert capture.read_baseline_call(view.call_id, INITIAL_BOUNDARY, owned) is None
    assert capture.read_baseline_call(view.call_id, view.boundary, owned) == view


@pytest.mark.parametrize('field,value', [
    ('action_id', 999), ('activity_id', 999),
    ('binding', DeviceBinding('other', 'dji-action6')),
    ('directories', ('/different',)),
])
async def test_intent_requires_actual_activity_binding_and_scope(owned, tmp_path, field, value):
    request = await _intent(owned, tmp_path)
    if field == 'activity_id':
        request = replace(request, activity_id=value,
                          baseline_ref=replace(request.baseline_ref, activity_id=value))
    else:
        request = replace(request, **{field: value})
    saved = CaptureRepository().save_baseline_read_intent(request, request.call_key, owned)
    assert saved.kind is DbOutcomeKind.ROLLED_BACK
    assert owned.connection.execute('SELECT COUNT(*) FROM baseline_read_calls').fetchone() == (0,)


async def test_original_intent_key_reuses_original_input_after_baseline_fixed(owned, tmp_path):
    request = await _intent(owned, tmp_path)
    capture = CaptureRepository()
    first = capture.save_baseline_read_intent(request, request.call_key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    assert _fix(owned, request.activity_id).kind is DbOutcomeKind.COMPLETED
    replay = capture.save_baseline_read_intent(request, request.call_key, owned)
    assert replay.kind is DbOutcomeKind.COMPLETED, replay.error
    assert replay.value == first.value
    changed = replace(request, timeout_s=Decimal('2'))
    assert capture.save_baseline_read_intent(
        changed, changed.call_key, owned).kind is DbOutcomeKind.ROLLED_BACK
    assert owned.connection.execute('SELECT COUNT(*) FROM baseline_read_calls').fetchone() == (1,)


async def test_one_pending_call_keeps_its_original_double_keys(owned, tmp_path):
    request = await _intent(owned, tmp_path)
    capture = CaptureRepository()
    first = capture.save_baseline_read_intent(request, request.call_key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    second = replace(request, call_key=new_operation_key(), result_save_key=new_operation_key())
    assert capture.save_baseline_read_intent(
        second, second.call_key, owned).kind is DbOutcomeKind.ROLLED_BACK
    assert capture.save_baseline_read_intent(
        request, new_operation_key(), owned).kind is DbOutcomeKind.ROLLED_BACK
    assert owned.connection.execute('SELECT COUNT(*) FROM baseline_read_calls').fetchone() == (1,)


@pytest.mark.parametrize('bounded', [False, True])
@pytest.mark.parametrize('foreign', [False, True])
async def test_kernel_checks_actual_binding_in_both_entrypoints(owned, tmp_path, bounded, foreign):
    # 即使事件和上下文一起自报其他设备，原SQL绑定仍须决定是否接受。
    from camctl.persistence.repositories.baseline_reads import SaveBaselineReadIntent
    request = await _intent(owned, tmp_path)

    class Command:
        def plan(self, scope):
            plan = SaveBaselineReadIntent(request, request.call_key).plan(scope)
            if foreign:
                event = plan.events[0]
                row = event.rows[0]
                evidence = {'baseline_read_request': {
                    **event.evidence['baseline_read_request'], 'device_id': 'other'}}
                event = replace(event, evidence=evidence, rows=(replace(row,
                    after=replace(row.after, values={**row.after.values, 'device_id': 'other'})),))
                state = {**plan.state_rows, 'actions': {request.action_id: {
                    **plan.state_rows['actions'][request.action_id], 'device_id': 'other'}}}
                plan = replace(plan, events=(event,), state_rows=state)
            if bounded:
                return BoundedCommandPlan(plan.events,
                    lambda event, transaction: EventContext(transaction, plan.owners, plan.state_rows),
                    plan.result, lambda connection: None)
            return plan

    receipt = commit_operation(Command(), request.call_key, owned)
    assert receipt.kind == ('rolled_back' if foreign else 'completed'), receipt.error
    assert owned.connection.execute('SELECT COUNT(*) FROM baseline_read_calls').fetchone() == (0 if foreign else 1,)


async def test_call_belongs_to_action_history_and_snapshot(owned, tmp_path):
    from camctl.history.snapshots import SnapshotRef, prepare_snapshot
    from camctl.persistence.executor import DbExecutor
    from camctl.persistence.repositories.history import HistoryRepository, SqliteSnapshotStore
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
    request = await _intent(owned, tmp_path)
    history = HistoryRepository(tmp_path / 'state.db')
    before = history.current_boundary()
    capture = CaptureRepository()
    saved = capture.save_baseline_read_intent(request, request.call_key, owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    view = saved.value
    identity = ('baseline_read_calls', view.call_id)
    assert identity not in history.restore_entity('action', request.action_id, before)
    original = history.restore_entity('action', request.action_id, view.boundary)[identity]
    executor = DbExecutor(lambda: open_existing(tmp_path / 'state.db', DbOpenMode.EXISTING_RW, DbConfig()),
                          capacity=4, enqueue_timeout_seconds=9)
    try:
        store = SqliteSnapshotStore(tmp_path / 'state.db', executor)
        image = (await store.load_entity_images((SnapshotRef(1, request.action_id),)))[0]
        assert any((row.table, row.row_id) == identity for row in image.rows)
        await store.save_snapshots((prepare_snapshot(image),))
    finally:
        await executor.close()
    assert _fix(owned, request.activity_id).kind is DbOutcomeKind.COMPLETED
    assert history.restore_entity('action', request.action_id, view.boundary)[identity] == original
    assert capture.read_baseline_call(view.call_id, view.boundary, owned) == view

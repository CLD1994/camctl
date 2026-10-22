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


@pytest.mark.parametrize('count', [0, 1, 127, 128])
async def test_actual_return_saves_one_atomic_group(owned, tmp_path, count):
    from camctl.capture import baseline_models as models
    from camctl.contracts.pages import Page
    from camctl.devices.directory import DirectoryCursor, DirectoryRead
    from ..capture.test_baseline_history import _entries
    intent = await _intent(owned, tmp_path)
    capture = CaptureRepository()
    first = capture.save_baseline_read_intent(intent, intent.call_key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    entries = _entries(*(f'{index:03d}.OSV' for index in range(count)))
    cursor = DirectoryCursor(intent.binding, intent.directories, 1, None)
    returned = models.BaselineReadReturn(first.value.call_id, _SCHEDULED + 1, None,
        DirectoryRead(None, None, Page(entries, cursor)))
    saved = capture.save_baseline_read_return(returned, intent.result_save_key, owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    result = saved.value
    assert result.request == intent and result.state.name == 'RETURNED'
    assert result.returned_at == _SCHEDULED + 1 and result.error is None
    assert result.next_cursor == cursor
    transaction = owned.connection.execute(
        'SELECT first_event_id,last_event_id FROM history_transactions WHERE operation_key=?',
        (str(intent.result_save_key),)).fetchone()
    assert transaction == (result.result_event_id - bool(count), result.result_event_id)
    if count:
        assert result.chunk_event_id == transaction[0]
        raw = owned.connection.execute('SELECT body_json FROM history_events WHERE id=?',
                                      (result.chunk_event_id,)).fetchone()[0]
        import json
        assert len(json.loads(raw)['evidence']['entries']) == count
    else:
        assert result.chunk_event_id is None
    assert capture.read_baseline_call(result.call_id, first.value.boundary, owned) == first.value
    assert capture.read_baseline_call(result.call_id, result.boundary, owned) == result
    assert capture.save_baseline_read_return(
        returned, intent.result_save_key, owned).value == result


async def test_empty_page_uses_actual_next_cursor_for_followup(owned, tmp_path):
    from camctl.capture import baseline_models as models
    from camctl.contracts.pages import Page
    from camctl.devices.directory import DirectoryCursor, DirectoryRead
    intent = await _intent(owned, tmp_path)
    capture = CaptureRepository()
    first = capture.save_baseline_read_intent(intent, intent.call_key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    cursor = DirectoryCursor(intent.binding, intent.directories, 1, None)
    returned = models.BaselineReadReturn(first.value.call_id, _SCHEDULED + 1, None,
        DirectoryRead(None, None, Page((), cursor)))
    saved = capture.save_baseline_read_return(returned, intent.result_save_key, owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    assert saved.value.next_cursor == cursor and saved.value.chunk_event_id is None
    followup = replace(intent, cursor=cursor, read_no=2, call_key=new_operation_key(),
        result_save_key=new_operation_key(), occurred_at=_SCHEDULED + 2)
    second = capture.save_baseline_read_intent(followup, followup.call_key, owned)
    assert second.kind is DbOutcomeKind.COMPLETED, second.error
    assert second.value.request.baseline_ref.chunk_count == 0
    assert second.value.request.baseline_ref.entry_count == 0


async def test_actual_error_stays_returned_and_does_not_end_action(owned, tmp_path):
    from camctl.capture import baseline_models as models
    from camctl.devices.directory import DirectoryRead
    from camctl.operations.models import ErrorValue
    intent = await _intent(owned, tmp_path)
    capture = CaptureRepository()
    first = capture.save_baseline_read_intent(intent, intent.call_key, owned)
    assert first.kind is DbOutcomeKind.COMPLETED, first.error
    error = ErrorValue('transport_timeout', 'baseline', {'command': 'directory'})
    returned = models.BaselineReadReturn(first.value.call_id, _SCHEDULED + 1, error,
        DirectoryRead(None, error, None))
    saved = capture.save_baseline_read_return(returned, intent.result_save_key, owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    assert saved.value.error == error and saved.value.state.name == 'RETURNED'
    assert saved.value.chunk_event_id is None and saved.value.next_cursor is None
    assert capture.read_baseline_failure_anchor(intent.action_id,first.value.call_id,
        first.value.boundary,owned) is None
    assert capture.read_baseline_failure_anchor(intent.action_id,first.value.call_id,
        saved.value.boundary,owned)==saved.value
    assert owned.connection.execute('SELECT status FROM actions WHERE id=?',
                                  (intent.action_id,)).fetchone() == (2,)


@pytest.mark.parametrize('bounded', [False, True])
@pytest.mark.parametrize('mutation', ['valid','chunk_only','return_only','wrong_chunk','wrong_time','extra','no_scope'])
async def test_return_group_is_complete_in_both_entrypoints(owned,tmp_path,bounded,mutation):
    from camctl.capture import baseline_models as models
    from camctl.contracts.pages import Page
    from camctl.devices.directory import DirectoryCursor, DirectoryRead
    from camctl.persistence.repositories.baseline_reads import SaveBaselineReadReturn
    from ..capture.test_baseline_history import _entries
    intent=await _intent(owned,tmp_path)
    capture=CaptureRepository()
    first=capture.save_baseline_read_intent(intent,intent.call_key,owned)
    assert first.kind is DbOutcomeKind.COMPLETED,first.error
    request=models.BaselineReadReturn(first.value.call_id,_SCHEDULED+1,None,
        DirectoryRead(None,None,Page(_entries('a.OSV'),
            DirectoryCursor(intent.binding,intent.directories,1,None))))
    before=owned.connection.execute('SELECT COUNT(*) FROM history_events').fetchone()

    class Command:
        def plan(self,scope):
            plan=SaveBaselineReadReturn(request,intent.result_save_key).plan(scope)
            events=plan.events
            write_scope=plan.baseline_read_write_scope
            if mutation=='chunk_only':
                events=events[:1]
            elif mutation=='return_only':
                events=events[1:]
            elif mutation=='wrong_chunk':
                raw={**events[-1].evidence['baseline_read_request'],'chunk_event_id':first.value.intent_event_id}
                events=(*events[:-1],replace(events[-1],evidence={'baseline_read_request':raw}))
            elif mutation=='wrong_time':
                events=(replace(events[0],occurred_at=_SCHEDULED+5),events[1])
            elif mutation=='extra':
                events=(*events,events[-1])
            elif mutation=='no_scope':
                write_scope=None
            if bounded:
                return BoundedCommandPlan(events,
                    lambda event,transaction:EventContext(transaction,plan.owners,plan.state_rows,
                        baseline_reads=plan.baseline_reads),plan.result,lambda connection:None,
                    baseline_read_write_scope=write_scope)
            return replace(plan,events=events,baseline_read_write_scope=write_scope)

    receipt=commit_operation(Command(),intent.result_save_key,owned)
    assert receipt.kind==('completed' if mutation=='valid' else 'rolled_back'),receipt.error
    if mutation=='valid':
        assert capture.read_baseline_call(first.value.call_id,receipt.result.boundary,owned)==receipt.result
    else:
        assert owned.connection.execute('SELECT COUNT(*) FROM history_events').fetchone()==before
        assert owned.connection.execute('SELECT state,result_event_id FROM baseline_read_calls').fetchone()==(1,None)
        assert _ref(owned,intent.activity_id)==intent.baseline_ref


@pytest.mark.parametrize('field', ['returned_at','entries','key'])
async def test_return_original_key_rejects_changed_input(owned,tmp_path,field):
    from camctl.capture import baseline_models as models
    from camctl.contracts.pages import Page
    from camctl.devices.directory import DirectoryCursor,DirectoryRead
    from ..capture.test_baseline_history import _entries
    intent=await _intent(owned,tmp_path)
    capture=CaptureRepository()
    first=capture.save_baseline_read_intent(intent,intent.call_key,owned)
    cursor=DirectoryCursor(intent.binding,intent.directories,1,None)
    request=models.BaselineReadReturn(first.value.call_id,_SCHEDULED+1,None,
        DirectoryRead(None,None,Page(_entries('a.OSV'),cursor)))
    saved=capture.save_baseline_read_return(request,intent.result_save_key,owned)
    assert saved.kind is DbOutcomeKind.COMPLETED,saved.error
    changed=(replace(request,returned_at=_SCHEDULED+2) if field=='returned_at' else
        replace(request,returned=DirectoryRead(None,None,Page(_entries('b.OSV'),cursor)))
        if field=='entries' else request)
    receipt=capture.save_baseline_read_return(changed,
        new_operation_key() if field=='key' else intent.result_save_key,owned)
    assert receipt.kind is DbOutcomeKind.ROLLED_BACK
    assert capture.read_baseline_call(first.value.call_id,saved.value.boundary,owned)==saved.value


async def test_nonempty_return_provides_exact_followup_prefix(owned,tmp_path):
    from camctl.capture import baseline_models as models
    from camctl.contracts.pages import Page
    from camctl.devices.directory import DirectoryCursor,DirectoryRead
    from ..capture.test_baseline_history import _entries
    intent=await _intent(owned,tmp_path)
    capture=CaptureRepository()
    first=capture.save_baseline_read_intent(intent,intent.call_key,owned)
    cursor=DirectoryCursor(intent.binding,intent.directories,0,'/mnt/media_rw/emulated/DCIM/b.OSV')
    returned=models.BaselineReadReturn(first.value.call_id,_SCHEDULED+1,None,
        DirectoryRead(None,None,Page(_entries('a.OSV','b.OSV'),cursor)))
    saved=capture.save_baseline_read_return(returned,intent.result_save_key,owned)
    assert saved.kind is DbOutcomeKind.COMPLETED,saved.error
    ref=models.BaselineRef(intent.activity_id,models.BaselineState.COLLECTING,
        saved.value.chunk_event_id,saved.value.chunk_event_id,1,2)
    followup=replace(intent,baseline_ref=ref,cursor=cursor,read_no=2,
        call_key=new_operation_key(),result_save_key=new_operation_key())
    wrong=replace(followup,baseline_ref=replace(ref,entry_count=3))
    assert capture.save_baseline_read_intent(wrong,wrong.call_key,owned).kind is DbOutcomeKind.ROLLED_BACK
    second=capture.save_baseline_read_intent(followup,followup.call_key,owned)
    assert second.kind is DbOutcomeKind.COMPLETED,second.error


async def test_return_replay_after_later_call_and_fix_keeps_original_group(owned,tmp_path):
    from camctl.capture import baseline_models as models
    from camctl.contracts.pages import Page
    from camctl.devices.directory import DirectoryCursor,DirectoryRead
    from ..capture.test_baseline_history import _entries
    intent=await _intent(owned,tmp_path)
    capture=CaptureRepository()
    first=capture.save_baseline_read_intent(intent,intent.call_key,owned)
    cursor=DirectoryCursor(intent.binding,intent.directories,1,None)
    returned=models.BaselineReadReturn(first.value.call_id,_SCHEDULED+1,None,
        DirectoryRead(None,None,Page(_entries('a.OSV'),cursor)))
    saved=capture.save_baseline_read_return(returned,intent.result_save_key,owned)
    assert saved.kind is DbOutcomeKind.COMPLETED,saved.error
    ref=models.BaselineRef(intent.activity_id,models.BaselineState.COLLECTING,
        saved.value.chunk_event_id,saved.value.chunk_event_id,1,1)
    followup=replace(intent,cursor=cursor,read_no=2,baseline_ref=ref,
        call_key=new_operation_key(),result_save_key=new_operation_key())
    second=capture.save_baseline_read_intent(followup,followup.call_key,owned)
    assert second.kind is DbOutcomeKind.COMPLETED,second.error
    last=models.BaselineReadReturn(second.value.call_id,_SCHEDULED+2,None,
        DirectoryRead(None,None,Page((),None)))
    assert capture.save_baseline_read_return(last,followup.result_save_key,owned).kind is DbOutcomeKind.COMPLETED
    assert _fix(owned,intent.activity_id,chunks=1,entries=1).kind is DbOutcomeKind.COMPLETED
    replay=capture.save_baseline_read_return(returned,intent.result_save_key,owned)
    assert replay.kind is DbOutcomeKind.COMPLETED,replay.error
    assert replay.value==saved.value
    assert capture.read_baseline_call(first.value.call_id,first.value.boundary,owned)==first.value


async def test_fresh_original_result_lookup_and_verification_never_create_a_write(owned,tmp_path):
    from camctl.capture import baseline_models as models
    from camctl.contracts.pages import Page
    from camctl.devices.directory import DirectoryCursor,DirectoryRead
    from camctl.persistence.runtime import DbConfig,DbOpenMode,open_existing
    intent=await _intent(owned,tmp_path)
    capture=CaptureRepository()
    first=capture.save_baseline_read_intent(intent,intent.call_key,owned)
    assert first.kind is DbOutcomeKind.COMPLETED,first.error
    assert capture.read_baseline_call_result(first.value.call_id,owned) is None
    cursor=DirectoryCursor(intent.binding,intent.directories,1,None)
    returned=models.BaselineReadReturn(first.value.call_id,_SCHEDULED+1,None,
        DirectoryRead(None,None,Page((),cursor)))
    saved=capture.save_baseline_read_return(returned,intent.result_save_key,owned)
    assert saved.kind is DbOutcomeKind.COMPLETED,saved.error
    before=owned.connection.execute('SELECT COUNT(*) FROM history_events').fetchone()
    fresh=open_existing(tmp_path/'state.db',DbOpenMode.EXISTING_RW,DbConfig())
    try:
        assert capture.read_baseline_call_result(first.value.call_id,fresh)==saved.value
        assert capture.verify_baseline_read_write(returned,intent.result_save_key,fresh)==saved.value
        assert capture.verify_baseline_read_write(intent,intent.call_key,fresh)==first.value
        assert capture.verify_baseline_read_write(replace(intent,call_key=new_operation_key()),
            new_operation_key(),fresh) is None
    finally:
        fresh.connection.close()
    assert owned.connection.execute('SELECT COUNT(*) FROM history_events').fetchone()==before


async def test_call_discovery_uses_fixed_boundary_and_cursor(owned,tmp_path):
    from camctl.capture import baseline_models as models
    from camctl.contracts.pages import Page
    from camctl.devices.directory import DirectoryCursor,DirectoryRead
    intent=await _intent(owned,tmp_path)
    capture=CaptureRepository()
    first=capture.save_baseline_read_intent(intent,intent.call_key,owned)
    cursor=DirectoryCursor(intent.binding,intent.directories,1,None)
    returned=models.BaselineReadReturn(first.value.call_id,_SCHEDULED+1,None,
        DirectoryRead(None,None,Page((),cursor)))
    saved=capture.save_baseline_read_return(returned,intent.result_save_key,owned)
    followup=replace(intent,cursor=cursor,read_no=2,call_key=new_operation_key(),result_save_key=new_operation_key())
    second=capture.save_baseline_read_intent(followup,followup.call_key,owned)
    assert second.kind is DbOutcomeKind.COMPLETED,second.error
    old=capture.read_baseline_calls(intent.action_id,first.value.boundary,None,1,owned)
    assert old.items==(first.value,) and old.next_cursor is None
    page=capture.read_baseline_calls(intent.action_id,second.value.boundary,None,1,owned)
    assert page.items==(saved.value,) and page.next_cursor is not None
    last=capture.read_baseline_calls(intent.action_id,second.value.boundary,page.next_cursor,1,owned)
    assert last.items==(second.value,) and last.next_cursor is None
    from camctl.contracts.values import ConsistencyError
    with pytest.raises(ConsistencyError):
        capture.read_baseline_calls(intent.action_id,first.value.boundary,page.next_cursor,1,owned)
    for limit in (2,128):
        complete=capture.read_baseline_calls(intent.action_id,second.value.boundary,None,limit,owned)
        assert complete.items==(saved.value,second.value) and complete.next_cursor is None

"""电机历史的初始回放、快照正向恢复及当前投影逆向恢复。"""
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from camctl.contracts.history_values import INITIAL_BOUNDARY
from camctl.contracts.public_projection import ProjectionInput, project_public
from camctl.contracts.values import new_operation_key
from camctl.history.replay import EntityImage, RestoreSeed, restore
from camctl.history.snapshots import (
    MaintenanceContext, MaintenanceState, SnapshotRef, decode_snapshot, maintain_snapshots,
)
from camctl.motor.models import FinishSendRequest, MotorFinalKind
from camctl.motor.notification import NotificationWriter
from camctl.persistence.executor import DbExecutor
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.history import HistoryRepository, SqliteSnapshotStore
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..persistence.test_motor_transactions import NOW, _admit, _body, owned, prepared
from ..history.test_complete_history import _validated_events, _boundary_of


@pytest.mark.asyncio
async def test_motor_history_all_restore_paths_have_same_public_result(prepared, monkeypatch):
    owned, motor, _, _, permit = prepared
    path = Path(owned.connection.execute('PRAGMA database_list').fetchone()[2])
    history = HistoryRepository(path)
    pending_boundary = history.current_boundary()
    executor = DbExecutor(lambda: open_existing(path, DbOpenMode.EXISTING_RW, DbConfig()),
                          capacity=8, enqueue_timeout_seconds=9)
    store = SqliteSnapshotStore(path, executor)
    sender = Mock(side_effect=AssertionError('历史恢复不能发送'))
    monkeypatch.setattr(NotificationWriter, 'send', sender)
    try:
        await maintain_snapshots(MaintenanceContext(
            threshold=1, batch_size=8, state=MaintenanceState(), store=store))
        snapshot = owned.connection.execute(
            'SELECT boundary_event_id,content FROM entity_snapshots WHERE entity_type=1 AND entity_id=1'
        ).fetchone()
        assert snapshot is not None
        header, rows = decode_snapshot(snapshot[1], expect_ref=SnapshotRef(1,1))
        finished = motor.finish_send(FinishSendRequest(
            1,NOW+1,MotorFinalKind.WRITTEN,permit,written_bytes=78),new_operation_key(),owned)
        assert finished.kind is DbOutcomeKind.COMPLETED,finished.error
        boundary = history.current_boundary()
        events = _validated_events(owned.connection,'action',1,boundary.last_event_id)
        # 每行按实际对象归属回放，不把同事务的父计划行加入动作映像。
        events = [replace(event,row_owners={
            (change.table,change.row_id):(4,change.row_id) if change.table=='plans' else (1,1)
            for change in event.envelope.rows}) for event in events]
        initial = restore(RestoreSeed(EntityImage(1,1,False,{},0,0),INITIAL_BOUNDARY),events,boundary)
        seed = EntityImage(1,1,True,{(row.table,row.row_id):dict(row.values) for row in rows},
                           snapshot[0],header['change_count'])
        forward = restore(RestoreSeed(seed,_boundary_of(owned.connection,snapshot[0])),events,boundary)
        reverse = history.restore_entity('action',1,boundary,event_batch_size=1)
        expected = {'action_instance_id':'1','name':'定位0','type':'motor_control',
                    'scheduled_at':'2026-10-08 09:00:00','input_params':{'position':100},
                    'policy':{'max_delay_ms':1000},'status':'succeeded'}
        for image in [initial.rows,forward.rows,reverse]:
            assert image[('motor_notifications',1)]['outcome']==3
            assert project_public(ProjectionInput('action',1,{'actions':{1:image[('actions',1)]}}))==expected
        old = history.restore_entity('action',1,pending_boundary,event_batch_size=1)
        assert old[('motor_notifications',1)]['outcome']==1
        assert old[('actions',1)]['status']==2
        sender.assert_not_called()
    finally:
        await executor.close()


def test_group_source_resolution_excludes_motor(owned):
    from camctl.outputs.sources import SourceSpec, resolve_source, ResolveFailure
    from camctl.persistence.repositories.outputs import SqliteSourceLookup
    body=_body()
    body['actions'][0]['group']='axis'
    _admit(owned,body)
    result=resolve_source(SourceSpec(group='axis'),SqliteSourceLookup(owned.connection,1))
    assert result.member_action_ids==()
    assert result.failure is ResolveFailure.NOT_OUTPUT_SOURCE


def test_partial_write_is_persisted_and_disables_later_actions(owned):
    from types import SimpleNamespace
    from camctl.motor.service import MotorRuntime, advance_motor
    from camctl.persistence.repositories.motor import MotorRepository, register_motor_guards
    from camctl.persistence.repositories.scheduling import register_window_guard
    from camctl.session.clock import ClockCheckInput

    register_motor_guards()
    register_window_guard()
    _admit(owned, _body(count=2))
    write = Mock(return_value=5)
    close = Mock()
    writer = NotificationWriter(123, pipe_buf=512, write=write, close=close)
    runtime = MotorRuntime(owned, MotorRepository(), writer,
                           SimpleNamespace(utc_micros=lambda: NOW),
                           lambda connection: ClockCheckInput(None, 0, 0, 0), {})
    advance_motor(1, runtime)
    advance_motor(2, runtime)
    write.assert_called_once()
    close.assert_called_once_with(123)
    first = runtime.repository.read_facts(1, owned)
    second = runtime.repository.read_facts(2, owned)
    assert first.action['error_code'] == 28
    assert first.action['error_details_json'] == {'reason': 'short_write', 'written_bytes': 5}
    assert first.notification.written_bytes == 5
    assert second.action['error_code'] == 27
    assert second.action['error_details_json'] == {'reason': 'disabled_after_partial_write'}
    assert second.notification.intent_at is None
    assert second.notification.outcome.name == 'NOT_SENT'

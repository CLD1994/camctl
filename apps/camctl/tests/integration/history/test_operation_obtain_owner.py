"""既有 run producer 明写空 obtain owner，完整事件与同 H 恢复保持一致。"""
from pathlib import Path

import pytest

from camctl.contracts.json_values import json_equal, parse_exact_json
from camctl.contracts.values import new_operation_key
from camctl.history.events import business_columns
from camctl.operations.attempts import AttemptConfig, AttemptIntent, AttemptTarget, OperationKind, QueryPurpose
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.history import HistoryRepository
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import row_facts

from ..capture.result_consumer_fixtures import consumer_world
from .test_operation_ownership import operation_world, owned, _create_flow
from .test_complete_history import world


def _assert_same_h_null_owner(owned, expected, owner):
    """独立新连接经真实历史仓储恢复全对象，逐列核 run 和完整创建行。"""
    path = Path(owned.connection.execute('PRAGMA database_list').fetchone()[2])
    history = HistoryRepository(path)
    boundary = history.current_boundary()
    frozen = history.restore_entity(*owner, boundary)
    assert expected
    for run_id in expected:
        saved = row_facts(owned.connection, 'operation_runs', run_id)
        assert saved['obtain_item_id'] is None
        restored = frozen['operation_runs', run_id]
        assert restored['obtain_item_id'] is None
        assert all(json_equal(restored[column], saved[column]) for column in business_columns('operation_runs'))
        creations = []
        for (text,) in owned.connection.execute('SELECT body_json FROM history_events ORDER BY id'):
            for row in parse_exact_json(text).get('rows', ()):
                if row['table'] == 'operation_runs' and row['id'] == run_id and not row['before']['exists']:
                    creations.append(row['after']['values'])
        assert len(creations) == 1
        assert set(creations[0]) == set(business_columns('operation_runs'))
        assert creations[0]['obtain_item_id'] is None
        for name in ('action_id','delivery_id','kind','query_purpose','responsibility_key',
                     'activity_id','copy_id','cleanup_item_id','obtain_item_id','session_key'):
            assert json_equal(creations[0][name], saved[name])
    fresh = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        for run_id in expected:
            assert row_facts(fresh.connection, 'operation_runs', run_id)['obtain_item_id'] is None
        assert HistoryRepository(path).restore_entity(*owner, boundary) == frozen
    finally:
        fresh.connection.close()
    return boundary, frozen


@pytest.mark.asyncio
async def test_scheduling_start_and_ordinary_query_keep_null_owner(tmp_path):
    owned, runtime, action_id, _ = await consumer_world(tmp_path, 'photo')
    try:
        starts = [row[0] for row in owned.connection.execute('SELECT id FROM operation_runs WHERE kind=1')]
        assert len(starts) == 1
        intent = AttemptIntent('query', action_id, OperationKind.QUERY_ACTIVITY, AttemptTarget(),
            QueryPurpose.BEFORE_EXECUTION, AttemptConfig(3, '5', '0'), runtime.wall_us())
        key = new_operation_key()
        first = OperationRepository().begin_attempt(intent, key, owned)
        assert first.kind is DbOutcomeKind.COMPLETED, first.error
        _assert_same_h_null_owner(owned, starts+[first.value.ticket.run_id], ('action', action_id))
        repeated = OperationRepository().begin_attempt(intent, key, owned)
        assert repeated.kind is DbOutcomeKind.COMPLETED and repeated.value == first.value
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_emergency_final_run_and_attempt_history_keep_null_owner(operation_world):
    run_id, owner = _create_flow(operation_world, 'emergency_stop')
    _assert_same_h_null_owner(operation_world.owned, [run_id], ('action', owner))


@pytest.mark.asyncio
async def test_binding_failure_creates_both_necessary_runs_with_null_owner(tmp_path):
    from ..capture.test_cancel_timelapse_exhaustion_files import _apply_public_cancel
    from camctl.capture.media import RecordingFailure
    from camctl.persistence.repositories.capture import CaptureRepository, FinishBindingFailure
    owned, runtime, action_id, _ = await consumer_world(tmp_path, 'timelapse')
    try:
        _apply_public_cancel(owned, action_id, runtime.wall_us())
        request = FinishBindingFailure(action_id, runtime.wall_us(), RecordingFailure(
            'device_binding_unavailable', {'device_id':'cam-1','expected_driver_id':'camctl-adb','reason':'missing'}),
            canceled=True, stop_config=runtime.stop_config, check_config=runtime.check_config)
        key = new_operation_key()
        result = CaptureRepository().finish_binding_failure(request, key, owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        runs = owned.connection.execute('SELECT id,kind FROM operation_runs WHERE kind IN (2,7)').fetchall()
        assert {kind for _,kind in runs} == {2,7}
        boundary, frozen = _assert_same_h_null_owner(owned, [identity for identity,_ in runs], ('action', action_id))
        before = tuple(owned.connection.iterdump())
        repeated = CaptureRepository().finish_binding_failure(request, key, owned)
        assert repeated.kind is DbOutcomeKind.COMPLETED, repeated.error
        assert tuple(owned.connection.iterdump()) == before
        path = Path(owned.connection.execute('PRAGMA database_list').fetchone()[2])
        assert HistoryRepository(path).restore_entity('action', action_id, boundary) == frozen
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_delivery_read_run_full_event_and_history_keep_null_owner(world):
    runs = world.owned.connection.execute('SELECT id,delivery_id FROM operation_runs WHERE kind=3').fetchall()
    assert runs
    for run_id, delivery_id in runs:
        assert delivery_id is not None
        _assert_same_h_null_owner(world.owned, [run_id], ('delivery', delivery_id))


@pytest.mark.asyncio
async def test_internal_read_run_full_event_and_history_keep_null_owner(tmp_path):
    from dataclasses import replace
    from ..outputs.test_read_associations import _command
    from .test_file_history import _environment, _observe, _confirm, _complete, _presence, _seed_processing
    from camctl.persistence.repositories.capture import CaptureRepository
    from camctl.persistence.repositories.outputs import OutputsRepository
    from camctl.outputs.qualification import QualificationOutcome
    owned, _ = _environment(tmp_path)
    try:
        capture = CaptureRepository()
        source = _observe(capture, owned, 11, 'task-a/internal.mp4')
        _confirm(capture, owned, source, 11)
        _complete(capture, owned, source)
        _presence(capture, owned, source, 2)
        _seed_processing(owned.connection, 1, 11, source)
        owned.connection.commit()
        command = replace(_command(internal=True), processing_id=1, source_device_file_id=source)
        key = new_operation_key()
        result = OutputsRepository().grant_file(command, key, owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert result.value.outcome is QualificationOutcome.GRANTED, result.value
        _assert_same_h_null_owner(owned, [result.value.run_id], ('action', 11))
        before = tuple(owned.connection.iterdump())
        repeated = OutputsRepository().grant_file(command, key, owned)
        assert repeated.kind is DbOutcomeKind.COMPLETED, repeated.error
        assert repeated.value == result.value
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()

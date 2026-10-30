"""范围清理的来源事务与原申请保存责任共同恢复。"""

import json

import pytest

from camctl.contracts.values import new_operation_key
from camctl.outputs.sources import SourceSpec
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.outputs import OutputsRepository, ResolveSources
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from .test_sources import _NOW, _seed_action, _six_form_environment
from .test_source_resolution_batches import _real_environment


def _environment(tmp_path):
    target, owned = _six_form_environment(tmp_path)
    owned.connection.execute(
        'UPDATE actions SET type=5,execution_spec_json=\'{}\',target_selection_state=1,input_fields_json=? WHERE id=30',
        (json.dumps({'params': {'source': {'plan_instance_id': '2'}}}),))
    owned.connection.commit()
    return target, owned


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['rollback', 'unknown_before_commit', 'unknown_after_commit'])
async def test_cleanup_source_save_rebuild_keeps_original_request_and_key(tmp_path, capture_resources, monkeypatch, fault):
    from dataclasses import replace
    from camctl.bootstrap.cleanup_assembly import session_cleanup_assembly
    from camctl.contracts.values import ConsistencyError
    from camctl.devices.drivers.registry import DriverRegistry
    from camctl.outputs.cleanup_flow import advance_cleanup
    from .test_cleanup_query_commit_recovery import _CommitFaultConnection

    target, owned, request = await _real_environment(tmp_path, cleanup=True, captures=1)
    owner_id = request.action_id
    member_id = owned.connection.execute('SELECT id FROM actions WHERE plan_id=1').fetchone()[0]
    database = await capture_resources.database(target)
    pending, calls = {}, []
    actual = OutputsRepository.resolve_sources
    def resolve(repository, current_request, key, current):
        calls.append((current_request, key))
        if len(calls) == 1:
            if fault == 'rollback':
                return DbOutcome(DbOutcomeKind.ROLLED_BACK, error=RuntimeError('保存结果中断'))
            current = replace(current, connection=_CommitFaultConnection(
                current.connection, after_commit=fault == 'unknown_after_commit'))
        return actual(repository, current_request, key, current)
    monkeypatch.setattr(OutputsRepository, 'resolve_sources', resolve)
    factory = session_cleanup_assembly(devices={}, drivers=DriverRegistry(),
        max_delete_attempts=1, max_query_attempts=1, pending_saves=pending, occurred_at=lambda: _NOW)
    try:
        runtime = await factory(database)
        with pytest.raises(ConsistencyError):
            await advance_cleanup(runtime)
        assert len(calls) == 1
        first_request, first_key = calls[0]
        assert first_request == request
        database = await capture_resources.restart_database(target)
        owned.connection.close()
        owned = open_existing(target, DbOpenMode.EXISTING_RW, DbConfig())
        resumed = await session_cleanup_assembly(devices={}, drivers=DriverRegistry(),
            max_delete_attempts=1, max_query_attempts=1, pending_saves=pending,
            occurred_at=lambda: _NOW + 100)(database)
        await advance_cleanup(resumed)
        assert calls[:2] == [(request, first_key), (request, first_key)]
        assert len(calls) == 2 and not pending
        assert owned.connection.execute(
            'SELECT source_resolution_state,resolved_source_plan_id FROM actions WHERE id=?', (owner_id,)).fetchone() == (2, 1)
        assert owned.connection.execute(
            'SELECT depends_on_action_id FROM action_dependencies WHERE action_id=?', (owner_id,)).fetchall() == [(member_id,)]
        assert owned.connection.execute('SELECT COUNT(*) FROM obtain_source_selections').fetchone() == (0,)
        assert owned.connection.execute('SELECT COUNT(*) FROM history_transactions WHERE operation_key=?',
            (str(first_key),)).fetchone() == (1,)
        assert owned.connection.execute(
            'SELECT DISTINCT occurred_at FROM history_events WHERE event_type=3').fetchall() == [(_NOW,)]
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_fixed_cleanup_source_is_reused_without_new_dependencies(tmp_path):
    _, owned, request = await _real_environment(tmp_path, cleanup=True, captures=1)
    repository = OutputsRepository()
    key = new_operation_key()
    try:
        first = repository.resolve_sources(request, key, owned)
        assert first.kind is DbOutcomeKind.COMPLETED, first.error
        history = owned.connection.execute('SELECT * FROM history_events').fetchall()
        for operation_key in (key, new_operation_key()):
            result = repository.resolve_sources(request, operation_key, owned)
            assert result.kind is DbOutcomeKind.COMPLETED, result.error
            assert result.value == first.value
            assert owned.connection.execute('SELECT * FROM history_events').fetchall() == history
        assert owned.connection.execute('SELECT COUNT(*) FROM obtain_source_selections').fetchone() == (0,)
    finally:
        owned.connection.close()


@pytest.mark.parametrize('source,expected', [
    ({'plan_instance_id': '2'}, (21, 23, 24)),
    ({'plan_instance_id': '2', 'group': '晨拍'}, (21, 23)),
    ({'action_instance_id': '21'}, (21,)),
])
def test_cleanup_resolves_every_selected_capture_and_excludes_other_actions(tmp_path, source, expected):
    from camctl.outputs.sources import read_source_spec

    _, owned = _environment(tmp_path)
    try:
        connection = owned.connection
        _seed_action(connection, 23, 2, action_type=1, group_name='晨拍')
        _seed_action(connection, 24, 2, action_type=3, group_name='晚拍')
        fields = json.dumps({'params': {'source': source}}, ensure_ascii=False)
        connection.execute('UPDATE actions SET input_fields_json=? WHERE id=30', (fields,))
        connection.commit()
        result = OutputsRepository().resolve_sources(
            ResolveSources(30, read_source_spec(fields), _NOW), new_operation_key(), owned)

        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert result.value.member_count == len(expected)
        assert result.value.selection_count == 0
        assert connection.execute(
            'SELECT depends_on_action_id FROM action_dependencies WHERE action_id=30 ORDER BY id').fetchall() == [(identity,) for identity in expected]
        assert connection.execute('SELECT COUNT(*) FROM obtain_source_selections').fetchone() == (0,)
    finally:
        owned.connection.close()

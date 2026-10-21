"""范围清理的来源事务与原申请保存责任共同恢复。"""

import json
from types import SimpleNamespace

import pytest

from camctl.contracts.values import new_operation_key
from camctl.outputs.cleanup_ownership import save_cleanup_request
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


@pytest.mark.parametrize('fault', ['rollback', 'unknown_before_commit', 'unknown_after_commit'])
def test_cleanup_source_save_rebuild_keeps_original_request_and_key(tmp_path, fault):
    target, owned, request = _real_environment(tmp_path,cleanup=True,captures=1)
    owner_id = request.action_id
    member_id = owned.connection.execute("SELECT id FROM actions WHERE plan_id=1").fetchone()[0]
    calls = []
    repository = OutputsRepository()

    class InterruptedRepository:
        def resolve_sources(self, request, key, connection):
            calls.append((request, key))
            if len(calls) == 1:
                if fault == 'unknown_after_commit':
                    saved = repository.resolve_sources(request, key, connection)
                    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
                kind = DbOutcomeKind.ROLLED_BACK if fault == 'rollback' else DbOutcomeKind.UNKNOWN
                return DbOutcome(kind, error=RuntimeError('保存结果中断'))
            return repository.resolve_sources(request, key, connection)

    pending = {}
    port = InterruptedRepository()
    runtime = SimpleNamespace(owned=owned, outputs=port, pending_saves=pending)
    first = save_cleanup_request(runtime, 'resolve_sources', request, action=True)
    assert first.kind is not DbOutcomeKind.COMPLETED
    assert len(pending) == 1
    original = pending[('resolve_sources', owner_id)]
    owned.connection.close()
    owned = open_existing(target, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        resumed = SimpleNamespace(owned=owned, outputs=port, pending_saves=pending)
        result = save_cleanup_request(resumed, 'resolve_sources',
            ResolveSources(owner_id, request.spec, _NOW + 100), action=True)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        assert result.value.fixed and result.value.member_count == 1
        assert result.value.selection_count == 0
        assert calls == [(request, original.key), (request, original.key)]
        assert pending == {}
        assert owned.connection.execute(
            'SELECT source_resolution_state,resolved_source_plan_id FROM actions WHERE id=?', (owner_id,)).fetchone() == (2, 1)
        assert owned.connection.execute(
            'SELECT depends_on_action_id FROM action_dependencies WHERE action_id=?', (owner_id,)).fetchall() == [(member_id,)]
        assert owned.connection.execute('SELECT COUNT(*) FROM obtain_source_selections').fetchone() == (0,)
        assert owned.connection.execute(
            'SELECT DISTINCT occurred_at FROM history_events WHERE event_type=3').fetchall() == [(_NOW,)]
    finally:
        owned.connection.close()


def test_fixed_cleanup_source_is_reused_without_new_dependencies(tmp_path):
    _, owned, request = _real_environment(tmp_path,cleanup=True,captures=1)
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

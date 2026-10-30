"""真实清理事务交付逐项拒绝，公开原键重送不新增历史或诊断。"""

import json

import pytest

from camctl.logging_runtime.diagnostics import diagnostic_scope, error_diagnostics_context
from camctl.outputs.cleanup_flow import advance_cleanup
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.models import DbOutcomeKind

from support.repository_ports import repository_port
from ..bootstrap.test_output_binding_changes import Catalog, _NOW, environment
from ..support.outputs import submit_output_document
from .test_cleanup_delete_save_recovery import _configured


@pytest.mark.asyncio
async def test_target_rejection_from_real_transaction_is_diagnosed_once(environment):
    cfg, owned, context, driver = environment
    accepted = await submit_output_document(context.database, catalog=Catalog(), occurred_at=_NOW, body={
        'request_id': '2', 'created_at': '2026-01-15 08:00:00', 'name': '错误目标',
        'actions': [{'name': '清理', 'type': 'delete_action_outputs', 'scheduled_at': '2026-01-15 09:00:00',
                     'params': {'output_ids': ['999']}}]})
    assert accepted.plan_id is not None
    action_id, = owned.connection.execute('SELECT id FROM actions WHERE plan_id=?', (accepted.plan_id,)).fetchone()

    class TargetPort(OutputsRepository):
        def __init__(self):
            self.calls = []
            self.receipt = None

        def fix_cleanup_targets(self, request, key, worker):
            self.calls.append((request, key))
            self.receipt = super().fix_cleanup_targets(request, key, worker)
            return self.receipt

    runtime = await _configured(environment, owned, driver, delete_attempts=1, query_attempts=1)
    port = TargetPort()
    lines = []
    with error_diagnostics_context(None, lines.append):
        async with diagnostic_scope():
            with repository_port(OutputsRepository, port, ("fix_cleanup_targets",),
                    matches=lambda name, args, kwargs: args[0].action_id == action_id):
                await advance_cleanup(runtime)
            result = port.receipt
            assert result.kind is DbOutcomeKind.COMPLETED, result.error
            assert len(result.value.failures) == 1
            item_id, error = result.value.failures[0]
            assert owned.connection.execute('SELECT status,error_details_json FROM cleanup_items WHERE id=?',
                (item_id,)).fetchone() == (5, '{"requested_output_id":"999"}')
            assert error.code == 'output_not_found'
            history = tuple(owned.connection.execute('SELECT * FROM history_events'))
            request, key = port.calls[0]
            replayed = OutputsRepository().fix_cleanup_targets(request, key, owned)
            assert replayed.kind is DbOutcomeKind.COMPLETED, replayed.error
            assert replayed.value.failures == result.value.failures
            with repository_port(OutputsRepository, port, ("fix_cleanup_targets",),
                    matches=lambda name, args, kwargs: args[0] == request and args[1] == key):
                await advance_cleanup(runtime)
            assert tuple(owned.connection.execute('SELECT * FROM history_events')) == history
            assert port.calls == [(request, key)]
    assert len(lines) == 1
    document = json.loads(lines[0])
    assert document['error']['details'] == {'requested_output_id': '999'}
    assert document['context']['cleanup_item_id'] == item_id

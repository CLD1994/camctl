"""真实清理事务将逐项拒绝交回诊断，原事务重送不新增错误。"""
import json
from types import SimpleNamespace

import pytest

from camctl.logging_runtime.diagnostics import diagnostic_scope, error_diagnostics_context
from camctl.outputs.cleanup_flow import FixCleanupTargets
from camctl.outputs.cleanup_ownership import PendingCleanupSave, save_cleanup_request
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.models import DbOutcomeKind
from camctl.contracts.values import new_operation_key
from support.business_database import inline_business_database
from .test_cleanup_guards import _environment, _NOW


@pytest.mark.asyncio
async def test_target_rejection_from_real_transaction_is_diagnosed_once(tmp_path):
    owned = _environment(tmp_path)
    try:
        owned.connection.execute('DELETE FROM cleanup_items')
        owned.connection.execute('UPDATE actions SET input_fields_json=? WHERE id=30',
            (json.dumps({'params': {'output_ids': ['999']}}),))
        owned.connection.commit()
        runtime = SimpleNamespace(database=inline_business_database(owned),
            outputs=OutputsRepository(), pending_saves={})
        request = FixCleanupTargets(30, _NOW)
        pending = PendingCleanupSave(owned.metadata.instance_id, 'fix_cleanup_targets', request, new_operation_key())
        runtime.pending_saves[('fix_cleanup_targets', 30)] = pending
        lines = []
        with error_diagnostics_context(None, lines.append):
            async with diagnostic_scope():
                result = await save_cleanup_request(runtime, 'fix_cleanup_targets',
                    request, action=True)
                assert result.kind is DbOutcomeKind.COMPLETED, result.error
                assert len(result.value.failures) == 1
                item_id, error = result.value.failures[0]
                assert owned.connection.execute('SELECT status,error_details_json FROM cleanup_items WHERE id=?',
                    (item_id,)).fetchone() == (5, '{"requested_output_id":"999"}')
                assert error.code == 'output_not_found'
                history = owned.connection.execute('SELECT COUNT(*) FROM history_events').fetchone()
                runtime.pending_saves[('fix_cleanup_targets', 30)] = pending
                replayed = await save_cleanup_request(runtime, 'fix_cleanup_targets', request, action=True)
                assert replayed.kind is DbOutcomeKind.COMPLETED, replayed.error
                assert replayed.value.failures == result.value.failures
                assert owned.connection.execute('SELECT COUNT(*) FROM history_events').fetchone() == history
        assert len(lines) == 1
        document = json.loads(lines[0])
        assert document['error']['details'] == {'requested_output_id': '999'}
        assert document['context']['cleanup_item_id'] == item_id
    finally:
        owned.connection.close()

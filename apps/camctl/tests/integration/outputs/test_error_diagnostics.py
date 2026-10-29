"""真实清理事务交付逐项拒绝，公开原键重送不新增历史或诊断。"""

import json

import pytest

from camctl.devices.drivers.registry import DriverRegistry
from camctl.logging_runtime.diagnostics import diagnostic_scope, error_diagnostics_context
from camctl.outputs.cleanup_flow import advance_cleanup
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.models import DbOutcomeKind

from ..support.capture import capture_database
from ..support.outputs import cleanup_factory
from .test_cleanup_guards import _environment, _NOW


@pytest.mark.asyncio
async def test_target_rejection_from_real_transaction_is_diagnosed_once(tmp_path):
    owned = _environment(tmp_path)
    try:
        owned.connection.execute('DELETE FROM cleanup_items')
        owned.connection.execute('UPDATE actions SET input_fields_json=? WHERE id=30',
            (json.dumps({'params': {'output_ids': ['999']}}),))
        owned.connection.commit()

        class TargetPort(OutputsRepository):
            calls = []
            receipt = None

            def fix_cleanup_targets(self, request, key, worker):
                self.calls.append((request, key))
                self.receipt = super().fix_cleanup_targets(request, key, worker)
                return self.receipt

        factory = cleanup_factory(devices={}, drivers=DriverRegistry(),
            max_delete_attempts=1, max_query_attempts=1, occurred_at=lambda: _NOW)
        runtime = await factory(await capture_database(owned))
        port = TargetPort()
        runtime.outputs = port
        lines = []
        with error_diagnostics_context(None, lines.append):
            async with diagnostic_scope():
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
                await advance_cleanup(runtime)
                assert tuple(owned.connection.execute('SELECT * FROM history_events')) == history
                assert port.calls == [(request, key)]
        assert len(lines) == 1
        document = json.loads(lines[0])
        assert document['error']['details'] == {'requested_output_id': '999'}
        assert document['context']['cleanup_item_id'] == item_id
    finally:
        owned.connection.close()

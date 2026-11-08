"""真实清理、文件和发布消费者交付原 ERROR，并保留原结果责任。"""

import json
import os
from pathlib import Path
from dataclasses import replace

import pytest

from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.devices.evidence import DeviceObservation
from camctl.devices.ports import DeviceCallResult
from camctl.devices.drivers.registry import DriverRegistry
from camctl.host_files.handoff import PublishResult, PublishStage
from camctl.host_files.io import DirectorySyncStage
from camctl.logging_runtime.diagnostics import diagnostic_scope, error_diagnostics_context, flush_diagnostics
from camctl.operations.models import AttemptStatus, CallInfo, CallOutcome, EffectState, ErrorValue, EvidenceValue, Settlement, SettlementBasis
from camctl.operations.validation import OutcomeValidationError
from camctl.outputs import cleanup_flow, handoff, work_files
from camctl.outputs.cleanup_flow import advance_cleanup, delete_source_file
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.outputs import OutputsRepository

from support.repository_ports import repository_port
from ..bootstrap.test_cleanup_flow import _seed_repaired_artifact
from ..bootstrap.test_output_binding_changes import environment, Catalog, _NOW, _save_photos, _registry
from ..support.database import business_database, database_path
from ..support.outputs import output_operation, prepare_cancel_target, submit_output_document
from .test_cleanup_delete_save_recovery import _prepared, _configured
from .test_read_associations import read_targets  # noqa: F401  work_env 的依赖
from .test_work_files import work_env, _cancel_delivery

pytestmark = pytest.mark.asyncio


def query_result(item_id):
    return DeviceCallResult.from_outcome(CallOutcome(status=AttemptStatus.FAILED,
        error=ErrorValue('device_error', 'query', {'exit_code': 7}), effect=EffectState.CONFIRMED,
        settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue('file_presence', 1, {})),
        observations=(DeviceObservation('file_presence', 1, {'cleanup_item_id': str(item_id), 'present': False}),),
        call_info=CallInfo(remote_exit_code=7)))


async def query_world(environment):
    owned, _, runtime, driver, item_id = await _prepared(environment)
    driver.unknown_delete = True
    result = query_result(item_id)
    calls = []
    async def query(request):
        calls.append(request)
        return result
    driver.query_file = query
    return owned, runtime, driver, item_id, result, calls


async def test_failed_query_saved_with_reliable_absence_emits_original_error_once(environment):
    owned, runtime, driver, item_id, result, calls = await query_world(environment)
    lines = []
    with error_diagnostics_context(None, lines.append):
        async with diagnostic_scope():
            assert (await delete_source_file(runtime, item_id)).phase == 'succeeded'
            assert not lines
            assert (await delete_source_file(runtime, item_id)).phase == 'already_terminal'
    assert len(lines) == len(calls) == 1
    record = json.loads(lines[0])
    assert record['error']['code'] == 'device_error'
    assert record['error']['details']['exit_code'] == 7
    assert record['ticket']['attempt_id'] == calls[0].ticket.attempt_id
    assert record['context']['cleanup_item_id'] == item_id
    assert record['outcome']['call_info']['remote_exit_code'] == 7
    row = owned.connection.execute('SELECT a.status,a.result_json FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.responsibility_key=?', (f'exists/{item_id}',)).fetchone()
    assert row[0] == int(enum_for("operation_attempts.status").FAILED)
    assert json.loads(row[1])['observations'][0]['data']['present'] is False


async def test_delete_legacy_failure_retains_error_and_observation(environment):
    owned, _, runtime, driver, item_id = await _prepared(environment)
    original = {'code': 'local_io_failed', 'stage': 'unlink', 'details': {'path': 'file', 'errno': 5}}
    calls = []
    async def delete(request):
        calls.append(request)
        return DeviceCallResult((DeviceObservation('file_absent', 1, {'cleanup_item_id': str(item_id)}),), original)
    driver.delete = delete
    lines = []
    with error_diagnostics_context(None, lines.append):
        async with diagnostic_scope():
            assert (await delete_source_file(runtime, item_id)).phase == 'succeeded'
            await delete_source_file(runtime, item_id)
    assert len(lines) == len(calls) == 1
    record = json.loads(lines[0])
    assert record['error'] == original
    assert record['details']['observations'][0]['type'] == 'file_absent'
    assert owned.connection.execute('SELECT status FROM cleanup_items WHERE id=?', (item_id,)).fetchone() == (4,)


async def test_unperformed_canceled_query_does_not_emit_error(environment):
    owned, _, runtime, driver, item_id = await _prepared(environment, before_query=True)
    action, = owned.connection.execute('SELECT action_id FROM cleanup_items WHERE id=?', (item_id,)).fetchone()
    class CancelAtIntent(OutputsRepository):
        tickets = []
        def begin_cleanup_attempt(self, request, key, worker):
            result = super().begin_cleanup_attempt(request, key, worker)
            if request.operation == 'query':
                self.tickets.append(result.value.ticket)
                prepare_cancel_target(worker, catalog=Catalog(), now=_NOW, action_id=action, request_id='9')
            return result
    port = CancelAtIntent()
    before = len(driver.queries)
    lines = []
    with repository_port(OutputsRepository, port, ("begin_cleanup_attempt",),
            matches=lambda name, args, kwargs: args[0].target.cleanup_item_id == item_id), \
            error_diagnostics_context(None, lines.append):
        async with diagnostic_scope():
            await delete_source_file(runtime, item_id)
    assert port.tickets and len(driver.queries) == before
    assert lines == []


@pytest.mark.parametrize('operation', ['query', 'delete'])
async def test_work_file_io_failure_saved_and_logged_without_repeating_io(monkeypatch, operation, work_env):
    owned, roots, prepared = work_env
    _cancel_delivery(owned, prepared.delivery_id)
    relative, = owned.connection.execute('SELECT relative_path FROM intermediate_files WHERE id=?', (prepared.target_file_id,)).fetchone()
    target = roots.staging / relative
    failure = OSError(5, '文件IO失败', str(target))
    calls = []
    stat, unlink = Path.stat, Path.unlink
    def observe(path, *args, **kwargs):
        if path == target:
            calls.append('query')
            if operation == 'query':
                raise failure
        return stat(path, *args, **kwargs)
    def remove(path, *args, **kwargs):
        if path == target:
            calls.append('delete')
            raise failure
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'stat', observe)
    monkeypatch.setattr(Path, 'unlink', remove)
    lines = []
    async with business_database(database_path(owned)) as database:
        context = work_files.WorkFileContext(OutputsRepository(), database, roots.staging, _NOW,
            work_files.WorkFileLimits(1, 1))
        with error_diagnostics_context(None, lines.append):
            async with diagnostic_scope():
                result = await work_files.clean_one_work_file(prepared.target_file_id, context)
                assert result.outcome is work_files.WorkFileSingleOutcome.FAILED
                assert not lines
                again = await work_files.clean_one_work_file(prepared.target_file_id, context)
                assert again.outcome is work_files.WorkFileSingleOutcome.FAILED
    assert calls == (['query'] if operation == 'query' else ['query', 'delete'])
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record['stage'] == f'work_file_{operation}'
    assert record['error']['type'] == 'builtins.OSError'
    assert record['error']['errno'] == 5
    assert record['context']['file_id'] == prepared.target_file_id
    assert record['details']['failure']['code'] == 'work_file_delete_failed'
    state, raw = owned.connection.execute('SELECT cleanup_state,last_error_json FROM intermediate_files WHERE id=?', (prepared.target_file_id,)).fetchone()
    assert state == 5 and json.loads(raw)['code'] == 'work_file_delete_failed'


async def test_publication_sync_failure_is_logged_once_before_recovery(monkeypatch, environment):
    from camctl.bootstrap.obtain_assembly import obtain_flow
    from ..bootstrap.test_output_binding_changes import _CONTENT
    from ..support.outputs import obtain_factory

    cfg, owned, context, driver = environment
    await _save_photos(cfg, owned, context, driver)
    source_id, = owned.connection.execute(
        "SELECT o.source_action_id FROM outputs o JOIN actions a ON a.id=o.source_action_id"
        " WHERE a.device_id='cam-a'"
    ).fetchone()
    accepted = await submit_output_document(context.database, catalog=Catalog(), occurred_at=_NOW, body={
        'request_id': '2', 'created_at': '2026-01-15 08:00:00', 'name': '取回',
        'actions': [{'name': '取回', 'type': 'obtain_action_outputs',
                     'scheduled_at': '2026-01-15 09:00:00',
                     'params': {'source': {'action_instance_id': str(source_id)}}}]})
    assert accepted.plan_id is not None, accepted
    directories = handoff.DeliveryDirectories(
        staging=Path(cfg.paths.staging), ready=Path(cfg.paths.ready),
        processing=Path(cfg.paths.processing))
    factory = obtain_factory(devices=cfg.devices, drivers=_registry(driver),
        staging=directories.staging, ready=directories.ready, processing=directories.processing,
        occurred_at=lambda: _NOW, monotonic_ns=lambda: 0, segment_size=4)
    await obtain_flow(factory)(context)
    delivery_id, status, name = owned.connection.execute(
        'SELECT id,status,file_name FROM deliveries').fetchone()
    assert status == 3
    assert len(driver.reads) == 1
    failure = PermissionError(13, '目录同步权限失败')
    calls = []
    async def publish(ref, bound, actual_directories, name):
        calls.append(ref)
        # camctl 已移动完整文件到 ready，随后目录同步失败。
        os.replace(bound.staging / ref.relative_path, actual_directories.ready / name.name)
        return PublishResult(PublishStage.MOVED, DirectorySyncStage.FAILED, True, 'directory_sync_failed', failure)
    monkeypatch.setattr(handoff, 'publish_file', publish)
    lines = []
    with error_diagnostics_context(None, lines.append):
        async with diagnostic_scope():
            with pytest.raises(handoff.DeliveryHandoffError) as caught:
                await output_operation(owned, handoff.publish_delivery, delivery_id, handoff.DeliveryContext,
                    repository=OutputsRepository(), directories=directories, occurred_at=_NOW)
            assert caught.value.stage == 'publication_sync_failed'
            assert caught.value.__cause__ is failure
            status, intent, published = owned.connection.execute(
                'SELECT status,publication_intent_event_id,published_event_id FROM deliveries WHERE id=?',
                (delivery_id,)).fetchone()
            assert status == 4 and intent is not None and published is None
            assert (directories.ready / name).read_bytes() == _CONTENT
            assert not (directories.staging / calls[0].relative_path).exists()
            result = await output_operation(owned, handoff.publish_delivery, delivery_id, handoff.DeliveryContext,
                repository=OutputsRepository(), directories=directories, occurred_at=_NOW)
    assert result.phase is handoff.DeliveryPhase.PUBLISHED
    assert len(lines) == len(calls) == 1
    assert len(driver.reads) == 1
    record = json.loads(lines[0])
    assert record['error']['type'] == 'builtins.PermissionError'
    assert record['error']['errno'] == 13
    assert record['context']['delivery_id'] == delivery_id
    status, saved_intent, published = owned.connection.execute(
        'SELECT status,publication_intent_event_id,published_event_id FROM deliveries WHERE id=?',
        (delivery_id,)).fetchone()
    assert status == 5 and saved_intent == intent and published is not None
    assert (directories.ready / name).read_bytes() == _CONTENT


async def test_member_rejection_logs_original_request_once_during_save_retry(environment):
    owned, _, runtime, driver, item_id = await _prepared(environment, delete_attempts=1, query_attempts=1)
    entries = _registry(driver)
    registry = DriverRegistry(tuple(replace(entry, declaration=replace(entry.declaration,
        delete_supported=False, file_query_supported=False)) for entry in
        (entries.entry('camctl-adb'), entries.entry('alternate-camera'))))
    runtime = await _configured(environment, owned, driver, registry=registry)
    class RejectOnce(OutputsRepository):
        calls = []
        def fail_cleanup_item(self, request, key, worker):
            self.calls.append((request, key))
            if len(self.calls) == 1:
                return DbOutcome(DbOutcomeKind.ROLLED_BACK, error=RuntimeError('save failed'))
            return super().fail_cleanup_item(request, key, worker)
    port = RejectOnce()
    lines = []
    with repository_port(OutputsRepository, port, ("fail_cleanup_item",),
            matches=lambda name, args, kwargs: args[0].item_id == item_id), \
            error_diagnostics_context(None, lines.append):
        async with diagnostic_scope():
            await delete_source_file(runtime, item_id)
            await delete_source_file(runtime, item_id)
    assert len(port.calls) == 2 and port.calls[0] == port.calls[1]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record['error']['code'] == 'file_delete_failed'
    assert record['error']['details'] == port.calls[0][0].details
    assert record['context']['cleanup_item_id'] == item_id


async def test_target_fixed_result_returns_and_logs_each_business_rejection(environment):
    cfg, owned, context, driver = environment
    await _save_photos(cfg, owned, context, driver)
    accepted = await submit_output_document(context.database, catalog=Catalog(), occurred_at=_NOW, body={
        'request_id': '2', 'created_at': '2026-01-15 08:00:00', 'name': '错误目标',
        'actions': [{'name': '清理', 'type': 'delete_action_outputs', 'scheduled_at': '2026-01-15 09:00:00',
                     'params': {'output_ids': ['999001']}}]})
    runtime = await _configured(environment, owned, driver)
    lines = []
    with error_diagnostics_context(None, lines.append):
        async with diagnostic_scope():
            await advance_cleanup(runtime)
            await advance_cleanup(runtime)
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record['error']['code'] == 'output_not_found'
    item, code, raw = owned.connection.execute('SELECT id,error_code,error_details_json FROM cleanup_items').fetchone()
    assert code is not None
    assert record['context']['cleanup_item_id'] == item
    assert record['error']['details'] == json.loads(raw) == {'requested_output_id': '999001'}
    assert not driver.deletes and not driver.queries


async def test_query_target_exception_graph_is_preserved(environment):
    owned, _, runtime, driver, item_id = await _prepared(environment, before_query=True)
    cfg = environment[0]
    output, = owned.connection.execute('SELECT output_id FROM cleanup_items WHERE id=?', (item_id,)).fetchone()
    _seed_repaired_artifact(Path(cfg.paths.state_db), cfg, output)
    runtime = await _configured(environment, owned, driver)
    class CorruptAfterIntent(OutputsRepository):
        tickets = []
        def begin_cleanup_attempt(self, request, key, worker):
            result = super().begin_cleanup_attempt(request, key, worker)
            if request.operation == 'query':
                self.tickets.append(result.value.ticket)
                worker.connection.execute("UPDATE intermediate_files SET relative_path='../900.jpg' WHERE id=900")
                worker.connection.commit()
            return result
    port = CorruptAfterIntent()
    lines = []
    with repository_port(OutputsRepository, port, ("begin_cleanup_attempt",),
            matches=lambda name, args, kwargs: args[0].target.cleanup_item_id == item_id), \
            error_diagnostics_context(None, lines.append):
        async with diagnostic_scope():
            await delete_source_file(runtime, item_id)
    assert len(lines) == 1
    error = json.loads(lines[0])['run_error']
    assert error['type'].endswith('.LocalArtifactTargetError')
    assert error['cause'] is not None
    assert '900.jpg' in error['cause']['message']


async def test_restored_saved_query_does_not_generate_old_error_again(environment):
    owned, runtime, driver, item_id, result, calls = await query_world(environment)
    class RejectMember(OutputsRepository):
        enabled = True
        def finish_cleanup_item(self, request, key, worker):
            if self.enabled:
                return DbOutcome(DbOutcomeKind.ROLLED_BACK, error=OSError('member save failed'))
            return super().finish_cleanup_item(request, key, worker)
    port = RejectMember()
    first, restored = [], []
    with repository_port(OutputsRepository, port, ("finish_cleanup_item",),
            matches=lambda name, args, kwargs: args[0].item_id == item_id), \
            error_diagnostics_context(None, first.append):
        async with diagnostic_scope():
            await delete_source_file(runtime, item_id)
    assert len(first) == len(calls) == 1
    assert owned.connection.execute('SELECT a.status FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.responsibility_key=?', (f'exists/{item_id}',)).fetchone() == (3,)
    port.enabled = False
    resumed = await _configured(environment, owned, driver)
    with error_diagnostics_context(None, restored.append):
        async with diagnostic_scope():
            assert (await delete_source_file(resumed, item_id)).phase in ('succeeded', 'already_terminal')
    assert restored == [] and len(calls) == 1


async def test_query_validation_failure_retains_original_exception(monkeypatch, environment):
    owned, runtime, driver, item_id, result, calls = await query_world(environment)
    async def successful_query(request):
        calls.append(request)
        return DeviceCallResult((DeviceObservation('file_presence', 1,
            {'cleanup_item_id': str(item_id), 'present': False}),), None)
    driver.query_file = successful_query
    failure = TypeError('driver returned an invalid value')
    from camctl.operations import validation as validation_module
    validate = validation_module.validate_outcome
    def validation(ticket, outcome, evidence):
        if ticket.operation == 'query':
            raise failure
        return validate(ticket, outcome, evidence)
    monkeypatch.setattr(validation_module, 'validate_outcome', validation)
    lines = []
    with error_diagnostics_context(None, lines.append):
        with pytest.raises(OutcomeValidationError):
            async with diagnostic_scope():
                await delete_source_file(runtime, item_id)
        assert not lines
        await flush_diagnostics()
    assert len(lines) == len(calls) == 1
    record = json.loads(lines[0])
    assert record['error']['type'] == 'builtins.TypeError'
    assert record['error']['message'] == str(failure)
    assert record['details']['ticket']['attempt_id'] == calls[0].ticket.attempt_id
    assert owned.connection.execute('SELECT a.status,a.result_json FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.responsibility_key=?', (f'exists/{item_id}',)).fetchone() == (1, None)

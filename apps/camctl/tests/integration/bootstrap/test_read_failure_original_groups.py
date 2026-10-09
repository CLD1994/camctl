"""普通读取失败原键以合法原 H 的完整交付、源依赖和 slot 组恢复。"""

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio

from camctl.contracts.values import new_operation_key
from camctl.devices.bindings import DeviceBinding, check_binding
from camctl.outputs.qualification import FileCandidate, OperationConfig
from camctl.outputs.slots import SlotRequest
from camctl.outputs.sources import ResolutionState, SelectionMode, SourceResolution, SourceSpec, select_outputs
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.outputs import (
    FailReadBinding, FixSelection, OutputsRepository, ResolveSources, StartObtainAction, load_selection_facts,
)
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..cancellation.test_report_sync_lifecycle import _fault_owned
from .test_output_binding_changes import _NOW, _accept, _changed, _save_photos, environment
from .test_output_binding_transactions import pending_read
from .test_read_exhaustion_transactions import _exhausted

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def prepared_without_attempt(environment):
    cfg, owned, context, driver = environment
    await _save_photos(cfg, owned, context, driver)
    _accept(owned, "2", [{"name": "取回", "type": "obtain_action_outputs",
        "scheduled_at": "2026-01-15 09:00:00", "params": {"source": {"plan_instance_id": "1", "group": "files"}}}])
    repository = OutputsRepository()
    action_id = owned.connection.execute("SELECT id FROM actions WHERE type=4").fetchone()[0]
    started = repository.start_obtain_action(StartObtainAction(action_id, _NOW), new_operation_key(), owned)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    resolved = repository.resolve_sources(ResolveSources(action_id,
        SourceSpec(plan_instance_id=1, group="files"), _NOW), new_operation_key(), owned)
    assert resolved.kind is DbOutcomeKind.COMPLETED and resolved.value.fixed, resolved.error
    for selection_id, source_action in owned.connection.execute(
            "SELECT s.id,d.depends_on_action_id FROM obtain_source_selections s"
            " JOIN action_dependencies d ON d.id=s.dependency_id WHERE d.action_id=?", (action_id,)).fetchall():
        snapshot = select_outputs(SourceResolution(ResolutionState.FIXED, (source_action,), 1),
            load_selection_facts(owned.connection, source_action), SelectionMode.DEFAULT, ())
        fixed = repository.fix_selection(FixSelection(selection_id, snapshot, _NOW), new_operation_key(), owned)
        assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
    item_id, output_id, source_id = owned.connection.execute(
        "SELECT i.id,o.id,o.device_file_id FROM obtain_items i JOIN outputs o ON o.id=i.output_id"
        " JOIN device_files f ON f.id=o.device_file_id JOIN actions a ON a.id=f.observer_action_id"
        " WHERE a.device_id='cam-a'").fetchone()
    granted = repository.grant_file(FileCandidate(action_id, item_id, None, output_id, source_id,
        "part", "jpg", "原图.jpg", OperationConfig(3, Decimal('10'), Decimal('0')), _NOW),
        new_operation_key(), owned)
    assert granted.kind is DbOutcomeKind.COMPLETED, granted.error
    copy_id = granted.value.copy_id
    assert owned.connection.execute("SELECT status,attempts_used,retry_wait_required FROM operation_runs WHERE copy_id=?",
        (copy_id,)).fetchone() == (1, 0, 0)
    command = FailReadBinding(copy_id, source_id,
        check_binding(DeviceBinding("cam-a", "camctl-adb"), _changed(cfg, "missing")), _NOW)
    return cfg, owned, command


@pytest.mark.parametrize("held_slot", [False, True])
@pytest.mark.parametrize("after_commit", [False, True])
async def test_zero_attempt_binding_original_key_preserves_noop_retry_wait_and_original_slot(
        prepared_without_attempt, held_slot, after_commit):
    cfg, owned, command = prepared_without_attempt
    repository, key = OutputsRepository(), new_operation_key()
    if held_slot:
        granted = repository.grant_read_slot(SlotRequest(command.copy_id, _NOW), new_operation_key(), owned)
        assert granted.kind is DbOutcomeKind.COMPLETED, granted.error
        assert owned.connection.execute("SELECT slot_device_id FROM file_copies WHERE id=?",
            (command.copy_id,)).fetchone() == ('cam-a',)
    faulty = _fault_owned(owned, "COMMIT", after_commit=after_commit)
    uncertain = repository.fail_read_binding(command, key, faulty)
    assert faulty.connection.failed and uncertain.kind is DbOutcomeKind.UNKNOWN
    owned.connection.close()
    reopened = open_existing(Path(cfg.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig())
    try:
        saved = repository.fail_read_binding(command, key, reopened)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        assert reopened.connection.execute("SELECT status,attempts_used,retry_wait_required FROM operation_runs WHERE copy_id=?",
            (command.copy_id,)).fetchone() == (4, 0, 0)
        assert reopened.connection.execute("SELECT COUNT(*) FROM operation_attempts WHERE run_id=(SELECT id FROM operation_runs WHERE copy_id=?)",
            (command.copy_id,)).fetchone() == (0,)
        assert reopened.connection.execute("SELECT slot_device_id FROM file_copies WHERE id=?",
            (command.copy_id,)).fetchone() == (None,)
        group = reopened.connection.execute(
            "SELECT event_type,body_json FROM history_events WHERE transaction_id=(SELECT id FROM history_transactions WHERE operation_key=?) ORDER BY id",
            (str(key),)).fetchall()
        assert [kind for kind, _body in group] == [23, 10, 21] + ([22] if held_slot else [])
        import json
        assert 'retry_wait_required' not in json.loads(group[1][1])['rows'][0]['after']['values']
        before = tuple(reopened.connection.iterdump())
        assert repository.fail_read_binding(command, key, reopened).kind is DbOutcomeKind.COMPLETED
        assert tuple(reopened.connection.iterdump()) == before
        for changed in (replace(command, copy_id=command.copy_id + 1),
                        replace(command, source_device_file_id=command.source_device_file_id + 1),
                        replace(command, occurred_at=command.occurred_at + 1),
                        replace(command, binding_result=check_binding(DeviceBinding("cam-a", "camctl-adb"),
                            _changed(cfg, "mismatch")))):
            result = repository.fail_read_binding(changed, key, reopened)
            assert result.kind is DbOutcomeKind.ROLLED_BACK
            assert tuple(reopened.connection.iterdump()) == before
    finally:
        reopened.connection.close()


@pytest.mark.parametrize("held_slot", [False, True])
@pytest.mark.parametrize("after_commit", [False, True])
async def test_exhausted_delivery_original_key_keeps_original_source_release_and_slot_group(
        pending_read, held_slot, after_commit):
    cfg, owned, binding = pending_read
    command = _exhausted(owned, binding.copy_id)
    repository, key = OutputsRepository(), new_operation_key()
    item_id = owned.connection.execute("SELECT id FROM obtain_items WHERE delivery_id=?", (command.delivery_id,)).fetchone()[0]
    if not held_slot:
        released = repository.release_read_slot(SlotRequest(binding.copy_id, _NOW), new_operation_key(), owned)
        assert released.kind is DbOutcomeKind.COMPLETED, released.error
    assert owned.connection.execute("SELECT slot_device_id FROM file_copies WHERE id=?", (binding.copy_id,)).fetchone() == ("cam-a" if held_slot else None,)
    faulty = _fault_owned(owned, "COMMIT", after_commit=after_commit)
    uncertain = repository.fail_read_delivery(command, key, faulty)
    assert faulty.connection.failed and uncertain.kind is DbOutcomeKind.UNKNOWN
    owned.connection.close()
    reopened = open_existing(Path(cfg.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig())
    try:
        saved = repository.fail_read_delivery(command, key, reopened)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        import json
        group = [(kind, json.loads(body)) for kind, body in reopened.connection.execute(
            "SELECT event_type,body_json FROM history_events WHERE transaction_id=(SELECT id FROM history_transactions WHERE operation_key=?) ORDER BY id",
            (str(key),)).fetchall()]
        assert [kind for kind, _body in group] == [23, 21] + ([22] if held_slot else [])
        assert group[1][1]['rows'] == [{'table': 'obtain_items', 'id': item_id,
            'before': {'exists': True, 'values': {'source_dependency': 1}},
            'after': {'exists': True, 'values': {'source_dependency': 0}}}]
        if held_slot:
            assert group[2][1]['rows'] == [{'table': 'file_copies', 'id': binding.copy_id,
                'before': {'exists': True, 'values': {'slot_device_id': 'cam-a'}},
                'after': {'exists': True, 'values': {'slot_device_id': None}}}]
        before = tuple(reopened.connection.iterdump())
        for changed in (replace(command, delivery_id=command.delivery_id + 1),
                        replace(command, occurred_at=command.occurred_at + 1), replace(command, checksum_mismatch=True)):
            result = repository.fail_read_delivery(changed, key, reopened)
            assert result.kind is DbOutcomeKind.ROLLED_BACK
            assert tuple(reopened.connection.iterdump()) == before
    finally:
        reopened.connection.close()


@pytest.mark.parametrize('after_commit', [False, True])
async def test_actual_obtain_binding_child_unknown_reopens_same_factory_with_original_request_and_key(pending_read, monkeypatch, after_commit):
    from camctl.bootstrap.obtain_assembly import session_obtain_assembly
    from camctl.capture.recovery import RecoveryBoundary
    from camctl.contracts.values import ConsistencyError
    from camctl.devices.drivers.registry import DriverRegistry
    from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
    from camctl.outputs.obtain_flow import advance_obtain
    from .test_output_binding_changes import _registry
    from .test_output_read_recovery import _start_unended_read
    from .test_read_execution_runtime import ReadRequests, _CONTENT

    cfg, owned, binding = pending_read
    ticket = _start_unended_read(owned, binding.copy_id)
    horizon = owned.connection.execute('SELECT MAX(id) FROM history_events').fetchone()[0]
    reader = ReadRequests(owned, _CONTENT)
    entry = _registry(reader).entry('camctl-adb')
    entry = replace(entry, declaration=replace(entry.declaration,
        adb_foreground_recovery_operations=frozenset({'read'})), evidence=EvidenceRegistry((
            EvidenceContract('adb_foreground_recovery', 1, 'read', frozenset()),)))
    factory = session_obtain_assembly(devices=_changed(cfg, 'missing').devices, drivers=DriverRegistry((entry,)),
        staging=Path(cfg.paths.staging), ready=Path(cfg.paths.ready), processing=Path(cfg.paths.processing), segment_size=4,
        occurred_at=lambda: _NOW, monotonic_ns=lambda: 0, recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED,
        recovery_max_event_id=lambda: horizon)
    original = OutputsRepository.fail_read_binding
    saves = []

    def uncertain_once(repository, request, key, connection):
        saves.append((request, key))
        if len(saves) == 1:
            faulty = _fault_owned(connection, 'COMMIT', after_commit=after_commit)
            saved = original(repository, request, key, faulty)
            assert faulty.connection.failed and saved.kind is DbOutcomeKind.UNKNOWN
            return saved
        return original(repository, request, key, connection)

    monkeypatch.setattr(OutputsRepository, 'fail_read_binding', uncertain_once)
    with pytest.raises(ConsistencyError):
        await advance_obtain(factory(owned))
    owned.connection.close()
    reopened = open_existing(Path(cfg.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig())
    try:
        await advance_obtain(factory(reopened))
        assert len(saves) == 2 and saves[0] == saves[1]
        assert reopened.connection.execute('SELECT status,attempts_used FROM operation_runs WHERE id=?',
            (ticket.run_id,)).fetchone() == (4, ticket.attempt_id)
        assert reopened.connection.execute('SELECT source_dependency FROM obtain_items WHERE delivery_id=(SELECT delivery_id FROM file_copies WHERE id=?)',
            (binding.copy_id,)).fetchone() == (0,)
        assert reopened.connection.execute('SELECT slot_device_id FROM file_copies WHERE id=?', (binding.copy_id,)).fetchone() == (None,)
        assert reader.requests == []
    finally:
        reopened.connection.close()

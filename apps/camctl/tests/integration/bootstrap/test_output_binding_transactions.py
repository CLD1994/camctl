"""原设备读取绑定失败的完整事务与原操作键恢复。"""

from dataclasses import replace
from pathlib import Path

import pytest
import pytest_asyncio

from camctl.bootstrap.obtain_assembly import obtain_flow, session_obtain_assembly
from camctl.contracts.values import new_operation_key
from camctl.devices.bindings import DeviceBinding, check_binding
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.outputs import FailCleanupBinding, FailReadBinding, OutputsRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..cancellation.test_report_sync_lifecycle import _fault_owned
from .test_output_binding_changes import (
    _NOW, _accept, _changed, _pending_cleanup, _registry, _save_photos, environment,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def pending_read(environment):
    cfg, owned, context, driver = environment
    await _save_photos(cfg, owned, context, driver)
    _accept(owned, "2", [{"name": "取回", "type": "obtain_action_outputs",
                          "scheduled_at": "2026-01-15 09:00:00",
                          "params": {"source": {"plan_instance_id": "1", "group": "files"}}}])
    driver.fail_a = True
    factory = session_obtain_assembly(
        devices=cfg.devices, drivers=_registry(driver), staging=Path(cfg.paths.staging),
        ready=Path(cfg.paths.ready), processing=Path(cfg.paths.processing), segment_size=4,
        occurred_at=lambda: _NOW, monotonic_ns=lambda: 0)
    await obtain_flow(factory)(context)
    copy_id, source_id = owned.connection.execute(
        "SELECT c.id,c.source_device_file_id FROM file_copies c"
        " JOIN device_files f ON f.id=c.source_device_file_id"
        " JOIN actions a ON a.id=f.observer_action_id WHERE a.device_id='cam-a'").fetchone()
    command = FailReadBinding(copy_id, source_id, check_binding(
        DeviceBinding("cam-a", "camctl-adb"), _changed(cfg, "missing")), _NOW)
    return cfg, owned, command


@pytest.mark.parametrize("prefix", [
    "UPDATE deliveries", "UPDATE operation_runs", "UPDATE obtain_items", "UPDATE file_copies",
])
async def test_read_binding_projection_failure_rolls_back_complete_group(pending_read, prefix):
    _cfg, owned, command = pending_read
    before = tuple(owned.connection.iterdump())

    result = OutputsRepository().fail_read_binding(command, new_operation_key(), _fault_owned(owned, prefix))

    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("after_commit", [False, True])
async def test_read_binding_unknown_commit_reopens_and_verifies_original_key(pending_read, after_commit):
    cfg, owned, command = pending_read
    before = tuple(owned.connection.iterdump())
    key = new_operation_key()
    repository = OutputsRepository()

    uncertain = repository.fail_read_binding(command, key, _fault_owned(owned, "COMMIT", after_commit=after_commit))

    assert uncertain.kind is DbOutcomeKind.UNKNOWN
    owned.connection.close()
    recovered = open_existing(Path(cfg.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig())
    try:
        if not after_commit:
            assert tuple(recovered.connection.iterdump()) == before
        retry = repository.fail_read_binding(command, key, recovered)
        assert retry.kind is DbOutcomeKind.COMPLETED, retry.error
        group = recovered.connection.execute(
            "SELECT e.id,e.event_type FROM history_events e JOIN history_transactions t"
            " ON t.id=e.transaction_id WHERE t.operation_key=? ORDER BY e.id", (str(key),)).fetchall()
        assert len(group) == 4
        assert [identity for identity, _kind in group] == list(range(group[0][0], group[0][0] + 4))
        assert [kind for _identity, kind in group] == [23, 10, 21, 22]
        assert recovered.connection.execute(
            "SELECT committed_bytes,slot_device_id FROM file_copies WHERE id=?", (command.copy_id,)).fetchone() == (4, None)
        committed = tuple(recovered.connection.iterdump())
        assert repository.fail_read_binding(command, key, recovered).kind is DbOutcomeKind.COMPLETED
        assert tuple(recovered.connection.iterdump()) == committed
    finally:
        recovered.connection.close()


@pytest.mark.parametrize("field", ["copy_id", "source_device_file_id", "binding_result", "occurred_at"])
async def test_read_binding_original_key_rejects_changed_effective_input(pending_read, field):
    cfg, owned, command = pending_read
    repository, key = OutputsRepository(), new_operation_key()
    assert repository.fail_read_binding(command, key, owned).kind is DbOutcomeKind.COMPLETED
    changed = {
        "copy_id": command.copy_id + 1,
        "source_device_file_id": command.source_device_file_id + 1,
        "binding_result": check_binding(DeviceBinding("cam-a", "camctl-adb"), _changed(cfg, "mismatch")),
        "occurred_at": command.occurred_at + 1,
    }[field]
    before = tuple(owned.connection.iterdump())

    result = repository.fail_read_binding(replace(command, **{field: changed}), key, owned)

    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert tuple(owned.connection.iterdump()) == before


@pytest_asyncio.fixture
async def pending_cleanup(environment):
    cfg, owned, _context, _driver = environment
    output_id = await _pending_cleanup(environment)
    item_id, source_id = owned.connection.execute(
        "SELECT c.id,o.device_file_id FROM cleanup_items c JOIN outputs o ON o.id=c.output_id"
        " WHERE c.output_id=?", (output_id,)).fetchone()
    keys = tuple(row[0] for row in owned.connection.execute(
        "SELECT responsibility_key FROM operation_runs WHERE cleanup_item_id=? ORDER BY id", (item_id,)))
    command = FailCleanupBinding(item_id, source_id, check_binding(
        DeviceBinding("cam-a", "camctl-adb"), _changed(cfg, "missing")), keys, _NOW)
    return cfg, owned, command


@pytest.mark.parametrize("prefix", ["UPDATE cleanup_items", "UPDATE outputs", "UPDATE operation_runs"])
async def test_cleanup_binding_projection_failure_rolls_back_complete_group(pending_cleanup, prefix):
    _cfg, owned, command = pending_cleanup
    before = tuple(owned.connection.iterdump())

    result = OutputsRepository().fail_cleanup_binding(command, new_operation_key(), _fault_owned(owned, prefix))

    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("after_commit", [False, True])
async def test_cleanup_binding_unknown_commit_reopens_and_verifies_complete_group(pending_cleanup, after_commit):
    cfg, owned, command = pending_cleanup
    before = tuple(owned.connection.iterdump())
    attempts = tuple(owned.connection.execute(
        "SELECT * FROM operation_attempts WHERE run_id IN"
        " (SELECT id FROM operation_runs WHERE cleanup_item_id=?) ORDER BY id", (command.item_id,)))
    repository, key = OutputsRepository(), new_operation_key()

    uncertain = repository.fail_cleanup_binding(command, key, _fault_owned(owned, "COMMIT", after_commit=after_commit))

    assert uncertain.kind is DbOutcomeKind.UNKNOWN
    owned.connection.close()
    recovered = open_existing(Path(cfg.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig())
    try:
        if not after_commit:
            assert tuple(recovered.connection.iterdump()) == before
        retried = repository.fail_cleanup_binding(command, key, recovered)
        assert retried.kind is DbOutcomeKind.COMPLETED, retried.error
        group = recovered.connection.execute(
            "SELECT e.id,e.event_type FROM history_events e JOIN history_transactions t"
            " ON t.id=e.transaction_id WHERE t.operation_key=? ORDER BY e.id", (str(key),)).fetchall()
        assert [kind for _identity, kind in group] == [24, 20, 10, 10]
        assert [identity for identity, _kind in group] == list(range(group[0][0], group[0][0] + 4))
        assert tuple(recovered.connection.execute(
            "SELECT * FROM operation_attempts WHERE run_id IN"
            " (SELECT id FROM operation_runs WHERE cleanup_item_id=?) ORDER BY id", (command.item_id,))) == attempts
        assert recovered.connection.execute(
            "SELECT status,attempts_used,retry_wait_required FROM operation_runs"
            " WHERE cleanup_item_id=? ORDER BY id", (command.item_id,)).fetchall() == [(4, 1, 0), (4, 1, 0)]
        committed = tuple(recovered.connection.iterdump())
        assert repository.fail_cleanup_binding(command, key, recovered).kind is DbOutcomeKind.COMPLETED
        assert tuple(recovered.connection.iterdump()) == committed
    finally:
        recovered.connection.close()


@pytest.mark.parametrize("field", [
    "item_id", "source_device_file_id", "binding_result", "responsibility_keys", "occurred_at", "canceled",
])
async def test_cleanup_binding_original_key_rejects_changed_effective_input(pending_cleanup, field):
    cfg, owned, command = pending_cleanup
    repository, key = OutputsRepository(), new_operation_key()
    saved = repository.fail_cleanup_binding(command, key, owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    changed = {
        "item_id": command.item_id + 1,
        "source_device_file_id": command.source_device_file_id + 1,
        "binding_result": check_binding(DeviceBinding("cam-a", "camctl-adb"), _changed(cfg, "mismatch")),
        "responsibility_keys": command.responsibility_keys[:1],
        "occurred_at": command.occurred_at + 1,
        "canceled": True,
    }[field]
    before = tuple(owned.connection.iterdump())

    changed_command = (replace(command, item_id=changed, responsibility_keys=(f"delete/{changed}", f"exists/{changed}"))
                       if field == "item_id" else replace(command, **{field: changed}))
    result = repository.fail_cleanup_binding(changed_command, key, owned)

    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert tuple(owned.connection.iterdump()) == before

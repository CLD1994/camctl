"""读取耗尽的交付失败、源依赖与机会必须原子收场。"""

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.contracts.values import new_operation_key
from camctl.operations.attempts import AttemptConfig, AttemptIntent, AttemptTarget, OperationKind
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.outputs import FailReadDelivery, OutputsRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..cancellation.test_report_sync_lifecycle import _fault_owned
from .test_output_binding_changes import _NOW, environment
from .test_output_binding_transactions import pending_read

pytestmark = pytest.mark.asyncio


def _exhausted(owned, copy_id):
    action_id, copy_round, delivery_id = owned.connection.execute(
        "SELECT r.action_id,c.round,c.delivery_id FROM operation_runs r"
        " JOIN file_copies c ON c.id=r.copy_id WHERE c.id=?", (copy_id,)).fetchone()
    result = OperationRepository().begin_attempt(AttemptIntent(
        "read", action_id, OperationKind.READ_FILE, AttemptTarget(copy_id=copy_id), None,
        AttemptConfig(1, Decimal("1.25"), Decimal("0")), _NOW, copy_round=copy_round),
        new_operation_key(), owned)
    assert result.kind is DbOutcomeKind.COMPLETED, result.error
    assert result.value.ticket is None
    assert owned.connection.execute(
        "SELECT status FROM operation_runs WHERE copy_id=?", (copy_id,)).fetchone() == (4,)
    return FailReadDelivery(delivery_id, _NOW)


@pytest.mark.parametrize("prefix", ["UPDATE deliveries", "UPDATE obtain_items", "UPDATE file_copies"])
async def test_exhaustion_projection_failure_keeps_delivery_source_and_slot(pending_read, prefix):
    _cfg, owned, original = pending_read
    command = _exhausted(owned, original.copy_id)
    before = tuple(owned.connection.iterdump())

    faulty = _fault_owned(owned, prefix)
    failed = OutputsRepository().fail_read_delivery(command, new_operation_key(), faulty)

    assert faulty.connection.failed
    assert failed.kind is DbOutcomeKind.ROLLED_BACK
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("after_commit", [False, True])
async def test_exhaustion_unknown_commit_reopens_and_verifies_complete_original_group(pending_read, after_commit):
    cfg, owned, original = pending_read
    command = _exhausted(owned, original.copy_id)
    key, repository = new_operation_key(), OutputsRepository()
    faulty = _fault_owned(owned, "COMMIT", after_commit=after_commit)
    uncertain = repository.fail_read_delivery(command, key, faulty)
    assert faulty.connection.failed
    assert uncertain.kind is DbOutcomeKind.UNKNOWN
    owned.connection.close()
    recovered = open_existing(Path(cfg.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig())
    try:
        saved = repository.fail_read_delivery(command, key, recovered)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        assert recovered.connection.execute(
            "SELECT status,json_extract(error_json,'$.code') FROM deliveries WHERE id=?",
            (command.delivery_id,)).fetchone() == (6, "read_attempts_exhausted")
        assert recovered.connection.execute(
            "SELECT source_dependency FROM obtain_items WHERE delivery_id=?",
            (command.delivery_id,)).fetchone() == (0,)
        assert recovered.connection.execute(
            "SELECT slot_device_id FROM file_copies WHERE id=?", (original.copy_id,)).fetchone() == (None,)
        group = recovered.connection.execute(
            "SELECT e.event_type FROM history_events e JOIN history_transactions t"
            " ON t.id=e.transaction_id WHERE t.operation_key=? ORDER BY e.id", (str(key),)).fetchall()
        assert group == [(23,), (21,), (22,)]
        before = tuple(recovered.connection.iterdump())
        assert repository.fail_read_delivery(command, key, recovered).kind is DbOutcomeKind.COMPLETED
        assert tuple(recovered.connection.iterdump()) == before
        for changed in (replace(command, delivery_id=command.delivery_id + 1),
                        replace(command, occurred_at=command.occurred_at + 1)):
            assert repository.fail_read_delivery(changed, key, recovered).kind is DbOutcomeKind.ROLLED_BACK
            assert tuple(recovered.connection.iterdump()) == before
    finally:
        recovered.connection.close()

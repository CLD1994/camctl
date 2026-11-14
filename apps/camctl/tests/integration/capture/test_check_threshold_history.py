"""检查门槛在真实受理、事件和历史恢复中的一致性。"""
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.capture.processing import (
    CheckBasis, CheckDecisionChoice, CheckDecisionSave, CheckPhase, CheckReason,
    CheckResultSave, MediaObservation,
)
from camctl.contracts.values import new_operation_key
from camctl.history.snapshots import SnapshotRef, prepare_snapshot
from camctl.persistence.executor import DbExecutor
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.history import HistoryRepository, SqliteSnapshotStore
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from .media_retry_fixtures import media_pipeline  # noqa: F401
from .test_input_copy import _NOW


@pytest.mark.asyncio
async def test_check_threshold_restores_from_snapshot_and_current(media_pipeline):
    owned, _, _ = media_pipeline
    path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    history = HistoryRepository(path)
    before = history.current_boundary()
    executor = DbExecutor(lambda: open_existing(path, DbOpenMode.EXISTING_RW, DbConfig()),
                          capacity=4, enqueue_timeout_seconds=9)
    try:
        store = SqliteSnapshotStore(path, executor)
        seed = (await store.load_entity_images((SnapshotRef(1, 1),)))[0]
        await store.save_snapshots((prepare_snapshot(seed),))
        repository = CaptureRepository()
        basis = CheckBasis(CheckReason.INSUFFICIENT_TIMING, 60000, threshold_s=Decimal("72.5"))
        result = repository.save_check_decision(CheckDecisionSave(
            1, CheckDecisionChoice.REQUIRED, basis, _NOW + 1), new_operation_key(), owned)
        assert result.kind is DbOutcomeKind.COMPLETED, result.error
        after = history.current_boundary()
        for phase in (CheckPhase.RUNNING, CheckPhase.COMPLETED):
            observation = MediaObservation(phase, duration_s=Decimal("71") if phase is CheckPhase.COMPLETED else None)
            result = repository.save_check_result(CheckResultSave(1, observation, occurred_at=_NOW + 2),
                                                  new_operation_key(), owned)
            assert result.kind is DbOutcomeKind.COMPLETED, result.error
        previous = history.restore_entity("action", 1, before)
        restored = history.restore_entity("action", 1, after)
        current = history.restore_entity("action", 1, history.current_boundary())
        assert "threshold_s" not in previous[("recording_processing", 1)]["check_basis_json"]
        assert restored[("recording_processing", 1)]["check_basis_json"]["threshold_s"] == Decimal("72.5")
        assert current[("recording_processing", 1)]["check_basis_json"]["threshold_s"] == Decimal("72.5")
    finally:
        await executor.close()

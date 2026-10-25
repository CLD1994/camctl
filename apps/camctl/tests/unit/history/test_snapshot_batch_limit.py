"""快照维护的显式批次数界，不把让出误报为清空。"""

from unittest.mock import create_autospec

import pytest

from camctl.contracts.history_values import HistoryBoundary
from camctl.history.snapshots import (
    EntityImage, MaintenanceContext, MaintenanceOutcome, MaintenanceState,
    SavedProgress, SnapshotMaintenanceError, SnapshotRef, SnapshotRow,
    SnapshotStore, maintain_snapshots,
)


def _context():
    store = create_autospec(SnapshotStore, instance=True, spec_set=True)
    ref = SnapshotRef(1, 8)
    store.snapshot_candidates.return_value = (ref,)
    store.load_entity_images.return_value = (EntityImage(
        ref.entity_type, ref.entity_id, HistoryBoundary(3, 30), 5,
        (SnapshotRow("actions", 8, {"id": 8}),)),)
    store.save_snapshots.return_value = (SavedProgress(ref, 0),)
    return MaintenanceContext(64, 1, MaintenanceState(), store)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 2])
async def test_explicit_batch_limit_yields_with_only_bounded_results(limit):
    context = _context()
    result = await maintain_snapshots(context, max_batches=limit)
    assert result.outcome is MaintenanceOutcome.YIELDED
    assert result.batches == limit
    assert len(result.saved) == limit
    assert context.store.snapshot_candidates.await_count == limit


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [0, -1, True, 1.5, "1"])
async def test_invalid_batch_limit_fails_before_any_store_work(invalid):
    context = _context()
    with pytest.raises(SnapshotMaintenanceError):
        await maintain_snapshots(context, max_batches=invalid)
    context.store.snapshot_candidates.assert_not_awaited()


@pytest.mark.asyncio
async def test_short_candidate_batch_is_drained_within_explicit_limit():
    context = _context()
    context = MaintenanceContext(64, 16, context.state, context.store)
    result = await maintain_snapshots(context, max_batches=1)
    assert result.outcome is MaintenanceOutcome.DRAINED
    assert result.batches == 1


@pytest.mark.asyncio
async def test_default_limit_continues_until_actual_empty_candidates():
    context = _context()
    context.store.snapshot_candidates.side_effect = [(SnapshotRef(1, 8),), ()]
    result = await maintain_snapshots(context)
    assert result.outcome is MaintenanceOutcome.IDLE
    assert result.batches == 1

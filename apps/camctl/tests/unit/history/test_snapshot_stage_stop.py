"""快照维护在阶段交接时停止，并交付已取得资源及实际错误。"""

from __future__ import annotations

from unittest.mock import create_autospec

import pytest

from camctl.history import snapshots
from camctl.history.bounded import SegmentHistoryHandle
from camctl.history.snapshot_stream import PreparedSnapshotSource
from camctl.history.snapshots import (
    MaintenanceContext,
    MaintenanceOutcome,
    MaintenanceState,
    SavedProgress,
    SnapshotMaintenanceError,
    SnapshotRef,
    SnapshotStore,
    maintain_snapshots,
)


_REF = SnapshotRef(entity_type=1, entity_id=8)


def _store():
    store = create_autospec(SnapshotStore, instance=True, spec_set=True)
    store.snapshot_candidates.return_value = (_REF,)
    store.load_entity_images.return_value = ()
    store.save_snapshots.return_value = ()
    return store


def _context(store, state, *, batch_size=16):
    return MaintenanceContext(
        threshold=64, batch_size=batch_size, state=state, store=store)


def _image():
    return create_autospec(SegmentHistoryHandle, instance=True, spec_set=True)


def _prepared():
    return create_autospec(PreparedSnapshotSource, instance=True, spec_set=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("candidates", [(), (_REF,)])
async def test_closing_after_candidates_does_not_start_image_read(
        mocker, candidates):
    state = MaintenanceState()
    store = _store()
    prepare = mocker.patch.object(snapshots, "_prepare_images", autospec=True)

    async def candidates_then_close(threshold, limit):
        state.close()
        return candidates

    store.snapshot_candidates.side_effect = candidates_then_close

    result = await maintain_snapshots(_context(store, state))

    assert result.outcome is MaintenanceOutcome.CLOSING
    assert result.saved == ()
    assert result.batches == 0
    store.load_entity_images.assert_not_awaited()
    prepare.assert_not_awaited()
    store.save_snapshots.assert_not_awaited()
    assert not state.disabled


@pytest.mark.asyncio
async def test_closing_after_images_releases_every_image_without_preparing(mocker):
    state = MaintenanceState()
    store = _store()
    images = (_image(), _image())
    prepare = mocker.patch.object(snapshots, "_prepare_images", autospec=True)

    async def images_then_close(refs):
        state.close()
        return images

    store.load_entity_images.side_effect = images_then_close

    result = await maintain_snapshots(_context(store, state))

    assert result.outcome is MaintenanceOutcome.CLOSING
    assert result.saved == ()
    assert result.batches == 0
    prepare.assert_not_awaited()
    store.save_snapshots.assert_not_awaited()
    for image in images:
        image.close.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("prepared_count", [0, 2])
async def test_closing_after_preparation_releases_resources_without_saving(
        mocker, prepared_count):
    state = MaintenanceState()
    store = _store()
    images = (_image(), _image())
    prepared = [_prepared() for _ in range(prepared_count)]
    store.load_entity_images.return_value = images

    async def prepare_then_close(loaded):
        state.close()
        return prepared

    mocker.patch.object(
        snapshots, "_prepare_images", autospec=True, side_effect=prepare_then_close)

    result = await maintain_snapshots(_context(store, state))

    assert result.outcome is MaintenanceOutcome.CLOSING
    assert result.saved == ()
    assert result.batches == 0
    store.save_snapshots.assert_not_awaited()
    for resource in (*images, *prepared):
        resource.close.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["candidates", "images", "preparation"])
async def test_closing_does_not_hide_stage_failure(mocker, stage):
    state = MaintenanceState()
    store = _store()
    image = _image()
    store.load_entity_images.return_value = (image,)
    failure = SnapshotMaintenanceError("维护阶段的实际失败")
    prepare = mocker.patch.object(
        snapshots, "_prepare_images", autospec=True, return_value=[])

    async def fail_after_close(*args):
        state.close()
        raise failure

    if stage == "candidates":
        store.snapshot_candidates.side_effect = fail_after_close
    elif stage == "images":
        store.load_entity_images.side_effect = fail_after_close
    else:
        prepare.side_effect = fail_after_close

    with pytest.raises(SnapshotMaintenanceError) as caught:
        await maintain_snapshots(_context(store, state))

    assert caught.value is failure
    assert not state.disabled
    store.save_snapshots.assert_not_awaited()
    if stage == "preparation":
        image.close.assert_called_once_with()
    else:
        image.close.assert_not_called()


@pytest.mark.asyncio
async def test_closing_cleanup_failure_still_releases_other_resources(mocker):
    state = MaintenanceState()
    store = _store()
    images = (_image(), _image())
    prepared = [_prepared(), _prepared()]
    failure = SnapshotMaintenanceError("临时资源回收失败")
    images[-1].close.side_effect = failure
    store.load_entity_images.return_value = images

    async def prepare_then_close(loaded):
        state.close()
        return prepared

    mocker.patch.object(
        snapshots, "_prepare_images", autospec=True, side_effect=prepare_then_close)

    with pytest.raises(SnapshotMaintenanceError) as caught:
        await maintain_snapshots(_context(store, state))

    assert caught.value is failure
    store.save_snapshots.assert_not_awaited()
    for resource in (*images, *prepared):
        resource.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_closing_keeps_previously_saved_progress(mocker):
    state = MaintenanceState()
    store = _store()
    image, prepared = _image(), _prepared()
    saved = SavedProgress(ref=_REF, remaining_changes=0)
    store.load_entity_images.return_value = (image,)
    store.save_snapshots.return_value = (saved,)
    mocker.patch.object(
        snapshots, "_prepare_images", autospec=True, return_value=[prepared])
    candidate_calls = 0

    async def next_candidates(threshold, limit):
        nonlocal candidate_calls
        candidate_calls += 1
        if candidate_calls == 2:
            state.close()
        return (_REF,)

    store.snapshot_candidates.side_effect = next_candidates

    result = await maintain_snapshots(_context(store, state, batch_size=1))

    assert result.outcome is MaintenanceOutcome.CLOSING
    assert result.saved == (saved,)
    assert result.batches == 1
    store.load_entity_images.assert_awaited_once_with((_REF,))
    store.save_snapshots.assert_awaited_once_with((prepared,))
    image.close.assert_called_once_with()
    prepared.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_closing_does_not_reset_disabled_maintenance():
    state = MaintenanceState()
    state.disable()
    state.close()
    store = _store()

    result = await maintain_snapshots(_context(store, state))

    assert result.outcome is MaintenanceOutcome.DISABLED
    assert state.disabled
    assert state.closing
    store.snapshot_candidates.assert_not_awaited()

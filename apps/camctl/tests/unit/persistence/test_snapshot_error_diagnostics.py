"""快照错误诊断及关闭组合；模块导入读取真实包资源。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.history import snapshots
from camctl.history.bounded import SegmentHistoryHandle
from camctl.history.snapshot_stream import PreparedSnapshotSource
from camctl.history.snapshots import (
    MaintenanceContext, MaintenanceOutcome, MaintenanceResult, MaintenanceState,
    SnapshotEnqueueTimeout, SnapshotMaintenanceError, SnapshotRef, SnapshotStore,
    maintain_snapshots,
)
from camctl.persistence import snapshot_runtime
from camctl.persistence.business import BusinessDatabase


def _owner(mocker):
    database = create_autospec(BusinessDatabase, instance=True)
    database.metadata = SimpleNamespace(instance_id="a" * 32)
    store = mocker.patch.object(snapshot_runtime, "SqliteSnapshotStore", autospec=True)
    store.return_value.discover_objects.return_value = (12, False)
    maintain = mocker.patch.object(snapshot_runtime, "maintain_snapshots", autospec=True)
    maintain.return_value = MaintenanceResult(MaintenanceOutcome.IDLE)
    disabled = mocker.Mock()
    owner = snapshot_runtime.SnapshotMaintenanceOwner(database, Path("/state.sqlite"),
        threshold=64, batch_size=16, on_disabled=disabled)
    return owner, database, store, maintain, disabled


def _store():
    store = create_autospec(SnapshotStore, instance=True, spec_set=True)
    store.snapshot_candidates.return_value = (SnapshotRef(1, 8),)
    return store


def _image():
    return create_autospec(SegmentHistoryHandle, instance=True, spec_set=True)


def _prepared():
    return create_autospec(PreparedSnapshotSource, instance=True, spec_set=True)


def _context(store, state):
    return MaintenanceContext(64, 16, state, store)


@pytest.mark.asyncio
async def test_disabled_snapshot_discovery_retains_actual_timeout_diagnostic(mocker):
    from camctl.logging_runtime.diagnostics import error_diagnostics_context, flush_diagnostics
    owner, _, store, _, _ = _owner(mocker)
    try:
        raise TimeoutError("数据库队列持续满载")
    except TimeoutError as cause:
        try:
            raise SnapshotEnqueueTimeout("快照发现未入队") from cause
        except SnapshotEnqueueTimeout as timeout:
            store.return_value.discover_objects.side_effect = timeout
    diagnostics = []
    with error_diagnostics_context(None, diagnostics.append):
        await owner.start()
        await owner.settle()
        await flush_diagnostics()

    store.return_value.discover_objects.assert_awaited_once_with(None, 64)
    assert len(diagnostics) == 1
    for fact in ("TimeoutError", "数据库队列持续满载", "快照发现未入队", "/state.sqlite", "traceback", "a" * 32):
        assert fact in diagnostics[0]


@pytest.mark.asyncio
async def test_save_queue_timeout_and_cleanup_failure_preserve_both_errors(mocker):
    from camctl.operations.error_diagnostics import exception_diagnostic
    import json
    state = MaintenanceState()
    store = _store()
    images = (_image(), _image())
    prepared = [_prepared(), _prepared()]
    timeout = snapshots.SnapshotEnqueueTimeout("快照保存尚未入队")
    failure = SnapshotMaintenanceError("临时资源关闭失败")
    images[-1].close.side_effect = failure
    store.load_entity_images.return_value = images
    store.save_snapshots.side_effect = timeout

    def prepare(image):
        for source, result in zip(images, prepared, strict=True):
            if image is source:
                return result
        raise AssertionError("收到未知快照来源")

    async def run_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    mocker.patch.object(snapshots, "prepare_snapshot", autospec=True, side_effect=prepare)
    mocker.patch.object(asyncio, "to_thread", autospec=True, side_effect=run_inline)

    with pytest.raises(SnapshotMaintenanceError) as caught:
        await maintain_snapshots(_context(store, state))

    assert caught.value is failure
    assert caught.value.__cause__ is timeout
    assert state.disabled
    store.save_snapshots.assert_awaited_once_with(tuple(prepared))
    diagnostic = json.dumps(exception_diagnostic(caught.value), ensure_ascii=False)
    assert "快照保存尚未入队" in diagnostic
    assert "临时资源关闭失败" in diagnostic
    for resource in (*images, *prepared):
        resource.close.assert_called_once_with()

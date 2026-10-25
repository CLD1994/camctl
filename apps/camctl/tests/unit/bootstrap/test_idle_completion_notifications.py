"""纯发现结束不制造自身下一轮，真实结果和错误仍立即交接。"""

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec

import pytest

from camctl.bootstrap.background_flow import BackgroundFlow
from camctl.bootstrap.cleanup_assembly import cleanup_flow
from camctl.bootstrap.flows import CaptureFlow
from camctl.operations.attempts import AttemptConfig
from camctl.outputs.cleanup_flow import CleanupRuntime
from camctl.persistence.business import BusinessDatabase
from camctl.persistence.runtime import DatabaseMetadata, OwnedConnection
from camctl.scheduling.notifications import WakeReason, WorkNotifier

pytestmark = pytest.mark.asyncio


def _empty_cleanup():
    connection = create_autospec(sqlite3.Connection, instance=True, spec_set=True)
    cursor = create_autospec(sqlite3.Cursor, instance=True, spec_set=True)
    cursor.fetchall.return_value = []
    connection.execute.return_value = cursor
    metadata = DatabaseMetadata("camctl", "a" * 32, 5, "/staging", "/ready", "/processing")
    owned = OwnedConnection(connection, metadata)
    database = create_autospec(BusinessDatabase, instance=True, spec_set=True)
    database.metadata = metadata

    async def read(callback, **_options):
        # 模拟数据库跨事件循环交付；纯查询也会在第一次让步后才结束。
        await asyncio.sleep(0)
        return callback(owned)

    database.read.side_effect = read
    runtime = CleanupRuntime(database, None, None, None, None, AsyncMock(),
                             lambda: 1000, AttemptConfig(1, None), AttemptConfig(1, None))
    wake = WorkNotifier()
    return runtime, SimpleNamespace(database=database, wake=wake)


async def test_real_empty_cleanup_rounds_do_not_self_wake_while_capture_owner_is_pending():
    runtime, context = _empty_cleanup()
    release = asyncio.Event()

    async def actual_capture(_context):
        await release.wait()

    capture = CaptureFlow(AsyncMock())
    capture._devices["cam"] = BackgroundFlow(actual_capture)
    await capture._devices["cam"](context)
    cleanup = cleanup_flow(AsyncMock(return_value=runtime))
    try:
        observed = context.wake.snapshot()
        for _ in range(12):
            await cleanup(context)
            await cleanup.settle()
            assert capture.owns_device("cam")
        assert context.wake.snapshot().version == observed.version
        context.database.write.assert_not_called()

        # 新业务仍使用原通知端口打断中央等待。
        waiter = asyncio.create_task(context.wake.wait_changed(observed))
        context.wake.mark_changed(WakeReason.NEW_WORK)
        notice = await asyncio.wait_for(waiter, 1)
        assert WakeReason.NEW_WORK in notice.reasons
    finally:
        release.set()
        await capture.settle()
        await cleanup.settle()


async def test_empty_cleanup_query_error_notifies_and_keeps_original_result_owned():
    runtime, context = _empty_cleanup()
    failure = ValueError("清理发现查询失败")

    async def fail_read(callback, **_options):
        await asyncio.sleep(0)
        raise failure

    context.database.read.side_effect = fail_read
    cleanup = cleanup_flow(AsyncMock(return_value=runtime))
    observed = context.wake.snapshot()
    await cleanup(context)
    notice = await asyncio.wait_for(context.wake.wait_changed(observed), 1)
    assert WakeReason.ACTIVITY_FINISHED in notice.reasons
    assert cleanup.required_settlements() == 1
    with pytest.raises(ValueError) as caught:
        await cleanup.settle()
    assert caught.value is failure
    assert cleanup.required_settlements() == 0

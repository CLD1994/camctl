"""维护轮次按真实监督协议消费已结束责任后才能重入。"""

from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.bootstrap.work_file_assembly import WorkFileRuntime
from camctl.outputs.work_files import CleanupScan, WorkFileLimits
from camctl.persistence.runtime import OwnedConnection


@pytest.mark.asyncio
async def test_completed_maintenance_is_consumed_before_next_round(monkeypatch):
    runtime = WorkFileRuntime(staging=Path("/staging"), limits=WorkFileLimits(1, 2))
    runtime.history.scan = CleanupScan(start_after=0, ceiling=2, after_id=0,
                                      remaining=1, batch_size=1)
    calls = []

    async def maintain(context):
        calls.append(context)

    monkeypatch.setattr(runtime, "_maintain", maintain)
    monkeypatch.setattr(runtime, "_discover", lambda _owned: None)
    owned = create_autospec(OwnedConnection, instance=True)
    owned.connection = create_autospec(sqlite3.Connection, instance=True)
    first = SimpleNamespace(open_connection=lambda: owned)
    second = SimpleNamespace(open_connection=lambda: owned)
    original_scan = runtime.history.scan
    await runtime.flow(first)
    await runtime._task

    await runtime.flow(second)
    await runtime.settle()

    assert calls == [first, second]
    assert runtime.supervisor.outstanding() == ()
    assert runtime.history.scan is original_scan
    assert runtime.processed == {}

"""由真实录像修复与正式成品登记准备主机清理目标。"""

from contextlib import contextmanager
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest_asyncio
from camctl.host_files.tasks import FileTaskExecutor
from camctl.session.supervision import Supervisor

from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from .recording_consumer_cases import reconciliation_case
from ..bootstrap.test_output_binding_changes import _NOW
from ..outputs.test_cleanup_delete_save_recovery import _configured
from ..outputs.test_cleanup_query_save_recovery import _MatrixDriver, _start_cleanup


@pytest_asyncio.fixture
async def repaired_cleanup(tmp_path, recording_cases):
    case = await reconciliation_case(tmp_path, started_at_offset_s=120, repair=True)
    await case.advance()
    output_id, file_id, relative = case.owned.connection.execute(
        "SELECT o.id,i.id,i.relative_path FROM outputs o JOIN intermediate_files i"
        " ON i.id=o.intermediate_file_id WHERE o.kind=2").fetchone()
    target = Path(case.config.paths.staging) / relative
    content = case.tools.content
    assert target.read_bytes() == content
    assert case.stop_calls == ["stop_recording"] and case.tools.calls == ["repair"]
    _, item_id = _start_cleanup(case.owned, output_id, "2")
    context = SimpleNamespace(open_connection=lambda: open_existing(
        case.path, DbOpenMode.EXISTING_RW, DbConfig()))
    driver = _MatrixDriver(case.owned)
    environment = (case.config, case.owned, context, driver)
    supervisor = Supervisor()
    executor = FileTaskExecutor(supervisor)
    runtime = await _configured(environment, case.owned, driver, wall=lambda: _NOW, file_executor=executor)
    try:
        yield environment, runtime, item_id, output_id, file_id, target, content, executor
    finally:
        await supervisor.drain_required()


@contextmanager
def unknown_host_delete(target, *, query_unknown=False):
    """删除和删除后检查均失败，保留真实文件并产生未知删除结果。"""
    unlink, stat = Path.unlink, os.stat
    post_delete = False
    calls = []

    def fail_unlink(path, *args, **kwargs):
        nonlocal post_delete
        if path == target:
            calls.append(path)
            post_delete = True
            raise OSError(5, "删除结果未知", str(path))
        return unlink(path, *args, **kwargs)

    def fail_stat(path, *args, **kwargs):
        nonlocal post_delete
        if post_delete and Path(path) == target:
            if not query_unknown:
                post_delete = False
            raise OSError(5, "删除后的观察结果未知", str(path))
        return stat(path, *args, **kwargs)

    with patch.object(Path, "unlink", fail_unlink), patch.object(os, "stat", fail_stat):
        yield calls

"""业务 worker 每次打开连接都使用本会话生效的数据库配置。"""

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import call, create_autospec

import pytest

from camctl.acceptance.service import CommandMode
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.bootstrap.lifecycle import RuntimeDeps, _open_runtime_connection
from camctl.contracts.values import new_operation_key
from camctl.persistence.executor import DbExecutor
from camctl.persistence.models import (
    DbJob, DbJobKind, DbOutcome, DbOutcomeKind, DbPriority, ReadReceipt,
)
from camctl.persistence.runtime import DatabaseMetadata, DbConfig, DbOpenMode, OwnedConnection


def _deps():
    config = load_config({"database": {
        "busy_timeout_ms": 321, "wal_autocheckpoint_pages": 37,
    }}, ConfigDefaults())
    return RuntimeDeps(CommandMode.RUN, config, Path("/state.db"),
                       Path("/session.lock"), Path("/admission.lock"), None)


def _owned(deps):
    return OwnedConnection(create_autospec(sqlite3.Connection, instance=True, spec_set=True),
        DatabaseMetadata("camctl", "e" * 32, 5, str(deps.config.paths.staging),
                         str(deps.config.paths.ready), str(deps.config.paths.processing)))


def test_runtime_factory_uses_effective_database_config(mocker):
    deps = _deps()
    owned = _owned(deps)
    opening = mocker.patch("camctl.bootstrap.lifecycle.open_existing", autospec=True,
                           return_value=owned)
    binding = mocker.patch("camctl.bootstrap.lifecycle.verify_directory_binding", autospec=True)

    assert _open_runtime_connection(deps) is owned

    opening.assert_called_once_with(deps.state_db, DbOpenMode.EXISTING_RW,
                                   DbConfig(busy_timeout_ms=321, wal_autocheckpoint_pages=37))
    binding.assert_called_once_with(owned.metadata, deps.config.paths.staging,
                                   deps.config.paths.ready, deps.config.paths.processing)
    owned.connection.close.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_write_reopens_worker_connection_with_same_effective_config(mocker):
    """真实worker循环内联处理队列，不启动线程或连接真实数据库。"""
    deps = _deps()
    first, second = _owned(deps), _owned(deps)
    opening = mocker.patch("camctl.bootstrap.lifecycle.open_existing", autospec=True,
                           side_effect=[first, second])
    binding = mocker.patch(
        "camctl.bootstrap.lifecycle.verify_directory_binding", autospec=True)
    executor = DbExecutor(lambda: _open_runtime_connection(deps))
    assert executor._thread is None

    write_job = DbJob(
        new_operation_key(), "未知提交", DbJobKind.WRITE, DbPriority.BUSINESS,
        lambda owned: DbOutcome(DbOutcomeKind.UNKNOWN),
    )

    def read_reopened(owned):
        assert owned is second
        first.connection.close.assert_called_once_with()
        second.connection.close.assert_not_called()
        return owned.metadata.instance_id

    read_job = DbJob(
        new_operation_key(), "重开连接", DbJobKind.READ, DbPriority.BUSINESS,
        read_reopened,
    )
    futures = []
    for job in (write_job, read_job):
        future = asyncio.get_running_loop().create_future()
        executor._results[job.key] = future
        futures.append(future)
        assert executor._core.try_admit(job)
    executor._stopping = True
    executor._run()
    assert executor._thread is None

    write_result, read_result = await asyncio.gather(*futures)
    assert write_result.kind is DbOutcomeKind.UNKNOWN
    assert isinstance(read_result, ReadReceipt)
    assert read_result.value == second.metadata.instance_id

    opening.assert_has_calls([call(deps.state_db, DbOpenMode.EXISTING_RW,
        DbConfig(busy_timeout_ms=321, wal_autocheckpoint_pages=37))] * 2)
    binding.assert_has_calls([call(first.metadata, deps.config.paths.staging,
        deps.config.paths.ready, deps.config.paths.processing),
        call(second.metadata, deps.config.paths.staging, deps.config.paths.ready,
        deps.config.paths.processing)])
    assert opening.call_count == 2
    second.connection.close.assert_called_once_with()
    first.connection.execute.assert_not_called()
    second.connection.execute.assert_not_called()

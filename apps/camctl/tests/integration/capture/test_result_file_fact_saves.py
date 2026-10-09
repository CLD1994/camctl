"""RESULTS 在场、归属与完成事实的原申请和提交未知恢复。"""

from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from camctl.contracts.values import ConsistencyError
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from .test_result_consumer_saves import _actual, _consume, _consumer_world, _result_port
from .test_result_file_recovery import _fresh_runtime

pytestmark = pytest.mark.asyncio

_COLUMNS = {"presence": "presence_state", "ownership": "source_action_id", "completion": "completion_state"}


class FileFactFault:
    """实际设备文件事实投影与 COMMIT 前后的一次确定故障。"""

    def __init__(self, real, stage, mode):
        self.real, self.stage, self.mode = real, stage, mode
        self.armed = True
        self.fact_transaction = False

    def __getattr__(self, name):
        return getattr(self.real, name)

    def execute(self, statement, params=()):
        if statement.startswith("UPDATE device_files SET") and f"{_COLUMNS[self.stage]} = ?" in statement:
            self.fact_transaction = True
            if self.armed and self.mode == "projection":
                self.armed = False
                raise sqlite3.OperationalError("文件事实投影写入失败")
        if self.armed and self.fact_transaction and statement == "COMMIT":
            self.armed = False
            if self.mode == "commit_after":
                self.real.execute(statement, params)
            raise sqlite3.OperationalError("文件事实提交边界错误")
        result = self.real.execute(statement, params)
        if statement in ("COMMIT", "ROLLBACK"):
            self.fact_transaction = False
        return result


class FileFactSpy(CaptureRepository):
    """真实仓储保存事实，记录所选阶段的完整公开请求和 key。"""

    def __init__(self, stage):
        self.stage, self.requests = stage, []

    def _record(self, stage, command, key):
        if self.stage == stage:
            self.requests.append((command, key))

    def save_file_presence(self, command, key, owned):
        self._record("presence", command, key)
        return super().save_file_presence(command, key, owned)

    def save_file_ownership(self, command, key, owned):
        self._record("ownership", command, key)
        return super().save_file_ownership(command, key, owned)

    def save_file_completion(self, command, key, owned):
        self._record("completion", command, key)
        return super().save_file_completion(command, key, owned)


@pytest.mark.parametrize("consumer", ["photo", "record"])
@pytest.mark.parametrize("stage", ["presence", "ownership", "completion"])
@pytest.mark.parametrize("mode", ["projection", "commit_before", "commit_after"])
async def test_file_fact_fault_keeps_original_stage_request_and_key(tmp_path, consumer, stage, mode):
    owned, runtime, action_id, handler = await _consumer_world(tmp_path, consumer)
    actual = _actual(action_id, with_files=True, complete=True)
    if consumer == "photo":
        actual.observations[0].data["entries"][0]["kind"] = "photo"
    driver = _result_port(runtime, actual)
    spy = FileFactSpy(stage)
    proxy = FileFactFault(owned.connection, stage, mode)
    runtime.capture = spy
    runtime.owned = replace(owned, connection=proxy)
    original_wall = runtime.wall_us()
    path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    metadata = owned.metadata
    try:
        try:
            await _consume(runtime, consumer, action_id, handler)
        except (ConsistencyError, AssertionError):
            pending, = runtime.pending_start_results.values()
            assert pending.finish.outcome.outcome is actual
        assert not proxy.armed, "必须实际到达所选文件事实的投影或提交边界"
        command, key = spy.requests[0]
        assert command.occurred_at == original_wall
        owned.connection.close()
        owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        assert owned.metadata == metadata
        resumed = _fresh_runtime(owned, runtime)
        resumed.capture = spy
        resumed.wall_us = lambda: original_wall + 5_000_000

        await _consume(resumed, consumer, action_id, handler)

        driver.list_results.assert_awaited_once()
        assert all(request == command and request_key == key for request, request_key in spy.requests)
        assert owned.connection.execute(
            "SELECT source_action_id,presence_state,completion_state,size_bytes FROM device_files"
            " WHERE observer_action_id=?", (action_id,)).fetchone() == (action_id, 2, 3, 41)
        assert owned.connection.execute(
            "SELECT attempts_used FROM operation_runs WHERE responsibility_key=?",
            (f"results/{action_id}",)).fetchone() == (1,)
    finally:
        owned.connection.close()

"""RESULTS 多轮文件事实与文件发现请求的真实保存责任。"""

from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from camctl.capture.handlers import (
    _begin_check_round, _finish_listing_result, _listing_round, _register_observed,
)
from camctl.capture.models import ResultSetPhase, ResultSetSave
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.operations.attempts import RunOutcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from .test_capture_contract import _runtime
from .test_result_consumer_saves import (
    _EVIDENCE, _actual, _consume, _consumer_world, _result_port,
)

pytestmark = pytest.mark.asyncio


def _fresh_runtime(owned, original):
    resumed = _runtime(owned, driver=original.driver)
    resumed.evidence = _EVIDENCE
    resumed.check_config = original.check_config
    resumed.wall_us = original.wall_us
    resumed.monotonic_ns = original.monotonic_ns
    resumed.results = original.results
    resumed.pending_start_results = original.pending_start_results
    resumed.pending_file_observations = original.pending_file_observations
    resumed.retry_gate = original.retry_gate
    return resumed


@pytest.mark.parametrize("consumer", ["photo", "record", "cancel", "timelapse"])
async def test_closed_latest_error_keeps_previously_registered_file_input(tmp_path, consumer):
    """后轮没有文件观察不能抹去已登记原文件的本地消费资格。"""
    owned, runtime, action_id, handler = await _consumer_world(tmp_path, consumer)
    first = _actual(action_id, with_files=True, complete=True)
    if consumer == "photo":
        # 固定任务的完整照片与延时集合结束是独立契约。
        first.observations[0].data["entries"][0]["kind"] = "photo"
    driver = _result_port(runtime, first)
    try:
        listing = await _listing_round(runtime, action_id)
        _register_observed(runtime, action_id, listing.entries, occurred_at=listing.occurred_at)
        _finish_listing_result(runtime, listing, retry_wait=True)
        assert owned.connection.execute(
            "SELECT source_action_id,completion_state,size_bytes FROM device_files"
            " WHERE observer_action_id=?", (action_id,)).fetchone() == (action_id, 3, 41)

        # 原已登记文件或独立正式结论允许责任关闭；后轮错误本身不提供结论。
        ticket = _begin_check_round(runtime, action_id).ticket
        assert ticket is not None
        latest = _actual(action_id, with_files=False)
        runtime.finish(ticket, latest, end_run=RunOutcome.SUCCEEDED)
        if consumer == "timelapse":
            receipt = runtime.capture.confirm_result_set(ResultSetSave(
                action_id=action_id, occurred_at=runtime.wall_us(),
                phase=ResultSetPhase.UNSATISFIED, contract="task_scope_files",
                observation={"reason": "known_failure"},
                capture={"status": "failed", "error": {"code": "capture_unsatisfied"}},
                evidence={"method": "known_failure", "observation": {"reason": "known_failure"}},
            ), new_operation_key(), owned)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        resumed = _fresh_runtime(owned, runtime)
        unavailable = _result_port(resumed, latest)
        unavailable.list_results.side_effect = AssertionError("CLOSED 本地恢复禁止新列举")

        await _consume(resumed, consumer, action_id, handler)

        unavailable.list_results.assert_not_awaited()
        driver.list_results.assert_awaited_once()
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM outputs WHERE source_action_id=? AND device_file_id IS NOT NULL",
            (action_id,)).fetchone() == (1,)
        attempts = owned.connection.execute(
            "SELECT t.status,t.result_json,r.attempts_used FROM operation_attempts t"
            " JOIN operation_runs r ON r.id=t.run_id WHERE r.responsibility_key=?"
            " ORDER BY t.attempt_no", (f"results/{action_id}",)).fetchall()
        assert len(attempts) == 2
        assert [(row[0], row[2]) for row in attempts] == [(3, 2), (3, 2)]
        assert json.loads(attempts[-1][1])["observations"] == []
        assert json.loads(attempts[0][1])["observations"][0]["data"]["entries"][0]["identity"] == "original"
    finally:
        owned.connection.close()


class FileWriteFault:
    """只在真实文件发现事务投影或提交边界注入一次错误。"""

    def __init__(self, real, mode):
        self.real, self.mode = real, mode
        self.armed = True
        self.file_transaction = False

    def __getattr__(self, name):
        return getattr(self.real, name)

    def execute(self, statement, params=()):
        if statement.startswith("INSERT INTO device_files"):
            self.file_transaction = True
            if self.armed and self.mode == "projection":
                self.armed = False
                raise sqlite3.OperationalError("文件发现投影写入失败")
        if self.armed and self.file_transaction and statement == "COMMIT":
            self.armed = False
            if self.mode == "commit_after":
                self.real.execute(statement, params)
            raise sqlite3.OperationalError("文件发现提交边界错误")
        result = self.real.execute(statement, params)
        if statement in ("COMMIT", "ROLLBACK"):
            self.file_transaction = False
        return result


class FileObservationSpy(CaptureRepository):
    """真实仓储仍执行完整事务，记录需要保持的公开发现请求。"""

    def __init__(self):
        self.requests = []

    def save_file_observation(self, command, key, owned):
        self.requests.append((command, key))
        return super().save_file_observation(command, key, owned)


@pytest.mark.parametrize("consumer", ["photo", "record"])
@pytest.mark.parametrize("mode", ["projection", "commit_before", "commit_after"])
async def test_file_discovery_fault_resumes_original_request_key_without_listing(tmp_path, consumer, mode):
    """发现事务不确定后跨运行时继续原请求，而不是新发现事务。"""
    owned, runtime, action_id, handler = await _consumer_world(tmp_path, consumer)
    actual = _actual(action_id, with_files=True, complete=False)
    driver = _result_port(runtime, actual)
    proxy = FileWriteFault(owned.connection, mode)
    runtime.owned = replace(owned, connection=proxy)
    spy = FileObservationSpy()
    runtime.capture = spy
    original_wall = runtime.wall_us()
    database_path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    metadata = owned.metadata
    try:
        try:
            await _consume(runtime, consumer, action_id, handler)
        except (ConsistencyError, AssertionError):
            # 一次仓储故障可以就地核实，也可以保留完整责任后停止。
            pending, = runtime.pending_start_results.values()
            assert pending.finish.outcome.outcome is actual
        assert not proxy.armed, "必须实际进入文件保存故障边界"
        original_request, original_key = spy.requests[0]
        assert original_request.observer_action_id == action_id
        assert original_request.file_identity == "original"
        assert original_request.locator == {"path": "/DCIM/original.mp4"}
        assert original_request.occurred_at == original_wall

        # 关闭原未知事务连接，按公开入口重开同一实例；关闭会回滚未提交事务。
        owned.connection.close()
        owned = open_existing(database_path, DbOpenMode.EXISTING_RW, DbConfig())
        assert owned.metadata == metadata
        resumed = _fresh_runtime(owned, runtime)
        resumed.capture = spy
        resumed.wall_us = lambda: original_wall + 5_000_000
        await _consume(resumed, consumer, action_id, handler)

        driver.list_results.assert_awaited_once()
        # 同一完整发现请求的原 key 是未知提交的核实依据。
        assert all(command == original_request and key == original_key
                   for command, key in spy.requests)
        assert owned.connection.execute(
            "SELECT source_action_id,presence_state FROM device_files"
            " WHERE observer_action_id=?", (action_id,)).fetchone() == (action_id, 2)
        assert owned.connection.execute(
            "SELECT attempts_used FROM operation_runs WHERE responsibility_key=?",
            (f"results/{action_id}",)).fetchone() == (1,)
    finally:
        owned.connection.close()

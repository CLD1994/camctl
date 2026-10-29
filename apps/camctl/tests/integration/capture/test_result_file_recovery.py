"""RESULTS 多轮文件事实与文件发现请求的真实保存责任。"""

from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from camctl.capture.models import ResultSetPhase, ResultSetSave
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.operations.attempts import AttemptFinish, RunFinish, RunOutcome
from camctl.operations.validation import validate_outcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from .test_result_consumer_saves import (
    _EVIDENCE, _actual, _consume,
)
from ..support.result_cases import begin_result_attempt, result_case, result_case_for
from camctl.bootstrap.capture_assembly import DriverResultListing
from camctl.devices.bindings import DeviceBinding
from camctl.devices.ports import DeviceCallResult, ResultDriver
from unittest.mock import create_autospec

pytestmark = pytest.mark.asyncio


async def _fresh_runtime(owned, original):
    """原会话工厂继续拥有保存申请；实际旧数据库先结束再重开。"""
    return await result_case_for().reopen(owned)


def _result_driver(actual):
    driver = create_autospec(ResultDriver, instance=True)
    driver.list_results.return_value = DeviceCallResult.from_outcome(actual)
    result_case_for().results.delegate = DriverResultListing(
        driver, DeviceBinding("cam-1", "camctl-adb"), _EVIDENCE)
    return driver


@pytest.mark.parametrize("consumer", ["photo", "record", "cancel", "timelapse"])
async def test_closed_latest_error_keeps_previously_registered_file_input(tmp_path, consumer):
    """后轮没有文件观察不能抹去已登记原文件的本地消费资格。"""
    case = result_case_for()
    owned, runtime, action_id, handler = await case.world(tmp_path, consumer, result_page_contract=False)
    first = _actual(action_id, with_files=True, complete=True)
    if consumer == "photo":
        # 固定任务的完整照片与延时集合结束是独立契约。
        first.observations[0].data["entries"][0]["kind"] = "photo"
    driver = _result_driver(first)
    try:
        await _consume(runtime, consumer, action_id, handler)
        assert owned.connection.execute(
            "SELECT source_action_id,completion_state,size_bytes FROM device_files"
            " WHERE observer_action_id=?", (action_id,)).fetchone() == (action_id, 3, 41)

        # 原已登记文件或独立正式结论允许责任关闭；后轮错误本身不提供结论。
        ticket = begin_result_attempt(owned, action_id, case.options['check_config'], case.clock.wall)
        latest = _actual(action_id, with_files=False)
        saved = OperationRepository().finish_attempt(AttemptFinish(ticket,
            validate_outcome(ticket, latest, _EVIDENCE), case.clock.wall,
            run_finish=RunFinish(RunOutcome.SUCCEEDED)), new_operation_key(), owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        if consumer == "timelapse":
            receipt = runtime.capture.confirm_result_set(ResultSetSave(
                action_id=action_id, occurred_at=case.clock.wall,
                phase=ResultSetPhase.UNSATISFIED, contract="task_scope_files",
                observation={"reason": "known_failure"},
                capture={"status": "failed", "error": {"code": "capture_failed", "stage": "execution",
                    "details": {"activity_id": str(action_id), "reason": "invalid_outputs"}}},
                evidence={"method": "known_failure", "observation": {"reason": "known_failure"}},
            ), new_operation_key(), owned)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        resumed = await case.reopen(owned, cold=True)
        unavailable = _result_driver(latest)
        unavailable.list_results.side_effect = AssertionError("CLOSED 本地恢复禁止新列举")

        await _consume(resumed, consumer, action_id, handler)

        unavailable.list_results.assert_not_awaited()
        driver.list_results.assert_awaited_once()
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM outputs WHERE source_action_id=? AND device_file_id IS NOT NULL",
            (action_id,)).fetchone() == (1,)
        if consumer in ("photo", "record"):
            status, error = owned.connection.execute(
                'SELECT status,error_details_json FROM actions WHERE id=?', (action_id,)).fetchone()
            assert status == 4
            assert json.loads(error)["reason"] == "outputs_unknown"
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

    def __init__(self, real, mode, *, failures=1):
        self.real, self.mode = real, mode
        self.armed = True
        self.file_transaction = False
        self.remaining = failures

    def __getattr__(self, name):
        return getattr(self.real, name)

    def execute(self, statement, params=()):
        commit = False

        def fail():
            self.remaining -= 1
            self.armed = self.remaining > 0

        def authorize(operation, table, _column, _database, _source):
            nonlocal commit
            if operation == sqlite3.SQLITE_INSERT and table == 'device_files':
                self.file_transaction = True
                if self.armed and self.mode == 'projection':
                    fail()
                    return sqlite3.SQLITE_DENY
            if operation == sqlite3.SQLITE_TRANSACTION:
                commit = table == 'COMMIT' and self.file_transaction and self.armed
                if commit and self.mode == 'commit_before':
                    fail()
                    return sqlite3.SQLITE_DENY
                if table == 'ROLLBACK':
                    self.file_transaction = False
            return sqlite3.SQLITE_OK

        self.real.set_authorizer(authorize)
        try:
            result = self.real.execute(statement, params)
            if commit:
                self.file_transaction = False
                if self.armed and self.mode == 'commit_after':
                    fail()
                    raise sqlite3.OperationalError('文件发现提交边界错误')
            return result
        finally:
            self.real.set_authorizer(None)


class FileObservationSpy(CaptureRepository):
    """真实仓储仍执行完整事务，记录需要保持的公开发现请求。"""

    def __init__(self, mode=None, *, failures=1):
        self.requests = []
        self.mode, self.failures, self.fault = mode, failures, None

    def save_file_observation(self, command, key, owned):
        self.requests.append((command, key))
        if self.mode is not None:
            if self.fault is None:
                self.fault = FileWriteFault(owned.connection, self.mode, failures=self.failures)
            else:
                self.fault.real = owned.connection
            owned = replace(owned, connection=self.fault)
        return super().save_file_observation(command, key, owned)


@pytest.mark.parametrize("consumer", ["photo", "record"])
@pytest.mark.parametrize("mode", ["projection", "commit_before", "commit_after"])
async def test_file_discovery_fault_resumes_original_request_key_without_listing(tmp_path, consumer, mode):
    """发现事务不确定后跨运行时继续原请求，而不是新发现事务。"""
    case = result_case_for()
    owned, runtime, action_id, handler = await case.world(tmp_path, consumer, result_page_contract=False)
    actual = _actual(action_id, with_files=True, complete=False)
    driver = _result_driver(actual)
    spy = FileObservationSpy(mode)
    runtime.capture = spy
    original_wall = case.clock.wall
    database_path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    metadata = owned.metadata
    try:
        try:
            await _consume(runtime, consumer, action_id, handler)
        except ConsistencyError:
            # 正式处理器可在本次停止，原工厂继续拥有完整保存申请。
            pass
        assert spy.fault is not None and not spy.fault.armed, "必须实际进入文件保存故障边界"
        original_request, original_key = spy.requests[0]
        assert original_request.observer_action_id == action_id
        assert original_request.file_identity == "original"
        assert original_request.locator == {"path": "/DCIM/original.mp4"}
        assert original_request.occurred_at == original_wall

        # 关闭原未知事务连接，按公开入口重开同一实例；关闭会回滚未提交事务。
        owned.connection.close()
        owned = open_existing(database_path, DbOpenMode.EXISTING_RW, DbConfig())
        assert owned.metadata == metadata
        resumed = (await _fresh_runtime(owned, runtime))
        resumed.capture = spy
        case.clock.wall = original_wall + 5_000_000
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

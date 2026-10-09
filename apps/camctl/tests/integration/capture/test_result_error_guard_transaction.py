"""三个公开结果保存入口在事务内核实调用方实际提交的错误。

受理、调度、START、RESULTS 和文件登记均使用真实组件；构造完成
后修改原嵌套字典，使实际事件守卫成为错误实例的验证边界。
"""

from copy import deepcopy
from dataclasses import dataclass, replace
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import traceback

import pytest

from camctl.capture.handlers import (
    ListingPhase, _listing_round, _register_listing, capture_handler,
)
from camctl.capture.models import ResultSetPhase, ResultSetSave
from camctl.contracts.values import new_operation_key
from camctl.contracts import workflow_errors
from camctl.contracts.schemas import SchemaRuleError
from camctl.contracts.workflow_errors import registered_error
from camctl.devices.evidence import DeviceObservation
from camctl.history.validators import EventValidationError
from camctl.operations.attempts import AttemptConfig, AttemptFinish, RunFinish, RunOutcome
from camctl.operations.models import (
    AttemptStatus, CallOutcome, EffectState, ErrorValue, EvidenceValue,
    Settlement, SettlementBasis,
)
from camctl.operations.validation import validate_outcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository, register_capture_guards
from camctl.persistence.repositories.operations import register_operation_guards
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.persistence.repositories.timelapse import register_timelapse_guards
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from .result_consumer_fixtures import consumer_world
from .test_result_consumer_saves import _result_port

pytestmark = pytest.mark.asyncio
register_capture_guards()
register_operation_guards()
register_outputs_guards()
register_timelapse_guards()

_ENTRIES = ("confirm_result_set", "finish_result_check", "close_result_check_unconfirmed")


def _known_error(activity_id):
    code = "capture_result_unconfirmed"
    return {"code": code, "stage": registered_error(code)["stage"],
            "details": {"activity_id": str(activity_id), "reason": "outputs_unknown"}}


def _unknown_error(origin="original"):
    return {"code": "vendor_capture_uncertain", "stage": "vendor_result",
            "details": {"origin": origin, "message": "文件名含中文、引号\"及换行\n",
                        "received": [7, {"path": "/DCIM/原片.bin", "complete": False}]}}


def _database_facts(owned):
    """保存全部持久表，覆盖不可变历史、归属链接和所有当前投影。"""
    tables = owned.connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return {name: tuple(sorted(owned.connection.execute(
        'SELECT * FROM "' + name.replace('"', '""') + '"').fetchall(), key=repr))
        for (name,) in tables}


@dataclass
class _World:
    owned: object
    runtime: object
    action_id: int
    activity_id: int
    result_driver: object
    entry: str
    command: ResultSetSave
    finish: AttemptFinish | None
    capture_error: dict | None
    last_error: dict | None

    def save(self, command, key):
        repository = CaptureRepository()
        if self.entry == "finish_result_check":
            return repository.finish_result_check(self.finish, command, key, self.owned)
        return getattr(repository, self.entry)(command, key, self.owned)

    def calls(self):
        return (tuple(self.runtime.driver.mock_calls), tuple(self.result_driver.mock_calls))


async def _world(tmp_path, entry, *, error_kind="known"):
    owned, runtime, action_id, handler = await consumer_world(
        tmp_path, "photo", independent_activity=True)
    try:
        activity_id, = owned.connection.execute(
            "SELECT id FROM device_activities WHERE action_id=?", (action_id,)).fetchone()
        assert action_id != activity_id
        actual = CallOutcome(
            status=AttemptStatus.SUCCEEDED, effect=EffectState.CONFIRMED,
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("results_returned", 1, {})),
            observations=(DeviceObservation("result_files_listed", 1, {
                "activity_id": str(activity_id), "entries": [{"identity": "auxiliary",
                    "kind": "other", "complete": True, "size_bytes": 41,
                    "locator": {"path": "/DCIM/auxiliary"},
                    "original_name": "auxiliary.bin", "media_type": "application/octet-stream"}]}),))
        driver = _result_port(runtime, actual)
        runtime.check_config = AttemptConfig(1, Decimal("1.25"), Decimal(0))
        finish = None
        if entry == "finish_result_check":
            listing = await _listing_round(runtime, action_id)
            assert listing.phase is ListingPhase.LISTED
            assert listing.outcome is actual
            registered = _register_listing(runtime, action_id, listing)
            assert len(registered) == 1
            original = runtime.pending_start_results[(listing.ticket.run_id, listing.ticket.attempt_id)]
            assert original.finish.ticket == listing.ticket
            assert original.finish.occurred_at == listing.occurred_at
            # 最后一轮实际返回仍不能确认 PHOTO 要求；正常调用结果
            # 与无法确认的用途分别表达，不改原 Outcome 或票据。
            run_error = _known_error(activity_id)
            finish = replace(original.finish, outcome=validate_outcome(
                listing.ticket, actual, runtime.evidence),
                run_finish=RunFinish(RunOutcome.UNCONFIRMED, ErrorValue(
                    run_error["code"], run_error["stage"], run_error["details"])))
            assert finish.outcome.outcome is actual
            assert owned.connection.execute(
                "SELECT status,result_event_id FROM operation_attempts WHERE run_id=?",
                (listing.ticket.run_id,)).fetchone() == (1, None)
            formed_at = original.finish.occurred_at
        else:
            await capture_handler(handler)(action_id, runtime)
            run_id, status, used, retry = owned.connection.execute(
                "SELECT id,status,attempts_used,retry_wait_required FROM operation_runs"
                " WHERE responsibility_key=?", (f"results/{activity_id}",)).fetchone()
            assert (status, used, retry) == (2, 1, 1)
            assert owned.connection.execute(
                "SELECT status,result_json IS NOT NULL FROM operation_attempts WHERE run_id=?",
                (run_id,)).fetchone() == (2, 1)
            formed_at = runtime.wall_us()
        driver.list_results.assert_awaited_once()
        runtime.driver.control.assert_awaited_once()
        assert owned.connection.execute("SELECT COUNT(*) FROM device_files").fetchone() == (1,)
        assert owned.connection.execute(
            "SELECT result_set_state,capture_json,last_error_json FROM device_activities WHERE id=?",
            (activity_id,)).fetchone() == (1, None, None)
        if error_kind == "none":
            capture_error = last_error = capture = None
        else:
            error = _known_error(activity_id) if error_kind == "known" else _unknown_error()
            # 两处各自持有独立字典，单一位置修改不得同时污染另一处。
            capture_error, last_error = deepcopy(error), deepcopy(error)
            capture = {"status": "unconfirmed", "error": capture_error}
        command = ResultSetSave(action_id, formed_at, ResultSetPhase.UNCONFIRMED,
            contract="task_scope_files", observation={"reason": "attempts_exhausted"},
            capture=capture, error=last_error)
        assert command.error is last_error
        if capture is not None:
            assert command.capture["error"] is capture_error
            assert capture_error is not last_error
        return _World(owned, runtime, action_id, activity_id, driver, entry,
                      command, finish, capture_error, last_error)
    except BaseException:
        owned.connection.close()
        raise


@pytest.mark.parametrize("entry", _ENTRIES)
@pytest.mark.parametrize("field", ["capture.error", "error"])
@pytest.mark.parametrize("mutation", ["missing-stage", "wrong-registered-stage", "wrong-registered-details"])
async def test_mutated_result_error_rolls_back_whole_public_transaction(tmp_path, entry, field, mutation):
    world = await _world(tmp_path, entry)
    try:
        command = world.command
        before = _database_facts(world.owned)
        calls = world.calls()
        original = deepcopy(command)
        value = world.capture_error if field == "capture.error" else world.last_error
        if mutation == "missing-stage":
            del value["stage"]
        elif mutation == "wrong-registered-stage":
            value["stage"] = "read"
            assert value["stage"] != registered_error(value["code"])["stage"]
        else:
            value["details"]["activity_id"] = "not-an-object-id"
        if field == "capture.error":
            assert command.error == original.error
        else:
            assert command.capture == original.capture

        receipt = world.save(command, new_operation_key())

        assert receipt.kind is DbOutcomeKind.ROLLED_BACK, receipt.error
        assert isinstance(receipt.error, EventValidationError), receipt.error
        assert receipt.error.__cause__ is not None
        assert world.owned.connection.in_transaction is False
        assert _database_facts(world.owned) == before
        assert world.calls() == calls
    finally:
        world.owned.connection.close()


def _result_set_value(receipt, entry):
    return receipt.value.result_set if entry == "finish_result_check" else receipt.value


@pytest.mark.parametrize("entry", _ENTRIES)
@pytest.mark.parametrize("error_kind", ["known", "unknown"])
async def test_legal_errors_preserve_values_and_original_public_request_identity(tmp_path, entry, error_kind):
    world = await _world(tmp_path, entry, error_kind=error_kind)
    try:
        command = world.command
        original = deepcopy(command)
        calls = world.calls()
        original_attempts = world.owned.connection.execute(
            "SELECT * FROM operation_attempts ORDER BY id").fetchall()
        original_files = world.owned.connection.execute("SELECT * FROM device_files ORDER BY id").fetchall()
        key = new_operation_key()

        first = world.save(command, key)

        assert first.kind is DbOutcomeKind.COMPLETED, first.error
        result = _result_set_value(first, entry)
        assert result.result_set_state == 4
        capture, last_error = world.owned.connection.execute(
            "SELECT capture_json,last_error_json FROM device_activities WHERE id=?",
            (world.activity_id,)).fetchone()
        assert json.loads(capture) == original.capture
        assert json.loads(last_error) == original.error
        assert command == original
        assert world.calls() == calls
        assert world.owned.connection.execute("SELECT * FROM device_files ORDER BY id").fetchall() == original_files
        if entry != "finish_result_check":
            assert world.owned.connection.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall() == original_attempts
        else:
            assert world.owned.connection.execute(
                "SELECT status FROM operation_attempts WHERE run_id=?",
                (world.finish.ticket.run_id,)).fetchone() == (2,)
            assert world.owned.connection.execute(
                "SELECT status,attempts_used FROM operation_runs WHERE id=?",
                (world.finish.ticket.run_id,)).fetchone() == (6, 1)
        saved = _database_facts(world.owned)

        replay = world.save(command, key)

        assert replay.kind is DbOutcomeKind.COMPLETED, replay.error
        recovered = _result_set_value(replay, entry)
        assert (recovered.result_set_state, recovered.completion_basis) == (
            result.result_set_state, result.completion_basis)
        assert recovered.disposition.value == "already"
        assert _database_facts(world.owned) == saved
        # 每次只改一个完整输入成员，所有修改仍是结构合法的申请。
        changes = {
            "occurred_at": command.occurred_at + 1,
            "contract": "another_result_contract",
            "observation": {"reason": "different_original_observation"},
            "capture": {"status": "unconfirmed", "error": _unknown_error("changed-capture")},
            "error": _unknown_error("changed-last-error"),
        }
        for name, value in changes.items():
            rejected = world.save(replace(command, **{name: value}), key)
            assert rejected.kind is DbOutcomeKind.ROLLED_BACK, (name, rejected.error)
            assert _database_facts(world.owned) == saved
        assert world.calls() == calls
        assert command == original
    finally:
        world.owned.connection.close()


@pytest.mark.parametrize("entry", _ENTRIES)
async def test_allowed_null_capture_and_error_remain_null_on_original_key_replay(tmp_path, entry):
    world = await _world(tmp_path, entry, error_kind="none")
    try:
        assert world.command.capture is None and world.command.error is None
        calls = world.calls()
        key = new_operation_key()

        first = world.save(world.command, key)

        assert first.kind is DbOutcomeKind.COMPLETED, first.error
        assert world.owned.connection.execute(
            "SELECT result_set_state,capture_json,last_error_json FROM device_activities WHERE id=?",
            (world.activity_id,)).fetchone() == (4, None, None)
        saved = _database_facts(world.owned)
        replay = world.save(world.command, key)
        assert replay.kind is DbOutcomeKind.COMPLETED, replay.error
        assert _result_set_value(replay, entry).result_set_state == 4
        assert _database_facts(world.owned) == saved
        assert world.calls() == calls
    finally:
        world.owned.connection.close()


def _clear_error_resources():
    workflow_errors._registry.cache_clear()
    workflow_errors._details_validator.cache_clear()
    workflow_errors._public_json_registry.cache_clear()
    workflow_errors._public_json_validator.cache_clear()


def _resource_failure(monkeypatch, connection):
    """只控制稳定包资源端口，记录事务内实际触发的校验位置。"""
    original = workflow_errors.resource_bytes
    failure = OSError("workflow-code 包资源不可读取")
    hits = []

    def read(name):
        if name == "protocol/workflow-codes.json":
            hits.append((connection.in_transaction,
                         tuple(frame.name for frame in traceback.extract_stack())))
            raise failure
        return original(name)

    monkeypatch.setattr(workflow_errors, "resource_bytes", read)
    _clear_error_resources()
    return failure, hits


def _exception_chain(error):
    pending, seen = [error], []
    while pending:
        current = pending.pop()
        if current is None or any(current is saved for saved in seen):
            continue
        seen.append(current)
        pending.extend((current.__cause__, current.__context__))
    return seen


class _RollbackFailure:
    """真实连接协议代理；仅 ROLLBACK 在执行前遇到 SQLite 错误。"""

    def __init__(self, connection):
        self.connection = connection
        self.rollback_calls = 0
        self.failure = sqlite3.OperationalError("实际事务的 ROLLBACK 执行失败")

    def execute(self, sql, parameters=()):
        if " ".join(sql.split()).upper() == "ROLLBACK":
            self.rollback_calls += 1
            raise self.failure
        return self.connection.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self.connection, name)


async def test_schema_rule_failure_after_begin_releases_public_owned_transaction(tmp_path, monkeypatch):
    world = await _world(tmp_path, "confirm_result_set")
    try:
        before = _database_facts(world.owned)
        calls = world.calls()
        failure, hits = _resource_failure(monkeypatch, world.owned.connection)

        with pytest.raises(SchemaRuleError) as raised:
            world.save(world.command, new_operation_key())

        assert raised.value.__cause__ is failure
        assert len(hits) == 1 and hits[0][0] is True, hits
        assert "commit_operation" in hits[0][1]
        # 当前校验也可能先由报告影响派生执行。记录实际位置，不能
        # 把事务内核的反例冒充活动守卫自身的行为红。
        assert any(name in hits[0][1] for name in (
            "_activity_guard", "_result_check_guard", "project_public")), hits
        assert world.owned.connection.in_transaction is False
        assert _database_facts(world.owned) == before
        assert world.calls() == calls
    finally:
        _clear_error_resources()
        world.owned.connection.close()


async def test_schema_rule_failure_and_failed_rollback_return_unknown_with_both_diagnostics(tmp_path, monkeypatch):
    world = await _world(tmp_path, "confirm_result_set")
    connection = world.owned.connection
    try:
        before = _database_facts(world.owned)
        calls = world.calls()
        database_path = Path(connection.execute("PRAGMA database_list").fetchone()[2])
        failure, hits = _resource_failure(monkeypatch, connection)
        proxy = _RollbackFailure(connection)
        world.owned = replace(world.owned, connection=proxy)

        receipt = world.save(world.command, new_operation_key())

        assert receipt.kind is DbOutcomeKind.UNKNOWN, receipt.error
        assert len(hits) == 1 and hits[0][0] is True, hits
        assert "commit_operation" in hits[0][1]
        assert proxy.rollback_calls == 1
        diagnostics = _exception_chain(receipt.error)
        assert any(error is proxy.failure for error in diagnostics), diagnostics
        assert any(isinstance(error, SchemaRuleError) for error in diagnostics), diagnostics
        assert any(error is failure for error in diagnostics), diagnostics
        assert connection.in_transaction is True
        assert world.calls() == calls
        # UNKNOWN 连接不得继续查询或保存。关闭实际连接后，以 fresh
        # Owned 核实可靠历史和投影，不能把原连接可见值当可靠回滚。
        connection.close()
        reopened = open_existing(database_path, DbOpenMode.EXISTING_RW, DbConfig())
        try:
            assert _database_facts(reopened) == before
        finally:
            reopened.connection.close()
    finally:
        _clear_error_resources()
        connection.close()

"""QUERY 已可靠保存后，成员及伴随结果沿原申请在新连接上续接。

所有前置业务事实通过公开事务建立。连接包装只在选中的真实结果
事务 COMMIT 前后制造错误，不伪造 UNKNOWN，也不直接修改投影。
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from sqlite3 import OperationalError

import pytest

from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.operations.attempts import AttemptConfig
from camctl.outputs.cleanup_flow import (
    DeleteDriverPort, FinishCleanupAction, delete_source_file,
)
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.cancellation import register_cancellation_guards
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.operations import OperationRepository, register_operation_guards
from camctl.persistence.repositories.outputs import OutputsRepository, register_outputs_guards

from .test_cleanup_query_save_recovery import (
    _MatrixDriver, _apply_cleanup_cancel, _member_facts, _new_runtime,
    _original_member_rows, _start_cleanup,
)
from ..bootstrap.test_output_binding_changes import _NOW, _save_photos, environment


pytestmark = pytest.mark.asyncio

register_operation_guards()
register_outputs_guards()
register_capture_guards()
register_cancellation_guards()


class _CommitFault:
    """执行真实事务；选中的 COMMIT 前后分别丢失可靠完成结果。"""

    def __init__(self, connection, after_commit):
        self.connection = connection
        self.after_commit = after_commit
        self.triggered = False

    def execute(self, statement, parameters=()):
        if statement == "COMMIT" and not self.triggered:
            self.triggered = True
            if self.after_commit:
                self.connection.execute(statement, parameters)
            raise OperationalError("本地清理结果事务提交完成情况未知")
        return self.connection.execute(statement, parameters)

    def __getattr__(self, name):
        return getattr(self.connection, name)


class _NoDeviceCalls(DeleteDriverPort):
    """原 QUERY 的本地续接不允许再派发设备删除或查询。"""

    async def delete(self, request):
        raise AssertionError("原 QUERY 的本地消费重新派发了 DELETE")

    async def query_state(self, request):
        raise AssertionError("原 QUERY 的本地消费重新派发了 QUERY")


def _history(owned):
    return (
        tuple(owned.connection.execute("SELECT * FROM history_transactions ORDER BY id")),
        tuple(owned.connection.execute("SELECT * FROM history_events ORDER BY id")),
        tuple(owned.connection.execute(
            "SELECT * FROM entity_event_links ORDER BY entity_type,entity_id,event_id")),
        tuple(owned.connection.execute(
            "SELECT * FROM report_entity_changes ORDER BY entity_type,entity_id,event_id")),
    )


def _assert_history_prefix(owned, prefix):
    current = _history(owned)
    assert current[0][:len(prefix[0])] == prefix[0]
    assert current[1][:len(prefix[1])] == prefix[1]
    # 两类实体目录按实体排序，后续合法事件可能插入其他实体之间。
    assert set(prefix[2]) <= set(current[2])
    assert set(prefix[3]) <= set(current[3])


def _query_facts(owned, item_id):
    run = owned.connection.execute(
        "SELECT id,attempts_used FROM operation_runs WHERE responsibility_key=?",
        (f"exists/{item_id}",),
    ).fetchone()
    assert run is not None
    attempts = tuple(owned.connection.execute(
        "SELECT * FROM operation_attempts WHERE run_id=? ORDER BY attempt_no", (run[0],)))
    return run, attempts


def _saved_key_count(owned, key):
    return owned.connection.execute(
        "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?", (str(key),),
    ).fetchone()[0]


class _MemberResults(OutputsRepository):
    """保存成员的真实仓储，同时记录完整输入并选择一次 COMMIT 故障。"""

    def __init__(self, item_id, method, after_commit):
        self.item_id = item_id
        self.method = method
        self.after_commit = after_commit
        self.calls = []
        self.results = []
        self.fault = None

    def _save(self, method, request, key, owned):
        if request.item_id != self.item_id:
            return getattr(super(), method)(request, key, owned)
        self.calls.append((method, request, key))
        actual_owned = owned
        if method == self.method and self.fault is None:
            self.fault = _CommitFault(owned.connection, self.after_commit)
            actual_owned = replace(owned, connection=self.fault)
        result = getattr(super(), method)(request, key, actual_owned)
        self.results.append(result)
        return result

    def finish_cleanup_item(self, request, key, owned):
        return self._save("finish_cleanup_item", request, key, owned)

    def cancel_cleanup_item(self, request, key, owned):
        return self._save("cancel_cleanup_item", request, key, owned)


class _LocalOperations(OperationRepository):
    """QUERY 正常提交；必要时在真实伴随收场事务注入一次 COMMIT 故障。"""

    def __init__(self, item_id, retry_gate, *, companion_fault=False, after_commit=False):
        self.item_id = item_id
        self.retry_gate = retry_gate
        self.companion_fault = companion_fault
        self.after_commit = after_commit
        self.query_facts = None
        self.query_prefix = None
        self.companion_calls = []
        self.companion_results = []
        self.companion_anchors = None
        self.fault = None

    def finish_attempt(self, request, key, owned):
        result = super().finish_attempt(request, key, owned)
        if (request.ticket.operation == "query"
                and request.ticket.target_id == str(self.item_id)
                and self.query_facts is None):
            assert result.kind is DbOutcomeKind.COMPLETED, result.error
            self.query_facts = _query_facts(owned, self.item_id)
            self.query_prefix = _history(owned)
        return result

    def finish_stale_runs(self, request, key, owned):
        self.companion_calls.append((request, key))
        actual_owned = owned
        if self.companion_fault and self.fault is None:
            self.companion_anchors = dict(self.retry_gate.anchors)
            self.fault = _CommitFault(owned.connection, self.after_commit)
            actual_owned = replace(owned, connection=self.fault)
        result = super().finish_stale_runs(request, key, actual_owned)
        self.companion_results.append(result)
        return result


async def _world(environment, consumer, present):
    cfg, owned, context, capture_driver = environment
    await _save_photos(cfg, owned, context, capture_driver)
    output_id = owned.connection.execute(
        "SELECT o.id FROM outputs o JOIN actions a ON a.id=o.source_action_id"
        " WHERE a.device_id='cam-a'",
    ).fetchone()[0]
    driver = _MatrixDriver(owned)
    old_member = None
    if consumer == "before":
        old_action, old_item = _start_cleanup(owned, output_id, "2")
        prior_runtime = _new_runtime(cfg, owned, driver, query_attempts=1)
        first = await delete_source_file(prior_runtime, old_item)
        assert first.phase == "query_unknown", first
        exhausted = await delete_source_file(prior_runtime, old_item)
        assert (exhausted.phase, exhausted.detail) == (
            "failed", "file_query_attempts_exhausted"), exhausted
        finished = OutputsRepository().finish_cleanup_action(
            FinishCleanupAction(old_action, _NOW), new_operation_key(), owned)
        assert finished.kind is DbOutcomeKind.COMPLETED, finished.error
        old_member = (old_action, old_item, _original_member_rows(owned, old_action, old_item))
        action_id, item_id = _start_cleanup(owned, output_id, "3")
    else:
        action_id, item_id = _start_cleanup(owned, output_id, "2")
    base = _new_runtime(cfg, owned, driver)
    driver.matrix_enabled, driver.present = True, present
    if consumer == "cancel":
        driver.on_delete_return = lambda: _apply_cleanup_cancel(owned, action_id)
    return base, driver, action_id, item_id, old_member


def _instrument(base, item_id, *, member_method=None, companion_fault=False, after_commit=False):
    members = _MemberResults(item_id, member_method, after_commit)
    operations = _LocalOperations(
        item_id, base.retry_gate, companion_fault=companion_fault, after_commit=after_commit)
    base.outputs, base.operations = members, operations
    return base.for_item(item_id), members, operations


async def _first_rejected(runtime, item_id, phases):
    try:
        step = await delete_source_file(runtime, item_id)
    except ConsistencyError:
        return
    assert step.phase in phases, step


def _fresh_runtime(runtime, owned, binding):
    # 原 for_item 和 binding_of 的装配闭包引用旧连接，不带到新一轮。
    return replace(
        runtime,
        owned=owned,
        driver=_NoDeviceCalls(),
        binding_of=lambda _item: binding,
        for_item=None,
        occurred_at=lambda: _NOW + 50_000_000,
        monotonic_ns=lambda: 1_000_000_000_000,
        delete_config=AttemptConfig(9, Decimal(20), Decimal(5)),
        query_config=AttemptConfig(9, Decimal(20), Decimal(5)),
    )


def _check_original_query(owned, operations, item_id):
    assert operations.query_facts is not None and operations.query_prefix is not None
    assert _query_facts(owned, item_id) == operations.query_facts
    assert operations.query_facts[0][1] == 1
    assert len(operations.query_facts[1]) == 1
    _assert_history_prefix(owned, operations.query_prefix)


def _check_old_member(owned, old_member):
    if old_member is not None:
        action_id, item_id, original = old_member
        assert _original_member_rows(owned, action_id, item_id) == original


@pytest.mark.parametrize("after_commit", [False, True], ids=["before-commit", "after-commit"])
@pytest.mark.parametrize("consumer", ["before", "after", "cancel"])
async def test_saved_absence_member_finish_interrupted(environment, consumer, after_commit):
    base, driver, action_id, item_id, old_member = await _world(environment, consumer, False)
    runtime, members, operations = _instrument(
        base, item_id, member_method="finish_cleanup_item", after_commit=after_commit)
    await _first_rejected(runtime, item_id, {"result_rejected"})
    assert members.fault is not None and members.fault.triggered
    assert len(members.calls) == 1
    assert members.results[0].kind is DbOutcomeKind.UNKNOWN
    _, first_request, first_key = members.calls[0]
    calls_before = (len(driver.deletes), len(driver.queries))
    context = environment[2]
    runtime.owned.connection.close()
    fresh = context.open_connection()
    try:
        original_member = _member_facts(fresh, item_id)
        assert original_member[0] == (4 if after_commit else 3)
        assert (original_member[4] is not None) is after_commit
        _check_original_query(fresh, operations, item_id)
        resumed = _fresh_runtime(runtime, fresh, driver.query_requests[-1].binding)

        step = await delete_source_file(resumed, item_id)

        assert step.phase in {"succeeded", "already_terminal"}, step
        assert len(members.calls) == 2
        _, second_request, second_key = members.calls[1]
        assert second_request is first_request
        assert second_key == first_key
        assert second_request.occurred_at == _NOW
        final_member = _member_facts(fresh, item_id)
        assert final_member[:4] == (4, 4, 3, None)
        assert final_member[4] is not None
        if after_commit:
            assert final_member == original_member
        assert _saved_key_count(fresh, first_key) == 1
        assert (len(driver.deletes), len(driver.queries)) == calls_before
        _check_original_query(fresh, operations, item_id)
        _check_old_member(fresh, old_member)
        assert f"exists/{item_id}" not in resumed.retry_gate.anchors
        assert f"delete/{item_id}" not in resumed.retry_gate.anchors
        if consumer == "cancel":
            assert fresh.connection.execute(
                "SELECT cancel_requested FROM actions WHERE id=?", (action_id,),
            ).fetchone() == (1,)
    finally:
        fresh.connection.close()


@pytest.mark.parametrize("after_commit", [False, True], ids=["before-commit", "after-commit"])
@pytest.mark.parametrize("present", [True, None], ids=["present", "unknown"])
async def test_saved_cancel_observation_member_finish_interrupted(environment, present, after_commit):
    base, driver, action_id, item_id, _ = await _world(environment, "cancel", present)
    runtime, members, operations = _instrument(
        base, item_id, member_method="cancel_cleanup_item", after_commit=after_commit)
    await _first_rejected(runtime, item_id, {"cancel_rejected"})
    assert members.fault is not None and members.fault.triggered
    assert len(members.calls) == 1
    assert members.results[0].kind is DbOutcomeKind.UNKNOWN
    _, first_request, first_key = members.calls[0]
    calls_before = (len(driver.deletes), len(driver.queries))
    runtime.owned.connection.close()
    fresh = environment[2].open_connection()
    try:
        original_member = _member_facts(fresh, item_id)
        assert original_member[0] == (6 if after_commit else 3)
        _check_original_query(fresh, operations, item_id)
        resumed = _fresh_runtime(runtime, fresh, driver.query_requests[-1].binding)

        step = await delete_source_file(resumed, item_id)

        assert step.phase in {"canceled", "already_terminal"}, step
        assert len(members.calls) == 2
        _, second_request, second_key = members.calls[1]
        assert second_request is first_request
        assert second_key == first_key
        assert second_request.occurred_at == _NOW
        assert second_request.code == ("file_delete_failed" if present else "delete_unconfirmed")
        final_member = _member_facts(fresh, item_id)
        assert final_member[0] == 6 and final_member[4] is not None
        if after_commit:
            assert final_member == original_member
        assert _saved_key_count(fresh, first_key) == 1
        assert (len(driver.deletes), len(driver.queries)) == calls_before
        _check_original_query(fresh, operations, item_id)
        assert fresh.connection.execute(
            "SELECT cancel_requested FROM actions WHERE id=?", (action_id,),
        ).fetchone() == (1,)
        assert f"exists/{item_id}" not in resumed.retry_gate.anchors
        assert f"delete/{item_id}" not in resumed.retry_gate.anchors
    finally:
        fresh.connection.close()


@pytest.mark.parametrize("after_commit", [False, True], ids=["before-commit", "after-commit"])
@pytest.mark.parametrize("consumer,present", [
    pytest.param("after", False, id="succeeded-member"),
    pytest.param("cancel", None, id="canceled-member"),
])
async def test_terminal_member_companion_finish_interrupted(environment, consumer, present, after_commit):
    base, driver, _, item_id, _ = await _world(environment, consumer, present)
    runtime, members, operations = _instrument(
        base, item_id, companion_fault=True, after_commit=after_commit)
    await _first_rejected(runtime, item_id, {"companion_rejected"})
    assert operations.fault is not None and operations.fault.triggered
    assert len(members.calls) == 1 and members.results[0].kind is DbOutcomeKind.COMPLETED
    assert len(operations.companion_calls) == 1
    assert operations.companion_results[0].kind is DbOutcomeKind.UNKNOWN
    first_request, first_key = operations.companion_calls[0]
    assert runtime.retry_gate.anchors == operations.companion_anchors
    calls_before = (len(driver.deletes), len(driver.queries))
    runtime.owned.connection.close()
    fresh = environment[2].open_connection()
    try:
        original_member = _member_facts(fresh, item_id)
        assert original_member[0] == (4 if present is False else 6)
        assert original_member[4] is not None
        before_recovery = _history(fresh)
        resumed = _fresh_runtime(runtime, fresh, driver.query_requests[-1].binding)

        step = await delete_source_file(resumed, item_id)

        assert step.phase in {"succeeded", "canceled", "already_terminal"}, step
        assert len(members.calls) == 1
        assert len(operations.companion_calls) == 2
        second_request, second_key = operations.companion_calls[1]
        assert second_request is first_request
        assert second_key == first_key
        assert second_request.occurred_at == _NOW
        assert _member_facts(fresh, item_id) == original_member
        assert _saved_key_count(fresh, first_key) == 1
        if after_commit:
            assert _history(fresh) == before_recovery
        else:
            _assert_history_prefix(fresh, before_recovery)
        assert (len(driver.deletes), len(driver.queries)) == calls_before
        _check_original_query(fresh, operations, item_id)
        assert f"exists/{item_id}" not in resumed.retry_gate.anchors
        assert f"delete/{item_id}" not in resumed.retry_gate.anchors
    finally:
        fresh.connection.close()

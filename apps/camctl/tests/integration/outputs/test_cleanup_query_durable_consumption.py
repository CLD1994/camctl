"""全新清理会话从持久化原 QUERY 结果继续本地消费。

原运行完成 QUERY 保存后中断；新工厂不接收旧 runtime 或待存集合。
已提交结果不变，未知观察只建立一次本次运行的等待，后续可能删除
会使较旧的在场查询失去直接消费资格。
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.bootstrap.cleanup_assembly import session_cleanup_assembly
from camctl.contracts.workflow_errors import item_error_id
from camctl.outputs.cleanup_flow import delete_source_file
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository

from .test_cleanup_query_local_consumption import (
    _CommitFault, _NoDeviceCalls, _assert_history_prefix, _check_old_member,
    _first_rejected, _history, _instrument, _query_facts, _saved_key_count, _world,
)
from .test_cleanup_query_save_recovery import _MatrixDriver, _member_facts
from ..bootstrap.test_output_binding_changes import _NOW, _registry, environment


pytestmark = pytest.mark.asyncio


class _Clock:
    def __init__(self, ns=10_000_000_000):
        self.ns = ns

    def __call__(self):
        return self.ns


def _new_factory(cfg, driver, clock, *, query_interval=Decimal(3)):
    devices = {
        identity: {
            **declaration,
            "cleanup": {
                **declaration.get("cleanup", {}),
                "query_retry_interval_s": query_interval,
            },
        }
        for identity, declaration in cfg.devices.items()
    }
    return session_cleanup_assembly(
        devices=devices,
        drivers=_registry(driver),
        max_delete_attempts=3,
        max_query_attempts=3,
        staging=Path(cfg.paths.staging),
        occurred_at=lambda: _NOW + 50_000_000,
        monotonic_ns=clock,
    )


class _OnlyNewQuery(_MatrixDriver):
    """允许到时的合法新 QUERY，任何额外 DELETE 都是测试失败。"""

    def __init__(self, owned):
        super().__init__(owned)
        self.matrix_enabled = True
        self.present = False
        self.allow_query = False

    async def delete(self, request):
        raise AssertionError("持久化 QUERY 的恢复又派发了 DELETE")

    async def query_state(self, request):
        if not self.allow_query:
            raise AssertionError("原 QUERY 本地交接或间隔等待又查询了设备")
        return await super().query_state(request)


class _QueryIntentCommitFault(OperationRepository):
    """后续 DELETE 已可靠返回后，使新的 QUERY 意图在 COMMIT 前中断。"""

    def __init__(self):
        self.fault = None
        self.result = None

    def begin_attempt(self, intent, key, owned):
        if intent.operation == "query" and self.fault is None:
            self.fault = _CommitFault(owned.connection, after_commit=False)
            self.result = super().begin_attempt(intent, key, replace(owned, connection=self.fault))
            return self.result
        return super().begin_attempt(intent, key, owned)


@pytest.mark.parametrize("consumer,present,method", [
    pytest.param("before", False, "finish_cleanup_item", id="before-absent"),
    pytest.param("after", False, "finish_cleanup_item", id="after-absent"),
    pytest.param("cancel", False, "finish_cleanup_item", id="cancel-absent"),
    pytest.param("cancel", True, "cancel_cleanup_item", id="cancel-present"),
    pytest.param("cancel", None, "cancel_cleanup_item", id="cancel-unknown"),
])
async def test_new_factory_consumes_original_saved_query_without_memory(environment, consumer, present, method):
    cfg, _, context, _ = environment
    base, driver, action_id, item_id, old_member = await _world(environment, consumer, present)
    runtime, members, operations = _instrument(
        base, item_id, member_method=method, after_commit=False)
    await _first_rejected(runtime, item_id, {"result_rejected", "cancel_rejected"})
    assert members.fault is not None and members.fault.triggered
    assert members.results[0].kind is DbOutcomeKind.UNKNOWN
    _, _, abandoned_key = members.calls[0]
    original_query = operations.query_facts
    original_prefix = operations.query_prefix
    calls_before = (len(driver.deletes), len(driver.queries))
    runtime.owned.connection.close()
    fresh = context.open_connection()
    try:
        assert _member_facts(fresh, item_id)[0] == 3
        assert _saved_key_count(fresh, abandoned_key) == 0
        assert _query_facts(fresh, item_id) == original_query
        # 新工厂只接收本次设备配置与连接，不传原集合或旧 runtime。
        factory = _new_factory(cfg, _NoDeviceCalls(), _Clock())
        resumed = factory(fresh)

        step = await delete_source_file(resumed, item_id)

        member = _member_facts(fresh, item_id)
        if present is False:
            assert step.phase == "succeeded", step
            assert member[:4] == (4, 4, 3, None)
        else:
            code = "file_delete_failed" if present is True else "delete_unconfirmed"
            assert (step.phase, step.detail) == ("canceled", code), step
            assert member[:4] == (6, 4, None, item_error_id("cleanup_items", code))
        assert member[4] is not None
        assert (len(driver.deletes), len(driver.queries)) == calls_before
        assert _query_facts(fresh, item_id) == original_query
        _assert_history_prefix(fresh, original_prefix)
        _check_old_member(fresh, old_member)
        assert f"exists/{item_id}" not in resumed.retry_gate.anchors
        assert f"delete/{item_id}" not in resumed.retry_gate.anchors
        if consumer == "cancel":
            assert fresh.connection.execute(
                "SELECT cancel_requested FROM actions WHERE id=?", (action_id,),
            ).fetchone() == (1,)
    finally:
        fresh.connection.close()


async def test_saved_unknown_is_consumed_once_and_wait_anchor_survives_fresh_rounds(environment):
    cfg, _, context, _ = environment
    base, old_driver, _, item_id, _ = await _world(environment, "after", None)
    first_runtime = base.for_item(item_id)
    first = await delete_source_file(first_runtime, item_id)
    assert first.phase == "query_unknown", first
    original_query = _query_facts(first_runtime.owned, item_id)
    original_prefix = _history(first_runtime.owned)
    old_calls = (len(old_driver.deletes), len(old_driver.queries))
    first_runtime.owned.connection.close()

    fresh = context.open_connection()
    clock = _Clock()
    driver = _OnlyNewQuery(fresh)
    factory = _new_factory(cfg, driver, clock, query_interval=Decimal(3))
    try:
        recovered_runtime = factory(fresh)
        recovered = await delete_source_file(recovered_runtime, item_id)
        assert recovered.phase == "query_retry_wait", recovered
        first_anchor = recovered_runtime.retry_gate.anchors[f"exists/{item_id}"]
        assert _query_facts(fresh, item_id) == original_query
        assert _history(fresh) == original_prefix
        assert driver.queries == [] and driver.deletes == []
        fresh.connection.close()

        clock.ns = 12_000_000_000
        fresh = context.open_connection()
        waiting_runtime = factory(fresh)
        waiting = await delete_source_file(waiting_runtime, item_id)
        assert waiting.phase == "query_retry_wait", waiting
        assert waiting_runtime.retry_gate.anchors[f"exists/{item_id}"] == first_anchor
        assert _query_facts(fresh, item_id) == original_query
        assert _history(fresh) == original_prefix
        assert driver.queries == [] and driver.deletes == []
        fresh.connection.close()

        clock.ns = 13_000_000_000
        driver.allow_query = True
        fresh = context.open_connection()
        due_runtime = factory(fresh)
        due = await delete_source_file(due_runtime, item_id)
        assert due.phase == "succeeded", due
        assert len(driver.queries) == 1 and driver.deletes == []
        assert (len(old_driver.deletes), len(old_driver.queries)) == old_calls
        current_run, current_attempts = _query_facts(fresh, item_id)
        assert current_run == (original_query[0][0], 2)
        assert len(current_attempts) == 2
        assert current_attempts[0] == original_query[1][0]
        _assert_history_prefix(fresh, original_prefix)
        assert _member_facts(fresh, item_id)[0] == 4
    finally:
        fresh.connection.close()


async def test_query_before_later_possible_delete_is_not_recovered_as_latest_observation(environment):
    cfg, _, context, _ = environment
    base, old_driver, _, item_id, _ = await _world(environment, "after", True)
    runtime = base.for_item(item_id)
    first = await delete_source_file(runtime, item_id)
    assert first.phase == "still_present", first
    original_query = _query_facts(runtime.owned, item_id)
    original_prefix = _history(runtime.owned)
    # 同一成员在删除间隔到时后真正发出第二次 DELETE，仍返回未知。
    # 紧随其后的 QUERY 意图在真实 COMMIT 前失败，关闭连接回滚该意图。
    fault_port = _QueryIntentCommitFault()
    base.operations = fault_port
    base.monotonic_ns = lambda: 10_000_000_000
    again = await delete_source_file(base.for_item(item_id), item_id)
    assert again.phase == "query_rejected", again
    assert fault_port.fault is not None and fault_port.fault.triggered
    assert fault_port.result.kind is DbOutcomeKind.UNKNOWN
    assert len(old_driver.deletes) == 2 and len(old_driver.queries) == 1
    runtime.owned.connection.close()

    fresh = context.open_connection()
    try:
        assert _query_facts(fresh, item_id) == original_query
        latest_delete, old_query_intent = fresh.connection.execute(
            "SELECT MAX(CASE WHEN r.kind=4 THEN a.intent_event_id END),"
            " MAX(CASE WHEN r.kind=5 THEN a.intent_event_id END)"
            " FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id"
            " WHERE r.cleanup_item_id=?", (item_id,),
        ).fetchone()
        assert latest_delete > old_query_intent
        driver = _OnlyNewQuery(fresh)
        driver.allow_query = True
        resumed = _new_factory(cfg, driver, _Clock())(fresh)

        step = await delete_source_file(resumed, item_id)

        assert step.phase == "succeeded", step
        assert len(driver.queries) == 1 and driver.deletes == []
        current_run, current_attempts = _query_facts(fresh, item_id)
        assert current_run == (original_query[0][0], 2)
        assert current_attempts[0] == original_query[1][0]
        _assert_history_prefix(fresh, original_prefix)
        assert _member_facts(fresh, item_id)[0] == 4
    finally:
        fresh.connection.close()


@pytest.mark.parametrize("binding", ["removed", "changed", "unregistered"])
async def test_saved_absence_finishes_before_current_binding_or_driver_resolution(environment, binding):
    cfg, _, context, _ = environment
    base, _, _, item_id, _ = await _world(environment, "after", False)
    runtime, _, operations = _instrument(
        base, item_id, member_method="finish_cleanup_item", after_commit=False)
    await _first_rejected(runtime, item_id, {"result_rejected"})
    original_query = operations.query_facts
    runtime.owned.connection.close()
    devices = dict(cfg.devices)
    if binding == "removed":
        del devices["cam-a"]
    else:
        devices["cam-a"] = {**devices["cam-a"], "driver": "unregistered-driver"}
        if binding == "changed":
            devices["cam-a"] = {"driver": "different-driver"}
    fresh = context.open_connection()
    try:
        changed = replace(cfg, devices=devices)
        resumed = _new_factory(changed, _NoDeviceCalls(), _Clock())(fresh)
        step = await delete_source_file(resumed, item_id)
        assert step.phase == "succeeded", step
        assert _member_facts(fresh, item_id)[0] == 4
        assert _query_facts(fresh, item_id) == original_query
    finally:
        fresh.connection.close()

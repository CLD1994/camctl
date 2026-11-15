"""X8 源产物清理删除链的组件集成测试。

真实目标固定、限制建立、删除意图（operations 删除流程独立预算）、
契约替身设备调用与结果事务组合：可靠删除直接成功并同事务保存文件
缺席、产物 CLEANED 与成员终态；效果未知只能用查询预算核实；查询
确认仍在时等待预算内重试；删除预算耗尽按公共错误终态。
"""

from __future__ import annotations

import json
from time import monotonic_ns as _default_monotonic_ns

import pytest
import pytest_asyncio

from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.ports import DeviceCallResult
from camctl.outputs.cleanup_flow import (
    delete_source_file,
)

from ..bootstrap.test_output_binding_changes import (
    Catalog, _NOW, _save_photos, environment,  # noqa: F401
)
from ..support.outputs import cleanup_runtime, prepare_cleanup_member


_EVIDENCE = EvidenceRegistry(
    (
        EvidenceContract(type="delete_returned", version=1, operation="delete",
                         fields=frozenset()),
        EvidenceContract(type="file_absent", version=1, operation="delete",
                         fields=frozenset({"cleanup_item_id"}),
                         identity_field="cleanup_item_id"),
        EvidenceContract(type="file_presence", version=1, operation="query",
                         fields=frozenset({"cleanup_item_id", "present"}),
                         identity_field="cleanup_item_id"),
    )
)


class DriverDouble:
    """契约替身：按编排返回删除/查询观察。"""

    def __init__(self, *, absent=None, delete_error=None, present=None,
                 query_error=None) -> None:
        self.absent = absent
        self.delete_error = delete_error
        self.present = present
        self.query_error = query_error
        self.delete_calls = 0
        self.query_calls = 0

    async def delete(self, request) -> DeviceCallResult:
        self.delete_calls += 1
        observations = ()
        if self.absent:
            observations = (
                DeviceObservation(
                    type="file_absent", version=1,
                    data={"cleanup_item_id": request.params["cleanup_item_id"]}),
            )
        return DeviceCallResult(
            observations=observations, error=self.delete_error)

    async def query_file(self, request) -> DeviceCallResult:
        self.query_calls += 1
        observations = ()
        if self.present is not None:
            observations = (
                DeviceObservation(
                    type="file_presence", version=1,
                    data={"cleanup_item_id": request.params["cleanup_item_id"], "present": self.present}),
            )
        return DeviceCallResult(
            observations=observations, error=self.query_error)


@pytest_asyncio.fixture
async def pipeline(environment):
    cfg, owned, context, capture_driver = environment
    await _save_photos(cfg, owned, context, capture_driver)
    output_id, file_id = owned.connection.execute(
        "SELECT id,device_file_id FROM outputs ORDER BY id LIMIT 1").fetchone()
    _action_id, item_id = prepare_cleanup_member(
        owned, catalog=Catalog(), now=_NOW, output_id=output_id, request_id="2")
    yield owned, context.database, output_id, file_id, item_id


async def _runtime(database, driver, item_id, *, delete_attempts=1, query_attempts=1,
                   monotonic_ns=_default_monotonic_ns):
    return await cleanup_runtime(
        database, driver, _EVIDENCE, item_id=item_id, device_id="cam-a",
        delete_attempts=delete_attempts, query_attempts=query_attempts,
        occurred_at=lambda: _NOW + 10, monotonic_ns=monotonic_ns,
    )


def _value(owned, sql: str, *params):
    row = owned.connection.execute(sql, params).fetchone()
    assert row is not None, f"查询无结果: {sql}"
    return row


pytestmark = pytest.mark.asyncio


class TestDeletionChain:
    async def test_confirmed_delete_succeeds_with_cleaned_projection(self, pipeline):
        owned, database, output_id, file_id, item_id = pipeline
        driver = DriverDouble(absent=True)
        step = await delete_source_file(await _runtime(database, driver, item_id), item_id)
        assert step.phase == "succeeded" and step.detail == "DELETED", step
        assert _value(
            owned, "SELECT status, restriction_state, outcome, error_code"
            " FROM cleanup_items WHERE id = ?", item_id) == (4, 4, 1, None)
        assert _value(
            owned, "SELECT availability, cleanup_status FROM outputs"
            " WHERE id = ?", output_id) == (3, 4)
        assert _value(
            owned, "SELECT presence_state FROM device_files WHERE id = ?", file_id) == (3,)
        # 删除流程一次尝试；查询流程未动。
        assert driver.delete_calls == 1 and driver.query_calls == 0
        assert _value(
            owned, "SELECT COUNT(*) FROM operation_runs"
            " WHERE responsibility_key = ?", f"delete/{item_id}") == (1,)

    async def test_unknown_delete_confirmed_absent_by_query(self, pipeline):
        owned, database, output_id, file_id, item_id = pipeline
        driver = DriverDouble(absent=False, present=False)
        step = await delete_source_file(await _runtime(database, driver, item_id), item_id)
        assert step.phase == "succeeded" and step.detail == "ABSENCE_CONFIRMED", step
        assert _value(
            owned, "SELECT status, outcome FROM cleanup_items WHERE id = ?", item_id
        ) == (4, 3)
        assert driver.delete_calls == 1 and driver.query_calls == 1
        # 两个流程各自一次尝试：预算独立。
        assert _value(
            owned, "SELECT COUNT(*) FROM operation_runs"
            " WHERE responsibility_key IN (?, ?)", f"delete/{item_id}", f"exists/{item_id}") == (2,)

    async def test_unknown_delete_with_file_still_present_waits(self, pipeline):
        owned, database, output_id, file_id, item_id = pipeline
        driver = DriverDouble(absent=False, present=True)
        clock = {"ns": _default_monotonic_ns()}
        runtime = await _runtime(
            database, driver, item_id, delete_attempts=2,
            monotonic_ns=lambda: clock["ns"])
        step = await delete_source_file(runtime, item_id)
        assert step.phase == "still_present", step
        # 成员保持删除中，不因文件仍在判定失败。
        assert _value(
            owned, "SELECT status FROM cleanup_items WHERE id = ?", item_id) == (3,)
        # 未到原间隔不得消费第二次删除预算。
        waiting = await delete_source_file(runtime, item_id)
        assert waiting.phase == "delete_retry_wait", waiting
        assert driver.delete_calls == 1
        # 预算内重试：删除间隔到时后再次推进发起第二次删除并成功。
        driver.absent = True
        clock["ns"] += 1_000_000_000
        retry = await delete_source_file(runtime, item_id)
        assert retry.phase == "succeeded", retry
        assert driver.delete_calls == 2

    async def test_delete_budget_exhaustion_fails_with_registered_error(
            self, pipeline):
        owned, database, output_id, file_id, item_id = pipeline
        driver = DriverDouble(absent=False, present=True)
        # 第一次：删除效果未知，查询确认仍在——等待预算内重试。
        first = await delete_source_file(
            await _runtime(database, driver, item_id, delete_attempts=1, query_attempts=1), item_id)
        assert first.phase == "still_present", first
        # 重试进入删除：删除预算耗尽按公共错误终态失败；查询预算独立
        # 不受影响（未确认缺席不以删除代替核实）。
        second = await delete_source_file(
            await _runtime(database, driver, item_id, delete_attempts=1, query_attempts=1), item_id)
        assert second.phase == "failed", second
        assert second.detail == "delete_attempts_exhausted", second
        row = _value(
            owned, "SELECT status, error_code, error_details_json"
            " FROM cleanup_items WHERE id = ?", item_id)
        assert row[0] == 5
        assert row[1] == 2
        assert json.loads(row[2])["output_id"] == str(output_id)

    async def test_query_budget_exhaustion_fails_without_another_delete(self, pipeline):
        owned, database, output_id, _file_id, item_id = pipeline
        driver = DriverDouble(absent=False, query_error={"code": "source_query_failed"})
        runtime = await _runtime(database, driver, item_id, delete_attempts=1, query_attempts=1)
        first = await delete_source_file(runtime, item_id)
        assert first.phase == "query_unknown", first
        second = await delete_source_file(runtime, item_id)
        assert (second.phase, second.detail) == ("failed", "file_query_attempts_exhausted")
        assert (driver.delete_calls, driver.query_calls) == (1, 1)
        row = _value(owned, "SELECT status,error_details_json FROM cleanup_items WHERE id=?", item_id)
        assert row[0] == 5 and json.loads(row[1])["output_id"] == str(output_id)

    async def test_terminal_member_is_idempotent(self, pipeline):
        owned, database, output_id, file_id, item_id = pipeline
        driver = DriverDouble(absent=True)
        await delete_source_file(await _runtime(database, driver, item_id), item_id)
        again = await delete_source_file(await _runtime(database, driver, item_id), item_id)
        assert again.phase == "already_terminal", again
        assert driver.delete_calls == 1
        assert _value(owned, "SELECT COUNT(*) FROM outputs") == (2,)

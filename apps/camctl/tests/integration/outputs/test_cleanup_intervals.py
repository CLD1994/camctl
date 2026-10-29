"""清理删除与查询重试间隔时间强制的组件集成测试。

真实仓储、操作预算与受控单调钟组合：删除效果未知或查询失败保存
重试等待后，按本次配置的间隔到时才允许下一次尝试——未到时不提交
新意图、不消耗次数；预算耗尽即时交由意图事务按耗尽收场。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from camctl.outputs.cleanup_flow import delete_source_file

from .test_cleanup_recovery import (
    _BINDING,
    _EVIDENCE,
    _NOW,
    DriverDouble,
    _seed_pending_delete,
    _value,
    pipeline,  # noqa: F401  环境夹具
)

from ..support.capture import capture_database
from ..support.outputs import cleanup_runtime

pytestmark = pytest.mark.asyncio


class _Clock:
    """受控单调钟：按秒推进读数。"""

    def __init__(self, ns: int = 5_000_000_000) -> None:
        self.ns = ns

    def __call__(self) -> int:
        return self.ns

    def advance_s(self, seconds: Decimal) -> None:
        self.ns += int(Decimal(seconds) * 1_000_000_000)


async def _runtime(owned, driver, clock: _Clock, *,
                   delete_interval=Decimal("3"), query_interval=Decimal("3"),
                   delete_attempts=3, query_attempts=3):
    database = await capture_database(owned)
    return await cleanup_runtime(
        database, driver, _EVIDENCE, item_id=91,
        delete_attempts=delete_attempts, query_attempts=query_attempts,
        delete_interval=delete_interval, query_interval=query_interval,
        occurred_at=lambda: _NOW + 10, monotonic_ns=clock,
    )


def _attempts(owned, responsibility: str):
    """该责任当前保存的累计尝试次数。"""
    return _value(
        owned,
        "SELECT attempts_used FROM operation_runs"
        " WHERE responsibility_key = ?",
        responsibility)[0]


class TestDeleteInterval:
    async def test_unknown_delete_waits_interval_before_next_attempt(
            self, pipeline) -> None:
        owned = pipeline
        _seed_pending_delete(owned)
        try:
            driver = DriverDouble(absent=False, present=True)
            clock = _Clock()
            runtime = await _runtime(owned, driver, clock, delete_interval=Decimal("3"))
            first = await delete_source_file(runtime, 91)
            # 删除效果未知且查询确认仍在：等待预算内重试。
            assert first.phase == "still_present", first
            assert driver.delete_calls == 1
            assert _attempts(owned, "delete/91") == 1
            clock.advance_s(Decimal("1"))
            second = await delete_source_file(runtime, 91)
            # 间隔未到：不提交新的删除意图。
            assert second.phase == "delete_retry_wait", second
            assert driver.delete_calls == 1
            assert _attempts(owned, "delete/91") == 1
            clock.advance_s(Decimal("2"))
            third = await delete_source_file(runtime, 91)
            # 到时：按剩余预算再次删除。
            assert third.phase == "still_present", third
            assert driver.delete_calls == 2
            assert _attempts(owned, "delete/91") == 2
        finally:
            owned.connection.close()

    async def test_zero_delete_interval_retries_immediately(
            self, pipeline) -> None:
        owned = pipeline
        _seed_pending_delete(owned)
        try:
            driver = DriverDouble(absent=False, present=True)
            runtime = await _runtime(
                owned, driver, _Clock(), delete_interval=Decimal("0"),
                query_interval=Decimal("0"))
            first = await delete_source_file(runtime, 91)
            second = await delete_source_file(runtime, 91)
            assert first.phase == second.phase == "still_present"
            assert driver.delete_calls == 2
        finally:
            owned.connection.close()


class TestQueryInterval:
    async def test_unknown_query_waits_interval_before_next_attempt(
            self, pipeline) -> None:
        owned = pipeline
        _seed_pending_delete(owned)
        try:
            driver = DriverDouble(absent=False, query_error={"code": "device_error", "stage": "query"})
            clock = _Clock()
            runtime = await _runtime(owned, driver, clock, query_interval=Decimal("3"))
            first = await delete_source_file(runtime, 91)
            # 查询失败：保存重试等待，本次不判定失败。
            assert first.phase == "query_unknown", first
            assert driver.query_calls == 1
            clock.advance_s(Decimal("1"))
            second = await delete_source_file(runtime, 91)
            # 间隔未到：未决删除先核实，但查询间隔未到不提交新意图。
            assert second.phase == "query_retry_wait", second
            assert driver.query_calls == 1
            clock.advance_s(Decimal("2"))
            third = await delete_source_file(runtime, 91)
            assert third.phase == "query_unknown", third
            assert driver.query_calls == 2
        finally:
            owned.connection.close()

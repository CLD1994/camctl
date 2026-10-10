"""拍摄流程把状态前提失效传给会话，已保存设备失败仍为局部结果。"""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from camctl.bootstrap.flows import capture_flow
from camctl.bootstrap.background_flow import CombinedLocalWork
from camctl.session.service import _drive_flows

from ..capture.test_capture_contract import (
    DriverDouble, _entry, _environment, _runtime, _NOW,
)
from camctl.capture.results import FileKind
from ..operations.test_result_reuse import _FaultConnection
from .test_binding_changes import _RecoveryDriver, _recovering_runtime, environment
from .test_binding_transactions import _unknown_owned


class _FlowConnection:
    """保持真实连接可由测试核对，业务流仅关闭本轮借用包装。"""

    def __init__(self, connection, fail_prefix):
        self._connection = connection
        self._fault = _FaultConnection(connection, fail_prefix)

    def execute(self, sql, parameters=()):
        return self._fault.execute(sql, parameters)

    @property
    def in_transaction(self):
        return self._connection.in_transaction

    def close(self):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["query", "projection"])
async def test_capture_state_failure_stops_later_device_flow(environment, fault):
    _, _, home = environment
    directory = home / "failure"
    owned, _ = _unknown_owned(directory, False)
    next_calls = []
    driver = _RecoveryDriver()
    from ..capture.test_capture_contract import ResultsDouble

    prefix = ("SELECT 1 FROM operation_attempts t JOIN operation_runs r ON r.id = t.run_id"
              if fault == "query" else "UPDATE operation_runs")
    wrapped = replace(owned, connection=_FlowConnection(owned.connection, prefix))

    async def next_device(context):
        next_calls.append("called")

    context = SimpleNamespace(
        open_connection=lambda: wrapped, clock=SimpleNamespace(utc_micros=lambda: _NOW),
        flows={"capture": capture_flow(lambda connection, device: _recovering_runtime(
            connection, directory, "missing", driver, ResultsDouble({}))),
               "next": next_device},
    )
    context.local_work = CombinedLocalWork((context.flows["capture"],))
    try:
        before = tuple(owned.connection.iterdump())

        fatal = await _drive_flows(context)

        assert fatal is not None
        assert next_calls == []
        assert driver.calls == []
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_saved_device_failure_allows_other_device_work(environment):
    _, _, home = environment
    directory = home / "ordinary"
    directory.mkdir()
    owned = _environment(directory, ((11, 1),))
    next_calls = []
    driver = DriverDouble(error={"code": "device_error"})
    wrapped = replace(owned, connection=_FlowConnection(owned.connection, "NEVER MATCH"))

    async def next_device(context):
        next_calls.append("called")

    context = SimpleNamespace(
        open_connection=lambda: wrapped, clock=SimpleNamespace(utc_micros=lambda: _NOW),
        flows={"capture": capture_flow(lambda connection, device: _runtime(
            connection, driver=driver, files={11: (_entry("photo", kind=FileKind.PHOTO),)})),
               "next": next_device},
    )
    context.local_work = CombinedLocalWork((context.flows["capture"],))
    try:
        fatal = await _drive_flows(context)
        await context.local_work.settle()

        assert fatal is None
        assert next_calls == ["called"]
        assert driver.calls == ["take_photo"]
        assert owned.connection.execute("SELECT status FROM actions WHERE id = 11").fetchone() == (4,)
    finally:
        owned.connection.close()

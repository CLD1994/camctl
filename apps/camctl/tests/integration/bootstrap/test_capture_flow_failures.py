"""实际会话区别状态库失败与已经可靠保存的设备失败。"""

import sqlite3
from types import SimpleNamespace

import pytest

from camctl.bootstrap.flows import capture_flow
from camctl.capture.handlers import capture_handler
from camctl.operations.models import ErrorValue

from ..capture.test_recording_start_runtime import _result
from ..support.bootstrap_capture_cases import bootstrap_case, run_flow_round
from ..support.capture import capture_assembly

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("fault", ["query", "projection"])
async def test_capture_state_failure_stops_later_device_flow(tmp_path, bootstrap_case, fault):
    case = bootstrap_case
    runtime = await case.world(tmp_path, control=_result())
    await capture_handler(case.handler)(case.action_id, runtime)
    case.factory = capture_assembly(**{**case.options, "devices": {}})
    case.fault.table = "operation_attempts" if fault == "query" else "operation_runs"
    case.fault.operation = sqlite3.SQLITE_READ if fault == "query" else sqlite3.SQLITE_UPDATE
    case.fault.enabled = True
    flow = capture_flow(case.factory_view)
    following, snapshots = [], []

    async def capture(context):
        before = tuple(case.owned.connection.iterdump())
        try:
            await flow(context)
            await flow.settle()
        finally:
            snapshots.append((before, tuple(case.owned.connection.iterdump())))

    async def next_device(context):
        following.append(context)

    outcome = await run_flow_round(case.path,
        clock=SimpleNamespace(utc_micros=case.clock.wall_us), catalog=case.catalog,
        open_database=case.new_database, flows={"capture": capture, "next": next_device})
    assert outcome.reason == "state_db_error" and case.fault.hits
    assert following == [] and case.driver.control.await_count == 1
    assert len(snapshots) == 1 and snapshots[0][0] == snapshots[0][1]


async def test_saved_device_failure_allows_other_device_work(tmp_path, bootstrap_case):
    case = bootstrap_case
    await case.world(tmp_path, "photo", control=_result(no_effect=True,
        error=ErrorValue("transport_timeout", "transport")))
    following = []
    flow = capture_flow(case.factory_view)

    async def capture(context):
        await flow(context)
        await flow.settle()

    async def next_device(context):
        following.append(context)

    outcome = await run_flow_round(case.path,
        clock=SimpleNamespace(utc_micros=case.clock.wall_us), catalog=case.catalog,
        open_database=case.new_database, flows={"capture": capture, "next": next_device})
    assert outcome is None and len(following) == 1
    assert case.driver.control.await_count == 1
    assert case.owned.connection.execute("SELECT status FROM actions WHERE id=?",
        (case.action_id,)).fetchone() == (4,)

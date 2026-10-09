"""设备清理按删除核实阶段计时，并在新会话采用本次尝试上限。"""

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.outputs.cleanup_flow import delete_source_file
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from .test_cleanup_intervals import _Clock, _attempts, _runtime
from .test_cleanup_recovery import (
    DriverDouble,
    _seed_pending_delete,
    pipeline,  # noqa: F401  环境夹具
)

pytestmark = pytest.mark.asyncio


class _FirstQueryTakesFiveSeconds(DriverDouble):
    """只有首次存在性查询消耗五秒，其余调用即时返回。"""

    def __init__(self, clock):
        super().__init__(absent=False, present=True)
        self.clock = clock

    async def query_state(self, request):
        if self.query_calls == 0:
            self.clock.advance_s(Decimal("5"))
        return await super().query_state(request)


def _history(owned):
    return owned.connection.execute(
        "SELECT * FROM history_events ORDER BY id").fetchall()


def _assert_history_prefix(owned, prefix):
    assert _history(owned)[:len(prefix)] == prefix


def _run(owned, responsibility):
    row = owned.connection.execute(
        "SELECT id, attempts_used, status, retry_wait_required, max_attempts_used"
        " FROM operation_runs WHERE responsibility_key = ?",
        (responsibility,)).fetchone()
    assert row is not None
    return row


def _reopen(owned):
    """关闭原连接，再可靠打开同一状态库并核对部署身份。"""
    metadata = owned.metadata
    path = owned.connection.execute("PRAGMA database_list").fetchone()[2]
    owned.connection.close()
    fresh = open_existing(Path(path), DbOpenMode.EXISTING_RW, DbConfig())
    assert fresh.metadata == metadata
    return fresh


async def test_delete_interval_starts_after_required_query_confirms_presence(pipeline):
    owned = pipeline
    _seed_pending_delete(owned)
    clock = _Clock()
    driver = _FirstQueryTakesFiveSeconds(clock)
    runtime = _runtime(
        owned, driver, clock, delete_interval=Decimal("3"),
        query_interval=Decimal("0"))

    first = await delete_source_file(runtime, 91)
    assert first.phase == "still_present", first
    assert (driver.delete_calls, driver.query_calls) == (1, 1)
    assert _attempts(owned, "delete/91") == 1

    waiting = await delete_source_file(runtime, 91)
    assert waiting.phase == "delete_retry_wait", waiting
    assert driver.delete_calls == 1
    assert _attempts(owned, "delete/91") == 1

    clock.advance_s(Decimal("3"))
    retried = await delete_source_file(runtime, 91)
    assert retried.phase == "still_present", retried
    assert driver.delete_calls == 2
    assert _attempts(owned, "delete/91") == 2


async def test_new_unknown_delete_checks_immediately_after_prior_known_presence(pipeline):
    owned = pipeline
    _seed_pending_delete(owned)
    driver = DriverDouble(absent=False, present=True)
    runtime = _runtime(
        owned, driver, _Clock(), delete_interval=Decimal("0"),
        query_interval=Decimal("3"))

    first = await delete_source_file(runtime, 91)
    assert first.phase == "still_present", first
    original_query_run = _run(owned, "exists/91")[0]
    assert (driver.delete_calls, driver.query_calls) == (1, 1)

    second = await delete_source_file(runtime, 91)
    assert second.phase == "still_present", second
    assert (driver.delete_calls, driver.query_calls) == (2, 2)
    assert _attempts(owned, "delete/91") == 2
    assert _attempts(owned, "exists/91") == 2
    assert _run(owned, "exists/91")[0] == original_query_run


@pytest.mark.parametrize("operation", ["delete", "query"])
async def test_higher_current_limit_restarts_original_retry_wait(pipeline, operation):
    owned = pipeline
    _seed_pending_delete(owned)
    driver = (DriverDouble(absent=False, present=True) if operation == "delete"
              else DriverDouble(absent=False, query_error=object()))
    first = _runtime(
        owned, driver, _Clock(), delete_interval=Decimal("0"),
        query_interval=Decimal("0"))
    if operation == "delete":
        first.delete_config = replace(first.delete_config, max_attempts=1)
        responsibility, phase = "delete/91", "still_present"
        wait_phase = "delete_retry_wait"
    else:
        first.query_config = replace(first.query_config, max_attempts=1)
        responsibility, phase = "exists/91", "query_unknown"
        wait_phase = "query_retry_wait"

    saved = await delete_source_file(first, 91)
    assert saved.phase == phase, saved
    assert (driver.delete_calls, driver.query_calls) == (1, 1)
    original = _run(owned, responsibility)
    assert original[1] == 1
    assert original[2] in (1, 2)
    assert original[3:] == (1, 1)
    prefix = _history(owned)

    fresh = _reopen(owned)
    try:
        clock = _Clock(100_000_000_000)
        resumed_driver = (DriverDouble(absent=False, present=True) if operation == "delete"
                          else DriverDouble(absent=False, query_error=object()))
        second = _runtime(
            fresh, resumed_driver, clock, delete_interval=Decimal("0"),
            query_interval=Decimal("0"))
        if operation == "delete":
            second.delete_config = replace(
                second.delete_config, max_attempts=3, retry_interval_s=Decimal("3"))
        else:
            second.query_config = replace(
                second.query_config, max_attempts=3, retry_interval_s=Decimal("3"))
        assert second.retry_gate.anchors == {}

        waiting = await delete_source_file(second, 91)
        assert waiting.phase == wait_phase, waiting
        assert (resumed_driver.delete_calls, resumed_driver.query_calls) == (0, 0)
        assert _attempts(fresh, responsibility) == 1
        assert _run(fresh, responsibility)[0] == original[0]
        _assert_history_prefix(fresh, prefix)

        clock.advance_s(Decimal("3"))
        retried = await delete_source_file(second, 91)
        assert retried.phase == phase, retried
        assert _attempts(fresh, responsibility) == 2
        assert _run(fresh, responsibility)[0] == original[0]
        expected_calls = (1, 1) if operation == "delete" else (0, 1)
        assert (resumed_driver.delete_calls, resumed_driver.query_calls) == expected_calls
        _assert_history_prefix(fresh, prefix)
    finally:
        fresh.connection.close()


@pytest.mark.parametrize("operation", ["delete", "query"])
async def test_lower_current_limit_exhausts_without_retry_wait(pipeline, operation):
    owned = pipeline
    _seed_pending_delete(owned)
    driver = (DriverDouble(absent=False, present=True) if operation == "delete"
              else DriverDouble(absent=False, query_error=object()))
    first = _runtime(
        owned, driver, _Clock(), delete_interval=Decimal("0"),
        query_interval=Decimal("0"))
    phase = "still_present" if operation == "delete" else "query_unknown"
    responsibility = "delete/91" if operation == "delete" else "exists/91"

    saved = await delete_source_file(first, 91)
    assert saved.phase == phase, saved
    assert (driver.delete_calls, driver.query_calls) == (1, 1)
    original = _run(owned, responsibility)
    assert original[1] == 1
    assert original[2] in (1, 2)
    assert original[3:] == (1, 3)
    prefix = _history(owned)

    fresh = _reopen(owned)
    try:
        resumed_driver = (DriverDouble(absent=False, present=True) if operation == "delete"
                          else DriverDouble(absent=False, query_error=object()))
        second = _runtime(
            fresh, resumed_driver, _Clock(100_000_000_000),
            delete_interval=Decimal("0"), query_interval=Decimal("0"))
        if operation == "delete":
            second.delete_config = replace(
                second.delete_config, max_attempts=1, retry_interval_s=Decimal("3"))
            exhausted_reason = "delete_attempts_exhausted"
        else:
            second.query_config = replace(
                second.query_config, max_attempts=1, retry_interval_s=Decimal("3"))
            exhausted_reason = "file_query_attempts_exhausted"
        assert second.retry_gate.anchors == {}

        exhausted = await delete_source_file(second, 91)
        assert exhausted.phase == "failed", exhausted
        assert exhausted.detail == exhausted_reason, exhausted
        assert (resumed_driver.delete_calls, resumed_driver.query_calls) == (0, 0)
        assert _attempts(fresh, responsibility) == 1
        assert _run(fresh, responsibility)[0] == original[0]
        _assert_history_prefix(fresh, prefix)
    finally:
        fresh.connection.close()

"""驱动缺少 result 端口时，旧录像路径不应先发出 START。"""

from __future__ import annotations

import json

import pytest

from camctl.capture.handlers import capture_handler
from camctl.contracts.workflow_errors import registered_error
from camctl.devices.drivers.registry import CapabilityNotDeclaredError
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from .test_closed_result_local_consumption import (
    _assert_local_finish,
    _assert_preserved,
    closed_consumer_world,
    fresh_closed_runtime,
)
from .test_capture_contract import _RECORD, _TIMELAPSE
from ..support.result_cases import result_case, result_case_for

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    ("actions", "action_id", "action_type"),
    [(_RECORD, 12, "camera_record"), (_TIMELAPSE, 13, "camera_timelapse")],
)
async def test_legacy_start_is_blocked_before_device_call_without_results(
    tmp_path, actions, action_id, action_type,
):
    owned, runtime, action_id, _ = await result_case_for().world(tmp_path,
        "record" if action_type == "camera_record" else "timelapse", start=False,
        assembly_options={"results": None})
    try:
        assert runtime.results is None

        with pytest.raises(CapabilityNotDeclaredError, match="result"):
            await capture_handler(action_type)(action_id, runtime)

        assert result_case_for().driver.control.await_count == 0
        assert owned.connection.execute(
            "SELECT status FROM operation_runs WHERE responsibility_key=?",
            (f"start/{action_id}",),
        ).fetchone() is None
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM operation_runs WHERE action_id=?", (action_id,)
        ).fetchone() == (0,)
    finally:
        owned.connection.close()


async def test_legacy_photo_saves_binding_failure_before_missing_result_capability(tmp_path):
    owned, runtime, action_id, _ = await result_case_for().world(tmp_path, "photo",
        start=False, assembly_options={"devices": {}, "results": None})
    driver = result_case_for().driver
    try:
        assert runtime.results is None

        await capture_handler("camera_take_photo")(1, runtime)

        status, error_code, details = owned.connection.execute(
            "SELECT status,error_code,error_details_json FROM actions WHERE id=1").fetchone()
        assert status == 4
        assert error_code == registered_error("device_binding_unavailable")["action_error_id"]
        assert json.loads(details)["reason"] == "missing"
        assert driver.control.await_count == 0
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM operation_runs WHERE action_id=1").fetchone() == (0,)
    finally:
        await runtime.database.close()
        owned.connection.close()


@pytest.mark.parametrize("consumer", ["photo", "timelapse", "canceled_timelapse"])
@pytest.mark.parametrize("binding", ["missing", "mismatch"])
async def test_saved_closed_result_finishes_locally_without_result_port(
    tmp_path, monkeypatch, consumer, binding,
):
    world = await closed_consumer_world(
        tmp_path, monkeypatch, consumer=consumer, history="latest_complete")
    owned = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        runtime, methods = await fresh_closed_runtime(owned, world, binding,
            result_supported=False)
        assert runtime.results is None
        _assert_preserved(owned, world, methods)

        await capture_handler(world.handler)(world.action_id, runtime)

        _assert_local_finish(owned, world, methods)
    finally:
        owned.connection.close()

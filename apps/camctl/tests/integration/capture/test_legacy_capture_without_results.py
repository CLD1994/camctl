"""驱动缺少 result 端口时，旧录像路径不应先发出 START。"""

from __future__ import annotations

import json

import pytest

from camctl.capture.handlers import _listing_round, capture_handler
from camctl.contracts.workflow_errors import registered_error
from camctl.devices.bindings import BindingResult, BindingStatus
from camctl.devices.drivers.registry import CapabilityNotDeclaredError
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from .test_closed_result_local_consumption import (
    _assert_local_finish,
    _assert_preserved,
    closed_consumer_world,
    fresh_closed_runtime,
)
from .test_capture_contract import _RECORD, _TIMELAPSE, _environment, _runtime
from .test_host_photo_flow import _world

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    ("actions", "action_id", "action_type"),
    [(_RECORD, 12, "camera_record"), (_TIMELAPSE, 13, "camera_timelapse")],
)
async def test_legacy_start_is_blocked_before_device_call_without_results(
    tmp_path, actions, action_id, action_type,
):
    owned = _environment(tmp_path, actions)
    try:
        runtime = _runtime(owned)
        # Page evidence remains present to exercise assembly's possible state:
        # evidence alone does not provide the result-listing port.
        runtime.results = None

        with pytest.raises(CapabilityNotDeclaredError, match="result"):
            await capture_handler(action_type)(action_id, runtime)

        assert runtime.driver.calls == []
        assert owned.connection.execute(
            "SELECT status FROM operation_runs WHERE responsibility_key=?",
            (f"start/{action_id}",),
        ).fetchone() is None
    finally:
        owned.connection.close()


async def test_legacy_photo_saves_binding_failure_before_missing_result_capability(tmp_path):
    _config, owned, driver, runtime, _clock = await _world(tmp_path, legacy=True)
    try:
        assert runtime.results is None
        runtime.binding_check = lambda binding: BindingResult(
            BindingStatus.DEVICE_MISSING, binding, None)

        await capture_handler("camera_take_photo")(1, runtime)

        status, error_code, details = owned.connection.execute(
            "SELECT status,error_code,error_details_json FROM actions WHERE id=1").fetchone()
        assert status == 4
        assert error_code == registered_error("device_binding_unavailable")["action_error_id"]
        assert json.loads(details)["reason"] == "missing"
        assert driver.requests == []
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM operation_runs WHERE action_id=1").fetchone() == (0,)
    finally:
        await runtime.database.close()
        owned.connection.close()


async def test_result_page_evidence_without_result_port_fails_as_capability_error(tmp_path):
    owned = _environment(tmp_path, _RECORD)
    try:
        runtime = _runtime(owned)
        runtime.results = None

        with pytest.raises(CapabilityNotDeclaredError, match="result"):
            await _listing_round(runtime, 12)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM operation_runs WHERE action_id=12").fetchone() == (0,)
    finally:
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
        runtime, methods = fresh_closed_runtime(owned, world, binding)
        # Keep result-page evidence while disconnecting only the runtime port.
        runtime.results = None
        _assert_preserved(owned, world, methods)

        await capture_handler(world.handler)(world.action_id, runtime)

        _assert_local_finish(owned, world, methods)
    finally:
        owned.connection.close()

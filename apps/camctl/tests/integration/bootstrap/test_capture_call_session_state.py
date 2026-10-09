"""跨普通、残留和受限工厂接手真实返回后的会话计时消费。"""

from dataclasses import replace
from decimal import Decimal
import json
import sqlite3
from unittest.mock import create_autospec

import pytest

from camctl.acceptance.service import CommandMode
from camctl.bootstrap import capture_assembly, lifecycle
from camctl.capture.handlers import capture_handler
from camctl.capture.recording import RecordingFacts, RecordingPhase, decide_recording_next
from camctl.contracts.values import ConsistencyError
from camctl.devices.catalog import build_catalog
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.ports import DriverDeclaration
from camctl.operations.models import ErrorValue
from camctl.persistence.initialization import initialize_state
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.session.outcome import SessionOutcome

from ..capture.test_capture_contract import ResultsDouble, _NOW, _seed_action
from ..capture.test_recording_start_runtime import _START_EVIDENCE, StartDriver, _result
from ..capture.test_recording_start_save_boundary import ResultWriteFault
from ..devices.test_catalog import _DEFINITIONS
from ..scheduling.test_resources import _seed_activity
from .test_composition import _config_for

pytestmark = pytest.mark.asyncio
_RETURNED_AT = _NOW + 2_000_000
_RETURNED_NS = 7_000_000_000


async def _session_factories(tmp_path, monkeypatch, actual, *, maximum=3, interval=2):
    """隔离会话推进，保留三处生产装配、驱动声明和真实状态库。"""
    config = replace(_config_for(tmp_path), devices={"cam-1": {
        "kind": "camera", "driver": "camctl-adb", "recording": {
            "max_start_attempts": maximum, "start_retry_interval_s": Decimal(interval)},
    }})
    initialize_state(config, tmp_path / "state.db")
    owned = open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
    connection = owned.connection
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "INSERT INTO history_transactions (id,operation_key,first_event_id,last_event_id)"
        " VALUES (1,?,1,1)", ("f" * 32,))
    connection.execute(
        "INSERT INTO history_events (id,transaction_id,event_type,event_version,occurred_at,"
        " clock_status,change_seq,body_json) VALUES (1,1,2,1,?,2,NULL,?)",
        (_NOW, json.dumps({"reason": 1, "evidence": {}, "rows": []})))
    connection.execute(
        "INSERT INTO plans (id,request_id,name,created_at,status,created_event_id,"
        " last_event_id,change_count) VALUES (1,4242,'seed',?,1,1,1,1)", (_NOW,))
    _seed_action(connection, 12, 2)
    _seed_activity(connection, 12)
    params = {"type": "ordinary", "duration_s": 60}
    connection.execute(
        "UPDATE actions SET input_fields_json=?,effective_params_json=?,max_delay_ms=60000"
        " WHERE id=12", (json.dumps({"params": params, "policy": {"max_delay_ms": 60000}}),
                          json.dumps(params)))
    connection.execute("COMMIT")
    driver = StartDriver(actual)
    entry = DriverEntry(
        driver_id="camctl-adb", driver=driver,
        declaration=DriverDeclaration(control_supported=True, stop_supported=False,
            query_supported=False, result_supported=False, read_supported=False,
            digest_supported=False, delete_supported=False),
        evidence=_START_EVIDENCE, status=DriverStatus.SOFTWARE_CONTRACT_VERIFIED)
    from camctl.devices.drivers import runtime as driver_runtime

    monkeypatch.setattr(driver_runtime, "current_registry", lambda: DriverRegistry((entry,)))
    wall, mono = [_RETURNED_AT], [_RETURNED_NS]
    factories = []
    original = capture_assembly.session_capture_assembly

    def observed(**kwargs):
        factory = original(**kwargs, results=ResultsDouble({}),
            wall_us=lambda: wall[0], monotonic_ns=lambda: mono[0])
        factories.append(factory)
        return factory

    monkeypatch.setattr(capture_assembly, "session_capture_assembly", observed)
    monkeypatch.setattr(lifecycle, "run_session", create_autospec(
        lifecycle.run_session, return_value=SessionOutcome(succeeded=True)))
    # 动作已作为历史事实保存；目录仍按真实部署定义检查设备身份，
    # 不让未登记的驱动定义遮蔽会话计时的实际消费断言。
    catalog = build_catalog(config, _DEFINITIONS)
    deps = lifecycle.build_runtime(CommandMode.RUN, config, catalog=catalog)
    try:
        assert deps.startup_error is None
        await lifecycle.execute_command(deps, None)
        ordinary, residual, restricted = factories
        return owned, driver, wall, mono, ordinary, residual, restricted
    except BaseException:
        owned.connection.close()
        raise
    finally:
        lifecycle.close_runtime(deps)


async def _hold_return(owned, driver, ordinary):
    class PendingCommit(ResultWriteFault):
        def execute(self, statement, params=()):
            if self.driver.calls and statement == "COMMIT":
                raise sqlite3.OperationalError("原实际结果尚不能提交")
            return super().execute(statement, params)

    proxy = PendingCommit(owned.connection, driver, "persistent")
    runtime = ordinary(replace(owned, connection=proxy), "cam-1")
    with pytest.raises(ConsistencyError):
        await capture_handler("camera_record")(12, runtime)
    pending, = runtime.pending_start_results.values()
    assert pending.finish.outcome.outcome is driver.outcome
    request, = driver.calls
    return request.ticket


@pytest.mark.parametrize("saver", ["residual", "restricted"])
async def test_actual_start_saved_by_other_factory_preserves_ordinary_monotonic_anchor(
    tmp_path, monkeypatch, saver,
):
    actual = _result(confirmed=True, error=ErrorValue("transport_timeout", "transport"))
    owned, driver, wall, mono, ordinary, residual, restricted = await _session_factories(
        tmp_path, monkeypatch, actual)
    try:
        ticket = await _hold_return(owned, driver, ordinary)
        wall[0], mono[0] = _RETURNED_AT + 5_000_000, 12_000_000_000
        take_over = residual if saver == "residual" else restricted
        assert take_over(owned, "cam-1").recover_attempt(ticket)
        runtime = ordinary(owned, "cam-1")
        state = runtime.recording_state.recording_state(12)
        assert state.anchor_from_current_session
        assert state.stop_target_ns == 67_000_000_000
        assert decide_recording_next(state, RecordingFacts()).phase is RecordingPhase.WAIT_RECORD
        assert owned.connection.execute(
            "SELECT started_at FROM device_activities WHERE action_id=12").fetchone() == (_RETURNED_AT,)
        assert len(driver.calls) == 1
    finally:
        owned.connection.close()


@pytest.mark.parametrize("saver", ["residual", "restricted"])
@pytest.mark.parametrize("maximum,interval,offset_ns,expected", [
    (3, 2, 1_000_000_000, Decimal("1")),
    (3, 2, 2_000_000_000, None),
    (1, 2, 1_000_000_000, None),
    (3, 0, 1_000_000_000, None),
])
async def test_actual_start_saved_by_other_factory_preserves_ordinary_retry_budget_gate(
    tmp_path, monkeypatch, saver, maximum, interval, offset_ns, expected,
):
    actual = _result(no_effect=True, error=ErrorValue("transport_timeout", "transport"))
    owned, driver, wall, mono, ordinary, residual, restricted = await _session_factories(
        tmp_path, monkeypatch, actual, maximum=maximum, interval=interval)
    try:
        ticket = await _hold_return(owned, driver, ordinary)
        wall[0], mono[0] = _RETURNED_AT + 5_000_000, _RETURNED_NS + offset_ns
        take_over = residual if saver == "residual" else restricted
        assert take_over(owned, "cam-1").recover_attempt(ticket)
        runtime = ordinary(owned, "cam-1")
        assert runtime.retry_wait_remaining(
            "start/12", runtime.start_config.retry_interval_s,
            maximum=runtime.start_config.max_attempts) == expected
        assert owned.connection.execute(
            "SELECT attempts_used FROM operation_runs WHERE responsibility_key='start/12'"
        ).fetchone() == (1,)
        assert len(driver.calls) == 1
    finally:
        owned.connection.close()

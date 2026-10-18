"""真实启动仓储与具体相机设置编排共同核对取消及窗口。"""
import json
from decimal import Decimal
from dataclasses import replace
from copy import deepcopy
import shlex

import pytest

from camctl.capture.handlers import capture_handler
from camctl.contracts.enums import enum_for
from camctl.contracts.pages import Page
from camctl.devices.adb_transport import DeviceCommand, InterpretedFacts
from camctl.devices.directory import DirectoryRead
from camctl.devices.drivers.adb_cameras import timelapse_parameters
from camctl.devices.drivers.adb_cameras.commands import CameraModel, settings_for
from camctl.devices.drivers.adb_cameras.contracts import CameraCall, CameraContract
from camctl.devices.drivers.adb_cameras.driver import AdbCameraDriver
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.operations.models import EffectState
from camctl.operations.process import LocalExit, RawToolOutcome
from ..capture.test_baseline_start import _prepared, _runtime
from ..capture.test_baseline_start import _grant
from ..capture.test_baseline_history import _fix
from ..capture.test_baseline_prepare import Directory
from ..capture.test_capture_contract import _CONTRACTS
from ..capture.test_cancel_late_start import _cancel
from ..scheduling.test_start_action import _SCHEDULED, owned
from ..scheduling.test_start_action import _accept_plan, _start
from ..acceptance.test_acceptance import _plan_body
from ..acceptance.test_adb_camera_definitions import (
    _catalog, _legacy_action6_catalog, _legacy_timelapse_catalog, _params,
)
from camctl.devices.drivers.adb_cameras import definitions
from ..devices.adb_camera_fixtures import software_contract
from camctl.acceptance.schema import BodySchemaError
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.models import DbOutcomeKind


SETTING = EvidenceContract("setting_applied", 1, "control", frozenset({"activity_id"}), identity_field="activity_id")
ASSUMED = EvidenceContract("adb_foreground_recovery", 1, "control", frozenset({"terminate_grace_s"}))
PREVENTED = EvidenceContract("dispatch_prevented", 1, "control", frozenset())
EVIDENCE = EvidenceRegistry((*_CONTRACTS, SETTING, ASSUMED, PREVENTED))
RETURNED = EVIDENCE.contract("operation_returned", 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("action_type", ["camera_record", "camera_timelapse"])
@pytest.mark.parametrize("reason", ["canceled", "window_ended"])
@pytest.mark.parametrize("after_setting", [False, True])
async def test_multistep_start_rechecks_current_business_qualification(owned, tmp_path, action_type, reason, after_setting):
    await _prepared(owned, tmp_path, action_type)
    now, calls = [_SCHEDULED], []
    def invalidate():
        if reason == "canceled":
            _cancel(owned, 1, occurred_at=int(_SCHEDULED))
        else:
            now[0] = _SCHEDULED + 30_000_001
    class Transport:
        async def run(self, spec, stop):
            calls.append(spec)
            if after_setting:
                invalidate()
            return RawToolOutcome(LocalExit(exit_code=0), b"OK", None, None, stderr=b"")
    class Parser:
        def interpret(self, raw):
            return InterpretedFacts((DeviceObservation("setting_applied", 1, {"activity_id": "1"}),), None, EffectState.CONFIRMED)
    def command(request, serial, argv, batch):
        return DeviceCommand("control", argv, request.binding, request.timeout_s, Decimal("1"),
                             EVIDENCE, RETURNED, ASSUMED, Parser(), "setting")
    class Driver(AdbCameraDriver):
        async def control(self, request):
            if not after_setting:
                invalidate()
            return await super().control(request)
    contract = CameraContract(CameraModel.ACTION6, commands={CameraCall.SETTING: command, CameraCall.START: command}, evidence=EVIDENCE)
    driver = Driver(contract, {"cam-1": {"driver": CameraModel.ACTION6, "adb": {"serial": "serial-1"}}},
                    Transport(), terminate_grace_s=Decimal("1"), monotonic_ns=lambda: 0)
    runtime = _runtime(owned, Directory([lambda _request: DirectoryRead(None, None, Page((), None))]), driver)
    runtime.evidence = EVIDENCE
    runtime.wall_us = lambda: now[0]
    await capture_handler(action_type)(1, runtime)
    assert len(calls) == int(after_setting)
    saved = owned.connection.execute("SELECT result_json,effect_state,error_json FROM operation_attempts").fetchone()
    result = json.loads(saved[0])
    assert saved[1] == int(enum_for("operation_attempts.effect_state").NO_EFFECT)
    assert json.loads(saved[2])["code"] == reason
    assert result["settlement"]["basis"] == ("observed" if after_setting else "not_dispatched")
    assert len(result["observations"]) == int(after_setting)
    assert owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE responsibility_key='start/1'").fetchone() == (1,)
    assert owned.connection.execute("SELECT sent_at,started_at,activity_state,occupancy_state FROM device_activities").fetchone() == (None, None, 1, 2)
    expected = enum_for("actions.status").CANCELED if reason == "canceled" else enum_for("actions.status").EXPIRED
    assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (int(expected),)


@pytest.mark.asyncio
@pytest.mark.parametrize("interval_s,duration_s,exposure,payload", [
    (30, 5400, {"mode": "manual", "iso": 800}, "0400002c01181500000000000000000000"),
    (25, 6000, {"mode": "auto"}, "040000fa00701700000000000000000000"),
])
async def test_restart_dispatch_uses_original_admitted_parameters(
        owned, tmp_path, interval_s, duration_s, exposure, payload):
    """旧软件合同受理、真实重开数据库及公共 handler 不受新候选域改写。"""
    params = {"type": "action6_timelapse", "interval_s": interval_s, "duration_s": duration_s,
              "outputs": "video", "exposure": exposure}
    body = _plan_body()
    body["actions"][0].update(type="camera_timelapse", params=params)
    await _accept_plan(owned, tmp_path, body, _legacy_action6_catalog(interval_s, duration_s))
    original = owned.connection.execute(
        "SELECT device_id,driver_id,effective_params_json,execution_spec_json FROM actions WHERE id=1").fetchone()
    assert owned.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone() == (0,)
    # 新入口继续使用当前完整 Schema；相同值只使新动作失败。
    with pytest.raises(BodySchemaError):
        settings_for(CameraModel.ACTION6, "camera_timelapse", params)
    current = _catalog(CameraModel.ACTION6)
    body["request_id"] = "43"
    await _accept_plan(owned, tmp_path, body, current)
    assert owned.connection.execute("SELECT status FROM actions WHERE id=2").fetchone() == (4,)
    owned.connection.close()
    restarted = open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
    try:
        assert restarted.connection.execute(
            "SELECT device_id,driver_id,effective_params_json,execution_spec_json FROM actions WHERE id=1").fetchone() == original
        assert _start(restarted, 1, now=_SCHEDULED).kind is DbOutcomeKind.COMPLETED
        calls, requests = [], []
        contract = software_contract(CameraModel.ACTION6)
        class Transport:
            async def run(self, spec, stop):
                calls.append(spec)
                return RawToolOutcome(LocalExit(exit_code=0), b"OK", None, None, stderr=b"")
        def capture_request(request, serial, argv, batch):
            requests.append(request)
            return contract.commands[CameraCall.START](request, serial, argv, batch)
        dispatch_contract = replace(contract, commands={**contract.commands, CameraCall.START: capture_request})
        driver = AdbCameraDriver(dispatch_contract,
            {"cam-1": {"driver": CameraModel.ACTION6, "adb": {"serial": "serial-original"}}},
            Transport(), terminate_grace_s=Decimal("1"), monotonic_ns=lambda: 0)
        directory = Directory([lambda _r: DirectoryRead(None, None, Page((), None))])
        runtime = _runtime(restarted, directory, driver)
        runtime.evidence = contract.evidence
        await capture_handler("camera_timelapse")(1, runtime)
        assert len(requests) == 1
        assert requests[0].params == params
        assert requests[0].binding.device_id == "cam-1" and requests[0].binding.driver_id == CameraModel.ACTION6
        assert all(call.argv[:5] == ("adb", "-s", "serial-original", "shell", "-T") for call in calls)
        command_words = [shlex.split(shlex.split(call.argv[5])[2]) for call in calls]
        assert [words[-1] for words in command_words if words[-2] == "6c"] == [payload]
        assert sum(words[-2:] == ["01", "01"] for words in command_words) == 1
        assert restarted.connection.execute(
            "SELECT attempts_used FROM operation_runs WHERE responsibility_key='start/1'").fetchone() == (1,)
        assert restarted.connection.execute(
            "SELECT device_id,driver_id,effective_params_json,execution_spec_json FROM actions WHERE id=1").fetchone() == original
    finally:
        restarted.connection.close()
    # 已存在 START 尝试的下一次重启沿用发送及等待事实，不重发或重取基准。
    resumed = open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
    try:
        runtime = _runtime(resumed, Directory([]), driver)
        runtime.evidence = contract.evidence
        await capture_handler("camera_timelapse")(1, runtime)
        assert len(requests) == 1
        assert resumed.connection.execute(
            "SELECT COUNT(*) FROM operation_attempts WHERE run_id IN "
            "(SELECT id FROM operation_runs WHERE responsibility_key='start/1')").fetchone() == (1,)
    finally:
        resumed.connection.close()


def _narrow_authoritative_time_domain(monkeypatch, field, excluded, model):
    if field == "interval_s":
        original = timelapse_parameters.timelapse_intervals_s
        narrowed = tuple(value for value in original(model.value) if value != excluded)
        assert len(narrowed) == len(original(model.value)) - 1

        def current_intervals(requested_model):
            return narrowed if requested_model == model.value else original(requested_model)

        monkeypatch.setattr(timelapse_parameters, "timelapse_intervals_s", current_intervals)
        return
    narrowed = tuple(value for value in timelapse_parameters.TIMELAPSE_DURATIONS_S if value != excluded)
    assert len(narrowed) == len(timelapse_parameters.TIMELAPSE_DURATIONS_S) - 1
    monkeypatch.setattr(timelapse_parameters, "TIMELAPSE_DURATIONS_S", narrowed)


@pytest.mark.asyncio
@pytest.mark.parametrize("model,narrow_field,interval,excluded,payload,legacy", [
    (CameraModel.ACTION6, "interval_s", 1, 1,
     "0400000a002c0100000000000000000000", False),
    (CameraModel.OSMO360II, "duration_s", 2, 300,
     "04000014002c01000000000000000000", False),
    (CameraModel.OSMO360II, "interval_s", 1, 2,
     "0400000a002c01000000000000000000", True),
])
async def test_restart_dispatch_keeps_first_format_after_current_domain_narrows(
        owned, tmp_path, monkeypatch, model, narrow_field, interval, excluded, payload, legacy):
    driver_id = model.value
    params = {"type": "action6_timelapse" if model is CameraModel.ACTION6 else "osmo360ii_timelapse",
              "interval_s": interval, "duration_s": 300, "outputs": "video", "exposure": {"mode": "auto"}}
    if model is CameraModel.OSMO360II:
        del params["outputs"]
        params["bitrate"] = "high"
    body = _plan_body()
    body["actions"][0].update(type="camera_timelapse", params=params)
    accepted_catalog = (_legacy_timelapse_catalog(driver_id, interval, 300)
                        if legacy else _catalog(driver_id))
    await _accept_plan(owned, tmp_path, body, accepted_catalog)
    original = owned.connection.execute(
        "SELECT device_id,driver_id,effective_params_json,execution_spec_json FROM actions WHERE id=1").fetchone()
    assert owned.connection.execute("SELECT COUNT(*) FROM operation_attempts").fetchone() == (0,)

    owned.connection.close()
    _narrow_authoritative_time_domain(
        monkeypatch, narrow_field, excluded, model)
    restarted = open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
    try:
        assert restarted.connection.execute(
            "SELECT device_id,driver_id,effective_params_json,execution_spec_json FROM actions WHERE id=1").fetchone() == original
        with pytest.raises(BodySchemaError):
            settings_for(model, "camera_timelapse", params)
        body["request_id"] = "43"
        await _accept_plan(restarted, tmp_path, body, _catalog(driver_id))
        assert restarted.connection.execute("SELECT status FROM actions WHERE id=2").fetchone() == (4,)

        assert _start(restarted, 1, now=_SCHEDULED).kind is DbOutcomeKind.COMPLETED
        calls, requests = [], []
        contract = software_contract(model)

        class Transport:
            async def run(self, spec, stop):
                calls.append(spec)
                return RawToolOutcome(LocalExit(exit_code=0), b"OK", None, None, stderr=b"")

        def capture_start(request, serial, argv, batch):
            requests.append(request)
            return contract.commands[CameraCall.START](request, serial, argv, batch)

        dispatch_contract = replace(contract, commands={
            **contract.commands, CameraCall.START: capture_start})
        driver = AdbCameraDriver(
            dispatch_contract,
            {"cam-1": {"driver": model, "adb": {"serial": "serial-original"}}},
            Transport(), terminate_grace_s=Decimal("1"), monotonic_ns=lambda: 0)
        directory = Directory([lambda _request: DirectoryRead(None, None, Page((), None))])
        runtime = _runtime(restarted, directory, driver)
        runtime.evidence = contract.evidence
        await capture_handler("camera_timelapse")(1, runtime)

        assert len(requests) == 1
        assert requests[0].params == params
        assert requests[0].binding.device_id == "cam-1"
        assert requests[0].binding.driver_id == model
        assert all(call.argv[:5] == ("adb", "-s", "serial-original", "shell", "-T") for call in calls)
        command_words = [shlex.split(shlex.split(call.argv[5])[2]) for call in calls]
        assert [words[-1] for words in command_words if words[-2] == "6c"] == [payload]
        assert sum(words[-2:] == ["01", "01"] for words in command_words) == 1
        assert restarted.connection.execute(
            "SELECT attempts_used FROM operation_runs WHERE responsibility_key='start/1'").fetchone() == (1,)
        assert restarted.connection.execute(
            "SELECT device_id,driver_id,effective_params_json,execution_spec_json FROM actions WHERE id=1").fetchone() == original
    finally:
        restarted.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("interval_s,duration_s,exposure", [
    (30, 5400, {"mode": "manual", "iso": 800}), (25, 6000, {"mode": "auto"}),
])
async def test_restart_with_unresolved_start_keeps_original_attempt_without_dispatch(
        owned, tmp_path, interval_s, duration_s, exposure):
    body = _plan_body()
    body["actions"][0].update(type="camera_timelapse", params={
        "type": "action6_timelapse", "interval_s": interval_s, "duration_s": duration_s,
        "outputs": "video", "exposure": exposure})
    await _accept_plan(owned, tmp_path, body, _legacy_action6_catalog(interval_s, duration_s))
    started = _start(owned, 1, now=_SCHEDULED)
    assert started.kind is DbOutcomeKind.COMPLETED
    assert _fix(owned, started.value.activity_id).kind is DbOutcomeKind.COMPLETED
    granted = _grant(owned)
    assert granted.kind is DbOutcomeKind.COMPLETED and granted.value.ticket is not None
    attempt = owned.connection.execute("SELECT * FROM operation_attempts").fetchone()
    owned.connection.close()
    reopened = open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
    try:
        class Transport:
            async def run(self, spec, stop):
                pytest.fail("已有结果未取得的 START 尝试不能重新派发设备命令")
        contract = software_contract(CameraModel.ACTION6)
        driver = AdbCameraDriver(contract,
            {"cam-1": {"driver": CameraModel.ACTION6, "adb": {"serial": "serial-original"}}},
            Transport(), terminate_grace_s=Decimal("1"), monotonic_ns=lambda: 0)
        runtime = _runtime(reopened, Directory([]), driver)
        runtime.evidence = contract.evidence
        for _ in range(2):
            await capture_handler("camera_timelapse")(1, runtime)
        assert reopened.connection.execute("SELECT * FROM operation_attempts").fetchall() == [attempt]
        assert reopened.connection.execute(
            "SELECT attempts_used FROM operation_runs WHERE responsibility_key='start/1'").fetchone() == (1,)
        assert runtime.baseline_directory.calls == []
    finally:
        reopened.connection.close()


@pytest.mark.asyncio
async def test_record_restart_dispatch_uses_original_parameters_after_schema_narrows(owned, tmp_path, monkeypatch):
    params = {**_params(CameraModel.ACTION6, "camera_record"), "resolution": "4k30"}
    body = _plan_body()
    body["actions"][0].update(type="camera_record", params=params)
    await _accept_plan(owned, tmp_path, body, _catalog(CameraModel.ACTION6))
    original = owned.connection.execute(
        "SELECT device_id,driver_id,effective_params_json,execution_spec_json FROM actions WHERE id=1").fetchone()
    candidates = definitions.candidate_capabilities
    def narrowed(driver_id):
        capabilities = []
        for capability in candidates(driver_id):
            if capability.action_type == "camera_record" and driver_id == CameraModel.ACTION6:
                schema = deepcopy(capability.schema)
                schema["properties"]["resolution"]["enum"] = ["8k30"]
                capability = replace(capability, schema=schema)
            capabilities.append(capability)
        return tuple(capabilities)
    monkeypatch.setattr(definitions, "candidate_capabilities", narrowed)
    with pytest.raises(BodySchemaError):
        settings_for(CameraModel.ACTION6, "camera_record", params)
    body["request_id"] = "43"
    await _accept_plan(owned, tmp_path, body, _catalog(CameraModel.ACTION6))
    assert owned.connection.execute("SELECT status FROM actions WHERE id=2").fetchone() == (4,)
    owned.connection.close()
    reopened = open_existing(tmp_path / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
    try:
        assert _start(reopened, 1, now=_SCHEDULED).kind is DbOutcomeKind.COMPLETED
        calls, requests = [], []
        contract = software_contract(CameraModel.ACTION6)
        class Transport:
            async def run(self, spec, stop):
                calls.append(spec)
                return RawToolOutcome(LocalExit(exit_code=0), b"OK", None, None, stderr=b"")
        def capture_request(request, serial, argv, batch):
            requests.append(request)
            return contract.commands[CameraCall.START](request, serial, argv, batch)
        driver = AdbCameraDriver(replace(contract, commands={**contract.commands, CameraCall.START: capture_request}),
            {"cam-1": {"driver": CameraModel.ACTION6, "adb": {"serial": "serial-original"}}},
            Transport(), terminate_grace_s=Decimal("1"), monotonic_ns=lambda: 0)
        runtime = _runtime(reopened, Directory([lambda _r: DirectoryRead(None, None, Page((), None))]), driver)
        runtime.evidence = contract.evidence
        await capture_handler("camera_record")(1, runtime)
        assert len(requests) == 1 and requests[0].params == params
        assert requests[0].binding.device_id == "cam-1" and requests[0].binding.driver_id == CameraModel.ACTION6
        assert all(call.argv[:5] == ("adb", "-s", "serial-original", "shell", "-T") for call in calls)
        words = [shlex.split(shlex.split(call.argv[5])[2]) for call in calls]
        assert [command[-1] for command in words if command[-2] == "18"] == ["1003000000"]
        assert sum(command[-2:] == ["02", "01"] for command in words) == 1
        assert reopened.connection.execute(
            "SELECT attempts_used FROM operation_runs WHERE responsibility_key='start/1'").fetchone() == (1,)
        assert reopened.connection.execute(
            "SELECT device_id,driver_id,effective_params_json,execution_spec_json FROM actions WHERE id=1").fetchone() == original
    finally:
        reopened.connection.close()

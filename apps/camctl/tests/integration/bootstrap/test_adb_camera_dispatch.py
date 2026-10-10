"""真实启动仓储与具体相机设置编排共同核对取消及窗口。"""
import json
from decimal import Decimal

import pytest

from camctl.capture.handlers import capture_handler
from camctl.contracts.enums import enum_for
from camctl.contracts.pages import Page
from camctl.devices.adb_transport import DeviceCommand, InterpretedFacts
from camctl.devices.directory import DirectoryRead
from camctl.devices.drivers.adb_cameras.commands import CameraModel
from camctl.devices.drivers.adb_cameras.contracts import CameraCall, CameraContract
from camctl.devices.drivers.adb_cameras.driver import AdbCameraDriver
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.operations.models import EffectState
from camctl.operations.process import LocalExit, RawToolOutcome
from ..capture.test_baseline_start import _prepared, _runtime
from ..capture.test_baseline_prepare import Directory
from ..capture.test_capture_contract import _CONTRACTS
from ..capture.test_cancel_late_start import _cancel
from ..scheduling.test_start_action import _SCHEDULED, owned


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

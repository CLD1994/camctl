"""真实单张返回的诊断校验必须晚于原结果持有。"""
from dataclasses import replace
from pathlib import Path

import pytest

from camctl.capture.handlers import _control_call
from camctl.contracts.values import ConsistencyError
from camctl.devices.drivers.hasselblad.driver import HasselbladDriver
from camctl.devices.ports import DeviceCallResult
from camctl.host_files.models import BoundDirectories
from camctl.operations import tool_diagnostics

from .test_host_photo_flow import _ManagedCaptureTransport, _world

pytestmark = pytest.mark.asyncio


async def _actual_world(tmp_path):
    transport = _ManagedCaptureTransport()
    def make_driver(config):
        return HasselbladDriver(config.devices, BoundDirectories(Path(config.paths.staging)),
                                monotonic_ns=lambda: 1, transport=transport)
    world = await _world(tmp_path, driver_factory=make_driver)
    return (*world, transport)


@pytest.mark.parametrize("fault", ["index", "stream", "cap", "unregistered", "temporary"])
async def test_actual_return_validation_preserves_original_request(tmp_path, monkeypatch, fault):
    cfg, owned, driver, runtime, clock, transport = await _actual_world(tmp_path)
    original_control = driver.control
    actual = None
    checks = 0
    validate = tool_diagnostics.validate_diagnostics
    def failing(value):
        nonlocal checks
        checks += 1
        if checks == 1:
            raise ValueError("一次临时诊断校验故障")
        return validate(value)
    async def returned(request):
        nonlocal actual
        result = await original_control(request)
        diagnostics = result.outcome.diagnostics
        if fault == "unregistered":
            runtime.evidence = runtime.evidence.with_contract(replace(
                runtime.evidence.contract("operation_returned", 1), max_diagnostic_calls=None))
        if fault == "cap":
            diagnostics = replace(diagnostics, call_limit=diagnostics.call_limit + 1)
        elif fault in ("index", "stream"):
            first = diagnostics.calls[0]
            first = (replace(first, step_index=True) if fault == "index" else
                     replace(first, stdout=replace(first.stdout, state="complete")))
            diagnostics = replace(diagnostics, calls=(first, *diagnostics.calls[1:]))
        actual = replace(result.outcome, diagnostics=diagnostics)
        if fault == "temporary":
            monkeypatch.setattr(tool_diagnostics, "validate_diagnostics", failing)
        return DeviceCallResult.from_outcome(actual, diagnostics_check=result.diagnostics_check)
    monkeypatch.setattr(driver, "control", returned)
    try:
        with pytest.raises(ConsistencyError):
            await _control_call(runtime, runtime.action(1), "take_photo", "photo_taken")
        assert len(transport.calls) == 9
        pending, = runtime.pending_start_results.values()
        assert not pending.validated
        assert pending.finish.outcome.outcome is actual
        ticket, key = pending.finish.ticket, pending.key
        occurred_at, returned_ns = pending.finish.occurred_at, pending.returned_ns
        assert [c.stdout.data for c in actual.diagnostics.calls] == [r.output for r in transport.outcomes]
        assert owned.connection.execute("SELECT result_json,error_json FROM operation_attempts").fetchone() == (None, None)
        runtime.wall_us = lambda: occurred_at + 99
        runtime.monotonic_ns = lambda: returned_ns + 99
        if fault == "temporary":
            assert checks == 1
            assert runtime.recover_attempt(ticket)
            assert checks >= 3  # 显式接手校验、编码及事务守卫解码
            assert not runtime.pending_start_results
            saved, = owned.connection.execute("SELECT result_json FROM operation_attempts").fetchone()
            assert actual.diagnostics.calls[-1].stdout.data.hex() in saved
        else:
            with pytest.raises(ConsistencyError):
                runtime.recover_attempt(ticket)
            held, = runtime.pending_start_results.values()
            assert held.key == key
            assert held.finish.outcome.outcome is actual
            assert held.finish.occurred_at == occurred_at
            assert held.returned_ns == returned_ns
        assert len(transport.calls) == 9
    finally:
        owned.connection.close()


async def test_driver_prefix_failure_hands_original_bytes_to_owner(tmp_path, monkeypatch):
    from camctl.devices.drivers.hasselblad import driver as module
    cfg, owned, driver, runtime, clock, transport = await _actual_world(tmp_path)
    construct = module.ToolCallOutput
    entered = []
    from camctl.devices.ports import DeviceCallResult
    from_outcome = DeviceCallResult.from_outcome
    def intercept(outcome, *, diagnostics_check=None):
        def check(original):
            pending, = runtime.pending_start_results.values()
            assert pending.finish.outcome.outcome is original
            assert not pending.validated
            entered.append(pending.key)
            diagnostics_check(original)
        return from_outcome(outcome, diagnostics_check=check)
    monkeypatch.setattr(DeviceCallResult, "from_outcome", intercept)
    monkeypatch.setattr(module, "ToolCallOutput", lambda index, name, stdout, stderr:
                        construct(index, "wrong-prefix", stdout, stderr))
    try:
        with pytest.raises(ConsistencyError, match="诊断校验失败"):
            await _control_call(runtime, runtime.action(1), "take_photo", "photo_taken")
        pending, = runtime.pending_start_results.values()
        assert not pending.validated
        actual = pending.finish.outcome.outcome
        assert len(transport.calls) == 9
        assert actual.observations[0].type == "photo_taken"
        assert [c.stdout.data for c in actual.diagnostics.calls] == [r.output for r in transport.outcomes]
        key = pending.key
        assert entered == [key]
        frozen_check = pending.diagnostics_check
        with pytest.raises(ConsistencyError, match="诊断校验失败"):
            runtime.recover_attempt(pending.finish.ticket)
        held, = runtime.pending_start_results.values()
        assert held.key == key
        assert held.diagnostics_check is frozen_check
        assert held.finish.outcome.outcome is actual
        assert entered == [key, key]
        assert owned.connection.execute("SELECT result_json,error_json FROM operation_attempts").fetchone() == (None, None)
        assert len(transport.calls) == 9
    finally:
        owned.connection.close()


async def test_driver_diagnostic_validation_only_runs_after_owner_holds_result(tmp_path, monkeypatch):
    cfg, owned, driver, runtime, clock, transport = await _actual_world(tmp_path)
    checked = []
    def failing(value):
        pending, = runtime.pending_start_results.values()
        assert pending.finish.outcome.outcome.diagnostics is value
        checked.append(value)
        raise ValueError("拥有者已持有之后的一次校验故障")
    monkeypatch.setattr(tool_diagnostics, "validate_diagnostics", failing)
    try:
        with pytest.raises(ConsistencyError):
            await _control_call(runtime, runtime.action(1), "take_photo", "photo_taken")
        assert len(checked) == 1
        pending, = runtime.pending_start_results.values()
        assert pending.finish.outcome.outcome.diagnostics is checked[0]
        assert len(transport.calls) == 9
    finally:
        owned.connection.close()


async def test_general_invalid_observation_keeps_existing_rejection(tmp_path, monkeypatch):
    cfg, owned, driver, runtime, clock, transport = await _actual_world(tmp_path)
    original_control = driver.control
    async def returned(request):
        result = await original_control(request)
        observation = result.outcome.observations[0]
        actual = replace(result.outcome, observations=(replace(observation, type="unregistered"),))
        return DeviceCallResult.from_outcome(actual, diagnostics_check=result.diagnostics_check)
    monkeypatch.setattr(driver, "control", returned)
    try:
        result = await _control_call(runtime, runtime.action(1), "take_photo", "photo_taken")
        assert result.detail == "invalid_device_result"
        assert not runtime.pending_start_results
        saved, error = owned.connection.execute("SELECT result_json,error_json FROM operation_attempts").fetchone()
        import json
        assert json.loads(saved)["observations"] == []
        assert json.loads(error)["code"] == "invalid_device_result"
        assert len(transport.calls) == 9
    finally:
        owned.connection.close()


async def test_temporary_frozen_prefix_check_resumes_same_original_without_replanning(tmp_path, monkeypatch):
    from camctl.devices.drivers.hasselblad import driver as module
    cfg, owned, driver, runtime, clock, transport = await _actual_world(tmp_path)
    factory = DeviceCallResult.from_outcome
    checked = []
    def intercept(outcome, *, diagnostics_check=None):
        def check(original):
            pending, = runtime.pending_start_results.values()
            assert pending.finish.outcome.outcome is original
            checked.append((pending.key, original))
            if len(checked) == 1:
                raise RuntimeError("本次冻结前缀检查的一次临时故障")
            diagnostics_check(original)
        return factory(outcome, diagnostics_check=check)
    monkeypatch.setattr(DeviceCallResult, "from_outcome", intercept)
    try:
        with pytest.raises(ConsistencyError):
            await _control_call(runtime, runtime.action(1), "take_photo", "photo_taken")
        pending, = runtime.pending_start_results.values()
        ticket, key, actual = pending.finish.ticket, pending.key, pending.finish.outcome.outcome
        assert checked == [(key, actual)]
        frozen = pending.diagnostics_check
        def forbidden(_settings):
            raise AssertionError("原申请接手不得重新规划命令")
        monkeypatch.setattr(module, "photo_start_commands", forbidden)
        assert runtime.recover_attempt(ticket)
        assert checked == [(key, actual), (key, actual)]
        assert len(transport.calls) == 9
        assert not runtime.pending_start_results
        assert frozen is not None
    finally:
        owned.connection.close()


async def test_actual_failed_capture_error_survives_bad_diagnostic_index(tmp_path, monkeypatch):
    from camctl.operations.models import AttemptStatus, EffectState
    from camctl.operations.process import LocalExit
    cfg, owned, driver, runtime, clock, transport = await _actual_world(tmp_path)
    run = transport.run
    async def failed_capture(spec, stop):
        raw = await run(spec, stop)
        if spec.argv[3] == "capture":
            raw = replace(raw, exit=LocalExit(exit_code=7))
            transport.outcomes[-1] = raw
        return raw
    monkeypatch.setattr(transport, "run", failed_capture)
    control = driver.control
    actual = None
    async def returned(request):
        nonlocal actual
        result = await control(request)
        diagnostic = result.outcome.diagnostics
        actual = replace(result.outcome, diagnostics=replace(diagnostic, calls=(
            replace(diagnostic.calls[0], step_index=True), *diagnostic.calls[1:])))
        return DeviceCallResult.from_outcome(actual, diagnostics_check=result.diagnostics_check)
    monkeypatch.setattr(driver, "control", returned)
    try:
        with pytest.raises(ConsistencyError):
            await _control_call(runtime, runtime.action(1), "take_photo", "photo_taken")
        held, = runtime.pending_start_results.values()
        assert held.finish.outcome.outcome is actual
        assert actual.status is AttemptStatus.FAILED
        assert actual.effect is EffectState.UNKNOWN
        assert actual.error.code == "device_error"
        assert actual.error.details["step"] == "capture"
        assert actual.call_info.local_exit_code == 7
        assert actual.diagnostics.calls[-1].stdout.data == transport.outcomes[-1].output
        assert owned.connection.execute("SELECT result_json,error_json FROM operation_attempts").fetchone() == (None, None)
        assert len(transport.calls) == 9
    finally:
        owned.connection.close()

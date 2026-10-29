"""独立准备只设置可重复参数，凭据将真正拍摄绑定到同一次准备。"""

from dataclasses import replace
from decimal import Decimal
import json
from unittest.mock import create_autospec

import pytest

from camctl.devices.bindings import DeviceBinding
from camctl.devices.drivers.hasselblad.driver import EVIDENCE
from camctl.devices.start_entry import EntryPhase, StartEntryPermit
from camctl.logging_runtime.events import event_logging, log_context
from camctl.logging_runtime.service import LogChannel
from camctl.operations.models import AttemptStatus, AttemptTicket, EffectState, SettlementBasis
from camctl.operations.process import LocalExit, RawToolOutcome, ToolStartError
from camctl.operations.validation import validate_outcome

from .test_hasselblad_driver import (
    ACTIVITY_ID, DRIVER_ID, SERIAL, WORKSPACE, FakeTransport, _declaration,
    _driver, _photo_params, _request, _subcommands, host_files, LIST_OUTPUT, INFO_OUTPUT,
)


def _prepare_request(**kwargs):
    return _request(ticket=AttemptTicket(2, "control", ACTIVITY_ID, "prepare/1", 9), **kwargs)


def _prepared_start(preparation, **kwargs):
    return replace(_request(**kwargs), prepared_by=preparation.ticket)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_setting", [False, True])
async def test_preparation_logs_each_tool_with_step_and_attempt_context(host_files, monkeypatch, failed_setting):
    from camctl.operations import process as process_module
    from ..operations.test_process import FakeProcess

    async def spawn(spec, *, stdout_sink=None):
        command = spec.argv[3:]
        output = LIST_OUTPUT if command == ("list",) else INFO_OUTPUT if command == ("info",) else b""
        process = FakeProcess(output)
        process.stderr = b""
        process.exit(exit_code=17 if failed_setting and command[:2] == ("set", "exposure-mode") else 0)
        return process

    monkeypatch.setattr(process_module, "spawn_subprocess", spawn)
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    driver = _driver(None, host_files[0])
    with event_logging(channel), log_context(action_id=13):
        result = await driver.prepare_capture(_prepare_request())

    records = [json.loads(call.args[0].rendered_message()) for call in channel.slog.call_args_list]
    calls = [item for item in records if item["stage"] == "external_tool" and item["event"] == "returned"]
    names = ["list", "info", "drive_mode_single", "capture_format", "exposure_mode"]
    if failed_setting:
        assert result.outcome.error.details["step"] == "exposure_mode"
        assert calls[-1]["local_exit_code"] == 17
    else:
        names += ["iso", "white_balance_mode", "focus_mode"]
        assert result.outcome.status is AttemptStatus.SUCCEEDED
    assert [item["step_name"] for item in calls] == names
    assert len({item["tool_call_id"] for item in calls}) == len(calls)
    assert all(item["elapsed_ms"] >= 0 and item["action_id"] == 13
               and item["activity_id"] == ACTIVITY_ID and item["device_id"] == "camera-1"
               and item["run_id"] == 9 and item["attempt_id"] == 2
               and "tool_argv" in item and "timeout_s" in item for item in calls)


@pytest.mark.asyncio
async def test_availability_logs_step_names_and_device_context(monkeypatch):
    from camctl.operations import process as process_module
    from ..operations.test_process import FakeProcess

    async def spawn(spec, *, stdout_sink=None):
        output = {"list": LIST_OUTPUT, "info": INFO_OUTPUT, "doctor": b"doctor: PASS\n"}[spec.argv[3]]
        process = FakeProcess(output)
        process.stderr = b""
        process.exit(exit_code=0)
        return process

    monkeypatch.setattr(process_module, "spawn_subprocess", spawn)
    channel = create_autospec(LogChannel, instance=True, spec_set=True)
    driver = _driver(None, None)
    binding = DeviceBinding("camera-1", DRIVER_ID)
    with event_logging(channel):
        result = await driver.check_availability(binding, expected_serial=SERIAL,
            expected_product_id=10, timeout_s=Decimal("5"))
    assert result.available
    records = [json.loads(call.args[0].rendered_message()) for call in channel.slog.call_args_list]
    calls = [item for item in records if item["stage"] == "external_tool" and item["event"] == "returned"]
    assert [item["step_name"] for item in calls] == ["list", "info", "doctor"]
    assert all(item["device_id"] == "camera-1" and item["elapsed_ms"] >= 0 for item in calls)


@pytest.mark.asyncio
async def test_preparation_timeout_error_log_contains_elapsed_for_every_executed_step(host_files, monkeypatch):
    from camctl.devices.drivers.hasselblad import driver as driver_module
    from camctl.logging_runtime.diagnostics import (
        diagnostic_record_for, error_diagnostics_context, record_call_outcome,
    )

    ticks = iter([0, 2_000_000, 3_000_000, 8_000_000, 9_000_000, 16_000_000,
                  17_000_000, 28_000_000, 29_000_000, 29_029_000_000])
    monkeypatch.setattr(driver_module, "diagnostic_monotonic_ns", lambda: next(ticks))
    timeout = RawToolOutcome(LocalExit(signal=15), b"", "timeout", Decimal("3"), stderr=b"camera busy")
    driver = _driver(FakeTransport(outcomes={5: timeout}), host_files[0])
    request = _prepare_request()
    result = await driver.prepare_capture(request)
    with error_diagnostics_context(None, lambda message: None):
        state = record_call_outcome(result.outcome, ticket=request.ticket,
                                    run_error=result.outcome.error, context={"action_id": 13})
        record = json.loads(diagnostic_record_for(state).rendered_message())
    assert record["level"] == "ERROR"
    assert record["error"]["details"]["reason"] == "timeout"
    steps = record["error"]["details"]["steps"]
    assert [step["name"] for step in steps] == [
        "list", "info", "drive_mode_single", "capture_format", "exposure_mode"]
    assert [step["elapsed_ms"] for step in steps] == [2, 5, 7, 11, 29000]
    assert steps[-1]["local_signal"] == 15
    assert record["outcome"]["error"] == {"$ref": "#/error"}


@pytest.mark.asyncio
async def test_availability_error_log_contains_all_step_durations(monkeypatch):
    from camctl.devices.drivers.hasselblad import driver as driver_module
    from camctl.logging_runtime.diagnostics import (
        diagnostic_record_for, error_diagnostics_context, record_call_outcome,
    )

    ticks = iter([0, 2_000_000, 3_000_000, 8_000_000, 9_000_000, 29_000_000])
    monkeypatch.setattr(driver_module, "diagnostic_monotonic_ns", lambda: next(ticks))

    class Transport:
        async def run(self, spec, stop):
            if spec.argv[3] == "doctor":
                return RawToolOutcome(LocalExit(signal=15), b"", "timeout", Decimal("3"), stderr=b"")
            output = LIST_OUTPUT if spec.argv[3] == "list" else INFO_OUTPUT
            return RawToolOutcome(LocalExit(exit_code=0), output, None, None, stderr=b"")

    driver = _driver(Transport(), None)
    result = await driver.check_availability(DeviceBinding("camera-1", DRIVER_ID),
        expected_serial=SERIAL, expected_product_id=10, timeout_s=Decimal("5"))
    ticket = AttemptTicket(1, "availability", "31", "availability/31", 9)
    with error_diagnostics_context(None, lambda message: None):
        state = record_call_outcome(result.to_device_result("31").outcome, ticket=ticket)
        record = json.loads(diagnostic_record_for(state).rendered_message())
    assert record["level"] == "ERROR"
    assert record["error"]["details"]["reason"] == "timeout"
    assert record["error"]["details"]["steps"] == [
        {"name": "list", "elapsed_ms": 2},
        {"name": "info", "elapsed_ms": 5},
        {"name": "doctor", "elapsed_ms": 20},
    ]


@pytest.mark.asyncio
async def test_tool_start_failure_reports_attempt_elapsed_without_claiming_exit(host_files, monkeypatch):
    from camctl.devices.drivers.hasselblad import driver as driver_module
    from camctl.logging_runtime.diagnostics import (
        diagnostic_record_for, error_diagnostics_context, record_call_outcome,
    )

    ticks = iter([0, 8_000_000])
    monkeypatch.setattr(driver_module, "diagnostic_monotonic_ns", lambda: next(ticks))
    request = _prepare_request()
    result = await _driver(FakeTransport(outcomes={1: ToolStartError("cannot start")}), host_files[0]).prepare_capture(request)
    with error_diagnostics_context(None, lambda message: None):
        state = record_call_outcome(result.outcome, ticket=request.ticket)
        record = json.loads(diagnostic_record_for(state).rendered_message())
    assert record["error"]["details"]["reason"] == "tool_start_failed"
    step, = record["error"]["details"]["steps"]
    assert step["name"] == "list" and step["elapsed_ms"] == 8
    assert "start_error" in step and "local_exit_code" not in step


@pytest.mark.asyncio
async def test_prepare_settings_without_capture_relative_move_or_start_entry(host_files):
    roots, prepared, verified = host_files
    params = _photo_params()
    params["focus"] = {"mode": "MF", "relative_move": 5}
    request = _prepare_request(params=params)
    permit = StartEntryPermit("database", 1)
    permit.bind("database", replace(request.ticket, responsibility_key="start/1"))
    request = replace(request, start_entry=permit)
    transport = FakeTransport()
    driver = _driver(transport, roots)

    result = await driver.prepare_capture(request)

    assert driver.declaration.capture_preparation_supported is True
    assert driver.default_prepare_lead_s == Decimal("10")
    assert _subcommands(transport) == [
        ("list",), ("info",), ("set", "drive-mode", "single"),
        ("set", "capture-format", "raw"), ("set", "exposure-mode", "P"),
        ("set", "iso", "100"), ("set", "wb-mode", "auto"),
        ("set", "focus-mode", "MF"),
    ]
    assert prepared == verified == []
    assert permit.phase is EntryPhase.READY
    assert result.outcome.status is AttemptStatus.SUCCEEDED
    assert result.outcome.effect is EffectState.CONFIRMED
    assert [(o.type, o.version, o.data) for o in result.observations[:1]] == [
        ("capture_preparation_completed", 1, {"activity_id": ACTIVITY_ID})]
    assert [o.type for o in result.observations] == [
        "capture_preparation_completed", "photo_preparation_steps"]
    assert all("elapsed_ms" not in step for step in result.observations[1].data["steps"])
    assert result.outcome.settlement.basis is SettlementBasis.OBSERVED
    assert validate_outcome(request.ticket, result.outcome, EVIDENCE)
    assert driver.prepared_capture_matches(_prepared_start(request, params=params))


@pytest.mark.asyncio
@pytest.mark.parametrize("capture_format", ["raw", "jpeg"])
async def test_prepared_start_only_moves_focus_then_captures_and_enters_original_permit(host_files, capture_format):
    roots, prepared, verified = host_files
    params = _photo_params(capture_format)
    params["focus"] = {"mode": "MF", "relative_move": -5}
    preparation = _prepare_request(params=params)
    transport = FakeTransport(capture_format=capture_format)
    driver = _driver(transport, roots)
    await driver.prepare_capture(preparation)
    transport.calls.clear()
    request = _prepared_start(preparation, params=params)
    permit = StartEntryPermit("database", 1)
    permit.bind("database", replace(request.ticket, responsibility_key="start/1"))
    request = replace(request, ticket=permit.ticket, start_entry=permit)

    result = await driver.control(request)

    assert _subcommands(transport) == [("focus", "-5"), ("capture", "--output", str(WORKSPACE))]
    assert permit.phase is EntryPhase.ENTERED
    assert result.outcome.status is AttemptStatus.SUCCEEDED
    assert [o.type for o in result.observations] == ["photo_taken", "hasselblad_start_entered"]
    assert [s["name"] for s in result.observations[0].data["steps"]] == ["focus", "capture"]
    assert len(prepared) == len(verified) == 1
    assert validate_outcome(request.ticket, result.outcome, EVIDENCE)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["ticket", "activity", "device", "serial", "product_id", "params", "operation", "session"])
async def test_prepared_start_requires_exact_same_session_preparation_without_device_io(host_files, change):
    roots, _, _ = host_files
    preparation = _prepare_request()
    transport = FakeTransport()
    driver = _driver(transport, roots)
    await driver.prepare_capture(preparation)
    transport.calls.clear()
    request = _prepared_start(preparation)
    if change == "ticket":
        request = replace(request, prepared_by=replace(preparation.ticket, attempt_id=3))
    elif change == "activity":
        request = replace(request, ticket=replace(request.ticket, target_id="8"))
    elif change == "device":
        driver.devices = {**driver.devices, "camera-2": _declaration()}
        request = replace(request, binding=DeviceBinding("camera-2", DRIVER_ID))
    elif change == "serial":
        driver.devices = {"camera-1": _declaration(serial=SERIAL + "-changed")}
    elif change == "product_id":
        driver.devices = {"camera-1": _declaration(product_id=11)}
    elif change == "params":
        request = replace(request, params={**request.params, "capture_format": "jpeg"})
    elif change == "operation":
        request = replace(request, operation="start_recording")
    elif change == "session":
        driver.begin_session()

    assert driver.prepared_capture_matches(request) is False
    with pytest.raises(ValueError):
        await driver.control(request)
    assert transport.calls == []


@pytest.mark.asyncio
async def test_prepare_repeated_and_new_session_never_repeats_relative_focus(host_files):
    roots, _, _ = host_files
    params = _photo_params()
    params["focus"] = {"mode": "MF", "relative_move": 5}
    preparation = _prepare_request(params=params)
    transport = FakeTransport()
    driver = _driver(transport, roots)

    await driver.prepare_capture(preparation)
    await driver.prepare_capture(replace(preparation, ticket=replace(preparation.ticket, attempt_id=3)))
    driver.begin_session()
    await driver.prepare_capture(replace(preparation, ticket=replace(preparation.ticket, attempt_id=4)))

    assert all(c.argv[3] not in ("focus", "autofocus", "capture") for c in transport.calls)
    assert sum(c.argv[3] == "list" for c in transport.calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("completed", range(9))
async def test_prepare_dispatch_window_is_checked_before_each_command_and_completion(host_files, completed):
    roots, prepared, verified = host_files
    transport = FakeTransport()
    request = _prepare_request(dispatch_check=lambda: "window_elapsed" if len(transport.calls) == completed else None)
    driver = _driver(transport, roots)

    result = await driver.prepare_capture(request)

    assert len(transport.calls) == completed
    assert result.outcome.status is AttemptStatus.FAILED
    assert result.outcome.effect is EffectState.NO_EFFECT
    assert result.outcome.error.code == "window_elapsed"
    assert result.observations == ()
    assert result.outcome.settlement.basis is (
        SettlementBasis.NOT_DISPATCHED if completed == 0 else SettlementBasis.OBSERVED)
    assert prepared == verified == []
    assert not driver.prepared_capture_matches(_prepared_start(request))
    assert validate_outcome(request.ticket, result.outcome, EVIDENCE)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_call", [1, 2, 3, 8])
async def test_prepare_tool_failure_keeps_diagnostics_and_cannot_authorize_start(host_files, failed_call):
    roots, prepared, verified = host_files
    raw = RawToolOutcome(LocalExit(signal=15), b"partial", "timeout", Decimal("1.25"), stderr=b"camera busy")
    transport = FakeTransport(outcomes={failed_call: raw})
    driver = _driver(transport, roots)
    request = _prepare_request()

    result = await driver.prepare_capture(request)

    assert len(transport.calls) == failed_call
    assert result.outcome.status is AttemptStatus.FAILED
    assert result.outcome.effect is EffectState.NO_EFFECT
    assert result.observations == ()
    assert result.outcome.error.details["reason"] == "timeout"
    assert result.outcome.diagnostics.calls[-1].stderr.data == b"camera busy"
    assert result.outcome.settlement.evidence.data == {"terminate_grace_s": Decimal("1.25")}
    assert prepared == verified == []
    assert not driver.prepared_capture_matches(_prepared_start(request))
    assert validate_outcome(request.ticket, result.outcome, EVIDENCE)


@pytest.mark.asyncio
async def test_prepare_total_deadline_covers_all_settings_and_final_completion(host_files):
    roots, _, _ = host_files
    now = [0]
    transport = FakeTransport(after_call=lambda count: now.__setitem__(0, count * 1_000_000_000))
    driver = _driver(transport, roots, clock=lambda: now[0])
    request = _prepare_request(timeout=Decimal("8"))

    result = await driver.prepare_capture(request)

    assert [c.timeout_s for c in transport.calls] == [Decimal(n) for n in range(8, 0, -1)]
    assert result.outcome.status is AttemptStatus.FAILED
    assert result.outcome.error.code == "start_preparation_timeout"
    assert result.observations == ()
    assert not driver.prepared_capture_matches(_prepared_start(request))


@pytest.mark.asyncio
async def test_failed_repreparation_invalidates_previous_success(host_files):
    roots, _, _ = host_files
    transport = FakeTransport()
    driver = _driver(transport, roots)
    request = _prepare_request()
    await driver.prepare_capture(request)
    transport.outcomes[9] = RawToolOutcome(LocalExit(exit_code=2), b"", None, None, stderr=b"disconnected")

    result = await driver.prepare_capture(replace(request, ticket=replace(request.ticket, attempt_id=3)))

    assert result.outcome.status is AttemptStatus.FAILED
    assert not driver.prepared_capture_matches(_prepared_start(request))


@pytest.mark.asyncio
async def test_preparation_freezes_nested_params_before_first_device_call(host_files):
    roots, _, _ = host_files
    params = _photo_params()
    preparation = _prepare_request(params=params)
    transport = FakeTransport(after_call=lambda _: params["exposure"].update(iso=200))
    driver = _driver(transport, roots)

    await driver.prepare_capture(preparation)

    assert ("set", "iso", "100") in _subcommands(transport)
    assert driver.prepared_capture_matches(_prepared_start(preparation, params=_photo_params()))
    assert not driver.prepared_capture_matches(_prepared_start(preparation, params=params))


@pytest.mark.asyncio
async def test_autofocus_is_deferred_until_prepared_start(host_files):
    roots, _, _ = host_files
    params = {**_photo_params(), "focus": {"mode": "AF-S"}}
    preparation = _prepare_request(params=params)
    transport = FakeTransport()
    driver = _driver(transport, roots)

    await driver.prepare_capture(preparation)

    assert _subcommands(transport)[-1] == ("set", "focus-mode", "AF-S")
    transport.calls.clear()
    result = await driver.control(_prepared_start(preparation, params=params))
    assert _subcommands(transport) == [("autofocus",), ("capture", "--output", str(WORKSPACE))]
    assert result.outcome.status is AttemptStatus.SUCCEEDED


@pytest.mark.asyncio
@pytest.mark.parametrize("focus", [{"mode": "MF"}, {"mode": "MF", "relative_move": 5}])
async def test_prepared_capture_spawn_failure_uses_only_actual_start_settlement(host_files, focus):
    roots, prepared, verified = host_files
    params = {**_photo_params(), "focus": focus}
    preparation = _prepare_request(params=params)
    transport = FakeTransport()
    driver = _driver(transport, roots)
    await driver.prepare_capture(preparation)
    transport.calls.clear()
    capture_call = 2 if "relative_move" in focus else 1
    transport.outcomes[capture_call] = ToolStartError("spawn denied")

    result = await driver.control(_prepared_start(preparation, params=params))

    assert result.outcome.status is AttemptStatus.FAILED
    assert result.outcome.effect is EffectState.NO_EFFECT
    assert result.outcome.settlement.basis is (
        SettlementBasis.OBSERVED if "relative_move" in focus else SettlementBasis.NOT_DISPATCHED)
    assert len(result.outcome.diagnostics.calls) == capture_call
    assert prepared == [(roots, ACTIVITY_ID)]
    assert verified == []


@pytest.mark.asyncio
async def test_prepared_capture_timeout_is_unknown_and_never_retried(host_files):
    roots, _, verified = host_files
    preparation = _prepare_request()
    transport = FakeTransport()
    driver = _driver(transport, roots)
    await driver.prepare_capture(preparation)
    transport.calls.clear()
    transport.outcomes[1] = RawToolOutcome(
        LocalExit(signal=15), b"partial", "timeout", Decimal("3"), stderr=b"unknown capture")

    result = await driver.control(_prepared_start(preparation))

    assert _subcommands(transport) == [("capture", "--output", str(WORKSPACE))]
    assert result.outcome.status is AttemptStatus.FAILED
    assert result.outcome.effect is EffectState.UNKNOWN
    assert result.outcome.error.details["reason"] == "timeout"
    assert result.outcome.diagnostics.calls[-1].stderr.data == b"unknown capture"
    assert verified == []


@pytest.mark.asyncio
async def test_prepared_start_has_its_own_budget_after_preparation_and_waiting(host_files):
    roots, _, _ = host_files
    params = {**_photo_params(), "focus": {"mode": "MF", "relative_move": 5}}
    preparation = _prepare_request(params=params)
    now = [0]
    transport = FakeTransport(after_call=lambda _: now.__setitem__(0, now[0] + 7_000_000_000))
    driver = _driver(transport, roots, clock=lambda: now[0])
    await driver.prepare_capture(preparation)
    transport.calls.clear()
    now[0] += 1_000_000_000_000

    result = await driver.control(_prepared_start(preparation, params=params))

    assert result.outcome.status is AttemptStatus.SUCCEEDED
    assert [call.timeout_s for call in transport.calls] == [Decimal("180"), Decimal("173")]


@pytest.mark.asyncio
async def test_prepared_relative_focus_then_closed_window_preserves_lens_fact(host_files):
    roots, prepared, verified = host_files
    params = {**_photo_params(), "focus": {"mode": "MF", "relative_move": 5}}
    preparation = _prepare_request(params=params)
    transport = FakeTransport()
    driver = _driver(transport, roots)
    await driver.prepare_capture(preparation)
    transport.calls.clear()
    request = _prepared_start(preparation, params=params, dispatch_check=lambda: (
        "window_elapsed" if transport.calls else None))

    result = await driver.control(request)

    assert _subcommands(transport) == [("focus", "5")]
    assert result.outcome.status is AttemptStatus.FAILED
    assert result.outcome.effect is EffectState.NO_EFFECT
    assert result.outcome.settlement.basis is SettlementBasis.OBSERVED
    assert result.outcome.error.details["steps"][-1]["lens_operation_returned"] is True
    assert prepared == verified == []


@pytest.mark.asyncio
async def test_successful_preparation_preserves_structured_steps_through_result_encoding(host_files):
    from camctl.contracts.enums import enum_for
    from camctl.operations.result_format import read_result_document, result_document

    roots, _, _ = host_files
    transport = FakeTransport()
    driver = _driver(transport, roots)
    request = _prepare_request()

    result = await driver.prepare_capture(request)

    assert [item.type for item in result.observations] == [
        "capture_preparation_completed", "photo_preparation_steps"]
    restored = read_result_document(
        int(enum_for("operation_attempts.status").SUCCEEDED),
        int(enum_for("operation_attempts.effect_state").CONFIRMED),
        result_document(result.outcome), None)
    assert restored.observations == result.observations
    assert restored.diagnostics == result.outcome.diagnostics
    steps = restored.observations[1].data["steps"]
    assert restored.observations[1].data["activity_id"] == ACTIVITY_ID
    assert [step["name"] for step in steps] == [
        "list", "info", "drive_mode_single", "capture_format", "exposure_mode",
        "iso", "white_balance_mode", "focus_mode"]
    assert steps[0]["matching_count"] == 1
    assert steps[1]["observed_serial"] == SERIAL
    assert steps[1]["serial_verified"] is True
    assert all(step["setting_returned"] is True for step in steps[2:])
    assert all(step["local_exit_code"] == 0 for step in steps)
    assert [call.step_name for call in restored.diagnostics.calls] == [step["name"] for step in steps]
    assert validate_outcome(request.ticket, restored, EVIDENCE)

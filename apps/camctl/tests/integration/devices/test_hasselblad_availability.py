from dataclasses import replace
from decimal import Decimal
import asyncio
import os
import signal
import sys
import time

import pytest

from camctl.devices.bindings import DeviceBinding, DeviceConfigurationError
from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
from camctl.devices.drivers.hasselblad.driver import HasselbladDriver, AvailabilityCancelled
from camctl.operations.process import LocalExit, RawToolOutcome, ToolCallCancelled, ToolStartError
from camctl.operations.owned_calls import owned_tool_call
from camctl.operations.models import AttemptTicket, AttemptStatus, EffectState
from camctl.operations.result_format import result_document
from camctl.operations.validation import validate_outcome


def outcome(output, *, stderr=b""):
    return RawToolOutcome(LocalExit(exit_code=0), output, None, None, stderr=stderr)


class Transport:
    def __init__(self, overrides=None, after_call=None):
        self.calls = []
        self.overrides = overrides or {}
        self.after_call = after_call

    async def run(self, spec, stop):
        self.calls.append(spec)
        if self.after_call:
            self.after_call(len(self.calls))
        result = self.overrides.get(spec.argv[3], {
            "list": outcome(b"vid=0x2756 pid=0x000a  bus=  1 addr= 23\n"),
            "info": outcome(b"serial : CAMERA-1\n"),
            "doctor": outcome(b"doctor: PASS\n", stderr=b"warning\n"),
        }[spec.argv[3]])
        if isinstance(result, BaseException):
            raise result
        return result


def driver(transport, clock=lambda: 0):
    return HasselbladDriver({"camera-1": {"kind": "camera", "driver": DRIVER_ID,
        "hasselblad": {"executable": "/opt/hb_cli", "serial": "CAMERA-1"}}},
        None, monotonic_ns=clock, transport=transport)


BINDING = DeviceBinding("camera-1", DRIVER_ID)


async def check(driver, **kwargs):
    return await driver.check_availability(BINDING, expected_serial="CAMERA-1",
        expected_product_id=10, timeout_s=Decimal(5), **kwargs)


@pytest.mark.asyncio
async def test_shared_deadline_and_full_facts():
    now = [0]
    transport = Transport(after_call=lambda _: now.__setitem__(0, now[0] + 1_000_000_000))
    result = await check(driver(transport, lambda: now[0]))
    assert result.available and result.reason is None
    assert result.expected_serial == result.observed_serial == "CAMERA-1"
    assert result.expected_product_id == 10
    assert [call.name for call in result.calls] == ["list", "info", "doctor"]
    assert [call.returned_monotonic_ns for call in result.calls] == [1_000_000_000, 2_000_000_000, 3_000_000_000]
    assert result.calls[-1].outcome.stderr == b"warning\n"
    assert [call.timeout_s for call in transport.calls] == [Decimal(5), Decimal(4), Decimal(3)]
    assert all(call.argv[:3] == ("/opt/hb_cli", "--pid", "0x000a") for call in transport.calls)


@pytest.mark.asyncio
async def test_result_converts_to_registered_check_evidence_and_diagnostics():
    checked_driver = driver(Transport())
    result = await check(checked_driver)
    device_result = result.to_device_result("31")
    outcome = device_result.outcome
    ticket = AttemptTicket(attempt_id=1, operation="availability", target_id="31",
                           responsibility_key="availability/31", run_id=1)
    validate_outcome(ticket, outcome, checked_driver.evidence)
    assert outcome.status is AttemptStatus.SUCCEEDED and outcome.effect is EffectState.NO_EFFECT
    data = outcome.observations[0].data
    assert data["check_id"] == "31" and data["observed_serial"] == "CAMERA-1"
    assert [step["name"] for step in data["steps"]] == ["list", "info", "doctor"]
    assert all(step["exit"] == {"exit_code": 0, "signal": None} for step in data["steps"])
    assert all(step["error"] is None for step in data["steps"])
    encoded = result_document(outcome)
    assert bytes.fromhex(encoded["diagnostics"]["calls"][-1]["stderr"]["hex"]) == b"warning\n"


@pytest.mark.asyncio
async def test_failure_conversion_keeps_original_exception_and_partial_streams():
    error = ToolStartError("exec failed")
    result = await check(driver(Transport({"info": error})))
    device_result = result.to_device_result("31")
    assert device_result.diagnostic_error is error
    assert device_result.outcome.status is AttemptStatus.FAILED
    steps = device_result.outcome.observations[0].data["steps"]
    assert steps[1]["exit"] is None and steps[1]["error"]["exception"]["message"] == "exec failed"
    assert result_document(device_result.outcome)["diagnostics"]["calls"][1]["stdout"]["hex"] is None


@pytest.mark.asyncio
async def test_unsettled_result_cannot_be_presented_as_settled_outcome():
    result = await check(driver(Transport({"info": RuntimeError("lost ownership")})))
    with pytest.raises(ValueError):
        result.to_device_result("31")


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides, reason, last", [
    ({"list": outcome(b"(no Hasselblad USB devices found)\n")}, "no_matching_device", "list"),
    ({"list": outcome(b"vid=0x2756 pid=0x000a  bus=  1 addr= 23\nvid=0x2756 pid=0x000a  bus=  1 addr= 24\n")}, "multiple_matching_devices", "list"),
    ({"list": outcome(b"broken\n")}, "invalid_response", "list"),
    ({"info": outcome(b"serial : DIFFERENT\n")}, "serial_mismatch", "info"),
    ({"doctor": outcome(b"doctor: FAIL\n")}, "doctor_not_passed", "doctor"),
    ({"doctor": outcome(b"not doctor: PASS\n")}, "doctor_not_passed", "doctor"),
    ({"doctor": outcome(b"doctor: PASS\ndoctor: FAIL\n")}, "doctor_not_passed", "doctor"),
    ({"doctor": outcome(b"doctor: PASS\n", stderr=b"doctor: FAIL\n")}, "doctor_not_passed", "doctor"),
    ({"doctor": outcome(b"doctor: PASS\n\xff")}, "invalid_response", "doctor"),
])
async def test_rejects_identity_and_doctor_failures(overrides, reason, last):
    transport = Transport(overrides)
    result = await check(driver(transport))
    assert not result.available and result.reason == reason
    assert result.calls[-1].name == transport.calls[-1].argv[3] == last


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [
    replace(outcome(b"doctor: PASS\n"), exit=LocalExit(exit_code=2)),
    replace(outcome(b"doctor: PASS\n"), exit=LocalExit(signal=15)),
    replace(outcome(b"doctor: PASS\n"), error="timeout"),
    replace(outcome(b"doctor: PASS\n"), output_failure="stderr: output_limit_exceeded"),
    replace(outcome(b"doctor: PASS\n"), output=None),
    replace(outcome(b"doctor: PASS\n"), stderr=None),
])
async def test_pass_does_not_override_incomplete_or_failed_call(raw):
    result = await check(driver(Transport({"doctor": raw})))
    assert not result.available
    assert result.calls[-1].outcome is raw


@pytest.mark.asyncio
async def test_start_exception_preserves_prior_results_and_exception():
    error = ToolStartError("exec failed")
    result = await check(driver(Transport({"info": error})))
    assert result.reason == "tool_start_failed"
    assert result.calls[0].outcome is not None
    assert result.calls[-1].error is error and result.calls[-1].outcome is None


@pytest.mark.asyncio
async def test_transport_exception_preserves_error_and_unconfirmed_settlement():
    error = RuntimeError("transport lost ownership")
    result = await check(driver(Transport({"info": error})))
    assert result.reason == "transport_failed" and not result.settled
    assert result.calls[-1].error is error


@pytest.mark.asyncio
async def test_deadline_prevents_next_dispatch():
    now = [0]
    transport = Transport(after_call=lambda _: now.__setitem__(0, 5_000_000_000))
    result = await check(driver(transport, lambda: now[0]))
    assert result.reason == "deadline_elapsed" and len(transport.calls) == 1


@pytest.mark.asyncio
async def test_cancelled_call_propagates_with_all_results():
    raw = replace(outcome(b"partial", stderr=b"error"), error="cancelled")
    transport = Transport({"info": ToolCallCancelled(raw)})
    with pytest.raises(AvailabilityCancelled) as exc:
        await check(driver(transport))
    assert exc.value.result.reason == "cancelled"
    assert [call.name for call in exc.value.result.calls] == ["list", "info"]
    assert exc.value.result.calls[-1].outcome is raw


@pytest.mark.asyncio
@pytest.mark.parametrize("serial, product", [("OTHER", 10), ("CAMERA-1", 11)])
async def test_changed_binding_rejected_before_dispatch(serial, product):
    transport = Transport()
    with pytest.raises(DeviceConfigurationError):
        await driver(transport).check_availability(BINDING, expected_serial=serial,
            expected_product_id=product, timeout_s=Decimal(5))
    assert transport.calls == []


@pytest.mark.asyncio
async def test_complete_doctor_result_survives_cancellation_race():
    raw = outcome(b"doctor: PASS\n")
    with pytest.raises(AvailabilityCancelled) as exc:
        await check(driver(Transport({"doctor": ToolCallCancelled(raw)})))
    assert exc.value.result.available
    assert exc.value.result.calls[-1].outcome is raw


@pytest.mark.skipif(os.name == "nt", reason="受管脚本使用 POSIX 可执行入口")
@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [0, 2])
async def test_real_process_pass_requires_zero_exit_and_keeps_streams(tmp_path, exit_code):
    executable = tmp_path / "hb_cli"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "assert sys.argv[1:3] == ['--pid', '0x000a']\n"
        "if sys.argv[3] == 'list': print('vid=0x2756 pid=0x000a  bus=  1 addr= 23')\n"
        "elif sys.argv[3] == 'info': print('serial : CAMERA-1')\n"
        "elif sys.argv[3] == 'doctor':\n"
        "    print('doctor: PASS')\n"
        "    print('diagnostic', file=sys.stderr)\n"
        f"    sys.exit({exit_code})\n", encoding="utf-8")
    executable.chmod(0o700)
    device = {"kind": "camera", "driver": DRIVER_ID,
              "hasselblad": {"executable": str(executable), "serial": "CAMERA-1"}}
    real_driver = HasselbladDriver({"camera-1": device}, None, monotonic_ns=time.monotonic_ns)
    result = await check(real_driver)
    assert result.available is (exit_code == 0)
    assert result.reason == (None if exit_code == 0 else "exit_nonzero")
    assert [call.name for call in result.calls] == ["list", "info", "doctor"]
    raw = result.calls[-1].outcome
    assert raw.exit.exit_code == exit_code
    assert raw.output == b"doctor: PASS\n" and raw.stderr == b"diagnostic\n"


@pytest.mark.skipif(os.name == "nt", reason="受管脚本使用 POSIX 信号")
@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_real_process_settles_and_preserves_streams_before_result(tmp_path, cancel):
    marker = tmp_path / "started"
    executable = tmp_path / "hb_cli"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, signal, sys, time\n"
        "assert sys.argv[1:3] == ['--pid', '0x000a']\n"
        "if sys.argv[3] == 'list': print('vid=0x2756 pid=0x000a  bus=  1 addr= 23')\n"
        "elif sys.argv[3] == 'info': print('serial : CAMERA-1')\n"
        "elif sys.argv[3] == 'doctor':\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    print('doctor: PASS', flush=True)\n"
        "    print('diagnostic', file=sys.stderr, flush=True)\n"
        f"    pathlib.Path({str(marker)!r}).write_text('started')\n"
        "    time.sleep(30)\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    device = {"kind": "camera", "driver": DRIVER_ID,
              "hasselblad": {"executable": str(executable), "serial": "CAMERA-1"}}
    real_driver = HasselbladDriver({"camera-1": device}, None,
                                  monotonic_ns=time.monotonic_ns, terminate_grace_s=Decimal("0.05"))
    saved = []

    @owned_tool_call
    async def consume():
        value = await real_driver.check_availability(BINDING, expected_serial="CAMERA-1",
            expected_product_id=10, timeout_s=Decimal("0.5") if not cancel else Decimal(10))
        saved.append(value)

    task = asyncio.create_task(consume())
    async with asyncio.timeout(5):
        while not marker.exists():
            await asyncio.sleep(0.01)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    assert len(saved) == 1
    result = saved[0]
    assert not result.available and result.settled
    assert result.reason == ("cancelled" if cancel else "timeout")
    assert [call.name for call in result.calls] == ["list", "info", "doctor"]
    raw = result.calls[-1].outcome
    assert raw.exit.signal == signal.SIGKILL and raw.used_grace_s == Decimal("0.05")
    assert raw.output == b"doctor: PASS\n" and raw.stderr == b"diagnostic\n"

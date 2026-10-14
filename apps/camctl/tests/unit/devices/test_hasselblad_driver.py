"""Hasselblad 控制只在身份、调用与主机文件事实齐备时确认拍摄。"""

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.devices.bindings import DeviceBinding
from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID, PARAMETER_TYPE
from camctl.devices.drivers.hasselblad.driver import EVIDENCE, HasselbladDriver
from camctl.devices.evidence import DeviceObservation
from camctl.devices.ports import ControlRequest, DriverDeclaration
from camctl.host_files.capture_sources import CapturedArtifact, CaptureFileSpec
from camctl.host_files.models import BoundDirectories
from camctl.operations.models import (
    AttemptStatus,
    AttemptTicket,
    EffectState,
    SettlementBasis,
)
from camctl.operations.process import LocalExit, RawToolOutcome, ToolCallCancelled, ToolStartError
from camctl.operations.validation import validate_outcome


ACTIVITY_ID = "7"
SERIAL = "CFV100C-001"
EXECUTABLE = "/opt/hasselblad/bin/hb_cli"
WORKSPACE = Path("/bounded/staging/capture-work/7")
ORIGINAL_NAME = "capture_22544386.3FR"
PREVIEW_NAME = f"{ORIGINAL_NAME}.thumbnail.jpg"
ORIGINAL_SIZE = 211_222_528
PREVIEW_SIZE = 689_251
ORIGINAL_SHA256 = "a" * 64
PREVIEW_SHA256 = "b" * 64

LIST_OUTPUT = b"vid=0x2756 pid=0x000a  bus=  1 addr= 23\n"
INFO_OUTPUT = b"serial : CFV100C-001\nfirmware : v4.1.0\n"


class FakeTransport:
    def __init__(self, *, outcomes=None, after_call=None):
        self.calls = []
        self.outcomes = outcomes or {}
        self.after_call = after_call

    async def run(self, spec, stop):
        self.calls.append(spec)
        if self.after_call is not None:
            self.after_call(len(self.calls))
        result = self.outcomes.get(len(self.calls))
        if isinstance(result, BaseException):
            raise result
        if result is not None:
            return result

        command = spec.argv[3]
        output = {
            "list": LIST_OUTPUT,
            "info": INFO_OUTPUT,
            "set": b"",
            "capture": _capture_output(WORKSPACE),
        }[command]
        return RawToolOutcome(LocalExit(exit_code=0), output, None, None, stderr=b"")


def _capture_output(workspace):
    prefix = str(workspace)
    return (
        f"capture: OK\n"
        f"saved {prefix}/{ORIGINAL_NAME} ({ORIGINAL_SIZE} bytes)\n"
        f"saved {prefix}/{PREVIEW_NAME} ({PREVIEW_SIZE} bytes)\n"
    ).encode()


def _declaration(serial=SERIAL, product_id=10):
    return {
        "kind": "camera",
        "driver": DRIVER_ID,
        "hasselblad": {
            "executable": EXECUTABLE,
            "serial": serial,
            "product_id": product_id,
        },
    }


def _request(*, device_id="camera-1", params=None, timeout=Decimal("180"),
             ticket=None, dispatch_check=lambda: None):
    return ControlRequest(
        "take_photo",
        DeviceBinding(device_id, DRIVER_ID),
        {"type": PARAMETER_TYPE} if params is None else params,
        ticket=AttemptTicket(1, "control", ACTIVITY_ID, "capture/1", 9) if ticket is None else ticket,
        timeout_s=timeout,
        dispatch_check=dispatch_check,
    )


def _artifacts():
    return (
        CapturedArtifact(ORIGINAL_NAME, ORIGINAL_SIZE, ORIGINAL_SHA256, "image/x-hasselblad-3fr"),
        CapturedArtifact(PREVIEW_NAME, PREVIEW_SIZE, PREVIEW_SHA256, "image/jpeg"),
    )


@pytest.fixture
def host_files(monkeypatch):
    from camctl.devices.drivers.hasselblad import driver as driver_module

    prepared = []
    verified = []
    roots = BoundDirectories(Path("/bounded/staging"))

    async def prepare(actual_roots, activity_id):
        prepared.append((actual_roots, activity_id))
        return WORKSPACE

    async def verify(actual_roots, activity_id, specs):
        verified.append((actual_roots, activity_id, specs))
        return _artifacts()

    monkeypatch.setattr(driver_module, "prepare_capture_workspace", prepare)
    monkeypatch.setattr(driver_module, "verify_capture_files", verify)
    return roots, prepared, verified


def _driver(transport, roots, *, clock=lambda: 0, devices=None):
    return HasselbladDriver(
        {"camera-1": _declaration()} if devices is None else devices,
        roots,
        monotonic_ns=clock,
        transport=transport,
    )


def _subcommands(transport):
    return [call.argv[3:] for call in transport.calls]


@pytest.mark.asyncio
@pytest.mark.parametrize("control_request, message", [
    (_request(ticket=AttemptTicket(1, "stop", ACTIVITY_ID, "capture/1", 9)), "已提交的 control"),
    (_request(timeout=Decimal("NaN")), "有限正 Decimal"),
    (_request(params={"type": "other"}), "已登记照片类型"),
    (ControlRequest(
        "start_recording", DeviceBinding("camera-1", DRIVER_ID),
        {"type": PARAMETER_TYPE}, AttemptTicket(1, "control", ACTIVITY_ID, "capture/1", 9),
        Decimal("180"), lambda: None,
    ), "只接受 take_photo"),
])
async def test_invalid_control_contract_fails_before_any_tool_dispatch(host_files, control_request, message):
    roots, _prepared, _verified = host_files
    transport = FakeTransport()

    with pytest.raises(ValueError, match=message):
        await _driver(transport, roots).control(control_request)

    assert transport.calls == []


@pytest.mark.asyncio
async def test_success_requires_matching_device_and_two_verified_host_files(host_files):
    roots, prepared, verified = host_files
    transport = FakeTransport()
    driver = _driver(transport, roots)

    result = await driver.control(_request())

    assert _subcommands(transport) == [
        ("list",),
        ("info",),
        ("set", "drive-mode", "single"),
        ("set", "capture-format", "raw"),
        ("capture", "--output", str(WORKSPACE)),
    ]
    assert all(call.argv[:3] == (EXECUTABLE, "--pid", "0x000a") for call in transport.calls)
    assert [call.timeout_s for call in transport.calls] == [Decimal("180")] * 5
    assert prepared == [(roots, ACTIVITY_ID)]
    assert verified == [
        (roots, ACTIVITY_ID, (
            CaptureFileSpec(ORIGINAL_NAME, ORIGINAL_SIZE, "image/x-hasselblad-3fr"),
            CaptureFileSpec(PREVIEW_NAME, PREVIEW_SIZE, "image/jpeg"),
        ))
    ]
    assert result.outcome.status is AttemptStatus.SUCCEEDED
    assert result.outcome.effect is EffectState.CONFIRMED
    assert result.outcome.error is None
    assert result.outcome.call_info.local_exit_code == 0
    assert result.observations == result.outcome.observations
    photo = result.observations[0]
    assert photo == DeviceObservation(
        "photo_taken", 1,
        {
            "activity_id": ACTIVITY_ID,
            "original": {
                "original_name": ORIGINAL_NAME,
                "size_bytes": ORIGINAL_SIZE,
                "sha256": ORIGINAL_SHA256,
                "media_type": "image/x-hasselblad-3fr",
            },
            "preview": {
                "original_name": PREVIEW_NAME,
                "size_bytes": PREVIEW_SIZE,
                "sha256": PREVIEW_SHA256,
                "media_type": "image/jpeg",
            },
            "steps": photo.data["steps"],
        },
    )
    assert set(photo.data) == {"activity_id", "original", "preview", "steps"}
    assert set(photo.data["original"]) == {"original_name", "size_bytes", "sha256", "media_type"}
    assert set(photo.data["preview"]) == {"original_name", "size_bytes", "sha256", "media_type"}
    assert len(photo.data["steps"]) == 5
    assert all("used_grace_s" not in step for step in photo.data["steps"])
    assert result.outcome.settlement.evidence.data == {}
    assert [step["name"] for step in photo.data["steps"]] == [
        "list", "info", "drive_mode_single", "capture_format_raw", "capture"
    ]
    assert validate_outcome(_request().ticket, result.outcome, EVIDENCE)


@pytest.mark.asyncio
@pytest.mark.parametrize("list_output", [
    b"(no Hasselblad USB devices found)\n",
    LIST_OUTPUT + LIST_OUTPUT,
])
async def test_zero_or_multiple_matching_usb_devices_never_reach_info_or_capture(host_files, list_output):
    roots, _prepared, _verified = host_files
    transport = FakeTransport(outcomes={1: RawToolOutcome(LocalExit(exit_code=0), list_output, None, None)})

    result = await _driver(transport, roots).control(_request())

    assert len(transport.calls) == 1
    assert result.outcome.status is AttemptStatus.FAILED
    assert result.outcome.effect is EffectState.NO_EFFECT
    assert result.outcome.observations == ()
    assert result.error["code"] == "device_error"
    assert result.error["details"]["step"] == "list"
    assert result.error["details"]["reason"] in {"no_matching_device", "multiple_matching_devices"}


@pytest.mark.asyncio
async def test_list_ignores_other_usb_ids_and_info_must_match_configured_serial(host_files):
    roots, _prepared, _verified = host_files
    list_output = (
        b"vid=0x1111 pid=0x000a  bus=  1 addr= 22\n"
        b"vid=0x2756 pid=0x000b  bus=  1 addr= 23\n"
        + LIST_OUTPUT
    )
    transport = FakeTransport(outcomes={
        1: RawToolOutcome(LocalExit(exit_code=0), list_output, None, None),
        2: RawToolOutcome(LocalExit(exit_code=0), b"serial : CFV100C-OTHER\nfirmware : v4.1.0\n", None, None),
    })

    result = await _driver(transport, roots).control(_request())

    assert _subcommands(transport) == [("list",), ("info",)]
    assert result.outcome.status is AttemptStatus.FAILED
    assert result.outcome.effect is EffectState.NO_EFFECT
    assert result.error["code"] == "device_error"
    assert result.error["details"]["step"] == "info"
    assert result.error["details"]["reason"] == "serial_mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_step", [3, 4])
async def test_setting_failure_is_recorded_and_blocks_capture(host_files, failed_step):
    roots, _prepared, _verified = host_files
    transport = FakeTransport(outcomes={failed_step: RawToolOutcome(
        LocalExit(exit_code=2), b"setting rejected\n", None, None, stderr=b"camera busy\n")
    })

    result = await _driver(transport, roots).control(_request())

    assert len(transport.calls) == failed_step
    assert transport.calls[-1].argv[3] == "set"
    assert result.outcome.status is AttemptStatus.FAILED
    assert result.outcome.effect is EffectState.NO_EFFECT
    assert result.outcome.call_info.local_exit_code == 2
    assert result.error["code"] == "device_error"
    assert result.error["details"]["steps"][-1]["stderr_hex"] == "63616d65726120627573790a"


@pytest.mark.asyncio
@pytest.mark.parametrize("completed_calls", range(5))
async def test_dispatch_check_blocks_each_next_command_without_retry(host_files, completed_calls):
    roots, prepared, _verified = host_files
    transport = FakeTransport()
    request = _request(dispatch_check=lambda: "canceled" if len(transport.calls) == completed_calls else None)

    result = await _driver(transport, roots).control(request)

    assert len(transport.calls) == completed_calls
    assert prepared == []
    assert result.outcome.status is AttemptStatus.FAILED
    assert result.outcome.effect is EffectState.NO_EFFECT
    assert result.outcome.error.code == "canceled"
    if completed_calls == 0:
        assert result.outcome.settlement.basis is SettlementBasis.NOT_DISPATCHED
        assert result.outcome.call_info is None
    else:
        assert result.outcome.settlement.basis is SettlementBasis.OBSERVED
        assert result.outcome.call_info.local_exit_code == 0


@pytest.mark.asyncio
async def test_one_deadline_is_shared_by_all_tool_dispatches_and_can_block_capture(host_files):
    roots, prepared, _verified = host_files
    now = [0]

    def after_call(count):
        if count <= 4:
            now[0] += 45_000_000_000

    transport = FakeTransport(after_call=after_call)
    result = await _driver(transport, roots, clock=lambda: now[0]).control(_request())

    assert [call.timeout_s for call in transport.calls] == [
        Decimal("180"), Decimal("135"), Decimal("90"), Decimal("45")
    ]
    assert len(transport.calls) == 4
    assert prepared == []
    assert result.outcome.status is AttemptStatus.FAILED
    assert result.outcome.effect is EffectState.NO_EFFECT
    assert result.error["code"] == "start_preparation_timeout"
    assert result.error["details"]["reason"] == "deadline_elapsed"


@pytest.mark.asyncio
async def test_owned_call_interruption_blocks_following_dispatch(host_files, monkeypatch):
    from camctl.devices.drivers.hasselblad import driver as driver_module

    roots, _prepared, _verified = host_files
    transport = FakeTransport()

    class Interrupted:
        interrupted = RuntimeError("cancel requested")

    monkeypatch.setattr(driver_module, "current_call_scope", lambda: Interrupted())
    result = await _driver(transport, roots).control(_request())

    assert len(transport.calls) == 0
    assert result.outcome.effect is EffectState.NO_EFFECT
    assert result.outcome.settlement.basis is SettlementBasis.NOT_DISPATCHED
    assert result.outcome.error.code == "transport_cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("completed_calls", range(1, 5))
async def test_owned_call_interruption_after_each_return_blocks_next_dispatch(
    host_files, monkeypatch, completed_calls
):
    from camctl.devices.drivers.hasselblad import driver as driver_module

    roots, _prepared, verified = host_files
    scope = type("Scope", (), {"interrupted": None})()
    monkeypatch.setattr(driver_module, "current_call_scope", lambda: scope)

    def after_call(count):
        if count == completed_calls:
            scope.interrupted = asyncio.CancelledError("cancel requested")

    transport = FakeTransport(after_call=after_call)
    result = await _driver(transport, roots).control(_request())

    assert len(transport.calls) == completed_calls
    assert result.outcome.status is AttemptStatus.FAILED
    assert result.outcome.effect is EffectState.NO_EFFECT
    assert result.outcome.error.code == "transport_cancelled"
    assert result.outcome.settlement.basis is SettlementBasis.OBSERVED
    assert result.outcome.call_info.local_exit_code == 0
    assert result.outcome.observations == ()
    assert verified == []


@pytest.mark.asyncio
async def test_tool_start_error_before_capture_is_no_effect_and_keeps_prior_local_exit(host_files):
    roots, _prepared, _verified = host_files
    transport = FakeTransport(outcomes={5: ToolStartError("spawn denied")})

    result = await _driver(transport, roots).control(_request())

    assert len(transport.calls) == 5
    assert result.outcome.status is AttemptStatus.FAILED
    assert result.outcome.effect is EffectState.NO_EFFECT
    assert result.outcome.call_info.local_exit_code == 0
    assert result.outcome.observations == ()
    assert result.error["details"]["reason"] == "tool_start_failed"


@pytest.mark.asyncio
async def test_tool_start_error_before_first_process_is_reliably_not_dispatched(host_files):
    roots, _prepared, _verified = host_files
    transport = FakeTransport(outcomes={1: ToolStartError("spawn denied")})

    result = await _driver(transport, roots).control(_request())

    assert len(transport.calls) == 1
    assert result.outcome.status is AttemptStatus.FAILED
    assert result.outcome.effect is EffectState.NO_EFFECT
    assert result.outcome.settlement.basis is SettlementBasis.NOT_DISPATCHED
    assert result.outcome.call_info is None
    assert result.outcome.observations == ()
    assert result.error["code"] == "device_error"
    assert result.error["details"]["reason"] == "tool_start_failed"


@pytest.mark.asyncio
async def test_exception_after_capture_dispatch_preserves_unsettled_attempt_without_retry(host_files):
    roots, prepared, verified = host_files
    transport = FakeTransport(outcomes={5: RuntimeError("runner lost its result")})

    with pytest.raises(RuntimeError, match="runner lost its result"):
        await _driver(transport, roots).control(_request())

    assert len(transport.calls) == 5
    assert transport.calls[-1].argv[3] == "capture"
    assert prepared == [(roots, ACTIVITY_ID)]
    assert verified == []


@pytest.mark.asyncio
async def test_capture_timeout_keeps_workspace_and_does_not_emit_photo_taken(host_files):
    roots, prepared, verified = host_files
    transport = FakeTransport(outcomes={5: RawToolOutcome(
        LocalExit(signal=15), b"capture: OK\n", "timeout", Decimal("3"), stderr=b"timed out\n")
    })

    result = await _driver(transport, roots).control(_request())

    assert len(transport.calls) == 5
    assert prepared == [(roots, ACTIVITY_ID)]
    assert verified == []
    assert result.outcome.status is AttemptStatus.UNKNOWN
    assert result.outcome.effect is EffectState.UNKNOWN
    assert result.outcome.call_info.local_signal == 15
    assert result.outcome.observations == ()
    assert result.outcome.settlement.evidence.data == {"terminate_grace_s": Decimal("3")}
    assert result.error["details"]["steps"][-1]["used_grace_s"] == Decimal("3")
    assert result.error["details"]["steps"][-1]["stderr_hex"] == "74696d6564206f75740a"


@pytest.mark.asyncio
@pytest.mark.parametrize(("call_number", "step_name"), [
    (1, "list"),
    (2, "info"),
    (3, "drive_mode_single"),
    (4, "capture_format_raw"),
    (5, "capture"),
])
async def test_step_records_only_its_actual_termination_grace(host_files, call_number, step_name):
    roots, _prepared, _verified = host_files
    grace = Decimal("1.25")
    transport = FakeTransport(outcomes={call_number: RawToolOutcome(
        LocalExit(signal=15), b"", "timeout", grace, stderr=b"timed out\n")
    })

    result = await _driver(transport, roots).control(_request())

    steps = result.error["details"]["steps"]
    assert [step["name"] for step in steps] == [
        "list", "info", "drive_mode_single", "capture_format_raw", "capture"
    ][:call_number]
    assert steps[-1]["name"] == step_name
    assert steps[-1]["used_grace_s"] == grace
    assert all("used_grace_s" not in step for step in steps[:-1])
    assert result.outcome.settlement.evidence.data == {"terminate_grace_s": grace}


@pytest.mark.asyncio
async def test_tool_call_cancelled_uses_its_actual_local_outcome(host_files):
    roots, _prepared, verified = host_files
    transport = FakeTransport(outcomes={5: ToolCallCancelled(RawToolOutcome(
        LocalExit(signal=15), b"capture: OK\n", "cancelled", Decimal("3"), stderr=b"stopped\n")
    )})

    result = await _driver(transport, roots).control(_request())

    assert len(transport.calls) == 5
    assert result.outcome.status is AttemptStatus.UNKNOWN
    assert result.outcome.effect is EffectState.UNKNOWN
    assert result.outcome.call_info.local_signal == 15
    assert result.outcome.settlement.basis is SettlementBasis.OBSERVED
    assert result.outcome.settlement.evidence.data == {"terminate_grace_s": Decimal("3")}
    assert result.error["details"]["steps"][-1]["used_grace_s"] == Decimal("3")
    assert result.outcome.observations == ()
    assert verified == []


@pytest.mark.asyncio
async def test_unparseable_capture_response_is_unknown_even_after_local_exit_zero(host_files):
    roots, _prepared, verified = host_files
    transport = FakeTransport(outcomes={5: RawToolOutcome(
        LocalExit(exit_code=0), b"capture: OK\n", None, None, stderr=b""
    )})

    result = await _driver(transport, roots).control(_request())

    assert verified == []
    assert result.outcome.status is AttemptStatus.UNKNOWN
    assert result.outcome.effect is EffectState.UNKNOWN
    assert result.outcome.call_info.local_exit_code == 0
    assert result.outcome.observations == ()
    assert result.error["details"]["reason"] == "capture_response_unconfirmed"


@pytest.mark.asyncio
async def test_file_verification_failure_keeps_unknown_photo_workspace(host_files, monkeypatch):
    from camctl.devices.drivers.hasselblad import driver as driver_module
    from camctl.host_files.capture_sources import CaptureFileError

    roots, prepared, _verified = host_files

    async def missing_original(_roots, _activity_id, _specs):
        raise CaptureFileError("source_open", WORKSPACE / ORIGINAL_NAME, detail="文件不存在")

    monkeypatch.setattr(driver_module, "verify_capture_files", missing_original)
    transport = FakeTransport()

    result = await _driver(transport, roots).control(_request())

    assert len(transport.calls) == 5
    assert prepared == [(roots, ACTIVITY_ID)]
    assert result.outcome.status is AttemptStatus.UNKNOWN
    assert result.outcome.effect is EffectState.UNKNOWN
    assert result.outcome.observations == ()
    assert result.error["details"]["step"] == "verify_files"
    assert result.error["details"]["reason"] == "source_open"


@pytest.mark.asyncio
async def test_configuration_updates_are_visible_to_the_same_registered_driver(host_files):
    roots, prepared, _verified = host_files
    transport = FakeTransport()
    driver = _driver(transport, roots)
    updated_roots = BoundDirectories(Path("/updated/staging"))
    updated = _declaration(serial="CFV100C-002", product_id=11)
    driver.devices = {"camera-2": updated}
    driver.roots = updated_roots
    transport.outcomes[1] = RawToolOutcome(
        LocalExit(exit_code=0), LIST_OUTPUT.replace(b"000a", b"000b"), None, None
    )
    transport.outcomes[2] = RawToolOutcome(
        LocalExit(exit_code=0), b"serial : CFV100C-002\nfirmware : v4.1.0\n", None, None
    )

    result = await driver.control(_request(device_id="camera-2"))

    assert all(call.argv[:3] == (EXECUTABLE, "--pid", "0x000b") for call in transport.calls)
    assert prepared == [(updated_roots, ACTIVITY_ID)]
    assert result.outcome.effect is EffectState.CONFIRMED
    assert driver.declaration == DriverDeclaration(
        control_supported=True,
        stop_supported=False,
        query_supported=False,
        result_supported=False,
        read_supported=True,
        digest_supported=False,
        delete_supported=False,
        directory_supported=False,
        local_read_recovery_supported=True,
    )

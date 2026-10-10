"""真实录像结果入口的停止上下文、等待与分页契约。"""

import asyncio
from copy import deepcopy
from decimal import Decimal

import pytest

from camctl.contracts.enums import enum_for
from camctl.devices.bindings import DeviceBinding
from camctl.devices.directory import DirectoryCursor
from camctl.devices.drivers.adb_cameras.commands import CameraModel
from camctl.devices.drivers.adb_cameras.driver import AdbCameraDriver
from camctl.devices.drivers.adb_cameras.registration import builtin_camera_contracts
from camctl.devices.ports import ControlRequest
from camctl.operations.models import AttemptStatus, AttemptTicket, EffectState
from camctl.operations.process import LocalExit, RawToolOutcome


BINDING = DeviceBinding("camera", CameraModel.ACTION6)
ROOT = "/mnt/media_rw/emulated/DCIM"
VIDEO = ROOT + "/DJI_001/new.MP4"
TICKET = AttemptTicket(1, "result", "7", "results/7", 9)


def _raw(output, *, exit_code=0, error=None, stderr=b""):
    return RawToolOutcome(LocalExit(exit_code=exit_code), output, None, error, stderr=stderr)


def _frame(*paths, more=False):
    return b"\0".join((b"CAMCTL-DIRECTORY/1", *(path.encode() for path in paths),
                       b"MORE" if more else b"END", b""))


def _context():
    return {"activity_id": "7", "stop_result_event_id": 30, "stop_returned_at_us": 1000,
            "stop_response": {"status": int(enum_for("operation_attempts.status").SUCCEEDED),
                              "effect_state": int(enum_for("operation_attempts.effect_state").CONFIRMED),
                              "error": None, "result": {"format_version": 1,
                                  "settlement": {"basis": "observed", "evidence": {
                                      "type": "stop_returned", "version": 1, "data": {}}},
                                  "observations": [{"type": "stop_confirmed", "version": 1, "data": {
                                      "activity_id": "7", "response_payload": "00",
                                      "confirmation_basis": "action6_record_ack/v1"}}]}},
            "file_completion_wait_ms": 5000, "prior_completion": None}


def _completion():
    return {"method": "stop_return_and_wait", "version": 1, "activity_id": "7",
            "stop_result_event_id": 30, "required_wait_ms": 5000,
            "observed_wait_ns": 5_000_000_000, "completed": True}


class Transport:
    def __init__(self, responses, clock):
        self.responses = list(responses)
        self.specs = []
        self.clock = clock

    async def run(self, spec, stop):
        self.specs.append(spec)
        self.clock[0] += 10_000_000
        return self.responses.pop(0)


def _driver(monkeypatch, responses, *, wait_error=False):
    contract = next(item for item in builtin_camera_contracts() if item.driver_id == CameraModel.ACTION6)
    assert callable(getattr(contract, "result_reader", None))
    clock, waits = [0], []

    async def sleep(seconds):
        waits.append(seconds)
        if wait_error:
            raise asyncio.CancelledError()
        clock[0] += int(Decimal(str(seconds)) * 1_000_000_000)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    transport = Transport(responses, clock)
    driver = AdbCameraDriver(contract,
        {"camera": {"driver": CameraModel.ACTION6, "adb": {"serial": "serial-1"}}}, transport,
        terminate_grace_s=Decimal("1"), monotonic_ns=lambda: clock[0])
    return driver, transport, waits


async def _list(driver, *, context=None, cursor=None, timeout_s=Decimal("10")):
    params = {"activity_id": "7", "output_scope": {"directories": [ROOT]}}
    if context is not None:
        params["completion_context"] = context
    if cursor is not None:
        params["cursor"] = cursor.as_json()
    return await driver.list_results(ControlRequest("result", BINDING, params,
                                                   TICKET, timeout_s), 3)


def _page(result):
    return next(item.data for item in result.observations if item.type == "result_files_listed")


@pytest.mark.asyncio
async def test_known_stop_waits_five_seconds_before_listing_and_finalizes_mp4(monkeypatch):
    driver, transport, waits = _driver(monkeypatch, [_raw(_frame(VIDEO)), _raw(b"10\n")])
    result = await _list(driver, context=_context())
    page = _page(result)
    assert result.outcome.status is AttemptStatus.SUCCEEDED
    assert result.outcome.effect is EffectState.CONFIRMED
    assert waits == [5.0]
    assert page["set_finalized"] is True and page["completion_evidence"] is None
    assert page["entries"] == [{"identity": VIDEO, "locator": {"path": VIDEO}, "complete": True,
                                "size_bytes": 10, "kind": "video", "format_id": "mp4",
                                "original_name": "new.MP4", "media_type": None,
                                "paired_identity": None}]
    assert result.outcome.settlement.evidence.data["file_completion"] == _completion()
    assert result.outcome.settlement.evidence.data["file_completion_source_page_event_id"] is None
    assert all(spec.argv[:5] == ("adb", "-s", "serial-1", "shell", "-T") for spec in transport.specs)
    assert all(Decimal("0") < spec.timeout_s <= Decimal("5") for spec in transport.specs)


@pytest.mark.asyncio
async def test_missing_stop_context_preserves_actual_entries_without_completion(monkeypatch):
    driver, _, waits = _driver(monkeypatch, [_raw(_frame(VIDEO)), _raw(b"10\n")])
    page = _page(await _list(driver))
    assert waits == []
    assert page["set_finalized"] is False
    assert page["entries"][0]["complete"] is False
    assert page["entries"][0]["size_bytes"] == 10


@pytest.mark.asyncio
async def test_more_page_does_not_finalize_even_after_completion_wait(monkeypatch):
    driver, _, _ = _driver(monkeypatch, [_raw(_frame(VIDEO, more=True)), _raw(b"10\n")])
    page = _page(await _list(driver, context=_context()))
    assert page["set_finalized"] is False
    assert page["next_cursor"] == DirectoryCursor(BINDING, (ROOT,), 0, VIDEO).as_json()


@pytest.mark.asyncio
async def test_reliable_empty_last_page_finalizes_only_with_completed_wait(monkeypatch):
    driver, _, _ = _driver(monkeypatch, [_raw(_frame())])
    page = _page(await _list(driver, context=_context()))
    assert page["entries"] == [] and page["set_finalized"] is True


@pytest.mark.asyncio
async def test_auxiliary_files_are_not_registered_as_formal_video(monkeypatch):
    driver, transport, _ = _driver(monkeypatch, [_raw(_frame(ROOT + "/new.LRF"))])
    page = _page(await _list(driver, context=_context()))
    assert page["entries"] == [] and page["set_finalized"] is True
    assert len(transport.specs) == 1


@pytest.mark.asyncio
async def test_prior_completed_wait_is_reused_for_next_page_without_new_wait(monkeypatch):
    driver, _, waits = _driver(monkeypatch, [_raw(_frame(VIDEO)), _raw(b"10\n")])
    context = _context()
    context["prior_completion"] = {"result_page_event_id": 40, "evidence": _completion()}
    result = await _list(driver, context=context)
    assert waits == [] and _page(result)["set_finalized"] is True
    assert result.outcome.settlement.evidence.data["file_completion"] == _completion()
    assert result.outcome.settlement.evidence.data["file_completion_source_page_event_id"] == 40


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("activity_id", "8"), ("stop_result_event_id", 31),
                                         ("required_wait_ms", 6000), ("observed_wait_ns", 1),
                                         ("completed", False), ("version", 2)])
async def test_mismatched_prior_completion_is_rejected_before_device_calls(monkeypatch, field, value):
    driver, transport, waits = _driver(monkeypatch, [])
    context = _context()
    completion = _completion()
    completion[field] = value
    context["prior_completion"] = {"result_page_event_id": 40, "evidence": completion}
    with pytest.raises(ValueError):
        await _list(driver, context=context)
    assert transport.specs == [] and waits == []


@pytest.mark.asyncio
async def test_deadline_too_short_does_not_claim_completed_wait_or_dispatch(monkeypatch):
    driver, transport, waits = _driver(monkeypatch, [])
    result = await _list(driver, context=_context(), timeout_s=Decimal("4"))
    assert result.error is not None and result.outcome.effect is EffectState.UNKNOWN
    assert result.observations == () and transport.specs == [] and waits == []
    assert result.outcome.settlement.evidence.data["file_completion"]["completed"] is False


@pytest.mark.asyncio
async def test_interrupted_wait_retains_failure_without_dispatch_or_finalization(monkeypatch):
    driver, transport, waits = _driver(monkeypatch, [], wait_error=True)
    result = await _list(driver, context=_context())
    assert waits == [5.0] and result.error is not None
    assert result.observations == () and transport.specs == []
    assert result.outcome.settlement.evidence.data["file_completion"]["completed"] is False


@pytest.mark.asyncio
async def test_directory_failure_does_not_become_final_empty_page(monkeypatch):
    driver, _, _ = _driver(monkeypatch, [_raw(b"", exit_code=1, stderr=b"find failed")])
    result = await _list(driver, context=_context())
    assert result.error is not None and result.observations == ()
    assert result.outcome.settlement.evidence.data["file_completion"]["completed"] is True


@pytest.mark.asyncio
async def test_metadata_failure_keeps_partial_files_and_error_without_finalizing(monkeypatch):
    second = ROOT + "/DJI_001/new2.MP4"
    driver, _, _ = _driver(monkeypatch, [_raw(_frame(VIDEO, second)), _raw(b"10\n"),
                                       _raw(b"", exit_code=1, stderr=b"stat failed")])
    result = await _list(driver, context=_context())
    page = _page(result)
    assert result.error is not None and page["set_finalized"] is False
    assert page["entries"][0]["size_bytes"] == 10
    assert page["entries"][1]["complete"] is False and page["entries"][1]["size_bytes"] is None


@pytest.mark.asyncio
async def test_completion_context_for_other_activity_is_rejected_before_wait(monkeypatch):
    driver, transport, waits = _driver(monkeypatch, [])
    context = deepcopy(_context())
    context["activity_id"] = "8"
    with pytest.raises(ValueError):
        await _list(driver, context=context)
    assert not transport.specs and not waits

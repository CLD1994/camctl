"""结果列举的一次真实调用保持原票据、期限和完整结果。"""

from decimal import Decimal
from copy import deepcopy
from unittest.mock import create_autospec

import pytest

from camctl.bootstrap.capture_assembly import DriverResultListing
from camctl.devices.bindings import DeviceBinding
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.ports import DeviceCallResult, ResultDriver
from camctl.operations.models import (
    AttemptStatus, AttemptTicket, CallInfo, CallOutcome, EffectState, ErrorValue,
    EvidenceValue, Settlement, SettlementBasis,
)


_BINDING = DeviceBinding("cam-1", "driver-1")
_TICKET = AttemptTicket(2, "result", "71", "results/71", 19)
_REGISTRY = EvidenceRegistry((
    EvidenceContract("result_files_listed", 1, "result",
        frozenset({"activity_id", "entries"}), identity_field="activity_id"),
    EvidenceContract("adb_foreground_assumption", 1, "result",
        frozenset({"terminate_grace_s"})),
))


def _result(with_files):
    observations = (DeviceObservation("result_files_listed", 1, {
        "activity_id": "71", "entries": [{
            "identity": "video-original", "locator": {"path": "/DCIM/original.mp4"},
            "size_bytes": 41, "complete": True, "kind": "video",
        }],
    }),) if with_files else ()
    return CallOutcome(
        status=AttemptStatus.FAILED,
        error=ErrorValue("transport_timeout", "transport", {"received_bytes": 23}),
        effect=EffectState.CONFIRMED if with_files else EffectState.UNKNOWN,
        settlement=Settlement(SettlementBasis.ASSUMED,
            EvidenceValue("adb_foreground_assumption", 1,
                {"terminate_grace_s": Decimal("0.25")})),
        observations=observations, call_info=CallInfo(local_exit_code=7))


@pytest.mark.asyncio
@pytest.mark.parametrize("with_files", [False, True])
async def test_result_round_preserves_real_error_and_observations(with_files):
    original = _result(with_files)
    driver = create_autospec(ResultDriver, instance=True)
    driver.list_results.return_value = DeviceCallResult.from_outcome(original)
    adapter = DriverResultListing(driver, _BINDING, _REGISTRY)

    listed = await adapter.list_round(_TICKET, timeout_s=Decimal("1.25"))

    assert listed.outcome is original
    assert tuple(entry.identity for entry in listed.entries) == (
        ("video-original",) if with_files else ())
    driver.list_results.assert_awaited_once()
    request, batch = driver.list_results.call_args.args
    assert request.binding == _BINDING
    assert request.ticket is _TICKET
    assert request.params == {"activity_id": "71"}
    assert request.timeout_s == Decimal("1.25")
    assert batch > 0


@pytest.mark.asyncio
async def test_result_round_rejects_missing_original_full_result():
    driver = create_autospec(ResultDriver, instance=True)
    driver.list_results.return_value = DeviceCallResult((), {"code": "transport_timeout"})
    adapter = DriverResultListing(driver, _BINDING, _REGISTRY)

    with pytest.raises(ValueError):
        await adapter.list_round(_TICKET, timeout_s=Decimal("1.25"))


@pytest.mark.asyncio
async def test_result_round_rejects_foreign_operation_before_device_call():
    driver = create_autospec(ResultDriver, instance=True)
    adapter = DriverResultListing(driver, _BINDING, _REGISTRY)
    other = AttemptTicket(2, "query", "71", "query/start/71", 19)

    with pytest.raises(ValueError):
        await adapter.list_round(other, timeout_s=Decimal("1.25"))

    driver.list_results.assert_not_awaited()


@pytest.mark.asyncio
async def test_result_page_uses_original_ticket_cursor_and_single_call_limit():
    from camctl.capture.result_inputs import RESULT_PAGE_CONTRACT
    from camctl.devices.directory import DirectoryCursor
    cursor = DirectoryCursor(_BINDING, ("/DCIM",), 0, "/DCIM/a.mp4")
    actual = CallOutcome(effect=EffectState.CONFIRMED, settlement=Settlement(
        SettlementBasis.ASSUMED, EvidenceValue("adb_foreground_assumption", 1, {"terminate_grace_s": Decimal("0.25")})),
        observations=(DeviceObservation("result_files_listed", 2, {
            "activity_id": "71", "entries": [], "cursor": cursor.as_json(),
            "next_cursor": None, "set_finalized": True, "completion_evidence": None}),))
    registry = EvidenceRegistry((RESULT_PAGE_CONTRACT, _REGISTRY.contract("adb_foreground_assumption", 1)))
    driver = create_autospec(ResultDriver, instance=True)
    driver.list_results.return_value = DeviceCallResult.from_outcome(actual)
    page = await DriverResultListing(driver, _BINDING, registry).list_page(_TICKET, cursor=cursor, timeout_s=Decimal("1.25"))
    assert page.outcome is actual and page.set_finalized and page.scan_complete
    request, batch = driver.list_results.call_args.args
    assert request.ticket is _TICKET and request.timeout_s == Decimal("1.25")
    assert request.params == {"activity_id": "71", "cursor": cursor.as_json()}
    driver.list_results.assert_awaited_once()


@pytest.mark.asyncio
async def test_result_page_passes_frozen_completion_context_to_original_driver_request():
    from camctl.capture.result_inputs import RESULT_PAGE_CONTRACT
    context = {"activity_id": "71", "stop_result_event_id": 7,
        "stop_returned_at_us": 1_750_000_000_000_000,
        "stop_response": {"status": 2, "effect_state": 2, "error": None,
            "result": {"format_version": 1, "settlement": {"basis": 1,
                "evidence": {"type": "stop_returned", "version": 1, "data": {}}},
                "observations": [{"type": "stop_confirmed", "version": 1,
                    "data": {"activity_id": "71"}}]}},
        "file_completion_wait_ms": 5000, "prior_completion": None}
    frozen = deepcopy(context)
    original = CallOutcome(effect=EffectState.CONFIRMED, settlement=Settlement(
        SettlementBasis.ASSUMED, EvidenceValue("adb_foreground_assumption", 1,
            {"terminate_grace_s": Decimal("0.25")})),
        observations=(DeviceObservation("result_files_listed", 2, {
            "activity_id": "71", "entries": [], "cursor": None,
            "next_cursor": None, "set_finalized": True, "completion_evidence": None}),))
    registry = EvidenceRegistry((RESULT_PAGE_CONTRACT,
        _REGISTRY.contract("adb_foreground_assumption", 1)))
    driver = create_autospec(ResultDriver, instance=True)

    async def read(request, batch):
        context["stop_response"]["result"]["observations"].clear()
        return DeviceCallResult.from_outcome(original)

    driver.list_results.side_effect = read
    page = await DriverResultListing(driver, _BINDING, registry).list_page(
        _TICKET, cursor=None, timeout_s=Decimal("10"), completion_context=context)

    assert page.outcome is original
    request, _ = driver.list_results.call_args.args
    assert request.params == {"activity_id": "71", "completion_context": frozen}

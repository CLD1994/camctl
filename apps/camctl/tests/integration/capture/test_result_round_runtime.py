"""RESULTS 真实意图、活动身份和完整驱动结果保持同一来源。"""

from decimal import Decimal
import json
from unittest.mock import create_autospec

import pytest

from camctl.bootstrap.capture_assembly import DriverResultListing
from camctl.capture.handlers import ListingPhase, _listing_round
from camctl.devices.bindings import DeviceBinding
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.ports import DeviceCallResult, ResultDriver
from camctl.operations.attempts import AttemptConfig
from camctl.operations.models import (
    AttemptStatus, CallInfo, CallOutcome, EffectState, ErrorValue, EvidenceValue,
    Settlement, SettlementBasis,
)

from .result_consumer_fixtures import consumer_world


_CONTRACTS = EvidenceRegistry((
    EvidenceContract("results_returned", 1, "result", frozenset()),
    EvidenceContract("result_files_listed", 1, "result",
        frozenset({"activity_id", "entries"}), identity_field="activity_id"),
    EvidenceContract("adb_foreground_assumption", 1, "result",
        frozenset({"terminate_grace_s"})),
))


@pytest.mark.asyncio
@pytest.mark.parametrize("with_files", [False, True])
async def test_result_consumer_uses_original_activity_ticket_timeout_and_outcome(tmp_path, with_files):
    owned, runtime, action_id, _handler = await consumer_world(tmp_path, "record", independent_activity=True)
    try:
        # 先行报告动作使拍摄动作 2 与真实调度分配的活动 1 分别独立。
        assert action_id == 2
        assert owned.connection.execute(
            "SELECT id FROM device_activities WHERE action_id=?", (action_id,)).fetchone() == (1,)
        actual = CallOutcome(
            status=AttemptStatus.FAILED,
            error=ErrorValue("transport_timeout", "transport", {"received_bytes": 23}),
            effect=EffectState.CONFIRMED if with_files else EffectState.UNKNOWN,
            settlement=Settlement(SettlementBasis.ASSUMED,
                EvidenceValue("adb_foreground_assumption", 1,
                    {"terminate_grace_s": Decimal("0.25")})),
            observations=(DeviceObservation("result_files_listed", 1, {
                "activity_id": "1", "entries": [{
                    "identity": "original", "locator": {"path": "/DCIM/original.mp4"},
                    "size_bytes": 41, "complete": True, "kind": "video",
                }],
            }),) if with_files else (),
            call_info=CallInfo(local_exit_code=7))
        driver = create_autospec(ResultDriver, instance=True)
        driver.list_results.return_value = DeviceCallResult.from_outcome(actual)
        runtime.evidence = _CONTRACTS
        runtime.results = DriverResultListing(driver, DeviceBinding("cam-1", "camctl-adb"), _CONTRACTS)
        runtime.check_config = AttemptConfig(3, Decimal("1.25"), Decimal("2"))

        listing = await _listing_round(runtime, action_id)

        driver.list_results.assert_awaited_once()
        request, _batch = driver.list_results.call_args.args
        assert request.ticket is not None
        assert request.ticket.target_id == "1"
        assert request.ticket.responsibility_key == "results/1"
        assert request.params == {"activity_id": "1"}
        assert request.timeout_s == Decimal("1.25")
        assert owned.connection.execute(
            "SELECT action_id,activity_id,attempts_used FROM operation_runs"
            " WHERE responsibility_key='results/1'").fetchone() == (2, 1, 1)
        if with_files:
            assert listing.phase is ListingPhase.LISTED
            assert listing.outcome is actual
            assert tuple(entry.identity for entry in listing.entries) == ("original",)
        else:
            assert listing.phase is ListingPhase.RETRY_WAIT
            status, result_json, error_json = owned.connection.execute(
                "SELECT a.status,a.result_json,a.error_json FROM operation_attempts a"
                " JOIN operation_runs r ON r.id=a.run_id WHERE r.responsibility_key='results/1'"
            ).fetchone()
            assert status == 3
            saved = json.loads(result_json)
            assert saved["settlement"] == {"basis": "assumed", "evidence": {
                "type": "adb_foreground_assumption", "version": 1,
                "data": {"terminate_grace_s": 0.25}}}
            assert saved["call_info"] == {"local_exit": {"exit_code": 7}}
            assert json.loads(error_json) == {"code": "transport_timeout", "stage": "transport",
                                            "details": {"received_bytes": 23}}
    finally:
        owned.connection.close()

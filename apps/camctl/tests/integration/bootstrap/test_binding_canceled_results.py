"""取消延时任务的必要结果核实独立于已成功的 STOP。"""

from types import SimpleNamespace

import pytest

from camctl.cancellation.settlement import TargetSettlement
from camctl.capture.handlers import capture_handler
from camctl.capture.result_inputs import ListedResult
from camctl.devices.drivers.registry import DriverEntry, DriverRegistry, DriverStatus
from camctl.devices.ports import DriverDeclaration
from camctl.operations.models import AttemptStatus, CallOutcome, EffectState, ErrorValue, EvidenceValue, Settlement, SettlementBasis
from camctl.persistence.repositories.cancellation import CancellationRepository

from ..capture.test_capture_contract import ResultsDouble
from ..support.bootstrap_capture_cases import bootstrap_case, SavedBoundary
from ..support.capture import capture_assembly

pytestmark = pytest.mark.asyncio


class FailedResults(ResultsDouble):
    async def list_round(self, ticket, *, timeout_s):
        self.calls.append(int(ticket.target_id))
        return ListedResult((), CallOutcome(status=AttemptStatus.UNKNOWN, effect=EffectState.UNKNOWN,
            error=ErrorValue("transport_timeout", "transport"), observations=(),
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("results_returned", 1, {}))))


@pytest.mark.parametrize("change", ["missing", "mismatch"])
@pytest.mark.parametrize("result_started", [False, True])
async def test_canceled_timelapse_binding_failure_keeps_result_settlement_failure(
        tmp_path, bootstrap_case, change, result_started):
    case = bootstrap_case
    runtime = await case.world(tmp_path, "timelapse", results=FailedResults({}))
    await capture_handler(case.handler)(case.action_id, runtime)
    await case.cancel()
    case.boundary = "finish_capture_stop"
    with pytest.raises(SavedBoundary):
        await capture_handler(case.handler)(case.action_id, await case.view())
    if result_started:
        case.boundary = "finish_attempt"
        with pytest.raises(SavedBoundary):
            await capture_handler(case.handler)(case.action_id, await case.view())
    case.boundary = None
    connection = case.owned.connection
    stop_before = connection.execute("SELECT * FROM operation_runs WHERE responsibility_key=?",
        (f"stop/{case.action_id}",)).fetchone()
    attempts_before = tuple(connection.execute("SELECT * FROM operation_attempts"))
    activity_before = connection.execute("SELECT * FROM device_activities WHERE action_id=?",
        (case.action_id,)).fetchone()
    calls_before = list(case.results.calls)
    options = {**case.options, "devices": {} if change == "missing" else {"cam-1": {"driver": "other"}}}
    ports = SimpleNamespace(control=case.driver.control, stop=case.stopper.stop)
    declaration = DriverDeclaration(control_supported=True, stop_supported=True, query_supported=False,
        result_supported=False, read_supported=False, digest_supported=False, delete_supported=False)
    options["drivers"] = DriverRegistry(tuple(DriverEntry(name, ports, declaration, case.evidence,
        DriverStatus.SOFTWARE_CONTRACT_VERIFIED) for name in ("camctl-adb", "other")))
    runtime = await case.view(capture_assembly(**options))
    await capture_handler(case.handler)(case.action_id, runtime)
    assert case.driver.control.await_count == len(case.stopper.calls) == 1
    assert case.results.calls == calls_before
    assert connection.execute("SELECT status,cancel_requested,driver_id FROM actions WHERE id=?",
        (case.action_id,)).fetchone() == (6, 1, "camctl-adb")
    assert connection.execute("SELECT * FROM operation_runs WHERE responsibility_key=?",
        (f"stop/{case.action_id}",)).fetchone() == stop_before
    assert tuple(connection.execute("SELECT * FROM operation_attempts")) == attempts_before
    assert connection.execute("SELECT * FROM device_activities WHERE action_id=?",
        (case.action_id,)).fetchone() == activity_before
    assert connection.execute("SELECT COUNT(*) FROM outputs WHERE source_action_id=?",
        (case.action_id,)).fetchone() == (0,)
    settled = await TargetSettlement(case.database, None, CancellationRepository(),
        lambda _: "unknown", case.clock.wall_us).settle(case.action_id)
    assert settled.complete and settled.failed

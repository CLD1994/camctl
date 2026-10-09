"""普通停止沿用已提交尝试，并将本次期限传给真实设备端口。"""

from decimal import Decimal

import pytest

from camctl.capture.handlers import _stop_call
from camctl.devices.evidence import DeviceObservation
from camctl.devices.ports import DeviceCallResult
from camctl.operations.attempts import AttemptConfig

from .test_capture_contract import _environment, _RECORD
from .test_retry_intervals import _runtime


@pytest.mark.asyncio
async def test_stop_request_carries_original_ticket_and_current_timeout(tmp_path):
    owned = _environment(tmp_path, _RECORD)
    requests = []

    class Stopper:
        async def stop(self, request):
            requests.append(request)
            return DeviceCallResult((DeviceObservation("stop_confirmed", 1, {"activity_id": "12"}),), None)

    try:
        runtime = _runtime(owned, stopper=Stopper())
        runtime.stop_config = AttemptConfig(7, Decimal("0.125"), Decimal("0.5"))

        await _stop_call(runtime, runtime.action(12))

        assert len(requests) == 1
        request = requests[0]
        assert request.ticket is not None
        row = owned.connection.execute(
            "SELECT t.attempt_no, t.run_id, r.responsibility_key, r.timeout_s_json"
            " FROM operation_attempts t JOIN operation_runs r ON r.id = t.run_id"
            " WHERE r.responsibility_key = 'stop/12'").fetchone()
        assert request.ticket.attempt_id == row[0]
        assert request.ticket.run_id == row[1]
        assert request.ticket.responsibility_key == row[2]
        assert request.ticket.operation == "stop"
        assert request.ticket.target_id == "12"
        assert request.timeout_s == Decimal("0.125")
        assert row[3] == "0.125"
    finally:
        owned.connection.close()

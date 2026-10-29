"""普通停止沿用已提交尝试，并将本次期限传给真实设备端口。"""

from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.capture.handlers import capture_handler
from camctl.devices.evidence import DeviceObservation
from camctl.devices.ports import DeviceCallResult
from camctl.operations.attempts import AttemptConfig

from ..support.result_cases import result_case
from .test_capture_contract import _NOW
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository


@pytest.mark.asyncio
async def test_stop_request_carries_original_ticket_and_current_timeout(tmp_path, result_case, monkeypatch):
    owned, runtime, action_id, handler = await result_case.world(tmp_path, "record",
        independent_activity=True, prepare_start_only=True,
        assembly_options={"stop_config": AttemptConfig(7, Decimal("0.125"), Decimal("0.5"))})
    activity_id, = owned.connection.execute("SELECT id FROM device_activities WHERE action_id=?", (action_id,)).fetchone()
    requests = []

    class Stopper:
        async def stop(self, request):
            requests.append(request)
            return DeviceCallResult((DeviceObservation("stop_confirmed", 1, {"activity_id": str(activity_id)}),), None)

    try:
        result_case.stopper.stop.side_effect = Stopper().stop
        result_case.clock.wall = _NOW + 60_000_000
        result_case.clock.monotonic = 65_000_000_000
        original = CaptureRepository.finish_capture_stop
        class StopSaved(Exception):
            pass
        def stop_after_saved(repository, *args, **kwargs):
            receipt = original(repository, *args, **kwargs)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            return replace(receipt, handoff_error=StopSaved())
        monkeypatch.setattr(CaptureRepository, "finish_capture_stop", stop_after_saved)
        with pytest.raises(StopSaved):
            await capture_handler(handler)(action_id, runtime)

        assert len(requests) == 1
        request = requests[0]
        assert request.ticket is not None
        row = owned.connection.execute(
            "SELECT t.attempt_no, t.run_id, r.responsibility_key, r.timeout_s_json"
            " FROM operation_attempts t JOIN operation_runs r ON r.id = t.run_id"
            " WHERE r.responsibility_key = ?", (f"stop/{action_id}",)).fetchone()
        assert request.ticket.attempt_id == row[0]
        assert request.ticket.run_id == row[1]
        assert request.ticket.responsibility_key == row[2]
        assert request.ticket.operation == "stop"
        assert request.ticket.target_id == str(activity_id)
        assert request.timeout_s == Decimal("0.125")
        assert row[3] == "0.125"
        # 正式 STOP 事务保存采集已结束；文件核实与动作收场继续持有占用。
        assert owned.connection.execute("SELECT activity_state,occupancy_state FROM device_activities WHERE action_id=?", (action_id,)).fetchone() == (3, 1)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (2,)
        assert owned.connection.execute("SELECT status,effect_state FROM operation_attempts WHERE run_id=?",
            (request.ticket.run_id,)).fetchone() == (2, 3)
    finally:
        owned.connection.close()

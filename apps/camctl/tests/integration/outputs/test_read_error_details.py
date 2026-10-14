"""原 READ 结束事务保留源读取异常中的调用与收场事实。"""

import json
from types import SimpleNamespace

import pytest

from camctl.capture.input_copy import InputPhase, InputStep
from camctl.devices.drivers.hasselblad.driver import CameraReadError, EVIDENCE
from camctl.operations.attempts import RetryWaitGate
from camctl.outputs.obtain_flow import _finish_read_attempt
from camctl.outputs.read_attempts import running_read_ticket
from camctl.persistence.repositories.operations import OperationRepository

from .test_grant_reuse import granted, read_request, read_targets, local_read, _later_attempt


@pytest.mark.parametrize("read_request", ["device"], indirect=True)
def test_original_read_result_keeps_download_exit_and_cleanup_error(granted):
    owned, command, _, prepared = granted
    _later_attempt(owned, command, prepared)
    ticket, _ = running_read_ticket(owned, prepared.copy_id)
    error = CameraReadError("download_failed; cleanup: permission denied", [
        {"name": "download", "local_exit_code": 17, "stderr_hex": b"USB interrupted".hex()}])
    runtime = SimpleNamespace(owned=owned, pending_read_results={}, pending_read_ends={},
        operations=OperationRepository(), occurred_at=lambda: command.occurred_at + 20,
        retry_gate=RetryWaitGate(), monotonic_ns=lambda: 0)
    _finish_read_attempt(runtime, SimpleNamespace(evidence=EVIDENCE, max_read_attempts=3),
        {"id": prepared.copy_id, "delivery_status": 1}, ticket,
        InputStep(InputPhase.SEGMENT_FAILED, copy_id=prepared.copy_id, error=error, source_failed=True))
    stored = json.loads(owned.connection.execute(
        "SELECT error_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
        (ticket.run_id, ticket.attempt_id)).fetchone()[0])
    assert stored["code"] == "device_error"
    assert "permission denied" in stored["details"]["reason"]
    assert "17" in stored["details"]["reason"]
    assert b"USB interrupted".hex() in stored["details"]["reason"]

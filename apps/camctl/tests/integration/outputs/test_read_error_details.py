"""公开取回保存 READ 失败时保留下载退出和原收场异常。"""

import json

import pytest

from camctl.devices.drivers.hasselblad.driver import CameraReadError, EVIDENCE
from camctl.outputs.obtain_flow import advance_obtain

from ..support.outputs import read_test_runtime
from .test_grant_reuse import granted, read_request, read_targets, local_read


@pytest.mark.asyncio
@pytest.mark.parametrize("read_request", ["device"], indirect=True)
async def test_original_read_result_keeps_download_exit_and_cleanup_error(granted, tmp_path):
    owned, command, _, prepared = granted
    owned.connection.execute("UPDATE actions SET scheduled_at=? WHERE id=32", (command.occurred_at + 60_000_000,))
    owned.connection.commit()
    error = CameraReadError("download_failed; cleanup: permission denied", [
        {"name": "download", "local_exit_code": 17, "stderr_hex": b"USB interrupted".hex()}])

    class ReadDriver:
        tickets = []

        async def open_read(self, source, offset, ticket, *, idle_timeout_s):
            self.tickets.append(ticket)
            raise error

    driver = ReadDriver()
    async with read_test_runtime(owned, driver, EVIDENCE, staging=tmp_path / "staging",
            occurred_at=lambda: command.occurred_at + 20) as runtime:
        await advance_obtain(runtime)
    ticket, = driver.tickets
    assert ticket.target_id == str(prepared.copy_id)
    stored = json.loads(owned.connection.execute(
        "SELECT error_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
        (ticket.run_id, ticket.attempt_id)).fetchone()[0])
    assert stored["code"] == "device_error"
    assert "permission denied" in stored["details"]["reason"]
    assert "17" in stored["details"]["reason"]
    assert b"USB interrupted".hex() in stored["details"]["reason"]

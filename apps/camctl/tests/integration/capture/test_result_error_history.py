"""公开拍摄历史的有限 RESULTS 耗尽保存完整活动错误。"""

from decimal import Decimal
import json

import pytest

from camctl.capture.handlers import capture_handler
from camctl.contracts.workflow_errors import registered_error
from camctl.devices.evidence import DeviceObservation
from camctl.operations.attempts import AttemptConfig
from camctl.operations.models import AttemptStatus, CallOutcome, EffectState, EvidenceValue, Settlement, SettlementBasis
from camctl.persistence.repositories.capture import register_capture_guards
from camctl.persistence.repositories.operations import register_operation_guards
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.persistence.repositories.timelapse import register_timelapse_guards

from .result_consumer_fixtures import consumer_world
from .test_result_consumer_saves import _result_port

pytestmark = pytest.mark.asyncio
register_capture_guards()
register_operation_guards()
register_outputs_guards()
register_timelapse_guards()


@pytest.mark.parametrize("consumer", ["photo", "timelapse"])
@pytest.mark.parametrize("independent_activity", [False, True], ids=["same-id", "independent-activity"])
async def test_finite_results_exhaustion_saves_complete_activity_error(
    tmp_path, consumer, independent_activity,
):
    owned, runtime, action_id, handler = await consumer_world(
        tmp_path, consumer, independent_activity=independent_activity)
    try:
        activity_id, = owned.connection.execute(
            "SELECT id FROM device_activities WHERE action_id=?", (action_id,)).fetchone()
        assert (action_id != activity_id) is independent_activity
        # 可靠 OTHER 文件不能证明 PHOTO/VIDEO 要求或集合结束；不填 v1 未声明的成员。
        entry = {"identity": "auxiliary", "kind": "other", "complete": True,
                 "locator": {"path": "/DCIM/auxiliary"}, "size_bytes": 41,
                 "original_name": "auxiliary.bin", "media_type": "application/octet-stream"}
        actual = CallOutcome(
            status=AttemptStatus.SUCCEEDED, effect=EffectState.CONFIRMED,
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("results_returned", 1, {})),
            observations=(DeviceObservation("result_files_listed", 1,
                {"activity_id": str(activity_id), "entries": [entry]}),))
        driver = _result_port(runtime, actual)
        runtime.check_config = AttemptConfig(2, Decimal("1.25"), Decimal(0))
        advance = capture_handler(handler)
        for _ in range(2):
            await advance(action_id, runtime)
            assert runtime.action(action_id)["status"] == 2
        run_id, status, used, retry = owned.connection.execute(
            "SELECT id,status,attempts_used,retry_wait_required FROM operation_runs"
            " WHERE responsibility_key=?", (f"results/{activity_id}",)).fetchone()
        assert (status, used, retry) == (2, 2, 1)
        original_attempts = owned.connection.execute(
            "SELECT * FROM operation_attempts ORDER BY id").fetchall()
        result_rows = owned.connection.execute(
            "SELECT status,result_json,error_json FROM operation_attempts WHERE run_id=? ORDER BY attempt_no",
            (run_id,)).fetchall()
        expected_input = {
            "format_version": 1,
            "settlement": {"basis": "observed", "evidence": {"type": "results_returned", "version": 1, "data": {}}},
            "observations": [{"type": "result_files_listed", "version": 1,
                              "data": {"activity_id": str(activity_id), "entries": [entry]}}],
        }
        assert len(result_rows) == 2
        assert all(status == 2 and error is None and json.loads(result) == expected_input
                   for status, result, error in result_rows)
        original_files = owned.connection.execute("SELECT * FROM device_files ORDER BY id").fetchall()
        assert len(original_files) == 1
        original_history = owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()
        assert driver.list_results.await_count == 2

        await advance(action_id, runtime)

        assert owned.connection.execute(
            "SELECT status,attempts_used,retry_wait_required FROM operation_runs WHERE id=?",
            (run_id,)).fetchone() == (6, 2, 0)
        assert runtime.action(action_id)["status"] == 4
        expected_error = {"code": "capture_result_unconfirmed",
            "stage": registered_error("capture_result_unconfirmed")["stage"],
            "details": {"activity_id": str(activity_id), "reason": "outputs_unknown"}}
        action_error, = owned.connection.execute(
            "SELECT error_details_json FROM actions WHERE id=?", (action_id,)).fetchone()
        assert json.loads(action_error) == expected_error["details"]
        capture, last_error = owned.connection.execute(
            "SELECT capture_json,last_error_json FROM device_activities WHERE id=?", (activity_id,)).fetchone()
        assert json.loads(last_error) == expected_error, "有限核实错误必须保存完整公共身份和真实活动"
        assert json.loads(capture) == {"status": "unconfirmed", "error": expected_error}
        assert owned.connection.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall() == original_attempts
        assert owned.connection.execute("SELECT * FROM device_files ORDER BY id").fetchall() == original_files
        assert owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()[:len(original_history)] == original_history
        assert driver.list_results.await_count == 2
        runtime.driver.control.assert_awaited_once()
        await advance(action_id, runtime)
        assert driver.list_results.await_count == 2
        assert owned.connection.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall() == original_attempts
    finally:
        owned.connection.close()

"""正式后续动作在原残留责任上执行查询、停止和绑定失败收场。"""

from decimal import Decimal
import json

import pytest

from camctl.contracts.workflow_errors import registered_error
from camctl.persistence.repositories.history import HistoryRepository

from ..support.bootstrap_residual_recovery import ResidualDriver, residual_case

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("operation,expected_timeout", [("query", "17"), ("stop", "13")])
async def test_residual_first_call_carries_attempt_ticket_and_timeout(
        tmp_path, monkeypatch, capture_resources, operation, expected_timeout):
    async with residual_case(tmp_path, capture_resources, monkeypatch) as case:
        request = [request for call, request in case.driver.calls if call == operation][-1]
        assert request.ticket is not None
        assert request.ticket.responsibility_key == (
            f"query/preflight/{case.trigger}" if operation == "query"
            else f"followup/{case.trigger}/{case.activity}")
        assert request.timeout_s == Decimal(expected_timeout)
        assert request.ticket.run_id == case.run(request.ticket.responsibility_key)[0]


async def test_residual_confirmation_carries_attempt_ticket_and_timeout(
        tmp_path, monkeypatch, capture_resources):
    async with residual_case(tmp_path, capture_resources, monkeypatch) as case:
        await case.advance()
        request = [request for call, request in case.driver.calls if call == "query"][-1]
        assert request.ticket is not None
        assert request.ticket.responsibility_key == f"query/residual/{case.trigger}/{case.activity}"
        assert request.timeout_s == Decimal("17")


@pytest.mark.parametrize("stop_error", [False, True])
async def test_new_driver_trigger_preserves_old_driver_residual(
        tmp_path, monkeypatch, capture_resources, stop_error):
    async with residual_case(tmp_path, capture_resources, monkeypatch,
                             stop_error=stop_error, cancel_trigger=True) as case:
        trigger = await case.accept(4, case.photo("新驱动触发"), driver_id="alternate-camera")
        before_activity = case.owned.connection.execute(
            "SELECT * FROM device_activities WHERE id=?", (case.activity,)).fetchone()
        before_attempts = case.owned.connection.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall()
        before_old = case.action(case.old_action)
        history = HistoryRepository(case.path)
        frozen_boundary = history.current_boundary()
        frozen_old = history.restore_entity("action", case.old_action, frozen_boundary)
        current_driver = ResidualDriver(case.activity, stop_error=stop_error)
        await case.reconnect()
        await case.capture(case.new_factory(identity="alternate-camera", driver=current_driver))
        assert current_driver.calls == []
        assert case.owned.connection.execute("SELECT * FROM device_activities WHERE id=?",
            (case.activity,)).fetchone() == before_activity
        assert case.owned.connection.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall() == before_attempts
        after_old = case.action(case.old_action)
        assert {key: value for key, value in after_old.items() if key not in ("last_event_id", "change_count")} == {
            key: value for key, value in before_old.items() if key not in ("last_event_id", "change_count")}
        original_links = case.owned.connection.execute(
            "SELECT l.event_id,l.change_count,e.event_type,e.body_json"
            " FROM entity_event_links l JOIN history_events e ON e.id=l.event_id"
            " WHERE l.entity_type=1 AND l.entity_id=? AND l.event_id>? ORDER BY l.event_id",
            (case.old_action, frozen_boundary.last_event_id)).fetchall()
        assert len(original_links) == 1
        event_id, change_count, event_type, body = original_links[0]
        assert (event_type, change_count, after_old["last_event_id"], after_old["change_count"]) == (
            10, before_old["change_count"] + 1, event_id, change_count)
        event = json.loads(body)
        assert event["reason"] == 3
        assert [(row["table"], row["id"]) for row in event["rows"]] == [("operation_runs", case.flow[0])]
        assert case.owned.connection.execute("SELECT entity_type,entity_id FROM entity_event_links"
            " WHERE event_id=?", (event_id,)).fetchall() == [(1, case.old_action)]
        assert history.restore_entity("action", case.old_action, frozen_boundary) == frozen_old
        failed_flow = case.owned.connection.execute("SELECT status,attempts_used,error_json FROM operation_runs"
            " WHERE id=?", (case.flow[0],)).fetchone()
        assert failed_flow[:2] == (4, 1)
        assert json.loads(failed_flow[2]) == {"code": "device_binding_unavailable", "stage": "execution",
            "details": {"device_id": "cam-1", "expected_driver_id": "camctl-adb",
                "actual_driver_id": "alternate-camera", "reason": "mismatch"}}
        result = case.action(trigger)
        assert (result["status"], result["execution_started"], result["driver_id"], result["error_code"]) == (
            4, 1, "alternate-camera", registered_error("device_activity_unresolved")["action_error_id"])
        assert result["error_details_json"] == {"activity_id": str(case.activity), "device_id": "cam-1"}
        assert case.action(case.trigger)["status"] == 6

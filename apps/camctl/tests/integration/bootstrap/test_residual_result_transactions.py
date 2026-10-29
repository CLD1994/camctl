"""残留确认查询由公开流程保存结果、停止收场及活动结束的共同事务。"""

from decimal import Decimal
import json

import pytest

from camctl.devices.bindings import DeviceBinding
from camctl.persistence.transaction import row_facts

from ..support.bootstrap_residual_recovery import residual_case

pytestmark = pytest.mark.asyncio


def _calls(case, operation, prefix=None):
    return [request for call, request in case.driver.calls if call == operation and
        (prefix is None or request.ticket.responsibility_key.startswith(prefix))]


async def test_residual_confirm_query_settles_in_one_transaction(tmp_path, monkeypatch, capture_resources):
    async with residual_case(tmp_path, capture_resources, monkeypatch,
            query_attempts=2, query_timeout=Decimal("0.5"), query_interval=Decimal("1")) as case:
        case.driver.query_mode = "idle"
        await case.advance()
        assert len(_calls(case, "stop")) == 1
        queries = _calls(case, "query", "query/residual/")
        assert len(queries) == 1
        request = queries[0]
        assert request.operation == "query" and request.binding == DeviceBinding("cam-1", "camctl-adb")
        key = f"query/residual/{case.trigger}/{case.activity}"
        assert request.ticket.responsibility_key == key
        confirmation = case.run(key)
        assert confirmation[1:] == (3, 1)
        assert case.owned.connection.execute("SELECT action_id FROM operation_runs WHERE id=?",
            (confirmation[0],)).fetchone() == (case.trigger,)
        assert case.owned.connection.execute("SELECT action_id FROM device_activities WHERE id=?",
            (case.activity,)).fetchone() == (case.old_action,)
        winddown = case.run(f"followup/{case.trigger}/{case.activity}")
        assert winddown[1] == 3
        activity = row_facts(case.owned.connection, "device_activities", case.activity)
        assert (activity["activity_state"], activity["occupancy_state"]) == (3, 2)
        transactions = set()
        for transaction_id, body in case.owned.connection.execute(
                "SELECT transaction_id,body_json FROM history_events ORDER BY id"):
            for row in json.loads(body)["rows"]:
                values = row["after"]["values"]
                if row["table"] == "operation_runs" and row["id"] == confirmation[0] and values.get("status") == 3:
                    transactions.add(("query", transaction_id))
                if row["table"] == "operation_runs" and row["id"] == winddown[0] and values.get("status") == 3:
                    transactions.add(("residual_stop", transaction_id))
                if row["table"] == "device_activities" and row["id"] == case.activity:
                    if values.get("activity_state") == 3:
                        transactions.add(("ended", transaction_id))
                    if values.get("occupancy_state") == 2:
                        transactions.add(("released", transaction_id))
        assert {name for name, _ in transactions} == {"query", "residual_stop", "ended", "released"}
        assert len({txn for _, txn in transactions}) == 1


async def test_residual_confirm_query_still_recording_continues_stop(tmp_path, monkeypatch, capture_resources):
    async with residual_case(tmp_path, capture_resources, monkeypatch,
            query_attempts=2, query_timeout=Decimal("0.5"), query_interval=Decimal("1")) as case:
        case.elapse(2)
        await case.advance()
        key = f"query/residual/{case.trigger}/{case.activity}"
        assert case.run(key)[1:] == (2, 1)
        case.elapse(2)
        await case.advance()
        assert len(_calls(case, "query", "query/residual/")) == 2
        assert case.run(key)[1:] == (2, 2)
        case.elapse(2)
        await case.advance()
        assert len(_calls(case, "query", "query/residual/")) == 2
        assert case.run(key)[1:] == (6, 2)
        assert len(_calls(case, "stop")) == 2
        assert case.run(f"followup/{case.trigger}/{case.activity}")[1:] == (2, 2)
        activity = row_facts(case.owned.connection, "device_activities", case.activity)
        assert (activity["activity_state"], activity["occupancy_state"]) == (2, 1)


async def test_residual_confirm_query_failure_continues_stop_budget(tmp_path, monkeypatch, capture_resources):
    async with residual_case(tmp_path, capture_resources, monkeypatch,
            query_attempts=2, query_timeout=Decimal("0.5"), query_interval=Decimal("1")) as case:
        case.driver.query_mode = "error"
        case.elapse(2)
        await case.advance()
        await case.advance()
        assert len(_calls(case, "query", "query/residual/")) == 1
        assert len(_calls(case, "stop")) == 2
        assert case.run(f"query/residual/{case.trigger}/{case.activity}")[1:] == (2, 1)
        assert case.run(f"followup/{case.trigger}/{case.activity}")[1:] == (2, 2)

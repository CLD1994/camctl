"""安装 wheel、真实 C host 和客户端的正式源精确清理联合验收。"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from _cleanup_roundtrip_support import (
    RAW, PREVIEW, World, _hb_tool, cleanup_package, json_write, process_identity, records, rows, sha, wait,
)
from _cleanup_source_snapshot import cleanup_osmo_source
from camctl.contracts.enums import enum_for
from camctl.contracts.workflow_errors import action_error_id, item_error_id


def test_cleanup_hasselblad_tool_rejects_unrecognized_setting(tmp_path):
    tool = tmp_path / "hb_cli"
    tool.write_text("#!" + sys.executable + "\n" + _hb_tool("contract"), encoding="utf-8")
    tool.chmod(0o700)

    refused = subprocess.run(
        [str(tool), "--pid", "0x000a", "set", "unrecognized-setting", "1"],
        capture_output=True, text=True,
    )

    assert refused.returncode != 0, "清理替身意外接受了未声明的相机参数"


def test_exact_outputs_and_non_cascade(tmp_path, cleanup_osmo_source, monkeypatch):
    with cleanup_osmo_source.consumer(tmp_path, monkeypatch) as world:
        world.prepare_hasselblad()
        world.publish_copies()
        for source in ("repaired", "osv", "lrf", "hb-original", "hb-preview"):
            world.clean_exact(source)
        world.assert_report_ack_and_history()


def test_cleanup_device_delete_unsupported_with_host_files(
        tmp_path, host_demo, cleanup_package, monkeypatch):
    """不支持设备删除不能阻断同一请求中主机两份正式源的实际清理。"""
    with World(cleanup_package, tmp_path, host_demo, monkeypatch, osmo=False) as world:
        world.prepare_hasselblad()
        world.assert_report_ack_and_history()
        original = world.sources["hb-original"]
        preview = world.sources["hb-preview"]
        device = world.sources["hb-device-original"]
        device_preview = world.sources["hb-device-preview"]
        ids = [device["id"], original["id"], preview["id"]]
        device_before = rows(world.db, "SELECT * FROM device_files ORDER BY id")
        tool_calls = {mode: records(world.tools / f"hb-{mode}.jsonl") for mode in ("host", "device")}
        for calls in tool_calls.values():
            capture_calls = [call for call in calls if call["argv"][3] == "capture"]
            assert len(capture_calls) == 1
        for key, content in (("hb-original", RAW), ("hb-preview", PREVIEW)):
            assert world.path(key).read_bytes() == content
            assert rows(world.db, "SELECT size_bytes,sha256 FROM intermediate_files WHERE id=?",
                        (world.sources[key]["intermediate_file_id"],)) == [{"size_bytes": len(content), "sha256": sha(content)}]
        plan, receipt = world.export("mixed-hb-cleanup", [world.action(
            "mixed-hb-cleanup", "delete_action_outputs", {"output_ids": list(map(str, ids))})])
        world.host.submit(plan)
        action, = world.wait_action(receipt["request_id"], "FAILED")
        items = rows(world.db, "SELECT * FROM cleanup_items WHERE action_id=? ORDER BY id", (action["id"],))
        assert [item["output_id"] for item in items] == ids
        statuses = enum_for("cleanup_items.status")
        assert [item["status"] for item in items] == [int(statuses.FAILED), int(statuses.SUCCEEDED), int(statuses.SUCCEEDED)]
        assert items[0]["error_code"] == item_error_id("cleanup_items", "file_delete_failed")
        assert items[0]["restriction_state"] == int(enum_for("cleanup_items.restriction_state").ACTIVE)
        assert rows(world.db, "SELECT * FROM operation_runs WHERE cleanup_item_id=?", (items[0]["id"],)) == []
        for item in items[1:]:
            run, = rows(world.db, "SELECT * FROM operation_runs WHERE cleanup_item_id=?", (item["id"],))
            assert run["responsibility_key"] == f"delete/{item['id']}"
            assert run["attempts_used"] == 1
            assert run["timeout_s_json"] is None and run["retry_interval_s_json"] is None
            attempt, = rows(world.db, "SELECT * FROM operation_attempts WHERE run_id=?", (run["id"],))
            assert attempt["attempt_no"] == 1 and attempt["error_json"] is None
            result = json.loads(attempt["result_json"])
            assert result["settlement"]["basis"] == "observed"
            assert result["observations"] == [{"type": "file_absent", "version": 1,
                "data": {"cleanup_item_id": str(item["id"])}}]
        assert rows(world.db, "SELECT * FROM device_files ORDER BY id") == device_before
        assert not world.path("hb-original").exists()
        assert not world.path("hb-preview").exists()
        for mode, calls in tool_calls.items():
            assert records(world.tools / f"hb-{mode}.jsonl") == calls
        assert world.calls() == []
        output_rows = {row["id"]: row for row in rows(world.db, "SELECT * FROM outputs")}
        availability = enum_for("outputs.availability")
        assert output_rows[device["id"]]["availability"] == int(availability.RESTRICTED)
        assert output_rows[device["id"]]["cleanup_status"] == int(enum_for("outputs.cleanup_status").INCOMPLETE)
        assert json.loads(output_rows[device["id"]]["cleanup_error_json"])["code"] == "file_delete_failed"
        assert output_rows[device_preview["id"]] == {key: value for key, value in device_preview.items()
            if key not in ("action_name", "locator_json", "relative_path")}
        assert all(output_rows[source["id"]]["availability"] == int(availability.CLEANED)
                   for source in (original, preview))
        snapshot = world.assert_report_ack_and_history()
        reported = next(a for plan in snapshot["plans"] for a in plan.get("actions", [])
                        if a["action_instance_id"] == str(action["id"]))
        assert reported["status"] == "failed"
        assert [(item["output_id"], item["status"]) for item in reported["result"]["items"]] == [
            (str(ids[0]), "failed"), (str(ids[1]), "succeeded"), (str(ids[2]), "succeeded")]
        assert reported["result"]["items"][0]["error"]["code"] == "file_delete_failed"


def test_cleanup_new_request_for_cleaned_output(tmp_path, cleanup_osmo_source, monkeypatch):
    with cleanup_osmo_source.consumer(tmp_path, monkeypatch) as world:
        world.clean_exact("osv")
        old_items = rows(world.db, "SELECT * FROM cleanup_items")
        old_runs = rows(world.db, "SELECT * FROM operation_runs")
        old_attempts = rows(world.db, "SELECT * FROM operation_attempts")
        old_calls = world.calls()
        request = world.begin_cleanup()
        world.wait_action(request, "SUCCEEDED")
        item = world.cleanup_item(request)
        assert item["id"] != old_items[0]["id"]
        assert item["outcome"] == int(enum_for("cleanup_items.outcome").ALREADY_CLEANED)
        assert world.attempts(item["id"]) == []
        assert rows(world.db, "SELECT * FROM cleanup_items WHERE id=?", (old_items[0]["id"],)) == old_items
        assert rows(world.db, "SELECT * FROM operation_runs") == old_runs
        assert rows(world.db, "SELECT * FROM operation_attempts") == old_attempts
        assert world.calls() == old_calls
        world.assert_terminal_report(request, "SUCCEEDED")


def _assert_original_attempt(world, original, intent):
    actual = next(t for t in world.attempts(world.cleanup_item(original["request"])["id"])
                  if t["id"] == original["attempt"]["id"])
    for field in ("id", "run_id", "attempt_no", "intent_event_id", "max_attempts_used",
                  "timeout_s_json", "retry_interval_s_json"):
        assert actual[field] == original["attempt"][field]
    assert actual["status"] == int(enum_for("operation_attempts.status").UNKNOWN)
    assert actual["result_event_id"] is not None
    assert world.event(actual["intent_event_id"]) == intent
    result_changes = [change for event in rows(world.db, "SELECT body_json FROM history_events")
                      for change in json.loads(event["body_json"])["rows"]
                      if change["table"] == "operation_attempts" and change["id"] == actual["id"]
                      and change["after"]["values"].get("result_event_id") is not None]
    assert len(result_changes) == 1
    return actual


def _interrupt(world, *, fault="before"):
    world.write_config(max_d=2, max_q=1, timeout="7.125", interval="0.625")
    request = world.begin_cleanup(fault=fault)
    marker = world.wait_marker("entered" if fault == "before" else "observed")
    item = world.cleanup_item(request)
    attempt, = world.attempts(item["id"])
    assert attempt["status"] == int(enum_for("operation_attempts.status").RUNNING)
    assert attempt["result_event_id"] is None
    assert (attempt["max_attempts_used"], attempt["timeout_s_json"], attempt["retry_interval_s_json"]) == (2, "7.125", "0.625")
    assert marker["ticket"]["id"] == attempt["id"]
    assert marker["modeled_unlink_count"] == (1 if fault == "after" else 0)
    assert world.path("osv").exists() == (fault == "before")
    intent = world.event(attempt["intent_event_id"])
    world.stop()
    return {"request": request, "attempt": attempt}, intent


def test_cleanup_interrupted_delete_absent(tmp_path, cleanup_osmo_source, monkeypatch):
    with cleanup_osmo_source.consumer(tmp_path, monkeypatch) as world:
        original, intent = _interrupt(world, fault="after")
        item = world.recover(original["request"], "SUCCEEDED", max_d=1, max_q=1)
        _assert_original_attempt(world, original, intent)
        assert [c["operation"] for c in world.calls()] == ["delete", "query"]
        assert item["outcome"] == int(enum_for("cleanup_items.outcome").ABSENCE_CONFIRMED)
        assert len(world.attempts(item["id"])) == 2
        query = world.attempts(item["id"])[1]
        assert json.loads(query["result_json"])["observations"] == [{"type": "file_presence", "version": 1,
            "data": {"cleanup_item_id": str(item["id"]), "present": False}}]
        world.assert_terminal_report(original["request"], "SUCCEEDED")


@pytest.mark.parametrize("partition", ["P1", "P2", "P3"])
def test_cleanup_query_present_or_unknown(partition, tmp_path, cleanup_osmo_source, monkeypatch):
    with cleanup_osmo_source.consumer(tmp_path, monkeypatch) as world:
        original, intent = _interrupt(world)
        expected = "SUCCEEDED" if partition == "P1" else "FAILED"
        item = world.recover(original["request"], expected, max_d={"P1": 3, "P2": 1, "P3": 2}[partition],
                             max_q=1, timeout="9.25", interval="0", query_timeout="11.5",
                             query_unknown=partition == "P3")
        old = _assert_original_attempt(world, original, intent)
        calls = world.calls()
        assert [c["operation"] for c in calls] == (["delete", "query", "delete"] if partition == "P1"
                                                else ["delete", "query"])
        attempts = world.attempts(item["id"])
        queries = [t for t in attempts if t["responsibility_key"].startswith("exists/")]
        assert len(queries) == 1 and queries[0]["attempt_no"] == 1
        assert queries[0]["timeout_s_json"] == "11.5" and queries[0]["max_attempts_used"] == 1
        expected_error = "delete_attempts_exhausted" if partition == "P2" else "file_query_attempts_exhausted"
        query_result = json.loads(queries[0]["result_json"])
        assert query_result["observations"] == ([] if partition == "P3" else [{
            "type": "file_presence", "version": 1,
            "data": {"cleanup_item_id": str(item["id"]), "present": True}}])
        if partition == "P1":
            deletes = [t for t in attempts if t["responsibility_key"].startswith("delete/")]
            assert len(deletes) == 2
            new = deletes[1]
            assert new["run_id"] == old["run_id"] and new["attempt_no"] == 2
            assert (new["timeout_s_json"], new["retry_interval_s_json"], new["max_attempts_used"]) == ("9.25", "0", 3)
            transaction = world.event(old["result_event_id"])["transaction_id"]
            result_events = [json.loads(e["body_json"]) for e in rows(world.db,
                "SELECT body_json FROM history_events WHERE transaction_id=? ORDER BY id", (transaction,))]
            changed_run, = [r for event in result_events for r in event["rows"]
                            if r["table"] == "operation_runs" and r["id"] == old["run_id"]]
            assert changed_run["after"]["values"]["retry_wait_required"] == 1
            assert not world.path("osv").exists()
        else:
            assert len(attempts) == 2 and world.path("osv").exists()
            assert item["error_code"] == item_error_id("cleanup_items", expected_error)
            assert item["restriction_state"] == int(enum_for("cleanup_items.restriction_state").IRREVERSIBLE)
        runs = rows(world.db, "SELECT * FROM operation_runs WHERE cleanup_item_id=? ORDER BY id", (item["id"],))
        assert [r["attempts_used"] for r in runs] == ([2, 1] if partition == "P1" else [1, 1])
        if partition == "P1":
            assert (runs[0]["max_attempts_used"], runs[0]["timeout_s_json"], runs[0]["retry_interval_s_json"]) == (3, "9.25", "0")
        world.assert_terminal_report(original["request"], expected, expected_error)


@pytest.mark.parametrize("phase", ["commit_unknown", "terminal_companions"])
def test_cleanup_saved_result_and_terminal_companions(phase, tmp_path, cleanup_osmo_source, monkeypatch):
    with cleanup_osmo_source.consumer(tmp_path, monkeypatch) as world:
        request = world.begin_cleanup(persistence_fault=phase)
        marker = world.wait_marker("cli_exited_after_unknown" if phase == "commit_unknown" else "terminal_before_companions")
        before = world.cleanup_item(request)
        attempts = world.attempts(before["id"])
        assert len(attempts) == 1
        saved = attempts[0]
        assert saved["status"] == int(enum_for("operation_attempts.status").SUCCEEDED)
        assert saved["result_event_id"] is not None and not world.path("osv").exists()
        result_event = world.event(saved["result_event_id"])
        assert len(world.calls("delete")) == 1 and world.calls("query") == []
        if phase == "commit_unknown":
            assert before["final_event_id"] is None and marker["exitcode"] == 1
            assert marker["key"] == result_event["operation_key"]
        else:
            assert before["status"] == int(enum_for("cleanup_items.status").SUCCEEDED)
            assert before["final_event_id"] is not None
            assert all(r["responsibility_key"].startswith("delete/") for r in marker["runs"])
        after = world.recover(request, "SUCCEEDED")
        assert world.attempts(after["id"]) == attempts
        assert world.event(saved["result_event_id"]) == result_event
        assert len(world.calls("delete")) == 1 and world.calls("query") == []
        if phase == "terminal_companions":
            assert after == before
        runs = rows(world.db, "SELECT * FROM operation_runs WHERE cleanup_item_id=?", (after["id"],))
        assert len(runs) == 1 and runs[0]["status"] == int(enum_for("operation_runs.status").SUCCEEDED)
        world.assert_terminal_report(request, "SUCCEEDED")


def test_cleanup_cancel_inflight_delete_recover(tmp_path, cleanup_osmo_source, monkeypatch):
    with cleanup_osmo_source.consumer(tmp_path, monkeypatch) as world:
        world.write_config(max_d=2, max_q=1, timeout="30")
        request = world.begin_cleanup(fault="before")
        marker = world.wait_marker("entered")
        item = world.cleanup_item(request)
        original, = world.attempts(item["id"])
        intent = world.event(original["intent_event_id"])
        plan, cancel = world.export("cancel-cleanup", [world.action("cancel-cleanup", "cancel_task",
                                   {"target": {"request_id": request}})])
        world.host.submit(plan)
        wait(lambda: rows(world.db, "SELECT a.cancel_requested FROM actions a JOIN plans p ON p.id=a.plan_id WHERE p.request_id=?",
                          (request,)) == [{"cancel_requested": 1}], "真实业务取消尚未保存")
        inflight, = world.attempts(item["id"])
        assert inflight == original
        assert inflight["status"] == int(enum_for("operation_attempts.status").RUNNING)
        assert inflight["result_event_id"] is None
        assert [call["operation"] for call in world.calls()] == ["delete"]
        process = process_identity(marker["pid"])
        assert process["starttime"] == marker["starttime"]
        assert process["pgid"] == marker["pgid"] and process["state"] not in ("Z", "X")
        json_write(world.root / "cancel-inflight-window.json", {
            "cancel_request": cancel["request_id"], "target_request": request,
            "target_actions": rows(world.db, "SELECT a.* FROM actions a JOIN plans p ON p.id=a.plan_id WHERE p.request_id=?", (request,)),
            "attempt": inflight, "gate": marker, "process": process,
            "calls": world.calls(),
        })
        final = world.recover(request, "CANCELED", max_d=2, max_q=1)
        _assert_original_attempt(world, {"request": request, "attempt": original}, intent)
        assert [c["operation"] for c in world.calls()] == ["delete", "query"]
        assert final["status"] == int(enum_for("cleanup_items.status").CANCELED)
        assert final["error_code"] == item_error_id("cleanup_items", "file_delete_failed")
        assert final["restriction_state"] == int(enum_for("cleanup_items.restriction_state").IRREVERSIBLE)
        assert world.path("osv").exists()
        cancel_action, = world.wait_action(cancel["request_id"], "FAILED")
        assert cancel_action["error_code"] == action_error_id("cancel_items_failed")
        cancel_item, = rows(world.db, "SELECT * FROM cancel_items WHERE action_id=?", (cancel_action["id"],))
        assert cancel_item["target_action_id"] == final["action_id"]
        assert cancel_item["status"] == int(enum_for("cancel_items.status").FAILED)
        world.assert_terminal_report(request, "CANCELED")

"""在预算组外准备参数并核对独立 worker 的完整持久结果。"""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[5]


def memory_budget_backend():
    sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))
    return importlib.import_module("camctl_memory")


def worker_command(input_path, result_path):
    return [sys.executable, str(Path(__file__).with_name("segment_capacity_worker.py")),
            str(input_path), str(result_path)]


def worker_environment():
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(str(REPOSITORY_ROOT / part) for part in (
        "apps/camctl/src", "apps/camctl/tests"))
    return environment


def _history_digest(connection, last_event):
    digest = hashlib.sha256()
    with closing(connection.execute("SELECT * FROM history_events WHERE id<=? ORDER BY id", (last_event,))) as cursor:
        for row in cursor:
            digest.update(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode())
            digest.update(b"\n")
    return digest.hexdigest()


def prepare_case(owned, request, *, observations=3):
    """仅序列化可靠原申请和身份；夹具及预期大数组留在组外。"""
    from camctl.capture.segment_finish import request_document
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.repositories.segments import SegmentRepository

    db = owned.connection
    setrow = SegmentRepository().read_set(request.ref, owned)
    members = db.execute(
        "SELECT m.id,m.file_id,f.original_name,f.media_type,m.page_event_id FROM segment_members m"
        " JOIN device_files f ON f.id=m.file_id WHERE m.set_id=?"
        " ORDER BY COALESCE(m.directory_number,0),COALESCE(m.segment_index,0),m.identity,m.id",
        (setrow["id"],),
    ).fetchall()
    assert len(members) >= observations
    assert db.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
    scope = db.execute(
        "SELECT s.id,s.member_id,s.ordinal,m.file_id FROM segment_scope_members s"
        " JOIN segment_members m ON m.id=s.member_id WHERE s.set_id=? ORDER BY s.ordinal",
        (setrow["id"],),
    ).fetchall()
    count, last = db.execute("SELECT COUNT(*),MAX(id) FROM history_events").fetchone()
    manifest = {
        "version": 1,
        "database": str(Path(db.execute("PRAGMA database_list").fetchone()[2]).resolve()),
        "request": request_document(request), "key": str(new_operation_key()), "batch_size": 4,
        "observations": [{"file_id": row[1], "key": str(new_operation_key()),
                          "occurred_at": request.occurred_at + index + 1, "sha256": "a" * 64}
                         for index, row in enumerate(members[:observations])],
    }
    expected = {
        "members": members, "scope": scope, "set_id": setrow["id"],
        "history_before": [count, last], "history_prefix_sha256": _history_digest(db, last),
        "journal_mode": db.execute("PRAGMA journal_mode").fetchone()[0],
        "result": {"ref": manifest["request"]["ref"], "action_status": 3, "plan_status": 3,
                   "output_count": len(members), "original_count": len(members),
                   "preview_count": 0, "scope_count": len(scope)},
    }
    return manifest, expected


def read_worker_result(path, manifest, expected):
    """退出成功也必须有属于本次完整申请的实际结果。"""
    if not path.is_file():
        raise AssertionError("容量 worker 没有完整业务结果")
    actual = json.loads(path.read_text())
    required = {"version", "database", "key", "request", "write", "verified", "repeated",
                "history_before", "history_after_write", "history_after_repeated", "rounds"}
    assert isinstance(actual, dict) and required <= set(actual), "容量 worker 业务结果不完整"
    assert actual["version"] == 1
    for key in ("database", "key", "request"):
        assert actual[key] == manifest[key], key
    assert actual["history_before"] == expected["history_before"]
    assert actual["write"]["kind"] == actual["repeated"]["kind"] == "completed"
    result = actual["write"]["result"]
    assert result == actual["verified"] == actual["repeated"]["result"]
    assert {key: value for key, value in result.items() if key != "boundary"} == expected["result"]
    assert actual["write"]["boundary"] == result["boundary"]
    assert actual["history_after_repeated"] == actual["history_after_write"]
    assert len(actual["rounds"]) == len(manifest["observations"])
    previous = actual["history_after_write"]
    for observed, wanted in zip(actual["rounds"], manifest["observations"]):
        assert observed["observation"] == wanted
        assert observed["before"] == previous
        assert observed["after"] == [previous[0] + 1, previous[1] + 1]
        assert observed["after_verify"] == observed["after"]
        assert observed["verified"] == result
        previous = observed["after"]
    return actual


def assert_completed_case(manifest, expected, result_path):
    """连接重开后逐项核对全体来源、范围、原完整组和后续观察。"""
    from .segment_capacity_worker import decode_request
    from camctl.contracts.values import OperationKey
    from camctl.history.decoding import decode_event_row
    from camctl.persistence.repositories.segment_transactions import verify_segment_finish
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

    actual = read_worker_result(result_path, manifest, expected)
    assert actual["runtime"]["camctl_file"] == str(REPOSITORY_ROOT / "apps/camctl/src/camctl/__init__.py")
    result = actual["verified"]
    owned = open_existing(Path(manifest["database"]), DbOpenMode.EXISTING_RW, DbConfig())
    try:
        db = owned.connection
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == expected["journal_mode"]
        assert _history_digest(db, expected["history_before"][1]) == expected["history_prefix_sha256"]
        transaction = db.execute(
            "SELECT id,first_event_id,last_event_id FROM history_transactions WHERE operation_key=?",
            (manifest["key"],),
        ).fetchall()
        assert len(transaction) == 1
        txn, first, last = transaction[0]
        assert first == expected["history_before"][1] + 1
        assert {"txn_id": txn, "last_event_id": last} == result["boundary"]
        rows = db.execute(
            "SELECT id,transaction_id,event_type,event_version,occurred_at,clock_status,change_seq,body_json"
            " FROM history_events WHERE transaction_id=? ORDER BY id", (txn,),
        ).fetchall()
        assert [row[0] for row in rows] == list(range(first, last + 1))
        assert all(row[1] == txn for row in rows)
        events = [decode_event_row(row) for row in rows]
        assert events[0].evidence["segment_finish_request"]["request"] == manifest["request"]
        output_events = [(event, row) for event in events for row in event.rows if row.table == "outputs"]
        assert len(output_events) == len(expected["members"])
        output_ids = {}
        columns = [row[1] for row in db.execute("PRAGMA table_info(outputs)")]
        persisted = [dict(zip(columns, row)) for row in db.execute("SELECT * FROM outputs ORDER BY id")]
        assert len(persisted) == len(expected["members"])
        for number, ((event, change), member, saved) in enumerate(zip(output_events, expected["members"], persisted), 1):
            _, file_id, original_name, media_type, _ = member
            wanted = {"source_action_id": manifest["request"]["ref"]["action_id"], "kind": 1,
                      "device_file_id": file_id, "intermediate_file_id": None,
                      "original_name": original_name, "media_type": media_type, "availability": 1,
                      "cleanup_status": 1, "cleanup_error_json": None,
                      "media_json": {"check_status": "not_performed", "duration": {"status": "unknown"}},
                      "error_json": None}
            assert change.row_id == saved["id"] == number
            assert not change.before.exists and change.after.exists
            assert dict(change.after.values) == wanted
            for key, value in wanted.items():
                persisted_value = saved[key]
                if key.endswith("_json") and persisted_value is not None:
                    persisted_value = json.loads(persisted_value)
                assert persisted_value == value, (number, key)
            assert saved["created_event_id"] == saved["last_event_id"] == event.event_id
            assert saved["change_count"] == 1
            output_ids[file_id] = number
        assert db.execute("SELECT COUNT(*) FROM output_origins").fetchone() == (0,)
        wanted_scope = [(row_id, member_id, ordinal, output_ids[file_id])
                        for row_id, member_id, ordinal, file_id in expected["scope"]]
        persisted_scope = db.execute(
            "SELECT id,member_id,ordinal,output_id FROM segment_scope_members WHERE set_id=? ORDER BY ordinal",
            (expected["set_id"],),
        ).fetchall()
        assert persisted_scope == wanted_scope
        scope_changes = [row for event in events for row in event.rows if row.table == "segment_scope_members"]
        assert [(row.row_id, row.after.values["output_id"]) for row in scope_changes] == [
            (row[0], row[3]) for row in wanted_scope]
        assert all(dict(row.before.values) == {"output_id": None} for row in scope_changes)
        assert db.execute("SELECT published_event_id FROM segment_sets WHERE id=?", (expected["set_id"],)).fetchone() == (last,)
        assert events[-1].event_id == last
        assert events[-1].rows[0].table == "segment_sets"
        for index, observation in enumerate(manifest["observations"], 1):
            assert db.execute(
                "SELECT checksum_support,sha256,last_event_id FROM device_files WHERE id=?",
                (observation["file_id"],),
            ).fetchone() == (2, observation["sha256"], last + index)
            assert db.execute(
                "SELECT first_event_id,last_event_id FROM history_transactions WHERE operation_key=?",
                (observation["key"],),
            ).fetchone() == (last + index, last + index)
        assert db.execute("SELECT COUNT(*),MAX(id) FROM history_events").fetchone() == (
            actual["history_after_write"][0] + len(manifest["observations"]), last + len(manifest["observations"]))
        action_id = manifest["request"]["ref"]["action_id"]
        assert db.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (3,)
        assert db.execute("SELECT p.status FROM plans p JOIN actions a ON a.plan_id=p.id WHERE a.id=?",
                          (action_id,)).fetchone() == (3,)
        assert db.execute("SELECT status FROM operation_runs WHERE id=?",
                          (manifest["request"]["run_id"],)).fetchone() == (3,)
        verified = verify_segment_finish(decode_request(manifest["request"]), OperationKey(manifest["key"]),
                                         owned, execution_finished=True, batch_size=4)
        assert asdict(verified) == result
    finally:
        owned.connection.close()
    return actual

"""来源容量的组外准备、公开 worker 协议和独立持久结果核对。"""

from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, fields
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
import time


REPOSITORY_ROOT = Path(__file__).parents[5]
STAGES = ("write", "reuse", "repeat")
_NOW = 1_750_000_000_000_000


def result_document(value):
    """保存完整可比较回执；运行期诊断对象不是业务结果字段。"""
    return {field.name: getattr(value, field.name)
            for field in fields(value) if field.compare}


def expected_result(manifest):
    return {
        "fixed": True, "member_count": manifest["count"],
        "selection_count": manifest["count"],
        "source_plan_id": manifest["request"]["spec"]["plan_instance_id"],
        "error_code": None, "error_details": None,
    }


def validate_stage_message(message, manifest, stage):
    """退出成功之外，逐次核对原申请、原键及完整返回，禁止漏过中间步骤。"""
    assert isinstance(message, dict) and message.get("phase") == "result"
    for name in ("version", "database", "key", "request", "batch_size"):
        assert message.get(name) == manifest[name], name
    assert message.get("stage") == stage
    assert message.get("kind") == "completed"
    assert message.get("value") == expected_result(manifest)
    seconds = message.get("seconds")
    assert isinstance(seconds, (float, int)) and math.isfinite(seconds) and seconds >= 0


def memory_budget_backend():
    sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))
    return importlib.import_module("camctl_memory")


def _save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2))


def _digest(connection, *, last_event=None):
    digest = hashlib.sha256()
    if last_event is None:
        for line in connection.iterdump():
            digest.update(line.encode())
            digest.update(b"\n")
    else:
        with closing(connection.execute(
                "SELECT * FROM history_events WHERE id<=? ORDER BY id", (last_event,))) as cursor:
            for row in cursor:
                digest.update(json.dumps(row, ensure_ascii=False,
                                         separators=(",", ":")).encode())
                digest.update(b"\n")
    return digest.hexdigest()


async def prepare_case(directory, *, count=2400):
    """正式受理两份计划并实际开始取回；所有准备连接在 worker 入组前关闭。"""
    from camctl.contracts.values import new_operation_key
    from camctl.outputs.sources import read_source_spec
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.acceptance import register_acceptance_guards
    from camctl.persistence.repositories.capture import register_capture_guards
    from camctl.persistence.repositories.operations import register_operation_guards
    from camctl.persistence.repositories.outputs import (
        OutputsRepository, ResolveSources, StartObtainAction, register_outputs_guards,
    )
    from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
    from .database import create_test_database
    from .output_source_cases import accept_body

    for register in (register_acceptance_guards, register_capture_guards,
                     register_operation_guards, register_outputs_guards):
        register()
    directory.mkdir()
    path = directory / "state.db"
    create_test_database(path)
    owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        shots = [{
            "name": f"shot-{index}", "type": "camera_take_photo", "device_id": "cam-1",
            "scheduled_at": "2026-01-15 09:00:00", "params": {"type": "single_shot"},
            "policy": {"max_delay_ms": 1000},
        } for index in range(count)]
        source = await accept_body(owned, {
            "request_id": "501", "created_at": "2026-01-15 08:00:00",
            "name": "capacity sources", "actions": shots,
        }, occurred_at=_NOW)
        spec = {"plan_instance_id": str(source.plan_id)}
        owner = await accept_body(owned, {
            "request_id": "502", "created_at": "2026-01-15 08:00:00",
            "name": "capacity obtain", "actions": [{
                "name": "fetch", "type": "obtain_action_outputs",
                "scheduled_at": "2026-01-15 09:00:00",
                "params": {"source": spec, "purpose": "manual"},
            }],
        }, occurred_at=_NOW)
        connection = owned.connection
        action_id, = connection.execute(
            "SELECT id FROM actions WHERE plan_id=?", (owner.plan_id,)).fetchone()
        started = OutputsRepository(1).start_obtain_action(
            StartObtainAction(action_id, _NOW), new_operation_key(), owned)
        assert started.kind is DbOutcomeKind.COMPLETED, started.error
        request = ResolveSources(action_id, read_source_spec(
            json.dumps({"params": {"source": spec}})), _NOW)
        last, = connection.execute("SELECT MAX(id) FROM history_events").fetchone()
        manifest = {
            "version": 1, "count": count, "database": str(path.resolve()),
            "request": asdict(request), "key": str(new_operation_key()), "batch_size": 4,
        }
        expected = {
            "last_event_before": last,
            "history_prefix": _digest(connection, last_event=last),
            "source_action_ids": [row[0] for row in connection.execute(
                "SELECT id FROM actions WHERE plan_id=? ORDER BY id", (source.plan_id,))],
        }
        assert len(expected["source_action_ids"]) == count
        assert connection.in_transaction is False
        return manifest, expected
    finally:
        owned.connection.close()


def assert_database(manifest, expected):
    """组外重开实际库，核对所有成员、选择、完整历史、外键和全库摘要。"""
    with closing(sqlite3.connect(manifest["database"])) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert _digest(connection, last_event=expected["last_event_before"]) == expected["history_prefix"]
        action_id = manifest["request"]["action_id"]
        dependencies = connection.execute(
            "SELECT id,depends_on_action_id FROM action_dependencies WHERE action_id=? ORDER BY id",
            (action_id,)).fetchall()
        assert [row[1] for row in dependencies] == expected["source_action_ids"]
        selections = connection.execute(
            "SELECT s.dependency_id,s.status,s.error_code,s.error_details_json"
            " FROM obtain_source_selections s JOIN action_dependencies d ON d.id=s.dependency_id"
            " WHERE d.action_id=? ORDER BY s.id", (action_id,)).fetchall()
        assert [row[0] for row in selections] == [row[0] for row in dependencies]
        assert all(row[1:] == (1, None, None) for row in selections)
        assert len(selections) == len(dependencies) == manifest["count"]
        assert connection.execute(
            "SELECT source_resolution_state,resolved_source_plan_id FROM actions WHERE id=?",
            (action_id,)).fetchone() == (2, expected_result(manifest)["source_plan_id"])
        transactions = connection.execute(
            "SELECT id,first_event_id,last_event_id FROM history_transactions WHERE operation_key=?",
            (manifest["key"],)).fetchall()
        assert len(transactions) == 1
        transaction_id, first, last = transactions[0]
        assert first == expected["last_event_before"] + 1
        assert last - first + 1 == math.ceil(manifest["count"] / manifest["batch_size"]) + 1
        counts = {"action_dependencies": 0, "obtain_source_selections": 0}
        identities = []
        for identity, body in connection.execute(
                "SELECT id,body_json FROM history_events WHERE transaction_id=? ORDER BY id",
                (transaction_id,)):
            identities.append(identity)
            event = json.loads(body)
            assert len(event["rows"]) <= 128
            for row in event["rows"]:
                if row["table"] in counts:
                    counts[row["table"]] += 1
        assert identities == list(range(first, last + 1))
        assert all(value == manifest["count"] for value in counts.values())
        return {"digest": _digest(connection), "transaction": transactions[0],
                "event_count": last - first + 1, "member_count": len(dependencies)}


async def run_capacity(directory):
    """本来源负载的进程组预算验收；仅证明实际执行规模。"""
    backend = memory_budget_backend()
    budget = backend.get_budget_config()
    timing = {"count": 2400, "batch_size": 4}
    started = time.perf_counter()
    backend.ensure_memory_budget_available(budget)
    timing["preflight_seconds"] = time.perf_counter() - started
    started = time.perf_counter()
    manifest, expected = await prepare_case(directory / "world")
    timing["preparation_seconds"] = time.perf_counter() - started
    _save(directory / "input.json", manifest)
    _save(directory / "expected.json", expected)
    worker = REPOSITORY_ROOT / "apps/camctl/tests/integration/outputs/source_capacity_worker.py"
    command = [sys.executable, str(worker), str(directory / "input.json"),
               str(directory / "result.json")]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(str(REPOSITORY_ROOT / path)
        for path in ("apps/camctl/src", "apps/camctl/tests"))
    messages, checks = [], []
    timing["outside_database_checks_seconds"] = 0.0
    started = time.perf_counter()
    try:
        with (directory / "worker.stderr").open("w") as stderr, (directory / "worker.stdout").open("w") as stdout:
            with backend.BudgetedPopen(
                    command, budget=budget, receipt_path=directory / "memory-budget.json",
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                    cwd=REPOSITORY_ROOT, env=environment, text=True, bufsize=1) as process:
                for stage in (*STAGES, "closed"):
                    assert select.select([process.stdout], [], [], 600)[0], "来源 worker 未交付结果"
                    line = process.stdout.readline()
                    stdout.write(line)
                    stdout.flush()
                    assert line, (directory / "worker.stderr").read_text()
                    message = json.loads(line)
                    if stage == "closed":
                        assert message.get("phase") == "closed"
                        closed = message
                        break
                    validate_stage_message(message, manifest, stage)
                    messages.append(message)
                    check_started = time.perf_counter()
                    facts = assert_database(manifest, expected)
                    if checks:
                        assert facts["digest"] == checks[0]["digest"]
                    checks.append(facts)
                    timing["outside_database_checks_seconds"] += time.perf_counter() - check_started
                    _save(directory / "checks.json", checks)
                    process.stdin.write("continue\n")
                    process.stdin.flush()
                assert process.wait(timeout=600) == 0
    finally:
        timing["budgeted_process_with_outside_checks_seconds"] = time.perf_counter() - started
        _save(directory / "capacity-timing.json", timing)
    actual = json.loads((directory / "result.json").read_text())
    assert actual == closed and actual["messages"] == messages
    resource = json.loads((directory / "memory-budget.json").read_text())
    runtime = actual["runtime"]
    assert runtime["pid"] == resource["pid"]
    assert runtime["camctl_file"] == str(REPOSITORY_ROOT / "apps/camctl/src/camctl/__init__.py")
    assert runtime["pytest_imported"] is runtime["tracemalloc_active"] is runtime["profile_active"] is False
    assert Path(resource["group"]).name in runtime["cgroup"]
    assert Path(resource["group"]).name not in Path("/proc/self/cgroup").read_text()
    assert resource["status"] == "completed" and resource["returncode"] == 0
    assert resource["group_removed"] and not Path(resource["group"]).exists()
    evidence = resource["evidence"]
    assert evidence["limit_bytes"] == evidence["combined_limit_bytes"] == budget.limit_bytes
    assert evidence["combined_peak_bytes"] <= budget.limit_bytes
    assert all(isinstance(value, int) for value in evidence["events"].values())
    assert evidence["events"].get("oom_kill", evidence["events"].get("oom", 0)) == 0
    check_started = time.perf_counter()
    assert assert_database(manifest, expected)["digest"] == checks[0]["digest"]
    timing["final_outside_check_seconds"] = time.perf_counter() - check_started
    timing["worker_seconds"] = {message["stage"]: message["seconds"] for message in messages}
    _save(directory / "capacity-timing.json", timing)
    return checks[-1]

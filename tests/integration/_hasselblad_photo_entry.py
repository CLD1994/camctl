"""复制到仓库外的安装 CLI 入口；只记录来源并装配本测试的确定断点。"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, fields, is_dataclass
from enum import Enum
import json
import os
from pathlib import Path
import sys
import time
import hashlib
import sqlite3
from datetime import datetime, timezone

import camctl


def _require_boundary(connection, boundary):
    row = connection.execute("SELECT * FROM history_transactions WHERE id=?", (boundary["txn_id"],)).fetchone()
    assert row is not None and row["last_event_id"] == boundary["last_event_id"], "H 必须是完整事务结束位置"
    return dict(row)


def _event_boundary(connection, event_id):
    event = connection.execute("SELECT transaction_id FROM history_events WHERE id=?", (event_id,)).fetchone()
    assert event is not None, ("缺少原事件", event_id)
    row = dict(connection.execute("SELECT * FROM history_transactions WHERE id=?", (event[0],)).fetchone())
    assert row["first_event_id"] <= event_id <= row["last_event_id"]
    boundary = {"txn_id": row["id"], "last_event_id": row["last_event_id"]}
    _require_boundary(connection, boundary)
    return {**boundary, "operation_key": row["operation_key"], "first_event_id": row["first_event_id"]}


def _validated_control_result(attempt, raw, target):
    from camctl.contracts.json_values import parse_exact_json
    from camctl.operations.result_format import read_result_document
    from camctl.operations.models import AttemptTicket, AttemptStatus, EffectState
    from camctl.operations.validation import validate_outcome
    from camctl.devices.drivers.hasselblad.driver import EVIDENCE
    result = parse_exact_json(raw)
    outcome = read_result_document(attempt["status"], attempt["effect_state"], result,
                                   None if attempt["error_json"] is None else parse_exact_json(attempt["error_json"]))
    ticket = AttemptTicket(attempt["attempt_no"], "control", str(target["activity_id"]),
                           target["responsibility_key"], target["run_id"])
    validate_outcome(ticket, outcome, EVIDENCE)
    assert outcome.status is AttemptStatus.SUCCEEDED and outcome.effect is EffectState.CONFIRMED
    return result, ticket


def _assert_steps(result, steps, expected_steps, expected):
    assert [step["name"] for step in steps] == expected_steps
    calls = result["diagnostics"]["calls"]
    assert len(calls) == len(steps)
    streams = []
    for index, (step, call) in enumerate(zip(steps, calls, strict=True), 1):
        assert call["step_index"] == index and call["step_name"] == step["name"]
        assert step["local_exit_code"] == 0 and step["local_signal"] is None
        assert step["transport_error"] is None and step["output_failure"] is None and step["signal_failures"] == []
        if step["name"] in ("focus", "autofocus"):
            assert step["lens_operation_returned"] is True
        if step["name"] == "capture" and "camera_name" in expected:
            assert step["files"] == [{"host_name": expected["artifacts"][role]["original_name"],
                                      "size_bytes": expected["artifacts"][role]["size_bytes"],
                                      "media_type": expected["artifacts"][role]["media_type"]}
                                     for role in ("original", "preview")]
            assert step["camera_locator"] == {"status": "path", "camera_name": expected["camera_name"],
                                               "camera_path": expected["camera_path"]}
        facts = {}
        for name in ("stdout", "stderr"):
            stream = call[name]
            assert stream["state"] == "complete"
            data = bytes.fromhex(stream["hex"])
            facts[name] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            if name == "stdout":
                assert len(data) == step["stdout_size_bytes"] and facts[name]["sha256"] == step["stdout_sha256"]
            else:
                assert data == b""
        streams.append({"step_index": index, "step_name": step["name"], **facts})
    return streams


def _assert_start_result(attempt, raw, target, expected):
    result, ticket = _validated_control_result(attempt, raw, target)
    assert [item["type"] for item in result["observations"]] == ["photo_taken", "hasselblad_start_entered"]
    for observation in result["observations"]:
        assert observation["version"] == 1 and observation["data"]["activity_id"] == str(target["activity_id"])
    photo = result["observations"][0]["data"]
    for role in ("original", "preview"):
        assert photo[role] == expected["artifacts"][role], ("原角色字节身份不符", role)
    steps = photo["steps"]
    assert expected["steps"].count("capture") == 1
    streams = _assert_steps(result, steps, expected["steps"], expected)
    return {"result_json": raw, "size": len(raw.encode()), "sha256": hashlib.sha256(raw.encode()).hexdigest(),
            "ticket": {"attempt_no": ticket.attempt_id, "run_id": ticket.run_id,
                       "activity_id": ticket.target_id, "responsibility_key": ticket.responsibility_key},
            "photo": photo, "streams": streams}


def _assert_preparation_result(attempt, raw, target, expected):
    result, ticket = _validated_control_result(attempt, raw, target)
    assert [item["type"] for item in result["observations"]] == [
        "capture_preparation_completed", "photo_preparation_steps"]
    for observation in result["observations"]:
        assert observation["version"] == 1 and observation["data"]["activity_id"] == str(target["activity_id"])
    streams = _assert_steps(result, result["observations"][1]["data"]["steps"], expected["steps"], expected)
    return {"result_json": raw, "ticket": asdict(ticket), "streams": streams}


def _history_facts(database, request, expected, original=None):
    from camctl.contracts.enums import enum_for, load_registry
    from camctl.contracts.json_values import parse_exact_json
    from camctl.contracts.history_values import HistoryBoundary
    from camctl.persistence.repositories.history import HistoryRepository
    from camctl.history.host_photo_request import parse_host_photo_request
    public = parse_exact_json(Path(request).read_text())
    assert len(public["actions"]) == 1 and public["actions"][0]["type"] == "camera_take_photo"
    assert public["actions"][0]["params"] == parse_exact_json(expected["params_text"])
    connection = sqlite3.connect(f"file:{Path(database).as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    def one(sql, params=()):
        rows = connection.execute(sql, params).fetchall()
        assert len(rows) == 1, (sql, len(rows))
        return dict(rows[0])
    try:
        action = one("SELECT a.* FROM actions a JOIN plans p ON p.id=a.plan_id WHERE p.request_id=? AND a.name=?",
                     (int(public["request_id"]), public["actions"][0]["name"]))
        plan = one("SELECT * FROM plans WHERE id=?", (action["plan_id"],))
        assert action["type"] == int(enum_for("actions.type").CAMERA_TAKE_PHOTO)
        assert action["device_id"] == public["actions"][0]["device_id"]
        assert action["driver_id"] == "hasselblad_cfv100c"
        run = one("SELECT * FROM operation_runs WHERE action_id=? AND kind=?",
                  (action["id"], int(enum_for("operation_runs.kind").START)))
        attempt = one("SELECT * FROM operation_attempts WHERE run_id=?", (run["id"],))
        activity = one("SELECT * FROM device_activities WHERE id=?", (run["activity_id"],))
        assert activity["action_id"] == action["id"] and run["responsibility_key"] == f"start/{action['id']}"
        target = {"request_id": str(plan["request_id"]), "plan_id": plan["id"], "action_id": action["id"],
                  "run_id": run["id"], "attempt_id": attempt["id"], "activity_id": activity["id"],
                  "responsibility_key": run["responsibility_key"], "result_event_id": attempt["result_event_id"]}
        preparation_run = one("SELECT * FROM operation_runs WHERE action_id=? AND kind=?",
                              (action["id"], int(enum_for("operation_runs.kind").PREPARE_CAPTURE)))
        preparation_attempt = one("SELECT * FROM operation_attempts WHERE run_id=?", (preparation_run["id"],))
        assert preparation_run["activity_id"] == activity["id"]
        preparation_target = {**target, "run_id": preparation_run["id"],
                              "responsibility_key": preparation_run["responsibility_key"]}
        preparation_steps = [name for name in expected["steps"] if name not in ("focus", "autofocus", "capture")]
        start_steps = [name for name in expected["steps"] if name in ("focus", "autofocus", "capture")]
        preparation = _assert_preparation_result(preparation_attempt, preparation_attempt["result_json"],
                                                 preparation_target, {**expected, "steps": preparation_steps})
        start = _assert_start_result(attempt, attempt["result_json"], target, {**expected, "steps": start_steps})
        files = [dict(row) for row in connection.execute(
            "SELECT * FROM intermediate_files WHERE owner_action_id=? AND purpose IN (?,?) ORDER BY purpose",
            (action["id"], int(enum_for("intermediate_files.purpose").CAPTURE_ORIGINAL),
             int(enum_for("intermediate_files.purpose").CAPTURE_PREVIEW)))]
        outputs = [dict(row) for row in connection.execute(
            "SELECT * FROM outputs WHERE source_action_id=? ORDER BY kind", (action["id"],))]
        assert len(files) == len(outputs) == 2
        complete = []
        for event in connection.execute("SELECT id,body_json FROM history_events WHERE json_extract(body_json,'$.evidence.host_photo_request.action_id')=?", (action["id"],)):
            raw = parse_exact_json(event["body_json"])["evidence"]["host_photo_request"]
            if "original_file_id" not in raw:
                continue
            saved = parse_host_photo_request(raw, reason=2)
            assert saved.original_file_id == files[0]["id"] and saved.preview_file_id == files[1]["id"]
            for role in ("original", "preview"):
                assert getattr(saved, role + "_size_bytes") == expected["artifacts"][role]["size_bytes"]
                assert getattr(saved, role + "_sha256") == expected["artifacts"][role]["sha256"]
            complete.append((event["id"], raw))
        assert len(complete) == 1
        derived = {"start_result": _event_boundary(connection, attempt["result_event_id"]),
                   "files_complete": _event_boundary(connection, complete[0][0]),
                   "outputs_registered": _event_boundary(connection, outputs[0]["created_event_id"])}
        assert _event_boundary(connection, outputs[1]["created_event_id"]) == derived["outputs_registered"]
        derived["start_result"]["event_ids"] = [attempt["result_event_id"]]
        derived["files_complete"]["event_ids"] = [complete[0][0]]
        derived["outputs_registered"]["event_ids"] = [row["created_event_id"] for row in outputs]
        entities = [{"entity": "plan", "id": plan["id"], "table": "plans", "created_event_id": plan["created_event_id"]},
                    {"entity": "action", "id": action["id"], "table": "actions", "created_event_id": action["created_event_id"]}]
        for entity, table, values in (("intermediate_file", "intermediate_files", files), ("output", "outputs", outputs)):
            entities.extend({"entity": entity, "table": table, "id": row["id"], "created_event_id": row["created_event_id"]} for row in values)
        device_files = [dict(row) for row in connection.execute("SELECT * FROM device_files WHERE source_action_id=? ORDER BY role", (action["id"],))]
        entities.extend({"entity": "device_file", "table": "device_files", "id": row["id"], "created_event_id": row["created_event_id"]} for row in device_files)
        for entity in entities:
            assert load_registry()["history_objects"][entity["entity"]]["table"] == entity["table"]
            entity["created_boundary"] = _event_boundary(connection, entity["created_event_id"])
        if original is not None:
            assert target == original["identity"] and entities == original["entities"]
            assert derived == original["boundaries"] and start == original["start"]
            assert preparation == original["preparation"]
            boundaries = original["boundaries"]
        else:
            boundaries = derived
        restored = {}
        for phase, boundary in boundaries.items():
            _require_boundary(connection, boundary)
            repository = HistoryRepository(Path(database))
            h = HistoryBoundary(boundary["txn_id"], boundary["last_event_id"])
            image = {}
            for entity in entities:
                rows = repository.restore_entity(entity["entity"], entity["id"], h)
                present = entity["created_boundary"]["last_event_id"] <= h.last_event_id
                assert ((entity["table"], entity["id"]) in rows) is present
                if not present:
                    assert rows == {}, (phase, entity)
                image[f"{entity['entity']}/{entity['id']}"] = [
                    {"table": table, "id": row_id, "values": values}
                    for (table, row_id), values in sorted(rows.items())]
            action_rows = {(row["table"], row["id"]): row["values"] for row in image[f"action/{action['id']}"]}
            historical = action_rows[("actions", action["id"])]
            assert historical["input_fields_json"]["params"] == parse_exact_json(expected["params_text"])
            assert historical["effective_params_json"] == parse_exact_json(expected["params_text"])
            assert historical["execution_spec_json"] == {"output_storage": expected["source"], "start_call_timeout_s": 180}
            for table, row in (("operation_runs", run), ("operation_attempts", attempt), ("device_activities", activity)):
                assert (table, row["id"]) in action_rows
            saved_attempt = action_rows[("operation_attempts", attempt["id"])]
            historical_run = action_rows[("operation_runs", run["id"])]
            for field in ("action_id", "activity_id", "responsibility_key", "kind"):
                assert historical_run[field] == run[field]
            assert saved_attempt["run_id"] == run["id"] and saved_attempt["attempt_no"] == attempt["attempt_no"]
            assert action_rows[("device_activities", activity["id"])]["action_id"] == action["id"]
            assert saved_attempt["result_json"] == parse_exact_json(attempt["result_json"])
            assert saved_attempt["result_event_id"] == attempt["result_event_id"]
            historical_preparation = action_rows[("operation_attempts", preparation_attempt["id"])]
            assert historical_preparation["result_json"] == parse_exact_json(preparation_attempt["result_json"])
            assert historical_preparation["result_event_id"] == preparation_attempt["result_event_id"]
            for index, role in enumerate(("original", "preview")):
                file_rows = image[f"intermediate_file/{files[index]['id']}"]
                completed = h.last_event_id >= boundaries["files_complete"]["last_event_id"]
                assert file_rows or not completed, "完成事务 H 缺少原捕获文件"
                if file_rows:
                    file = next(row["values"] for row in file_rows if row["table"] == "intermediate_files")
                    assert file["owner_action_id"] == action["id"] and file["purpose"] == files[index]["purpose"]
                    assert file["relative_path"] == files[index]["relative_path"]
                    assert file["size_bytes"] == (expected["artifacts"][role]["size_bytes"] if completed else None)
                    assert file["sha256"] == (expected["artifacts"][role]["sha256"] if completed else None)
                output_rows = image[f"output/{outputs[index]['id']}"]
                if not output_rows:
                    continue
                output = next(row["values"] for row in output_rows if row["table"] == "outputs")
                assert output["source_action_id"] == action["id"] and output["kind"] == int(enum_for("outputs.kind")[role.upper()])
                assert output["media_type"] == expected["artifacts"][role]["media_type"]
                assert output["original_name"] == expected["artifacts"][role]["original_name"]
                if expected["source"] == "host":
                    assert output["intermediate_file_id"] == files[index]["id"] and output["device_file_id"] is None
                else:
                    assert len(device_files) == 2 and output["device_file_id"] == device_files[index]["id"] and output["intermediate_file_id"] is None
                    device = next(row["values"] for row in image[f"device_file/{device_files[index]['id']}"] if row["table"] == "device_files")
                    locator = device["locator_json"]
                    assert locator == {"camera_path": expected["camera_path"], "camera_name": expected["camera_name"],
                                       "variant": "original" if index == 0 else "thumbnail", "sha256": expected["artifacts"][role]["sha256"]}
                    assert device["size_bytes"] == expected["artifacts"][role]["size_bytes"]
                    assert device["sha256"] == expected["artifacts"][role]["sha256"]
                    assert device["original_name"] == expected["artifacts"][role]["original_name"]
                    assert device["media_type"] == expected["artifacts"][role]["media_type"]
                    assert device["role"] == int(enum_for("device_files.role")[role.upper()])
                    assert device["original_device_file_id"] == (None if index == 0 else device_files[0]["id"])
                if index:
                    origin, = [row["values"] for row in output_rows if row["table"] == "output_origins"]
                    assert (origin["output_id"], origin["original_output_id"]) == (outputs[1]["id"], outputs[0]["id"])
            restored[phase] = image
        return {"identity": target, "boundaries": boundaries, "entities": entities,
                "preparation": preparation, "start": start, "complete_request": complete[0][1], "snapshots": restored}
    finally:
        connection.close()


def _history_command(arguments):
    from camctl.contracts.json_values import parse_exact_json
    from camctl.persistence.transaction import encode_json_value
    database, request, expected_path, output, *previous = arguments
    expected = parse_exact_json(Path(expected_path).read_text())
    old = parse_exact_json(Path(previous[0]).read_text())["facts"] if previous else None
    facts = _history_facts(database, request, expected, old)
    def canonical(value):
        if isinstance(value, dict):
            return {key: canonical(value[key]) for key in sorted(value)}
        if isinstance(value, list):
            return [canonical(item) for item in value]
        return value
    Path(output).write_text(encode_json_value(canonical({"facts": facts, "provenance": {
        "python": sys.executable, "camctl": camctl.__file__, "pid": os.getpid(),
        "utc": datetime.now(timezone.utc).isoformat(), "database": str(database)}})))
    return 0


def _plain(value):
    if is_dataclass(value):
        return {field.name: _plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(_plain(item) for item in value)
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if isinstance(value, bytes):
        return {"hex": value.hex()}
    return value


def _write(root: Path, name: str, value: dict):
    temporary = root / f".{name}.{os.getpid()}.tmp"
    with temporary.open("x") as stream:
        from camctl.persistence.transaction import encode_json_value
        stream.write(encode_json_value(_plain({"pid": os.getpid(), "pgid": os.getpgrp(), "utc": datetime.now(timezone.utc).isoformat(), **value})))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(root / f"{name}.json")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _once(root: Path, name: str) -> bool:
    try:
        descriptor = os.open(root / f"{name}.once", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    os.close(descriptor)
    return True


def _barrier(root: Path, name: str, value: dict):
    if not _once(root, name):
        return
    _write(root, name, value)
    # 门闩由父进程明确释放，或由真实 host 终止整个执行组。
    deadline = time.monotonic() + 180
    while not (root / f"{name}.release").exists():
        if time.monotonic() >= deadline:
            raise RuntimeError(f"测试门闩未释放: {name}")
        time.sleep(0.01)


def _target(root: Path, connection):
    from camctl.contracts.enums import enum_for
    public = root / "photo-request.json"
    if not public.is_file():
        return None
    request_id = int(json.loads(public.read_text())["request_id"])
    rows = connection.execute(
        "SELECT p.id,a.id,a.device_id,a.driver_id,r.id,r.kind,r.activity_id,r.responsibility_key "
        "FROM plans p JOIN actions a ON a.plan_id=p.id JOIN operation_runs r ON r.action_id=a.id "
        "WHERE p.request_id=? AND a.name=? AND a.type=? AND r.kind=?",
        (request_id, "capture", int(enum_for("actions.type").CAMERA_TAKE_PHOTO), int(enum_for("operation_runs.kind").START)),
    ).fetchall()
    if not rows:
        return None
    assert len(rows) == 1, rows
    plan_id, action_id, device_id, driver_id, run_id, kind, activity_id, responsibility_key = rows[0]
    assert device_id == "hb-host" and driver_id == "hasselblad_cfv100c"
    assert responsibility_key == f"start/{action_id}" and activity_id is not None
    return {"request_id": str(request_id), "plan_id": plan_id, "action_id": action_id,
            "run_id": run_id, "activity_id": activity_id, "responsibility_key": responsibility_key,
            "run_kind": kind, "device_id": device_id, "driver_id": driver_id}


def _report_management_fact(connection, report_id, branch, event_id=None):
    from camctl.history.events import load_event_registry
    event = load_event_registry()["events"]["REPORT_CHANGED"]
    rows = connection.execute(
        "SELECT e.id,e.body_json,t.id,t.operation_key,t.first_event_id,t.last_event_id "
        "FROM history_events e JOIN history_transactions t ON t.id=e.transaction_id, "
        "json_each(e.body_json,'$.rows') changed "
        "WHERE e.event_type=? AND json_extract(e.body_json,'$.reason')=? AND json_extract(changed.value,'$.table')='reports' "
        "AND json_extract(changed.value,'$.id')=? "
        "AND (? IS NULL OR e.id=?) ORDER BY e.id DESC LIMIT 1",
        (event["id"], event["branches"][branch]["reason"], report_id, event_id, event_id)).fetchall()
    assert len(rows) == 1
    identity, body, txn_id, key, first, last = rows[0]
    assert first <= identity <= last
    return {"event_id": identity, "event_body": json.loads(body), "operation_key": key,
            "first_event_id": first, "boundary": {"txn_id": txn_id, "last_event_id": last}}


def _install_fault(root: Path, fault: str):
    from camctl.persistence.models import DbOutcome, DbOutcomeKind
    if fault in ("after-start-result", "before-start-result"):
        from camctl.persistence.repositories.capture import CaptureRepository
        real = CaptureRepository.finish_start_result

        def finish_start_result(self, finish, observation, key, owned, *, start_finish=None,
                   action_finish=None, expiration=None):
            target = _target(root, owned.connection)
            matches = target is not None and finish.ticket.responsibility_key == f"start/{target['action_id']}"
            if matches:
                assert finish.ticket.operation == "control"
                assert finish.ticket.run_id == target["run_id"]
                assert finish.ticket.target_id == str(target["activity_id"])
                value = {"ticket": asdict(finish.ticket), "operation_key": str(key), "target": target,
                         "finish": _plain(finish), "observation": _plain(observation),
                         "start_finish": _plain(start_finish), "action_finish": _plain(action_finish),
                         "expiration": _plain(expiration), "in_transaction": owned.connection.in_transaction}
                if fault == "before-start-result":
                    _barrier(root, fault, value)
            result = real(self, finish, observation, key, owned, start_finish=start_finish,
                          action_finish=action_finish, expiration=expiration)
            if matches and fault == "after-start-result" and result.kind is DbOutcomeKind.COMPLETED:
                assert not owned.connection.in_transaction
                attempt = owned.connection.execute(
                    "SELECT result_event_id FROM operation_attempts WHERE run_id=? AND attempt_no=?",
                    (finish.ticket.run_id, finish.ticket.attempt_id)).fetchone()
                saved = owned.connection.execute(
                    "SELECT t.id,t.last_event_id,t.operation_key FROM history_transactions t "
                    "JOIN history_events e ON e.transaction_id=t.id WHERE e.id=?", (attempt[0],)).fetchone()
                assert saved is not None and saved[2] == str(key)
                boundary = {"txn_id": saved[0], "last_event_id": saved[1]}
                _barrier(root, fault, {**value, "result_event_id": attempt[0],
                    "boundary": {key: boundary[key] for key in ("txn_id", "last_event_id")},
                    "in_transaction": owned.connection.in_transaction})
            return result
        CaptureRepository.finish_start_result = finish_start_result

        if fault == "before-start-result":
            from camctl.capture.handlers import CaptureRuntime
            real_recovery = CaptureRuntime.recover_attempt

            async def recover(runtime, ticket, *, retry_wait=False):
                target = await runtime.database.read(
                    lambda owned: _target(root, owned.connection), description="观察原拍摄责任")
                matches = target is not None and ticket.operation == "control" and ticket.run_id == target["run_id"]
                observed = None
                if matches and not (root / "start-recovery-observed.json").exists():
                    def read_facts(owned):
                        attempt = owned.connection.execute(
                            "SELECT intent_event_id,status FROM operation_attempts WHERE run_id=? AND attempt_no=?",
                            (ticket.run_id, ticket.attempt_id)).fetchone()
                        maximum = owned.connection.execute("SELECT MAX(id) FROM history_events").fetchone()[0]
                        return attempt, maximum
                    attempt, maximum = await runtime.database.read(read_facts, description="观察原恢复依据")
                    local = runtime.start_entries.get(runtime.database.metadata.instance_id, target["action_id"])
                    observed = {"target":target,"ticket":asdict(ticket),
                        "boundary":runtime.recovery_boundary.name,"horizon":runtime.recovery_max_event_id,
                        "intent_event_id":attempt[0],"attempt_status_before":attempt[1],
                        "max_event_before":maximum,"local_entry_active":local is not None and local.active}
                result = await real_recovery(runtime, ticket, retry_wait=retry_wait)
                if observed is not None:
                    diagnostic = runtime.last_recovery_diagnostic
                    _write(root, "start-recovery-observed", {**observed,"returned":result,
                        "diagnostic":None if diagnostic is None else diagnostic.reason.name})
                return result
            CaptureRuntime.recover_attempt = recover

    elif fault == "complete-receipt-loss":
        from camctl.persistence.repositories.host_capture import HostCaptureRepository
        real_save = HostCaptureRepository.save_host_photo_complete
        real_verify = HostCaptureRepository.verify_operation
        write_connections = {}

        def save(self, request, key, owned):
            result = real_save(self, request, key, owned)
            target = _target(root, owned.connection)
            if (target is not None and request.action_id == target["action_id"]
                    and result.kind is DbOutcomeKind.COMPLETED and _once(root, "complete-commit")):
                assert not owned.connection.in_transaction
                write_connections[str(key)] = owned.connection
                _write(root, "complete-commit", {
                    "request": asdict(request), "operation_key": str(key), "target": target,
                    "boundary": asdict(result.boundary), "in_transaction": owned.connection.in_transaction,
                    "write_connection": id(owned.connection), "returned_receipt": "unknown",
                })
                return DbOutcome(DbOutcomeKind.UNKNOWN, error=RuntimeError("H8b 完整 COMMIT 回执丢失一次"))
            return result

        def verify(self, request, key, owned):
            query_only, = owned.connection.execute("PRAGMA query_only").fetchone()
            if str(key) not in write_connections:
                return real_verify(self, request, key, owned)
            original = write_connections[str(key)]
            assert owned.connection is not original and query_only == 1
            result = real_verify(self, request, key, owned)
            _write(root, "complete-verifier", {
                "request": asdict(request), "operation_key": str(key),
                "boundary": asdict(result.boundary) if result.boundary else None,
                "write_connection": id(original), "verifier_connection": id(owned.connection),
                "query_only": query_only, "returned_receipt": result.kind.value,
            })
            return result
        HostCaptureRepository.save_host_photo_complete = save
        HostCaptureRepository.verify_operation = verify
    elif fault == "report-before-publish":
        from camctl.persistence.repositories.history import HistoryRepository
        from camctl.reporting.publication import DbPublicationSession
        real = DbPublicationSession.record_intent

        def intent(self, report_id):
            result = real(self, report_id)
            if result.kind is not DbOutcomeKind.COMPLETED or (root / f"{fault}.once").exists():
                return result
            assert not self._owned.connection.in_transaction
            path = self._owned.connection.execute("PRAGMA database_list").fetchone()[2]
            target = _target(root, self._owned.connection)
            if target is None:
                return result
            history = HistoryRepository(Path(path))
            frozen = history.frozen_registration(report_id)
            restored = history.restore_entity("action", target["action_id"], frozen.boundary)
            action = restored.get(("actions", target["action_id"]))
            from camctl.contracts.enums import enum_for
            output_ids = [row[0] for row in self._owned.connection.execute(
                "SELECT id FROM outputs WHERE source_action_id=? ORDER BY kind", (target["action_id"],))]
            outputs = [history.restore_entity("output", identity, frozen.boundary).get(("outputs", identity))
                       for identity in output_ids]
            outputs = [row for row in outputs if row is not None and row["source_action_id"] == target["action_id"]]
            if action and action["status"] == int(enum_for("actions.status").SUCCEEDED) and len(outputs) == 2:
                _barrier(root, fault, {"report_id": report_id, "target": target, "boundary": asdict(frozen.boundary),
                                      "from_wm": frozen.from_wm, "to_wm": frozen.to_wm,
                                      "management": _report_management_fact(self._owned.connection, report_id, "INTENT"),
                                      "in_transaction": self._owned.connection.in_transaction})
            return result
        real_save_publication = DbPublicationSession.save_publication

        def published(self, report_id, file_result):
            result = real_save_publication(self, report_id, file_result)
            checkpoint = root / "report-before-publish.json"
            if result.kind is DbOutcomeKind.COMPLETED and checkpoint.is_file():
                original = json.loads(checkpoint.read_text())
                if report_id == original["report_id"]:
                    assert not self._owned.connection.in_transaction
                    _barrier(root, "report-rebuilt-published", {
                        "report_id": report_id, "returned_receipt": result.kind.value,
                        "target": _target(root, self._owned.connection),
                        "file_result": _plain(file_result), "publication": _plain(result.value),
                        "management": _report_management_fact(self._owned.connection, report_id, "PUBLISH", result.value.published_event_id),
                        "in_transaction": self._owned.connection.in_transaction,
                    })
            return result
        DbPublicationSession.record_intent = intent
        DbPublicationSession.save_publication = published
    else:
        raise ValueError(f"未知 H8b 故障选择: {fault}")


def main() -> int:
    root = Path(os.environ["CAMCTL_HB_TEST_ROOT"])
    python = Path(os.environ["CAMCTL_HB_TEST_INSTALLED_PYTHON"])
    assert sys.executable == str(python), (sys.executable, python)
    assert Path(camctl.__file__).is_relative_to(python.parent.parent)
    if sys.argv[1:2] == ["--h8b-history"]:
        return _history_command(sys.argv[2:])
    with (root / "processes.jsonl").open("a") as stream:
        stream.write(json.dumps({
            "kind": "camctl", "argv": sys.argv, "python": sys.executable,
            "camctl": camctl.__file__, "cwd": os.getcwd(), "pid": os.getpid(),
            "ppid": os.getppid(), "pgid": os.getpgrp(),
            "fault_environment": {
                "present": "CAMCTL_HB_TEST_FAULT" in os.environ,
                "value": os.environ.get("CAMCTL_HB_TEST_FAULT"),
            },
            "utc": datetime.now(timezone.utc).isoformat(),
        }) + "\n")
    fault = os.environ.get("CAMCTL_HB_TEST_FAULT")
    if fault:
        _install_fault(root, fault)
    from camctl.cli import main as cli_main
    return cli_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())

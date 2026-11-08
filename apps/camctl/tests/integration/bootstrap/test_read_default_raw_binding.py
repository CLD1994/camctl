"""普通与残留入口先保存真实完整READ，再按必要摘要的绑定错误收场。"""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from camctl.capture.media_flow import run_recording_media
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.devices.bindings import check_binding
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.operations import OperationRepository

from ..capture.media_retry_fixtures import media_pipeline as pipeline  # noqa: F401
from ..capture.test_input_copy import _CONTENT
from ..support.bootstrap_consumers import consumer_world
from .test_read_default_consumers import _snapshot
from .test_read_held_end_required_digest import _held_without_digest

pytestmark = pytest.mark.asyncio


def _binding_port(change):
    devices = {} if change == "missing" else {"cam-1": {
        "kind": "camera", "driver": "camctl-adb" if change == "matched" else "alternate-camera"}}
    return lambda saved: check_binding(saved, SimpleNamespace(devices=devices))


async def _raw_world(pipeline, capture_resources, monkeypatch):
    """真实源已交付完整End，实际主机摘要打开失败，尚未形成Finish。"""
    world = await consumer_world(pipeline, capture_resources)
    hashes = []
    original_open = os.open

    def unavailable(path, flags, *args, **kwargs):
        if isinstance(path, (str, Path)) and flags & os.O_ACCMODE == os.O_RDONLY:
            target = Path(path)
            if target.is_relative_to(world.roots.staging) and target.is_file():
                hashes.append(target)
                raise OSError("READ已实际结束，主机摘要文件暂不可读")
        return original_open(path, flags, *args, **kwargs)

    try:
        ended_at = world.wall[0]
        with monkeypatch.context() as fault:
            fault.setattr(os, "open", unavailable)
            with pytest.raises(ConsistencyError):
                await run_recording_media(world.runtime.media, 1, 1, world.source_id)
        ticket, progress = _held_without_digest(world.owned, world.runtime, world.reader)
        assert len(hashes) == len(world.reader.requests) == 1
        assert world.ends and all(end.stopped and end.error is None and end.bytes_read == len(_CONTENT)
                                  for end in world.ends)
        assert hashes[0].read_bytes() == _CONTENT
        assert world.driver.calls == [] and world.tools() == []
        action_id, processing_id = world.owned.connection.execute(
            "SELECT action_id,id FROM recording_processing WHERE source_device_file_id=?",
            (world.source_id,)).fetchone()
        assert world.owned.connection.execute(
            "SELECT COUNT(*) FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
            " WHERE r.action_id=? AND r.id!=? AND (t.status=1 OR t.result_json IS NULL)",
            (action_id, ticket.run_id)).fetchone() == (0,)
        copy_id = int(ticket.target_id)
        target_id, = world.owned.connection.execute(
            "SELECT target_file_id FROM file_copies WHERE id=?", (copy_id,)).fetchone()
        raw = SimpleNamespace(world=world, ticket=ticket, progress=progress, copy_id=copy_id,
            action_id=action_id, processing_id=processing_id, ended_at=ended_at, hashes=hashes,
            run=_snapshot(world.owned, "operation_runs", ticket.run_id),
            source=_snapshot(world.owned, "device_files", world.source_id), target_id=target_id,
            target=_snapshot(world.owned, "intermediate_files", target_id),
            history=world.owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall(),
            other_attempts=world.owned.connection.execute(
                "SELECT * FROM operation_attempts WHERE run_id!=? ORDER BY id", (ticket.run_id,)).fetchall())
        await world.reconnect()
        world.wall[0] += 1000
        world.prime_clock()
        return raw
    except BaseException:
        await world.close()
        world.owned.connection.close()
        raise


def _assert_raw_preserved(raw, *, released):
    world, owned = raw.world, raw.world.owned
    progress = owned.connection.execute(
        "SELECT committed_bytes,source_size,round,recopies_used,source_sha256,target_sha256,verification_state,slot_device_id"
        " FROM file_copies WHERE id=?", (raw.copy_id,)).fetchone()
    assert progress[:-1] == raw.progress[:-1]
    assert progress[-1] == (None if released else raw.progress[-1])
    run = _snapshot(owned, "operation_runs", raw.ticket.run_id)
    assert {**run, "status": raw.run["status"], "error_json": raw.run["error_json"]} == raw.run
    assert _snapshot(owned, "device_files", world.source_id) == raw.source
    assert _snapshot(owned, "intermediate_files", raw.target_id) == raw.target
    assert raw.hashes[0].read_bytes() == _CONTENT
    assert owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()[:len(raw.history)] == raw.history
    assert owned.connection.execute(
        "SELECT * FROM operation_attempts WHERE run_id!=? ORDER BY id", (raw.ticket.run_id,)).fetchall() == raw.other_attempts
    assert len(world.reader.requests) == len(raw.hashes) == 1
    assert world.driver.calls == [] and world.tools() == []


@pytest.mark.parametrize("pipeline", [True], indirect=True)
@pytest.mark.parametrize("entrance", ["normal", "residual"])
@pytest.mark.parametrize("change", ["missing", "mismatch"])
async def test_default_entry_preserves_distinct_source_observer_and_owner(
        pipeline, capture_resources, monkeypatch, entrance, change):
    owned, _roots, source_id = pipeline
    row = owned.connection.execute(
        "SELECT f.observer_action_id,f.source_action_id,p.action_id,"
        "ob.device_id,ob.driver_id,origin.device_id,origin.driver_id"
        " FROM device_files f JOIN recording_processing p ON p.source_device_file_id=f.id"
        " JOIN actions ob ON ob.id=f.observer_action_id JOIN actions origin ON origin.id=f.source_action_id"
        " WHERE f.id=?", (source_id,)).fetchone()
    assert row is not None and row[0] != row[1] and row[1] == row[2]
    assert row[3:5] == row[5:7]
    await _assert_default_binding(pipeline, capture_resources, monkeypatch, entrance, change)


@pytest.mark.parametrize("entrance", ["normal", "residual"])
@pytest.mark.parametrize("change", ["missing", "mismatch"])
async def test_default_entry_saves_clean_read_with_missing_required_sha_before_candidates(
        pipeline, capture_resources, monkeypatch, entrance, change):
    await _assert_default_binding(pipeline, capture_resources, monkeypatch, entrance, change)


async def _assert_default_binding(pipeline, capture_resources, monkeypatch, entrance, change):
    raw = await _raw_world(pipeline, capture_resources, monkeypatch)
    world, ticket = raw.world, raw.ticket
    saves = []
    actual_finish, actual_binding = OperationRepository.finish_binding_read, CaptureRepository.finish_binding_failure

    def finish(repository, request, key, current, *, current_identity=None):
        if request.ticket.operation == "read":
            saves.append(("finish", request, key))
        return actual_finish(repository, request, key, current, current_identity=current_identity)

    def binding(repository, request, key, current):
        saves.append(("binding", request, key))
        return actual_binding(repository, request, key, current)

    monkeypatch.setattr(OperationRepository, "finish_binding_read", finish)
    monkeypatch.setattr(CaptureRepository, "finish_binding_failure", binding)
    try:
        await world.replay(entrance, binding=_binding_port(change))
        owned = world.owned
        row = owned.connection.execute(
            "SELECT status,error_json,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id)).fetchone()
        assert row[:2] == (int(enum_for("operation_attempts.status").SUCCEEDED), None)
        assert json.loads(row[2])["settlement"] == {
            "basis": "observed", "evidence": {"type": "read_returned", "version": 1, "data": {}}}
        details = {"device_id": "cam-1", "expected_driver_id": "camctl-adb", "reason": change}
        if change == "mismatch":
            details["actual_driver_id"] = "alternate-camera"
        expected = {"code": "device_binding_unavailable", "stage": "execution", "details": details}
        run = _snapshot(owned, "operation_runs", ticket.run_id)
        assert run["status"] == int(enum_for("operation_runs.status").FAILED) and run["error_json"] == expected
        check, media, repair = owned.connection.execute(
            "SELECT check_state,media_json,repair_state FROM recording_processing WHERE id=?", (raw.processing_id,)).fetchone()
        assert check == int(enum_for("recording_processing.check_state").FAILED)
        assert json.loads(media)["error"] == expected
        assert repair == int(enum_for("recording_processing.repair_state").NOT_NEEDED)
        status, code, error = owned.connection.execute(
            "SELECT status,error_code,error_details_json FROM actions WHERE id=?", (raw.action_id,)).fetchone()
        assert (status, code) == (int(enum_for("actions.status").FAILED), 17)
        assert json.loads(error) == details
        assert [stage for stage, _request, _key in saves] == ["finish", "binding"]
        assert saves[0][1].ticket == ticket and saves[0][1].occurred_at == raw.ended_at
        assert saves[1][1].occurred_at == world.wall[0] and world.wall[0] > raw.ended_at
        assert saves[1][1].action_id == raw.action_id and saves[0][2] != saves[1][2]
        for _stage, _request, key in saves:
            assert owned.connection.execute(
                "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?", (str(key),)).fetchone() == (1,)
        _assert_raw_preserved(raw, released=True)
    finally:
        await world.close()
        world.owned.connection.close()


@pytest.mark.parametrize("entrance", ["normal", "residual"])
async def test_matched_binding_excludes_failure_path_and_keeps_read_open(pipeline, capture_resources, monkeypatch, entrance):
    raw = await _raw_world(pipeline, capture_resources, monkeypatch)
    world, ticket = raw.world, raw.ticket
    calls = []
    actual = CaptureRepository.finish_binding_failure

    def observe(repository, request, key, current):
        calls.append((request, key))
        return actual(repository, request, key, current)

    monkeypatch.setattr(CaptureRepository, "finish_binding_failure", observe)
    try:
        for _ in range(2):
            await world.replay(entrance, binding=_binding_port("matched"))
            assert world.owned.connection.execute(
                "SELECT status,error_json,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
                (ticket.run_id, ticket.attempt_id)).fetchone() == (1, None, None)
            assert world.owned.connection.execute(
                "SELECT status,error_json FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone() == (2, None)
            assert world.owned.connection.execute(
                "SELECT status,error_code FROM actions WHERE id=?", (raw.action_id,)).fetchone() == (2, None)
            assert calls == []
            _assert_raw_preserved(raw, released=False)
    finally:
        await world.close()
        world.owned.connection.close()

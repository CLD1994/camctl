"""普通／残留入口先保存真实完整 READ，再按必要摘要的绑定错误收场。"""

from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from camctl.bootstrap import capture_assembly, lifecycle
from camctl.capture.media_flow import run_recording_media
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.devices import bindings
from camctl.devices.drivers.registry import DriverRegistry
from camctl.persistence.repositories.acceptance import register_acceptance_guards
from camctl.persistence.repositories.capture import CaptureRepository, register_capture_guards
from camctl.persistence.repositories.operations import OperationRepository, register_operation_guards
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.persistence.repositories.scheduling import register_window_guard
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..capture.media_retry_fixtures import media_pipeline as pipeline  # noqa: F401
from ..capture.test_input_copy import _CONTENT
from .test_read_default_consumers import (
    _BeforeBusinessCandidates, _CandidateClock, _default_world, _snapshot,
)
from .test_read_held_end_required_digest import _held_without_digest, _hold_before_digest


pytestmark = pytest.mark.asyncio
register_acceptance_guards()
register_window_guard()
register_operation_guards()
register_capture_guards()
register_outputs_guards()


@pytest.mark.parametrize("pipeline", [True], indirect=True)
@pytest.mark.parametrize("entrance", ["normal", "residual"])
@pytest.mark.parametrize("change", ["missing", "mismatch"])
async def test_default_entry_preserves_distinct_source_observer_and_owner(
    pipeline, monkeypatch, entrance, change,
):
    owned, _roots, source_id = pipeline
    row = owned.connection.execute(
        "SELECT f.observer_action_id,f.source_action_id,p.action_id,"
        "ob.device_id,ob.driver_id,origin.device_id,origin.driver_id"
        " FROM device_files f JOIN recording_processing p ON p.source_device_file_id=f.id"
        " JOIN actions ob ON ob.id=f.observer_action_id JOIN actions origin ON origin.id=f.source_action_id"
        " WHERE f.id=?", (source_id,)).fetchone()
    assert row is not None and row[0] != row[1] and row[1] == row[2]
    assert row[3:5] == row[5:7]
    await test_default_entry_saves_clean_read_with_missing_required_sha_before_candidates(
        pipeline, monkeypatch, entrance, change)


class _ResidualReadBoundary:
    """真实 SQLite 透传；旧调用恢复查询前核实原 READ 已可靠保存。"""

    def __init__(self, connection, check):
        self.connection, self.check = connection, check
        self.hits = 0

    def execute(self, sql, parameters=()):
        if " ".join(sql.split()).startswith("SELECT t.id, t.attempt_no, r.id, r.kind"):
            self.hits += 1
            self.check()
            raise _BeforeBusinessCandidates
        return self.connection.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self.connection, name)


def _register_alternate_driver(monkeypatch):
    # 替代驱动有合法登记；配置与原 READ 参数保持固定，不重建第二套工厂。
    actual_factory = capture_assembly.session_capture_assembly

    def factory(**kwargs):
        original = kwargs["drivers"].entry("camctl-adb")
        return actual_factory(**{**kwargs, "drivers": DriverRegistry((
            original, replace(original, driver_id="alternate-camera")))})

    monkeypatch.setattr(capture_assembly, "session_capture_assembly", factory)


def _current_binding_fact(monkeypatch, change):
    """控制稳定绑定协作者的可用性；真正 check_binding 产生分类和详情。"""
    actual_check = bindings.check_binding
    fault_config = SimpleNamespace(devices={} if change == "missing" else {
        "cam-1": {"kind": "camera", "driver": "alternate-camera"}})
    checks = []

    def check(saved, current):
        if saved.device_id != "cam-1":
            return actual_check(saved, current)
        result = actual_check(saved, fault_config)
        checks.append(result)
        return result

    monkeypatch.setattr(bindings, "check_binding", check)
    monkeypatch.setattr(capture_assembly, "check_binding", check)
    return checks


@pytest.mark.parametrize("entrance", ["normal", "residual"])
@pytest.mark.parametrize("change", ["missing", "mismatch"])
async def test_default_entry_saves_clean_read_with_missing_required_sha_before_candidates(
    pipeline, monkeypatch, entrance, change,
):
    owned, roots, source_id = pipeline
    _register_alternate_driver(monkeypatch)
    world = await _default_world(pipeline, monkeypatch)
    deps, factories, context, reader, driver, ends, wall, factory_calls = world
    reopened = None
    try:
        # 原完整 End 由普通默认 factory 的真实任务取得，没有复制任何 READ 集合。
        action_id, processing_id = owned.connection.execute(
            "SELECT action_id,id FROM recording_processing WHERE source_device_file_id=?",
            (source_id,)).fetchone()
        runtime = factories[0](owned, "cam-1")
        flow = runtime.media
        assert flow is not None
        completions = _hold_before_digest(monkeypatch)
        with pytest.raises(ConsistencyError):
            await run_recording_media(flow, action_id, processing_id, source_id)
        ticket, before_progress = _held_without_digest(owned, flow, reader)
        copy_id = int(ticket.target_id)
        held = flow.pending_read_ends[copy_id]
        assert held.evidence is flow.evidence and held.resume is not None
        assert ends and all(end.stopped is True and end.error is None
                           and end.bytes_read == len(_CONTENT) for end in ends)
        assert held.end in ends and held.occurred_at == wall[0]
        assert flow.pending_read_results == flow.pending_read_business == {}
        assert len(completions) == 1 and driver.calls == []
        before_run = _snapshot(owned, "operation_runs", ticket.run_id)
        assert before_run["status"] == int(enum_for("operation_runs.status").ACTIVE)
        assert runtime.action(action_id)["status"] == int(enum_for("actions.status").RUNNING)
        assert runtime.action(action_id)["cancel_requested"] == 0
        # 除本次原 READ 外的全部实际调用都已可靠结束，不能用业务失败代替调用收场。
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
            " WHERE r.action_id=? AND r.id<>? AND (t.status=1 OR t.result_json IS NULL)",
            (action_id, ticket.run_id)).fetchone() == (0,)
        before_source = _snapshot(owned, "device_files", source_id)
        target_id, = owned.connection.execute(
            "SELECT target_file_id FROM file_copies WHERE id=?", (copy_id,)).fetchone()
        before_target = _snapshot(owned, "intermediate_files", target_id)
        target_path = roots.staging / before_target["relative_path"]
        assert target_path.read_bytes() == _CONTENT
        history = owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()
        original_attempts = owned.connection.execute(
            "SELECT * FROM operation_attempts WHERE run_id<>? ORDER BY id", (ticket.run_id,)).fetchall()

        owned.connection.close()
        reopened = open_existing(roots.staging.parent / "state.db", DbOpenMode.EXISTING_RW, DbConfig())
        assert reopened.metadata == owned.metadata
        # 新连接没有重建实际结果；原会话持有物和字节事实分别核实。
        assert reopened.connection.execute(
            "SELECT status,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id)).fetchone() == (1, None)
        assert flow.pending_read_ends[copy_id] == held
        wall[0] += 1000
        binding_checks = _current_binding_fact(monkeypatch, change)
        saves = []
        actual_finish, actual_binding = OperationRepository.finish_attempt, CaptureRepository.finish_binding_failure

        def finish(repository, request, key, current):
            if request.ticket.operation == "read":
                saves.append(("finish", request, key))
            return actual_finish(repository, request, key, current)

        def binding(repository, request, key, current):
            saves.append(("binding", request, key))
            return actual_binding(repository, request, key, current)

        monkeypatch.setattr(OperationRepository, "finish_attempt", finish)
        monkeypatch.setattr(CaptureRepository, "finish_binding_failure", binding)
        expected_details = {"device_id": "cam-1", "expected_driver_id": "camctl-adb", "reason": change}
        if change == "mismatch":
            expected_details["actual_driver_id"] = "alternate-camera"

        def saved_before_candidates():
            status, error, result = reopened.connection.execute(
                "SELECT status,error_json,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
                (ticket.run_id, ticket.attempt_id)).fetchone()
            assert status == int(enum_for("operation_attempts.status").SUCCEEDED), \
                "候选筛选前必须先保存原实际 clean READ，不能留待录像 handler"
            assert error is None
            assert json.loads(result)["settlement"] == {
                "basis": "observed", "evidence": {"type": "read_returned", "version": 1, "data": {}}}
            run = _snapshot(reopened, "operation_runs", ticket.run_id)
            assert run["status"] == int(enum_for("operation_runs.status").FAILED)
            assert run["error_json"] == {
                "code": "device_binding_unavailable", "stage": "execution", "details": expected_details}
            assert {**run, "status": before_run["status"], "error_json": before_run["error_json"]} == before_run
            state = reopened.connection.execute(
                "SELECT committed_bytes,source_size,round,recopies_used,source_sha256,target_sha256,verification_state,slot_device_id"
                " FROM file_copies WHERE id=?", (copy_id,)).fetchone()
            assert state[:-1] == before_progress[:-1] and state[-1] is None
            check, media, repair = reopened.connection.execute(
                "SELECT check_state,media_json,repair_state FROM recording_processing WHERE id=?", (processing_id,)).fetchone()
            assert check == int(enum_for("recording_processing.check_state").FAILED)
            assert json.loads(media)["error"] == {
                "code": "device_binding_unavailable", "stage": "execution", "details": expected_details}
            assert repair == int(enum_for("recording_processing.repair_state").NOT_NEEDED)
            action_status, error_code, error_details = reopened.connection.execute(
                "SELECT status,error_code,error_details_json FROM actions WHERE id=?", (action_id,)).fetchone()
            assert (action_status, error_code) == (int(enum_for("actions.status").FAILED), 17)
            assert json.loads(error_details) == expected_details
            assert [stage for stage, _request, _key in saves] == ["finish", "binding"]
            assert saves[0][1].ticket == ticket and saves[0][1].occurred_at > held.occurred_at
            assert saves[1][1].action_id == action_id and saves[0][2] != saves[1][2]
            for _stage, _request, key in saves:
                assert reopened.connection.execute(
                    "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?", (str(key),)).fetchone() == (1,)

        original_open, opened = context.open_connection, []

        def open_connection():
            current = original_open()
            assert current.metadata == reopened.metadata and current.connection is not reopened.connection
            opened.append(current)
            return replace(current, connection=_ResidualReadBoundary(current.connection, saved_before_candidates)) \
                if entrance == "residual" else current

        context.open_connection = open_connection
        if entrance == "normal":
            context.clock = _CandidateClock(saved_before_candidates)
        selected = context.flows["scheduling"] if entrance == "normal" else context.flows["residual"]
        with pytest.raises(_BeforeBusinessCandidates):
            await selected(context)
        saved_before_candidates()
        assert len(opened) == 1 and len(factory_calls) == 1 and binding_checks
        assert flow.pending_read_ends == flow.pending_read_results == flow.pending_read_business == {}
        assert len(reader.requests) == len(completions) == 1 and driver.calls == []
        assert _snapshot(reopened, "device_files", source_id) == before_source
        assert _snapshot(reopened, "intermediate_files", target_id) == before_target
        assert target_path.read_bytes() == _CONTENT
        assert reopened.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()[:len(history)] == history
        assert reopened.connection.execute(
            "SELECT * FROM operation_attempts WHERE run_id<>? ORDER BY id", (ticket.run_id,)).fetchall() == original_attempts
    finally:
        if reopened is not None:
            reopened.connection.close()
        lifecycle.close_runtime(deps)

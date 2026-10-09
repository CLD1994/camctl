"""默认入口形成的实际 READ／所属绑定失败申请跨 UNKNOWN 保存原身份。"""

from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from camctl.bootstrap import lifecycle
from camctl.capture.media_flow import run_recording_media
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import saved_transaction_events
from camctl.session.service import StateDbFailure

from ..capture.media_retry_fixtures import media_pipeline as pipeline  # noqa: F401
from ..capture.test_input_copy import _CONTENT
from .test_read_default_consumers import (
    _BeforeBusinessCandidates, _CandidateClock, _ReadCommitFailure, _default_world, _snapshot,
)
from .test_read_default_raw_binding import _ResidualReadBoundary, _current_binding_fact
from .test_read_held_end_required_digest import _held_without_digest, _hold_before_digest


pytestmark = pytest.mark.asyncio


def _fresh(state):
    if state.observer is not None:
        state.observer.connection.close()
    state.observer = open_existing(state.database, DbOpenMode.EXISTING_RW, DbConfig())
    return state.observer


async def _prepare_raw_unknown(pipeline, monkeypatch, entrance, stage, after_commit):
    """原 factory 取得 End；真实默认入口首次形成申请并命中实际 COMMIT。"""
    owned, roots, source_id = pipeline
    world = await _default_world(pipeline, monkeypatch)
    deps, factories, context, reader, driver, ends, wall, factory_calls = world
    state = SimpleNamespace(deps=deps, context=context, reader=reader, driver=driver,
        ends=ends, factory_calls=factory_calls, observer=None, database=roots.staging.parent / "state.db",
        finishes=[], children=[], returns=[], faults=[], prepared=[], opened=[], entrance=entrance)
    try:
        state.action_id, state.processing_id = owned.connection.execute(
            "SELECT action_id,id FROM recording_processing WHERE source_device_file_id=?", (source_id,)).fetchone()
        state.flow = factories[0](owned, "cam-1").media
        assert state.flow is not None
        state.completions = _hold_before_digest(monkeypatch)
        with pytest.raises(ConsistencyError):
            await run_recording_media(state.flow, state.action_id, state.processing_id, source_id)
        state.ticket, state.progress = _held_without_digest(owned, state.flow, reader)
        state.copy_id = int(state.ticket.target_id)
        state.held = state.flow.pending_read_ends[state.copy_id]
        assert state.held.end in ends and state.held.end.bytes_read == len(_CONTENT)
        assert state.held.end.stopped is True and state.held.end.error is None
        assert state.held.evidence is state.flow.evidence and state.held.resume is not None
        assert state.flow.pending_read_results == state.flow.pending_read_business == {}
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM operation_attempts t JOIN operation_runs r ON r.id=t.run_id"
            " WHERE r.action_id=? AND r.id!=? AND (t.status=1 OR t.result_json IS NULL)",
            (state.action_id, state.ticket.run_id)).fetchone() == (0,)
        state.run = _snapshot(owned, "operation_runs", state.ticket.run_id)
        state.source_id, state.source = source_id, _snapshot(owned, "device_files", source_id)
        state.target_id = owned.connection.execute(
            "SELECT target_file_id FROM file_copies WHERE id=?", (state.copy_id,)).fetchone()[0]
        state.target = _snapshot(owned, "intermediate_files", state.target_id)
        state.target_path = roots.staging / state.target["relative_path"]
        assert state.target_path.read_bytes() == _CONTENT
        state.history = owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()
        state.other_attempts = owned.connection.execute(
            "SELECT * FROM operation_attempts WHERE run_id!=? ORDER BY id", (state.ticket.run_id,)).fetchall()
        owned.connection.close()
        assert _fresh(state).metadata == owned.metadata
        state.bindings = _current_binding_fact(monkeypatch, "missing")
        state.time = [state.held.occurred_at + 1000]
        state.clock_reads = []

        def input_clock():
            state.clock_reads.append(state.time[0])
            return state.time[0]

        # 原输入形成钟和候选钟分别受控，T1 不能回填到 End 的 T0。
        monkeypatch.setattr(lifecycle, "SystemClock", lambda: SimpleNamespace(utc_micros=input_clock))
        actual_finish, actual_child = OperationRepository.finish_attempt, CaptureRepository.finish_binding_failure

        def submit(repository, request, key, current, operation, actual):
            inputs = state.finishes if operation == "finish" else state.children
            inputs.append((request, key))
            if operation == stage and len(inputs) == 1:
                fault = _ReadCommitFailure(current.connection, after_commit)
                state.faults.append(fault)
                result = actual(repository, request, key, replace(current, connection=fault))
                state.returns.append(result)
                assert result.kind is DbOutcomeKind.UNKNOWN, result
                assert fault.commit_calls == 1
                assert current.connection.in_transaction is not after_commit
                return result
            return actual(repository, request, key, current)

        def finish(repository, request, key, current):
            if request.ticket.operation != "read":
                return actual_finish(repository, request, key, current)
            pending = state.flow.pending_read_results[(state.ticket.run_id, state.ticket.attempt_id)]
            assert (pending.finish, pending.key) == (request, key)
            assert pending.business is not None, "第一次 Finish 前必须已经持有完整所属申请"
            if not state.prepared:
                state.prepared.append(pending)
            return submit(repository, request, key, current, "finish", actual_finish)

        def child(repository, request, key, current):
            original = state.prepared[0].business
            assert (request, key) == (original.request, original.key)
            assert saved_transaction_events(current.connection, state.prepared[0].key) is not None
            assert state.flow.pending_read_business[(state.action_id, "capture_binding_failure")] == original
            return submit(repository, request, key, current, "child", actual_child)

        monkeypatch.setattr(OperationRepository, "finish_attempt", finish)
        monkeypatch.setattr(CaptureRepository, "finish_binding_failure", child)
        state.original_open = context.open_connection

        def open_connection():
            current = state.original_open()
            assert current.connection is not state.observer.connection
            state.opened.append(current)
            return replace(current, connection=_ResidualReadBoundary(current.connection,
                lambda: pytest.fail("UNKNOWN 保存尚未可靠，不能查询残留候选"))) if entrance == "residual" else current

        context.open_connection = open_connection
        context.clock = _CandidateClock(lambda: pytest.fail("UNKNOWN 保存尚未可靠，不能进入候选钟"))
        state.selected = context.flows["scheduling"] if entrance == "normal" else context.flows["residual"]
        with pytest.raises(StateDbFailure):
            await state.selected(context)
        assert len(state.opened) == len(state.faults) == len(state.prepared) == 1
        assert state.faults[0].commit_calls == 1 and state.returns[0].kind is DbOutcomeKind.UNKNOWN
        pending = state.prepared[0]
        assert pending.finish.occurred_at == pending.business.request.occurred_at == state.time[0]
        assert state.clock_reads == [state.time[0]] and state.time[0] > state.held.occurred_at
        assert pending.finish.ticket == state.ticket and pending.key != pending.business.key
        assert state.copy_id not in state.flow.pending_read_ends
        assert len(state.finishes) == 1 and len(state.children) == (0 if stage == "finish" else 1)
        current = _fresh(state)
        assert (saved_transaction_events(current.connection, pending.key) is not None) is (
            stage == "child" or after_commit)
        assert (saved_transaction_events(current.connection, pending.business.key) is not None) is (
            stage == "child" and after_commit)
        assert current.connection.execute(
            "SELECT status FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (state.ticket.run_id, state.ticket.attempt_id)).fetchone() == (
                int(enum_for("operation_attempts.status").RUNNING)
                if stage == "finish" and not after_commit else int(enum_for("operation_attempts.status").SUCCEEDED),)
        assert current.connection.execute("SELECT status FROM actions WHERE id=?", (state.action_id,)).fetchone() == (
            int(enum_for("actions.status").FAILED) if stage == "child" and after_commit
            else int(enum_for("actions.status").RUNNING),)
        if stage == "finish":
            assert state.flow.pending_read_results[(state.ticket.run_id, state.ticket.attempt_id)] == pending
            assert state.flow.pending_read_business == {}
        else:
            assert state.flow.pending_read_results == {}
            assert state.flow.pending_read_business[(state.action_id, "capture_binding_failure")] == pending.business
        _assert_preserved(state, released=stage == "child" and after_commit)
        return state
    except BaseException:
        if state.observer is not None:
            state.observer.connection.close()
        lifecycle.close_runtime(deps)
        raise


def _assert_preserved(state, *, released):
    current = state.observer
    progress = current.connection.execute(
        "SELECT committed_bytes,source_size,round,recopies_used,source_sha256,target_sha256,verification_state,slot_device_id"
        " FROM file_copies WHERE id=?", (state.copy_id,)).fetchone()
    assert progress[:-1] == state.progress[:-1]
    assert progress[-1] == (None if released else state.progress[-1])
    run = _snapshot(current, "operation_runs", state.ticket.run_id)
    assert {**run, "status": state.run["status"], "error_json": state.run["error_json"]} == state.run
    assert _snapshot(current, "device_files", state.source_id) == state.source
    assert _snapshot(current, "intermediate_files", state.target_id) == state.target
    assert state.target_path.read_bytes() == _CONTENT
    assert current.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()[:len(state.history)] == state.history
    assert current.connection.execute(
        "SELECT * FROM operation_attempts WHERE run_id!=? ORDER BY id", (state.ticket.run_id,)).fetchall() == state.other_attempts
    assert len(state.reader.requests) == len(state.completions) == len(state.factory_calls) == 1
    assert state.driver.calls == []


@pytest.mark.parametrize("entrance", ["normal", "residual"])
@pytest.mark.parametrize("stage", ["finish", "child"])
@pytest.mark.parametrize("after_commit", [False, True])
async def test_default_raw_binding_unknown_retries_original_inputs(pipeline, monkeypatch, entrance, stage, after_commit):
    state = await _prepare_raw_unknown(pipeline, monkeypatch, entrance, stage, after_commit)
    try:
        pending = state.prepared[0]
        # 下一轮时刻改变、动作可能已终态，但完整申请核实优先，不能形成新 key/T1。
        state.time[0] += 5000

        def saved():
            current = state.observer
            for key in (pending.key, pending.business.key):
                assert current.connection.execute(
                    "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?", (str(key),)).fetchone() == (1,)
            assert len(state.finishes) == (2 if stage == "finish" else 1)
            assert all(pair == (pending.finish, pending.key) for pair in state.finishes)
            assert len(state.children) == (1 if stage == "finish" else 2)
            assert all(pair == (pending.business.request, pending.business.key) for pair in state.children)
            assert state.clock_reads == [pending.finish.occurred_at]
            status, error, result = current.connection.execute(
                "SELECT status,error_json,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
                (state.ticket.run_id, state.ticket.attempt_id)).fetchone()
            assert (status, error) == (int(enum_for("operation_attempts.status").SUCCEEDED), None)
            assert json.loads(result)["settlement"] == {
                "basis": "observed", "evidence": {"type": "read_returned", "version": 1, "data": {}}}
            details = {"device_id": "cam-1", "expected_driver_id": "camctl-adb", "reason": "missing"}
            assert _snapshot(current, "operation_runs", state.ticket.run_id)["error_json"] == {
                "code": "device_binding_unavailable", "stage": "execution", "details": details}
            assert current.connection.execute("SELECT status FROM actions WHERE id=?", (state.action_id,)).fetchone() == (
                int(enum_for("actions.status").FAILED),)
            check, media = current.connection.execute(
                "SELECT check_state,media_json FROM recording_processing WHERE id=?", (state.processing_id,)).fetchone()
            assert check == int(enum_for("recording_processing.check_state").FAILED)
            assert json.loads(media)["error"] == {
                "code": "device_binding_unavailable", "stage": "execution", "details": details}
            assert state.flow.pending_read_results == state.flow.pending_read_business == state.flow.pending_read_ends == {}
            _assert_preserved(state, released=True)

        def open_connection():
            current = state.original_open()
            assert current.connection is not state.observer.connection
            state.opened.append(current)
            return replace(current, connection=_ResidualReadBoundary(current.connection, saved)) \
                if entrance == "residual" else current

        state.context.open_connection = open_connection
        state.context.clock = _CandidateClock(saved)
        with pytest.raises(_BeforeBusinessCandidates):
            await state.selected(state.context)
        assert len(state.opened) == 2
        _fresh(state)
        saved()
    finally:
        state.observer.connection.close()
        lifecycle.close_runtime(state.deps)

"""完整文件已保存后，媒体完成沿原核实责任收场。"""

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import json

import pytest

from camctl.capture.handlers import capture_handler
from camctl.capture.media import RecordingFailure
from camctl.contracts.values import ConsistencyError
from camctl.host_files.media import MediaProbe
from camctl.host_files.tasks import FileTaskId, FileTaskResult
from camctl.persistence.repositories.outputs import register_outputs_guards
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.transaction import TransactionError
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from .media_retry_fixtures import _EVIDENCE, media_pipeline  # noqa: F401
from .test_capture_contract import ResultsDouble, _entry, _runtime
from .test_input_copy import _CONTENT, _NOW
from .test_media_flow import ReadDriverDouble, _flow
from ..bootstrap.test_binding_transactions import _CommitFailure

pytestmark = pytest.mark.asyncio
register_outputs_guards()


class _WaitingProbe:
    """第一次工具未执行，第二次返回实际时长观察。"""

    def __init__(self, duration_s):
        self.duration_s = duration_s
        self.calls = []

    async def probe(self, input):
        self.calls.append("probe")
        if len(self.calls) == 1:
            return FileTaskResult(task_id=FileTaskId("probe"), ran=False)
        return FileTaskResult(task_id=FileTaskId("probe"), ran=True,
            value=MediaProbe(duration_s=self.duration_s, error=None))

    async def repair(self, input, output, *, trim_s):
        self.calls.append("repair")
        return FileTaskResult(task_id=FileTaskId("repair"), ran=False)


def _attempts(owned):
    return owned.connection.execute(
        "SELECT * FROM operation_attempts ORDER BY id"
    ).fetchall()


def _result_run(owned):
    return owned.connection.execute(
        "SELECT id,status,attempts_used,retry_wait_required,max_attempts_used,"
        " timeout_s_json,retry_interval_s_json FROM operation_runs"
        " WHERE responsibility_key='results/1'"
    ).fetchone()


def _runtime_with_media(pipeline, duration_s):
    entry = replace(_entry("original-recording", size=len(_CONTENT)),
        locator={"path": "/DCIM/original.mp4"}, original_name="original.mp4")
    results = ResultsDouble({1: (entry,)})
    runtime = _runtime(pipeline[0], results=results, listing_cache={})
    runtime.evidence = _EVIDENCE
    reader, tools = ReadDriverDouble(_CONTENT), _WaitingProbe(duration_s)
    runtime.media = _flow(pipeline, reader, tools)
    return runtime, results, reader, tools


def _assert_joint_settlement(owned, run_id, action_status):
    transactions = {"run": set(), "action": set(), "output": set()}
    for transaction_id, body in owned.connection.execute(
            "SELECT transaction_id,body_json FROM history_events ORDER BY id"):
        for row in json.loads(body)["rows"]:
            values = row["after"]["values"]
            if row["table"] == "operation_runs" and row["id"] == run_id and values.get("status") == 3:
                transactions["run"].add(transaction_id)
            if row["table"] == "actions" and row["id"] == 1 and values.get("status") == action_status:
                transactions["action"].add(transaction_id)
            if row["table"] == "outputs" and values.get("source_action_id") == 1:
                transactions["output"].add(transaction_id)
    assert len(transactions["run"]) == 1
    assert transactions["run"] == transactions["action"] == transactions["output"]
    original = owned.connection.execute(
        "SELECT e.transaction_id FROM history_events e JOIN operation_attempts a"
        " ON a.result_event_id=e.id WHERE a.run_id=?", (run_id,)
    ).fetchone()[0]
    assert original not in transactions["run"]


@pytest.mark.parametrize("duration_s,action_status", [
    (Decimal("61"), 3), (Decimal("5"), 4),
], ids=["media-succeeded", "media-failed"])
@pytest.mark.parametrize("reopen", [False, True], ids=["same-runtime", "saved-history"])
async def test_media_completion_ends_original_results_without_new_observation(
        media_pipeline, duration_s, action_status, reopen):
    owned, roots, source_id = media_pipeline
    runtime, results, reader, tools = _runtime_with_media(media_pipeline, duration_s)

    await capture_handler("camera_record")(1, runtime)

    original_run = _result_run(owned)
    assert original_run[1:4] == (2, 1, 1)
    original_attempts = _attempts(owned)
    assert len(original_attempts) == 4  # START、STOP、RESULTS 和真实 READ。
    history = owned.connection.execute(
        "SELECT * FROM history_events ORDER BY id"
    ).fetchall()
    assert results.calls == [1]
    assert tools.calls == ["probe"]
    assert not runtime.pending_start_results
    assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (2,)
    assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)

    recovered = None
    try:
        if reopen:
            database_path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
            metadata = owned.metadata
            owned.connection.close()
            recovered = open_existing(database_path, DbOpenMode.EXISTING_RW, DbConfig())
            assert recovered.metadata == metadata
            owned = recovered
            runtime = _runtime(owned, results=results, listing_cache={})
            runtime.evidence = _EVIDENCE
            runtime.media = _flow((owned, roots, source_id), reader, tools)
        runtime.wall_us = lambda: _NOW + 200_000_000
        await capture_handler("camera_record")(1, runtime)

        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (action_status,)
        assert owned.connection.execute("SELECT device_file_id FROM outputs").fetchall() == [(source_id,)]
        assert results.calls == [1]
        assert len(reader.opens) == 1
        assert tools.calls == ["probe", "probe"]
        assert _attempts(owned) == original_attempts
        assert owned.connection.execute(
            "SELECT * FROM history_events WHERE id<=? ORDER BY id", (history[-1][0],)
        ).fetchall() == history
        current_run = _result_run(owned)
        assert current_run[0] == original_run[0]
        assert current_run[2] == original_run[2]
        assert current_run[4:] == original_run[4:]
        assert current_run[1:4] == (3, 1, 0)
        _assert_joint_settlement(owned, original_run[0], action_status)
    finally:
        if recovered is not None:
            recovered.connection.close()


class _TrackedCommitFailure(_CommitFailure):
    """只在复合仓储的真实 COMMIT 前后出错，并记录故障确实命中。"""

    def __init__(self, connection, after):
        super().__init__(connection, after)
        self.commit_calls = 0

    def execute(self, sql, parameters=()):
        if sql == "COMMIT":
            self.commit_calls += 1
        return super().execute(sql, parameters)


class _AtSettlementBoundary(Exception):
    """隔离仓储保存后的消费者步骤，让测试直接检查原子结果。"""


@pytest.mark.parametrize("duration_s", [Decimal("5"), Decimal("61")],
                         ids=["short-file-as-success", "long-file-as-failure"])
async def test_new_settlement_rejects_business_result_opposite_to_saved_media(
        media_pipeline, monkeypatch, duration_s):
    owned = media_pipeline[0]
    runtime, _results, _reader, _tools = _runtime_with_media(media_pipeline, duration_s)
    await capture_handler("camera_record")(1, runtime)
    original_save = CaptureRepository.finish_recording_results
    recorded = []

    def save(repository, request, key, save_owned):
        before = tuple(save_owned.connection.iterdump())
        contrary = (None if duration_s == Decimal("5") else
            RecordingFailure("recording_too_short", {"processing_id": "1"}))
        changed = replace(request, capture=replace(request.capture, failure=contrary))
        result = original_save(repository, changed, key, save_owned)
        recorded.append((before, result))
        raise _AtSettlementBoundary

    monkeypatch.setattr(CaptureRepository, "finish_recording_results", save)
    runtime.wall_us = lambda: _NOW + 200_000_000
    with pytest.raises(_AtSettlementBoundary):
        await capture_handler("camera_record")(1, runtime)
    assert len(recorded) == 1
    before, result = recorded[0]
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, (TransactionError, ConsistencyError))
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("member", [
    "run_id", "action_id", "occurred_at", "ownership", "file_complete", "sha256",
])
async def test_original_settlement_key_rejects_changed_request_member(media_pipeline, monkeypatch, member):
    owned = media_pipeline[0]
    runtime, results, reader, tools = _runtime_with_media(media_pipeline, Decimal("61"))
    await capture_handler("camera_record")(1, runtime)
    original_save = CaptureRepository.finish_recording_results
    inputs = []

    def save(repository, request, key, save_owned):
        inputs.append((request, key))
        return original_save(repository, request, key, save_owned)

    monkeypatch.setattr(CaptureRepository, "finish_recording_results", save)
    runtime.wall_us = lambda: _NOW + 200_000_000
    await capture_handler("camera_record")(1, runtime)
    assert len(inputs) == 1
    request, key = inputs[0]
    if member == "run_id":
        changed = replace(request, run_id=1)  # 实际 START，不能代替原 RESULTS。
    elif member == "action_id":
        changed = replace(request, capture=replace(request.capture, action_id=99))
    elif member == "occurred_at":
        changed = replace(request, capture=replace(request.capture, occurred_at=_NOW + 200_000_001))
    elif member == "ownership":
        changed = replace(request, capture=replace(request.capture,
            catalog_facts=replace(request.capture.catalog_facts, ownership_confirmed=False)))
    else:
        draft, = request.capture.drafts
        changed_draft = replace(draft, **({"file_complete": False} if member == "file_complete"
                                        else {"sha256": "a" * 64}))
        changed = replace(request, capture=replace(request.capture, drafts=(changed_draft,)))
    before = tuple(owned.connection.iterdump())
    result = original_save(CaptureRepository(), changed, key, owned)
    assert result.kind is DbOutcomeKind.ROLLED_BACK
    assert isinstance(result.error, (TransactionError, ConsistencyError))
    assert tuple(owned.connection.iterdump()) == before
    repeated = original_save(CaptureRepository(), request, key, owned)
    assert repeated.kind is DbOutcomeKind.COMPLETED, repeated.error
    assert tuple(owned.connection.iterdump()) == before
    assert results.calls == [1] and len(reader.opens) == 1 and tools.calls == ["probe", "probe"]


@pytest.mark.parametrize("duration_s,action_status", [
    (Decimal("61"), 3), (Decimal("5"), 4),
], ids=["media-succeeded", "media-failed"])
@pytest.mark.parametrize("after", [False, True], ids=["commit-before", "commit-after"])
async def test_unknown_settlement_reuses_whole_request_and_original_decision_time(
        media_pipeline, monkeypatch, duration_s, action_status, after):
    owned, roots, source_id = media_pipeline
    runtime, results, reader, tools = _runtime_with_media(media_pipeline, duration_s)
    await capture_handler("camera_record")(1, runtime)
    original_attempts = _attempts(owned)
    run_id = _result_run(owned)[0]
    original_save = CaptureRepository.finish_recording_results
    inputs, outcomes, proxies = [], [], []

    def save(repository, request, key, save_owned):
        inputs.append((request, key))
        if len(inputs) == 1:
            proxy = _TrackedCommitFailure(save_owned.connection, after)
            proxies.append(proxy)
            save_owned = replace(save_owned, connection=proxy)
        outcome = original_save(repository, request, key, save_owned)
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(CaptureRepository, "finish_recording_results", save)
    runtime.wall_us = lambda: _NOW + 200_000_000
    with pytest.raises((ConsistencyError, AssertionError)) as first_error:
        await capture_handler("camera_record")(1, runtime)
    assert len(inputs) == 1 and proxies[0].commit_calls == 1
    assert outcomes[0].kind is DbOutcomeKind.UNKNOWN
    original_request, original_key = inputs[0]
    assert original_request.run_id == run_id
    assert original_request.capture.occurred_at == _NOW + 200_000_000
    database_path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    metadata = owned.metadata
    owned.connection.close()
    reopened = open_existing(database_path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        assert reopened.metadata == metadata
        transaction = reopened.connection.execute(
            "SELECT id FROM history_transactions WHERE operation_key=?", (str(original_key),)
        ).fetchone()
        assert (transaction is not None) is after
        assert reopened.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (
            action_status if after else 2,)
        assert _result_run(reopened)[1:4] == ((3, 1, 0) if after else (2, 1, 1))
        assert _attempts(reopened) == original_attempts
        resumed = replace(runtime, owned=reopened,
            media=_flow((reopened, roots, source_id), reader, tools),
            wall_us=lambda: _NOW + 300_000_000)
        await capture_handler("camera_record")(1, resumed)

        assert inputs == [(original_request, original_key), (original_request, original_key)]
        assert outcomes[-1].kind is DbOutcomeKind.COMPLETED, outcomes[-1].error
        assert _result_run(reopened)[1:4] == (3, 1, 0)
        assert reopened.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (action_status,)
        assert reopened.connection.execute("SELECT device_file_id FROM outputs").fetchall() == [(source_id,)]
        _assert_joint_settlement(reopened, run_id, action_status)
        assert _attempts(reopened) == original_attempts
        assert results.calls == [1] and len(reader.opens) == 1
        assert tools.calls == ["probe", "probe"]
        saved_transaction = reopened.connection.execute(
            "SELECT id FROM history_transactions WHERE operation_key=?", (str(original_key),)
        ).fetchone()[0]
        assert reopened.connection.execute(
            "SELECT DISTINCT occurred_at FROM history_events WHERE transaction_id=?", (saved_transaction,)
        ).fetchall() == [(_NOW + 200_000_000,)]
        history = reopened.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()
        await capture_handler("camera_record")(1, resumed)
        assert reopened.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall() == history
        assert len(inputs) == 2 and tools.calls == ["probe", "probe"]
        assert isinstance(first_error.value, ConsistencyError)
    finally:
        reopened.connection.close()

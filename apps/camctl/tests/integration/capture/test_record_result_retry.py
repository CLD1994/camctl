"""录像控制完成后，必要文件沿原 RESULTS 责任有限核实。"""

import json
from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.capture.handlers import capture_handler
from camctl.capture.results import FileKind
from camctl.capture.models import ResultRunClose, ResultSetPhase, ResultSetSave
from camctl.contracts.values import new_operation_key
from camctl.operations.attempts import AttemptConfig
from camctl.persistence.models import DbOutcomeKind

from .result_consumer_fixtures import consumer_world
from .test_capture_contract import ResultsDouble, _entry, _PAGE_EVIDENCE
from .test_result_consumer_saves import _actual, _result_port

pytestmark = pytest.mark.asyncio

_UNFINISHED_FILES = [
    (),
    (_entry("clip", complete=False),),
    (_entry("auxiliary", kind=FileKind.OTHER),),
]
_FILE_PARTITIONS = ["empty", "incomplete-video", "missing-video-kind"]


def _result_run(owned, activity_id):
    return owned.connection.execute(
        "SELECT id,status,attempts_used,retry_wait_required FROM operation_runs"
        " WHERE responsibility_key=?", (f"results/{activity_id}",)
    ).fetchone()


def _attempts(owned, run_id):
    return owned.connection.execute(
        "SELECT id,attempt_no,status,effect_state,intent_event_id,result_event_id,"
        " result_json,error_json FROM operation_attempts WHERE run_id=?"
        " ORDER BY attempt_no", (run_id,)
    ).fetchall()


def _output_identities(owned, action_id):
    return tuple(
        json.loads(row[0])[2]
        for row in owned.connection.execute(
            "SELECT f.identity_key FROM outputs o JOIN device_files f"
            " ON f.id=o.device_file_id WHERE o.source_action_id=?"
            " ORDER BY f.identity_key", (action_id,)
        )
    )


@pytest.mark.parametrize("first_files", _UNFINISHED_FILES, ids=_FILE_PARTITIONS)
async def test_record_rechecks_unfinished_files_in_same_results_run(
    tmp_path, first_files,
):
    owned, runtime, action_id, handler = await consumer_world(tmp_path, "record")
    activity_id, = owned.connection.execute(
        "SELECT id FROM device_activities WHERE action_id=?", (action_id,)
    ).fetchone()
    clock = [65_000_000_000]
    runtime.monotonic_ns = lambda: clock[0]
    runtime.check_config = AttemptConfig(3, Decimal("1.25"), Decimal("3"))
    runtime.results = ResultsDouble({activity_id: first_files}, set_finalized=False)
    runtime.evidence = _PAGE_EVIDENCE
    advance = capture_handler(handler)
    try:
        await advance(action_id, runtime)

        first_run = _result_run(owned, activity_id)
        assert first_run[1:] == (2, 1, 1)
        assert runtime.action(action_id)["status"] == 2
        original = _attempts(owned, first_run[0])
        assert len(original) == 1
        assert original[0][1:4] == (1, 2, 3)
        assert original[0][-1] is None
        original_observation = json.loads(original[0][-2])["observations"][0]
        assert original_observation["version"] == 2
        assert set(original_observation["data"]) == {"activity_id", "entries", "cursor",
            "next_cursor", "set_finalized", "completion_evidence"}
        assert original_observation["data"]["set_finalized"] is False
        assert owned.connection.execute(
            "SELECT activity_state,occupancy_state,result_check_json"
            " FROM device_activities WHERE id=?", (activity_id,)
        ).fetchone() == (3, 2, None)

        runtime.results.files_by_action[activity_id] = (_entry("clip"),)
        runtime.results.set_finalized = True
        clock[0] += 2_999_999_999
        await advance(action_id, runtime)
        assert runtime.results.calls == [activity_id]
        assert _result_run(owned, activity_id) == first_run
        assert _attempts(owned, first_run[0]) == original

        clock[0] += 1
        await advance(action_id, runtime)

        assert runtime.action(action_id)["status"] == 3
        assert _result_run(owned, activity_id) == (first_run[0], 3, 2, 0)
        assert runtime.results.calls == [activity_id, activity_id]
        assert _attempts(owned, first_run[0])[:1] == original
        assert _output_identities(owned, action_id) == (
            ("auxiliary", "clip")
            if first_files and first_files[0].kind is FileKind.OTHER
            else ("clip",)
        )
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM operation_runs WHERE responsibility_key=?",
            (f"results/{activity_id}",)
        ).fetchone() == (1,)
        runtime.driver.control.assert_awaited_once()
        runtime.stopper.stop.assert_awaited_once()

        await advance(action_id, runtime)
        assert runtime.results.calls == [activity_id, activity_id]
    finally:
        owned.connection.close()


@pytest.mark.parametrize("files", _UNFINISHED_FILES, ids=_FILE_PARTITIONS)
@pytest.mark.parametrize("independent_activity", [False, True], ids=["same-id", "independent-activity"])
async def test_record_result_budget_preserves_complete_files(tmp_path, files, independent_activity):
    owned, runtime, action_id, handler = await consumer_world(
        tmp_path, "record", independent_activity=independent_activity)
    activity_id, = owned.connection.execute(
        "SELECT id FROM device_activities WHERE action_id=?", (action_id,)
    ).fetchone()
    runtime.check_config = AttemptConfig(2, Decimal("1.25"), Decimal(0))
    runtime.results = ResultsDouble({activity_id: files})
    advance = capture_handler(handler)
    try:
        await advance(action_id, runtime)
        await advance(action_id, runtime)

        run_id = _result_run(owned, activity_id)[0]
        assert _result_run(owned, activity_id) == (run_id, 2, 2, 1)
        original = _attempts(owned, run_id)
        assert len(original) == 2
        assert [attempt[1:4] for attempt in original] == [(1, 2, 3), (2, 2, 3)]

        await advance(action_id, runtime)

        assert runtime.action(action_id)["status"] == 4
        assert _result_run(owned, activity_id) == (run_id, 6, 2, 0)
        assert runtime.results.calls == [activity_id, activity_id]
        assert _attempts(owned, run_id) == original
        error_code, error_details = owned.connection.execute(
            "SELECT error_code,error_details_json FROM actions WHERE id=?",
            (action_id,)
        ).fetchone()
        assert error_code == 12
        assert json.loads(error_details) == {
            "activity_id": str(activity_id), "reason": "outputs_unknown",
        }
        run_error, = owned.connection.execute(
            "SELECT error_json FROM operation_runs WHERE id=?", (run_id,)
        ).fetchone()
        assert json.loads(run_error)["details"] == json.loads(error_details)
        kept = ("auxiliary",) if files and files[0].kind is FileKind.OTHER else ()
        assert _output_identities(owned, action_id) == kept
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM device_files WHERE source_action_id=?"
            " AND completion_state=3", (action_id,)
        ).fetchone() == (len(kept),)
        assert owned.connection.execute(
            "SELECT result_check_json FROM device_activities WHERE id=?",
            (activity_id,)
        ).fetchone() == (None,)
        runtime.driver.control.assert_awaited_once()
        runtime.stopper.stop.assert_awaited_once()

        await advance(action_id, runtime)
        assert runtime.results.calls == [activity_id, activity_id]
    finally:
        owned.connection.close()


@pytest.mark.parametrize("with_video", [False, True], ids=["no-observation", "complete-video"])
async def test_record_later_failed_result_preserves_original_files_and_error(
    tmp_path, with_video,
):
    owned, runtime, action_id, handler = await consumer_world(tmp_path, "record")
    activity_id, = owned.connection.execute(
        "SELECT id FROM device_activities WHERE action_id=?", (action_id,)
    ).fetchone()
    runtime.check_config = AttemptConfig(2, Decimal("1.25"), Decimal(0))
    runtime.results = ResultsDouble({
        activity_id: (_entry("auxiliary", kind=FileKind.OTHER),),
    })
    advance = capture_handler(handler)
    try:
        await advance(action_id, runtime)
        first_run = _result_run(owned, activity_id)
        assert first_run[1:] == (2, 1, 1)
        first = _attempts(owned, first_run[0])
        assert len(first) == 1

        actual = _actual(activity_id, with_files=with_video, complete=True)
        driver = _result_port(runtime, actual)
        await advance(action_id, runtime)
        second = _attempts(owned, first_run[0])
        assert len(second) == 2
        assert second[:1] == first
        assert second[1][1:3] == (2, 3)
        assert second[1][3] == (3 if with_video else 1)
        failed_result = json.loads(second[1][-2])
        assert failed_result["settlement"] == {
            "basis": "assumed",
            "evidence": {"type": "adb_foreground_assumption", "version": 1,
                         "data": {"terminate_grace_s": 0.25}},
        }
        assert failed_result["call_info"] == {"local_exit": {"exit_code": 7}}
        assert failed_result["observations"] == [
            {"type": observation.type, "version": observation.version,
             "data": dict(observation.data)}
            for observation in actual.observations
        ]
        assert json.loads(second[1][-1]) == {
            "code": "transport_timeout", "stage": "transport",
            "details": {"received_bytes": 23},
        }

        # 实际读取错误与完整单文件均保留；v1 没有集合保证，预算
        # 耗尽按无法确认结束，不能把后来出现的视频当成完整集合。
        assert runtime.action(action_id)["status"] == 2
        assert _result_run(owned, activity_id) == (first_run[0], 2, 2, 1)
        await advance(action_id, runtime)

        assert runtime.action(action_id)["status"] == 4
        assert _result_run(owned, activity_id) == (first_run[0], 6, 2, 0)
        assert _output_identities(owned, action_id) == (
            ("auxiliary", "original") if with_video else ("auxiliary",)
        )
        assert _attempts(owned, first_run[0]) == second
        driver.list_results.assert_awaited_once()
        request, _batch = driver.list_results.call_args.args
        assert request.ticket.run_id == first_run[0]
        assert request.ticket.target_id == str(activity_id)
        assert request.timeout_s == Decimal("1.25")
        runtime.driver.control.assert_awaited_once()
        runtime.stopper.stop.assert_awaited_once()

        await advance(action_id, runtime)
        driver.list_results.assert_awaited_once()
    finally:
        owned.connection.close()


@pytest.mark.parametrize("consumer", ["photo", "timelapse", "record"])
@pytest.mark.parametrize("independent_activity", [False, True])
async def test_result_budget_close_uses_original_activity_on_resend(
    tmp_path, consumer, independent_activity,
):
    from camctl.capture.handlers import _finish_listing_result, _listing_round

    owned, runtime, action_id, _ = await consumer_world(
        tmp_path, consumer, independent_activity=independent_activity)
    activity_id, = owned.connection.execute(
        "SELECT id FROM device_activities WHERE action_id=?", (action_id,)
    ).fetchone()
    runtime.check_config = AttemptConfig(1, Decimal("1.25"), Decimal(0))
    runtime.results = ResultsDouble({activity_id: ()})
    try:
        listing = await _listing_round(runtime, action_id)
        _finish_listing_result(runtime, listing, retry_wait=True)
        if consumer == "record":
            command = ResultRunClose(action_id, listing.occurred_at)
            save = runtime.capture.close_unconfirmed_result_run
        else:
            error = {"code": "capture_result_unconfirmed", "stage": "execution",
                     "details": {"activity_id": str(activity_id), "reason": "outputs_unknown"}}
            command = ResultSetSave(
                action_id, listing.occurred_at, ResultSetPhase.UNCONFIRMED,
                contract="task_scope_files", observation={"reason": "attempts_exhausted"},
                capture={"status": "unconfirmed", "error": error}, error=error)
            save = runtime.capture.close_result_check_unconfirmed
        key = new_operation_key()
        first = save(command, key, owned)
        assert first.kind is DbOutcomeKind.COMPLETED, first.error
        run_id = _result_run(owned, activity_id)[0]
        assert _result_run(owned, activity_id) == (run_id, 6, 1, 0)
        error, = owned.connection.execute(
            "SELECT error_json FROM operation_runs WHERE id=?", (run_id,)
        ).fetchone()
        assert json.loads(error)["details"]["activity_id"] == str(activity_id)
        history = owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()
        attempts = _attempts(owned, run_id)

        assert save(command, key, owned).kind is DbOutcomeKind.COMPLETED
        assert save(replace(command, occurred_at=command.occurred_at + 1), key, owned).kind is DbOutcomeKind.ROLLED_BACK
        assert owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall() == history
        assert _attempts(owned, run_id) == attempts
        assert runtime.results.calls == [activity_id]
    finally:
        owned.connection.close()

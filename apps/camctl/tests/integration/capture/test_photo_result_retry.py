"""照片完成响应与文件齐备分别满足，核实沿原有限责任推进。"""

from dataclasses import replace
from decimal import Decimal

import pytest

from camctl.capture.handlers import capture_handler
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.capture.results import FileKind
from camctl.operations.attempts import AttemptConfig

from .result_consumer_fixtures import result_driver
from ..support.result_cases import result_case
from .test_capture_contract import ResultsDouble, _entry
from .test_result_consumer_saves import _actual

pytestmark = pytest.mark.asyncio


def _result_run(owned, activity_id):
    return owned.connection.execute(
        "SELECT id,status,attempts_used,retry_wait_required FROM operation_runs"
        " WHERE responsibility_key=?", (f"results/{activity_id}",)).fetchone()


@pytest.mark.parametrize("first_files", [
    (),
    (_entry("shot", kind=FileKind.PHOTO, complete=False),),
    (_entry("auxiliary", kind=FileKind.OTHER),),
], ids=["empty", "incomplete-photo", "missing-photo-kind"])
async def test_photo_rechecks_unfinished_files_in_same_results_run(tmp_path, result_case, first_files):
    owned, runtime, action_id, handler = await result_case.world(tmp_path, "photo")
    activity_id, = owned.connection.execute(
        "SELECT id FROM device_activities WHERE action_id=?", (action_id,)).fetchone()
    results = ResultsDouble({activity_id: first_files}, set_finalized=False)
    result_case.results.delegate = results
    advance = capture_handler(handler)
    try:
        await advance(action_id, runtime)
        first_run = _result_run(owned, activity_id)
        assert first_run[1:] == (2, 1, 1)
        assert (await runtime.action(action_id))["status"] == 2
        original = owned.connection.execute(
            "SELECT result_event_id,result_json FROM operation_attempts WHERE run_id=?",
            (first_run[0],)).fetchone()

        results.files_by_action[activity_id] = (_entry("shot", kind=FileKind.PHOTO),)
        results.set_finalized = True
        await advance(action_id, runtime)
        assert results.calls == [activity_id]
        assert _result_run(owned, activity_id) == first_run

        result_case.clock.monotonic += 2_000_000_000
        await advance(action_id, runtime)

        assert (await runtime.action(action_id))["status"] == 3
        assert _result_run(owned, activity_id) == (first_run[0], 3, 2, 0)
        assert results.calls == [activity_id, activity_id]
        assert owned.connection.execute(
            "SELECT result_event_id,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=1",
            (first_run[0],)).fetchone() == original
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM outputs WHERE source_action_id=?", (action_id,)).fetchone() == (
                2 if first_files and first_files[0].kind is FileKind.OTHER else 1,)
        await advance(action_id, runtime)
        assert results.calls == [activity_id, activity_id]
    finally:
        owned.connection.close()


@pytest.mark.parametrize("kept_file", [False, True])
async def test_photo_result_budget_ends_without_losing_complete_files(tmp_path, result_case, kept_file):
    owned, runtime, action_id, handler = await result_case.world(tmp_path, "photo",
        assembly_options={"check_config": AttemptConfig(2, Decimal("1.25"), Decimal(0))})
    activity_id, = owned.connection.execute(
        "SELECT id FROM device_activities WHERE action_id=?", (action_id,)).fetchone()
    results = ResultsDouble({activity_id: (
        _entry("auxiliary", kind=FileKind.OTHER),) if kept_file else ()}, set_finalized=False)
    result_case.results.delegate = results
    advance = capture_handler(handler)
    try:
        await advance(action_id, runtime)
        await advance(action_id, runtime)
        run_id, = owned.connection.execute(
            "SELECT id FROM operation_runs WHERE responsibility_key=?", (f"results/{activity_id}",)).fetchone()
        assert _result_run(owned, activity_id) == (run_id, 2, 2, 1)

        await advance(action_id, runtime)

        assert (await runtime.action(action_id))["status"] == 4
        assert _result_run(owned, activity_id) == (run_id, 6, 2, 0)
        assert results.calls == [activity_id, activity_id]
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM outputs WHERE source_action_id=?", (action_id,)).fetchone() == (int(kept_file),)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM device_files WHERE source_action_id=? AND completion_state=3",
            (action_id,)).fetchone() == (int(kept_file),)
        await advance(action_id, runtime)
        assert results.calls == [activity_id, activity_id]
    finally:
        owned.connection.close()


@pytest.mark.parametrize("kept_file", [False, True])
async def test_photo_failed_result_budget_preserves_actual_errors(tmp_path, result_case, kept_file):
    owned, runtime, action_id, handler = await result_case.world(tmp_path, "photo",
        assembly_options={"check_config": AttemptConfig(2, Decimal("1.25"), Decimal(0))})
    activity_id, = owned.connection.execute(
        "SELECT id FROM device_activities WHERE action_id=?", (action_id,)).fetchone()
    actual = _actual(activity_id, with_files=False)
    advance = capture_handler(handler)
    try:
        if kept_file:
            result_case.results.delegate = ResultsDouble({activity_id: (_entry("auxiliary", kind=FileKind.OTHER),)}, set_finalized=False)
            await advance(action_id, runtime)
        driver = result_driver(result_case, actual)
        await advance(action_id, runtime)
        if not kept_file:
            await advance(action_id, runtime)
        original = owned.connection.execute(
            "SELECT t.result_event_id,t.result_json,t.error_json FROM operation_attempts t"
            " JOIN operation_runs r ON r.id=t.run_id WHERE r.responsibility_key=? ORDER BY attempt_no",
            (f"results/{activity_id}",)).fetchall()
        assert len(original) == 2

        await advance(action_id, runtime)

        assert (await runtime.action(action_id))["status"] == 4
        assert _result_run(owned, activity_id)[1:] == (6, 2, 0)
        assert driver.list_results.await_count == (1 if kept_file else 2)
        assert owned.connection.execute(
            "SELECT t.result_event_id,t.result_json,t.error_json FROM operation_attempts t"
            " JOIN operation_runs r ON r.id=t.run_id WHERE r.responsibility_key=? ORDER BY attempt_no",
            (f"results/{activity_id}",)).fetchall() == original
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM outputs WHERE source_action_id=?", (action_id,)).fetchone() == (int(kept_file),)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("kept_file", [False, True])
async def test_photo_unconfirmed_conclusion_finishes_without_new_round(tmp_path, result_case, monkeypatch, kept_file):
    owned, runtime, action_id, handler = await result_case.world(tmp_path, "photo",
        assembly_options={"check_config": AttemptConfig(2, Decimal("1.25"), Decimal(0))})
    activity_id, = owned.connection.execute(
        "SELECT id FROM device_activities WHERE action_id=?", (action_id,)).fetchone()
    results = ResultsDouble({activity_id: (
        _entry("auxiliary", kind=FileKind.OTHER),) if kept_file else ()}, set_finalized=False)
    result_case.results.delegate = results
    advance = capture_handler(handler)
    try:
        await advance(action_id, runtime)
        await advance(action_id, runtime)
        save = CaptureRepository.close_result_check_unconfirmed
        class ClosedBeforeBusiness(Exception):
            pass
        def stop_after_save(repository, request, key, owned):
            receipt = save(repository, request, key, owned)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            return replace(receipt, handoff_error=ClosedBeforeBusiness())
        with monkeypatch.context() as patch:
            patch.setattr(CaptureRepository, "close_result_check_unconfirmed", stop_after_save)
            with pytest.raises(ClosedBeforeBusiness):
                await advance(action_id, runtime)
        original = _result_run(owned, activity_id)
        assert original[1:] == (6, 2, 0)
        assert (await runtime.action(action_id))["status"] == 2

        await advance(action_id, runtime)

        assert (await runtime.action(action_id))["status"] == 4
        assert _result_run(owned, activity_id) == original
        assert results.calls == [activity_id, activity_id]
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM outputs WHERE source_action_id=?", (action_id,)).fetchone() == (int(kept_file),)
    finally:
        owned.connection.close()

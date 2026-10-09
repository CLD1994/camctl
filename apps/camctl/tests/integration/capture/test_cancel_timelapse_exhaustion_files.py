"""取消延时摄影耗尽核实后，完整原文件与取消终态共同登记。"""

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import CommandMode
from camctl.cancellation.models import (
    ApplyCancelTarget, CancelApplyMode, CancellationEffect, FixedCancelSet,
    FixedTarget, FixCancelTargets, SelectionBasis, StartCancelAction,
)
from camctl.capture.handlers import _stop_call, capture_handler
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.contracts.workflow_errors import registered_error
from camctl.devices.evidence import DeviceObservation
from camctl.devices.ports import DeviceCallResult, StopDriver
from camctl.operations.attempts import AttemptConfig
from camctl.operations.models import (
    AttemptStatus, CallOutcome, EffectState, EvidenceValue, Settlement, SettlementBasis,
)
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository, ProcessInput
from camctl.persistence.repositories.cancellation import CancellationRepository
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import saved_transaction_events

from .result_consumer_fixtures import ResultCatalog, consumer_world, returned
from .test_record_media_result_settlement import _TrackedCommitFailure
from .test_result_consumer_saves import _actual, _result_port


pytestmark = pytest.mark.asyncio


class _ClosedBeforeBusiness(Exception):
    """真实耗尽结论已经提交，在依赖取消收场之前截停。"""


def _apply_public_cancel(owned, action_id, occurred_at):
    instant = datetime.fromtimestamp(occurred_at // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    accepted = AcceptanceRepository().process_input(ProcessInput(ParsedInput("cancel-timelapse.json", {
        "request_id": "2", "created_at": instant, "name": "取消延时摄影",
        "actions": [{"name": "取消", "type": "cancel_task", "params": {
            "target": {"action_instance_id": str(action_id)}}}],
    }), ResultCatalog(), CommandMode.RUN, occurred_at), new_operation_key(), owned)
    assert accepted.kind is DbOutcomeKind.COMPLETED, accepted.error
    cancel_id, = owned.connection.execute("SELECT id FROM actions WHERE type=6").fetchone()
    repository = CancellationRepository()
    started = repository.start_cancel_action(StartCancelAction(cancel_id, occurred_at),
        new_operation_key(), owned)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    fixed = repository.fix_cancel_targets(FixCancelTargets(cancel_id, FixedCancelSet((FixedTarget(
        action_id, SelectionBasis.DIRECT, CancellationEffect.NOT_APPLIED),)), occurred_at),
        new_operation_key(), owned)
    assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
    item_id, = fixed.value.item_ids
    applied = repository.apply_cancel_target(ApplyCancelTarget(
        item_id, CancelApplyMode.WITH_STOP, occurred_at), new_operation_key(), owned)
    assert applied.kind is DbOutcomeKind.COMPLETED, applied.error
    assert owned.connection.execute(
        "SELECT status,cancel_requested FROM actions WHERE id=?", (action_id,)).fetchone() == (2, 1)


async def _canceled_exhausted_world(tmp_path, *, kind="photo", complete=True, empty=False,
                                  later_failed=False):
    owned, runtime, action_id, handler = await consumer_world(
        tmp_path, "timelapse", independent_activity=True)
    try:
        activity_id, = owned.connection.execute(
            "SELECT id FROM device_activities WHERE action_id=?", (action_id,)).fetchone()
        assert action_id != activity_id
        name, media_type = {"photo": ("retained-photo.jpg", "image/jpeg"),
                            "video": ("retained-video.mp4", "video/mp4")}[kind]
        entry = {"identity": f"retained-{kind}", "kind": kind, "complete": complete,
            "size_bytes": 41 if complete else None, "locator": {"path": f"/DCIM/{name}"},
            "original_name": name, "media_type": media_type}
        entries = [] if empty else [entry]
        actual = CallOutcome(status=AttemptStatus.SUCCEEDED, effect=EffectState.CONFIRMED,
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("results_returned", 1, {})),
            observations=(DeviceObservation("result_files_listed", 1, {
                "activity_id": str(activity_id), "entries": entries}),))
        driver = _result_port(runtime, actual)
        used = 2 if later_failed else 1
        runtime.check_config = AttemptConfig(used, Decimal("1.25"), Decimal(0))
        await capture_handler(handler)(action_id, runtime)
        run_id, status, used, retry = owned.connection.execute(
            "SELECT id,status,attempts_used,retry_wait_required FROM operation_runs"
            " WHERE responsibility_key=?", (f"results/{activity_id}",)).fetchone()
        assert (status, used, retry) == (2, 1, 1)
        raw, = owned.connection.execute(
            "SELECT result_json FROM operation_attempts WHERE run_id=?", (run_id,)).fetchone()
        assert json.loads(raw) == {"format_version": 1,
            "settlement": {"basis": "observed", "evidence": {"type": "results_returned", "version": 1, "data": {}}},
            "observations": [{"type": "result_files_listed", "version": 1,
                "data": {"activity_id": str(activity_id), "entries": entries}}]}
        rows = owned.connection.execute(
            "SELECT id,source_action_id,presence_state,completion_state,size_bytes FROM device_files").fetchall()
        if empty:
            assert rows == []
            file_id = None
        else:
            (file_id, source, presence, completion, size), = rows
            assert (source, presence) == (action_id, 2)
            if complete:
                assert (completion, size) == (3, 41)
            else:
                assert completion != 3 and size is None
        if later_failed:
            latest = _actual(activity_id, with_files=False)
            driver.list_results.return_value = DeviceCallResult.from_outcome(latest)
            await capture_handler(handler)(action_id, runtime)
            assert owned.connection.execute(
                "SELECT status,attempts_used,retry_wait_required FROM operation_runs WHERE id=?",
                (run_id,)).fetchone() == (2, 2, 1)
            result_status, result_json, error_json = owned.connection.execute(
                "SELECT status,result_json,error_json FROM operation_attempts WHERE run_id=?"
                " ORDER BY attempt_no DESC LIMIT 1", (run_id,)).fetchone()
            assert result_status == 3 and json.loads(result_json)["observations"] == []
            assert json.loads(error_json) == {"code": "transport_timeout", "stage": "transport",
                                             "details": {"received_bytes": 23}}
        used = 2 if later_failed else 1
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
        canceled_at = runtime.wall_us() + 10
        _apply_public_cancel(owned, action_id, canceled_at)
        runtime.wall_us = lambda: canceled_at + 10
        stopper = create_autospec(StopDriver, instance=True)
        stopper.stop.return_value = DeviceCallResult.from_outcome(returned("stop", "stop_confirmed", activity_id))
        runtime.stopper = stopper
        step = await _stop_call(runtime, runtime.action(action_id), "stop_timelapse")
        assert step.phase == "confirmed", step
        stopper.stop.assert_awaited_once()
        return SimpleNamespace(owned=owned, runtime=runtime, action_id=action_id,
            activity_id=activity_id, run_id=run_id, file_id=file_id, driver=driver,
            stopper=stopper, handler=handler, used=used,
            expected_outputs=[] if empty or not complete else [(1, file_id, name, media_type)])
    except BaseException:
        owned.connection.close()
        raise


async def _exercise_exhaustion(tmp_path, monkeypatch, partition, **file_case):
    world = await _canceled_exhausted_world(tmp_path, **file_case)
    owned, runtime, action_id = world.owned, world.runtime, world.action_id
    reopened = None
    try:
        attempts = owned.connection.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall()
        files = owned.connection.execute("SELECT * FROM device_files ORDER BY id").fetchall()
        history = owned.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()
        original_close = CaptureRepository.close_result_check_unconfirmed
        closes, outcomes, faults = [], [], []
        formed_at = runtime.wall_us()
        original_map = runtime.pending_result_closes

        def close(repository, request, key, current):
            held = runtime.pending_result_closes[action_id]
            assert (held.request, held.key) == (request, key)
            closes.append((request, key))
            if len(closes) == 1 and partition in ("pre_unknown", "post_unknown"):
                fault = _TrackedCommitFailure(current.connection, partition == "post_unknown")
                faults.append(fault)
                current = replace(current, connection=fault)
            receipt = original_close(repository, request, key, current)
            outcomes.append(receipt)
            if partition == "noholder_reopen":
                assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
                raise _ClosedBeforeBusiness
            return receipt

        monkeypatch.setattr(CaptureRepository, "close_result_check_unconfirmed", close)
        advance = capture_handler(world.handler)
        if partition == "direct":
            await advance(action_id, runtime)
        else:
            expected_exception = _ClosedBeforeBusiness if partition == "noholder_reopen" else ConsistencyError
            with pytest.raises(expected_exception):
                await advance(action_id, runtime)
            assert len(closes) == 1
            request, key = closes[0]
            if partition != "noholder_reopen":
                assert len(faults) == 1 and faults[0].commit_calls == 1
                assert outcomes[0].kind is DbOutcomeKind.UNKNOWN
                assert (original_map[action_id].request, original_map[action_id].key) == (request, key)
            assert owned.connection.execute(
                "SELECT status FROM actions WHERE id=?", (action_id,)).fetchone() == (2,)
            assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
            path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
            metadata = owned.metadata
            owned.connection.close()
            reopened = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
            assert reopened.metadata == metadata
            already_saved = partition in ("post_unknown", "noholder_reopen")
            assert (saved_transaction_events(reopened.connection, key) is not None) is already_saved
            assert reopened.connection.execute(
                "SELECT status,attempts_used,retry_wait_required FROM operation_runs WHERE id=?",
                (world.run_id,)).fetchone() == ((6, world.used, 0) if already_saved else (2, world.used, 1))
            assert reopened.connection.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall() == attempts
            assert reopened.connection.execute("SELECT * FROM device_files ORDER BY id").fetchall() == files
            # UNKNOWN 仍共享原 holder；只有可靠 close 的重启分区没有会话持有物。
            runtime = replace(runtime, owned=reopened, wall_us=lambda: formed_at + 5_000_000,
                pending_result_closes={} if partition == "noholder_reopen" else original_map)
            await advance(action_id, runtime)
        current = reopened if reopened is not None else owned
        assert len(closes) == (2 if partition in ("pre_unknown", "post_unknown") else 1)
        request, key = closes[0]
        assert all(saved_input == (request, key) for saved_input in closes)
        assert outcomes[-1].kind is DbOutcomeKind.COMPLETED
        assert not runtime.pending_result_closes
        assert request.action_id == action_id and request.occurred_at == formed_at
        expected_error = {"code": "capture_result_unconfirmed",
            "stage": registered_error("capture_result_unconfirmed")["stage"],
            "details": {"activity_id": str(world.activity_id), "reason": "outputs_unknown"}}
        assert request.contract == "task_scope_files" and request.observation == {"reason": "attempts_exhausted"}
        assert request.capture == {"status": "unconfirmed", "error": expected_error}
        assert request.error == expected_error
        assert saved_transaction_events(current.connection, key) is not None
        assert current.connection.execute(
            "SELECT status,cancel_requested FROM actions WHERE id=?", (action_id,)).fetchone() == (6, 1)
        assert current.connection.execute(
            "SELECT status,attempts_used,retry_wait_required FROM operation_runs WHERE id=?",
            (world.run_id,)).fetchone() == (6, world.used, 0)
        assert current.connection.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall() == attempts
        assert current.connection.execute("SELECT * FROM device_files ORDER BY id").fetchall() == files
        assert current.connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()[:len(history)] == history
        assert world.driver.list_results.await_count == world.used
        runtime.driver.control.assert_awaited_once()
        world.stopper.stop.assert_awaited_once()
        outputs = current.connection.execute(
            "SELECT kind,device_file_id,original_name,media_type FROM outputs WHERE source_action_id=?",
            (action_id,)).fetchall()
        assert outputs == world.expected_outputs, (
            "集合无法确认仍须登记已经完成且可靠归属的原文件")
        transactions = current.connection.execute(
            "SELECT oe.transaction_id,ae.transaction_id FROM outputs o"
            " JOIN history_events oe ON oe.id=o.created_event_id"
            " JOIN actions a ON a.id=o.source_action_id"
            " JOIN history_events ae ON ae.id=a.last_event_id WHERE o.source_action_id=?",
            (action_id,)).fetchall()
        assert len(transactions) == len(world.expected_outputs)
        assert all(output_transaction == action_transaction for output_transaction, action_transaction in transactions)
        assert current.connection.execute(
            "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?", (str(key),)).fetchone() == (1,)
        before = tuple(current.connection.iterdump())
        await advance(action_id, runtime)
        assert tuple(current.connection.iterdump()) == before
        assert world.driver.list_results.await_count == world.used
        world.stopper.stop.assert_awaited_once()
    finally:
        owned.connection.close()
        if reopened is not None:
            reopened.connection.close()


@pytest.mark.parametrize("kind", ["photo", "video"])
@pytest.mark.parametrize("partition", ["direct", "pre_unknown", "post_unknown", "noholder_reopen"])
async def test_canceled_timelapse_keeps_complete_file_when_results_are_unconfirmed(
        tmp_path, monkeypatch, partition, kind):
    await _exercise_exhaustion(tmp_path, monkeypatch, partition, kind=kind)


@pytest.mark.parametrize("partition", ["direct", "noholder_reopen"])
@pytest.mark.parametrize("file_case", ["empty", "incomplete"])
async def test_canceled_timelapse_does_not_publish_unqualified_files(
        tmp_path, monkeypatch, partition, file_case):
    await _exercise_exhaustion(tmp_path, monkeypatch, partition,
        empty=file_case == "empty", complete=file_case != "incomplete")


@pytest.mark.parametrize("partition", ["direct", "noholder_reopen"])
async def test_canceled_timelapse_keeps_prior_file_after_latest_failed_without_observation(
        tmp_path, monkeypatch, partition):
    await _exercise_exhaustion(tmp_path, monkeypatch, partition, kind="video", later_failed=True)

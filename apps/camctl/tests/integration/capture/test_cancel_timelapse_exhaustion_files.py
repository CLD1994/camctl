"""取消延时摄影耗尽核实后，原可靠文件与取消终态共同登记。"""

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import json

import pytest

from camctl.acceptance.input import ParsedInput
from camctl.acceptance.service import CommandMode
from camctl.cancellation.models import (
    ApplyCancelTarget, CancelApplyMode, CancellationEffect, FixedCancelSet,
    FixedTarget, FixCancelTargets, SelectionBasis, StartCancelAction,
)
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.contracts.workflow_errors import registered_error
from camctl.devices.evidence import DeviceObservation
from camctl.devices.ports import DeviceCallResult
from camctl.operations.models import AttemptStatus, CallOutcome, EffectState, EvidenceValue, Settlement, SettlementBasis
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.acceptance import AcceptanceRepository, ProcessInput
from camctl.persistence.repositories.cancellation import CancellationRepository
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.transaction import saved_transaction_events

from .result_consumer_fixtures import ResultCatalog
from .test_result_consumer_saves import _actual
from ..support.capture_cancellation_cases import cancellation_world
from ..support.bootstrap_consumers import TransactionFault

pytestmark = pytest.mark.asyncio


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



class SavedBoundary(Exception):
    """正式事务已经提交；专项在随后业务之前交付观察。"""


async def _canceled_exhausted_world(tmp_path, capture_resources, monkeypatch, *, kind="photo", complete=True, empty=False, later_failed=False):
    world = await cancellation_world(tmp_path, capture_resources, finite=False, canceled=False,
        independent_activity=True, check_attempts=2 if later_failed else 1,
        check_timeout_s=Decimal('1.25'), check_retry_s=Decimal('0'))
    assert world.action_id != world.activity_id
    name, media = {"photo": ("retained-photo.jpg", "image/jpeg"), "video": ("retained-video.mp4", "video/mp4")}[kind]
    entry = {"identity": f"retained-{kind}", "kind": kind, "complete": complete,
        "size_bytes": 41 if complete else None, "locator": {"path": f"/DCIM/{name}"},
        "original_name": name, "media_type": media}
    entries = [] if empty else [entry]
    async def read(request, batch):
        assert request.timeout_s == Decimal('1.25')
        assert world.owned.connection.execute(
            "SELECT max_attempts_used,timeout_s_json,retry_interval_s_json FROM operation_runs WHERE responsibility_key=?",
            (f"results/{world.activity_id}",)).fetchone() == (2 if later_failed else 1, '1.25', '0')
        if later_failed and len(world.ports.pages) > 1:
            return DeviceCallResult.from_outcome(_actual(world.activity_id, with_files=False))
        return DeviceCallResult.from_outcome(CallOutcome(status=AttemptStatus.SUCCEEDED,
            effect=EffectState.CONFIRMED, settlement=Settlement(SettlementBasis.OBSERVED,
                EvidenceValue("results_returned", 1, {})), observations=(DeviceObservation(
                    "result_files_listed", 1, {"activity_id": str(world.activity_id), "entries": entries}),)))
    world.ports.result_script = read
    world.elapse(700_000_000)
    await world.advance()
    run_id, status, used, retry = world.owned.connection.execute(
        "SELECT id,status,attempts_used,retry_wait_required FROM operation_runs WHERE responsibility_key=?",
        (f"results/{world.activity_id}",)).fetchone()
    assert (status, used, retry) == (2, 1, 1)
    original = world.owned.connection.execute("SELECT result_json FROM operation_attempts WHERE run_id=?", (run_id,)).fetchone()[0]
    assert json.loads(original) == {"format_version": 1,
        "settlement": {"basis": "observed", "evidence": {"type": "results_returned", "version": 1, "data": {}}},
        "observations": [{"type": "result_files_listed", "version": 1,
            "data": {"activity_id": str(world.activity_id), "entries": entries}}]}
    rows = world.owned.connection.execute("SELECT id,source_action_id,presence_state,completion_state,size_bytes FROM device_files").fetchall()
    if empty:
        assert rows == []
        world.file_id = None
    else:
        (world.file_id, source, presence, completion, size), = rows
        assert (source, presence) == (world.action_id, 2)
        assert (completion, size) == (3, 41) if complete else completion != 3 and size is None
    if later_failed:
        await world.advance()
        status, raw, error = world.owned.connection.execute(
            "SELECT status,result_json,error_json FROM operation_attempts WHERE run_id=? ORDER BY attempt_no DESC LIMIT 1", (run_id,)).fetchone()
        assert status == 3 and json.loads(raw)["observations"] == []
        assert json.loads(error) == {"code": "transport_timeout", "stage": "transport", "details": {"received_bytes": 23}}
    assert world.owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
    world.elapse(10)
    await world.cancel()
    world.elapse(10)
    async def stop(request):
        return DeviceCallResult((DeviceObservation("stop_confirmed", 1, {"activity_id": str(world.activity_id)}),), None)
    world.ports.stop_script = stop
    original_stop = CaptureRepository.finish_capture_stop
    def saved(repository, finish, key, owned):
        receipt = original_stop(repository, finish, key, owned)
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        return replace(receipt, handoff_error=SavedBoundary())
    with monkeypatch.context() as boundary:
        boundary.setattr(CaptureRepository, "finish_capture_stop", saved)
        with pytest.raises(SavedBoundary):
            await world.advance()
    assert len(world.ports.stops) == 1
    world.run_id, world.used = run_id, 2 if later_failed else 1
    world.expected_outputs = [] if empty or not complete else [(1, world.file_id, name, media)]
    return world


async def _exercise_exhaustion(tmp_path, capture_resources, monkeypatch, partition, **file_case):
    world = await _canceled_exhausted_world(tmp_path, capture_resources, monkeypatch, **file_case)
    try:
        connection = world.owned.connection
        attempts = connection.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall()
        files = connection.execute("SELECT * FROM device_files ORDER BY id").fetchall()
        history = connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()
        original_close = CaptureRepository.close_result_check_unconfirmed
        closes, outcomes, faults = [], [], []
        formed_at = world.wall[0]
        def close(repository, request, key, owned):
            closes.append((request, key))
            if len(closes) == 1 and partition in ("pre_unknown", "post_unknown"):
                fault = TransactionFault(owned.connection, after_commit=partition == "post_unknown")
                faults.append(fault)
                owned = replace(owned, connection=fault)
            receipt = original_close(repository, request, key, owned)
            outcomes.append(receipt)
            if partition == "noholder_reopen":
                assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
                return replace(receipt, handoff_error=SavedBoundary())
            return receipt
        monkeypatch.setattr(CaptureRepository, "close_result_check_unconfirmed", close)
        if partition == "direct":
            await world.advance()
        else:
            with pytest.raises(SavedBoundary if partition == "noholder_reopen" else ConsistencyError):
                await world.advance()
            assert len(closes) == 1
            request, key = closes[0]
            if partition != "noholder_reopen":
                assert len(faults) == 1 and faults[0].commit_calls == 1
                assert outcomes[0].kind is DbOutcomeKind.UNKNOWN
            assert world.owned.connection.execute("SELECT status FROM actions WHERE id=?", (world.action_id,)).fetchone() == (2,)
            assert world.owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
            already_saved = partition in ("post_unknown", "noholder_reopen")
            world.elapse(5_000_000)
            await world.reopen(cold=partition == "noholder_reopen")
            connection = world.owned.connection
            assert (saved_transaction_events(connection, key) is not None) is already_saved
            assert connection.execute("SELECT status,attempts_used,retry_wait_required FROM operation_runs WHERE id=?", (world.run_id,)).fetchone() == ((6, world.used, 0) if already_saved else (2, world.used, 1))
            assert connection.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall() == attempts
            assert connection.execute("SELECT * FROM device_files ORDER BY id").fetchall() == files
            await world.advance()
        connection = world.owned.connection
        assert len(closes) == (2 if partition in ("pre_unknown", "post_unknown") else 1)
        request, key = closes[0]
        assert all(pair == (request, key) for pair in closes)
        assert outcomes[-1].kind is DbOutcomeKind.COMPLETED
        assert request.action_id == world.action_id and request.occurred_at == formed_at
        error = {"code": "capture_result_unconfirmed", "stage": registered_error("capture_result_unconfirmed")["stage"],
            "details": {"activity_id": str(world.activity_id), "reason": "outputs_unknown"}}
        assert request.contract == "task_scope_files" and request.observation == {"reason": "attempts_exhausted"}
        assert request.capture == {"status": "unconfirmed", "error": error} and request.error == error
        assert saved_transaction_events(connection, key) is not None
        assert connection.execute("SELECT status,cancel_requested FROM actions WHERE id=?", (world.action_id,)).fetchone() == (6, 1)
        assert connection.execute("SELECT status,attempts_used,retry_wait_required FROM operation_runs WHERE id=?", (world.run_id,)).fetchone() == (6, world.used, 0)
        assert connection.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall() == attempts
        assert connection.execute("SELECT * FROM device_files ORDER BY id").fetchall() == files
        assert connection.execute("SELECT * FROM history_events ORDER BY id").fetchall()[:len(history)] == history
        assert len(world.ports.pages) == world.used
        assert len(world.ports.controls) == len(world.ports.stops) == 1
        outputs = connection.execute("SELECT kind,device_file_id,original_name,media_type FROM outputs WHERE source_action_id=?", (world.action_id,)).fetchall()
        assert outputs == world.expected_outputs
        transactions = connection.execute("SELECT oe.transaction_id,ae.transaction_id FROM outputs o JOIN history_events oe ON oe.id=o.created_event_id JOIN actions a ON a.id=o.source_action_id JOIN history_events ae ON ae.id=a.last_event_id WHERE o.source_action_id=?", (world.action_id,)).fetchall()
        assert len(transactions) == len(world.expected_outputs)
        assert all(first == last for first, last in transactions)
        assert connection.execute("SELECT COUNT(*) FROM history_transactions WHERE operation_key=?", (str(key),)).fetchone() == (1,)
        before = tuple(connection.iterdump())
        await world.advance()
        assert tuple(connection.iterdump()) == before
        assert len(world.ports.pages) == world.used and len(world.ports.stops) == 1
    finally:
        await world.close()


@pytest.mark.parametrize("kind", ["photo", "video"])
@pytest.mark.parametrize("partition", ["direct", "pre_unknown", "post_unknown", "noholder_reopen"])
async def test_canceled_timelapse_keeps_complete_file_when_results_are_unconfirmed(
        tmp_path, capture_resources, monkeypatch, partition, kind):
    await _exercise_exhaustion(tmp_path, capture_resources, monkeypatch, partition, kind=kind)


@pytest.mark.parametrize("partition", ["direct", "noholder_reopen"])
@pytest.mark.parametrize("file_case", ["empty", "incomplete"])
async def test_canceled_timelapse_does_not_publish_unqualified_files(
        tmp_path, capture_resources, monkeypatch, partition, file_case):
    await _exercise_exhaustion(tmp_path, capture_resources, monkeypatch, partition,
        empty=file_case == "empty", complete=file_case != "incomplete")


@pytest.mark.parametrize("partition", ["direct", "noholder_reopen"])
async def test_canceled_timelapse_keeps_prior_file_after_latest_failed_without_observation(
        tmp_path, capture_resources, monkeypatch, partition):
    await _exercise_exhaustion(tmp_path, capture_resources, monkeypatch, partition, kind="video", later_failed=True)

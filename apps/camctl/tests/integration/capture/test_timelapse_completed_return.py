"""已完成返回沿原 START 保存结束，文件与集合仍分别核实。"""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from camctl.capture.handlers import capture_handler
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.ports import DeviceCallResult
from camctl.devices.tasks import StartReturn
from camctl.operations.models import AttemptStatus, EffectState, ErrorValue
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.contracts.values import ConsistencyError

from .result_consumer_fixtures import RESULT_EVIDENCE, consumer_world, returned
from .test_capture_contract import _NOW
from .test_result_device_completion import DeviceCompletionCatalog, _EVIDENCE, device_pages

pytestmark = pytest.mark.asyncio

_CONTROL_COMPLETION = EvidenceContract("native_capture_returned", 1, "control",
    frozenset({"activity_id"}), identity_field="activity_id")


class CompletedCatalog(DeviceCompletionCatalog):
    def parameter_definition(self, device_id, action_type, parameter_type):
        definition = super().parameter_definition(device_id, action_type, parameter_type)
        if action_type != "camera_timelapse":
            return definition
        return replace(definition, task_factory=lambda params: replace(
            definition.task_factory(params), start_return_meaning=StartReturn.COMPLETED))


async def world(tmp_path, *, case="video", catalog=None):
    owned, runtime, action_id, handler = await consumer_world(tmp_path, "timelapse",
        catalog=CompletedCatalog() if catalog is None else catalog, start=False)
    driver = device_pages(runtime, completion_page=None, case=case)
    runtime.evidence = EvidenceRegistry((
        RESULT_EVIDENCE.contract("operation_returned", 1),
        _CONTROL_COMPLETION,
        *(_EVIDENCE.contract(name, version) for name, version in (
            ("result_files_listed", 2), ("timelapse_completed", 1), ("results_returned", 1))),
    ))
    # typed effect 是端口对原操作的可靠确认，不要求通用流程认识设备观察名。
    runtime.driver.control.return_value = DeviceCallResult.from_outcome(replace(
        returned("operation", "timelapse_sent", 1), observations=(
            DeviceObservation(_CONTROL_COMPLETION.type, 1, {"activity_id": "1"}),)))
    runtime.wall_us = lambda: _NOW
    return owned, runtime, action_id, handler, driver


@pytest.mark.parametrize("case", ["video", "empty", "wrong_kind"])
async def test_completed_return_ends_activity_without_inventing_start_time(tmp_path, case):
    owned, runtime, action_id, handler, driver = await world(tmp_path, case=case)
    try:
        await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute("SELECT status FROM actions").fetchone() == ((3,) if case == "video" else (4,))
        assert owned.connection.execute("SELECT activity_state,sent_at,started_at,expected_check_at,wait_completed_event_id FROM device_activities").fetchone() == (3, None, None, None, None)
        start_event, start_time, start_txn = owned.connection.execute(
            "SELECT h.id,h.occurred_at,h.transaction_id FROM history_events h JOIN operation_attempts a ON a.result_event_id=h.id JOIN operation_runs r ON r.id=a.run_id WHERE r.kind=1").fetchone()
        ends = [(time, txn, json.loads(body)) for time, txn, body in owned.connection.execute(
            "SELECT occurred_at,transaction_id,body_json FROM history_events WHERE event_type=13")
            if any(row["after"]["values"].get("activity_state") == 3 for row in json.loads(body)["rows"])]
        assert len(ends) == 1
        assert ends[0][:2] == (start_time, start_txn)
        assert ends[0][2]["evidence"]["observation"] == {"start_result_event_id": start_event}
        assert runtime.timelapse_deadlines == {}
        runtime.driver.control.assert_awaited_once()
        assert driver.list_results.await_count == 2
    finally:
        owned.connection.close()


async def test_completed_return_keeps_ended_while_file_format_is_unknown(tmp_path):
    class FormatCatalog(CompletedCatalog):
        def parameter_definition(self, device_id, action_type, parameter_type):
            definition = super().parameter_definition(device_id, action_type, parameter_type)
            return replace(definition, task_factory=lambda params: replace(
                definition.task_factory(params), product_rules=({"kind": "video",
                    "format_id": "mp4", "min_count": 1, "exact_count": None,
                    "require_pairing": False},)))
    owned, runtime, action_id, handler = await consumer_world(tmp_path, "timelapse",
        catalog=FormatCatalog(), start=False)
    device_pages(runtime, completion_page=None, case="unknown_format")
    runtime.evidence = EvidenceRegistry((_CONTROL_COMPLETION, RESULT_EVIDENCE.contract("operation_returned", 1),
        _EVIDENCE.contract("result_files_listed", 2), _EVIDENCE.contract("results_returned", 1)))
    runtime.driver.control.return_value = DeviceCallResult.from_outcome(replace(
        returned("operation", "timelapse_sent", 1), observations=(
            DeviceObservation(_CONTROL_COMPLETION.type, 1, {"activity_id": "1"}),)))
    try:
        await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute("SELECT status FROM actions").fetchone() == (2,)
        assert owned.connection.execute("SELECT activity_state,completion_basis FROM device_activities").fetchone() == (3, 1)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("committed", [False, True])
async def test_unknown_completed_return_save_retains_original_request_and_time(tmp_path, committed):
    class UnknownStart(CaptureRepository):
        def __init__(self):
            self.requests = []
        def finish_start_result(self, finish, observation, key, owned, **kwargs):
            self.requests.append((finish, observation, key, kwargs))
            if committed:
                receipt = super().finish_start_result(finish, observation, key, owned, **kwargs)
                assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=ConsistencyError("原启动结果回执未知"))
    owned, runtime, action_id, handler, driver = await world(tmp_path)
    repository = UnknownStart()
    runtime.capture = repository
    try:
        with pytest.raises(ConsistencyError):
            await capture_handler(handler)(action_id, runtime)
        assert len(repository.requests) == 2 and repository.requests[0] == repository.requests[1]
        pending, = runtime.pending_start_results.values()
        assert pending.observation is not None
        assert pending.finish.occurred_at == pending.observation.occurred_at == _NOW
        if committed:
            for changed in (replace(pending.finish, occurred_at=_NOW + 1),
                            replace(pending.finish, outcome=replace(pending.finish.outcome,
                                outcome=replace(pending.finish.outcome.outcome, effect=EffectState.UNKNOWN)))):
                rejected = CaptureRepository().finish_start_result(changed, pending.observation,
                    pending.key, owned)
                assert rejected.kind is DbOutcomeKind.ROLLED_BACK
        path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
        owned.connection.close()
        owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        runtime.owned = owned
        runtime.capture = CaptureRepository()
        runtime.wall_us = lambda: _NOW + 900_000_000
        await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute("SELECT status FROM actions").fetchone() == (3,)
        runtime.driver.control.assert_awaited_once()
        assert driver.list_results.await_count == 2
    finally:
        owned.connection.close()


@pytest.mark.parametrize("meaning", [StartReturn.SENT, StartReturn.STARTED])
async def test_other_successful_return_meanings_do_not_prove_device_end(tmp_path, meaning):
    class OtherCatalog(DeviceCompletionCatalog):
        def parameter_definition(self, device_id, action_type, parameter_type):
            definition = super().parameter_definition(device_id, action_type, parameter_type)
            return replace(definition, task_factory=lambda params: replace(
                definition.task_factory(params), start_return_meaning=meaning))
    owned, runtime, action_id, handler, driver = await world(tmp_path, catalog=OtherCatalog())
    try:
        await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute("SELECT status FROM actions").fetchone() == (2,)
        assert owned.connection.execute("SELECT activity_state,sent_at,started_at FROM device_activities").fetchone() == (
            (1, _NOW, None) if meaning is StartReturn.SENT else (2, None, _NOW))
        assert runtime.capture.read_result_completion(action_id, owned) is None
    finally:
        owned.connection.close()


async def test_completed_return_history_restores_same_activity_in_three_paths(tmp_path):
    from camctl.contracts.history_values import INITIAL_BOUNDARY
    from camctl.history.decoding import decode_event_row
    from camctl.history.events import branch_of, event_type_name
    from camctl.history.replay import EntityImage, RestoreSeed, restore
    from camctl.history.snapshots import SnapshotRef, prepare_snapshot
    from camctl.history.validators import ValidatedEvent
    from camctl.persistence.executor import DbExecutor
    from camctl.persistence.repositories.history import HistoryRepository, SqliteSnapshotStore

    owned, runtime, action_id, handler, driver = await world(tmp_path)
    path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    history = HistoryRepository(path)
    executor = DbExecutor(lambda: open_existing(path, DbOpenMode.EXISTING_RW, DbConfig()),
        capacity=4, enqueue_timeout_seconds=9)
    try:
        middle = history.current_boundary()
        store = SqliteSnapshotStore(path, executor)
        image, = await store.load_entity_images((SnapshotRef(1, action_id),))
        await store.save_snapshots((prepare_snapshot(image),))
        await capture_handler(handler)(action_id, runtime)
        end = history.current_boundary()
        reverse = history.restore_entity("action", action_id, middle)
        snapshot = history.restore_entity("action", action_id, end)
        events = []
        for raw in owned.connection.execute(
            "SELECT h.id,h.transaction_id,h.event_type,h.event_version,h.occurred_at,h.clock_status,h.change_seq,h.body_json"
            " FROM history_events h JOIN entity_event_links l ON l.event_id=h.id"
            " WHERE l.entity_type=1 AND l.entity_id=? ORDER BY h.id", (action_id,)):
            event = decode_event_row(raw)
            events.append(ValidatedEvent(event, event_type_name(event.event_type),
                branch_of(event.event_type, event.reason)[0], ((1, action_id),),
                {(row.table, row.row_id): (1, action_id) for row in event.rows}))
        forward = restore(RestoreSeed(EntityImage(1, action_id, False, {}, 0, 0), INITIAL_BOUNDARY), events, end)
        key = ("device_activities", 1)
        assert reverse[key]["activity_state"] == 1
        for column in ("activity_state", "completion_basis", "completion_evidence_json", "result_set_state", "occupancy_state"):
            assert snapshot[key][column] == forward.rows[key][column]
        assert snapshot[key]["activity_state"] == 3 and snapshot[key]["completion_basis"] == 2
        assert snapshot[key]["completion_evidence_json"] == runtime.capture.read_result_completion(action_id, owned)
    finally:
        await executor.close()
        owned.connection.close()


async def test_completed_definition_does_not_turn_failed_return_into_ended(tmp_path):
    owned, runtime, action_id, handler, driver = await world(tmp_path)
    runtime.driver.control.return_value = DeviceCallResult.from_outcome(replace(
        runtime.driver.control.return_value.outcome, status=AttemptStatus.FAILED,
        error=ErrorValue("camera_rejected", "device"), effect=EffectState.UNKNOWN, observations=()))
    try:
        await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute("SELECT activity_state,sent_at,started_at FROM device_activities").fetchone() == (1, None, None)
    finally:
        owned.connection.close()

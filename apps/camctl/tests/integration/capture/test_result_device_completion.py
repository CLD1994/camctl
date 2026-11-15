"""设备完成观察与原结果轮次共同保存，不把扫描结束当作设备结束。"""
from dataclasses import replace
import json
from unittest.mock import create_autospec

import pytest

from support.repository_ports import repository_port

from camctl.bootstrap.capture_assembly import DriverResultListing
from camctl.capture.handlers import capture_handler
from camctl.capture.result_inputs import RESULT_PAGE_CONTRACT
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.ports import DeviceCallResult, ResultDriver
from camctl.devices.tasks import CompletionMode
from camctl.contracts.values import ConsistencyError
from camctl.operations.models import ErrorValue
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from pathlib import Path
from camctl.history.decoding import decode_event_row
from camctl.history.events import event_type_name, branch_of, business_columns
from camctl.history.validators import ValidatedEvent
from camctl.history.replay import EntityImage, RestoreSeed, restore
from camctl.contracts.history_values import INITIAL_BOUNDARY
from camctl.contracts.json_values import parse_exact_json
from camctl.history.snapshots import SnapshotRef, prepare_snapshot
from camctl.persistence.executor import DbExecutor
from camctl.persistence.repositories.history import HistoryRepository, SqliteSnapshotStore
from .result_consumer_fixtures import RESULT_EVIDENCE, ResultCatalog
from ..support.result_cases import result_case, result_case_for
from .test_result_page_history import _BINDING, _CURSOR, _actual, _entry

pytestmark = pytest.mark.asyncio

_COMPLETION = EvidenceContract("timelapse_completed", 1, "result",
    frozenset({"activity_id"}), identity_field="activity_id")
_EVIDENCE = EvidenceRegistry((RESULT_PAGE_CONTRACT, _COMPLETION,
    RESULT_EVIDENCE.contract("results_returned", 1)))


class DeviceCompletionCatalog(ResultCatalog):
    def parameter_definition(self, device_id, action_type, parameter_type):
        definition = super().parameter_definition(device_id, action_type, parameter_type)
        if action_type != "camera_timelapse":
            return definition
        return replace(definition, task_factory=lambda params: replace(
            definition.task_factory(params), completion_mode=CompletionMode.DEVICE_EVIDENCE,
            wait_after_send=False, result_wait_margin_s=None))


def device_pages(runtime, *, completion_page=1, case="video"):
    driver = create_autospec(ResultDriver, instance=True)
    async def read(request, batch):
        cursor = None if "cursor" not in request.params else _CURSOR
        number = 1 if cursor is None else 2
        entries = [] if case == "empty" else [_entry("a" if number == 1 else "b")]
        if case == "wrong_kind":
            entries = [{**entry, "kind": "photo"} for entry in entries]
        if case == "unknown_format":
            entries = [{**entry, "format_id": None} for entry in entries]
        outcome = _actual(request.ticket, entries=entries, cursor=cursor,
            next_cursor=_CURSOR if number == 1 and case != "read_error" else None,
            finalized=number == 2,
            error=ErrorValue("directory_read_failed", "device", {"received_bytes": 17})
                if case == "read_error" else None)
        if number == completion_page:
            observation, = outcome.observations
            outcome = replace(outcome, observations=(DeviceObservation(observation.type,
                observation.version, {**observation.data, "completion_evidence": {
                    "type": _COMPLETION.type, "version": 1,
                    "data": {"activity_id": request.ticket.target_id}}}),))
        return DeviceCallResult.from_outcome(outcome)
    driver.list_results.side_effect = read
    try:
        case_support = result_case_for()
    except RuntimeError:
        runtime.results = DriverResultListing(driver, _BINDING, _EVIDENCE)
        runtime.evidence = _EVIDENCE
    else:
        case_support.results.delegate = DriverResultListing(driver, _BINDING, _EVIDENCE)
    return driver


@pytest.mark.parametrize("completion_page", [1, 2])
@pytest.mark.parametrize("case", ["video", "empty", "wrong_kind"])
async def test_device_completion_ends_original_activity_and_preserves_output_result(tmp_path, completion_page, case):
    owned, runtime, action_id, handler = await result_case_for().world(tmp_path, "timelapse",
        independent_activity=True, catalog=DeviceCompletionCatalog(), prepare_start_only=True, evidence_contracts=(_COMPLETION,))
    driver = device_pages(runtime, completion_page=completion_page, case=case)
    try:
        await capture_handler(handler)(action_id, runtime)
        status, raw = owned.connection.execute("SELECT status,error_details_json FROM actions WHERE id=?", (action_id,)).fetchone()
        activity = owned.connection.execute("SELECT activity_state,completion_basis,occupancy_state,expected_check_at,wait_completed_event_id FROM device_activities").fetchone()
        assert status == (3 if case == "video" else 4)
        assert activity == (3, 2 if case == "video" else 4, 2, None, None)
        if case != "video":
            assert json.loads(raw) == {"activity_id": "1", "reason": "no_outputs" if case == "empty" else "invalid_outputs"}
        events = owned.connection.execute("SELECT event_type,body_json,transaction_id FROM history_events WHERE event_type IN (13,16) ORDER BY id").fetchall()
        ends = [(json.loads(body), txn) for kind, body, txn in events if kind == 13
            and any(row["after"]["values"].get("activity_state") == 3 for row in json.loads(body)["rows"])]
        assert len(ends) == 1
        assert ends[0][1] == next(txn for kind, body, txn in events if kind == 16)
        assert ends[0][0]["evidence"]["observation"]["completion_evidence"]["data"]["activity_id"] == "1"
        assert driver.list_results.await_count == 2
    finally:
        owned.connection.close()


async def test_complete_file_set_without_device_completion_remains_unconfirmed(tmp_path):
    owned, runtime, action_id, handler = await result_case_for().world(tmp_path, "timelapse", catalog=DeviceCompletionCatalog(), prepare_start_only=True, evidence_contracts=(_COMPLETION,))
    device_pages(runtime, completion_page=None)
    try:
        await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute("SELECT status FROM actions").fetchone() == (2,)
        assert owned.connection.execute("SELECT activity_state,completion_basis,occupancy_state,expected_check_at FROM device_activities").fetchone() == (1, 1, 1, None)
        assert owned.connection.execute("SELECT retry_wait_required FROM operation_runs WHERE kind=7").fetchone() == (1,)
    finally:
        owned.connection.close()


async def test_device_completion_is_retained_while_product_format_remains_unknown(tmp_path):
    class FormatCatalog(DeviceCompletionCatalog):
        def parameter_definition(self, device_id, action_type, parameter_type):
            definition = super().parameter_definition(device_id, action_type, parameter_type)
            return replace(definition, task_factory=lambda params: replace(
                definition.task_factory(params), product_rules=({"kind": "video",
                    "format_id": "mp4", "min_count": 1, "exact_count": None,
                    "require_pairing": False},)))
    owned, runtime, action_id, handler = await result_case_for().world(tmp_path, "timelapse", catalog=FormatCatalog(), prepare_start_only=True, evidence_contracts=(_COMPLETION,))
    device_pages(runtime, case="unknown_format")
    try:
        await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute("SELECT status FROM actions").fetchone() == (2,)
        assert owned.connection.execute("SELECT activity_state,completion_basis FROM device_activities").fetchone() == (3, 1)
        assert owned.connection.execute("SELECT retry_wait_required FROM operation_runs WHERE kind=7").fetchone() == (1,)
    finally:
        owned.connection.close()


async def test_device_completion_remains_reliable_after_later_round_omits_it(tmp_path):
    owned, runtime, action_id, handler = await result_case_for().world(tmp_path, "timelapse", catalog=DeviceCompletionCatalog(), prepare_start_only=True, evidence_contracts=(_COMPLETION,))
    driver = device_pages(runtime)
    # 完成观察可靠，但第一轮末页的集合尚未确定。
    original = driver.list_results.side_effect
    async def incomplete(request, batch):
        returned = await original(request, batch)
        outcome = returned.outcome
        observation, = outcome.observations
        return DeviceCallResult.from_outcome(replace(outcome, observations=(DeviceObservation(
            observation.type, observation.version, {**observation.data, "set_finalized": False}),)))
    driver.list_results.side_effect = incomplete
    try:
        await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute("SELECT activity_state,completion_basis FROM device_activities").fetchone() == (3, 1)
        result_case_for().clock.monotonic = 10_000_000_000
        second = device_pages(runtime, completion_page=None)
        await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute("SELECT status FROM actions").fetchone() == (3,)
        assert owned.connection.execute("SELECT completion_basis FROM device_activities").fetchone() == (2,)
        assert driver.list_results.await_count == second.list_results.await_count == 2
    finally:
        owned.connection.close()


async def test_device_completion_is_kept_even_when_directory_read_returns_error(tmp_path):
    owned, runtime, action_id, handler = await result_case_for().world(tmp_path, "timelapse", catalog=DeviceCompletionCatalog(), prepare_start_only=True, evidence_contracts=(_COMPLETION,))
    driver = device_pages(runtime, case="read_error")
    try:
        await capture_handler(handler)(action_id, runtime)
        assert owned.connection.execute("SELECT activity_state,completion_basis FROM device_activities").fetchone() == (3, 1)
        raw, = owned.connection.execute("SELECT error_json FROM operation_attempts WHERE run_id=(SELECT id FROM operation_runs WHERE kind=7)").fetchone()
        assert json.loads(raw) == {"code": "directory_read_failed", "stage": "device", "details": {"received_bytes": 17}}
        driver.list_results.assert_awaited_once()
    finally:
        owned.connection.close()


@pytest.mark.parametrize("committed", [False, True])
async def test_unknown_result_end_save_reopens_original_complete_request(tmp_path, committed):
    class UnknownConclusion(CaptureRepository):
        def __init__(self):
            self.requests = []
        def finish_result_check(self, finish, confirm, key, owned, *, completion_page=None, **options):
            self.requests.append((finish, confirm, key, completion_page))
            if committed:
                receipt = super().finish_result_check(finish, confirm, key, owned,
                    completion_page=completion_page, **options)
                assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=ConsistencyError("结论提交回执未知"))
    owned, runtime, action_id, handler = await result_case_for().world(tmp_path, "timelapse", catalog=DeviceCompletionCatalog(), prepare_start_only=True, evidence_contracts=(_COMPLETION,))
    driver = device_pages(runtime)
    repository = UnknownConclusion()
    path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    try:
        with repository_port(CaptureRepository, repository, ("finish_result_check",),
                matches=lambda _name, args, _kwargs: args[0].ticket.responsibility_key == f"results/{action_id}"):
            with pytest.raises(ConsistencyError):
                await capture_handler(handler)(action_id, runtime)
        assert len(repository.requests) == 2 and repository.requests[0] == repository.requests[1]
        assert owned.connection.execute("SELECT activity_state,completion_basis FROM device_activities").fetchone() == ((3, 2) if committed else (1, 1))
        finish, result_set, key, completion_page = repository.requests[0]
        assert completion_page is not None and result_set is not None
        if committed:
            changed = replace(result_set, evidence={
                **result_set.evidence, "observation": {"changed": True}})
            rejected = CaptureRepository().finish_result_check(finish, changed,
                key, owned, completion_page=completion_page)
            assert rejected.kind is DbOutcomeKind.ROLLED_BACK
            rejected = CaptureRepository().finish_result_check(finish,
                result_set, key, owned, completion_page=None)
            assert rejected.kind is DbOutcomeKind.ROLLED_BACK
        owned.connection.close()
        owned = open_existing(path, DbOpenMode.EXISTING_RW, DbConfig())
        resumed = await result_case_for().reopen(owned)
        result_case_for().clock.wall = 1_750_001_000_000_000
        await capture_handler(handler)(action_id, resumed)
        assert owned.connection.execute("SELECT activity_state,completion_basis FROM device_activities").fetchone() == (3, 2)
        assert owned.connection.execute("SELECT status FROM actions").fetchone() == (3,)
        assert driver.list_results.await_count == 2
    finally:
        owned.connection.close()


async def test_device_completion_history_restores_same_activity_in_three_paths(tmp_path):
    owned, runtime, action_id, handler = await result_case_for().world(tmp_path, "timelapse", catalog=DeviceCompletionCatalog(), prepare_start_only=True, evidence_contracts=(_COMPLETION,))
    path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    history = HistoryRepository(path)
    executor = DbExecutor(lambda: open_existing(path, DbOpenMode.EXISTING_RW, DbConfig()),
        capacity=4, enqueue_timeout_seconds=9)
    try:
        middle = history.current_boundary()
        def activity_rows():
            cursor = owned.connection.execute(
                "SELECT * FROM device_activities WHERE action_id=?", (action_id,))
            names = [column[0] for column in cursor.description]
            rows = {}
            for raw in cursor:
                values = dict(zip(names, raw))
                rows[("device_activities", values["id"])] = {
                    name: parse_exact_json(value) if name.endswith("_json") and value is not None else value
                    for name, value in values.items() if name in business_columns("device_activities")}
            return rows
        middle_activity = activity_rows()
        assert len(middle_activity) == 1
        store = SqliteSnapshotStore(path, executor)
        image, = await store.load_entity_images((SnapshotRef(1, action_id),))
        await store.save_snapshots((prepare_snapshot(image),))
        device_pages(runtime)
        await capture_handler(handler)(action_id, runtime)
        end = history.current_boundary()
        end_activity = activity_rows()
        assert end_activity.keys() == middle_activity.keys()
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
        current = runtime.capture.read_result_completion(action_id, owned)
        key, = middle_activity
        for expected, actual in ((middle_activity, reverse), (end_activity, snapshot),
                                 (end_activity, forward.rows)):
            actual_activity = {row_key: {name: values[name] for name in business_columns("device_activities")}
                for row_key, values in actual.items() if row_key[0] == "device_activities"}
            assert actual_activity == expected
        assert reverse[key]["activity_state"] == 1
        for column in ("activity_state", "completion_basis", "completion_evidence_json", "result_set_state", "occupancy_state"):
            assert snapshot[key][column] == forward.rows[key][column]
        assert snapshot[key]["activity_state"] == 3 and snapshot[key]["completion_basis"] == 2
        assert snapshot[key]["completion_evidence_json"] == current
    finally:
        try:
            await executor.close()
        finally:
            owned.connection.close()

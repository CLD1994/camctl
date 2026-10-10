"""停止后等待假设的文件完成证据保留原页、原 STOP 和原绑定。"""

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import pytest
from pathlib import Path

from camctl.capture.files import FileCompletionSave, OwnershipSave
from camctl.capture.handlers import _finish_listing_result, _listing_round, _register_listing
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.contracts.history_values import TransactionRange
from camctl.history.validators import EventContext, EventValidationError, validate_event
from camctl.operations.models import ErrorValue
from camctl.operations.attempts import AttemptConfig
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.history import HistoryRepository
from camctl.persistence.transaction import event_envelope, row_facts, update_change

from .test_record_file_completion_wait import (
    _completed_result, _completion, _result_port, _stop_context, _wait_world,
)
from .test_result_page_history import _entry
from .test_result_file_fact_saves import FileFactFault, FileFactSpy

pytestmark = pytest.mark.asyncio


async def _page_world(tmp_path, *, completion_changes=None, entry_changes=None, error=None):
    owned, runtime, action_id, _ = await _wait_world(tmp_path)
    context = _stop_context(owned, action_id)
    entry = {**_entry("a"), **(entry_changes or {})}

    async def read(request, batch):
        return _completed_result(request.ticket, context, entries=[entry],
            finalized=error is None, error=error,
            completion=_completion(context, **(completion_changes or {})))

    driver = _result_port(runtime, read)
    listing = await _listing_round(runtime, action_id)
    saved = runtime.capture.read_last_result_page(listing.ticket, owned)
    file_id = dict(saved.file_ids)[entry["identity"]]
    return owned, runtime, action_id, context, entry, driver, listing, saved, file_id


def _completion_command(file_id, context, entry, saved, **changes):
    values = dict(file_id=file_id, state=3, occurred_at=saved.occurred_at,
        basis=3, observation=deepcopy(entry), size_bytes=entry["size_bytes"],
        activity_id=int(context["activity_id"]), result_page_event_id=saved.ref.event_id,
        stop_result_event_id=context["stop_result_event_id"])
    values.update(changes)
    return FileCompletionSave(**values)


def _own(owned, runtime, action_id, file_id, entry, saved):
    receipt = runtime.capture.save_file_ownership(OwnershipSave(
        file_id, action_id, 1, 2, entry, saved.occurred_at), new_operation_key(), owned)
    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error


async def test_registered_wait_record_file_keeps_assumption_basis_and_original_sources(tmp_path):
    owned, runtime, action_id, context, entry, driver, listing, saved, file_id = await _page_world(tmp_path)
    try:
        _register_listing(runtime, action_id, listing)
        facts = row_facts(owned.connection, "device_files", file_id)
        assert facts["completion_state"] == 3 and facts["size_bytes"] == 50
        assert facts["completion_evidence_json"] == {
            "basis": 3, "observation": entry, "activity_id": int(context["activity_id"]),
            "result_page_event_id": saved.ref.event_id,
            "stop_result_event_id": context["stop_result_event_id"],
        }
        path = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
        history = HistoryRepository(path)
        restored = history.restore_entity("device_file", file_id, history.current_boundary())
        assert restored[("device_files", file_id)]["completion_evidence_json"] == facts["completion_evidence_json"]
        driver.list_results.assert_awaited_once()
        runtime.stopper.stop.assert_awaited_once()
    finally:
        owned.connection.close()


async def test_page_error_preserves_already_complete_single_file_with_completed_wait(tmp_path):
    owned, runtime, action_id, context, entry, _, listing, saved, file_id = await _page_world(
        tmp_path, error=ErrorValue("metadata_failed", "result", {"path": "/DCIM/b.mp4"}))
    try:
        _register_listing(runtime, action_id, listing)
        facts = row_facts(owned.connection, "device_files", file_id)
        assert facts["completion_state"] == 3
        assert facts["completion_evidence_json"] == _completion_command(
            file_id, context, entry, saved).completion_evidence()
    finally:
        owned.connection.close()


async def test_completed_file_keeps_current_entry_page_and_actual_original_wait_page(tmp_path):
    owned, runtime, action_id, _ = await _wait_world(tmp_path)
    runtime.check_config = AttemptConfig(3, Decimal("10"), Decimal("0"))
    context = _stop_context(owned, action_id)

    async def actual_wait(request, batch):
        return _completed_result(request.ticket, context,
            error=ErrorValue("metadata_failed", "result", {"path": "/DCIM/a.mp4"}))

    first_driver = _result_port(runtime, actual_wait)
    try:
        first = await _listing_round(runtime, action_id)
        _finish_listing_result(runtime, first, retry_wait=True)
        original = runtime.capture.read_last_result_page(first.ticket, owned)
        entry = _entry("a")

        async def completed_file(request, batch):
            return _completed_result(request.ticket, context, entries=[entry], finalized=True,
                source_page_event_id=original.ref.event_id)

        last_driver = _result_port(runtime, completed_file)
        final = await _listing_round(runtime, action_id)
        current = runtime.capture.read_last_result_page(final.ticket, owned)
        file_id = dict(current.file_ids)[entry["identity"]]
        _register_listing(runtime, action_id, final)
        evidence = row_facts(owned.connection, "device_files", file_id)["completion_evidence_json"]
        assert evidence == _completion_command(file_id, context, entry, current).completion_evidence()
        assert evidence["result_page_event_id"] == current.ref.event_id > original.ref.event_id
        data = current.page.outcome.settlement.evidence.data
        assert data["file_completion_source_page_event_id"] == original.ref.event_id
        assert original.page.outcome.settlement.evidence.data["file_completion_source_page_event_id"] is None
        assert data["file_completion"] == original.page.outcome.settlement.evidence.data["file_completion"]
        first_driver.list_results.assert_awaited_once()
        last_driver.list_results.assert_awaited_once()
        runtime.stopper.stop.assert_awaited_once()
    finally:
        owned.connection.close()


@pytest.mark.parametrize("changes", [
    {"completed": False}, {"observed_wait_ns": 4_999_999_999},
    {"activity_id": "999"}, {"stop_result_event_id": 999},
    {"required_wait_ms": 4999}, {"version": 2},
])
async def test_completion_repository_rejects_incomplete_or_mismatched_saved_wait(tmp_path, changes):
    owned, runtime, action_id, context, entry, _, _, saved, file_id = await _page_world(
        tmp_path, completion_changes=changes)
    try:
        _own(owned, runtime, action_id, file_id, entry, saved)
        receipt = runtime.capture.save_file_completion(
            _completion_command(file_id, context, entry, saved), new_operation_key(), owned)
        assert receipt.kind is DbOutcomeKind.ROLLED_BACK, receipt.error
        assert isinstance(receipt.error, ConsistencyError)
        assert row_facts(owned.connection, "device_files", file_id)["completion_state"] == 1
    finally:
        owned.connection.close()


@pytest.mark.parametrize("changes", [
    {"activity_id": 999}, {"result_page_event_id": 999}, {"stop_result_event_id": 999},
    {"size_bytes": 51}, {"observation": {"identity": "/DCIM/other.mp4", "complete": True, "size_bytes": 50}},
])
async def test_completion_repository_rejects_changed_file_or_original_source_reference(tmp_path, changes):
    owned, runtime, action_id, context, entry, _, _, saved, file_id = await _page_world(tmp_path)
    try:
        _own(owned, runtime, action_id, file_id, entry, saved)
        receipt = runtime.capture.save_file_completion(
            _completion_command(file_id, context, entry, saved, **changes), new_operation_key(), owned)
        assert receipt.kind is DbOutcomeKind.ROLLED_BACK, receipt.error
        assert row_facts(owned.connection, "device_files", file_id)["completion_state"] == 1
    finally:
        owned.connection.close()


async def test_first_stop_wait_completion_rejects_locator_different_from_original_entry(tmp_path):
    owned, runtime, action_id, context, entry, _, _, saved, file_id = await _page_world(tmp_path)
    try:
        _own(owned, runtime, action_id, file_id, entry, saved)
        command = _completion_command(file_id, context, entry, saved,
            locator={"path": "/DCIM/changed.mp4"})
        receipt = runtime.capture.save_file_completion(command, new_operation_key(), owned)
        assert receipt.kind is DbOutcomeKind.ROLLED_BACK, receipt.error
        facts = row_facts(owned.connection, "device_files", file_id)
        assert facts["completion_state"] == 1 and facts["locator_json"] == entry["locator"]
    finally:
        owned.connection.close()


@pytest.mark.parametrize("entry_changes", [{"complete": False}, {"complete": False, "size_bytes": None}])
async def test_saved_wait_does_not_complete_an_unconfirmed_file_entry(tmp_path, entry_changes):
    owned, runtime, action_id, context, entry, _, _, saved, file_id = await _page_world(
        tmp_path, entry_changes=entry_changes)
    try:
        _own(owned, runtime, action_id, file_id, entry, saved)
        receipt = runtime.capture.save_file_completion(_completion_command(
            file_id, context, entry, saved, size_bytes=50), new_operation_key(), owned)
        assert receipt.kind is DbOutcomeKind.ROLLED_BACK, receipt.error
        assert row_facts(owned.connection, "device_files", file_id)["completion_state"] == 1
    finally:
        owned.connection.close()


async def test_wait_record_cannot_bypass_saved_wait_with_device_guarantee_basis(tmp_path):
    owned, runtime, action_id, _, entry, _, _, saved, file_id = await _page_world(tmp_path)
    try:
        _own(owned, runtime, action_id, file_id, entry, saved)
        receipt = runtime.capture.save_file_completion(FileCompletionSave(
            file_id, 3, saved.occurred_at, basis=1, observation=entry, size_bytes=50),
            new_operation_key(), owned)
        assert receipt.kind is DbOutcomeKind.ROLLED_BACK, receipt.error
        assert row_facts(owned.connection, "device_files", file_id)["completion_state"] == 1
    finally:
        owned.connection.close()


async def test_wait_record_cannot_bypass_stop_wait_with_time_and_outputs_basis(tmp_path):
    owned, runtime, action_id, context, entry, _, _, saved, file_id = await _page_world(tmp_path)
    try:
        _own(owned, runtime, action_id, file_id, entry, saved)
        # 构造旧依据入口允许的匹配引用，隔离新录像必须选 basis3 的规则。
        owned.connection.execute("UPDATE device_activities SET wait_completed_event_id=? WHERE id=?",
            (context["stop_result_event_id"], int(context["activity_id"])))
        owned.connection.commit()
        command = FileCompletionSave(file_id, 3, saved.occurred_at, basis=2,
            observation=entry, size_bytes=50, activity_id=int(context["activity_id"]),
            wait_completed_event_id=context["stop_result_event_id"])
        receipt = runtime.capture.save_file_completion(command, new_operation_key(), owned)
        assert receipt.kind is DbOutcomeKind.ROLLED_BACK, receipt.error
        assert row_facts(owned.connection, "device_files", file_id)["completion_state"] == 1
    finally:
        owned.connection.close()


def _guard_state(owned, file_id, context, page_id):
    state = {"outputs": {}}
    for table in ("actions", "device_activities", "operation_runs", "operation_attempts", "device_files"):
        state[table] = {identity: row_facts(owned.connection, table, identity)
                        for identity, in owned.connection.execute(f"SELECT id FROM {table}")}
    columns = ("id", "transaction_id", "event_type", "event_version", "occurred_at", "clock_status", "change_seq", "body_json")
    state["history_events"] = {}
    for event_id in (context["stop_result_event_id"], page_id):
        row = owned.connection.execute(
            "SELECT id,transaction_id,event_type,event_version,occurred_at,clock_status,change_seq,body_json"
            " FROM history_events WHERE id=?", (event_id,)).fetchone()
        state["history_events"][event_id] = dict(zip(columns, row))
    return state


async def test_wait_record_event_guard_rejects_time_and_outputs_first_completion(tmp_path):
    owned, runtime, action_id, context, entry, _, _, saved, file_id = await _page_world(tmp_path)
    try:
        _own(owned, runtime, action_id, file_id, entry, saved)
        state = _guard_state(owned, file_id, context, saved.ref.event_id)
        event_id = owned.connection.execute("SELECT MAX(id) FROM history_events").fetchone()[0] + 1
        txn_id = owned.connection.execute("SELECT MAX(id) FROM history_transactions").fetchone()[0] + 1
        evidence = {"basis": 2, "observation": entry, "activity_id": int(context["activity_id"]),
                    "wait_completed_event_id": context["stop_result_event_id"]}
        event = event_envelope(event_id, txn_id, 17, 3, (update_change("device_files", file_id,
            {"completion_state": 1, "completion_evidence_json": None, "size_bytes": None},
            {"completion_state": 3, "completion_evidence_json": evidence, "size_bytes": 50}),),
            saved.occurred_at, evidence={"completion_request": dict.fromkeys(
                ("locator", "original_name", "media_type"))})
        validation = EventContext(TransactionRange(txn_id, event_id, event_id),
            {("device_files", file_id): ("device_file", file_id)}, state)
        with pytest.raises(EventValidationError):
            validate_event(event, validation)
    finally:
        owned.connection.close()


async def test_stop_wait_completion_event_guard_rejects_changed_final_locator(tmp_path):
    owned, runtime, action_id, context, entry, _, _, saved, file_id = await _page_world(tmp_path)
    try:
        _own(owned, runtime, action_id, file_id, entry, saved)
        state = _guard_state(owned, file_id, context, saved.ref.event_id)
        event_id = owned.connection.execute("SELECT MAX(id) FROM history_events").fetchone()[0] + 1
        txn_id = owned.connection.execute("SELECT MAX(id) FROM history_transactions").fetchone()[0] + 1
        completion = _completion_command(file_id, context, entry, saved).completion_evidence()
        changed_locator = {"path": "/DCIM/changed.mp4"}
        event = event_envelope(event_id, txn_id, 17, 3, (update_change("device_files", file_id,
            {"completion_state": 1, "completion_evidence_json": None, "size_bytes": None,
             "locator_json": entry["locator"]},
            {"completion_state": 3, "completion_evidence_json": completion, "size_bytes": 50,
             "locator_json": changed_locator}),), saved.occurred_at,
            evidence={"completion_request": {"locator": changed_locator, "original_name": None, "media_type": None}})
        validation = EventContext(TransactionRange(txn_id, event_id, event_id),
            {("device_files", file_id): ("device_file", file_id)}, state)
        with pytest.raises(EventValidationError):
            validate_event(event, validation)
    finally:
        owned.connection.close()


async def test_completion_event_guard_requires_original_wait_sources_and_exact_evidence(tmp_path):
    owned, runtime, action_id, context, entry, _, _, saved, file_id = await _page_world(tmp_path)
    try:
        _own(owned, runtime, action_id, file_id, entry, saved)
        state = _guard_state(owned, file_id, context, saved.ref.event_id)
        evidence = _completion_command(file_id, context, entry, saved).completion_evidence()
        event_id = owned.connection.execute("SELECT MAX(id) FROM history_events").fetchone()[0] + 1
        txn_id = owned.connection.execute("SELECT MAX(id) FROM history_transactions").fetchone()[0] + 1
        validation = EventContext(TransactionRange(txn_id, event_id, event_id),
            {("device_files", file_id): ("device_file", file_id)}, state)

        def event(value):
            return event_envelope(event_id, txn_id, 17, 3, (update_change("device_files", file_id,
                {"completion_state": 1, "completion_evidence_json": None, "size_bytes": None},
                {"completion_state": 3, "completion_evidence_json": value, "size_bytes": 50}),),
                saved.occurred_at, evidence={"completion_request": {
                    "locator": None, "original_name": None, "media_type": None}})

        validate_event(event(evidence), validation)
        for changed in ({**evidence, "stop_result_event_id": 999},
                        {**evidence, "result_page_event_id": 999},
                        {**evidence, "activity_id": 999},
                        {**evidence, "unexpected": True}):
            with pytest.raises(EventValidationError):
                validate_event(event(changed), validation)
        absent = deepcopy(state)
        absent["history_events"].pop(saved.ref.event_id)
        with pytest.raises(EventValidationError):
            validate_event(event(evidence), replace(validation, state_rows=absent))
        for request in ({"locator": None, "original_name": None},
                        {"locator": None, "original_name": "changed.mp4", "media_type": None},
                        {"locator": None, "original_name": None, "media_type": 1}):
            with pytest.raises(EventValidationError):
                validate_event(replace(event(evidence), evidence={"completion_request": request}), validation)
    finally:
        owned.connection.close()


@pytest.mark.parametrize("change", [
    {"locator": {"path": "/DCIM/changed.mp4"}},
    {"original_name": "changed.mp4"}, {"media_type": "application/octet-stream"},
    {"stop_result_event_id": 999}, {"result_page_event_id": 999},
])
async def test_original_completion_key_rejects_changed_complete_request(tmp_path, change):
    owned, runtime, action_id, context, entry, driver, _, saved, file_id = await _page_world(tmp_path)
    try:
        _own(owned, runtime, action_id, file_id, entry, saved)
        command = _completion_command(file_id, context, entry, saved,
            original_name="a.mp4", media_type="video/mp4")
        key = new_operation_key()
        first = runtime.capture.save_file_completion(command, key, owned)
        assert first.kind is DbOutcomeKind.COMPLETED, first.error
        original_history = owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone()
        again = runtime.capture.save_file_completion(command, key, owned)
        assert again.kind is DbOutcomeKind.COMPLETED, again.error
        changed = runtime.capture.save_file_completion(replace(command, **change), key, owned)
        assert changed.kind is DbOutcomeKind.ROLLED_BACK, changed.error
        assert owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone() == original_history
        driver.list_results.assert_awaited_once()
    finally:
        owned.connection.close()


@pytest.mark.parametrize("mode", ["projection", "commit_before", "commit_after"])
async def test_wait_file_completion_fault_keeps_original_request_and_key(tmp_path, mode):
    owned, runtime, action_id, context, entry, driver, listing, saved, file_id = await _page_world(tmp_path)
    spy = FileFactSpy("completion")
    proxy = FileFactFault(owned.connection, "completion", mode)
    runtime.capture = spy
    runtime.owned = replace(owned, connection=proxy)
    try:
        _register_listing(runtime, action_id, listing)
        assert not proxy.armed
        original, key = spy.requests[0]
        assert original.basis == 3
        assert original.result_page_event_id == saved.ref.event_id
        assert original.stop_result_event_id == context["stop_result_event_id"]
        assert all(command == original and request_key == key for command, request_key in spy.requests)
        assert row_facts(owned.connection, "device_files", file_id)["completion_evidence_json"] == original.completion_evidence()
        driver.list_results.assert_awaited_once()
        runtime.stopper.stop.assert_awaited_once()
    finally:
        owned.connection.close()

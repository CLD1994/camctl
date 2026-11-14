"""分段文件只消费明确原页及固定历史位置的完成证明。"""
from dataclasses import replace
from decimal import Decimal
import json
import sqlite3

import pytest

from camctl.capture.files import FileCompletionSave, OwnershipFromResultPageSave
from .result_page_fixtures import begin_result_check
from camctl.capture.result_pages import ResultPageSave
from camctl.capture.segment_models import FileCompletionState, PreviewPairingState
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.operations.models import EvidenceValue, Settlement, SettlementBasis
from camctl.operations.validation import validate_outcome
from camctl.history.validators import EventContext
from camctl.persistence.repositories import capture as capture_storage
from .transaction_fixtures import capture_operation_command
from camctl.persistence.repositories.transactions import BoundedCommandPlan, commit_operation

from .test_segment_storage import SegmentedCatalog
from .test_segment_file_pairing import _entry, _ok
from ..capture.result_consumer_fixtures import _NOW, complete_natural_wait, consumer_world
from ..capture.test_record_file_completion_wait import (
    _REGISTRY, _completion, _stop_context,
)
from ..capture.test_result_page_history import _actual


class WaitSegmentedCatalog(SegmentedCatalog):
    def parameter_definition(self, *args):
        definition = super().parameter_definition(*args)
        if definition is None or args[1] not in ("camera_record", "camera_timelapse"):
            return definition
        factory = definition.task_factory
        return replace(definition, task_factory=lambda params: replace(
            factory(params), file_completion_wait_s=Decimal("5")))


def _boundary(owned):
    transaction, event = owned.connection.execute(
        "SELECT id,last_event_id FROM history_transactions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return HistoryBoundary(transaction, event)


async def _proof_world(tmp_path, size=10, *, entries=None, next_cursor=None):
    owned, runtime, source, _ = await consumer_world(
        tmp_path, "record", catalog=WaitSegmentedCatalog())
    try:
        ticket = begin_result_check(owned,runtime, source).ticket
        context = _stop_context(owned, source)
        actual = replace(_actual(ticket, entries=(
                                  [_entry("raw.LRF", size=size)] if entries is None else entries),
                                  next_cursor=next_cursor, finalized=next_cursor is None),
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue(
                "results_returned", 1, {"file_completion": _completion(context),
                                      "file_completion_source_page_event_id": None})))
        page = _ok(runtime.capture.save_result_page(ResultPageSave(
            ticket, 1, None, validate_outcome(ticket, actual, _REGISTRY),
            runtime.wall_us()), new_operation_key(), owned))
        saved = runtime.capture.read_result_page(page, owned)
        file_id = saved.file_ids[0][1]
        _ok(runtime.capture.save_ownership_from_result_page(
            OwnershipFromResultPageSave(page, file_id, runtime.wall_us()),
            new_operation_key(), owned))
        return owned, runtime, source, saved, file_id, context
    except BaseException:
        owned.connection.close()
        raise


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [0, 10])
@pytest.mark.parametrize("bounded", [False, True])
async def test_unpaired_preview_completion_requires_saved_page_and_original_wait(tmp_path, size, bounded):
    owned, runtime, _, saved, file_id, context = await _proof_world(tmp_path, size)
    try:
        before = _boundary(owned)
        command = FileCompletionSave(
            file_id, 3, runtime.wall_us(), basis=3, observation=saved.page.entries[0].evidence,
            size_bytes=size, activity_id=int(context["activity_id"]),
            result_page_event_id=saved.ref.event_id,
            stop_result_event_id=context["stop_result_event_id"])
        key = new_operation_key()
        if bounded:
            original_command = capture_operation_command(
                capture_storage,
                lambda: capture_storage.CaptureRepository().save_file_completion(command, key, owned),
            )
            class BoundedCompletion:
                def plan(self, scope):
                    finite = original_command.plan(scope)
                    return BoundedCommandPlan(
                        iter(finite.events),
                        lambda event, transaction: EventContext(
                            transaction, finite.owners, finite.state_rows,
                            transaction_rows=finite.state_rows,
                            read_coverage=finite.read_coverage,
                            baseline_reads=finite.baseline_reads),
                        finite.result, lambda connection: None)
            receipt = commit_operation(BoundedCompletion(), key, owned)
            assert receipt.kind == "completed", receipt.error
        else:
            _ok(runtime.capture.save_file_completion(command, key, owned))
        after = _boundary(owned)
        current = runtime.capture.read_saved_result_file_input(saved.ref, file_id, after, owned)
        assert current.completion is FileCompletionState.COMPLETE
        assert current.pairing_state is PreviewPairingState.UNKNOWN
        assert current.file_event_id == after.last_event_id
        old = runtime.capture.read_saved_result_file_input(saved.ref, file_id, before, owned)
        assert old.completion is FileCompletionState.UNKNOWN
        assert old.file_event_id == before.last_event_id
        assert owned.connection.execute(
            "SELECT original_device_file_id,pairing_evidence_json,size_bytes FROM device_files WHERE id=?",
            (file_id,)).fetchone() == (None, None, size)
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_legacy_completion_does_not_qualify_strict_file_input(tmp_path):
    from .test_segment_file_pairing import _world
    owned, runtime, _, saved = await _world(tmp_path, [_entry("legacy.OSV")])
    try:
        file_id = saved.file_ids[0][1]
        _ok(runtime.capture.save_ownership_from_result_page(
            OwnershipFromResultPageSave(saved.ref, file_id, runtime.wall_us()),
            new_operation_key(), owned))
        _ok(runtime.capture.save_file_completion(FileCompletionSave(
            file_id, 3, runtime.wall_us(), basis=1, observation={}, size_bytes=10),
            new_operation_key(), owned))

        result = runtime.capture.read_saved_result_file_input(
            saved.ref, file_id, _boundary(owned), owned)
        assert result.completion is FileCompletionState.UNKNOWN
    finally:
        owned.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing_page", "size", "observation", "future_page"])
async def test_declared_completion_proof_damage_is_rejected_at_its_owner(tmp_path, damage):
    owned, runtime, _, saved, file_id, context = await _proof_world(tmp_path)
    try:
        if damage == "observation":
            from camctl.persistence.models import DbOutcomeKind
            before = _boundary(owned)
            rejected = runtime.capture.save_file_completion(FileCompletionSave(
                file_id, 3, runtime.wall_us(), basis=3, observation={"different": True},
                size_bytes=10, activity_id=int(context["activity_id"]),
                result_page_event_id=saved.ref.event_id,
                stop_result_event_id=context["stop_result_event_id"]), new_operation_key(), owned)
            assert rejected.kind is DbOutcomeKind.ROLLED_BACK
            assert isinstance(rejected.error, ConsistencyError)
            assert _boundary(owned) == before
            assert not owned.connection.in_transaction
            return
        _ok(runtime.capture.save_file_completion(FileCompletionSave(
            file_id, 3, runtime.wall_us(), basis=3,
            observation=saved.page.entries[0].evidence, size_bytes=10,
            activity_id=int(context["activity_id"]), result_page_event_id=saved.ref.event_id,
            stop_result_event_id=context["stop_result_event_id"]),
            new_operation_key(), owned))
        boundary = _boundary(owned)
        if damage == "size":
            owned.connection.execute("UPDATE device_files SET size_bytes=11 WHERE id=?", (file_id,))
        else:
            raw = json.loads(owned.connection.execute(
                "SELECT completion_evidence_json FROM device_files WHERE id=?", (file_id,)
            ).fetchone()[0])
            raw["result_page_event_id"] = (
                boundary.last_event_id + 1 if damage == "future_page" else 1)
            owned.connection.execute(
                "UPDATE device_files SET completion_evidence_json=? WHERE id=?",
                (json.dumps(raw), file_id))
        with pytest.raises(ConsistencyError):
            runtime.capture.read_saved_result_file_input(saved.ref, file_id, boundary, owned)
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_strict_file_input_rejects_middle_future_and_foreign_references(tmp_path):
    owned, runtime, _, saved, file_id, _ = await _proof_world(tmp_path)
    try:
        current = _boundary(owned)
        page_transaction = owned.connection.execute(
            "SELECT transaction_id FROM history_events WHERE id=?", (saved.ref.event_id,)
        ).fetchone()[0]
        invalid = (
            (saved.ref, file_id, HistoryBoundary(page_transaction, saved.ref.event_id)),
            (saved.ref, file_id, HistoryBoundary(current.txn_id + 1, current.last_event_id + 1)),
            (saved.ref, file_id + 1, current),
            (replace(saved.ref, page_no=2), file_id, current),
        )
        for page, identity, boundary in invalid:
            with pytest.raises(ConsistencyError):
                runtime.capture.read_saved_result_file_input(page, identity, boundary, owned)
        assert not owned.connection.in_transaction
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_unpaired_preview_can_use_its_original_natural_wait(tmp_path):
    owned, runtime, source, _ = await consumer_world(
        tmp_path, "timelapse", catalog=WaitSegmentedCatalog())
    try:
        wait_id = await complete_natural_wait(owned, source, _NOW + 700_000_000)
        ticket = begin_result_check(owned,runtime, source).ticket
        actual = _actual(ticket, entries=[_entry("natural.LRF")], finalized=True)
        page = _ok(runtime.capture.save_result_page(ResultPageSave(
            ticket, 1, None, validate_outcome(ticket, actual, _REGISTRY),
            runtime.wall_us()), new_operation_key(), owned))
        saved = runtime.capture.read_result_page(page, owned)
        file_id = saved.file_ids[0][1]
        _ok(runtime.capture.save_ownership_from_result_page(
            OwnershipFromResultPageSave(page, file_id, runtime.wall_us()),
            new_operation_key(), owned))
        start_id = owned.connection.execute(
            "SELECT t.result_event_id FROM operation_attempts t JOIN operation_runs r"
            " ON r.id=t.run_id WHERE r.responsibility_key=?", (f"start/{source}",)
        ).fetchone()[0]
        _ok(runtime.capture.save_file_completion(FileCompletionSave(
            file_id, 3, runtime.wall_us(), basis=2,
            observation=saved.page.entries[0].evidence, size_bytes=10,
            activity_id=int(ticket.target_id), wait_completed_event_id=wait_id,
            result_page_event_id=page.event_id, start_result_event_id=start_id),
            new_operation_key(), owned))
        result = runtime.capture.read_saved_result_file_input(
            page, file_id, _boundary(owned), owned)
        assert result.completion is FileCompletionState.COMPLETE
        assert result.pairing_state is PreviewPairingState.UNKNOWN
        assert result.page_event_id == page.event_id
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_strict_file_proof_queries_its_owner_history_not_all_events(tmp_path):
    owned, runtime, _, saved, file_id, context = await _proof_world(tmp_path)
    try:
        _ok(runtime.capture.save_file_completion(FileCompletionSave(
            file_id, 3, runtime.wall_us(), basis=3,
            observation=saved.page.entries[0].evidence, size_bytes=10,
            activity_id=int(context["activity_id"]), result_page_event_id=saved.ref.event_id,
            stop_result_event_id=context["stop_result_event_id"]),
            new_operation_key(), owned))

        from camctl.contracts.enums import load_registry
        owner = load_registry()["history_objects"]["device_file"]["id"]
        boundary = _boundary(owned)
        observations = []

        class MeasuredConnection:
            def __init__(self, connection):
                self.connection = connection

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def execute(self, sql, params=()):
                normalized = " ".join(sql.lower().split())
                if ("entity_event_links" in normalized
                        and tuple(params) == (owner, file_id, boundary.last_event_id)):
                    plan = self.connection.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
                    observations.append(plan)
                return self.connection.execute(sql, params)

        result = runtime.capture.read_saved_result_file_input(
            saved.ref, file_id, boundary,
            replace(owned, connection=MeasuredConnection(owned.connection)))
        assert observations, "未观察到指定文件和历史边界的查询"
        for plan in observations:
            details = "".join("".join(row[-1].lower().split()) for row in plan)
            assert all(part in details for part in ("index", "entity_type=?", "entity_id=?", "event_id<?")), plan
        assert result.completion is FileCompletionState.COMPLETE
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_strict_file_input_preserves_database_read_failure(tmp_path):
    owned, runtime, _, saved, file_id, _ = await _proof_world(tmp_path)
    try:
        class BrokenConnection:
            def __getattr__(self, name):
                return getattr(owned.connection, name)

            def execute(self, sql, params=()):
                if "SELECT MAX(event_id) FROM entity_event_links" in sql:
                    raise sqlite3.DatabaseError("read failure")
                return owned.connection.execute(sql, params)

        with pytest.raises(sqlite3.DatabaseError):
            runtime.capture.read_saved_result_file_input(
                saved.ref, file_id, _boundary(owned),
                replace(owned, connection=BrokenConnection()))
        assert not owned.connection.in_transaction
    finally:
        owned.connection.close()

"""正式取回、交付与校验入口保留原业务错误和延迟诊断。"""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from camctl.bootstrap.obtain_assembly import obtain_flow
from camctl.capture.files import file_identity_key
from camctl.contracts.pages import Page
from camctl.devices.drivers.registry import DriverRegistry
from camctl.logging_runtime.diagnostics import diagnostic_scope, error_diagnostics_context, flush_diagnostics
from camctl.outputs.copy import CompletionContext, CopyCompletionError, complete_copy
from camctl.outputs.handoff import DeliveryContext, DeliveryHandoffError, publish_delivery
from camctl.outputs.obtain_flow import advance_obtain, settle_obtain_diagnostics
from camctl.persistence.models import DbOutcome, DbOutcomeKind, db_outcome_for
from camctl.persistence.repositories import segment_selections
from camctl.persistence.repositories.outputs import OutputsRepository
from camctl.persistence.repositories.segment_selections import SegmentSelectionSaved

from ..bootstrap.test_output_binding_changes import Catalog
from ..support.database import business_database, database_path
from ..support.outputs import obtain_factory, output_operation, submit_output_document
from .test_copy_complete import complete_env
from .test_copy_segments import _drive
from .test_delivery import prepared_env
from .test_read_associations import read_targets as base_read_targets
from support.logging import recording_log_channel
from .test_qualification import _NOW
from .test_segment_default_scope import _source, _obtain


@pytest.fixture
def read_targets(base_read_targets):
    owned = base_read_targets
    owned.connection.execute(
        "UPDATE device_files SET identity_key=? WHERE id=501",
        (file_identity_key("cam-1", "camctl-adb", "file-0501"),),
    )
    owned.connection.commit()
    return owned


@pytest.mark.asyncio
async def test_failed_source_resolution_records_original_returned_details(read_targets, tmp_path):
    owned = read_targets
    owned.connection.execute("UPDATE actions SET status=3")
    owned.connection.commit()
    instant = datetime.fromtimestamp(_NOW // 1_000_000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    factory = obtain_factory(devices={}, drivers=DriverRegistry(()), staging=tmp_path / "staging",
        ready=tmp_path / "ready", processing=tmp_path / "processing", occurred_at=lambda: _NOW)
    records = []
    channel = recording_log_channel(records)
    async with business_database(database_path(owned)) as database:
        accepted = await submit_output_document(database, catalog=Catalog(), occurred_at=_NOW, body={
            "request_id": "19", "created_at": instant, "name": "来源诊断",
            "actions": [{"name": "取回", "type": "obtain_action_outputs", "scheduled_at": instant,
                         "params": {"source": {"action_instance_id": "999001"}}}]})
        action, = owned.connection.execute("SELECT id FROM actions WHERE plan_id=?", (accepted.plan_id,)).fetchone()
        with error_diagnostics_context(channel, lambda _: None):
            async with diagnostic_scope():
                await advance_obtain(factory(database))
                await advance_obtain(factory(database))
    assert len(records) == 1
    status, code, raw = owned.connection.execute("SELECT status,error_code,error_details_json FROM actions WHERE id=?", (action,)).fetchone()
    assert status == 4 and code is not None
    assert json.loads(records[0].message)["error"]["code"] == "source_resolution_failed"
    assert json.loads(records[0].message)["error"]["details"] == json.loads(raw)
    assert "999001" in raw
    assert json.loads(records[0].message)["context"]["action_id"] == action


@pytest.mark.asyncio
async def test_unconfirmed_handoff_error_is_held_even_when_save_fails(prepared_env):
    owned, roots, directories, prepared = prepared_env
    relative, = owned.connection.execute("SELECT relative_path FROM intermediate_files WHERE id=?", (prepared.target_file_id,)).fetchone()
    (roots.staging / relative).unlink()
    original = OSError(5, "save failed")
    class SavePort(OutputsRepository):
        calls = []
        def save_unconfirmed_failure(self, request, key, worker):
            self.calls.append((request, key))
            return DbOutcome(DbOutcomeKind.ROLLED_BACK, error=original)
    port, records = SavePort(), []
    channel = recording_log_channel(records)
    with error_diagnostics_context(channel, lambda _: None):
        with pytest.raises(DeliveryHandoffError) as caught:
            async with diagnostic_scope():
                await output_operation(owned, publish_delivery, prepared.delivery_id, DeliveryContext,
                    repository=port, directories=directories, occurred_at=_NOW + 10)
        await flush_diagnostics()
    assert caught.value.__cause__ is original
    assert len(port.calls) == len(records) == 1
    assert json.loads(records[0].message)["error"]["code"] == "delivery_handoff_unconfirmed"
    assert json.loads(records[0].message)["error"]["details"] == {"delivery_id": str(prepared.delivery_id)}
    assert owned.connection.execute("SELECT status FROM deliveries WHERE id=?", (prepared.delivery_id,)).fetchone() == (3,)


@asynccontextmanager
async def selection_world(tmp_path, *, observer=None, second=False):
    owned, capture, source = await _source(tmp_path, indexes=(7,))
    try:
        action, selection = _obtain(owned, capture, source, [999001], 20)
        following = None
        if second:
            following = _obtain(owned, capture, source, [999002], 21)
        factory = obtain_factory(devices={}, drivers=DriverRegistry(()), staging=tmp_path / "staging",
            ready=tmp_path / "ready", processing=tmp_path / "processing", occurred_at=capture.wall_us)
        async with business_database(database_path(owned), confirmed_write_observer=observer) as database:
            yield owned, database, factory, action, selection, following
    finally:
        owned.connection.close()


@pytest.mark.asyncio
async def test_original_selection_error_pages_keep_empty_continuations_and_end_once(monkeypatch, tmp_path):
    original = segment_selections.read_original_selection_error_page
    cursors, pages = [], []
    sentinel = "empty-prefix"
    def read(ref, cursor, worker):
        cursors.append(cursor)
        if cursor is None:
            return Page((), sentinel)
        page = original(ref, None if cursor == sentinel else cursor, worker)
        pages.append(page)
        return page
    monkeypatch.setattr(segment_selections, "read_original_selection_error_page", read)
    records = []
    channel = recording_log_channel(records)
    async with selection_world(tmp_path) as (owned, database, factory, action, selection, _):
        with error_diagnostics_context(channel, lambda _: None):
            async with diagnostic_scope():
                await advance_obtain(factory(database))
                await settle_obtain_diagnostics(factory(database))
                await settle_obtain_diagnostics(factory(database))
        assert cursors[:2] == [None, sentinel]
        assert pages[-1].exhausted
        assert owned.connection.execute("SELECT status FROM obtain_source_selections WHERE id=?", (selection,)).fetchone() == (2,)
    assert len(records) == 1
    failures = json.loads(records[0].message)["error"]["failures"]
    actual = [failure for page in pages for failure in page.items]
    assert len(failures) == len(actual) == 1
    assert failures[0]["error"]["code"] == "output_not_found"
    assert failures[0]["error"]["details"] == {"requested_output_id": "999001"}


@pytest.mark.asyncio
async def test_error_receipt_read_failure_does_not_replace_saved_business_result(monkeypatch, tmp_path):
    failure = OSError(5, "original receipt unavailable")
    failure.__cause__ = PermissionError(13, "original receipt cause")
    refs = []
    def read(ref, cursor, worker):
        refs.append(ref)
        raise failure
    monkeypatch.setattr(segment_selections, "read_original_selection_error_page", read)
    records = []
    channel = recording_log_channel(records)
    async with selection_world(tmp_path) as (owned, database, factory, action, selection, _):
        with error_diagnostics_context(channel, lambda _: None):
            async with diagnostic_scope():
                await advance_obtain(factory(database))
                await settle_obtain_diagnostics(factory(database))
        assert len(refs) == 1 and refs[0].selection_id == selection
        assert owned.connection.execute("SELECT status FROM obtain_source_selections WHERE id=?", (selection,)).fetchone() == (2,)
        code, raw = owned.connection.execute("SELECT error_code,error_details_json FROM obtain_items WHERE selection_id=?", (selection,)).fetchone()
        assert code is not None and json.loads(raw) == {"requested_output_id": "999001"}
    assert len(records) == 1
    assert json.loads(records[0].message)["stage"] == "selection_diagnostic_receipt"
    assert json.loads(records[0].message)["error"]["errno"] == 5
    assert json.loads(records[0].message)["error"]["cause"]["errno"] == 13


@pytest.mark.asyncio
@pytest.mark.parametrize("exhausted", [False, True])
async def test_checksum_failure_is_held_before_recopy_registration(complete_env, exhausted):
    owned, roots, prepared = complete_env
    owned.connection.execute("UPDATE file_copies SET source_sha256=? WHERE id=?", ("a" * 64, prepared.copy_id))
    owned.connection.commit()
    first = await output_operation(owned, complete_copy, prepared.copy_id, CompletionContext,
        repository=OutputsRepository(), roots=roots, occurred_at=_NOW + 10)
    assert first.phase.value == "recopy_registered"
    await _drive(owned, roots, prepared.copy_id, segment_size=10, occurred_at=_NOW + 11)
    original = OSError(5, "save failed")
    class SavePort(OutputsRepository):
        calls = []
        def register_recopy(self, request, key, worker):
            self.calls.append((request, key))
            return DbOutcome(DbOutcomeKind.ROLLED_BACK, error=original)
    port, records = SavePort(), []
    channel = recording_log_channel(records)
    with error_diagnostics_context(channel, lambda _: None):
        with pytest.raises(CopyCompletionError) as caught:
            async with diagnostic_scope():
                await output_operation(owned, complete_copy, prepared.copy_id, CompletionContext,
                    repository=port, roots=roots, occurred_at=_NOW + 20, max_recopies=1 if exhausted else 2)
        await flush_diagnostics()
    assert caught.value.__cause__ is original
    assert len(port.calls) == len(records) == 1
    assert json.loads(records[0].message)["stage"] == ("checksum_exhausted" if exhausted else "checksum_verification")
    assert json.loads(records[0].message)["context"]["copy_round"] == 2
    assert owned.connection.execute("SELECT round,recopies_used FROM file_copies WHERE id=?", (prepared.copy_id,)).fetchone() == (2, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("controlled", [False, True])
async def test_completed_selection_delivery_error_defers_original_pages_until_settlement(monkeypatch, tmp_path, controlled):
    failure = asyncio.CancelledError("original control") if controlled else OSError(5, "observer failed")
    receipts = []
    def observer(worker, outcome, key):
        if isinstance(outcome.value, SegmentSelectionSaved):
            receipts.append((outcome, key))
            raise failure
    original = segment_selections.read_original_selection_error_page
    reads, settled = [], False
    def read(ref, cursor, worker):
        assert settled, "诊断读取不得先于必要收场"
        reads.append((ref, cursor))
        return original(ref, cursor, worker)
    monkeypatch.setattr(segment_selections, "read_original_selection_error_page", read)
    records = []
    channel = recording_log_channel(records)
    async with selection_world(tmp_path, observer=observer, second=True) as (owned, database, factory, action, selection, following):
        with error_diagnostics_context(channel, lambda _: None):
            with pytest.raises(type(failure)) as caught:
                async with diagnostic_scope():
                    await advance_obtain(factory(database))
            assert caught.value is failure
            assert db_outcome_for(failure).value is receipts[0][0].value
            assert db_outcome_for(failure).kind is DbOutcomeKind.COMPLETED
            assert not reads and not records
            assert owned.connection.execute("SELECT status FROM obtain_source_selections WHERE id=?", (following[1],)).fetchone() == (1,)
            settled = True
            async with diagnostic_scope():
                await settle_obtain_diagnostics(factory(database))
                saved_count = len(reads)
                await settle_obtain_diagnostics(factory(database))
            assert len(reads) == saved_count
    assert len(records) == 1 and json.loads(records[0].message)["stage"] == "output_selection"
    assert json.loads(records[0].message)["context"]["selection_id"] == selection


@pytest.mark.asyncio
async def test_obtain_flow_settlement_port_uses_original_context_database(tmp_path):
    failure = OSError(5, "post-commit observer failed")
    def observer(worker, outcome, key):
        if isinstance(outcome.value, SegmentSelectionSaved):
            raise failure
    records, databases = [], []
    channel = recording_log_channel(records)
    async with selection_world(tmp_path, observer=observer) as (owned, database, factory, action, selection, _):
        def tracked_factory(actual):
            databases.append(actual)
            return factory(actual)
        flow = obtain_flow(tracked_factory)
        context = SimpleNamespace(database=database)
        with error_diagnostics_context(channel, lambda _: None):
            with pytest.raises(OSError) as caught:
                await flow(context)
            assert caught.value is failure
            async with diagnostic_scope():
                await flow.settle_diagnostics(context)
                await flow.settle_diagnostics(context)
        assert databases == [database, database, database]
    assert len(records) == 1
    assert json.loads(records[0].message)["context"]["selection_id"] == selection

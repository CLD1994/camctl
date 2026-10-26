"""取回的已处理业务失败仍交付原错误事实。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from camctl.contracts.workflow_errors import action_error_id
from camctl.logging_runtime.diagnostics import (
    diagnostic_scope, error_diagnostics_context, flush_diagnostics,
)
from camctl.outputs import obtain_flow
from camctl.outputs.handoff import DeliveryFailure, _save_unconfirmed
from camctl.outputs.sources import SourceSpec
from camctl.contracts.pages import Page
from camctl.contracts.values import new_operation_key
from camctl.logging_runtime.diagnostics import DiagnosticState
from camctl.outputs.copy import _register_recopy
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.outputs import ResolveSourcesOutcome

from .test_read_diagnostics import Channel


@pytest.mark.asyncio
async def test_failed_source_resolution_records_original_returned_details(monkeypatch):
    details = {"reason": "action_not_found", "source_name": "missing capture"}
    result = ResolveSourcesOutcome(False, error_code=action_error_id("source_resolution_failed"),
                                  error_details=details)
    flow = SimpleNamespace(database=SimpleNamespace(write=AsyncMock(return_value=
        DbOutcome(DbOutcomeKind.COMPLETED, result))),
        outputs=SimpleNamespace(resolve_sources=lambda *args: None))
    monkeypatch.setattr(obtain_flow, "_source_spec", lambda _: SourceSpec(action_instance_id=7))
    channel = Channel()
    with error_diagnostics_context(channel, lambda _: None):
        async with diagnostic_scope():
            assert await obtain_flow._resolve_sources(flow, {"id": 11}, 123) is False
            assert await obtain_flow._resolve_sources(flow, {"id": 11}, 123) is False
    assert len(channel.records) == 1
    assert channel.records[0]["error"]["code"] == "source_resolution_failed"
    assert channel.records[0]["error"]["details"] == details
    assert channel.records[0]["context"]["action_id"] == 11


@pytest.mark.asyncio
async def test_unconfirmed_handoff_error_is_held_even_when_save_fails():
    failure = DeliveryFailure("delivery_handoff_unconfirmed", "publication", {"delivery_id": "7"})
    flow = SimpleNamespace(database=SimpleNamespace(write=AsyncMock(side_effect=OSError("save failed"))),
        repository=SimpleNamespace(save_unconfirmed_failure=lambda *args: None), occurred_at=123)
    channel = Channel()
    with error_diagnostics_context(channel, lambda _: None):
        with pytest.raises(OSError):
            async with diagnostic_scope():
                await _save_unconfirmed(7, failure, flow)
        await flush_diagnostics()
    assert len(channel.records) == 1
    assert channel.records[0]["error"]["code"] == "delivery_handoff_unconfirmed"


@pytest.mark.asyncio
async def test_original_selection_error_pages_keep_empty_continuations_and_end_once(monkeypatch):
    from camctl.persistence.repositories import segment_selections
    error = {"error": {"code": "preview_missing", "details": {"source": "original"}}}
    ref = SimpleNamespace(action_id=11, selection_id=17, operation_key=new_operation_key())
    pending = obtain_flow.PendingSegmentSelection(None, ref.operation_key,
        saved=SimpleNamespace(diagnostic_ref=ref))
    cursors = []
    pages = iter((Page((), "following"), Page((error,), None)))
    def read(actual_ref, cursor, owned):
        assert actual_ref is ref
        cursors.append(cursor)
        return next(pages)
    monkeypatch.setattr(segment_selections, "read_original_selection_error_page", read)
    async def database_read(callback, **_kwargs):
        return callback(None)
    flow = SimpleNamespace(database=SimpleNamespace(read=database_read))
    channel = Channel()
    with error_diagnostics_context(channel, lambda _: None):
        async with diagnostic_scope():
            await obtain_flow._consume_selection_diagnostics(flow, pending)
            await obtain_flow._consume_selection_diagnostics(flow, pending)
    assert cursors == [None, "following"]
    assert len(channel.records) == 1
    assert channel.records[0]["error"]["failures"] == [error]


@pytest.mark.asyncio
async def test_error_receipt_read_failure_does_not_replace_saved_business_result(monkeypatch):
    from camctl.persistence.repositories import segment_selections
    failure = OSError(5, "original receipt unavailable")
    def read(*_args):
        raise failure
    monkeypatch.setattr(segment_selections, "read_original_selection_error_page", read)
    ref = SimpleNamespace(action_id=11, selection_id=17, operation_key=new_operation_key())
    saved = SimpleNamespace(diagnostic_ref=ref)
    pending = obtain_flow.PendingSegmentSelection(None, ref.operation_key, saved=saved)
    async def database_read(callback, **_kwargs):
        return callback(None)
    channel = Channel()
    with error_diagnostics_context(channel, lambda _: None):
        async with diagnostic_scope():
            await obtain_flow._consume_selection_diagnostics(
                SimpleNamespace(database=SimpleNamespace(read=database_read)), pending)
    assert pending.saved is saved and pending.diagnostics_done
    assert len(channel.records) == 1
    assert channel.records[0]["stage"] == "selection_diagnostic_receipt"


@pytest.mark.asyncio
@pytest.mark.parametrize("exhausted", [False, True])
async def test_checksum_failure_is_held_before_recopy_registration(exhausted):
    facts = SimpleNamespace(source_sha256="a" * 64, round=2, recopies_used=int(exhausted))
    context = SimpleNamespace(key=None, max_recopies=1, occurred_at=123, diagnostic=DiagnosticState(),
        database=SimpleNamespace(write=AsyncMock(side_effect=OSError("save failed"))),
        repository=SimpleNamespace(register_recopy=lambda *args: None))
    channel = Channel()
    with error_diagnostics_context(channel, lambda _: None):
        with pytest.raises(OSError):
            async with diagnostic_scope():
                await _register_recopy(13, context, facts, "b" * 64)
        await flush_diagnostics()
    assert len(channel.records) == 1
    assert channel.records[0]["stage"] == ("checksum_exhausted" if exhausted else "checksum_verification")
    assert channel.records[0]["context"]["copy_round"] == 2

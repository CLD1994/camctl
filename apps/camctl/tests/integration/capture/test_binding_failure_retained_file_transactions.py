"""绑定失败登记的完整申请、预览关系和原键重送契约。"""

from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

from camctl.capture.handlers import capture_handler
from camctl.capture.media import RecordingFailure
from camctl.devices.evidence import DeviceObservation
from camctl.operations.attempts import AttemptConfig
from camctl.operations.models import AttemptStatus, CallOutcome, EffectState, EvidenceValue, Settlement, SettlementBasis
from camctl.outputs.catalog import FileReference, OutputCatalogFacts, OutputDraft, OutputKind
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture import CaptureRepository, FinishDisposition
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.transaction import saved_transaction_events

from .result_consumer_fixtures import consumer_world
from .test_binding_failure_keeps_timelapse_files import _BindingSaveSpy, _assert_joint_binding_finish
from .test_cancel_timelapse_exhaustion_files import _apply_public_cancel
from .test_closed_result_local_consumption import (
    _ClosedBeforeBusiness, _capture_facts, _rows, _unconfirmed_error, fresh_closed_runtime,
)
from .test_result_consumer_saves import _result_port


pytestmark = pytest.mark.asyncio


async def _paired_capture_world(tmp_path, monkeypatch, *, incomplete_a=False):
    owned, runtime, action_id, handler = await consumer_world(
        tmp_path, "timelapse", independent_activity=True)
    try:
        activity_id, = owned.connection.execute(
            "SELECT id FROM device_activities WHERE action_id=?", (action_id,)).fetchone()
        assert action_id != activity_id
        # 原片先保存可靠归属，预览才可以沿同来源的真实配对登记。
        entries = [
            {"identity": "original-a", "kind": "video", "complete": not incomplete_a,
             "size_bytes": None if incomplete_a else 41, "locator": {"path": "/DCIM/a.mp4"},
             "original_name": "a.mp4", "media_type": "video/mp4"},
            {"identity": "original-b", "kind": "video", "complete": True,
             "size_bytes": 43, "locator": {"path": "/DCIM/b.mp4"},
             "original_name": "b.mp4", "media_type": "video/mp4"},
            {"identity": "preview-a", "kind": "photo", "complete": True,
             "size_bytes": 17, "locator": {"path": "/DCIM/a.jpg"},
             "original_name": "a.jpg", "media_type": "image/jpeg", "paired_identity": "original-a"},
        ]
        actual = CallOutcome(status=AttemptStatus.SUCCEEDED, effect=EffectState.CONFIRMED,
            settlement=Settlement(SettlementBasis.OBSERVED, EvidenceValue("results_returned", 1, {})),
            observations=(DeviceObservation("result_files_listed", 1, {
                "activity_id": str(activity_id), "entries": entries}),))
        result_driver = _result_port(runtime, actual)
        runtime.check_config = AttemptConfig(1, Decimal("1.25"), Decimal(0))
        advance = capture_handler(handler)
        await advance(action_id, runtime)
        run_id, status, used, retry = owned.connection.execute(
            "SELECT id,status,attempts_used,retry_wait_required FROM operation_runs"
            " WHERE responsibility_key=?", (f"results/{activity_id}",)).fetchone()
        assert (status, used, retry) == (2, 1, 1)
        raw, = owned.connection.execute(
            "SELECT result_json FROM operation_attempts WHERE run_id=?", (run_id,)).fetchone()
        assert json.loads(raw)["observations"] == [{"type": "result_files_listed", "version": 1,
            "data": {"activity_id": str(activity_id), "entries": entries}}]
        rows = owned.connection.execute(
            "SELECT id,original_name,source_action_id,role,original_device_file_id,completion_state,size_bytes"
            " FROM device_files ORDER BY id").fetchall()
        (a, a_name, a_source, a_role, a_pair, a_complete, a_size), (
            b, b_name, b_source, b_role, b_pair, b_complete, b_size), (
            preview, p_name, p_source, p_role, p_pair, p_complete, p_size) = rows
        assert (a_name, a_source, a_role, a_pair) == ("a.mp4", action_id, 2, None)
        assert (b_name, b_source, b_role, b_pair, b_complete, b_size) == ("b.mp4", action_id, 2, None, 3, 43)
        assert (p_name, p_source, p_role, p_pair, p_complete, p_size) == ("a.jpg", action_id, 3, a, 3, 17)
        if incomplete_a:
            assert a_complete != 3 and a_size is None
        else:
            assert (a_complete, a_size) == (3, 41)
        original_close = CaptureRepository.close_result_check_unconfirmed
        closed = []

        def close(repository, request, key, save_owned):
            receipt = original_close(repository, request, key, save_owned)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            closed.append((request, key))
            raise _ClosedBeforeBusiness

        formed_at = runtime.wall_us()
        with monkeypatch.context() as patch:
            patch.setattr(CaptureRepository, "close_result_check_unconfirmed", close)
            with pytest.raises(_ClosedBeforeBusiness):
                await advance(action_id, runtime)
        (close_request, close_key), = closed
        assert close_request.capture == {"status": "unconfirmed", "error": _unconfirmed_error(activity_id)}
        assert owned.connection.execute(
            "SELECT status,attempts_used,retry_wait_required FROM operation_runs WHERE id=?",
            (run_id,)).fetchone() == (6, 1, 0)
        assert _capture_facts(owned, action_id)[5] == 1
        _apply_public_cancel(owned, action_id, formed_at + 10)
        world = SimpleNamespace(path=Path(owned.connection.execute("PRAGMA database_list").fetchone()[2]),
            metadata=owned.metadata, action_id=action_id, activity_id=activity_id, handler=handler,
            formed_at=formed_at, run_id=run_id, a=a, b=b, preview=preview,
            close_key=close_key, close_events=saved_transaction_events(owned.connection, close_key),
            files=_rows(owned, "device_files"), attempts=_rows(owned, "operation_attempts"),
            runs=_rows(owned, "operation_runs"), capture=_capture_facts(owned, action_id),
            prefix=_rows(owned, "history_events"), control=runtime.driver, results=result_driver)
        assert world.close_events is not None
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
    finally:
        owned.connection.close()
    reopened = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        next_runtime, methods = fresh_closed_runtime(reopened, world, "missing")
        return world, reopened, next_runtime, methods
    except BaseException:
        reopened.connection.close()
        raise


def _expected_drafts(world):
    original_a = OutputDraft(OutputKind.ORIGINAL, FileReference(device_file_id=world.a), True)
    original_b = OutputDraft(OutputKind.ORIGINAL, FileReference(device_file_id=world.b), True)
    preview = OutputDraft(OutputKind.PREVIEW, FileReference(device_file_id=world.preview), True,
                          original_batch_file_id=world.a)
    return preview, original_b, original_a


class _PreviewFirstSave(_BindingSaveSpy):
    """在首次真实写入前固定合法输入顺序，真实文件配对保持。"""

    def __init__(self, world):
        super().__init__()
        self.world = world

    def finish_binding_failure(self, request, key, owned):
        preview, b, a = _expected_drafts(self.world)
        assert request.drafts == (a, b, preview)
        selected = replace(request, drafts=(preview, b, a))
        return super().finish_binding_failure(selected, key, owned)


def _assert_source_preserved(owned, world, methods):
    assert _rows(owned, "device_files") == world.files
    assert _rows(owned, "operation_attempts") == world.attempts
    assert _capture_facts(owned, world.action_id) == world.capture
    for run in world.runs:
        assert owned.connection.execute("SELECT * FROM operation_runs WHERE id=?", (run[0],)).fetchone() == run
    assert saved_transaction_events(owned.connection, world.close_key) == world.close_events
    assert _rows(owned, "history_events")[:len(world.prefix)] == world.prefix
    world.control.control.assert_awaited_once()
    world.results.list_results.assert_awaited_once()
    for method in methods:
        method.assert_not_called()


@pytest_asyncio.fixture
async def saved_pair(tmp_path, monkeypatch):
    world, owned, runtime, methods = await _paired_capture_world(tmp_path, monkeypatch)
    reopened = None
    try:
        saves = _PreviewFirstSave(world)
        runtime.capture = saves
        await capture_handler(world.handler)(world.action_id, runtime)
        (request, key, _owned), = saves.calls
        receipt, = saves.receipts
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        assert request.drafts == _expected_drafts(world)
        assert request.catalog_facts == OutputCatalogFacts(world.action_id, True)
        # ID 由输入顺序分配；公开响应与固定文件关联独立核对。
        assert receipt.value.output_ids == (1, 2, 3)
        assert owned.connection.execute(
            "SELECT id,kind,device_file_id,original_name,media_type FROM outputs ORDER BY id").fetchall() == [
                (1, 3, world.preview, "a.jpg", "image/jpeg"),
                (2, 1, world.b, "b.mp4", "video/mp4"),
                (3, 1, world.a, "a.mp4", "video/mp4")]
        assert owned.connection.execute(
            "SELECT output_id,original_output_id FROM output_origins").fetchall() == [(1, 3)]
        _assert_source_preserved(owned, world, methods)
        world.request, world.key, world.response = request, key, receipt.value
        owned.connection.close()
        reopened = open_existing(world.path, DbOpenMode.EXISTING_RW, DbConfig())
        yield world, reopened
    finally:
        owned.connection.close()
        if reopened is not None:
            reopened.connection.close()


async def test_preview_first_request_reuses_original_output_id_order(saved_pair):
    world, owned = saved_pair
    before = tuple(owned.connection.iterdump())

    receipt = CaptureRepository().finish_binding_failure(world.request, world.key, owned)

    assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
    assert receipt.value.disposition is FinishDisposition.ALREADY
    assert (receipt.value.action_status, receipt.value.plan_status, receipt.value.output_ids) == (
        world.response.action_status, world.response.plan_status, (1, 2, 3))
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("changed", [
    "deleted_draft", "appended_draft", "exchanged_order", "different_pairing",
    "different_error", "different_time", "different_stop_config", "different_responsibilities",
])
async def test_original_key_rejects_changed_complete_binding_request(saved_pair, changed):
    world, owned = saved_pair
    request = world.request
    preview, b, a = request.drafts
    candidates = {
        "deleted_draft": lambda: replace(request, drafts=(b, a)),
        "appended_draft": lambda: replace(request, drafts=(preview, b, a,
            OutputDraft(OutputKind.ORIGINAL, FileReference(device_file_id=world.preview + 1), True))),
        "exchanged_order": lambda: replace(request, drafts=(preview, a, b)),
        "different_pairing": lambda: replace(request, drafts=(
            replace(preview, original_batch_file_id=world.b), b, a)),
        "different_error": lambda: replace(request, failure=RecordingFailure("device_binding_unavailable", {
            "device_id": "cam-1", "expected_driver_id": "camctl-adb", "reason": "mismatch",
            "actual_driver_id": "alternate-camera"})),
        "different_time": lambda: replace(request, occurred_at=request.occurred_at + 1),
        "different_stop_config": lambda: replace(request, stop_config=replace(
            request.stop_config, max_attempts=request.stop_config.max_attempts + 1)),
        "different_responsibilities": lambda: replace(request, responsibility_keys=(f"start/{world.action_id}",)),
    }
    altered = candidates[changed]()
    before = tuple(owned.connection.iterdump())

    receipt = CaptureRepository().finish_binding_failure(altered, world.key, owned)

    assert receipt.kind is DbOutcomeKind.ROLLED_BACK
    assert receipt.error is not None
    assert tuple(owned.connection.iterdump()) == before


@pytest.mark.parametrize("invalid", [
    "list_drafts", "catalog_missing", "catalog_other_action", "catalog_unowned", "catalog_bool_as_int",
    "incomplete", "complete_as_int", "noncanonical_sha", "intermediate_repaired", "empty_with_catalog",
])
async def test_binding_directory_input_rejects_noncanonical_members(saved_pair, invalid):
    world, owned = saved_pair
    request = world.request
    preview, b, a = request.drafts
    candidates = {
        "list_drafts": lambda: replace(request, drafts=list(request.drafts)),
        "catalog_missing": lambda: replace(request, catalog_facts=None),
        "catalog_other_action": lambda: replace(request, catalog_facts=OutputCatalogFacts(world.action_id + 1, True)),
        "catalog_unowned": lambda: replace(request, catalog_facts=OutputCatalogFacts(world.action_id, False)),
        "catalog_bool_as_int": lambda: replace(request, catalog_facts=OutputCatalogFacts(world.action_id, 1)),
        "incomplete": lambda: replace(request, drafts=(preview, b, replace(a, file_complete=False))),
        "complete_as_int": lambda: replace(request, drafts=(preview, b, replace(a, file_complete=1))),
        "noncanonical_sha": lambda: replace(request, drafts=(preview, b, replace(a, sha256="ab" * 32))),
        "intermediate_repaired": lambda: replace(request, drafts=(OutputDraft(OutputKind.REPAIRED,
            FileReference(intermediate_file_id=1), True, original_batch_file_id=world.a),)),
        "empty_with_catalog": lambda: replace(request, drafts=()),
    }
    before = tuple(owned.connection.iterdump())

    with pytest.raises((TypeError, ValueError)):
        candidates[invalid]()

    assert tuple(owned.connection.iterdump()) == before


async def test_binding_directory_input_accepts_empty_drafts_without_catalog(saved_pair):
    world, owned = saved_pair
    before = tuple(owned.connection.iterdump())

    empty = replace(world.request, drafts=(), catalog_facts=None)

    assert empty.drafts == () and empty.catalog_facts is None
    assert world.request.drafts == _expected_drafts(world)
    assert tuple(owned.connection.iterdump()) == before


async def test_incomplete_original_excludes_its_preview_and_keeps_other_complete_original(
        tmp_path, monkeypatch):
    world, owned, runtime, methods = await _paired_capture_world(tmp_path, monkeypatch, incomplete_a=True)
    try:
        saves = _BindingSaveSpy()
        runtime.capture = saves

        await capture_handler(world.handler)(world.action_id, runtime)

        (request, key, _owned), = saves.calls
        receipt, = saves.receipts
        assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        assert request.drafts == (OutputDraft(OutputKind.ORIGINAL, FileReference(device_file_id=world.b), True),)
        _assert_source_preserved(owned, world, methods)
        outputs = owned.connection.execute(
            "SELECT id,kind,device_file_id,original_name,media_type FROM outputs ORDER BY id").fetchall()
        assert outputs == [(1, 1, world.b, "b.mp4", "video/mp4")]
        assert owned.connection.execute("SELECT COUNT(*) FROM output_origins").fetchone() == (0,)
        assert owned.connection.execute(
            "SELECT status,cancel_requested FROM actions WHERE id=?", (world.action_id,)).fetchone() == (6, 1)
        stop_id, status, used = owned.connection.execute(
            "SELECT id,status,attempts_used FROM operation_runs WHERE responsibility_key=?",
            (f"stop/{world.action_id}",)).fetchone()
        assert (status, used) == (4, 0)
        _assert_joint_binding_finish(owned, world, key, stop_id, 1)
        terminal = tuple(owned.connection.iterdump())
        await capture_handler(world.handler)(world.action_id, runtime)
        assert tuple(owned.connection.iterdump()) == terminal
        assert len(saves.calls) == 1
    finally:
        owned.connection.close()

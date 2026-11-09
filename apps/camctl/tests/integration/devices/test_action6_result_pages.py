"""Action6 原片结果经真实驱动合同、分页历史与冻结产物规则保存。"""

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.bootstrap.capture_assembly import DriverResultListing
from camctl.bootstrap.config import ConfigDefaults, load_config
from ..persistence.result_page_fixtures import begin_result_check
from .result_page_storage import result_page_storage, completion_context, allow_dispatch, ResultPageTransport
from ..support.capture import capture_database
from camctl.capture.result_pages import ResultPageSave
from camctl.capture.results import (CaptureFile, CaptureFileSet, FileKind, ProductRequirements,
    ProductRule, RuleState, assess_capture_files)
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.bindings import DeviceBinding
from camctl.devices.catalog import DriverDefinition, DriverDefinitions, build_catalog
from camctl.devices.directory import DirectoryCursor
from camctl.devices.drivers.adb_cameras.action6_record import action6_record_contract
from camctl.devices.drivers.adb_cameras.commands import CameraModel
from camctl.devices.drivers.adb_cameras.driver import AdbCameraDriver
from camctl.devices.file_identity import FileIdentity
from camctl.devices.ports import ControlRequest
from camctl.devices.tasks import StartReturn
from camctl.operations.attempts import (AttemptConfig, AttemptFinish, AttemptIntent, AttemptTarget,
    OperationKind, RunFinish, RunOutcome)
from camctl.operations.validation import validate_outcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from ..acceptance.test_acceptance import _plan_body
from ..capture.test_baseline_history import _append, _fix
from ..capture.test_baseline_start import _grant
from ..scheduling.test_start_action import _SCHEDULED, _accept_plan, _start, owned


pytestmark = pytest.mark.asyncio
ROOT = "/mnt/media_rw/emulated/DCIM"
BINDING = DeviceBinding("cam-1", CameraModel.ACTION6)



async def _world(owned, tmp_path, monkeypatch, outputs, baseline=(), result_mode="single_file"):
    contract = action6_record_contract()
    driver_id = CameraModel.ACTION6
    params = {"type": "action6_timelapse", "interval_s": 8, "duration_s": 1800,
        "outputs": outputs, "exposure": {"mode": "auto"}}
    config = load_config({"devices": {"cam-1": {"kind": "camera", "driver": driver_id,
        "adb": {"serial": "serial-1"}, "video": {"result_mode": result_mode}}}}, ConfigDefaults())
    catalog = build_catalog(config, DriverDefinitions({driver_id: DriverDefinition(driver_id,
        {cap.action_type: (cap,) for cap in contract.capabilities()}, contract.declaration,
        source_storage_resolver=contract.source_storage_resolver)}))
    body = _plan_body()
    body["actions"][0].update(type="camera_timelapse", params=params)
    await _accept_plan(owned, tmp_path, body, catalog)
    started = _start(owned, 1, now=_SCHEDULED)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    activity = started.value.activity_id
    if baseline:
        appended = _append(owned, activity,
            tuple(FileIdentity(BINDING, path) for path in sorted(baseline)))
        assert appended.kind is DbOutcomeKind.COMPLETED, appended.error
    fixed = _fix(owned, activity, chunks=int(bool(baseline)), entries=len(baseline))
    assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
    granted = _grant(owned)
    assert granted.kind is DbOutcomeKind.COMPLETED, granted.error

    clock = [0]
    waits = []

    async def sleep(seconds):
        waits.append(seconds)
        clock[0] += int(seconds * 1_000_000_000)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    driver = AdbCameraDriver(contract, config.devices, ResultPageTransport(), terminate_grace_s=Decimal("1"),
        monotonic_ns=lambda: clock[0])
    operations = OperationRepository()
    start_ticket = granted.value.ticket
    result = await driver.control(ControlRequest("start_timelapse", BINDING, params, start_ticket,
        Decimal("90"), allow_dispatch, start_return_meaning=StartReturn.SENT))
    saved = operations.finish_attempt(AttemptFinish(start_ticket,
        validate_outcome(start_ticket, result.outcome, contract.evidence), _SCHEDULED,
        run_finish=RunFinish(RunOutcome.SUCCEEDED)), new_operation_key(), owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error

    stop = operations.begin_attempt(AttemptIntent(action_id=1, kind=OperationKind.STOP,
        target=AttemptTarget(activity_id=activity), operation="stop", query_purpose=None,
        config=AttemptConfig(1, Decimal("90")), occurred_at=_SCHEDULED + 1), new_operation_key(), owned)
    assert stop.kind is DbOutcomeKind.COMPLETED, stop.error
    stop_ticket = stop.value.ticket
    stopped = await driver.stop(ControlRequest("stop_timelapse", BINDING, {}, stop_ticket, Decimal("90")))
    saved = operations.finish_attempt(AttemptFinish(stop_ticket,
        validate_outcome(stop_ticket, stopped.outcome, contract.evidence), _SCHEDULED + 1,
        run_finish=RunFinish(RunOutcome.SUCCEEDED)), new_operation_key(), owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error

    runtime = await result_page_storage(owned, _SCHEDULED + 2)
    result_ticket = begin_result_check(owned, runtime, 1).ticket
    context = completion_context(owned, 1, kind="timelapse_stop")
    assert context["kind"] == "timelapse_stop" and context["file_completion_wait_ms"] == 3_000
    return contract, driver, runtime, result_ticket, context, waits


async def _save_pages(contract, driver, runtime, ticket, context, paths, batch=32):
    driver.transport.paths = paths
    cursor, page_no, refs = None, 1, []
    while True:
        request = ControlRequest("result", BINDING, {
            "activity_id": ticket.target_id,
            "output_scope": {"directories": [ROOT]},
            "cursor": None if cursor is None else cursor.as_json(),
            "completion_context": context,
            "video_result_mode": runtime.action(1)["execution_spec_json"]["video_result_mode"],
        }, ticket, Decimal("40"))
        result = await driver.list_results(request, batch)
        assert result.error is None, result.error
        validated = validate_outcome(ticket, result.outcome, contract.evidence)
        saved = runtime.capture.save_result_page(ResultPageSave(ticket, page_no, cursor, validated,
            _SCHEDULED + 2 + page_no), new_operation_key(), runtime.owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        refs.append(saved.value)
        completion = result.outcome.settlement.evidence.data["file_completion"]
        if context["prior_completion"] is None:
            context["prior_completion"] = {"result_page_event_id": saved.value.event_id,
                "evidence": completion}
        cursor = saved.value.next_cursor
        if cursor is None:
            break
        page_no += 1
    pages = runtime.capture.read_result_pages(ticket, None, 128, runtime.owned).items
    return pages, refs


def _assess_saved_pages(runtime, pages):
    action = runtime.action(1)
    rules = tuple(ProductRule.from_json(value)
        for value in action["execution_spec_json"]["product_rules"])
    files = []
    for saved in pages:
        identities = dict(saved.file_ids)
        for entry in saved.page.entries:
            file_id = identities.get(entry.identity)
            if file_id is None:
                continue
            pairing = None if entry.paired_identity is None else entry.paired_identity in {
                identity for page in pages for identity, _ in page.file_ids}
            files.append(CaptureFile(str(file_id), entry.kind, entry.complete, True,
                format_id=entry.format_id, pairing_confirmed=pairing, size_bytes=entry.size_bytes))
    return assess_capture_files(CaptureFileSet(tuple(files), pages[-1].page.set_finalized),
        ProductRequirements(frozenset({FileKind.VIDEO}), rules))


async def test_action6_pages_register_many_jpegs_and_cross_page_mp4_lrf_pair(
        owned, tmp_path, monkeypatch):
    historical = ROOT + "/a/000-old.JPG"
    contract, driver, runtime, ticket, context, waits = await _world(
        owned, tmp_path, monkeypatch, outputs="video_jpeg", baseline=(historical,))
    photos = [ROOT + f"/b/{index:03}.JPG" for index in range(126)]
    preview, video = ROOT + "/z/Clip.LRF", ROOT + "/z/Clip.MP4"
    paths = [historical, *photos, preview, video]
    current_paths = [*photos, preview, video]
    pages, _ = await _save_pages(contract, driver, runtime, ticket, context, paths)

    entries = [entry for saved in pages for entry in saved.page.entries]
    assert [entry.identity for entry in entries] == sorted(paths)
    assert waits == [3.0]
    assert pages[0].page.outcome.settlement.evidence.data["file_completion_source_page_event_id"] is None
    assert all(saved.page.outcome.settlement.evidence.data["file_completion_source_page_event_id"]
        == pages[0].ref.event_id for saved in pages[1:])
    assert len(pages) == 5 and pages[-1].page.set_finalized is True
    preview_page = next(saved for saved in pages
        if any(entry.identity == preview for entry in saved.page.entries))
    video_page = next(saved for saved in pages
        if any(entry.identity == video for entry in saved.page.entries))
    preview_entry = next(entry for entry in preview_page.page.entries if entry.identity == preview)
    assert preview_page.ref.page_no + 1 == video_page.ref.page_no
    assert preview_entry.paired_identity == video
    assert preview_page.page.outcome.settlement.evidence.version == 2
    preview_diagnostic = preview_page.page.outcome.settlement.evidence.data["optional_preview_observations"][0]
    assert preview_diagnostic["mp4_count"] == 1 and "osv_count" not in preview_diagnostic
    assert len(driver.transport.directory_calls) == len(pages)
    assert all(batch == 32 for _, _, batch in driver.transport.directory_calls)
    assert owned.connection.execute("SELECT COUNT(*) FROM device_files").fetchone() == (128,)
    assert sorted(identity for page in pages for identity, _ in page.file_ids) == sorted(current_paths)
    assert _assess_saved_pages(runtime, pages).is_complete is True


async def test_segmented_formal_adapter_pairs_only_new_files_after_complete_scan(
        owned, tmp_path, monkeypatch, request):
    from camctl.capture.files import FileCompletionSave, OwnershipFromResultPageSave, PairResultFilesSave

    baseline = tuple(ROOT + f"/A/{index:03}.JPG" for index in range(31))
    stem = ROOT + "/DJI_001/DJI_20261022100000_0001_D."
    preview, video = stem + "LRF", stem + "MP4"
    contract, driver, runtime, ticket, context, _ = await _world(
        owned, tmp_path, monkeypatch, outputs="video", baseline=baseline,
        result_mode="segmented")
    transport = driver.transport
    transport.paths = [*baseline, preview, video]
    adapter = DriverResultListing(driver, BINDING, contract.evidence,
        capture_repository=runtime.capture, database=runtime.database)
    request.addfinalizer(lambda current=adapter, original=ticket: current.close_scan(original))
    mode = runtime.action(1)["execution_spec_json"]["video_result_mode"]
    first = await adapter.list_page(ticket, cursor=None, timeout_s=Decimal("40"),
        output_scope={"directories": [ROOT]}, completion_context=context,
        video_result_mode=mode)
    saved = runtime.capture.save_result_page(ResultPageSave(
        ticket, 1, None, validate_outcome(ticket, first.outcome, contract.evidence),
        _SCHEDULED + 3), new_operation_key(), owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    context["prior_completion"] = {"result_page_event_id": saved.value.event_id,
        "evidence": first.outcome.settlement.evidence.data["file_completion"]}
    second = await adapter.list_page(ticket, cursor=first.next_cursor,
        timeout_s=Decimal("40"), output_scope={"directories": [ROOT]},
        completion_context=context, video_result_mode=mode)
    final = runtime.capture.save_result_page(ResultPageSave(
        ticket, 2, first.next_cursor,
        validate_outcome(ticket, second.outcome, contract.evidence), _SCHEDULED + 4),
        new_operation_key(), owned)
    assert final.kind is DbOutcomeKind.COMPLETED, final.error
    pages = runtime.capture.read_result_pages(ticket, None, 128, owned).items
    for page in pages:
        for entry in page.page.entries:
            file_id = dict(page.file_ids).get(entry.identity)
            if file_id is None:
                continue
            ownership = runtime.capture.save_ownership_from_result_page(
                OwnershipFromResultPageSave(page.ref, file_id, page.occurred_at),
                new_operation_key(), owned)
            assert ownership.kind is DbOutcomeKind.COMPLETED, ownership.error
            completion = runtime.capture.save_file_completion(FileCompletionSave(
                file_id, 3, page.occurred_at, basis=3, observation=entry.evidence,
                size_bytes=entry.size_bytes, activity_id=int(ticket.target_id),
                result_page_event_id=page.ref.event_id,
                stop_result_event_id=context["stop_result_event_id"]), new_operation_key(), owned)
            assert completion.kind is DbOutcomeKind.COMPLETED, completion.error
    preview_id = owned.connection.execute(
        "SELECT file_id FROM result_page_files WHERE identity=?", (preview,)).fetchone()[0]
    paired = runtime.capture.save_result_file_pairing(
        PairResultFilesSave(preview_id, final.value, _SCHEDULED + 5),
        new_operation_key(), owned)
    assert paired.kind is DbOutcomeKind.COMPLETED, paired.error
    assert len(transport.directory_calls) == 1
    assert owned.connection.execute("SELECT COUNT(*) FROM device_files").fetchone() == (2,)
    assert owned.connection.execute(
        "SELECT p.original_name,o.original_name FROM device_file_pairing_facts f"
        " JOIN device_files p ON p.id=f.preview_file_id"
        " JOIN device_files o ON o.id=f.original_file_id"
    ).fetchall() == [(preview.rsplit("/", 1)[1], video.rsplit("/", 1)[1])]


async def test_action6_dng_stays_dng_and_missing_frozen_jpeg_rule_is_explicit(
        owned, tmp_path, monkeypatch):
    contract, driver, runtime, ticket, context, waits = await _world(
        owned, tmp_path, monkeypatch, outputs="video_jpeg")
    dng, video = ROOT + "/A/Frame.DNG", ROOT + "/A/clip.MP4"
    pages, _ = await _save_pages(contract, driver, runtime, ticket, context,
        [dng, video])
    assert waits == [3.0]

    entries = [entry for saved in pages for entry in saved.page.entries]
    assert {entry.identity: (entry.kind, entry.format_id) for entry in entries} == {
        video: (FileKind.VIDEO, "mp4"), dng: (FileKind.PHOTO, "dng")}
    assessment = _assess_saved_pages(runtime, pages)
    assert assessment.is_complete is False and assessment.explicitly_unmet is True
    assert assessment.rules[0].state is RuleState.PASSED
    assert assessment.rules[1].state is RuleState.FAILED and assessment.rules[1].count == 0
    assert owned.connection.execute("SELECT COUNT(*) FROM device_files").fetchone() == (2,)


async def test_formal_result_adapter_rejects_lrf_after_saved_original_disappears(
        owned, tmp_path, monkeypatch, capture_resources, request):
    """可靠前页 MP4 在后页配对扫描前消失时，不能最终化孤立 LRF。"""
    # Action6 将驱动目录批量上限固定为 32：31 张照片后，MP4 正好成为页尾。
    photos = [ROOT + f"/a/{index:03}.JPG" for index in range(31)]
    video, preview, tail_photo = (ROOT + "/z/Clip.MP4", ROOT + "/z/Clip.lrf", ROOT + "/z/Z.JPG")
    contract, driver, runtime, ticket, context, waits = await _world(
        owned, tmp_path, monkeypatch, outputs="video_jpeg")
    transport = driver.transport
    transport.paths = [*photos, video, preview, tail_photo]
    database = Path(owned.connection.execute("PRAGMA database_list").fetchone()[2])
    from camctl.capture.result_reading import ResultReadingContext
    reading_context = ResultReadingContext(ticket)
    try:
        adapter = DriverResultListing(driver, BINDING, contract.evidence,
            capture_repository=runtime.capture, database=runtime.database)
        request.addfinalizer(lambda current=adapter, original=ticket: current.close_scan(original))
        output_scope = runtime.action(1)["execution_spec_json"]["output_scope"]

        first = await adapter.list_page(ticket, cursor=None, timeout_s=Decimal("40"),
            output_scope=output_scope, completion_context=context, reading_context=reading_context,
            video_result_mode=runtime.action(1)["execution_spec_json"]["video_result_mode"])
        first_save = runtime.capture.save_result_page(ResultPageSave(ticket, 1, None,
            validate_outcome(ticket, first.outcome, contract.evidence), _SCHEDULED + 3),
            new_operation_key(), owned)
        assert first_save.kind is DbOutcomeKind.COMPLETED, first_save.error
        assert first.next_cursor == DirectoryCursor(BINDING, (ROOT,), 0, video)
        context["prior_completion"] = {"result_page_event_id": first_save.value.event_id,
            "evidence": first.outcome.settlement.evidence.data["file_completion"]}

        # 主游标当前位于 MP4 后面；设备目录中该原片随后消失。
        transport.paths = [*photos, preview, tail_photo]
        owned.connection.close()
        reopened = open_existing(database, DbOpenMode.EXISTING_RW, DbConfig())
        try:
            # 数据库观察连接重开后，原扫描仍持有同一清单；前页依据从可靠历史重建。
            adapter = DriverResultListing(driver, BINDING, contract.evidence,
                capture_repository=runtime.capture, database=await capture_resources.restart_database(reopened))
            request.addfinalizer(lambda current=adapter, original=ticket: current.close_scan(original))
            second = await adapter.list_page(ticket, cursor=first.next_cursor, timeout_s=Decimal("40"),
                output_scope=output_scope, completion_context=context, reading_context=reading_context,
                video_result_mode="single_file")

            page = next(value.data for value in second.outcome.observations
                if value.type == "result_files_listed")
            diagnostic = second.outcome.settlement.evidence.data["optional_preview_observations"][0]
            assert second.outcome.error.code == "action6_result_conflict"
            assert page["set_finalized"] is False
            # Action6 将 LRF 作为配对诊断读取，不把孤立预览加入原片结果页。
            assert [(entry["identity"], entry["format_id"]) for entry in page["entries"]] == [
                (tail_photo, "jpeg")]
            assert diagnostic["state"] == "original_missing"
            assert diagnostic["original_identity"] == video
            second_save = runtime.capture.save_result_page(ResultPageSave(ticket, 2, first.next_cursor,
                validate_outcome(ticket, second.outcome, contract.evidence), _SCHEDULED + 4),
                new_operation_key(), reopened)
            assert second_save.kind is DbOutcomeKind.COMPLETED, second_save.error
            stored = runtime.capture.read_result_pages(ticket, None, 8, reopened).items
            assert stored[-1].page.outcome.error.code == "action6_result_conflict"
            assert [entry.identity for entry in stored[-1].page.entries] == [tail_photo]
            assert waits == [3.0]
        finally:
            reopened.connection.close()
    finally:
        reading_context.close()


async def test_formal_result_adapter_does_not_treat_baseline_original_as_new_history(
        owned, tmp_path, monkeypatch, request):
    photos = [ROOT + f"/a/{index:03}.JPG" for index in range(31)]
    video, preview, tail_photo = (ROOT + "/z/Clip.MP4", ROOT + "/z/Clip.lrf", ROOT + "/z/Z.JPG")
    contract, driver, runtime, ticket, context, _ = await _world(
        owned, tmp_path, monkeypatch, outputs="video_jpeg", baseline=(video,))
    transport = driver.transport
    transport.paths = [*photos, video, preview, tail_photo]
    adapter = DriverResultListing(driver, BINDING, contract.evidence,
        capture_repository=runtime.capture, database=runtime.database)
    request.addfinalizer(lambda current=adapter, original=ticket: current.close_scan(original))
    scope = runtime.action(1)["execution_spec_json"]["output_scope"]
    first = await adapter.list_page(ticket, cursor=None, timeout_s=Decimal("40"),
        output_scope=scope, completion_context=context,
        video_result_mode=runtime.action(1)["execution_spec_json"]["video_result_mode"])
    saved = runtime.capture.save_result_page(ResultPageSave(ticket, 1, None,
        validate_outcome(ticket, first.outcome, contract.evidence), _SCHEDULED + 3),
        new_operation_key(), owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    assert video in {entry.identity for entry in first.entries}
    first_page = runtime.capture.read_result_page(saved.value, owned)
    assert video not in dict(first_page.file_ids)
    assert first.next_cursor == DirectoryCursor(BINDING, (ROOT,), 0, video)
    context["prior_completion"] = {"result_page_event_id": saved.value.event_id,
        "evidence": first.outcome.settlement.evidence.data["file_completion"]}

    transport.paths = [*photos, preview, tail_photo]
    second = await adapter.list_page(ticket, cursor=first.next_cursor, timeout_s=Decimal("40"),
        output_scope=scope, completion_context=context,
        video_result_mode=runtime.action(1)["execution_spec_json"]["video_result_mode"])

    observation = second.outcome.settlement.evidence.data["optional_preview_observations"][0]
    page = next(value.data for value in second.outcome.observations
        if value.type == "result_files_listed")
    assert second.outcome.error is None
    assert page["set_finalized"] is True
    assert observation["state"] == "missing_pair"
    assert observation["mp4_count"] == 0


async def test_formal_result_adapter_rejects_a_forged_cursor_before_device_dispatch(
        owned, tmp_path, monkeypatch, request):
    photos = [ROOT + f"/a/{index:03}.JPG" for index in range(31)]
    video = ROOT + "/z/Clip.MP4"
    contract, driver, runtime, ticket, context, _ = await _world(
        owned, tmp_path, monkeypatch, outputs="video_jpeg")
    transport = driver.transport
    transport.paths = [*photos, video, ROOT + "/z/Z.JPG"]
    adapter = DriverResultListing(driver, BINDING, contract.evidence,
        capture_repository=runtime.capture, database=runtime.database)
    request.addfinalizer(lambda current=adapter, original=ticket: current.close_scan(original))
    scope = runtime.action(1)["execution_spec_json"]["output_scope"]
    first = await adapter.list_page(ticket, cursor=None, timeout_s=Decimal("40"),
        output_scope=scope, completion_context=context,
        video_result_mode=runtime.action(1)["execution_spec_json"]["video_result_mode"])
    saved = runtime.capture.save_result_page(ResultPageSave(ticket, 1, None,
        validate_outcome(ticket, first.outcome, contract.evidence), _SCHEDULED + 3),
        new_operation_key(), owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    calls_before = len(transport.directory_calls)

    with pytest.raises(ConsistencyError):
        await adapter.list_page(ticket, cursor=None, timeout_s=Decimal("40"),
            output_scope=scope, completion_context=context,
            video_result_mode=runtime.action(1)["execution_spec_json"]["video_result_mode"])
    forged = DirectoryCursor(BINDING, (ROOT,), 0, ROOT + "/z/Other.MP4")
    with pytest.raises(ConsistencyError):
        await adapter.list_page(ticket, cursor=forged, timeout_s=Decimal("40"),
            output_scope=scope, completion_context=context,
            video_result_mode=runtime.action(1)["execution_spec_json"]["video_result_mode"])
    foreign_binding = DirectoryCursor(DeviceBinding("cam-1", "other-driver"),
        (ROOT,), 0, ROOT + "/z/Clip.MP4")
    with pytest.raises(ConsistencyError):
        await adapter.list_page(ticket, cursor=foreign_binding, timeout_s=Decimal("40"),
            output_scope=scope, completion_context=context,
            video_result_mode=runtime.action(1)["execution_spec_json"]["video_result_mode"])
    foreign_scope = {"directories": [ROOT + "/other"]}
    with pytest.raises(ConsistencyError):
        await adapter.list_page(ticket, cursor=first.next_cursor, timeout_s=Decimal("40"),
            output_scope=foreign_scope, completion_context=context,
            video_result_mode=runtime.action(1)["execution_spec_json"]["video_result_mode"])

    assert len(transport.directory_calls) == calls_before


async def test_formal_result_adapter_rejects_later_cursor_without_saved_history(
        owned, tmp_path, monkeypatch, request):
    contract, driver, runtime, ticket, context, _ = await _world(
        owned, tmp_path, monkeypatch, outputs="video_jpeg")
    transport = driver.transport
    transport.paths = [ROOT + "/z/Clip.MP4"]
    adapter = DriverResultListing(driver, BINDING, contract.evidence,
        capture_repository=runtime.capture, database=runtime.database)
    request.addfinalizer(lambda current=adapter, original=ticket: current.close_scan(original))
    scope = runtime.action(1)["execution_spec_json"]["output_scope"]
    cursor = DirectoryCursor(BINDING, (ROOT,), 0, ROOT + "/z/Previous.MP4")

    with pytest.raises(ConsistencyError):
        await adapter.list_page(ticket, cursor=cursor, timeout_s=Decimal("40"),
            output_scope=scope, completion_context=context,
            video_result_mode=runtime.action(1)["execution_spec_json"]["video_result_mode"])

    assert transport.directory_calls == []


async def test_formal_result_adapter_does_not_read_previous_attempt_history(
        owned, tmp_path, monkeypatch, request):
    photos = [ROOT + f"/a/{index:03}.JPG" for index in range(31)]
    video = ROOT + "/z/Clip.MP4"
    contract, driver, runtime, first_ticket, context, _ = await _world(
        owned, tmp_path, monkeypatch, outputs="video_jpeg")
    transport = driver.transport
    transport.paths = [*photos, video, ROOT + "/z/Clip.lrf"]
    adapter = DriverResultListing(driver, BINDING, contract.evidence,
        capture_repository=runtime.capture, database=runtime.database)
    request.addfinalizer(lambda current=adapter, original=first_ticket: current.close_scan(original))
    scope = runtime.action(1)["execution_spec_json"]["output_scope"]
    first = await adapter.list_page(first_ticket, cursor=None, timeout_s=Decimal("40"),
        output_scope=scope, completion_context=context,
        video_result_mode=runtime.action(1)["execution_spec_json"]["video_result_mode"])
    saved = runtime.capture.save_result_page(ResultPageSave(first_ticket, 1, None,
        validate_outcome(first_ticket, first.outcome, contract.evidence), _SCHEDULED + 3),
        new_operation_key(), owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    assert first.next_cursor == DirectoryCursor(BINDING, (ROOT,), 0, video)
    finished = OperationRepository().finish_attempt(AttemptFinish(first_ticket,
        validate_outcome(first_ticket, first.outcome, contract.evidence), _SCHEDULED + 4,
        retry_wait=True), new_operation_key(), owned)
    assert finished.kind is DbOutcomeKind.COMPLETED, finished.error
    second_ticket = begin_result_check(owned, runtime, 1).ticket
    request.addfinalizer(lambda current=adapter, original=second_ticket: current.close_scan(original))
    assert second_ticket.attempt_id == first_ticket.attempt_id + 1
    calls_before = len(transport.directory_calls)

    with pytest.raises(ConsistencyError):
        await adapter.list_page(second_ticket, cursor=first.next_cursor, timeout_s=Decimal("40"),
            output_scope=scope, completion_context=context,
            video_result_mode=runtime.action(1)["execution_spec_json"]["video_result_mode"])

    assert len(transport.directory_calls) == calls_before


async def test_formal_result_adapter_rejects_continuation_after_failed_saved_page(
        owned, tmp_path, monkeypatch, request):
    failed_photo = ROOT + "/a/000.JPG"
    photos = [ROOT + f"/a/{index:03}.JPG" for index in range(31)]
    video = ROOT + "/z/Clip.MP4"
    contract, driver, runtime, ticket, context, _ = await _world(
        owned, tmp_path, monkeypatch, outputs="video_jpeg")
    transport = driver.transport
    transport.paths = [*photos, video, ROOT + "/z/Z.JPG"]
    transport.metadata_errors = {failed_photo}
    adapter = DriverResultListing(driver, BINDING, contract.evidence,
        capture_repository=runtime.capture, database=runtime.database)
    request.addfinalizer(lambda current=adapter, original=ticket: current.close_scan(original))
    scope = runtime.action(1)["execution_spec_json"]["output_scope"]
    first = await adapter.list_page(ticket, cursor=None, timeout_s=Decimal("40"),
        output_scope=scope, completion_context=context,
        video_result_mode=runtime.action(1)["execution_spec_json"]["video_result_mode"])
    assert first.outcome.error.code == "adb_metadata_failed"
    error, = first.outcome.error.details["item_errors"]
    assert (error["identity"], error["remote_exit_code"], error["stderr_hex"]) == (
        failed_photo, 1, b"metadata failed".hex())
    assert first.next_cursor == DirectoryCursor(BINDING, (ROOT,), 0, video)
    saved = runtime.capture.save_result_page(ResultPageSave(ticket, 1, None,
        validate_outcome(ticket, first.outcome, contract.evidence), _SCHEDULED + 3),
        new_operation_key(), owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    calls_before = len(transport.directory_calls)

    with pytest.raises(ConsistencyError):
        await adapter.list_page(ticket, cursor=first.next_cursor, timeout_s=Decimal("40"),
            output_scope=scope, completion_context=context,
            video_result_mode=runtime.action(1)["execution_spec_json"]["video_result_mode"])

    assert len(transport.directory_calls) == calls_before

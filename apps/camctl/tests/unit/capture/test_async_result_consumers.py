"""拍摄消费者按异步流读取，设备双文件仍形成有限草稿。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from camctl.capture.handlers import _catalog_drafts
from camctl.capture.host_photo import _device_drafts
from camctl.capture.host_photo import _persist_outputs
from camctl.capture.result_scans import assess_registered_files
from camctl.capture.results import CaptureFile, FileKind, ProductRequirements
from camctl.devices.drivers.hasselblad.files import CameraFileLocator, CameraLocatorStatus
from camctl.host_files.capture_sources import CapturedArtifact

pytestmark = pytest.mark.asyncio


async def test_registered_assessment_consumes_async_records():
    async def registered():
        yield CaptureFile("one", FileKind.VIDEO, True, True), 29

    result = await assess_registered_files(registered(),
        ProductRequirements(required_kinds=frozenset({FileKind.VIDEO})), set_finalized=True)

    assert result.is_complete


async def test_catalog_makes_two_passes_over_finite_registered_records():
    original = SimpleNamespace(identity="original", complete=True, paired_identity=None,
        kind=FileKind.VIDEO, format_id=None, size_bytes=100)
    preview = SimpleNamespace(identity="preview", complete=True, paired_identity="original",
        kind=FileKind.VIDEO, format_id=None, size_bytes=30)

    result = await _catalog_drafts(((None, 29), (None, 30)), (original, preview))

    assert isinstance(result, tuple)
    assert tuple(item.file.device_file_id for item in result) == (29, 30)
    assert result[1].original_batch_file_id == 29


async def test_device_photo_drafts_register_finite_pair_with_one_activity_read(mocker):
    runtime = SimpleNamespace(action=AsyncMock(return_value={
        "device_id": "camera", "driver_id": "hasselblad_cfv100c"}))
    artifacts = (CapturedArtifact("photo.3FR", 100, "a" * 64, "image/x-hasselblad-3fr"),
        CapturedArtifact("photo.3FR.thumbnail.jpg", 20, "b" * 64, "image/jpeg"))
    activity = mocker.patch("camctl.capture.host_photo._activity_id", autospec=True, return_value=7)
    mocker.patch("camctl.capture.host_photo._capture_locator", return_value=CameraFileLocator(
        CameraLocatorStatus.PATH, "/DCIM/photo.3FR", "photo.3FR"))
    register = mocker.patch("camctl.capture.handlers._register_observed", autospec=True,
        return_value=((None, 29), (None, 30)))

    outcome = SimpleNamespace(observations=(SimpleNamespace(type="photo_taken", data={
        "steps": [{"name": "capture", "camera_identity": {"serial": "SERIAL", "product_id": 10}}],
    }),))
    result = await _device_drafts(runtime, 21, artifacts, 1000, outcome)

    entries = register.call_args.args[2]
    assert isinstance(entries, tuple) and len(entries) == 2
    assert tuple(entry.evidence["activity_id"] for entry in entries) == ("7", "7")
    assert all((entry.locator["serial"], entry.locator["product_id"]) == ("SERIAL", 10)
               for entry in entries)
    assert entries[1].paired_identity == entries[0].identity
    assert isinstance(result, tuple) and len(result) == 2
    activity.assert_awaited_once_with(runtime, 21)


async def test_deferred_photo_drafts_use_start_identity_without_content_metadata(mocker):
    from camctl.capture.host_photo import SavedPhotoReturn, _deferred_device_drafts
    from camctl.operations.models import AttemptTicket

    runtime = SimpleNamespace(action=AsyncMock(return_value={
        "device_id": "camera", "driver_id": "hasselblad_cfv100c"}))
    register = mocker.patch("camctl.capture.handlers._register_observed", autospec=True,
        return_value=((None, 29), (None, 30)))
    saved = SavedPhotoReturn(AttemptTicket(1, "control", "7", "start/21", 3), 2, 1, 1000,
                             {}, None, result_event_id=11)
    observation = SimpleNamespace(data={"activity_id": "7",
        "camera_identity": {"serial": "SERIAL", "product_id": 10},
        "camera_locator": {"status": "path", "camera_path": "/DCIM/photo.3FR", "camera_name": "photo.3FR"}})

    drafts = await _deferred_device_drafts(runtime, 21, saved, observation)

    entries = register.call_args.args[2]
    assert len(entries) == 2 and entries[1].paired_identity == entries[0].identity
    assert all(entry.complete and entry.size_bytes is None and entry.media_type is None for entry in entries)
    assert [entry.evidence["photo_start"]["role"] for entry in entries] == ["original", "thumbnail"]
    assert all(entry.evidence["photo_start"]["start_result_event_id"] == 11 for entry in entries)
    assert [draft.file.device_file_id for draft in drafts] == [29, 30]
    assert all(draft.sha256 is None for draft in drafts)


async def test_device_photo_reads_cancellation_after_registering_output_facts(mocker):
    from camctl.persistence.repositories.capture import FinishCanceledCapture

    before = {"type": 1, "cancel_requested": 0, "execution_spec_json": {"output_storage": "device"}}
    after = {**before, "cancel_requested": 1}
    runtime = SimpleNamespace(action=AsyncMock(side_effect=(before, after)),
        save_capture_completion=AsyncMock())
    drafts = ()
    register = mocker.patch("camctl.capture.host_photo._device_drafts", autospec=True, return_value=drafts)

    await _persist_outputs(runtime, 21, (), (), 1000, object())

    register.assert_awaited_once()
    command = runtime.save_capture_completion.call_args.args[0]
    assert isinstance(command, FinishCanceledCapture)
    assert command.occurred_at == 1000 and command.drafts == drafts

"""拍摄消费者按异步流读取，设备双文件仍形成有限草稿。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from camctl.capture.handlers import _catalog_drafts
from camctl.capture.host_photo import _device_drafts
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

    result = await _device_drafts(runtime, 21, artifacts, 1000, object())

    entries = register.call_args.args[2]
    assert isinstance(entries, tuple) and len(entries) == 2
    assert tuple(entry.evidence["activity_id"] for entry in entries) == ("7", "7")
    assert entries[1].paired_identity == entries[0].identity
    assert isinstance(result, tuple) and len(result) == 2
    activity.assert_awaited_once_with(runtime, 21)

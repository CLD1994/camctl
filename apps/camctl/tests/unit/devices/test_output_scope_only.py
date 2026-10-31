"""已停止任务的完整路径范围不依赖文件元数据。"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from camctl.contracts.pages import Page
from camctl.devices.bindings import DeviceBinding
from camctl.devices.directory import DirectoryRead
from camctl.devices.file_identity import FileIdentity
from camctl.devices.ports import ControlRequest
from camctl.operations.models import AttemptStatus, AttemptTicket, ErrorValue
from camctl.operations.process import LocalExit, RawToolOutcome

from .test_osmo360ii_results import _stop


@pytest.mark.asyncio
@pytest.mark.parametrize("driver_id,extension", [("dji-action6", "MP4"), ("dji-osmo360-ii", "OSV")])
@pytest.mark.parametrize("mode", ["single_file", "segmented"])
async def test_scope_page_preserves_paths_without_metadata_calls(mocker, driver_id, extension, mode):
    from camctl.devices.drivers.adb_cameras.action6_results import list_action6_results
    from camctl.devices.drivers.adb_cameras.osmo360ii_results import list_osmo360ii_results

    binding = DeviceBinding("camera", driver_id)
    root = "/mnt/media_rw/emulated/DCIM"
    paths = (f"{root}/CAM_001/clip.LRF", f"{root}/CAM_001/clip.{extension}")
    clock, waits = [0], []

    async def sleep(seconds):
        waits.append(seconds)
        clock[0] += int(Decimal(str(seconds)) * 1_000_000_000)

    mocker.patch("asyncio.sleep", side_effect=sleep)
    raw = RawToolOutcome(LocalExit(exit_code=0), b"directory completed", None, None, stderr=b"")
    filesystem = SimpleNamespace(read_directory=AsyncMock(return_value=DirectoryRead(raw, None,
        Page(tuple(FileIdentity(binding, path) for path in paths), None))),
        metadata=AsyncMock(side_effect=AssertionError("范围扫描不能查询大小")),
        metadata_many=AsyncMock(side_effect=AssertionError("范围扫描不能查询大小")))
    driver = SimpleNamespace(_filesystem=lambda actual: filesystem, monotonic_ns=lambda: clock[0])
    params = {"activity_id": "7", "output_scope": {"directories": [root]},
        "video_result_mode": mode, "completion_context": _stop(), "scope_only": True}
    ticket = AttemptTicket(1, "result", "7", "scope/7", 9)
    reader = list_action6_results if driver_id == "dji-action6" else list_osmo360ii_results

    result = await reader(driver, ControlRequest("result", binding, params, ticket, Decimal("40")), 32)

    assert result.outcome.status is AttemptStatus.SUCCEEDED
    page = result.observations[0].data
    assert page["set_finalized"] is True and page["next_cursor"] is None
    assert tuple(entry["identity"] for entry in page["entries"]) == paths
    assert all(entry["size_bytes"] is None and entry["complete"] is False for entry in page["entries"])
    assert waits == [30.0]
    filesystem.metadata.assert_not_awaited()
    filesystem.metadata_many.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_directory_does_not_prove_scope():
    from camctl.devices.drivers.adb_cameras.osmo360ii_results import list_osmo360ii_results

    error = ErrorValue("adb_directory_failed", "device_file_access", {"reason": "timeout"})
    filesystem = SimpleNamespace(read_directory=AsyncMock(return_value=DirectoryRead(None, error, None)))
    driver = SimpleNamespace(_filesystem=lambda binding: filesystem, monotonic_ns=lambda: 0)
    request = ControlRequest("result", DeviceBinding("camera", "dji-osmo360-ii"),
        {"activity_id": "7", "output_scope": {"directories": ["/mnt/media_rw/emulated/DCIM"]},
         "video_result_mode": "segmented", "scope_only": True},
        AttemptTicket(1, "result", "7", "scope/7", 9), Decimal("10"))

    result = await list_osmo360ii_results(driver, request, 32)

    assert result.outcome.error is error
    assert not result.observations

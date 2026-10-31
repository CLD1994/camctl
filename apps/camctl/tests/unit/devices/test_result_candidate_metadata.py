"""结果保留路径页，只为主机选出的新增身份读取大小和核对配对。"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec

import pytest

from camctl.contracts.pages import Page
from camctl.devices.directory import DirectoryCursor, DirectoryRead
from camctl.devices.drivers.adb_cameras.action6_results import list_action6_results
from camctl.devices.drivers.adb_cameras.osmo360ii_results import list_osmo360ii_results
from camctl.devices.drivers.adb_cameras.filesystem import AdbFilesystem, FileMetadataBatch, FileMetadataItem
from camctl.devices.file_identity import FileIdentity
from camctl.devices.ports import ControlRequest
from camctl.operations.models import ErrorValue
from .test_osmo360ii_results import BINDING, ROOT, TICKET, _natural


@pytest.mark.asyncio
@pytest.mark.parametrize("read_results,suffix", [(list_action6_results, ".mp4"), (list_osmo360ii_results, ".osv")])
@pytest.mark.parametrize("mode", ["single_file", "segmented"])
async def test_136_paths_only_query_ten_new_sizes_and_preserve_all_old_entries(read_results, suffix, mode):
    filesystem = create_autospec(AdbFilesystem, instance=True)
    paths = tuple(FileIdentity(BINDING, f"{ROOT}/{index:04}{suffix}") for index in range(136))
    new = paths[::14]
    directory_calls = []

    async def directory_reader(actual_filesystem, request, *, stop):
        assert actual_filesystem is filesystem
        offset = 0 if request.cursor is None else paths.index(FileIdentity(BINDING, request.cursor.after_path)) + 1
        items = paths[offset:offset + request.batch]
        next_cursor = (DirectoryCursor(BINDING, (ROOT,), 0, items[-1].path)
            if offset + len(items) < len(paths) else None)
        directory_calls.append(request)
        return DirectoryRead(None, None, Page(items, next_cursor), call_performed=offset == 0)

    async def metadata(identities, *, timeout_s, stop):
        return FileMetadataBatch(None, tuple(FileMetadataItem(identity, 11, None) for identity in identities), None)

    filesystem.metadata_many.side_effect = metadata
    filesystem.read_directory.side_effect = AssertionError("结果读取须复用同尝试的目录清单")
    driver = SimpleNamespace(_filesystem=lambda binding: filesystem, monotonic_ns=lambda: 0)
    cursor, entries, call_counts = None, [], []
    while True:
        request = ControlRequest("result", BINDING,
            {"activity_id": "7", "output_scope": {"directories": [ROOT]},
                "completion_context": _natural(), "video_result_mode": mode, "cursor": cursor},
            TICKET, Decimal("40"), result_candidates=AsyncMock(side_effect=lambda identities: tuple(identity for identity in identities if identity in new)),
            result_directory_reader=directory_reader)
        result = await read_results(driver, request, 32)
        assert result.error is None
        page = result.observations[0].data
        entries.extend(page["entries"])
        call_counts.append(result.outcome.settlement.evidence.data["call_count"])
        cursor = page["next_cursor"]
        if cursor is None:
            assert page["set_finalized"]
            break

    assert [entry["identity"] for entry in entries] == [identity.path for identity in paths]
    assert [entry["identity"] for entry in entries if entry["complete"]] == [identity.path for identity in new]
    assert all(entry["size_bytes"] is None and not entry["complete"]
        for entry in entries if entry["identity"] not in {identity.path for identity in new})
    assert tuple(identity for call in filesystem.metadata_many.call_args_list for identity in call.args[0]) == new
    assert sum(call_counts) == 1 + filesystem.metadata_many.await_count
    filesystem.read_directory.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("read_results,suffix", [(list_action6_results, ".mp4"), (list_osmo360ii_results, ".osv")])
async def test_old_preview_is_not_rescanned_and_new_preview_still_pairs(read_results, suffix):
    filesystem = create_autospec(AdbFilesystem, instance=True)
    old_preview, old_original, preview, original = tuple(FileIdentity(BINDING, ROOT + path)
        for path in ("/old.LRF", "/old" + suffix, "/new.LRF", "/new" + suffix))
    paths = tuple(sorted((old_preview, old_original, preview, original), key=lambda identity: identity.sort_key))
    filesystem.read_directory.return_value = DirectoryRead(None, None, Page(paths, None))
    filesystem.metadata_many.return_value = FileMetadataBatch(None, (FileMetadataItem(original, 11, None),), None)
    filesystem.read_family.return_value = SimpleNamespace(outcome=None, error=None, value=(preview, original))
    filesystem.metadata.return_value = SimpleNamespace(outcome=None, error=None, value=5)
    request = ControlRequest("result", BINDING, {"activity_id": "7", "output_scope": {"directories": [ROOT]},
        "completion_context": _natural(), "video_result_mode": "single_file"}, TICKET, Decimal("40"),
        result_candidates=AsyncMock(side_effect=lambda identities: tuple(identity for identity in identities
            if identity in (preview, original))))
    driver = SimpleNamespace(_filesystem=lambda binding: filesystem, monotonic_ns=lambda: 0)

    result = await read_results(driver, request, 32)

    assert result.error is None and result.observations[0].data["set_finalized"]
    entries = {entry["identity"]: entry for entry in result.observations[0].data["entries"]}
    assert entries[original.path]["complete"] and entries[original.path]["size_bytes"] == 11
    assert entries[preview.path]["complete"] and entries[preview.path]["paired_identity"] == original.path
    assert not entries[old_original.path]["complete"] and old_preview.path not in entries
    assert filesystem.read_family.await_args.args == (preview,)
    filesystem.read_family.assert_awaited_once()
    assert filesystem.metadata_many.await_args.args == ((original,),)
    assert filesystem.metadata.await_args.args == (preview,)


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [False, True])
async def test_old_or_empty_page_skips_metadata_and_can_finalize(empty):
    filesystem = create_autospec(AdbFilesystem, instance=True)
    identity = FileIdentity(BINDING, ROOT + "/old.osv")
    filesystem.read_directory.return_value = DirectoryRead(None, None, Page(() if empty else (identity,), None))
    driver = SimpleNamespace(_filesystem=lambda binding: filesystem, monotonic_ns=lambda: 0)
    request = ControlRequest("result", BINDING, {"activity_id": "7", "output_scope": {"directories": [ROOT]},
        "completion_context": _natural(), "video_result_mode": "segmented"}, TICKET, Decimal("40"),
        result_candidates=AsyncMock(return_value=()))

    result = await list_osmo360ii_results(driver, request, 32)

    assert result.error is None and result.observations[0].data["set_finalized"]
    assert len(result.observations[0].data["entries"]) == (0 if empty else 1)
    filesystem.metadata_many.assert_not_awaited()


@pytest.mark.asyncio
async def test_metadata_failure_retains_successful_new_file_and_unknown_old_and_failed_files():
    filesystem = create_autospec(AdbFilesystem, instance=True)
    old, good, failed = tuple(FileIdentity(BINDING, ROOT + f"/{index}.osv") for index in range(3))
    filesystem.read_directory.return_value = DirectoryRead(None, None, Page((old, good, failed), None))
    error = ErrorValue("adb_metadata_failed", "device_file_access", {"reason": "call_failed"})
    filesystem.metadata_many.return_value = FileMetadataBatch(None, (
        FileMetadataItem(good, 11, None), FileMetadataItem(failed, None, error)), error)
    request = ControlRequest("result", BINDING, {"activity_id": "7", "output_scope": {"directories": [ROOT]},
        "completion_context": _natural(), "video_result_mode": "segmented"}, TICKET, Decimal("40"),
        result_candidates=AsyncMock(side_effect=lambda identities: identities[1:]))
    driver = SimpleNamespace(_filesystem=lambda binding: filesystem, monotonic_ns=lambda: 0)

    result = await list_osmo360ii_results(driver, request, 32)

    page = result.observations[0].data
    assert result.outcome.error is error and not page["set_finalized"]
    assert [(entry["size_bytes"], entry["complete"]) for entry in page["entries"]] == [
        (None, False), (11, True), (None, False)]

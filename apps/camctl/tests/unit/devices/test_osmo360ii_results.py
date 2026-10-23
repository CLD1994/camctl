"""OSV 必要事实与有界跨页 LRF 配对相互独立。"""

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest
from adb_metadata_fixtures import collect_fake_metadata_many

from camctl.contracts.pages import Page
from camctl.contracts.enums import enum_for
from camctl.devices.bindings import DeviceBinding
from camctl.devices.directory import DirectoryCursor, DirectoryRead
from camctl.devices.file_identity import FileIdentity
from camctl.devices.drivers.adb_cameras.filesystem import FileAccessRead
from camctl.devices.ports import ControlRequest
from camctl.operations.models import AttemptStatus, AttemptTicket, ErrorValue
from camctl.operations.process import LocalExit, RawToolOutcome


BINDING = DeviceBinding("camera", "dji-osmo360-ii")
ROOT = "/mnt/media_rw/emulated/DCIM"
OSV = ROOT + "/folder/Clip.OSV"
LRF = ROOT + "/folder/Clip.lRf"
TICKET = AttemptTicket(1, "result", "7", "results/7", 9)


def _response(name):
    return {"status": int(enum_for("operation_attempts.status").SUCCEEDED),
        "effect_state": int(enum_for("operation_attempts.effect_state").CONFIRMED),
        "error": None, "result": {"format_version": 1,
        "settlement": {"basis": "observed", "evidence": {"type": "control_returned" if name == "timelapse_sent"
            else "stop_returned", "version": 1, "data": {}}},
        "observations": [{"type": name, "version": 1, "data": {"activity_id": "7",
            "response_payload": "00", "confirmation_basis": "osmo360ii_command_response/v1"}}]}}


def _natural():
    return {"kind": "timelapse_natural", "activity_id": "7", "start_result_event_id": 20,
        "start_response": _response("timelapse_sent"), "sent_at_us": 1000,
        "wait_completed_event_id": 30, "wait_completed_at_us": 330001000,
        "target_duration_ms": 300000, "result_wait_margin_ms": 30000, "extra_wait_ms": 0,
        "expected_check_at_us": 330001000}


def _stop(kind="record_stop", prior=None):
    return {"kind": kind, "activity_id": "7", "stop_result_event_id": 30,
        "stop_returned_at_us": 1000, "stop_response": _response("stop_confirmed"),
        "file_completion_wait_ms": 30000, "prior_completion": prior}


def _tool_result(output, *, exit_code=0, stderr=b""):
    return RawToolOutcome(LocalExit(exit_code=exit_code), output, None, None, stderr=stderr)


class Filesystem:
    def __init__(self, paths, *, sizes=None, metadata_error=None):
        self.paths = sorted(paths)
        self.sizes = sizes or {}
        self.metadata_error = metadata_error
        self.directory_calls = []
        self.metadata_calls = []
        self.metadata_batches = []

    async def read_directory(self, request, *, stop):
        assert request.binding == BINDING and request.directories == (ROOT,)
        assert request.timeout_s > 0
        self.directory_calls.append(request)
        after = request.cursor.after_path if request.cursor is not None else None
        values = [path for path in self.paths if after is None or path > after]
        selected = values[:request.batch]
        cursor = (DirectoryCursor(BINDING, request.directories, 0, selected[-1])
                  if len(values) > len(selected) else None)
        marker = b"MORE" if cursor is not None else b"END"
        output = b"\0".join((b"CAMCTL-DIRECTORY/1", *(path.encode() for path in selected), marker, b""))
        return DirectoryRead(_tool_result(output), None,
            Page(tuple(FileIdentity(BINDING, path) for path in selected), cursor))

    async def metadata(self, identity, *, timeout_s, stop):
        assert identity.binding == BINDING and timeout_s > 0
        self.metadata_calls.append(identity)
        if identity.path == self.metadata_error:
            raw = _tool_result(b"", exit_code=1, stderr=b"metadata failed")
            return FileAccessRead(raw, None, ErrorValue("adb_metadata_failed", "device_file_access",
                {"reason": "call_failed", "transport_error": None, "output_failure": None,
                 "local_exit_code": 1, "stderr": "metadata failed"}))
        size = self.sizes.get(identity.path, 10)
        return FileAccessRead(_tool_result(f"{size}\n".encode()), size, None)

    async def metadata_many(self, identities, *, timeout_s, stop):
        self.metadata_batches.append((identities, timeout_s))
        return await collect_fake_metadata_many(self, identities, timeout_s=timeout_s, stop=stop)


async def _list(fs, *, context=None, cursor=None, batch=3, clock=None, timeout=Decimal("40"),
                scope=None, video_result_mode="segmented"):
    from camctl.devices.drivers.adb_cameras.osmo360ii_results import list_osmo360ii_results
    params = {"activity_id": "7", "output_scope": {"directories": [ROOT]} if scope is None else scope,
        "cursor": cursor}
    if video_result_mode is not None:
        params["video_result_mode"] = video_result_mode
    if context is not None:
        params["completion_context"] = context
    driver = SimpleNamespace(_filesystem=lambda binding: fs, monotonic_ns=lambda: 0 if clock is None else clock[0])
    return await list_osmo360ii_results(driver, ControlRequest("result", BINDING, params, TICKET, timeout), batch)


def _page(result):
    return result.observations[0].data


def _preview(result):
    return result.outcome.settlement.evidence.data["optional_preview_observations"]


@pytest.mark.asyncio
async def test_natural_completion_returns_raw_osv_and_lrf_entries_without_pairing(monkeypatch):
    async def forbidden_sleep(seconds):
        pytest.fail("自然完成上下文不得再次等待")
    monkeypatch.setattr(asyncio, "sleep", forbidden_sleep)
    result = await _list(Filesystem([LRF, OSV]), context=_natural())
    page = _page(result)
    assert result.error is None and page["set_finalized"] is True
    assert [(entry["identity"], entry["kind"], entry["format_id"], entry["paired_identity"])
        for entry in page["entries"]] == [(OSV, "video", "osv", None), (LRF, "video", "lrf", None)]
    assert all(entry["complete"] and entry["media_type"] is None for entry in page["entries"])
    assert page["completion_evidence"] is None
    assert result.outcome.settlement.evidence.data["file_completion"] is None


@pytest.mark.asyncio
async def test_single_file_pairs_lrf_with_matching_osv_from_scope():
    fs = Filesystem([LRF, OSV])

    result = await _list(fs, context=_natural(), video_result_mode="single_file")

    entries = {entry["identity"]: entry for entry in _page(result)["entries"]}
    assert result.error is None
    assert entries[OSV]["paired_identity"] is None
    assert entries[LRF]["paired_identity"] == OSV
    assert entries[LRF]["format_id"] == "lrf"


@pytest.mark.parametrize("video_result_mode", [None, "unknown"])
@pytest.mark.asyncio
async def test_video_result_mode_must_be_explicit_and_known(video_result_mode):
    fs = Filesystem([OSV])

    with pytest.raises(ValueError, match="video result mode"):
        await _list(fs, context=_natural(), video_result_mode=video_result_mode)


@pytest.mark.asyncio
async def test_pages_do_not_rescan_dcim_or_pair_files_from_different_pages():
    fs = Filesystem([LRF, OSV, ROOT + "/folder/zzz.tmp"])
    first = await _list(fs, context=_natural(), batch=1)
    assert [entry["identity"] for entry in _page(first)["entries"]] == [OSV]
    result = await _list(fs, context=_natural(), batch=1, cursor=_page(first)["next_cursor"])
    assert [(entry["identity"], entry["paired_identity"]) for entry in _page(result)["entries"]] == [(LRF, None)]
    assert len(fs.directory_calls) == 2
    assert all(call.batch == 1 for call in fs.directory_calls)
    assert _page(result)["set_finalized"] is False


@pytest.mark.asyncio
async def test_raw_page_preserves_all_observed_lrf_identities_including_zero_and_ambiguous_names():
    duplicate = ROOT + "/folder/Clip.LRF"
    orphan = ROOT + "/folder/Orphan.lrf"
    other_original = ROOT + "/folder/Other.OSV"
    fs = Filesystem([OSV, LRF, duplicate, orphan, other_original], sizes={LRF: 0})

    result = await _list(fs, context=_natural(), batch=16)

    page = _page(result)
    entries = {entry["identity"]: entry for entry in page["entries"]}
    assert set(entries) == {OSV, LRF, duplicate, orphan, other_original}
    assert entries[LRF]["size_bytes"] == 0 and entries[LRF]["complete"] is True
    assert all(entries[path]["format_id"] == "lrf" for path in (LRF, duplicate, orphan))
    assert all(entries[path]["paired_identity"] is None for path in entries)
    assert len(fs.directory_calls) == 1
    assert sorted(item.path for item in fs.metadata_calls) == sorted(entries)
    assert _preview(result) == []


@pytest.mark.asyncio
async def test_metadata_failure_preserves_every_file_identity_from_the_directory_page():
    other_original = ROOT + "/folder/Other.OSV"
    fs = Filesystem([OSV, LRF, other_original], metadata_error=LRF)

    result = await _list(fs, context=_natural(), batch=16)

    assert result.error is not None and result.error["code"] == "adb_metadata_failed"
    page = _page(result)
    entries = {entry["identity"]: entry for entry in page["entries"]}
    assert set(entries) == {OSV, LRF, other_original}
    assert entries[OSV]["size_bytes"] == 10 and entries[OSV]["complete"] is True
    assert entries[LRF]["size_bytes"] is None and entries[LRF]["complete"] is False
    assert entries[other_original]["size_bytes"] == 10 and entries[other_original]["complete"] is True
    assert page["set_finalized"] is False
    assert [item.path for item in fs.metadata_calls] == [OSV, LRF, other_original]


@pytest.mark.asyncio
async def test_missing_completion_preserves_raw_files_without_claiming_completion():
    result = await _list(Filesystem([OSV, LRF]))
    page = _page(result)
    assert page["set_finalized"] is False
    assert [(entry["identity"], entry["complete"], entry["paired_identity"])
        for entry in page["entries"]] == [(OSV, False, None), (LRF, False, None)]


@pytest.mark.asyncio
async def test_necessary_metadata_failure_is_failed_partial_page_not_reliable_empty():
    result = await _list(Filesystem([OSV], metadata_error=OSV), context=_natural())
    assert result.error is not None and _page(result)["set_finalized"] is False
    assert _page(result)["entries"][0]["size_bytes"] is None
    assert _page(result)["entries"][0]["complete"] is False


@pytest.mark.asyncio
async def test_zero_size_original_preserves_explicit_observed_size_for_frozen_rule():
    result = await _list(Filesystem([OSV], sizes={OSV: 0}), context=_natural())
    assert result.error is None and _page(result)["entries"][0]["size_bytes"] == 0
    assert _page(result)["entries"][0]["complete"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["record_stop", "timelapse_stop"])
async def test_stop_wait_runs_once_and_next_page_reuses_real_source(monkeypatch, kind):
    clock, waits = [0], []
    async def sleep(seconds):
        waits.append(seconds)
        clock[0] += int(seconds * 1_000_000_000)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    first = await _list(Filesystem([OSV]), context=_stop(kind), clock=clock)
    completion = first.outcome.settlement.evidence.data["file_completion"]
    assert waits == [30.0] and completion["completed"] is True
    second = await _list(Filesystem([OSV]), context=_stop(kind,
        {"result_page_event_id": 40, "evidence": completion}), clock=clock)
    assert waits == [30.0] and _page(second)["set_finalized"] is True
    assert second.outcome.settlement.evidence.data["file_completion_source_page_event_id"] == 40


@pytest.mark.asyncio
async def test_short_deadline_does_not_list_before_required_stop_wait():
    fs = Filesystem([OSV])
    result = await _list(fs, context=_stop(), timeout=Decimal("10"))
    assert result.error is not None and result.observations == () and fs.directory_calls == []


@pytest.mark.asyncio
async def test_one_page_does_not_scan_unrelated_later_directory_entries():
    paths = [OSV, LRF] + [ROOT + f"/zzz/{index:04}.tmp" for index in range(200)]
    fs = Filesystem(paths)
    result = await _list(fs, context=_natural(), batch=1,
        cursor=DirectoryCursor(BINDING, (ROOT,), 0, OSV).as_json())
    data = result.outcome.settlement.evidence.data
    assert len(_page(result)["entries"]) == 1
    assert data["call_count"] == len(data["calls"]) == 2
    assert len(fs.directory_calls) == 1
    assert len(fs.metadata_calls) == 1


@pytest.mark.asyncio
async def test_page_size_bounds_metadata_and_result_entries_as_directory_grows():
    paths = [ROOT + f"/folder/segment-{index:03}.OSV" for index in range(100)]
    fs = Filesystem(paths)

    result = await _list(fs, context=_natural(), batch=128)

    assert result.error is None
    assert len(_page(result)["entries"]) == 32
    assert _page(result)["next_cursor"] is not None
    assert len(fs.directory_calls) == 1 and fs.directory_calls[0].batch == 32
    assert len(fs.metadata_calls) == 32
    data = result.outcome.settlement.evidence.data
    assert data["call_count"] == len(data["calls"]) == 2
    assert len(fs.metadata_batches) == 1


@pytest.mark.asyncio
async def test_completion_context_for_other_activity_is_rejected_before_directory_access():
    fs = Filesystem([OSV])
    context = {**_natural(), "activity_id": "8"}
    with pytest.raises(ValueError):
        await _list(fs, context=context)
    assert fs.directory_calls == []


@pytest.mark.asyncio
async def test_invalid_output_scope_is_rejected_before_directory_access():
    fs = Filesystem([OSV])
    with pytest.raises(ValueError):
        await _list(fs, scope={"directories": [ROOT + "/other"]})
    assert fs.directory_calls == []


@pytest.mark.asyncio
async def test_orphan_lrf_is_returned_as_an_unclassified_raw_file():
    result = await _list(Filesystem([LRF]), context=_natural())
    assert _page(result)["set_finalized"] is True
    assert [(entry["identity"], entry["format_id"], entry["size_bytes"],
        entry["complete"], entry["paired_identity"]) for entry in _page(result)["entries"]] == [
            (LRF, "lrf", 10, True, None)]


@pytest.mark.asyncio
async def test_main_directory_failure_preserves_unknown_set():
    class FailedFilesystem(Filesystem):
        async def read_directory(self, request, *, stop):
            raw = _tool_result(b"", exit_code=1, stderr=b"directory failed")
            return DirectoryRead(raw, ErrorValue("adb_directory_failed", "device_file_access",
                {"reason": "call_failed", "transport_error": None, "output_failure": None,
                 "local_exit_code": 1, "stderr": "directory failed"}), None)
    result = await _list(FailedFilesystem([]), context=_natural())
    assert result.error is not None and result.observations == ()


@pytest.mark.asyncio
async def test_real_driver_filesystem_and_result_parser_accept_raw_osmo_page():
    from camctl.devices.drivers.adb_cameras.driver import AdbCameraDriver
    from camctl.devices.drivers.adb_cameras.osmo360ii_capture import osmo360ii_contract
    from camctl.capture.result_inputs import page_from_outcome

    def frame(*paths):
        return b"\0".join((b"CAMCTL-DIRECTORY/1", *(path.encode() for path in paths), b"END", b""))

    class Transport:
        def __init__(self):
            self.outputs = [frame(OSV, LRF), b"\0".join((b"CAMCTL-METADATA/1",
                OSV.encode(), b"0", b"10\n", b"", LRF.encode(), b"0", b"4\n", b"", b"END", b""))]
            self.specs = []

        async def run(self, spec, stop):
            self.specs.append(spec)
            assert spec.argv[:5] == ("adb", "-s", "serial-1", "shell", "-T")
            return RawToolOutcome(LocalExit(exit_code=0), self.outputs.pop(0), None, None, stderr=b"")

    transport = Transport()
    driver = AdbCameraDriver(osmo360ii_contract(), {"camera": {"driver": "dji-osmo360-ii",
        "adb": {"serial": "serial-1"}}}, transport, terminate_grace_s=Decimal("1"), monotonic_ns=lambda: 0)
    request = ControlRequest("result", BINDING, {"activity_id": "7", "output_scope": {"directories": [ROOT]},
        "completion_context": _natural(), "video_result_mode": "segmented"}, TICKET, Decimal("10"))
    result = await driver.list_results(request, 32)
    assert result.outcome.status is AttemptStatus.SUCCEEDED and transport.outputs == []
    assert len(transport.specs) == 2
    page = page_from_outcome(TICKET, result.outcome, binding=BINDING)
    assert page.set_finalized is True and page.scan_complete is True
    assert [(entry.identity, entry.paired_identity) for entry in page.entries] == [(OSV, None), (LRF, None)]
    assert [entry.kind.value for entry in page.entries] == ["video", "video"]


@pytest.mark.asyncio
async def test_page_keeps_directory_identities_observed_before_metadata_changes_the_directory():
    added = ROOT + "/folder/New.OSV"
    class ChangingFilesystem(Filesystem):
        async def metadata(self, identity, *, timeout_s, stop):
            actual = await super().metadata(identity, timeout_s=timeout_s, stop=stop)
            self.paths = sorted([OSV, LRF, added])
            return actual

    fs = ChangingFilesystem([OSV, LRF])
    result = await _list(fs, context=_natural())
    assert result.error is None
    assert {entry["identity"] for entry in _page(result)["entries"]} == {OSV, LRF}
    assert len(fs.directory_calls) == 1
    assert [item.path for item in fs.metadata_calls] == [OSV, LRF]


@pytest.mark.asyncio
async def test_interrupted_stop_wait_cannot_dispatch_directory_or_claim_completion(monkeypatch):
    async def sleep(seconds):
        raise asyncio.CancelledError()
    monkeypatch.setattr(asyncio, "sleep", sleep)
    fs = Filesystem([OSV])
    result = await _list(fs, context=_stop())
    assert result.error is not None and result.observations == () and fs.directory_calls == []
    assert result.outcome.settlement.evidence.data["file_completion"]["completed"] is False


@pytest.mark.asyncio
async def test_cursor_from_other_binding_is_rejected_before_wait(monkeypatch):
    clock, waits = [0], []
    async def sleep(seconds):
        waits.append(seconds)
        clock[0] += int(seconds * 1_000_000_000)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    cursor = DirectoryCursor(DeviceBinding("other", "dji-osmo360-ii"), (ROOT,), 0, OSV).as_json()
    with pytest.raises(ValueError):
        await _list(Filesystem([OSV]), context=_stop(), cursor=cursor, clock=clock)
    assert waits == []


@pytest.mark.asyncio
async def test_budget_exhaustion_preserves_page_identities_and_unknown_metadata():
    clock = [0]
    other_original = ROOT + "/folder/Other.OSV"
    class ExhaustingFilesystem(Filesystem):
        async def metadata_many(self, identities, *, timeout_s, stop):
            from camctl.devices.drivers.adb_cameras.filesystem import FileMetadataBatch, FileMetadataItem

            assert timeout_s == Decimal("0.5")
            result = await self.metadata(identities[0], timeout_s=timeout_s, stop=stop)
            clock[0] = 500_000_000
            raw = RawToolOutcome(LocalExit(signal=15), b"\0".join((b"CAMCTL-METADATA/1",
                identities[0].path.encode(), b"0", b"10\n", b"", b"")), "timeout", Decimal("1"), stderr=b"")
            error = ErrorValue("adb_metadata_failed", "device_file_access",
                {"reason": "call_failed", "transport_error": "timeout"})
            return FileMetadataBatch(raw, (FileMetadataItem(identities[0], result.value, None),), error)

    fs = ExhaustingFilesystem([OSV, LRF, other_original])
    result = await _list(fs, context=_natural(), clock=clock, timeout=Decimal("0.5"))

    assert result.error is not None
    assert result.error["code"] == "adb_metadata_failed"
    assert result.error["details"]["transport_error"] == "timeout"
    entries = {entry["identity"]: entry for entry in _page(result)["entries"]}
    assert set(entries) == {OSV, LRF, other_original}
    assert entries[OSV]["complete"] is True and entries[OSV]["size_bytes"] == 10
    assert all(not entries[path]["complete"] and entries[path]["size_bytes"] is None
        for path in (LRF, other_original))
    assert _page(result)["set_finalized"] is False
    assert [item.path for item in fs.metadata_calls] == [OSV]
    assert len(fs.directory_calls) == 1


@pytest.mark.asyncio
async def test_raw_page_batches_result_metadata_once_in_directory_page_order():
    other = ROOT + "/folder/Other.OSV"
    class OrderedFilesystem(Filesystem):
        def __init__(self):
            super().__init__([OSV, LRF, other])
            self.metadata_paths = []
        async def metadata(self, identity, *, timeout_s, stop):
            self.metadata_paths.append(identity.path)
            return await super().metadata(identity, timeout_s=timeout_s, stop=stop)
    fs = OrderedFilesystem()
    result = await _list(fs, context=_natural())
    assert result.error is None and fs.metadata_paths == [OSV, LRF, other]
    assert len(fs.metadata_batches) == 1
    assert [identity.path for identity in fs.metadata_batches[0][0]] == [OSV, LRF, other]
    assert [entry["identity"] for entry in _page(result)["entries"]] == [OSV, LRF, other]


def test_osmo_segment_sort_uses_numeric_cam_and_index_with_rollover():
    from camctl.devices.drivers.adb_cameras.osmo360ii_segments import parse_osmo_segment_key

    before = parse_osmo_segment_key(
        "/mnt/media_rw/emulated/DCIM/CAM_001/CAM_20261021090000_0999_D.OSV")
    after = parse_osmo_segment_key(
        "/mnt/media_rw/emulated/DCIM/CAM_002/CAM_20200101000000_0001_D.OSV")
    assert before is not None and after is not None
    assert before < after
    assert (before.directory_number, before.index) == (1, 999)
    assert (after.directory_number, after.index) == (2, 1)


def test_osmo_segment_name_ignores_datetime_for_sort_but_pairs_full_stem_only():
    from camctl.devices.drivers.adb_cameras.osmo360ii_segments import parse_osmo_segment_name

    original = parse_osmo_segment_name(
        "/mnt/media_rw/emulated/DCIM/CAM_001/CAM_20261021090000_0001_D.OSV")
    preview = parse_osmo_segment_name(
        "/mnt/media_rw/emulated/DCIM/CAM_001/CAM_20261021090000_0001_D.lRf")
    other_datetime = parse_osmo_segment_name(
        "/mnt/media_rw/emulated/DCIM/CAM_001/CAM_20200101000000_0001_D.LRF")
    renamed_stem = parse_osmo_segment_name(
        "/mnt/media_rw/emulated/DCIM/CAM_001/cam_20261021090000_0001_D.LRF")
    other_directory = parse_osmo_segment_name(
        "/mnt/media_rw/emulated/DCIM/CAM_002/CAM_20261021090000_0001_D.LRF")
    assert all(value is not None for value in (
        original, preview, other_datetime, renamed_stem, other_directory))
    assert original.key == preview.key == other_datetime.key
    assert original.pairing_identity == preview.pairing_identity
    assert original.pairing_identity != other_datetime.pairing_identity
    assert original.suffix == ".osv" and preview.suffix == ".lrf"
    assert original.pairing_identity != renamed_stem.pairing_identity
    assert original.pairing_identity != other_directory.pairing_identity
    with pytest.raises(ValueError, match="后缀"):
        type(original)(original.key, original.pairing_identity, ".mp4")


@pytest.mark.parametrize("path", [
    "/mnt/media_rw/emulated/DCIM/CAM_001/CAM_20261021090000_0000_D.OSV",
    "/mnt/media_rw/emulated/DCIM/CAM_001/CAM_20261021090000_1000_D.OSV",
    "/mnt/media_rw/emulated/DCIM/CAM_000/CAM_20261021090000_0001_D.OSV",
    "/mnt/media_rw/emulated/DCIM/CAM_0001/CAM_20261021090000_0001_D.OSV",
    "/mnt/media_rw/emulated/DCIM/CAM_001/../CAM_001/CAM_20261021090000_0001_D.OSV",
    "/mnt/media_rw/emulated/DCIM/CAM_001/CAM_20261021090000_0001_D.MP4",
    "/mnt/media_rw/emulated/DCIM/CAM_001/nested/CAM_20261021090000_0001_D.OSV",
    "/mnt/media_rw/emulated/DCIM/CAM_001/CAM_20261021090000_0001_OSV",
])
def test_osmo_segment_name_rejects_invalid_folder_name_and_index(path):
    from camctl.devices.drivers.adb_cameras.osmo360ii_segments import parse_osmo_segment_key

    assert parse_osmo_segment_key(path) is None

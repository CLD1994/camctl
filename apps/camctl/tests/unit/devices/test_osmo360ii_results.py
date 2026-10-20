"""OSV 必要事实与有界跨页 LRF 配对相互独立。"""

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest

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


class Filesystem:
    def __init__(self, paths, *, sizes=None, metadata_error=None, pairing_error=False):
        self.paths = sorted(paths)
        self.sizes = sizes or {}
        self.metadata_error = metadata_error
        self.pairing_error = pairing_error
        self.directory_calls = []

    async def read_directory(self, request, *, stop):
        self.directory_calls.append(request)
        if self.pairing_error and len(self.directory_calls) > 1:
            return DirectoryRead(None, ErrorValue("adb_directory_failed", "device_file_access"), None)
        after = request.cursor.after_path if request.cursor is not None else None
        values = [path for path in self.paths if after is None or path > after]
        selected = values[:request.batch]
        cursor = (DirectoryCursor(BINDING, request.directories, 0, selected[-1])
                  if len(values) > len(selected) else None)
        return DirectoryRead(None, None, Page(tuple(FileIdentity(BINDING, path) for path in selected), cursor))

    async def metadata(self, identity, *, timeout_s, stop):
        if identity.path == self.metadata_error:
            return FileAccessRead(None, None, ErrorValue("adb_metadata_failed", "device_file_access"))
        return FileAccessRead(None, self.sizes.get(identity.path, 10), None)


async def _list(fs, *, context=None, cursor=None, batch=3, clock=None, timeout=Decimal("40")):
    from camctl.devices.drivers.adb_cameras.osmo360ii_results import list_osmo360ii_results
    params = {"activity_id": "7", "output_scope": {"directories": [ROOT]}, "cursor": cursor}
    if context is not None:
        params["completion_context"] = context
    driver = SimpleNamespace(_filesystem=lambda binding: fs, monotonic_ns=lambda: 0 if clock is None else clock[0])
    return await list_osmo360ii_results(driver, ControlRequest("result", BINDING, params, TICKET, timeout), batch)


def _page(result):
    return result.observations[0].data


def _preview(result):
    return result.outcome.settlement.evidence.data["optional_preview_observations"]


@pytest.mark.asyncio
async def test_natural_completion_registers_osv_and_exact_mixed_case_preview_without_wait(monkeypatch):
    async def forbidden_sleep(seconds):
        pytest.fail("自然完成上下文不得再次等待")
    monkeypatch.setattr(asyncio, "sleep", forbidden_sleep)
    result = await _list(Filesystem([LRF, OSV]), context=_natural())
    page = _page(result)
    assert result.error is None and page["set_finalized"] is True
    assert [(entry["identity"], entry["kind"], entry["format_id"], entry["paired_identity"])
        for entry in page["entries"]] == [(OSV, "video", "osv", None), (LRF, "video", "lrf", OSV)]
    assert all(entry["complete"] and entry["media_type"] is None for entry in page["entries"])
    assert page["completion_evidence"] is None
    assert result.outcome.settlement.evidence.data["file_completion"] is None


@pytest.mark.asyncio
async def test_pairing_scans_across_pages_without_retaining_complete_directory():
    fs = Filesystem([LRF, OSV, ROOT + "/folder/zzz.tmp"])
    first = await _list(fs, context=_natural(), batch=1)
    assert [entry["identity"] for entry in _page(first)["entries"]] == [OSV]
    assert _preview(first)[0]["state"] == "not_observed_on_page"
    result = await _list(fs, context=_natural(), batch=1, cursor=_page(first)["next_cursor"])
    assert [(entry["identity"], entry["paired_identity"]) for entry in _page(result)["entries"]] == [(LRF, OSV)]
    assert len(fs.directory_calls) >= 4
    assert all(call.batch == 1 for call in fs.directory_calls)
    assert _page(result)["set_finalized"] is False


@pytest.mark.asyncio
async def test_missing_completion_preserves_original_without_claiming_file_or_set_complete():
    result = await _list(Filesystem([OSV, LRF]))
    page = _page(result)
    assert page["set_finalized"] is False
    assert [(entry["identity"], entry["complete"]) for entry in page["entries"]] == [(OSV, False)]
    assert _preview(result)[0]["state"] == "completion_unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("paths,sizes,error,state", [
    ([OSV], {}, None, "missing"),
    ([OSV, LRF], {LRF: 0}, None, "zero_size"),
    ([OSV, LRF], {}, LRF, "metadata_failed"),
    ([OSV, LRF, ROOT + "/folder/Clip.LRF"], {}, None, "ambiguous"),
    ([OSV, LRF, ROOT + "/folder/Clip.osv"], {}, None, "ambiguous"),
    ([OSV, ROOT + "/other/Clip.LRF"], {}, None, "missing"),
    ([OSV, ROOT + "/folder/clip.LRF"], {}, None, "missing"),
])
async def test_optional_preview_failures_preserve_valid_original(paths, sizes, error, state):
    result = await _list(Filesystem(paths, sizes=sizes, metadata_error=error), context=_natural())
    assert result.error is None and _page(result)["set_finalized"] is True
    assert all(entry["format_id"] == "osv" for entry in _page(result)["entries"])
    assert _preview(result)[0]["state"] == state


@pytest.mark.asyncio
async def test_optional_pairing_read_failure_does_not_erase_original():
    result = await _list(Filesystem([OSV, LRF], pairing_error=True), context=_natural())
    assert result.error is None and _page(result)["set_finalized"] is True
    assert _page(result)["entries"][0]["complete"] is True
    assert _preview(result)[0]["state"] == "scan_failed"


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
async def test_diagnostic_call_retention_is_bounded_even_for_many_pairing_pages():
    paths = [OSV, LRF] + [ROOT + f"/zzz/{index:04}.tmp" for index in range(200)]
    result = await _list(Filesystem(paths), context=_natural(), batch=1,
        cursor=DirectoryCursor(BINDING, (ROOT,), 0, OSV).as_json())
    data = result.outcome.settlement.evidence.data
    assert len(data["calls"]) == 128 and data["call_count"] > 200


@pytest.mark.asyncio
async def test_ambiguous_preview_retains_actual_candidate_identities_with_bounded_samples():
    other = ROOT + "/folder/Clip.LRF"
    result = await _list(Filesystem([OSV, LRF, other]), context=_natural())
    observation = _preview(result)[0]
    assert observation["state"] == "ambiguous"
    assert observation["observed_preview_identities"] == [other, LRF]
    assert observation["scan_complete"] is True


@pytest.mark.asyncio
async def test_completion_context_for_other_activity_is_rejected_before_directory_access():
    fs = Filesystem([OSV])
    context = {**_natural(), "activity_id": "8"}
    with pytest.raises(ValueError):
        await _list(fs, context=context)
    assert fs.directory_calls == []


@pytest.mark.asyncio
async def test_orphan_lrf_is_observed_without_becoming_original_or_registered_preview():
    result = await _list(Filesystem([LRF]), context=_natural())
    assert _page(result)["entries"] == [] and _page(result)["set_finalized"] is True
    assert _preview(result)[0]["preview_identity"] == LRF
    assert _preview(result)[0]["state"] == "missing_pair"
    assert _preview(result)[0]["osv_count"] == 0 and _preview(result)[0]["lrf_count"] == 1


@pytest.mark.asyncio
async def test_main_directory_failure_preserves_unknown_set():
    class FailedFilesystem(Filesystem):
        async def read_directory(self, request, *, stop):
            return DirectoryRead(None, ErrorValue("adb_directory_failed", "device_file_access"), None)
    result = await _list(FailedFilesystem([]), context=_natural())
    assert result.error is not None and result.observations == ()


@pytest.mark.asyncio
async def test_real_driver_filesystem_and_evidence_registry_accept_paired_result_page():
    from camctl.devices.drivers.adb_cameras.driver import AdbCameraDriver
    from camctl.devices.drivers.adb_cameras.osmo360ii_capture import osmo360ii_contract
    from camctl.capture.result_inputs import page_from_outcome

    def frame(*paths):
        return b"\0".join((b"CAMCTL-DIRECTORY/1", *(path.encode() for path in paths), b"END", b""))

    class Transport:
        def __init__(self):
            self.outputs = [frame(OSV, LRF), b"10\n", frame(OSV, LRF), b"4\n"]

        async def run(self, spec, stop):
            assert spec.argv[:5] == ("adb", "-s", "serial-1", "shell", "-T")
            return RawToolOutcome(LocalExit(exit_code=0), self.outputs.pop(0), None, None, stderr=b"")

    transport = Transport()
    driver = AdbCameraDriver(osmo360ii_contract(), {"camera": {"driver": "dji-osmo360-ii",
        "adb": {"serial": "serial-1"}}}, transport, terminate_grace_s=Decimal("1"), monotonic_ns=lambda: 0)
    request = ControlRequest("result", BINDING, {"activity_id": "7", "output_scope": {"directories": [ROOT]},
        "completion_context": _natural()}, TICKET, Decimal("10"))
    result = await driver.list_results(request, 32)
    assert result.outcome.status is AttemptStatus.SUCCEEDED and transport.outputs == []
    page = page_from_outcome(TICKET, result.outcome, binding=BINDING)
    assert page.set_finalized is True and page.scan_complete is True
    assert [(entry.identity, entry.paired_identity) for entry in page.entries] == [(OSV, None), (LRF, OSV)]
    assert [entry.kind.value for entry in page.entries] == ["video", "video"]


@pytest.mark.asyncio
async def test_reliable_second_scan_proving_original_disappeared_preserves_explicit_conflict():
    class DisappearingFilesystem(Filesystem):
        async def metadata(self, identity, *, timeout_s, stop):
            actual = await super().metadata(identity, timeout_s=timeout_s, stop=stop)
            self.paths = [LRF]
            return actual

    result = await _list(DisappearingFilesystem([OSV, LRF]), context=_natural())
    assert result.error is not None and result.error["code"] == "osmo360ii_result_conflict"
    assert result.outcome.status is AttemptStatus.FAILED and _page(result)["set_finalized"] is False
    assert _page(result)["entries"][0]["identity"] == OSV
    assert _page(result)["entries"][0]["size_bytes"] == 10
    assert _preview(result)[0]["state"] == "original_missing"


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
async def test_second_scan_cannot_replace_raw_page_preview_with_different_case_path():
    other = ROOT + "/folder/Clip.LRF"
    class ChangingFilesystem(Filesystem):
        async def metadata(self, identity, *, timeout_s, stop):
            actual = await super().metadata(identity, timeout_s=timeout_s, stop=stop)
            if identity.path == OSV:
                self.paths = sorted([OSV, other])
            return actual
    result = await _list(ChangingFilesystem([OSV, LRF]), context=_natural())
    assert result.error is None
    assert [entry["identity"] for entry in _page(result)["entries"]] == [OSV]
    assert _preview(result)[0]["state"] == "preview_missing"
    assert _preview(result)[0]["observed_preview_identities"] == [other]


@pytest.mark.asyncio
async def test_all_necessary_original_metadata_precedes_optional_preview_metadata():
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
    assert result.error is None and fs.metadata_paths == [OSV, other, LRF]
    assert [entry["identity"] for entry in _page(result)["entries"]] == [OSV, LRF, other]


def test_osmo_segment_sort_uses_numeric_cam_and_index_with_rollover():
    from camctl.devices.drivers.adb_cameras.osmo360ii_segments import parse_osmo_segment_key

    before = parse_osmo_segment_key(
        "/mnt/media_rw/emulated/DCIM/CAM001/CAM_20261021090000_0999_D.OSV")
    after = parse_osmo_segment_key(
        "/mnt/media_rw/emulated/DCIM/CAM002/CAM_20200101000000_0001_D.OSV")
    assert before < after
    assert (before.directory_number, before.index) == (1, 999)
    assert (after.directory_number, after.index) == (2, 1)


def test_osmo_segment_name_ignores_datetime_for_sort_but_pairs_full_stem_only():
    from camctl.devices.drivers.adb_cameras.osmo360ii_segments import parse_osmo_segment_name

    original = parse_osmo_segment_name(
        "/mnt/media_rw/emulated/DCIM/CAM001/CAM_20261021090000_0001_D.OSV")
    preview = parse_osmo_segment_name(
        "/mnt/media_rw/emulated/DCIM/CAM001/CAM_20261021090000_0001_D.lRf")
    other_datetime = parse_osmo_segment_name(
        "/mnt/media_rw/emulated/DCIM/CAM001/CAM_20200101000000_0001_D.LRF")
    renamed_stem = parse_osmo_segment_name(
        "/mnt/media_rw/emulated/DCIM/CAM001/cam_20261021090000_0001_D.LRF")
    other_directory = parse_osmo_segment_name(
        "/mnt/media_rw/emulated/DCIM/CAM002/CAM_20261021090000_0001_D.LRF")
    assert original.key == preview.key == other_datetime.key
    assert original.pairing_identity == preview.pairing_identity
    assert original.pairing_identity != other_datetime.pairing_identity
    assert original.suffix == ".osv" and preview.suffix == ".lrf"
    assert original.pairing_identity != renamed_stem.pairing_identity
    assert original.pairing_identity != other_directory.pairing_identity
    with pytest.raises(ValueError, match="后缀"):
        type(original)(original.key, original.pairing_identity, ".mp4")


@pytest.mark.parametrize("path", [
    "/mnt/media_rw/emulated/DCIM/CAM001/CAM_20261021090000_0000_D.OSV",
    "/mnt/media_rw/emulated/DCIM/CAM001/CAM_20261021090000_1000_D.OSV",
    "/mnt/media_rw/emulated/DCIM/CAM000/CAM_20261021090000_0001_D.OSV",
    "/mnt/media_rw/emulated/DCIM/CAM0001/CAM_20261021090000_0001_D.OSV",
    "/mnt/media_rw/emulated/DCIM/CAM001/../CAM001/CAM_20261021090000_0001_D.OSV",
    "/mnt/media_rw/emulated/DCIM/CAM001/CAM_20261021090000_0001_D.MP4",
    "/mnt/media_rw/emulated/DCIM/CAM001/nested/CAM_20261021090000_0001_D.OSV",
    "/mnt/media_rw/emulated/DCIM/CAM001/CAM_20261021090000_0001_OSV",
])
def test_osmo_segment_name_rejects_invalid_folder_name_and_index(path):
    from camctl.devices.drivers.adb_cameras.osmo360ii_segments import parse_osmo_segment_key

    assert parse_osmo_segment_key(path) is None

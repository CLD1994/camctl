"""Action6 结果读取保留原片事实，并把 LRF 配对限制为可选诊断。"""

import asyncio
import sqlite3
from decimal import Decimal
from types import SimpleNamespace

import pytest
from adb_metadata_fixtures import collect_fake_metadata_many

from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.contracts.pages import Page
from camctl.devices.bindings import DeviceBinding
from camctl.devices.directory import DirectoryCursor, DirectoryRead
from camctl.devices.drivers.adb_cameras.commands import CameraModel
from camctl.devices.drivers.adb_cameras.filesystem import FileAccessRead
from camctl.devices.file_identity import FileIdentity
from camctl.devices.ports import ControlRequest
from camctl.operations.models import AttemptTicket, ErrorValue
from camctl.operations.process import LocalExit, RawToolOutcome
from camctl.operations.validation import validate_outcome


ROOT = "/mnt/media_rw/emulated/DCIM"
BINDING = DeviceBinding("camera", CameraModel.ACTION6)
TICKET = AttemptTicket(1, "result", "7", "results/7", 9)
MP4 = ROOT + "/folder/Clip.MP4"
LRF = ROOT + "/folder/Clip.LRF"


def _response(name):
    return {"status": int(enum_for("operation_attempts.status").SUCCEEDED),
        "effect_state": int(enum_for("operation_attempts.effect_state").CONFIRMED),
        "error": None, "result": {"format_version": 1,
        "settlement": {"basis": "observed", "evidence": {"type": "control_returned" if name == "timelapse_sent"
            else "stop_returned", "version": 1, "data": {}}},
        "observations": [{"type": name, "version": 1, "data": {"activity_id": "7",
            "response_payload": "00", "confirmation_basis": "action6_command_response/v1"}}]}}


def _natural():
    due = 31_001_000
    return {"kind": "timelapse_natural", "activity_id": "7", "start_result_event_id": 20,
        "start_response": _response("timelapse_sent"), "sent_at_us": 1_000,
        "wait_completed_event_id": 30, "wait_completed_at_us": due,
        "target_duration_ms": 1_000, "result_wait_margin_ms": 30_000, "extra_wait_ms": 0,
        "expected_check_at_us": due}


def _stop(wait_ms=5_000, *, kind=None, prior=None):
    context = {"activity_id": "7", "stop_result_event_id": 30, "stop_returned_at_us": 1_000,
        "stop_response": _response("stop_confirmed"), "file_completion_wait_ms": wait_ms,
        "prior_completion": prior}
    if kind is not None:
        context["kind"] = kind
    return context


class Filesystem:
    def __init__(self, paths, *, sizes=None, metadata_errors=(), main_error=None,
                 pair_scan_error=False, change_after_metadata=None):
        self.paths = sorted(paths)
        self.sizes = sizes or {}
        self.metadata_errors = set(metadata_errors)
        self.main_error = main_error
        self.pair_scan_error = pair_scan_error
        self.change_after_metadata = change_after_metadata
        self.directory_calls = []
        self.metadata_paths = []

    async def read_directory(self, request, *, stop):
        self.directory_calls.append(request)
        if len(self.directory_calls) == 1 and self.main_error is not None:
            return DirectoryRead(None, self.main_error, None)
        if len(self.directory_calls) > 1 and self.pair_scan_error:
            return DirectoryRead(None, ErrorValue("adb_directory_failed", "device_file_access",
                {"reason": "pair_scan_failed"}), None)
        after = None if request.cursor is None else request.cursor.after_path
        values = [path for path in self.paths if after is None or path > after]
        selected = values[:request.batch]
        cursor = (DirectoryCursor(request.binding, request.directories, 0, selected[-1])
                  if len(values) > len(selected) else None)
        return DirectoryRead(None, None, Page(tuple(FileIdentity(request.binding, path) for path in selected), cursor))

    async def metadata(self, identity, *, timeout_s, stop):
        self.metadata_paths.append(identity.path)
        if identity.path in self.metadata_errors:
            return FileAccessRead(None, None, ErrorValue("adb_metadata_failed", "device_file_access",
                {"reason": "metadata_failed"}))
        size = self.sizes.get(identity.path, 10)
        if self.change_after_metadata is not None:
            self.paths = sorted(self.change_after_metadata)
            self.change_after_metadata = None
        return FileAccessRead(None, size, None)

    async def metadata_many(self, identities, *, timeout_s, stop):
        return await collect_fake_metadata_many(self, identities, timeout_s=timeout_s, stop=stop)


def _page(result):
    return next((item.data for item in result.observations if item.type == "result_files_listed"), None)


def _evidence(result):
    return result.outcome.settlement.evidence.data


async def _list(fs, *, context=None, cursor=None, batch=3, clock=None, timeout=Decimal("40"),
                params=None, prior_result_entries=None):
    request_params = {"activity_id": "7", "output_scope": {"directories": [ROOT]}, "cursor": cursor,
                      "video_result_mode": "single_file"}
    if context is not None:
        request_params["completion_context"] = context
    if params is not None:
        request_params.update(params)
    driver = SimpleNamespace(_filesystem=lambda binding: fs,
        monotonic_ns=lambda: 0 if clock is None else clock[0])
    from camctl.devices.drivers.adb_cameras.action6_results import list_action6_results
    return await list_action6_results(driver, ControlRequest("result", BINDING, request_params,
        TICKET, timeout, prior_result_entries=prior_result_entries), batch)


def _validate(result):
    from camctl.devices.drivers.adb_cameras.action6_record import action6_record_contract
    return validate_outcome(TICKET, result.outcome, action6_record_contract().evidence)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "item", "prefix"])
async def test_segmented_page_uses_one_metadata_batch_with_remaining_budget(failure):
    paths = [MP4, ROOT + "/folder/second.JPG", ROOT + "/folder/third.DNG"]
    clock = [0]
    expected_error = ErrorValue("adb_metadata_failed", "device_file_access", {"reason": failure or "none"})

    class BatchFilesystem(Filesystem):
        def __init__(self):
            super().__init__(paths)
            self.batch_calls = []

        async def read_directory(self, request, *, stop):
            result = await super().read_directory(request, stop=stop)
            clock[0] = 2_000_000_000
            return result

        async def metadata(self, identity, *, timeout_s, stop):
            raise AssertionError("分段主页必须使用一次批量 metadata 调用")

        async def metadata_many(self, identities, *, timeout_s, stop):
            self.batch_calls.append((identities, timeout_s))
            items = tuple(SimpleNamespace(identity=identity,
                value=None if failure == "item" and index == 1 else 10 + index,
                error=expected_error if failure == "item" and index == 1 else None)
                for index, identity in enumerate(identities))
            if failure == "prefix":
                items = items[:1]
            records = []
            for item in items:
                records.extend((item.identity.path.encode(), b"1" if item.error else b"0",
                    b"" if item.error else f"{item.value}\n".encode(),
                    b"metadata failed" if item.error else b""))
            output = b"\0".join((b"CAMCTL-METADATA/1", *records,
                *((b"END", b"") if failure != "prefix" else (b"",))))
            outcome = RawToolOutcome(LocalExit(exit_code=0), output,
                "output_failed" if failure == "prefix" else None, None,
                output_failure="truncated" if failure == "prefix" else None, stderr=b"")
            return SimpleNamespace(outcome=outcome, items=items,
                error=expected_error if failure else None)

    fs = BatchFilesystem()
    result = await _list(fs, context=_natural(), clock=clock,
        params={"video_result_mode": "segmented"})

    assert len(fs.batch_calls) == 1
    identities, timeout = fs.batch_calls[0]
    assert [identity.path for identity in identities] == paths
    assert timeout == Decimal("38")
    assert _evidence(result)["call_count"] == 2
    page = _page(result)
    expected_sizes = ([10, 11, 12] if failure is None else
                      [10, None, 12] if failure == "item" else [10, None, None])
    assert [entry["size_bytes"] for entry in page["entries"]] == expected_sizes
    assert page["set_finalized"] is (failure is None)
    assert (result.error is None) is (failure is None)
    _validate(result)


@pytest.mark.asyncio
async def test_segmented_results_return_raw_preview_and_photos_without_pair_rescan():
    jpeg = ROOT + "/folder/frame.JPG"
    fs = Filesystem([LRF, MP4, jpeg], sizes={LRF: 0}, pair_scan_error=True)
    result = await _list(fs, context=_natural(), batch=1,
                         params={"video_result_mode": "segmented"})
    assert result.error is None
    assert len(fs.directory_calls) == 1
    assert fs.metadata_paths == [LRF]
    assert [(entry["identity"], entry["size_bytes"], entry["paired_identity"])
            for entry in _page(result)["entries"]] == [(LRF, 0, None)]
    assert _page(result)["next_cursor"] == DirectoryCursor(BINDING, (ROOT,), 0, LRF).as_json()
    _validate(result)

    photo_fs = Filesystem([jpeg])
    photo = await _list(photo_fs, context=_natural(),
                        params={"video_result_mode": "segmented"})
    assert _page(photo)["entries"][0]["format_id"] == "jpeg"
    assert _page(photo)["set_finalized"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, "invalid", 1])
async def test_result_reader_rejects_invalid_frozen_mode_before_file_access(mode):
    fs = Filesystem([MP4])
    with pytest.raises(ValueError, match="模式"):
        await _list(fs, context=_natural(), params={"video_result_mode": mode})
    assert fs.directory_calls == []


@pytest.mark.asyncio
async def test_reads_actual_action6_formats_in_page_order_and_preserves_zero_size():
    dng = ROOT + "/folder/Frame.DnG"
    jpeg = ROOT + "/folder/frame.JpG"
    video = ROOT + "/folder/final.Mp4"
    fs = Filesystem([jpeg, video, dng], sizes={dng: 0})
    result = await _list(fs, context=_natural())
    page = _page(result)

    assert result.error is None and page["set_finalized"] is True
    assert [(item["identity"], item["kind"], item["format_id"], item["size_bytes"])
        for item in page["entries"]] == [
            (identity, "photo" if identity in (jpeg, dng) else "video",
             "jpeg" if identity == jpeg else "dng" if identity == dng else "mp4",
             0 if identity == dng else 10)
            for identity in sorted((jpeg, video, dng))]
    assert all(item["complete"] for item in page["entries"])
    validated = _validate(result)
    assert validated.outcome.settlement.evidence.type == "result_returned"
    assert validated.outcome.settlement.evidence.version == 2


@pytest.mark.asyncio
async def test_natural_context_does_not_repeat_wait(monkeypatch):
    async def forbidden_sleep(seconds):
        pytest.fail("自然完成上下文不得再次等待")
    monkeypatch.setattr(asyncio, "sleep", forbidden_sleep)

    result = await _list(Filesystem([MP4]), context=_natural())

    assert result.error is None and _page(result)["entries"][0]["complete"] is True
    assert _evidence(result)["file_completion"] is None
    _validate(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_ms,kind", [(5_000, None), (30_000, "timelapse_stop")])
async def test_stop_wait_uses_original_context_and_next_page_reuses_source(monkeypatch, wait_ms, kind):
    clock, waits = [0], []

    async def sleep(seconds):
        waits.append(seconds)
        clock[0] += int(seconds * 1_000_000_000)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    fs = Filesystem([MP4])
    context = _stop(wait_ms, kind=kind)
    first = await _list(fs, context=context, clock=clock)
    completion = _evidence(first)["file_completion"]
    assert waits == [wait_ms / 1000] and completion["required_wait_ms"] == wait_ms
    assert completion["completed"] is True and _evidence(first)["file_completion_source_page_event_id"] is None
    _validate(first)

    context["prior_completion"] = {"result_page_event_id": 31, "evidence": completion}
    second = await _list(fs, context=context, clock=clock)
    assert waits == [wait_ms / 1000]
    assert _evidence(second)["file_completion_source_page_event_id"] == 31
    _validate(second)


@pytest.mark.asyncio
async def test_pair_scan_handles_lrf_before_mp4_without_changing_main_cursor():
    fs = Filesystem([LRF, MP4])
    first = await _list(fs, context=_natural(), batch=1)
    page = _page(first)

    assert page["cursor"] is None
    assert page["next_cursor"] == DirectoryCursor(BINDING, (ROOT,), 0, LRF).as_json()
    assert [(item["identity"], item["paired_identity"]) for item in page["entries"]] == [(LRF, MP4)]
    assert len(fs.directory_calls) > 2
    assert fs.metadata_paths == [LRF]
    assert all(call.batch == 1 for call in fs.directory_calls)
    assert fs.directory_calls[0].cursor is None and fs.directory_calls[1].cursor is None
    _validate(first)


@pytest.mark.asyncio
async def test_pair_scan_handles_mp4_before_lrf_on_later_primary_page():
    video = ROOT + "/folder/clip.MP4"
    preview = ROOT + "/folder/clip.lrf"
    fs = Filesystem([video, preview])
    first = await _list(fs, context=_natural(), batch=1)
    first_page = _page(first)
    assert [item["identity"] for item in first_page["entries"]] == [video]
    assert first_page["next_cursor"] is not None

    second = await _list(fs, context=_natural(), cursor=first_page["next_cursor"], batch=1)
    second_page = _page(second)
    assert [(item["identity"], item["paired_identity"]) for item in second_page["entries"]] == [(preview, video)]
    assert second_page["cursor"] == first_page["next_cursor"]
    assert len(fs.directory_calls) >= 3


@pytest.mark.asyncio
async def test_photo_is_never_an_lrf_original_and_extra_photo_keeps_its_format():
    dng = ROOT + "/folder/Clip.DNG"
    result = await _list(Filesystem([LRF, dng]), context=_natural())
    page = _page(result)

    assert [(item["identity"], item["kind"], item["format_id"], item["paired_identity"])
        for item in page["entries"]] == [(dng, "photo", "dng", None)]
    assert _evidence(result)["optional_preview_observations"][0]["mp4_count"] == 0
    assert "osv_count" not in _evidence(result)["optional_preview_observations"][0]
    assert _evidence(result)["optional_preview_observations"][0]["state"] == "missing_pair"


@pytest.mark.asyncio
@pytest.mark.parametrize("fs,state", [
    (Filesystem([MP4, LRF], pair_scan_error=True), "scan_failed"),
    (Filesystem([MP4, LRF], sizes={LRF: 0}), "zero_size"),
    (Filesystem([MP4, LRF], metadata_errors={LRF}), "metadata_failed"),
    (Filesystem([MP4, ROOT + "/folder/Clip.mp4", LRF]), "ambiguous"),
])
async def test_optional_preview_failures_preserve_required_original(fs, state):
    result = await _list(fs, context=_natural())
    page = _page(result)

    assert result.error is None and page["set_finalized"] is True
    assert any(item["identity"] == MP4 and item["format_id"] == "mp4" for item in page["entries"])
    assert not any(item["format_id"] == "lrf" for item in page["entries"])
    assert _evidence(result)["optional_preview_observations"][0]["state"] == state


@pytest.mark.asyncio
async def test_main_page_original_disappearing_before_pair_scan_is_a_conflict():
    fs = Filesystem([MP4, LRF], change_after_metadata=[LRF])
    result = await _list(fs, context=_natural())
    page = _page(result)

    assert result.error is not None and result.error["code"] == "action6_result_conflict"
    assert page["set_finalized"] is False and page["entries"][0]["size_bytes"] == 10
    assert _evidence(result)["optional_preview_observations"][0]["state"] == "original_missing"
    _validate(result)


@pytest.mark.asyncio
async def test_later_preview_without_verified_history_is_not_treated_as_empty_history():
    photo = ROOT + "/folder/Z.JPG"
    preview = ROOT + "/folder/Clip.lrf"
    cursor = DirectoryCursor(BINDING, (ROOT,), 0, MP4).as_json()
    result = await _list(Filesystem([preview, photo]), context=_natural(), cursor=cursor, batch=2)
    page = _page(result)

    assert result.error is not None
    assert result.error["code"] == "action6_file_completion_unconfirmed"
    assert page["set_finalized"] is False
    assert [(item["identity"], item["format_id"]) for item in page["entries"]] == [(photo, "jpeg")]
    diagnostic = _evidence(result)["optional_preview_observations"][0]
    assert diagnostic["state"] == "history_unavailable"
    assert diagnostic["mp4_count"] == 0
    _validate(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [ConsistencyError, sqlite3.DatabaseError])
async def test_later_preview_propagates_history_call_error_unchanged(error_type):
    error = error_type("history read failed")
    preview = ROOT + "/folder/Clip.lrf"
    cursor = DirectoryCursor(BINDING, (ROOT,), 0, MP4).as_json()
    async def history():
        raise error
        yield  # 保持异步条目流契约；读取在首条交付前失败。

    with pytest.raises(error_type) as raised:
        await _list(Filesystem([preview, ROOT + "/folder/Z.JPG"]),
            context=_natural(), cursor=cursor, batch=2, prior_result_entries=history)

    assert raised.value is error


@pytest.mark.asyncio
async def test_later_preview_propagates_history_iteration_error_after_matching_entry():
    error = ConsistencyError("history tail is inconsistent")
    preview = ROOT + "/folder/Clip.lrf"
    cursor = DirectoryCursor(BINDING, (ROOT,), 0, MP4).as_json()

    async def history():
        yield {"identity": MP4, "format_id": "mp4"}
        raise error

    with pytest.raises(ConsistencyError) as raised:
        await _list(Filesystem([preview, ROOT + "/folder/Z.JPG"]),
            context=_natural(), cursor=cursor, batch=2, prior_result_entries=history)

    assert raised.value is error


@pytest.mark.asyncio
async def test_later_preview_checks_complete_history_before_returning_conflict():
    preview = ROOT + "/folder/Clip.lrf"
    cursor = DirectoryCursor(BINDING, (ROOT,), 0, MP4).as_json()
    history_finished = []

    async def history():
        yield {"identity": MP4, "format_id": "mp4"}
        history_finished.append(True)
        yield {"identity": ROOT + "/folder/other.MP4", "format_id": "mp4"}

    result = await _list(Filesystem([preview, ROOT + "/folder/Z.JPG"]),
        context=_natural(), cursor=cursor, batch=2, prior_result_entries=history)
    page = _page(result)
    diagnostic = _evidence(result)["optional_preview_observations"][0]

    assert history_finished == [True]
    assert result.error is not None and result.error["code"] == "action6_result_conflict"
    assert page["set_finalized"] is False
    assert diagnostic["state"] == "original_missing"
    assert diagnostic["mp4_count"] == 0 and diagnostic["lrf_count"] == 1
    assert diagnostic["scan_complete"] is True and diagnostic["original_identity"] == MP4
    _validate(result)


@pytest.mark.asyncio
async def test_later_preview_propagates_malformed_history_entry():
    preview = ROOT + "/folder/Clip.lrf"
    cursor = DirectoryCursor(BINDING, (ROOT,), 0, MP4).as_json()
    async def history():
        yield {"identity": MP4, "format_id": ""}

    with pytest.raises(ValueError, match="稳定身份或格式分类"):
        await _list(Filesystem([preview, ROOT + "/folder/Z.JPG"]),
            context=_natural(), cursor=cursor, batch=2, prior_result_entries=history)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_identity", [MP4, ROOT + "/folder/second.JPG"])
async def test_necessary_video_or_photo_metadata_failure_returns_partial_page_and_error(failed_identity):
    other = ROOT + "/folder/second.JPG"
    fs = Filesystem([MP4, other], metadata_errors={failed_identity})
    result = await _list(fs, context=_natural())
    page = _page(result)

    assert result.error is not None and result.error["code"] == "adb_metadata_failed"
    assert page["set_finalized"] is False
    failed = next(item for item in page["entries"] if item["identity"] == failed_identity)
    assert failed["complete"] is False and failed["size_bytes"] is None
    _validate(result)
    if failed_identity == MP4:
        assert failed == {"identity": MP4, "locator": {"path": MP4}, "complete": False,
            "size_bytes": None, "kind": "video", "format_id": "mp4", "original_name": "Clip.MP4",
            "media_type": None, "paired_identity": None}


@pytest.mark.asyncio
async def test_main_directory_failure_does_not_become_a_final_empty_page():
    result = await _list(Filesystem([], main_error=ErrorValue("adb_directory_failed", "device_file_access")),
        context=_natural())

    assert result.error is not None and result.observations == ()
    assert _evidence(result)["call_count"] == 1
    _validate(result)


@pytest.mark.asyncio
async def test_call_diagnostics_are_capped_but_total_count_is_exact():
    paths = [LRF, MP4] + [ROOT + f"/zz/{index:04}.tmp" for index in range(200)]
    fs = Filesystem(paths)
    result = await _list(fs, context=_natural(), batch=1)
    data = _evidence(result)

    assert len(data["calls"]) == 128
    assert data["call_count"] > 200
    assert data["call_count"] == len(fs.directory_calls) + len(fs.metadata_paths)


@pytest.mark.asyncio
@pytest.mark.parametrize("other_original", [
    ROOT + "/other/Clip.MP4",
    ROOT + "/folder/clip.MP4",
])
async def test_preview_pair_requires_exact_directory_and_stem_bytes(other_original):
    result = await _list(Filesystem([LRF, other_original]), context=_natural())
    observation = _evidence(result)["optional_preview_observations"][0]

    assert result.error is None and observation["state"] == "missing_pair"
    assert observation["mp4_count"] == 0
    assert not any(entry["format_id"] == "lrf" for entry in _page(result)["entries"])


@pytest.mark.asyncio
async def test_invalid_natural_or_prior_stop_context_is_rejected_before_listing():
    fs = Filesystem([MP4])
    wrong_activity = _natural()
    wrong_activity["activity_id"] = "8"
    early = _natural()
    early["wait_completed_at_us"] -= 1
    wrong_prior = _stop(prior={"result_page_event_id": 31, "evidence": {
        "method": "stop_return_and_wait", "version": 1, "activity_id": "8",
        "stop_result_event_id": 30, "required_wait_ms": 5_000,
        "observed_wait_ns": 5_000_000_000, "completed": True}})

    with pytest.raises(ValueError):
        await _list(fs, context=wrong_activity)
    with pytest.raises(ValueError):
        await _list(fs, context=early)
    with pytest.raises(ValueError):
        await _list(fs, context=wrong_prior)
    assert fs.directory_calls == []


def test_action6_keeps_the_legacy_v1_evidence_registration():
    from camctl.devices.drivers.adb_cameras.action6_record import action6_record_contract

    evidence = action6_record_contract().evidence
    assert evidence.contract("result_returned", 1).fields == frozenset({
        "file_completion", "file_completion_source_page_event_id", "calls"})
    assert evidence.contract("result_returned", 2).fields == frozenset({
        "file_completion", "file_completion_source_page_event_id", "calls", "call_count",
        "optional_preview_observations"})


@pytest.mark.asyncio
async def test_invalid_scope_activity_or_cursor_is_rejected_before_filesystem_access():
    fs = Filesystem([MP4])
    wrong_cursor = DirectoryCursor(DeviceBinding("other", CameraModel.ACTION6), (ROOT,), 0, MP4).as_json()

    with pytest.raises(ValueError):
        await _list(fs, params={"activity_id": "8"})
    with pytest.raises(ValueError):
        await _list(fs, params={"output_scope": {"directories": ["/sdcard/DCIM"]}})
    with pytest.raises(ValueError):
        await _list(fs, cursor=wrong_cursor)
    assert fs.directory_calls == []

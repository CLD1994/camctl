"""真实 OSMO 驱动页经原目录基准差集保存，跨页配对不改变页范围。"""
import asyncio
from decimal import Decimal
import json

import pytest

from camctl.bootstrap.capture_assembly import DriverResultListing
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.capture.handlers import _begin_check_round
from camctl.capture.result_pages import ResultPageSave
from camctl.contracts.pages import Page
from camctl.contracts.values import new_operation_key
from camctl.devices.bindings import DeviceBinding
from camctl.devices.catalog import DriverDefinition, DriverDefinitions, build_catalog
from camctl.devices.directory import DirectoryCursor, DirectoryRead
from camctl.devices.file_identity import FileIdentity
from camctl.devices.drivers.adb_cameras.driver import AdbCameraDriver
from camctl.devices.drivers.adb_cameras.filesystem import FileAccessRead
from camctl.devices.drivers.adb_cameras.osmo360ii_capture import osmo360ii_contract
from camctl.devices.ports import ControlRequest
from camctl.devices.tasks import StartReturn
from camctl.operations.attempts import (AttemptConfig, AttemptFinish, AttemptIntent, AttemptTarget,
    OperationKind, RunFinish, RunOutcome)
from camctl.operations.process import LocalExit, RawToolOutcome
from camctl.operations.validation import validate_outcome
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository
from ..acceptance.test_acceptance import _plan_body
from ..capture.test_baseline_history import _append, _fix
from ..capture.test_baseline_start import _grant
from ..capture.test_capture_contract import _runtime
from ..scheduling.test_start_action import _SCHEDULED, _accept_plan, _start, owned


pytestmark = pytest.mark.asyncio
ROOT = "/mnt/media_rw/emulated/DCIM"
BINDING = DeviceBinding("cam-1", "dji-osmo360-ii")


class Filesystem:
    def __init__(self, paths):
        self.paths = sorted(paths)

    async def read_directory(self, request, *, stop):
        after = None if request.cursor is None else request.cursor.after_path
        remaining = [path for path in self.paths if after is None or path > after]
        selected = remaining[:request.batch]
        cursor = (DirectoryCursor(BINDING, request.directories, 0, selected[-1])
                  if len(remaining) > len(selected) else None)
        return DirectoryRead(None, None, Page(tuple(FileIdentity(BINDING, path) for path in selected), cursor))

    async def metadata(self, identity, *, timeout_s, stop):
        return FileAccessRead(None, 10, None)


class Transport:
    async def run(self, spec, stop):
        return RawToolOutcome(LocalExit(exit_code=0), b"Resp message, len = 1, data:\n 00\n",
            None, None, stderr=b"")


async def _world(owned, tmp_path, baseline, monkeypatch):
    contract = osmo360ii_contract()
    params = {"type": "osmo360ii_record", "duration_s": 10,
        "bitrate": "standard", "exposure": {"mode": "auto"}}
    config = load_config({"devices": {"cam-1": {"kind": "camera", "driver": "dji-osmo360-ii",
        "adb": {"serial": "serial-1"}}}}, ConfigDefaults())
    catalog = build_catalog(config, DriverDefinitions({"dji-osmo360-ii": DriverDefinition("dji-osmo360-ii",
        {cap.action_type: (cap,) for cap in contract.capabilities()}, contract.declaration,
        source_storage_resolver=contract.source_storage_resolver)}))
    body = _plan_body()
    body["actions"][0].update(type="camera_record", params=params)
    await _accept_plan(owned, tmp_path, body, catalog)
    started = _start(owned, 1, now=_SCHEDULED)
    assert started.kind is DbOutcomeKind.COMPLETED, started.error
    activity = started.value.activity_id
    if baseline:
        appended = _append(owned, activity, tuple(FileIdentity(BINDING, path) for path in sorted(baseline)))
        assert appended.kind is DbOutcomeKind.COMPLETED, appended.error
    fixed = _fix(owned, activity, chunks=int(bool(baseline)), entries=len(baseline))
    assert fixed.kind is DbOutcomeKind.COMPLETED, fixed.error
    granted = _grant(owned)
    assert granted.kind is DbOutcomeKind.COMPLETED, granted.error
    clock = [0]
    async def sleep(seconds):
        clock[0] += int(seconds * 1_000_000_000)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    driver = AdbCameraDriver(contract, config.devices, Transport(), terminate_grace_s=Decimal("1"),
        monotonic_ns=lambda: clock[0])
    operations = OperationRepository()
    ticket = granted.value.ticket
    result = await driver.control(ControlRequest("start_recording", BINDING, params, ticket,
        Decimal("10"), lambda: None, start_return_meaning=StartReturn.SENT))
    saved = operations.finish_attempt(AttemptFinish(ticket, validate_outcome(ticket, result.outcome, contract.evidence),
        _SCHEDULED, run_finish=RunFinish(RunOutcome.SUCCEEDED)), new_operation_key(), owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    stop = operations.begin_attempt(AttemptIntent(action_id=1, kind=OperationKind.STOP,
        target=AttemptTarget(activity_id=activity), operation="stop", query_purpose=None,
        config=AttemptConfig(1, Decimal("10")), occurred_at=_SCHEDULED + 1), new_operation_key(), owned)
    assert stop.kind is DbOutcomeKind.COMPLETED, stop.error
    stop_ticket = stop.value.ticket
    stopped = await driver.stop(ControlRequest("stop_recording", BINDING, {}, stop_ticket, Decimal("10")))
    saved = operations.finish_attempt(AttemptFinish(stop_ticket,
        validate_outcome(stop_ticket, stopped.outcome, contract.evidence), _SCHEDULED + 1,
        run_finish=RunFinish(RunOutcome.SUCCEEDED)), new_operation_key(), owned)
    assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
    row = owned.connection.execute("SELECT status,effect_state,error_json,result_json,result_event_id"
        " FROM operation_attempts WHERE run_id=?", (stop_ticket.run_id,)).fetchone()
    context = {"kind": "record_stop", "activity_id": str(activity), "stop_result_event_id": row[4],
        "stop_returned_at_us": _SCHEDULED + 1, "file_completion_wait_ms": 30000, "prior_completion": None,
        "stop_response": {"status": row[0], "effect_state": row[1],
            "error": None if row[2] is None else json.loads(row[2]), "result": json.loads(row[3])}}
    runtime = _runtime(owned)
    return driver, runtime, _begin_check_round(runtime, 1).ticket, context


@pytest.mark.parametrize("paths,batch,baseline,expected", [
    (["Clip.LRF", "Clip.OSV"], 2, [], ["Clip.LRF", "Clip.OSV"]),
    (["Clip.LRF", "Clip.OSV"], 1, [], ["Clip.LRF", "Clip.OSV"]),
    (["Clip.OSV", "Clip.lrf"], 1, [], ["Clip.OSV", "Clip.lrf"]),
    (["New.OSV", "New.lrf", "Old.OSV", "Old.lrf"], 1, ["Old.OSV", "Old.lrf"], ["New.OSV", "New.lrf"]),
    (["Clip.OSV", "Clip.lrf"], 1, ["Clip.OSV"], ["Clip.lrf"]),
])
async def test_driver_pages_preserve_raw_range_order_and_actual_baseline_difference(
        owned, tmp_path, monkeypatch, paths, batch, baseline, expected):
    paths, baseline, expected = ([ROOT + "/" + name for name in values]
                                for values in (paths, baseline, expected))
    driver, runtime, ticket, context = await _world(owned, tmp_path, baseline, monkeypatch)
    fs = Filesystem(paths)
    monkeypatch.setattr(driver, "_filesystem", lambda binding: fs)
    cursor, page_no, saved_refs = None, 1, []
    while True:
        result = await driver.list_results(ControlRequest("result", BINDING,
            {"activity_id": ticket.target_id, "output_scope": {"directories": [ROOT]},
                "cursor": None if cursor is None else cursor.as_json(), "completion_context": context},
            ticket, Decimal("40")), batch)
        assert result.error is None, result.error
        request = ResultPageSave(ticket, page_no, cursor, validate_outcome(ticket, result.outcome,
            driver.contract.evidence), _SCHEDULED + 2 + page_no)
        saved = runtime.capture.save_result_page(request, new_operation_key(), owned)
        assert saved.kind is DbOutcomeKind.COMPLETED, saved.error
        saved_refs.append(saved.value)
        completion = result.outcome.settlement.evidence.data["file_completion"]
        if context["prior_completion"] is None:
            context["prior_completion"] = {"result_page_event_id": saved.value.event_id, "evidence": completion}
        cursor = saved.value.next_cursor
        if cursor is None:
            break
        page_no += 1
    pages = runtime.capture.read_result_pages(ticket, None, 128, owned).items
    assert [entry.identity for page in pages for entry in page.page.entries] == sorted(paths)
    assert [identity for page in pages for identity, _ in page.file_ids] == expected
    assert owned.connection.execute("SELECT COUNT(*) FROM device_files").fetchone() == (len(expected),)
    assert pages[-1].page.set_finalized is True
    previews = [entry for page in pages for entry in page.page.entries if entry.format_id == "lrf"]
    assert all(entry.paired_identity == entry.identity.rsplit(".", 1)[0] + ".OSV" for entry in previews)
    for preview in previews:
        original = runtime.capture.read_result_file_input(saved_refs[-1], preview.paired_identity,
            owned, allow_missing=True)
        if preview.paired_identity in baseline:
            assert original is None
        else:
            assert original is not None and original[0].identity == preview.paired_identity
    assert owned.connection.execute("SELECT COUNT(*) FROM device_files WHERE source_action_id IS NOT NULL").fetchone() == (0,)


async def test_osmo_formal_result_adapter_conflicts_when_prior_osv_disappears(
        owned, tmp_path, monkeypatch):
    paths = [ROOT + f"/a/{index:03}.OSV" for index in range(31)]
    original, preview = ROOT + "/z/Clip.OSV", ROOT + "/z/Clip.lrf"
    driver, runtime, ticket, context = await _world(owned, tmp_path, [], monkeypatch)
    filesystem = Filesystem([*paths, original, preview, ROOT + "/z/Z.tmp"])
    monkeypatch.setattr(driver, "_filesystem", lambda binding: filesystem)
    adapter = DriverResultListing(driver, BINDING, driver.contract.evidence,
        capture_repository=runtime.capture, owned=owned)
    output_scope = runtime.action(1)["execution_spec_json"]["output_scope"]

    first = await adapter.list_page(ticket, cursor=None, timeout_s=Decimal("40"),
        output_scope=output_scope, completion_context=context)
    first_save = runtime.capture.save_result_page(ResultPageSave(ticket, 1, None,
        validate_outcome(ticket, first.outcome, driver.contract.evidence), _SCHEDULED + 3),
        new_operation_key(), owned)
    assert first_save.kind is DbOutcomeKind.COMPLETED, first_save.error
    assert first.next_cursor == DirectoryCursor(BINDING, (ROOT,), 0, original)
    context["prior_completion"] = {"result_page_event_id": first_save.value.event_id,
        "evidence": first.outcome.settlement.evidence.data["file_completion"]}

    filesystem.paths = sorted([*paths, preview, ROOT + "/z/Z.tmp"])
    second = await adapter.list_page(ticket, cursor=first.next_cursor, timeout_s=Decimal("40"),
        output_scope=output_scope, completion_context=context)

    diagnostic = second.outcome.settlement.evidence.data["optional_preview_observations"][0]
    page = next(value.data for value in second.outcome.observations
        if value.type == "result_files_listed")
    assert second.outcome.error.code == "osmo360ii_result_conflict"
    assert second.outcome.settlement.evidence.version == 1
    assert page["set_finalized"] is False and page["entries"] == []
    assert diagnostic["state"] == "original_missing"
    assert diagnostic["original_identity"] == original
    assert diagnostic["osv_count"] == 0

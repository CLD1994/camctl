"""相机正式源保留单张拍摄身份、双角色及独立取回责任。"""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ..support.capture import capture_database, capture_runtime
from ..support.outputs import cleanup_factory, submit_output_document

from camctl.host_files.tasks import FileTaskExecutor
from camctl.session.supervision import Supervisor
from camctl.capture.handlers import capture_handler
from camctl.outputs.work_files import WorkFileLimits
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from camctl.persistence.repositories.capture import CaptureRepository
from .test_host_photo_flow import _world, _ORIGINAL, _PREVIEW, _NOW, _entry

pytestmark = pytest.mark.asyncio

async def test_device_photo_registers_pair_and_reclaims_host_copies(tmp_path):
    cfg, owned, driver, runtime, _ = await _world(tmp_path, storage="device")
    try:
        await capture_handler("camera_take_photo")(1, runtime)
        assert len(driver.requests) == 1
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (3,)
        files = owned.connection.execute("SELECT id,role,original_device_file_id,size_bytes,checksum_support,sha256,locator_json FROM device_files ORDER BY id").fetchall()
        assert len(files) == 2
        assert [(f[1], f[3], f[4], f[5]) for f in files] == [
            (2, None, 1, None), (3, None, 1, None)]
        assert files[1][2] == files[0][0]
        assert [json.loads(f[6])["variant"] for f in files] == ["original", "thumbnail"]
        assert all(json.loads(f[6])["camera_path"] == "/mnt/media/DCIM/B9996039.3FR" for f in files)
        assert all(json.loads(f[6])["camera_name"] == "B9996039.3FR" for f in files)
        outputs = owned.connection.execute("SELECT id,device_file_id,intermediate_file_id FROM outputs ORDER BY id").fetchall()
        assert [(f[1], f[2]) for f in outputs] == [(files[0][0], None), (files[1][0], None)]
        assert owned.connection.execute("SELECT output_id,original_output_id FROM output_origins").fetchall() == [(outputs[1][0], outputs[0][0])]
        paths = [Path(cfg.paths.staging) / r[0] for r in owned.connection.execute("SELECT relative_path FROM intermediate_files WHERE owner_action_id=1")]
        assert len(paths) == 2 and [p.read_bytes() for p in paths] == [_ORIGINAL, _PREVIEW]
        assert [call.argv[3] for call in driver.transport.calls].count("capture") == 1
        assert not any(call.argv[3] == "download" for call in driver.transport.calls)
        await _maintain(cfg, owned)
        assert all(not p.exists() for p in paths)
        assert owned.connection.execute("SELECT retention_state,cleanup_state FROM intermediate_files WHERE owner_action_id=1").fetchall() == [(2,4),(2,4)]
        assert owned.connection.execute("SELECT presence_state FROM device_files ORDER BY id").fetchall() == [(2,), (2,)]
        await capture_handler("camera_take_photo")(1, runtime)
        assert len(driver.requests) == 1
    finally:
        owned.connection.close()

@pytest.mark.parametrize("binding",["match","missing","changed"])
async def test_device_cleanup_without_capability_fails_each_item_without_attempt(tmp_path,binding):
    from camctl.acceptance.input import ParsedInput
    from camctl.acceptance.service import CommandMode
    from camctl.contracts.values import new_operation_key
    from camctl.devices.catalog import DriverDefinitions, build_catalog
    from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID, driver_definition
    from camctl.devices.drivers.registry import DriverRegistry
    from camctl.outputs.cleanup_flow import advance_cleanup
    from camctl.persistence.repositories.acceptance import AcceptanceRepository, ProcessInput
    from camctl.persistence.models import DbOutcomeKind
    from .test_host_photo_flow import _input_time, _entry

    cfg, owned, driver, runtime, _ = await _world(tmp_path, storage="device")
    try:
        await capture_handler("camera_take_photo")(1, runtime)
        accepted = (await submit_output_document(
        await capture_database(owned),
        catalog=build_catalog(cfg, DriverDefinitions({DRIVER_ID:driver_definition()})),
        body={
            "request_id":"2", "created_at":_input_time(), "name":"清理相机源",
            "actions":[{"name":"清理", "type":"delete_action_outputs", "scheduled_at":_input_time(),
                        "params":{"output_ids":["1","2"]}}]},
        occurred_at=_NOW,
    ))
        assert accepted.plan_id is not None, accepted
        from dataclasses import replace
        devices = {} if binding == "missing" else cfg.devices
        entry = _entry(driver)
        if binding == "changed":
            devices = {"cam-1": {"kind":"camera", "driver":"other"}}
            entry = replace(entry, driver_id="other")
        cleanup = (await cleanup_factory(
        devices=devices,
        drivers=DriverRegistry((entry,)),
        max_delete_attempts=2,
        max_query_attempts=2,
        staging=Path(cfg.paths.staging),
        occurred_at=lambda:_NOW,
    )(await capture_database(owned)))
        for _ in range(5):
            await advance_cleanup(cleanup)
        rows = owned.connection.execute("SELECT status,error_details_json FROM cleanup_items ORDER BY id").fetchall()
        assert len(rows) == 2
        assert all(row[0] == 5 for row in rows), rows
        from camctl.contracts.workflow_errors import registered_error_spec
        codes=[registered_error_spec("item_error_ids.cleanup_items",r[0])[0] for r in owned.connection.execute("SELECT error_code FROM cleanup_items ORDER BY id")]
        assert codes == ["file_delete_failed" if binding == "match" else "device_binding_unavailable"]*2
        assert owned.connection.execute("SELECT COUNT(*) FROM operation_runs WHERE cleanup_item_id IS NOT NULL").fetchone() == (0,)
        assert owned.connection.execute("SELECT presence_state FROM device_files ORDER BY id").fetchall() == [(2,),(2,)]
        assert owned.connection.execute("SELECT status FROM actions WHERE id=2").fetchone() == (4,)
    finally:
        owned.connection.close()

@pytest.mark.parametrize("first,current", [("device","host"),("host","device")])
async def test_original_storage_controls_full_capture_after_config_change(tmp_path, first, current):
    from camctl.bootstrap.capture_assembly import session_capture_assembly
    from camctl.devices.drivers.registry import DriverRegistry
    from .test_host_photo_flow import _config, _entry
    cfg, owned, driver, _, _ = await _world(tmp_path, storage=first)
    try:
        changed = _config(tmp_path,storage=current)
        runtime = (await capture_runtime(
        owned,
        driver=driver,
        evidence=_entry(driver).evidence,
        devices=changed.devices,
        drivers=DriverRegistry((_entry(driver),)),
        staging=Path(cfg.paths.staging),
        wait_config=lambda _:None,
        wall_us=lambda:_NOW,
    ))
        await capture_handler("camera_take_photo")(1,runtime)
        assert len(driver.requests) == 1
        status, execution = owned.connection.execute(
            "SELECT status,execution_spec_json FROM actions WHERE id=1").fetchone()
        assert status == 3
        spec = json.loads(execution)
        assert spec["output_storage"] == first
        assert spec.get("photo_source_policy") == ("deferred" if first == "device" else None)
        rows = owned.connection.execute("SELECT device_file_id,intermediate_file_id FROM outputs").fetchall()
        assert len(rows) == 2
        assert all((row[0] is not None) == (first == "device") for row in rows)
    finally:
        owned.connection.close()

@pytest.mark.parametrize("interruption", ["file_completion", "finish"])
@pytest.mark.parametrize("committed", [False, True])
async def test_device_registration_resumes_original_request_without_camera(tmp_path, monkeypatch, interruption, committed, capture_resources):
    from camctl.contracts.values import ConsistencyError
    from camctl.persistence.models import DbOutcome, DbOutcomeKind
    from camctl.devices.drivers.registry import DriverRegistry

    cfg, owned, driver, _, _ = await _world(tmp_path, storage="device")
    devices = dict(cfg.devices)
    factory = await capture_resources.factory(owned, driver=driver, evidence=_entry(driver).evidence,
        devices=devices, drivers=DriverRegistry((_entry(driver),)), staging=Path(cfg.paths.staging),
        wait_config=lambda _: None, wall_us=lambda: _NOW, monotonic_ns=lambda: 9_000_000_000)
    runtime = await factory(owned)
    method = "save_file_completion" if interruption == "file_completion" else "finish_capture"
    original = getattr(CaptureRepository, method)
    saved_calls = []
    first_receipts = []

    def interrupted(repository, request, key, current):
        if (getattr(request, "action_id", 1) != 1 or
                getattr(request, "file_id", 1) not in (1, 2)):
            return original(repository, request, key, current)
        saved_calls.append((request, key))
        if committed:
            result = original(repository, request, key, current)
            assert result.kind is DbOutcomeKind.COMPLETED, result.error
            first_receipts.append(result)
        unknown = DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError("回执未知"))
        first_receipts.append(unknown)
        return unknown

    monkeypatch.setattr(CaptureRepository, method, interrupted)
    try:
        with pytest.raises(ConsistencyError):
            await capture_handler("camera_take_photo")(1, runtime)
        assert len(driver.requests) == 1
        assert saved_calls and all(call == saved_calls[0] for call in saved_calls)
        assert first_receipts[-1].kind is DbOutcomeKind.UNKNOWN
        assert len(first_receipts) == len(saved_calls) * (2 if committed else 1)
        request, key = saved_calls[0]
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?", (str(key),)).fetchone() == (int(committed),)
        original_call_count = len(saved_calls)
        devices.pop("cam-1")
        runtime = await factory(owned)

        def resumed(repository, current_request, current_key, current):
            if (getattr(current_request, "action_id", 1) == 1 and
                    getattr(current_request, "file_id", 1) == getattr(request, "file_id", 1)):
                saved_calls.append((current_request, current_key))
            return original(repository, current_request, current_key, current)

        monkeypatch.setattr(CaptureRepository, method, resumed)
        await capture_handler("camera_take_photo")(1, runtime)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (3,)
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (2,)
        assert len(driver.requests) == 1
        same = [call for call in saved_calls if call[0].__class__ is request.__class__
                and getattr(call[0], "file_id", None) == getattr(request, "file_id", None)]
        assert len(same) > original_call_count and all(call == (request, key) for call in same)
        assert owned.connection.execute(
            "SELECT COUNT(*) FROM history_transactions WHERE operation_key=?", (str(key),)).fetchone() == (1,)
    finally:
        owned.connection.close()


async def test_device_sync_cache_archive_error_preserves_source_and_other_role(tmp_path, monkeypatch):
    from camctl.host_files import capture_sources

    cfg, owned, driver, runtime, _ = await _world(tmp_path, storage="device")
    link = capture_sources.os.link
    attempts = []

    def denied(source, destination, *args, **kwargs):
        if source == "capture.3fr":
            attempts.append((source, destination))
            raise PermissionError("同步原图归档被拒绝")
        return link(source, destination, *args, **kwargs)

    monkeypatch.setattr(capture_sources.os, "link", denied)
    try:
        await capture_handler("camera_take_photo")(1, runtime)
        assert len(attempts) == 1
        assert len(driver.requests) == 1
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (3,)
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (2,)
        assert owned.connection.execute("SELECT size_bytes,sha256 FROM device_files ORDER BY id").fetchall() == [(None, None)] * 2
        rows = owned.connection.execute(
            "SELECT relative_path,size_bytes,sha256,last_error_json FROM intermediate_files ORDER BY id").fetchall()
        assert rows[0][1:3] == (None, None) and rows[0][3] is not None
        assert rows[1][1:3] == (len(_PREVIEW), None) and rows[1][3] is None
        assert (Path(cfg.paths.staging) / rows[1][0]).read_bytes() == _PREVIEW
        assert (Path(cfg.paths.staging) / "capture-work/1/capture.3fr").read_bytes() == _ORIGINAL
        await capture_handler("camera_take_photo")(1, runtime)
        assert len(driver.requests) == 1
    finally:
        owned.connection.close()


async def test_cancel_after_reliable_photo_is_rejected_and_retains_device_pair(tmp_path, monkeypatch):
    from camctl.host_files import capture_sources
    from .test_host_photo_flow import _cancel_through_service
    cfg,owned,driver,runtime,_=await _world(tmp_path,storage="device",cancel_action=True)
    original=capture_sources.archive_photo_sync_cache
    canceled=False
    async def cancel_after_archive(*args, **kwargs):
        nonlocal canceled
        await original(*args, **kwargs)
        if not canceled:
            canceled=True
            result = await _cancel_through_service(owned, runtime)
            assert result[0] == 4
    monkeypatch.setattr(capture_sources,"archive_photo_sync_cache",cancel_after_archive)
    try:
        await capture_handler("camera_take_photo")(1,runtime)
        assert canceled
        assert len(driver.requests)==1
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone()==(3,)
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone()==(2,)
    finally:
        owned.connection.close()

async def test_unknown_device_capture_retains_workspace_and_never_restarts(tmp_path):
    from .test_host_photo_flow import _PhotoDriver
    driver=_PhotoDriver(tmp_path/"staging",response="unknown")
    cfg,owned,driver,runtime,_=await _world(tmp_path,storage="device",driver=driver)
    try:
        for _ in range(2):
            await capture_handler("camera_take_photo")(1,runtime)
        assert len(driver.requests)==1
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone()==(0,)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone()==(4,)
        assert list((Path(cfg.paths.staging)/"capture-work").rglob("*.3fr"))
    finally:
        owned.connection.close()

async def _maintain(cfg, owned):
    from camctl.outputs.work_files import WorkFileContext, clean_work_files
    from camctl.persistence.repositories.outputs import OutputsRepository
    database = await capture_database(owned)
    supervisor = Supervisor()
    executor = FileTaskExecutor(supervisor)
    try:
        await clean_work_files(WorkFileContext(repository=OutputsRepository(), database=database,
            staging=Path(cfg.paths.staging), occurred_at=_NOW, limits=WorkFileLimits(32, 10),
            executor=executor))
    finally:
        settled = await supervisor.drain_required()
        assert not settled.failed
        assert not executor.unfinished_files()


async def test_device_terminal_replay_after_cleanup_preserves_report_and_history(tmp_path, monkeypatch):
    from camctl.contracts.schemas import validate_document
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.repositories.history import HistoryRepository
    from camctl.persistence.models import DbOutcomeKind
    from camctl.reporting.generation import GenerationSpec,generate_report_file
    from camctl.reporting.policy import ReportingRepository
    cfg,owned,driver,runtime,_=await _world(tmp_path,storage="device")
    saved=[]
    original=CaptureRepository.finish_capture
    def remember(repository,request,key,current):
        if request.action_id == 1:
            saved.append((request,key))
        return original(repository,request,key,current)
    monkeypatch.setattr(CaptureRepository,"finish_capture",remember)
    try:
        await capture_handler("camera_take_photo")(1,runtime)
        history=HistoryRepository(Path(cfg.paths.state_db))
        boundary=history.current_boundary()
        snapshot={(entity,i):history.restore_entity(entity,i,boundary)
                  for entity in ("action","device_file","intermediate_file","output")
                  for i in ((1,) if entity=="action" else (1,2))}
        await _maintain(cfg,owned)
        request,key=saved[0]
        before=owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone()
        for replay_key in (key,new_operation_key()):
            replay=original(CaptureRepository(),request,replay_key,owned)
            assert replay.kind is DbOutcomeKind.COMPLETED,replay.error
        assert owned.connection.execute("SELECT COUNT(*) FROM history_events").fetchone()==before
        for (entity,i),rows in snapshot.items():
            assert history.restore_entity(entity,i,boundary)==rows
        report=ReportingRepository().freeze_report(new_operation_key(),owned,occurred_at=_NOW).value.report
        generated=generate_report_file(GenerationSpec(db_path=Path(cfg.paths.state_db),report_id=report.report_id,
            from_wm=report.from_wm,to_wm=report.to_wm,frozen_event_id=report.boundary.last_event_id,
            staging_path=Path(cfg.paths.staging)/"reports"/"device.json"))
        document=json.loads(generated.path.read_bytes())
        validate_document("protocol/status-report.schema.json",document)
        photo=next(a for p in document["plans"] for a in p["actions"] if a["action_instance_id"]=="1")
        pair={o["kind"]:o for o in photo["outputs"]}
        assert pair["preview"]["preview_of_output_id"]==pair["original"]["output_id"]
        for kind in ("original", "preview"):
            assert pair[kind]["checksum"] == {"status": "not_obtained"}
            assert "size" not in pair[kind] and "media_type" not in pair[kind]
    finally:
        owned.connection.close()


async def test_failed_host_copy_cleanup_retries_on_next_maintenance(tmp_path,monkeypatch):
    cfg,owned,driver,runtime,_=await _world(tmp_path,storage="device")
    try:
        await capture_handler("camera_take_photo")(1,runtime)
        relative, = owned.connection.execute(
            "SELECT relative_path FROM intermediate_files ORDER BY id LIMIT 1").fetchone()
        target = Path(cfg.paths.staging) / relative
        remove=Path.unlink
        def denied(path, *args, **kwargs):
            if path == target:
                raise PermissionError("主机副本删除被拒绝")
            return remove(path, *args, **kwargs)
        monkeypatch.setattr(Path,"unlink",denied)
        await _maintain(cfg,owned)
        rows=owned.connection.execute("SELECT cleanup_state,last_error_json,relative_path FROM intermediate_files ORDER BY id").fetchall()
        assert rows[0][0]==5 and rows[0][1] is not None
        assert (Path(cfg.paths.staging)/rows[0][2]).is_file()
        assert rows[1][0]==4
        monkeypatch.setattr(Path,"unlink",remove)
        await _maintain(cfg,owned)
        assert owned.connection.execute("SELECT cleanup_state FROM intermediate_files ORDER BY id").fetchall()==[(4,),(4,)]
        assert not (Path(cfg.paths.staging)/rows[0][2]).exists()
        assert owned.connection.execute("SELECT availability FROM outputs ORDER BY id").fetchall()==[(1,),(1,)]
    finally:
        owned.connection.close()


@pytest.mark.parametrize("defect",["empty","single","host","sha","pair","role","owner","complete","support","media","locator","variant"])
async def test_device_success_requires_complete_matching_device_pair(tmp_path,monkeypatch,defect):
    from dataclasses import replace
    from camctl.contracts.values import new_operation_key
    from camctl.outputs.catalog import FileReference
    from camctl.persistence.models import DbOutcomeKind
    from camctl.contracts.values import ConsistencyError
    cfg,owned,driver,runtime,_=await _world(tmp_path,storage="device")
    request=[]
    def interrupt(repository, value, key, current):
        from camctl.persistence.models import DbOutcome
        request.append(value)
        error = OSError("登记前中断")
        return DbOutcome(DbOutcomeKind.NOT_EXECUTED, error=error, handoff_error=error)
    finish_capture = CaptureRepository.finish_capture
    monkeypatch.setattr(CaptureRepository, "finish_capture", interrupt)
    try:
        with pytest.raises(OSError):
            await capture_handler("camera_take_photo")(1,runtime)
        command=request[0]
        original,preview=command.drafts
        if defect=="empty": command=replace(command,drafts=())
        elif defect=="single": command=replace(command,drafts=(original,))
        elif defect=="host": command=replace(command,drafts=(replace(original,file=FileReference(intermediate_file_id=1),original_name="capture.3fr",media_type="image/x-hasselblad-3fr"),preview))
        elif defect=="sha": command=replace(command,drafts=(replace(original,sha256="f"*64),preview))
        elif defect=="pair": command=replace(command,drafts=(original,replace(preview,original_batch_file_id=2)))
        elif defect in ("media", "locator", "variant"):
            if defect == "media":
                owned.connection.execute("UPDATE device_files SET media_type='image/png' WHERE id=2")
            else:
                locator = json.loads(owned.connection.execute("SELECT locator_json FROM device_files WHERE id=2").fetchone()[0])
                if defect == "locator":
                    locator["camera_name"] = "other.3FR"
                else:
                    locator["variant"] = "original"
                owned.connection.execute("UPDATE device_files SET locator_json=? WHERE id=2", (json.dumps(locator),))
            owned.connection.commit()
        else:
            column,value={"role":("role",1),"owner":("source_action_id",None),"complete":("completion_state",1),"support":("checksum_support",2)}[defect]
            # 故障注入制造不一致投影；仓储不得把损坏事实登记为成功。
            owned.connection.execute("PRAGMA ignore_check_constraints=ON")
            owned.connection.execute(f"UPDATE device_files SET {column}=? WHERE id=2",(value,))
            owned.connection.commit()
        monkeypatch.setattr(CaptureRepository, "finish_capture", finish_capture)
        before = tuple(owned.connection.iterdump())
        result=CaptureRepository().finish_capture(command,new_operation_key(),owned)
        assert result.kind is DbOutcomeKind.ROLLED_BACK,(defect, result)
        assert not owned.connection.in_transaction, defect
        assert tuple(owned.connection.iterdump()) == before, defect
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone()==(2,)
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone()==(0,)
        assert owned.connection.execute("SELECT retention_state FROM intermediate_files").fetchall()==[(1,),(1,)]
    finally:
        owned.connection.close()


async def test_cleanup_continues_from_unsupported_device_to_local_host_sources(tmp_path):
    from camctl.acceptance.input import ParsedInput
    from camctl.acceptance.service import CommandMode
    from camctl.contracts.values import new_operation_key
    from camctl.devices.catalog import DriverDefinitions,build_catalog
    from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID,driver_definition
    from camctl.devices.drivers.registry import DriverRegistry
    from camctl.outputs.cleanup_flow import advance_cleanup
    from camctl.persistence.repositories.acceptance import AcceptanceRepository,ProcessInput
    from camctl.persistence.models import DbOutcomeKind
    from .test_host_photo_flow import _config,_entry,_input_time,_accept_and_start_second_host_photo
    cfg,owned,driver,runtime,_=await _world(tmp_path,storage="device")
    try:
        await capture_handler("camera_take_photo")(1,runtime)
        hostcfg=_config(tmp_path,storage="host")
        second=_accept_and_start_second_host_photo(hostcfg,owned)
        await capture_handler("camera_take_photo")(second,runtime)
        before=owned.connection.execute("SELECT relative_path FROM intermediate_files WHERE owner_action_id=?",(second,)).fetchall()
        accepted=(await submit_output_document(
        await capture_database(owned),
        catalog=build_catalog(cfg,DriverDefinitions({DRIVER_ID:driver_definition()})),
        body={
            "request_id":"3","created_at":_input_time(),"name":"分别清理",
            "actions":[{"name":"mixed","type":"delete_action_outputs","scheduled_at":_input_time(),
                        "params":{"output_ids":["1","2","3","4"]}}]},
        occurred_at=_NOW,
    ))
        assert accepted.plan_id is not None, accepted
        supervisor = Supervisor()
        executor = FileTaskExecutor(supervisor)
        cleanup=(await cleanup_factory(
        devices=cfg.devices,
        drivers=DriverRegistry((_entry(driver),)),
        max_delete_attempts=2,
        max_query_attempts=2,
        staging=Path(cfg.paths.staging),
        file_executor=executor,
        occurred_at=lambda:_NOW,
    )(await capture_database(owned)))
        try:
            await advance_cleanup(cleanup)
        finally:
            settled = await supervisor.drain_required()
            assert not settled.failed
            assert not executor.unfinished_files()
        assert owned.connection.execute("SELECT status FROM cleanup_items ORDER BY id").fetchall()==[(5,),(5,),(4,),(4,)]
        assert all(not (Path(cfg.paths.staging)/r[0]).exists() for r in before)
        assert owned.connection.execute("SELECT COUNT(*) FROM operation_runs WHERE cleanup_item_id IN (1,2)").fetchone()==(0,)
        assert owned.connection.execute("SELECT presence_state FROM device_files").fetchall()==[(2,),(2,)]
    finally:
        owned.connection.close()


@pytest.mark.parametrize("capture_format,camera_name", [("raw", "B9996039.3FR"), ("jpeg", "B9996039.JPG")])
async def test_device_source_uses_camera_basename_and_full_locator(tmp_path, capture_format, camera_name):
    import asyncio
    from decimal import Decimal
    from camctl.devices.drivers.hasselblad.driver import HasselbladDriver
    from camctl.devices.read_session import DeferredSourceFile
    from camctl.host_files.models import BoundDirectories
    from camctl.operations.models import AttemptTicket
    from camctl.operations.process import LocalExit, RawToolOutcome
    from .test_host_photo_flow import _JPEG_ORIGINAL

    cfg, owned, captured_driver, runtime, _ = await _world(tmp_path, storage="device", capture_format=capture_format)
    original_bytes = _JPEG_ORIGINAL if capture_format == "jpeg" else _ORIGINAL
    payloads = {"original": original_bytes, "thumbnail": _PREVIEW}

    class CapturedDownloadTransport:
        def __init__(self):
            self.downloads = []

        async def run(self, spec, _stop):
            command = spec.argv[3]
            if command == "list":
                output = b"vid=0x2756 pid=0x000a  bus=  1 addr= 23\n"
            elif command == "info":
                output = b"serial : SERIAL-1\n"
            else:
                assert spec.argv[3] == "download"
                variant = spec.argv[4]
                assert spec.argv[5] == camera_name
                assert spec.argv[6] == "--output"
                self.downloads.append((variant, spec.argv[5]))
                (Path(spec.argv[7]) / "downloaded.bin").write_bytes(payloads[variant])
                output = b"downloaded\n"
            return RawToolOutcome(LocalExit(exit_code=0), output, None, None, stderr=b"")

    try:
        await capture_handler("camera_take_photo")(1, runtime)
        rows = owned.connection.execute("SELECT identity_key,locator_json,size_bytes,sha256 FROM device_files ORDER BY id").fetchall()
        assert len(rows) == 2
        transport = CapturedDownloadTransport()
        downloader = HasselbladDriver(cfg.devices, BoundDirectories(Path(cfg.paths.staging)),
            monotonic_ns=lambda: 0, transport=transport)
        for (identity_key, locator_json, size, digest), variant in zip(rows, ("original", "thumbnail")):
            locator = json.loads(locator_json)
            assert locator["camera_path"] == "/mnt/media/DCIM/" + camera_name
            assert locator["camera_name"] == camera_name
            assert locator["variant"] == variant
            assert "sha256" not in locator and size is None and digest is None
            source = DeferredSourceFile(identity_key, locator)
            session = await downloader.open_read(source, 0, AttemptTicket(1, "read", "22", "read/22", 31), idle_timeout_s=Decimal("10"))
            assert session.source_size == len(payloads[variant])
            chunk = await asyncio.to_thread(session.read_chunk, session.source_size)
            assert chunk.eof and chunk.data == payloads[variant]
            ended = await session.wait_stopped()
            assert ended.stopped and ended.error is None and ended.bytes_read == len(payloads[variant])
        assert transport.downloads == [("original", camera_name), ("thumbnail", camera_name)]
        assert len(captured_driver.requests) == 1
    finally:
        owned.connection.close()


@pytest.mark.parametrize("capture_format", ["raw", "jpeg"])
@pytest.mark.parametrize("committed", [False, True])
async def test_device_cold_resume_preserves_original_source_without_camera(tmp_path, monkeypatch, capture_format, committed, capture_resources):
    from camctl.contracts.values import ConsistencyError
    from camctl.devices.drivers.registry import DriverRegistry
    from camctl.persistence.models import DbOutcome, DbOutcomeKind

    cfg, owned, driver, runtime, _ = await _world(tmp_path, storage="device", capture_format=capture_format)
    original = CaptureRepository.finish_capture
    saves = []

    def interrupt(repository, request, key, current):
        saves.append((request, key))
        if committed:
            receipt = original(repository, request, key, current)
            assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
        return DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError("完成事务回执未知"))

    monkeypatch.setattr(CaptureRepository, "finish_capture", interrupt)
    try:
        with pytest.raises(ConsistencyError):
            await capture_handler("camera_take_photo")(1, runtime)
        assert saves and all(call == saves[0] for call in saves)
        request, key = saves[0]
        assert owned.connection.execute("SELECT COUNT(*) FROM history_transactions WHERE operation_key=?",
            (str(key),)).fetchone() == (int(committed),)
        start_before = owned.connection.execute(
            "SELECT a.result_json FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id "
            "WHERE r.responsibility_key='start/1'").fetchone()
        files_before = owned.connection.execute(
            "SELECT id,identity_key,locator_json,size_bytes,sha256 FROM device_files ORDER BY id").fetchall()
        assert len(files_before) == 2 and all(row[3:] == (None, None) for row in files_before)
        execution_before, = owned.connection.execute("SELECT execution_spec_json FROM actions WHERE id=1").fetchone()
        assert json.loads(execution_before)["photo_source_policy"] == "deferred"
        monkeypatch.setattr(CaptureRepository, "finish_capture", original)
        owned.connection.close()
        database = await capture_resources.restart_database(Path(cfg.paths.state_db))
        owned = open_existing(Path(cfg.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig())
        resumed = await capture_runtime(database, driver=driver, evidence=_entry(driver).evidence,
            devices={}, drivers=DriverRegistry(()), staging=Path(cfg.paths.staging),
            wait_config=lambda _: None, wall_us=lambda: _NOW, monotonic_ns=lambda: 9_000_000_000)
        await capture_handler("camera_take_photo")(1, resumed)
        assert owned.connection.execute("SELECT status,execution_spec_json FROM actions WHERE id=1").fetchone() == (3, execution_before)
        assert owned.connection.execute("SELECT id,identity_key,locator_json,size_bytes,sha256 FROM device_files ORDER BY id").fetchall() == files_before
        assert owned.connection.execute("SELECT a.result_json FROM operation_attempts a JOIN operation_runs r ON r.id=a.run_id WHERE r.responsibility_key='start/1'").fetchone() == start_before
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (2,)
        assert len(driver.requests) == 1
        if committed:
            assert owned.connection.execute("SELECT COUNT(*) FROM history_transactions WHERE operation_key=?",
                (str(key),)).fetchone() == (1,)
        await capture_handler("camera_take_photo")(1, resumed)
        assert len(driver.requests) == 1
    finally:
        owned.connection.close()


@pytest.mark.parametrize("capture_format", ["raw", "jpeg"])
async def test_photo_recovery_uses_saved_format_and_camera_locator(tmp_path, monkeypatch, capture_format, capture_resources):
    storage = "host"
    from camctl.bootstrap.capture_assembly import session_capture_assembly
    from camctl.capture import host_photo
    from camctl.devices.drivers.registry import DriverRegistry
    from camctl.persistence.repositories.history import HistoryRepository
    from .test_host_photo_flow import _JPEG_ORIGINAL

    cfg, owned, driver, runtime, _ = await _world(tmp_path, storage=storage, capture_format=capture_format)
    archive = host_photo.archive_capture_file

    async def interrupt_after_original(*args, **kwargs):
        await archive(*args, **kwargs)
        raise OSError("原图归档后重启")

    monkeypatch.setattr(host_photo, "archive_capture_file", interrupt_after_original)
    try:
        with pytest.raises(OSError, match="原图归档后重启"):
            await capture_handler("camera_take_photo")(1, runtime)
        pair_before = owned.connection.execute("SELECT id,purpose,relative_path FROM intermediate_files ORDER BY id").fetchall()
        start_before = owned.connection.execute("SELECT result_json FROM operation_attempts").fetchone()
        monkeypatch.setattr(host_photo, "archive_capture_file", archive)
        owned.connection.close()
        owned = open_existing(Path(cfg.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig())
        await capture_resources.restart_database(owned)
        resumed = (await capture_runtime(
        owned,
        driver=driver,
        evidence=_entry(driver).evidence,
        devices={},
        drivers=DriverRegistry(()),
        staging=Path(cfg.paths.staging),
        wait_config=lambda _: None,
        wall_us=lambda: _NOW,
    ))
        await capture_handler("camera_take_photo")(1, resumed)
        assert owned.connection.execute("SELECT id,purpose,relative_path FROM intermediate_files ORDER BY id").fetchall() == pair_before
        assert owned.connection.execute("SELECT result_json FROM operation_attempts").fetchone() == start_before
        original_bytes = _JPEG_ORIGINAL if capture_format == "jpeg" else _ORIGINAL
        assert [row[0] for row in owned.connection.execute("SELECT size_bytes FROM intermediate_files ORDER BY id")] == [len(original_bytes), len(_PREVIEW)]
        assert [row[0] for row in owned.connection.execute("SELECT sha256 FROM intermediate_files ORDER BY id")] == [hashlib.sha256(original_bytes).hexdigest(), hashlib.sha256(_PREVIEW).hexdigest()]
        extension = "jpg" if capture_format == "jpeg" else "3fr"
        assert pair_before[0][2].endswith("." + extension)
        history = HistoryRepository(Path(cfg.paths.state_db))
        boundary = history.current_boundary()
        at_boundary = history.restore_entity("action", 1, boundary)
        assert at_boundary[("actions", 1)]["status"] == 3
        assert len(driver.requests) == 1
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (2,)
        if storage == "device":
            expected_name = "B9996039.JPG" if capture_format == "jpeg" else "B9996039.3FR"
            for (locator_json,) in owned.connection.execute("SELECT locator_json FROM device_files"):
                assert json.loads(locator_json)["camera_path"] == "/mnt/media/DCIM/" + expected_name
        await capture_handler("camera_take_photo")(1, resumed)
        assert history.restore_entity("action", 1, boundary) == at_boundary
        assert len(driver.requests) == 1
    finally:
        owned.connection.close()


@pytest.mark.parametrize("capture_format", ["raw", "jpeg"])
@pytest.mark.parametrize("cleanup_state", ["pending", "running", "completed", "failed", "unknown"])
async def test_device_photo_original_file_requests_remain_verifiable_after_copy_cleanup(tmp_path, capture_format, cleanup_state):
    from camctl.contracts.values import new_operation_key
    from camctl.history.host_photo_request import PrepareHostPhoto, parse_host_photo_request
    from camctl.history.photo_cache_request import parse_photo_sync_cache_request
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.host_capture import HostCaptureRepository

    cfg, owned, driver, runtime, _ = await _world(tmp_path, storage="device", capture_format=capture_format)
    verification = None
    try:
        await capture_handler("camera_take_photo")(1, runtime)
        repository = HostCaptureRepository()
        prepared = repository.prepare_host_photo(1, _NOW, new_operation_key(), owned)
        assert prepared.kind is DbOutcomeKind.COMPLETED, prepared.error
        original_requests = []
        for key, body_json in owned.connection.execute(
                "SELECT t.operation_key,e.body_json FROM history_transactions t JOIN history_events e ON e.transaction_id=t.id WHERE e.event_type=26 ORDER BY e.id"):
            evidence = json.loads(body_json)["evidence"]
            if "host_photo_request" in evidence:
                original_requests.append((parse_host_photo_request(evidence["host_photo_request"], reason=1), key))
            elif "photo_sync_cache_request" in evidence:
                original_requests.append((parse_photo_sync_cache_request(evidence["photo_sync_cache_request"]), key))
        assert len(original_requests) == 3
        assert isinstance(original_requests[0][0], PrepareHostPhoto)
        assert [request.role for request, _ in original_requests[1:]] == ["original", "thumbnail"]
        assert all(request.archive_error is None for request, _ in original_requests[1:])
        await _advance_photo_copy_cleanup(cfg, owned, cleanup_state)
        expected_cleanup = {"pending": 2, "running": 3, "completed": 4, "failed": 5, "unknown": 6}[cleanup_state]
        assert owned.connection.execute("SELECT retention_state,cleanup_state FROM intermediate_files").fetchall() == [(2, expected_cleanup)] * 2
        verification = open_existing(Path(cfg.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig())
        transaction_count = owned.connection.execute("SELECT COUNT(*) FROM history_transactions").fetchone()
        for request, key in original_requests:
            verified = repository.verify_operation(request, key, verification)
            assert verified.kind is DbOutcomeKind.COMPLETED, verified.error
            if isinstance(request, PrepareHostPhoto):
                assert verified.value == prepared.value
                repeated = repository.prepare_host_photo(request.action_id, request.occurred_at, key, owned)
                fresh = repository.prepare_host_photo(request.action_id, request.occurred_at, new_operation_key(), owned)
            else:
                assert verified.value == request.file_id
                repeated = repository.save_photo_sync_cache_complete(request, key, owned)
                fresh = repository.save_photo_sync_cache_complete(request, new_operation_key(), owned)
            assert repeated.kind is DbOutcomeKind.COMPLETED, repeated.error
            assert repeated.value == verified.value
            assert fresh.kind is DbOutcomeKind.ROLLED_BACK
        assert owned.connection.execute("SELECT COUNT(*) FROM history_transactions").fetchone() == transaction_count
        assert len(driver.requests) == 1
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (2,)
    finally:
        if verification is not None:
            verification.connection.close()
        owned.connection.close()


async def _advance_photo_copy_cleanup(cfg, owned, cleanup_state):
    from dataclasses import replace
    from camctl.contracts.values import new_operation_key
    from camctl.outputs.work_files import (
        CleanupIntent, CleanupResultSave, RetentionRelease, WorkFileFailure, WorkFileOutcome,
    )
    from camctl.persistence.models import DbOutcomeKind
    from camctl.persistence.repositories.outputs import OutputsRepository
    from camctl.contracts.enums import enum_for
    from ..support.output_planning import capture_operation, EventSequence
    from camctl.persistence.repositories.transactions import commit_operation

    if cleanup_state == "completed":
        await _maintain(cfg, owned)
        return
    repository = OutputsRepository()
    file_ids = [row[0] for row in owned.connection.execute("SELECT id FROM intermediate_files ORDER BY id")]
    for file_id in file_ids:
        released = repository.save_retention_release(RetentionRelease(file_id, _NOW), new_operation_key(), owned)
        assert released.kind is DbOutcomeKind.COMPLETED, released.error
        if cleanup_state == "pending":
            continue
        intent = repository.save_cleanup_intent(CleanupIntent(file_id, _NOW), new_operation_key(), owned)
        assert intent.kind is DbOutcomeKind.COMPLETED, intent.error
        if cleanup_state == "running":
            continue
        request = CleanupResultSave(file_id, WorkFileOutcome.FAILED,
            WorkFileFailure("action_work_file_delete_failed", {"file_id": str(file_id)}), _NOW)
        if cleanup_state == "failed":
            result = repository.save_cleanup_result(request, new_operation_key(), owned)
            assert result.kind is DbOutcomeKind.COMPLETED, result.error
        else:
            # 该消费者存储专项使用正式格式中的已保存UNKNOWN历史。
            proposal = capture_operation(owned, lambda current: repository.save_cleanup_result(
                request, new_operation_key(), current))
            event, = proposal.events
            row, = event.rows
            changed = replace(event, rows=(replace(row, after=replace(row.after, values={
                **row.after.values,
                "cleanup_state": int(enum_for("intermediate_files.cleanup_state").UNKNOWN),
            })),))
            receipt = commit_operation(EventSequence((changed,), proposal), new_operation_key(), owned)
            assert receipt.kind == "completed", receipt.error


@pytest.mark.parametrize("phase", ["running", "failed"])
@pytest.mark.parametrize("capture_format", ["raw", "jpeg"])
async def test_photo_original_requests_reject_released_copies_without_registered_pair(tmp_path, monkeypatch, phase, capture_format):
    from dataclasses import replace
    from camctl.capture.media import RecordingFailure
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.models import DbOutcome, DbOutcomeKind
    from camctl.persistence.repositories.capture import CaptureRepository
    from camctl.persistence.repositories.host_capture import HostCaptureRepository

    cfg, owned, driver, runtime, _ = await _world(tmp_path, storage="device", capture_format=capture_format)
    commands = []
    def interrupt(repository, request, key, current):
        commands.append(request)
        error = OSError("正式产物登记前中断")
        return DbOutcome(DbOutcomeKind.NOT_EXECUTED, error=error, handoff_error=error)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(CaptureRepository, "finish_capture", interrupt)
            with pytest.raises(OSError, match="正式产物登记前中断"):
                await capture_handler("camera_take_photo")(1, runtime)
        original = commands[0]
        if phase == "failed":
            failed = CaptureRepository().finish_capture(replace(original, drafts=(),
                failure=RecordingFailure("capture_failed", {"activity_id": "1", "reason": "invalid_outputs"})),
                new_operation_key(), owned)
            assert failed.kind is DbOutcomeKind.COMPLETED, failed.error
            await _advance_photo_copy_cleanup(cfg, owned, "pending")
        else:
            from camctl.outputs.work_files import RetentionRelease
            from camctl.persistence.repositories.outputs import OutputsRepository
            for file_id in (1, 2):
                refused = OutputsRepository().save_retention_release(RetentionRelease(file_id, _NOW), new_operation_key(), owned)
                assert refused.kind is DbOutcomeKind.ROLLED_BACK
            # 投影故障注入：即使错误状态出现，原申请核实也不能据此放行。
            owned.connection.execute("UPDATE intermediate_files SET retention_state=2,cleanup_state=2")
            owned.connection.commit()
        repository = HostCaptureRepository()
        for key, body_json in owned.connection.execute(
                "SELECT t.operation_key,e.body_json FROM history_transactions t JOIN history_events e ON e.transaction_id=t.id WHERE e.event_type=26 ORDER BY e.id").fetchall():
            body = json.loads(body_json)
            raw = body["evidence"].get("host_photo_request")
            if raw is None:
                continue
            from camctl.history.host_photo_request import parse_host_photo_request
            request = parse_host_photo_request(raw, reason=1)
            assert repository.verify_operation(request, key, owned).kind is DbOutcomeKind.UNKNOWN
            fresh = repository.prepare_host_photo(request.action_id, request.occurred_at, new_operation_key(), owned)
            assert fresh.kind is DbOutcomeKind.ROLLED_BACK
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
        assert len(driver.requests) == 1
    finally:
        owned.connection.close()




@pytest.mark.parametrize("timing", ["after", "waiting", "missing_cache", "failed_cache", "redownload", "read_end"])
async def test_deferred_photo_obtain_downloads_each_role_and_records_delivery_bytes(tmp_path, monkeypatch, timing):
    from camctl.bootstrap.availability_assembly import AvailabilitySession
    from camctl.bootstrap.obtain_assembly import obtain_flow
    from camctl.operations.recovery import RecoveryBoundary
    from camctl.devices.catalog import DriverDefinitions, build_catalog
    from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID, driver_definition
    from camctl.devices.drivers.hasselblad.driver import HasselbladDriver
    from camctl.devices.drivers.registry import DriverRegistry
    from camctl.host_files.models import BoundDirectories
    from camctl.operations.process import LocalExit, RawToolOutcome
    from ..support.outputs import obtain_factory
    from .test_host_photo_flow import _input_time

    cfg, owned, capture_driver, runtime, _ = await _world(tmp_path, storage="device")
    payloads = {"original": b"B" * 180 if timing == "redownload" else _ORIGINAL,
                "thumbnail": _PREVIEW + b"-camera-new-preview"}
    first_original = b"A" * 140
    downloads_by_role = {"original": 0, "thumbnail": 0}
    calls = []
    monotonic = [0]

    class DownloadTransport:
        async def run(self, spec, _stop):
            command = spec.argv[3]
            calls.append(tuple(spec.argv[3:]))
            if command == "list":
                output = b"vid=0x2756 pid=0x000a  bus=  1 addr= 23\n"
            elif command == "info":
                output = b"serial : SERIAL-1\n"
            elif command == "download":
                role, camera_name = spec.argv[4:6]
                assert camera_name == "B9996039.3FR"
                assert spec.argv[6] == "--output"
                downloads_by_role[role] += 1
                content = (first_original if timing == "redownload" and role == "original"
                           and downloads_by_role[role] == 1 else payloads[role])
                (Path(spec.argv[7]) / "downloaded.bin").write_bytes(content)
                output = b"downloaded\n"
            else:
                raise AssertionError(spec.argv)
            return RawToolOutcome(LocalExit(exit_code=0), output, None, None, stderr=b"")

    supervisor = Supervisor()
    executor = FileTaskExecutor(supervisor)
    availability = None
    try:
        if timing in ("after", "redownload", "read_end"):
            await capture_handler("camera_take_photo")(1, runtime)
        catalog = build_catalog(cfg, DriverDefinitions({DRIVER_ID: driver_definition()}))
        for request_id, role in (("2", "original"), ("3", "preview")):
            params = {"source": {"action_instance_id": "1"}, "purpose": "manual"}
            if role == "preview":
                params["filter"] = "preview"
            accepted = await submit_output_document(await capture_database(owned), catalog=catalog,
                body={"request_id": request_id, "created_at": _input_time(), "name": role,
                    "actions": [{"name": role, "type": "obtain_action_outputs",
                                 "scheduled_at": _input_time(), "params": params}]}, occurred_at=_NOW)
            assert accepted.plan_id is not None, accepted
        download_driver = HasselbladDriver(cfg.devices, BoundDirectories(Path(cfg.paths.staging)),
            monotonic_ns=lambda: 0, transport=DownloadTransport())
        registry = DriverRegistry((_entry(download_driver),))
        availability = AvailabilitySession(cfg.devices, registry,
            monotonic_ns=lambda: monotonic[0], occurred_at=lambda: _NOW,
            recovery_boundary=RecoveryBoundary.UNCONFIRMED, recovery_max_event_id=lambda: None)
        factory = obtain_factory(devices=cfg.devices, drivers=registry,
            staging=Path(cfg.paths.staging), ready=Path(cfg.paths.ready), processing=Path(cfg.paths.processing),
            occurred_at=lambda: _NOW, monotonic_ns=lambda: monotonic[0], segment_size=64, file_executor=executor,
            availability=availability)
        flow = obtain_flow(factory)
        context = SimpleNamespace(database=await capture_database(owned), wake=None)
        cache_errors = []
        if timing not in ("after", "redownload", "read_end"):
            await flow(context)
            assert owned.connection.execute("SELECT status FROM actions WHERE type=4 ORDER BY id").fetchall() == [(2,), (2,)]
            assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (0,)
            assert not calls
            await capture_handler("camera_take_photo")(1, runtime)
            if timing == "missing_cache":
                original, = owned.connection.execute(
                    "SELECT relative_path FROM intermediate_files WHERE owner_action_id=1 ORDER BY id LIMIT 1").fetchone()
                (Path(cfg.paths.staging) / original).unlink()
            elif timing == "failed_cache":
                import os
                original_read = os.read
                cache_path = Path(cfg.paths.staging) / "capture-originals/1.3fr"

                def read_with_error(fd, size):
                    try:
                        path = Path(os.readlink(f"/proc/self/fd/{fd}"))
                    except OSError:
                        path = None
                    if path == cache_path and not cache_errors:
                        cache_errors.append(path)
                        raise OSError("测试原图同步副本读取错误")
                    return original_read(fd, size)

                monkeypatch.setattr(os, "read", read_with_error)
        read_errors = []
        if timing == "redownload":
            import os
            original_read = os.read
            first_reads = []

            def fail_after_first_segment(fd, size):
                try:
                    path = os.readlink(f"/proc/self/fd/{fd}")
                except OSError:
                    path = ""
                if path.endswith("/downloaded.bin") and downloads_by_role["original"] == 1 and not read_errors:
                    if first_reads:
                        read_errors.append(path)
                        raise OSError("测试首轮相机读取中断")
                    first_reads.append(path)
                return original_read(fd, size)

            monkeypatch.setattr(os, "read", fail_after_first_segment)
        assert len(capture_driver.requests) == 1
        assert owned.connection.execute("SELECT size_bytes,sha256 FROM device_files ORDER BY id").fetchall() == [(None, None)] * 2
        fallback = None
        initial_progress = None
        verification_saves = []
        reliable_end = None
        if timing == "read_end":
            from camctl.persistence.models import DbOutcome, DbOutcomeKind
            from camctl.persistence.repositories.outputs import OutputsRepository
            original_verify = OutputsRepository.save_prepared

            def fail_verification(repository, request, key, current):
                if request.copy_id == 1 and request.verification is not None:
                    verification_saves.append((request, key))
                    if len(verification_saves) == 1:
                        return DbOutcome(DbOutcomeKind.NOT_EXECUTED, error=OSError("校验保存回执失败"))
                return original_verify(repository, request, key, current)

            monkeypatch.setattr(OutputsRepository, "save_prepared", fail_verification)
        for _ in range(10):
            try:
                await flow(context)
            except Exception as error:
                if timing != "read_end" or reliable_end is not None:
                    raise
                from camctl.contracts.values import ConsistencyError
                assert isinstance(error, ConsistencyError)
                row = owned.connection.execute(
                    "SELECT read_end_event_id,source_size,committed_bytes FROM file_copies WHERE id=1").fetchone()
                assert row is not None and row[0] is not None and row[1:] == (len(payloads["original"]),) * 2
                event, = owned.connection.execute("SELECT body_json FROM history_events WHERE id=?", (row[0],)).fetchone()
                reliable_end = json.loads(event)["evidence"]["read_end"]
                assert reliable_end["end"] == {"stopped": True, "bytes_read": len(payloads["original"]), "error": None}
                assert [call[0] for call in calls].count("download") == 1
            if timing in ("missing_cache", "failed_cache", "redownload"):
                if timing == "redownload" and initial_progress is None:
                    initial_progress = owned.connection.execute(
                        "SELECT c.id,c.source_size,c.committed_bytes,r.attempts_used,r.retry_wait_required "
                        "FROM file_copies c JOIN operation_runs r ON r.copy_id=c.id AND r.kind=3 "
                        "JOIN deliveries d ON d.id=c.delivery_id WHERE d.action_id=2 "
                        "AND r.attempts_used=1 AND r.retry_wait_required=1").fetchone()
                if fallback is None:
                    fallback = owned.connection.execute(
                        "SELECT d.id,c.id,r.max_attempts_used FROM deliveries d "
                        "JOIN file_copies c ON c.delivery_id=d.id "
                        "JOIN operation_runs r ON r.copy_id=c.id AND r.kind=3 "
                        "WHERE d.action_id=2 AND c.source_device_file_id IS NOT NULL "
                        "AND r.attempts_used=1 AND r.retry_wait_required=1").fetchone()
                monotonic[0] += 4_000_000_000
            if owned.connection.execute("SELECT COUNT(*) FROM deliveries WHERE status=5").fetchone() == (2,):
                break
        rows = owned.connection.execute(
            "SELECT d.id,d.file_name,d.status,c.source_size,c.target_sha256,o.kind "
            "FROM deliveries d JOIN file_copies c ON c.delivery_id=d.id "
            "JOIN outputs o ON o.id=d.output_id ORDER BY o.kind").fetchall()
        assert len(rows) == 2 and len({row[0] for row in rows}) == 2
        for row, role in zip(rows, ("original", "thumbnail"), strict=True):
            identity, name, status, size, digest, _kind = row
            content = (payloads[role] if timing in ("after", "redownload", "read_end") or (timing in ("missing_cache", "failed_cache") and role == "original")
                       else (_ORIGINAL if role == "original" else _PREVIEW))
            assert status == 5 and size == len(content)
            assert digest == hashlib.sha256(content).hexdigest()
            assert (Path(cfg.paths.ready) / name).read_bytes() == content
        starts = [json.loads(row[0])["evidence"]["photo_read_start"] for row in owned.connection.execute(
            "SELECT body_json FROM history_events WHERE json_extract(body_json,'$.evidence.photo_read_start') IS NOT NULL ORDER BY id")]
        if timing in ("missing_cache", "failed_cache", "redownload"):
            assert fallback is not None
            if timing == "redownload":
                assert len(read_errors) == 1
                assert initial_progress is not None and initial_progress[1:] == (len(first_original), 64, 1, 1)
            if timing == "failed_cache":
                assert len(cache_errors) == 1
            assert owned.connection.execute(
                "SELECT d.id,c.id,r.max_attempts_used,r.attempts_used FROM deliveries d "
                "JOIN file_copies c ON c.delivery_id=d.id JOIN operation_runs r ON r.copy_id=c.id AND r.kind=3 "
                "WHERE d.action_id=2").fetchone() == (*fallback, 2)
        if timing == "read_end":
            assert reliable_end is not None and len(verification_saves) >= 2
            assert all(saved == verification_saves[0] for saved in verification_saves)
        downloads = [call[1:3] for call in calls if call[0] == "download"]
        expected_downloads = (["original", "original", "thumbnail"] if timing == "redownload" else
                              ["original", "thumbnail"] if timing in ("after", "read_end") else
                              ["original"] if timing in ("missing_cache", "failed_cache") else [])
        assert downloads == [(role, "B9996039.3FR") for role in expected_downloads]
        expected_sizes = ([len(first_original), len(payloads["original"]), len(payloads["thumbnail"])]
                          if timing == "redownload" else [len(payloads[role]) for role in expected_downloads])
        assert [item["size_bytes"] for item in starts] == expected_sizes
        assert owned.connection.execute("SELECT size_bytes,sha256 FROM device_files ORDER BY id").fetchall() == [(None, None)] * 2
        assert len(capture_driver.requests) == 1
    finally:
        import sys

        original_error = sys.exception()
        errors = []
        if availability is not None:
            try:
                availability.stop_new_work()
            except BaseException as error:
                errors.append(error)
            try:
                await availability.settle()
            except BaseException as error:
                errors.append(error)
            try:
                availability.check_completed()
            except BaseException as error:
                errors.append(error)
        try:
            settled = await supervisor.drain_required()
            assert not settled.failed
            assert not executor.unfinished_files()
        except BaseException as error:
            errors.append(error)
        try:
            owned.connection.close()
        except BaseException as error:
            errors.append(error)
        if errors:
            if original_error is not None:
                errors.insert(0, original_error)
            raise BaseExceptionGroup("照片取回及资源收场失败", errors)


@pytest.mark.parametrize("missing", ["preview", "pair"])
async def test_device_transaction_rejects_forged_partial_pair(tmp_path, monkeypatch, missing):
    from contextlib import closing
    import sqlite3
    from dataclasses import replace
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.models import DbOutcome, DbOutcomeKind
    from camctl.persistence.runtime import OwnedConnection
    from camctl.persistence.repositories.transactions import commit_operation
    from ..support.output_planning import EventSequence, capture_operation

    cfg, owned, driver, runtime, _ = await _world(tmp_path, storage="device")
    commands = []
    original = CaptureRepository.finish_capture

    def interrupt(repository, request, key, current):
        commands.append(request)
        error = OSError("保存前中断")
        return DbOutcome(DbOutcomeKind.NOT_EXECUTED, error=error, handoff_error=error)

    monkeypatch.setattr(CaptureRepository, "finish_capture", interrupt)
    try:
        with pytest.raises(OSError):
            await capture_handler("camera_take_photo")(1, runtime)
        assert len(driver.requests) == 1 and len(commands) == 1
        request = commands[0]
        monkeypatch.setattr(CaptureRepository, "finish_capture", original)
        proposal = capture_operation(owned, lambda current: original(
            CaptureRepository(), request, new_operation_key(), current))
        with closing(sqlite3.connect(":memory:")) as copied:
            owned.connection.backup(copied)
            foreign_keys, = owned.connection.execute("PRAGMA foreign_keys").fetchone()
            copied.execute(f"PRAGMA foreign_keys={foreign_keys}")
            baseline = commit_operation(EventSequence(proposal.events, proposal), new_operation_key(),
                                        OwnedConnection(copied, owned.metadata))
            assert baseline.kind == "completed", baseline.error
        preview_event, = [event for event in proposal.events if any(
            row.table == "output_origins" for row in event.rows)]
        if missing == "preview":
            forged = tuple(event for event in proposal.events if event is not preview_event)
        else:
            forged = tuple(replace(event, rows=tuple(row for row in event.rows if row.table != "output_origins"))
                           if event is preview_event else event for event in proposal.events)
        before = tuple(owned.connection.iterdump())
        from camctl.history.validators import EventValidationError
        rejected = commit_operation(EventSequence(forged, proposal), new_operation_key(), owned)
        assert rejected.kind == "rolled_back", rejected.error
        assert isinstance(rejected.error, EventValidationError)
        if missing == "preview":
            assert "正式产物必须恰有原图和预览" in str(rejected.error)
        else:
            assert "预览必须唯一关联同动作正式原图" in str(rejected.error)
        assert not owned.connection.in_transaction
        assert tuple(owned.connection.iterdump()) == before
    finally:
        owned.connection.close()


@pytest.mark.parametrize("committed", [False, True])
async def test_device_sync_cache_unknown_receipt_recovers_original_request_key(tmp_path, monkeypatch, committed):
    from camctl.persistence.models import DbOutcome, DbOutcomeKind
    from camctl.persistence.repositories.host_capture import HostCaptureRepository

    cfg, owned, driver, runtime, _ = await _world(tmp_path, storage="device")
    original_save = HostCaptureRepository.save_photo_sync_cache_complete
    original_verify = HostCaptureRepository.verify_operation
    saves, verifies = [], []

    def save(repository, request, key, current):
        if request.action_id != 1 or request.role != "original":
            return original_save(repository, request, key, current)
        saves.append((request, key))
        if len(saves) == 1:
            if committed:
                receipt = original_save(repository, request, key, current)
                assert receipt.kind is DbOutcomeKind.COMPLETED, receipt.error
            return DbOutcome(DbOutcomeKind.UNKNOWN, error=OSError("同步缓存回执未知"))
        return original_save(repository, request, key, current)

    def verify(repository, request, key, current):
        receipt = original_verify(repository, request, key, current)
        if getattr(request, "role", None) == "original":
            verifies.append((request, key, receipt.kind))
        return receipt

    monkeypatch.setattr(HostCaptureRepository, "save_photo_sync_cache_complete", save)
    monkeypatch.setattr(HostCaptureRepository, "verify_operation", verify)
    try:
        await capture_handler("camera_take_photo")(1, runtime)
        assert len(driver.requests) == 1
        assert len(verifies) == 1 and verifies[0][:2] == saves[0]
        assert verifies[0][2] is (DbOutcomeKind.COMPLETED if committed else DbOutcomeKind.ROLLED_BACK)
        assert len(saves) == (1 if committed else 2) and all(item == saves[0] for item in saves)
        request, key = saves[0]
        assert owned.connection.execute("SELECT COUNT(*) FROM history_transactions WHERE operation_key=?",
            (str(key),)).fetchone() == (1,)
        assert request.occurred_at == _NOW
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (3,)
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone() == (2,)
        assert owned.connection.execute("SELECT size_bytes,sha256 FROM intermediate_files ORDER BY id").fetchall() == [
            (len(_ORIGINAL), None), (len(_PREVIEW), None)]
        await capture_handler("camera_take_photo")(1, runtime)
        assert len(driver.requests) == 1 and len(verifies) == 1
    finally:
        owned.connection.close()

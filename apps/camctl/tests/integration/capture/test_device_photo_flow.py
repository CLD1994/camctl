"""相机正式源共用单张捕获并保留双源和固定摘要。"""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from camctl.bootstrap.work_file_assembly import WorkFileRuntime
from camctl.capture.handlers import capture_handler
from camctl.outputs.work_files import WorkFileLimits
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing
from .test_host_photo_flow import _world, _ORIGINAL, _PREVIEW, _NOW

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
            (2, len(_ORIGINAL), 2, hashlib.sha256(_ORIGINAL).hexdigest()),
            (3, len(_PREVIEW), 2, hashlib.sha256(_PREVIEW).hexdigest())]
        assert files[1][2] == files[0][0]
        assert [json.loads(f[6])["variant"] for f in files] == ["original", "thumbnail"]
        assert all(json.loads(f[6])["camera_path"] == "/mnt/media/DCIM/B9996039.3FR" for f in files)
        assert all(json.loads(f[6])["camera_name"] == "B9996039.3FR" for f in files)
        outputs = owned.connection.execute("SELECT id,device_file_id,intermediate_file_id FROM outputs ORDER BY id").fetchall()
        assert [(f[1], f[2]) for f in outputs] == [(files[0][0], None), (files[1][0], None)]
        assert owned.connection.execute("SELECT output_id,original_output_id FROM output_origins").fetchall() == [(outputs[1][0], outputs[0][0])]
        paths = [Path(cfg.paths.staging) / r[0] for r in owned.connection.execute("SELECT relative_path FROM intermediate_files WHERE owner_action_id=1")]
        assert len(paths) == 2 and all(p.is_file() for p in paths)
        maintenance = WorkFileRuntime(staging=Path(cfg.paths.staging), limits=WorkFileLimits(32, 10))
        maintenance.initialize(owned)
        context = SimpleNamespace(open_connection=lambda: open_existing(Path(cfg.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig()), clock=SimpleNamespace(utc_micros=lambda: _NOW))
        await maintenance.flow(context)
        await maintenance.settle()
        assert all(not p.exists() for p in paths)
        assert owned.connection.execute("SELECT retention_state,cleanup_state FROM intermediate_files WHERE owner_action_id=1").fetchall() == [(2,4),(2,4)]
        await capture_handler("camera_take_photo")(1, runtime)
        assert len(driver.requests) == 1
    finally:
        owned.connection.close()

@pytest.mark.parametrize("binding",["match","missing","changed"])
async def test_device_cleanup_without_capability_fails_each_item_without_attempt(tmp_path,binding):
    from camctl.acceptance.input import ParsedInput
    from camctl.acceptance.service import CommandMode
    from camctl.bootstrap.cleanup_assembly import session_cleanup_assembly
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
        accepted = AcceptanceRepository().process_input(ProcessInput(ParsedInput("cleanup.json", {
            "request_id":"2", "created_at":_input_time(), "name":"清理相机源",
            "actions":[{"name":"清理", "type":"delete_action_outputs", "scheduled_at":_input_time(),
                        "params":{"output_ids":["1","2"]}}]}),
            build_catalog(cfg, DriverDefinitions({DRIVER_ID:driver_definition()})), CommandMode.RUN, _NOW), new_operation_key(), owned)
        assert accepted.kind is DbOutcomeKind.COMPLETED, accepted.error
        from dataclasses import replace
        devices = {} if binding == "missing" else cfg.devices
        entry = _entry(driver)
        if binding == "changed":
            devices = {"cam-1": {"kind":"camera", "driver":"other"}}
            entry = replace(entry, driver_id="other")
        cleanup = session_cleanup_assembly(devices=devices, drivers=DriverRegistry((entry,)),
            max_delete_attempts=2,max_query_attempts=2,staging=Path(cfg.paths.staging),occurred_at=lambda:_NOW)(owned)
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
        runtime = session_capture_assembly(devices=changed.devices,drivers=DriverRegistry((_entry(driver),)),
            staging=Path(cfg.paths.staging),wait_config=lambda _:None,wall_us=lambda:_NOW)(owned,"cam-1")
        await capture_handler("camera_take_photo")(1,runtime)
        assert len(driver.requests) == 1
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone() == (3,)
        rows = owned.connection.execute("SELECT device_file_id,intermediate_file_id FROM outputs").fetchall()
        assert len(rows) == 2
        assert all((row[0] is not None) == (first == "device") for row in rows)
    finally:
        owned.connection.close()

@pytest.mark.parametrize("interruption", ["archive","checksum","finish"])
@pytest.mark.parametrize("committed", [False,True])
async def test_device_registration_resumes_original_request_without_camera(tmp_path, monkeypatch, interruption, committed):
    from camctl.capture import host_photo
    from camctl.contracts.values import ConsistencyError
    from camctl.devices.bindings import BindingResult,BindingStatus
    from camctl.persistence.models import DbOutcome,DbOutcomeKind
    cfg,owned,driver,runtime,_ = await _world(tmp_path,storage="device")
    saved_calls=[]
    try:
        if interruption == "archive":
            original=host_photo.archive_capture_file
            async def interrupted(*args):
                await original(*args)
                raise OSError("归档中断")
            monkeypatch.setattr(host_photo,"archive_capture_file",interrupted)
        else:
            method="save_file_checksum" if interruption == "checksum" else "finish_capture"
            original=getattr(runtime.capture,method)
            def interrupted(request,key,owned):
                saved_calls.append((request,key))
                if committed:
                    result=original(request,key,owned)
                    assert result.kind is DbOutcomeKind.COMPLETED,result.error
                return DbOutcome(DbOutcomeKind.UNKNOWN,error=OSError("回执未知"))
            monkeypatch.setattr(runtime.capture,method,interrupted)
        with pytest.raises((OSError,ConsistencyError)):
            await capture_handler("camera_take_photo")(1,runtime)
        assert len(driver.requests)==1
        paths=[Path(cfg.paths.staging)/r[0] for r in owned.connection.execute("SELECT relative_path FROM intermediate_files")]
        assert any(p.exists() for p in paths)
        runtime.binding_check=lambda saved:BindingResult(BindingStatus.DEVICE_MISSING,saved,None)
        if interruption == "archive":
            monkeypatch.setattr(host_photo,"archive_capture_file",original)
        else:
            def resumed(request,key,owned):
                saved_calls.append((request,key))
                return original(request,key,owned)
            monkeypatch.setattr(runtime.capture,method,resumed)
        await capture_handler("camera_take_photo")(1,runtime)
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone()==(3,)
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone()==(2,)
        assert len(driver.requests)==1
        if saved_calls:
            first=saved_calls[0]
            same=[call for call in saved_calls if call[0].__class__ is first[0].__class__ and getattr(call[0],"file_id",None)==getattr(first[0],"file_id",None)]
            assert all(call==first for call in same)
    finally:
        owned.connection.close()

async def test_cancel_after_reliable_photo_retains_device_pair(tmp_path, monkeypatch):
    from camctl.capture import host_photo
    from .test_host_photo_flow import _apply_cancel
    cfg,owned,driver,runtime,_=await _world(tmp_path,storage="device",cancel_action=True)
    original=host_photo.archive_capture_file
    canceled=False
    async def cancel_after_archive(*args):
        nonlocal canceled
        await original(*args)
        if not canceled:
            canceled=True
            _apply_cancel(owned,2,1)
    monkeypatch.setattr(host_photo,"archive_capture_file",cancel_after_archive)
    try:
        await capture_handler("camera_take_photo")(1,runtime)
        assert len(driver.requests)==1
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone()==(6,)
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
    maintenance=WorkFileRuntime(staging=Path(cfg.paths.staging),limits=WorkFileLimits(32,10))
    maintenance.initialize(owned)
    context=SimpleNamespace(open_connection=lambda:open_existing(Path(cfg.paths.state_db),DbOpenMode.EXISTING_RW,DbConfig()),clock=SimpleNamespace(utc_micros=lambda:_NOW))
    await maintenance.flow(context)
    await maintenance.settle()


async def test_device_terminal_replay_after_cleanup_preserves_report_and_history(tmp_path, monkeypatch):
    from camctl.contracts.schemas import validate_document
    from camctl.contracts.values import new_operation_key
    from camctl.persistence.repositories.history import HistoryRepository
    from camctl.persistence.models import DbOutcomeKind
    from camctl.reporting.generation import GenerationSpec,generate_report_file
    from camctl.reporting.policy import ReportingRepository
    cfg,owned,driver,runtime,_=await _world(tmp_path,storage="device")
    saved=[]
    original=runtime.capture.finish_capture
    def remember(request,key,current):
        saved.append((request,key))
        return original(request,key,current)
    monkeypatch.setattr(runtime.capture,"finish_capture",remember)
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
            replay=original(request,replay_key,owned)
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
        for kind,content in (("original",_ORIGINAL),("preview",_PREVIEW)):
            assert pair[kind]["checksum"]=={"status":"available","sha256":hashlib.sha256(content).hexdigest()}
            assert pair[kind]["size"]==len(content)
    finally:
        owned.connection.close()


async def test_failed_host_copy_cleanup_retries_on_next_maintenance(tmp_path,monkeypatch):
    from camctl.outputs import work_files
    cfg,owned,driver,runtime,_=await _world(tmp_path,storage="device")
    try:
        await capture_handler("camera_take_photo")(1,runtime)
        remove=work_files._remove_work_file
        def denied(path):
            if "capture-originals" in path.parts:
                raise PermissionError("主机副本删除被拒绝")
            return remove(path)
        monkeypatch.setattr(work_files,"_remove_work_file",denied)
        await _maintain(cfg,owned)
        rows=owned.connection.execute("SELECT cleanup_state,last_error_json,relative_path FROM intermediate_files ORDER BY id").fetchall()
        assert rows[0][0]==5 and rows[0][1] is not None
        assert (Path(cfg.paths.staging)/rows[0][2]).is_file()
        assert rows[1][0]==4
        monkeypatch.setattr(work_files,"_remove_work_file",remove)
        await _maintain(cfg,owned)
        assert owned.connection.execute("SELECT cleanup_state FROM intermediate_files ORDER BY id").fetchall()==[(4,),(4,)]
        assert not (Path(cfg.paths.staging)/rows[0][2]).exists()
        assert owned.connection.execute("SELECT availability FROM outputs ORDER BY id").fetchall()==[(1,),(1,)]
    finally:
        owned.connection.close()


@pytest.mark.parametrize("defect",["empty","single","host","sha","pair","role","owner","complete","support"])
async def test_device_success_requires_complete_matching_device_pair(tmp_path,monkeypatch,defect):
    from dataclasses import replace
    from camctl.contracts.values import new_operation_key
    from camctl.outputs.catalog import FileReference
    from camctl.persistence.models import DbOutcomeKind
    cfg,owned,driver,runtime,_=await _world(tmp_path,storage="device")
    request=[]
    def interrupt(value):
        request.append(value)
        raise OSError("登记前中断")
    monkeypatch.setattr(runtime,"save_capture_completion",interrupt)
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
        else:
            column,value={"role":("role",1),"owner":("source_action_id",None),"complete":("completion_state",1),"support":("checksum_support",1)}[defect]
            # 故障注入制造不一致投影；仓储不得把损坏事实登记为成功。
            owned.connection.execute("PRAGMA ignore_check_constraints=ON")
            owned.connection.execute(f"UPDATE device_files SET {column}=? WHERE id=2",(value,))
            owned.connection.commit()
        result=runtime.capture.finish_capture(command,new_operation_key(),owned)
        assert result.kind is DbOutcomeKind.ROLLED_BACK,result
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone()==(2,)
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone()==(0,)
        assert owned.connection.execute("SELECT retention_state FROM intermediate_files").fetchall()==[(1,),(1,)]
    finally:
        owned.connection.close()

@pytest.mark.parametrize("after_checksum",[False,True])
async def test_restart_without_configured_camera_finishes_original_registration(tmp_path,monkeypatch,after_checksum):
    from camctl.bootstrap.capture_assembly import session_capture_assembly
    from camctl.capture import host_photo
    from camctl.devices.drivers.registry import DriverRegistry
    from camctl.persistence.models import DbOutcomeKind
    cfg,owned,driver,runtime,_=await _world(tmp_path,storage="device")
    if after_checksum:
        original=runtime.capture.save_file_checksum
        def interrupted(request,key,current):
            receipt=original(request,key,current)
            assert receipt.kind is DbOutcomeKind.COMPLETED,receipt.error
            raise OSError("摘要保存后会话中断")
        monkeypatch.setattr(runtime.capture,"save_file_checksum",interrupted)
    else:
        original=host_photo.archive_capture_file
        async def interrupted(*args):
            await original(*args)
            raise OSError("归档后会话中断")
        monkeypatch.setattr(host_photo,"archive_capture_file",interrupted)
    try:
        with pytest.raises(OSError):
            await capture_handler("camera_take_photo")(1,runtime)
        if not after_checksum:
            monkeypatch.setattr(host_photo,"archive_capture_file",original)
        owned.connection.close()
        owned=open_existing(Path(cfg.paths.state_db),DbOpenMode.EXISTING_RW,DbConfig())
        resumed=session_capture_assembly(devices={},drivers=DriverRegistry(()),
            staging=Path(cfg.paths.staging),wait_config=lambda _:None,wall_us=lambda:_NOW)(owned,"cam-1")
        await capture_handler("camera_take_photo")(1,resumed)
        assert resumed.driver is None
        assert owned.connection.execute("SELECT status FROM actions WHERE id=1").fetchone()==(3,)
        assert owned.connection.execute("SELECT COUNT(*) FROM device_files").fetchone()==(2,)
        assert owned.connection.execute("SELECT COUNT(*) FROM outputs").fetchone()==(2,)
        assert len(driver.requests)==1
        await _maintain(cfg,owned)
        assert owned.connection.execute("SELECT cleanup_state FROM intermediate_files").fetchall()==[(4,),(4,)]
    finally:
        owned.connection.close()

async def test_cleanup_continues_from_unsupported_device_to_local_host_sources(tmp_path):
    from camctl.acceptance.input import ParsedInput
    from camctl.acceptance.service import CommandMode
    from camctl.bootstrap.cleanup_assembly import session_cleanup_assembly
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
        accepted=AcceptanceRepository().process_input(ProcessInput(ParsedInput("mixed-cleanup.json",{
            "request_id":"3","created_at":_input_time(),"name":"分别清理",
            "actions":[{"name":"mixed","type":"delete_action_outputs","scheduled_at":_input_time(),
                        "params":{"output_ids":["1","2","3","4"]}}]}),
            build_catalog(cfg,DriverDefinitions({DRIVER_ID:driver_definition()})),CommandMode.RUN,_NOW),new_operation_key(),owned)
        assert accepted.kind is DbOutcomeKind.COMPLETED,accepted.error
        work_files = WorkFileRuntime(
            staging=Path(cfg.paths.staging), limits=WorkFileLimits(32, 10))
        cleanup=session_cleanup_assembly(devices=cfg.devices,drivers=DriverRegistry((_entry(driver),)),
            max_delete_attempts=2,max_query_attempts=2,staging=Path(cfg.paths.staging),
            file_executor=work_files.executor, occurred_at=lambda:_NOW)(owned)
        await advance_cleanup(cleanup)
        assert owned.connection.execute("SELECT status FROM cleanup_items ORDER BY id").fetchall()==[(5,),(5,),(4,),(4,)]
        assert all(not (Path(cfg.paths.staging)/r[0]).exists() for r in before)
        assert owned.connection.execute("SELECT COUNT(*) FROM operation_runs WHERE cleanup_item_id IN (1,2)").fetchone()==(0,)
        assert owned.connection.execute("SELECT presence_state FROM device_files").fetchall()==[(2,),(2,)]
    finally:
        owned.connection.close()


@pytest.mark.parametrize("entry", ["action_event", "output_event"])
async def test_event_guards_reject_partial_device_pair(tmp_path, entry):
    """直接事件入口也不能绕过 FinishCapture 的双源约束。"""
    from camctl.contracts.enums import enum_for
    from camctl.history.validators import EventValidationError
    from camctl.persistence.repositories.capture import _action_finish_guard, _output_guard
    from camctl.persistence.transaction import row_facts

    cfg, owned, driver, runtime, _ = await _world(tmp_path, storage="device")
    try:
        await capture_handler("camera_take_photo")(1, runtime)
        state = {
            table: {identity: row_facts(owned.connection, table, identity)
                    for (identity,) in owned.connection.execute(f"SELECT id FROM {table}")}
            for table in ("actions", "device_files", "intermediate_files", "outputs", "output_origins")
        }
        original = next(row for row in state["outputs"].values()
                        if row["kind"] == enum_for("outputs.kind").ORIGINAL)

        class Context:
            state_rows = state
            transaction_rows = {"outputs": {original["id"]: original}, "output_origins": {}}

            def complete_rows(self, table, column, identity):
                return {key: row for key, row in state[table].items() if row[column] == identity}

        if entry == "action_event":
            guard = _action_finish_guard
            row = SimpleNamespace(
                table="actions", row_id=1,
                before=SimpleNamespace(exists=True, values={"cancel_requested": 0}),
                after=SimpleNamespace(values={"status": enum_for("actions.status").SUCCEEDED}),
            )
        else:
            guard = _output_guard
            row = SimpleNamespace(
                table="outputs", row_id=original["id"],
                before=SimpleNamespace(exists=False), after=SimpleNamespace(values=original),
            )
        with pytest.raises(EventValidationError, match="原图.*预览|两份"):
            guard(SimpleNamespace(rows=(row,), event_type=None), Context())
    finally:
        owned.connection.close()

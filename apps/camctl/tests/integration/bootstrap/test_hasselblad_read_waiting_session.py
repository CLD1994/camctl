"""默认 run 在准备前提未满足时退出，下次真实 run 沿原责任继续。"""

import asyncio
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path

import pytest

from camctl.acceptance.service import CommandMode
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.bootstrap.obtain_assembly import obtain_flow, session_obtain_assembly
from camctl.capture.recovery import RecoveryBoundary
from camctl.contracts.values import new_operation_key
from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
from camctl.operations.attempts import AttemptConfig, AttemptIntent, AttemptTarget, OperationKind
from camctl.outputs.read_attempts import running_read_ticket
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository

from ..devices.test_hasselblad_download import DownloadTransport, source
from .test_output_binding_changes import _CONTENT, _NOW, _accept, _registry, _save_photos, environment

pytestmark = pytest.mark.asyncio


async def _pending_last_read(environment):
    cfg, owned, context, driver = environment
    await _save_photos(cfg, owned, context, driver)
    source_action = owned.connection.execute("SELECT id FROM actions WHERE device_id='cam-a'").fetchone()[0]
    _accept(owned, "2", [{"name": "取回", "type": "obtain_action_outputs",
        "scheduled_at": "2026-01-15 09:00:00",
        "params": {"source": {"action_instance_id": str(source_action)}}}])
    devices = {key: dict(value, copy={"max_read_attempts": 2, "retry_interval_s": 0})
               for key, value in cfg.devices.items()}
    driver.fail_a = True
    factory = session_obtain_assembly(devices=devices, drivers=_registry(driver),
        staging=Path(cfg.paths.staging), ready=Path(cfg.paths.ready), processing=Path(cfg.paths.processing),
        segment_size=4, occurred_at=lambda: _NOW, monotonic_ns=lambda: 0)
    await obtain_flow(factory)(context)
    copy_id, source_id, action_id, first_run, first_attempt = owned.connection.execute(
        "SELECT c.id,c.source_device_file_id,r.action_id,r.id,t.attempt_no FROM file_copies c"
        " JOIN operation_runs r ON r.copy_id=c.id JOIN operation_attempts t ON t.run_id=r.id").fetchone()
    assert owned.connection.execute("SELECT committed_bytes FROM file_copies").fetchone() == (4,)
    request = AttemptIntent("read", action_id, OperationKind.READ_FILE, AttemptTarget(copy_id=copy_id), None,
        AttemptConfig(2, Decimal("10"), Decimal("0")), _NOW + 1, copy_round=1)
    begun = OperationRepository().begin_attempt(request, new_operation_key(), owned)
    assert begun.kind is DbOutcomeKind.COMPLETED, begun.error
    ticket, intent = running_read_ticket(owned, copy_id)
    assert ticket.attempt_id == 2
    fixed = source(content=_CONTENT)
    identity = json.loads(fixed.file_id)
    identity[0] = "cam-a"
    owned.connection.execute("UPDATE device_files SET identity_key=?,locator_json=? WHERE id=?",
        (json.dumps(identity, separators=(",", ":")), json.dumps(dict(fixed.locator)), source_id))
    owned.connection.execute("UPDATE actions SET driver_id=? WHERE device_id='cam-a'", (DRIVER_ID,))
    owned.connection.commit()
    devices = {"cam-a": {"kind": "camera", "driver": DRIVER_ID,
        "hasselblad": {"executable": "/tool/hb_cli", "serial": "SERIAL", "product_id": 10},
        "copy": {"max_read_attempts": 2, "retry_interval_s": 0}}}
    cfg = replace(cfg, devices=devices, cleanup=replace(cfg.cleanup, work_file_limit_per_run=1))
    staging = Path(cfg.paths.staging)
    earlier = staging / "read-downloads" / f"{first_run}-{first_attempt}"
    work = staging / "read-downloads" / f"{ticket.run_id}-{ticket.attempt_id}"
    for directory in (earlier, work):
        directory.mkdir(parents=True)
        (directory / "old").write_bytes(b"original responsibility")
    return cfg, owned, ticket, intent, earlier, work


async def _default_run(cfg):
    deps = build_runtime(CommandMode.RUN, cfg, recovery_boundary=RecoveryBoundary.HOST_LOCAL_SETTLED)
    try:
        # 超时只证伪会话没有自然退出；不能用测试取消来满足非成功退出断言。
        result = await asyncio.wait_for(execute_command(deps, None, poll_interval_s=0.001), timeout=5)
        assert all(owner.required_settlements() == 0 for owner in deps.background_flows)
        assert deps.work_files.required_settlements() == 0
        assert not deps.work_files.executor.unfinished_files()
        return result
    finally:
        close_runtime(deps)


@pytest.mark.parametrize("waiting_for", ["outside_budget", "prepare_denied"])
async def test_default_run_exits_on_local_precondition_then_resumes_original_last_ticket(
        environment, monkeypatch, waiting_for):
    from camctl.bootstrap import download_cleanup
    from camctl.devices.drivers.builtin import register_builtin_drivers
    from camctl.devices.drivers import runtime
    from camctl.host_files.downloads import DownloadWorkspace

    cfg, owned, ticket, intent, earlier, work = await _pending_last_read(environment)
    transport = DownloadTransport(payload=_CONTENT)
    register_builtin_drivers(cfg)
    driver = runtime.current_registry().entry(DRIVER_ID).driver
    monkeypatch.setattr(driver, "transport", transport)
    examined = []
    real_recover = download_cleanup.recover_download
    async def record(staging, original):
        examined.append(original)
        return await real_recover(staging, original)
    monkeypatch.setattr(download_cleanup, "recover_download", record)
    original_prepare = DownloadWorkspace._prepare
    if waiting_for == "prepare_denied":
        def denied(_workspace):
            raise PermissionError(f"workspace preparation denied: {work}")
        monkeypatch.setattr(DownloadWorkspace, "_prepare", denied)

    first = await _default_run(cfg)

    assert not first.succeeded, first
    assert str(work) in str(first.details)
    assert ("denied" if waiting_for == "prepare_denied" else "FileExistsError") in str(first.details)
    assert len(examined) == 1 and examined[0].attempt_id == 1
    assert not earlier.exists() and (work / "old").read_bytes() == b"original responsibility"
    assert not transport.calls
    cursor = Path(cfg.paths.staging) / ".read-download-cleanup-cursor"
    assert int(cursor.read_text()) < intent
    assert running_read_ticket(owned, int(ticket.target_id))[0] == ticket
    assert owned.connection.execute("SELECT attempts_used,max_attempts_used FROM operation_runs WHERE id=?",
        (ticket.run_id,)).fetchone() == (2, 2)
    assert owned.connection.execute("SELECT status,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=2",
        (ticket.run_id,)).fetchone() == (1, None)
    assert owned.connection.execute("SELECT slot_device_id FROM file_copies").fetchone() == ("cam-a",)
    print(f"{waiting_for}: first run succeeded={first.succeeded}, examined=1, download_calls=0, "
          f"ticket={ticket.run_id}/{ticket.attempt_id}, attempts_used=2, state=RUNNING; {dict(first.details)!r}")

    monkeypatch.setattr(DownloadWorkspace, "_prepare", original_prepare)
    second = await _default_run(cfg)

    assert second.succeeded, second
    assert len(examined) == 2 and examined[-1] == ticket
    assert int(cursor.read_text()) == intent
    assert not work.exists()
    assert [spec.argv[3] for spec in transport.calls] == ["list", "info", "download"]
    assert owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone() == (2,)
    saved_status, saved_result = owned.connection.execute(
        "SELECT status,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=2", (ticket.run_id,)).fetchone()
    assert saved_status == 2
    assert json.loads(saved_result)["settlement"]["evidence"]["type"] == "read_returned"
    assert owned.connection.execute("SELECT slot_device_id FROM file_copies").fetchone() == (None,)
    assert owned.connection.execute("SELECT status FROM deliveries").fetchone() == (5,)
    assert [path.read_bytes() for path in Path(cfg.paths.ready).glob("*.jpg")] == [_CONTENT]
    print(f"{waiting_for}: second run succeeded={second.succeeded}, examined=1, download_calls=1, "
          f"ticket={ticket.run_id}/{ticket.attempt_id}, attempts_used=2, read_returned, slot=None, published")

"""真实下载准备经过正式取回和媒体链，确认收场后才保存 READ。"""

import asyncio
import json
import os
import threading
import time
from pathlib import Path

import pytest

from camctl.devices.drivers.hasselblad.driver import EVIDENCE, HasselbladDriver
from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
from camctl.host_files.models import BoundDirectories
from camctl.devices.read_session import ReadPreparationError, ReadPreparationSettlement
from camctl.host_files.downloads import DownloadWorkspace, download_path, recover_download
from camctl.operations.process import RawToolOutcome, ToolStartError
from camctl.contracts.values import ConsistencyError
from camctl.outputs.obtain_flow import advance_obtain
from camctl.outputs.read_attempts import running_read_ticket

from ..support.outputs import output_drivers, read_test_runtime
from ..bootstrap.test_download_cleanup import photo_read_request
from ..capture.test_host_photo_flow import _config
from ..devices import test_hasselblad_download
from ..devices.test_hasselblad_download import DownloadTransport, RAW

def _real_download_driver(tmp_path, monkeypatch):
    config = _config(tmp_path, storage="device")
    staging = Path(config.paths.staging)
    monkeypatch.setattr(test_hasselblad_download, "NAME", "B9996039.3FR")
    transport = DownloadTransport(serial="SERIAL-1")
    driver = HasselbladDriver(config.devices, BoundDirectories(staging),
                              monotonic_ns=time.monotonic_ns, transport=transport)
    return config, driver, transport, staging


def _availability(config, driver, occurred_at):
    from camctl.bootstrap.availability_assembly import AvailabilitySession
    return AvailabilitySession(config.devices, output_drivers(driver, EVIDENCE, driver_id=DRIVER_ID, read=True),
        monotonic_ns=time.monotonic_ns, occurred_at=occurred_at,
        recovery_boundary=None, recovery_max_event_id=None)


async def _advance_until_read(runtime, ready):
    while not ready.is_set():
        await advance_obtain(runtime)
        if not ready.is_set():
            await asyncio.sleep(0)


async def _wait_read_ready(task, ready):
    waiter = asyncio.create_task(ready.wait())
    try:
        done, _ = await asyncio.wait((task, waiter), timeout=2, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task
            raise AssertionError("业务提前结束，尚未提交读取请求")
        if waiter not in done:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise TimeoutError("读取请求未在原准备窗口内建立")
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


def _error_text(error):
    seen = set()
    def messages(current):
        if current is None or id(current) in seen:
            return
        seen.add(id(current))
        yield f"{type(current).__name__}: {current}"
        yield from getattr(current, "__notes__", ())
        for member in getattr(current, "exceptions", ()):
            yield from messages(member)
        yield from messages(current.__cause__)
        yield from messages(current.__context__)
    return "\n".join(messages(error))


@pytest.mark.asyncio
@pytest.mark.parametrize("source_kind", ["device"])
@pytest.mark.parametrize("failure", ["not_started", "exited", "exited_cleanup", "no_exit", "transport_error", "bare_cancel", "verify_close", "dispose_close"])
async def test_preparation_settlement_survives_real_consumer_and_result_save(photo_read_request, tmp_path, source_kind, failure, monkeypatch):
    assert source_kind == "device"
    owned, command = photo_read_request
    config, driver, transport, staging = _real_download_driver(tmp_path, monkeypatch)
    original_run = transport.run
    async def run(spec, stop):
        if spec.argv[3] != "download":
            return await original_run(spec, stop)
        if failure == "not_started":
            transport.calls.append(spec)
            raise ToolStartError("exec not found")
        if failure in ("exited", "exited_cleanup"):
            transport.failure = "failed"
            return await original_run(spec, stop)
        await original_run(spec, stop)
        if failure == "no_exit":
            return RawToolOutcome(None, b"", None, None)
        if failure == "bare_cancel":
            raise asyncio.CancelledError("transport cancelled without exit")
        raise OSError("transport lost exit")
    transport.run = original_run if failure in ("verify_close", "dispose_close") else run
    # 在公开工作区构造边界记录本测试拥有的真实资源，关闭故障由 os.close 注入。
    workspaces = []
    class Workspace(DownloadWorkspace):
        def __init__(self, *args):
            super().__init__(*args)
            workspaces.append(self)
    from camctl.devices.drivers.hasselblad import driver as driver_module
    monkeypatch.setattr(driver_module, "DownloadWorkspace", Workspace)
    ready, start = asyncio.Event(), asyncio.Event()
    tickets = []
    original_open = driver.open_read
    async def open_read(source, offset, ticket, *, idle_timeout_s):
        tickets.append(ticket)
        ready.set()
        await start.wait()
        return await original_open(source, offset, ticket, idle_timeout_s=idle_timeout_s)
    driver.open_read = open_read
    entered, release = threading.Event(), threading.Event()
    real_close, real_stat = os.close, os.fstat
    close_calls = []
    work = None
    def fail_close(fd):
        if workspaces and workspaces[-1].fd == fd:
            close_calls.append(fd)
            raise OSError("file close not confirmed")
        return real_close(fd)
    def fault_stat(fd):
        if workspaces and workspaces[-1].fd == fd and not entered.is_set():
            entered.set()
            if failure == "verify_close":
                raise ValueError("source file verification rejected")
            assert release.wait(3)
        return real_stat(fd)
    from camctl.host_files import downloads
    if failure == "exited_cleanup":
        real_remove = downloads.shutil.rmtree
        def fail_remove(path):
            if path == work:
                raise PermissionError("download cleanup denied")
            return real_remove(path)
        monkeypatch.setattr(downloads.shutil, "rmtree", fail_remove)
    if failure in ("verify_close", "dispose_close"):
        monkeypatch.setattr(os, "close", fail_close)
    if failure in ("verify_close", "dispose_close"):
        monkeypatch.setattr(os, "fstat", fault_stat)
    unsettled = failure in ("no_exit", "transport_error", "bare_cancel", "verify_close", "dispose_close")
    try:
        async with read_test_runtime(owned, driver, EVIDENCE, staging=staging,
                max_read_attempts=5,
                occurred_at=lambda: command.occurred_at + 20, driver_id=DRIVER_ID,
                availability=_availability(config, driver, lambda: command.occurred_at + 20)) as runtime:
            task = asyncio.create_task(_advance_until_read(runtime, ready))
            await _wait_read_ready(task, ready)
            ticket = tickets[0]
            work = download_path(staging, ticket)
            start.set()
            if failure == "dispose_close":
                async with asyncio.timeout(2):
                    while not entered.is_set():
                        await asyncio.sleep(0.001)
                task.cancel("cancel_verify")
                release.set()
            if unsettled:
                with pytest.raises(BaseException) as observed:
                    await task
                if failure in ("verify_close", "dispose_close"):
                    assert "file close not confirmed" in _error_text(observed.value)
                    assert ("source file verification rejected" if failure == "verify_close" else "cancel_verify") in _error_text(observed.value)
                status, result = owned.connection.execute("SELECT status,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
                    (ticket.run_id, ticket.attempt_id)).fetchone()
                if failure in ("verify_close", "dispose_close"):
                    assert len(close_calls) == 1
                    assert os.fstat(close_calls[0]).st_size == len(RAW)
                assert status == 1 and result is None
                assert running_read_ticket(owned, int(ticket.target_id))[0] == ticket
                assert work.exists() and not await recover_download(staging, ticket)
                assert len(transport.calls) == 3
                # 正式入口再进入仍保留原运行尝试，不增加设备调用或读取预算。
                try:
                    await advance_obtain(runtime)
                except (ReadPreparationError, ConsistencyError, asyncio.CancelledError) as retained:
                    assert any(value in _error_text(retained) for value in
                        ("preparation_unconfirmed", "恢复", "原", "cancel_verify"))
                assert tickets == [ticket]
                assert owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE id=?", (ticket.run_id,)).fetchone() == (1,)
                assert owned.connection.execute("SELECT slot_device_id FROM file_copies WHERE id=?", (int(ticket.target_id),)).fetchone()[0] is not None
            else:
                await task
                status, result = owned.connection.execute("SELECT status,result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
                    (ticket.run_id, ticket.attempt_id)).fetchone()
                assert status == 3
                assert json.loads(result)["settlement"]["evidence"]["type"] == "read_returned"
                stored = json.loads(owned.connection.execute("SELECT error_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
                    (ticket.run_id, ticket.attempt_id)).fetchone()[0])
                assert stored["code"] == "device_error"
                assert ("exec not found" if failure == "not_started" else "17") in stored["details"]["reason"]
                if failure == "exited_cleanup":
                    assert "download cleanup denied" in stored["details"]["reason"] and work.exists()
                else:
                    assert not work.exists()
                assert RAW.decode("latin1") not in stored["details"]["reason"]
                await advance_obtain(runtime)
                assert tickets[-1].attempt_id == ticket.attempt_id + 1
                assert len(transport.calls) == 6
    finally:
        release.set()
        # 注入的 close 明确未执行真实关闭，测试在原描述符上完成实际收场。
        for workspace in workspaces:
            if workspace.fd is not None:
                real_close(workspace.fd)
                workspace.fd = None
            workspace.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("source_kind", ["device"])
@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("waiting_for", ["existing_directory", "prepare_denied"])
async def test_maintenance_waiting_keeps_original_read_even_when_user_cancelled(photo_read_request, tmp_path, source_kind, cancelled, waiting_for, monkeypatch):
    assert source_kind == "device"
    owned, command = photo_read_request
    config, driver, transport, staging = _real_download_driver(tmp_path, monkeypatch)
    ready, start = asyncio.Event(), asyncio.Event()
    tickets, failures = [], []
    original_open = driver.open_read
    async def open_read(source, offset, ticket, *, idle_timeout_s):
        tickets.append(ticket)
        ready.set()
        await start.wait()
        try:
            return await original_open(source, offset, ticket, idle_timeout_s=idle_timeout_s)
        except ReadPreparationError as error:
            failures.append(error)
            if cancelled:
                owned.connection.execute("UPDATE actions SET cancel_requested=1 WHERE id=?", (command.action_id,))
                owned.connection.commit()
            raise
    driver.open_read = open_read
    async with read_test_runtime(owned, driver, EVIDENCE, staging=staging,
            max_read_attempts=1,
            occurred_at=lambda: command.occurred_at + 20, driver_id=DRIVER_ID,
            availability=_availability(config, driver, lambda: command.occurred_at + 20)) as runtime:
        task = asyncio.create_task(_advance_until_read(runtime, ready))
        await _wait_read_ready(task, ready)
        ticket = tickets[0]
        work = download_path(staging, ticket)
        work.mkdir(parents=True)
        (work / "old").write_bytes(b"old responsibility")
        if waiting_for == "prepare_denied":
            real_mkdir = os.mkdir
            def denied(path, *args, **kwargs):
                if Path(path) == work:
                    raise PermissionError("download workspace preparation denied")
                return real_mkdir(path, *args, **kwargs)
            monkeypatch.setattr(os, "mkdir", denied)
        start.set()
        with pytest.raises(ReadPreparationError) as delivered:
            await task
        assert delivered.value is failures[0]
        assert delivered.value.settlement is ReadPreparationSettlement.WAITING
        assert ("preparation denied" if waiting_for == "prepare_denied" else str(work)) in str(delivered.value)
        assert running_read_ticket(owned, int(ticket.target_id))[0] == ticket
        assert not transport.calls
        assert (work / "old").read_bytes() == b"old responsibility"
        assert owned.connection.execute("SELECT result_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
            (ticket.run_id, ticket.attempt_id)).fetchone() == (None,)


def test_ordinary_business_waiting_is_not_a_preparation_error():
    from camctl.capture.input_copy import InputPhase, InputStep
    from camctl.outputs.read_attempts import require_preparation_settled
    assert require_preparation_settled(InputStep(InputPhase.WAITING)) is True

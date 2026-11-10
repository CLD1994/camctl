"""正式读取消费者保留原异常、票据与保存前诊断责任。"""

import json
from dataclasses import replace

import pytest

from support.repository_ports import repository_port

from camctl.capture.files import file_identity_key
from camctl.capture.media_flow import run_recording_media
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.devices.drivers.hasselblad.driver import CameraReadError, EVIDENCE
from camctl.devices.read_session import ReadSession, ReadEnd
from camctl.host_files.io import LocalSourceReader, PositionedWriter
from camctl.host_files.tasks import FileTaskExecutor
from camctl.session.supervision import Supervisor
from camctl.logging_runtime.diagnostics import diagnostic_scope, error_diagnostics_context, flush_diagnostics
from support.logging import recording_log_channel
from camctl.outputs import obtain_flow
from camctl.persistence.models import DbOutcome, DbOutcomeKind
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.outputs import OutputsRepository

from ..support.outputs import read_test_runtime, prepare_cancel_target
from ..bootstrap.test_output_binding_changes import Catalog
from .test_grant_reuse import local_read, read_targets
from .test_read_associations import _command
from .test_source_digest_failure_settlement import _ReadDriver, _CONTENT



@pytest.fixture
def consumer():
    return "device"


def prepare_diagnostic_read_case(request, consumer, *, known_digest=False):
    if consumer == "host":
        owned, command = request.getfixturevalue("local_read")
    else:
        owned = request.getfixturevalue("read_targets")
        command = _command(internal=consumer == "internal")
        # 录像来源须保留与动作绑定一致的固定身份，供正式读取与清理资格识别。
        owned.connection.execute(
            "UPDATE device_files SET identity_key=? WHERE id=501",
            (file_identity_key("cam-1", "camctl-adb", "file-0501"),),
        )
    if consumer == "internal":
        owned.connection.execute("DELETE FROM obtain_items")
        owned.connection.execute("DELETE FROM outputs")
    else:
        owned.connection.execute("UPDATE actions SET status=3 WHERE id=11")
    owned.connection.execute("UPDATE actions SET scheduled_at=? WHERE id=32", (command.occurred_at + 60_000_000,))
    owned.connection.execute("UPDATE recording_processing SET check_basis_json=? WHERE id=5", ('{"target_duration_ms":1000}',))
    if known_digest:
        if consumer == "host":
            owned.connection.execute("UPDATE intermediate_files SET size_bytes=?,sha256=? WHERE id=801", (len(_CONTENT), "a" * 64))
        else:
            owned.connection.execute("UPDATE device_files SET size_bytes=?,checksum_support=2,sha256=? WHERE id=501", (len(_CONTENT), "a" * 64))
    owned.connection.commit()
    grant = OutputsRepository().grant_file(command, new_operation_key(), owned)
    assert grant.kind is DbOutcomeKind.COMPLETED, grant.error
    return owned, command, grant.value

@pytest.fixture
def diagnostic_read_case(request, consumer):
    return prepare_diagnostic_read_case(request, consumer)

@pytest.fixture
def diagnostic_checksum_case(request, consumer):
    return prepare_diagnostic_read_case(request, consumer, known_digest=True)


async def execute_read(runtime, command):
    if command.processing_id is not None:
        return await run_recording_media(runtime, command.action_id, command.processing_id, 501,
                                         target_extension="part")
    return await obtain_flow.advance_obtain(runtime)


class SaveReadPort(OperationRepository):
    def __init__(self, *, failed=False):
        self.failed = failed
        self.calls = []
        self.error = OSError(5, "result not saved")

    def finish_attempt(self, request, key, worker):
        if request.ticket.operation == "read":
            self.calls.append((request, key))
            if self.failed:
                return DbOutcome(DbOutcomeKind.ROLLED_BACK, error=self.error)
        return super().finish_attempt(request, key, worker)


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", ["device", "host", "internal"])
@pytest.mark.parametrize("save_failed", [False, True])
async def test_actual_read_error_is_held_before_result_save(monkeypatch, consumer, save_failed, diagnostic_read_case, tmp_path):
    owned, command, prepared = diagnostic_read_case
    error = OSError(5, "actual source read failed", "/source/original.mov")
    error.__cause__ = PermissionError(13, "original source permission")
    tickets = []
    class Driver:
        async def open_read(self, source, offset, ticket, *, idle_timeout_s):
            tickets.append(ticket)
            raise error
    if consumer == "host":
        def open_source(self, path, offset):
            raise error
        monkeypatch.setattr(LocalSourceReader, "__init__", open_source)
    port = SaveReadPort(failed=save_failed)
    records, fallback = [], []
    channel = recording_log_channel(records)
    with repository_port(OperationRepository, port, ("finish_attempt",),
            matches=lambda _name, args, _kwargs: (
                args[0].ticket.operation == "read" and
                args[0].ticket.target_id == str(prepared.copy_id))):
        async with read_test_runtime(owned, Driver(), EVIDENCE, staging=tmp_path / "staging",
                internal=consumer == "internal", occurred_at=lambda: command.occurred_at + 20) as runtime:
            with error_diagnostics_context(channel, fallback.append):
                async with diagnostic_scope():
                    if save_failed:
                        with pytest.raises(ConsistencyError) as caught:
                            await execute_read(runtime, command)
                        assert caught.value.__cause__ is port.error
                    else:
                        await execute_read(runtime, command)
                    assert not records
                await flush_diagnostics()
                await flush_diagnostics()
    request, key = port.calls[0]
    assert len(port.calls) == 1
    ticket = request.ticket
    assert ticket.target_id == str(prepared.copy_id)
    if consumer != "host":
        assert tickets == [ticket]
    state, stored = owned.connection.execute("SELECT status,error_json FROM operation_attempts WHERE run_id=? AND attempt_no=?",
        (ticket.run_id, ticket.attempt_id)).fetchone()
    assert state == (1 if save_failed else 3)
    assert (stored is None) == save_failed
    assert len(records) == 1 and not fallback
    recorded = json.loads(records[0].message)
    text = json.dumps(recorded)
    assert "actual source read failed" in text and "/source/original.mov" in text
    assert "original source permission" in text
    assert recorded["ticket"]["run_id"] == ticket.run_id
    assert recorded["ticket"]["attempt_id"] == ticket.attempt_id


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", ["device", "host", "internal"])
async def test_checksum_exhaustion_records_verification_error(monkeypatch, consumer, diagnostic_checksum_case, tmp_path):
    owned, command, prepared = diagnostic_checksum_case
    # 已固定的来源摘要与实际内容不同，先完成一次真实重拷登记，再观察耗尽。
    staging = tmp_path / "staging"
    (staging / "derived").mkdir(parents=True)
    (staging / "derived/801.mp4").write_bytes(_CONTENT)
    records = []
    channel = recording_log_channel(records)
    async with read_test_runtime(owned, _ReadDriver(), EVIDENCE, staging=staging,
            internal=consumer == "internal", segment_size=len(_CONTENT),
            occurred_at=lambda: command.occurred_at + 20) as runtime:
        await execute_read(runtime, command)
        assert owned.connection.execute("SELECT round,recopies_used FROM file_copies WHERE id=?", (prepared.copy_id,)).fetchone() == (2, 1)
        with error_diagnostics_context(channel, lambda _: None):
            async with diagnostic_scope():
                await execute_read(runtime, command)
    assert len(records) == 1
    assert json.loads(records[0].message)["stage"] == "checksum_exhausted"
    assert "RecopyExhausted" in records[0].message
    assert owned.connection.execute("SELECT verification_state,recopies_used FROM file_copies WHERE id=?", (prepared.copy_id,)).fetchone() == (4, 1)


@pytest.mark.asyncio
async def test_normal_cancellation_before_dispatch_has_no_error_record(diagnostic_read_case, tmp_path):
    owned, command, prepared = diagnostic_read_case
    driver = _ReadDriver()
    class CancelAtIntent(OperationRepository):
        tickets = []
        def begin_attempt(self, intent, key, worker):
            result = super().begin_attempt(intent, key, worker)
            if intent.operation == "read":
                self.tickets.append(result.value.ticket)
                prepare_cancel_target(worker, catalog=Catalog(), now=command.occurred_at + 20,
                                      action_id=command.action_id)
            return result
    port = CancelAtIntent()
    records = []
    channel = recording_log_channel(records)
    with repository_port(OperationRepository, port, ("begin_attempt",),
            matches=lambda _name, args, _kwargs: (
                args[0].operation == "read" and
                args[0].target.copy_id == prepared.copy_id)):
        async with read_test_runtime(owned, driver, EVIDENCE, staging=tmp_path / "staging",
                occurred_at=lambda: command.occurred_at + 20) as runtime:
            with error_diagnostics_context(channel, lambda _: None):
                async with diagnostic_scope():
                    await execute_read(runtime, command)
    assert len(port.tickets) == 1 and driver.offsets == []
    assert records == []
    assert owned.connection.execute("SELECT attempts_used FROM operation_runs WHERE copy_id=?", (prepared.copy_id,)).fetchone() == (1,)


@pytest.mark.asyncio
async def test_download_read_error_keeps_complete_readable_and_raw_stderr(diagnostic_read_case, tmp_path):
    owned, command, prepared = diagnostic_read_case
    raw = "USB传输失败\n".encode() + b"\xff" + b"x" * 2048
    error = CameraReadError("download failed", [{"name": "download", "local_exit_code": 17,
                                                "stderr_hex": raw.hex()}])
    class Driver:
        tickets = []
        async def open_read(self, source, offset, ticket, *, idle_timeout_s):
            self.tickets.append(ticket)
            raise error
    driver, records = Driver(), []
    channel = recording_log_channel(records)
    async with read_test_runtime(owned, driver, EVIDENCE, staging=tmp_path / "staging",
            occurred_at=lambda: command.occurred_at + 20) as runtime:
        with error_diagnostics_context(channel, lambda _: None):
            async with diagnostic_scope():
                await execute_read(runtime, command)
    assert len(driver.tickets) == len(records) == 1
    details = json.loads(records[0].message)["error"]["details"]["exception"]["diagnostic_details"]["steps"][0]
    assert details["stderr_hex"] == raw.hex()
    assert details["stderr_text"] == raw.decode("utf-8", "backslashreplace")


class StopReadExecutor(FileTaskExecutor):
    """通过正式文件任务控制对象请求停止，不改写业务结果。"""
    control = None
    async def run_owned_async_file_task(self, task):
        body = task.body
        async def run(control):
            self.control = control
            return await body(control)
        return await super().run_owned_async_file_task(replace(task, body=run))


@pytest.mark.asyncio
@pytest.mark.parametrize("actual_write_error", [False, True])
async def test_stopped_source_preserves_real_write_error_without_logging_control_stop(actual_write_error, diagnostic_read_case, tmp_path, monkeypatch):
    owned, command, prepared = diagnostic_read_case
    supervisor = Supervisor()
    executor = StopReadExecutor(supervisor)
    original = OSError(28, "disk full")
    class Stream:
        def read(self, limit):
            if not actual_write_error:
                executor.control.request_stop()
            return b"x" * limit
        def cancel(self):
            pass
        def close(self):
            pass
    class Driver:
        sessions = []
        async def open_read(self, source, offset, ticket, *, idle_timeout_s):
            session = ReadSession(source, offset, Stream(), idle_timeout_s)
            self.sessions.append(session)
            return session
    def fail_write(self, data):
        executor.control.request_stop()
        raise original
    if actual_write_error:
        monkeypatch.setattr(PositionedWriter, "write", fail_write)
    driver, records = Driver(), []
    channel = recording_log_channel(records)
    async with read_test_runtime(owned, driver, EVIDENCE, staging=tmp_path / "staging",
            file_executor=executor, occurred_at=lambda: command.occurred_at + 20) as runtime:
        with error_diagnostics_context(channel, lambda _: None):
            async with diagnostic_scope():
                await execute_read(runtime, command)
    settled = await supervisor.drain_required()
    assert not settled.failed
    assert len(driver.sessions) == 1
    assert (await driver.sessions[0].wait_stopped()).error == "stopped"
    assert len(records) == int(actual_write_error)
    assert owned.connection.execute("SELECT status,result_json FROM operation_attempts WHERE run_id=?", (prepared.run_id,)).fetchone() == (1, None)
    if actual_write_error:
        record = json.loads(records[0].message)
        assert '"errno": 28' in json.dumps(record) and "disk full" in json.dumps(record)


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", ["host"])
async def test_local_read_normal_stopped_result_is_not_error(monkeypatch, diagnostic_read_case, tmp_path):
    owned, command, prepared = diagnostic_read_case
    supervisor = Supervisor()
    executor = StopReadExecutor(supervisor)
    staging = tmp_path / "staging"
    (staging / "derived").mkdir(parents=True)
    (staging / "derived/801.mp4").write_bytes(b"x" * 4096)
    read_chunk = LocalSourceReader.read_chunk
    def stop_after_read(self, limit):
        result = read_chunk(self, limit)
        executor.control.request_stop()
        return result
    monkeypatch.setattr(LocalSourceReader, "read_chunk", stop_after_read)
    records = []
    channel = recording_log_channel(records)
    async with read_test_runtime(owned, _ReadDriver(), EVIDENCE, staging=staging,
            file_executor=executor, occurred_at=lambda: command.occurred_at + 20) as runtime:
        with error_diagnostics_context(channel, lambda _: None):
            async with diagnostic_scope():
                await execute_read(runtime, command)
    assert not (await supervisor.drain_required()).failed
    assert not records, json.dumps({"records": [r.message for r in records], "attempt": owned.connection.execute("SELECT status,error_json,result_json FROM operation_attempts WHERE run_id=?", (prepared.run_id,)).fetchone()}, ensure_ascii=False)
    assert owned.connection.execute("SELECT status,result_json FROM operation_attempts WHERE run_id=?", (prepared.run_id,)).fetchone() == (1, None)

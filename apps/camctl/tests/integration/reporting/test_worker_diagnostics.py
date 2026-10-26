"""报告失败通过有界控制消息交付完整诊断，超限时直接使用 stderr。"""

import json
from multiprocessing.connection import Connection
from unittest.mock import create_autospec

import pytest

from camctl.persistence.runtime import StateDatabaseError
from camctl.reporting import worker
from camctl.reporting.messages import (
    MAX_MESSAGE_BYTES, ErrorKind, JobMessage, MessageProtocolError,
    ResultFailureMessage, ResultSuccessMessage, StartupFailedMessage, StartupPhase,
    decode_message, encode_message,
)


@pytest.fixture
def job():
    return JobMessage(
        job_id="diagnostic-1", report_id=1, from_wm=0, to_wm=1,
        frozen_event_id=1, instance_id="a" * 32, db_path="/state.db",
        staging_path="/staging/report.tmp", staging_root="/staging",
        ready_root="/ready", processing_root="/processing",
        entity_batch_size=1, event_batch_size=1, busy_timeout_ms=100,
    )


def _failure(**fields):
    return ResultFailureMessage(
        job_id="diagnostic-1", instance_id="a" * 32,
        error_kind=ErrorKind.REPORT, error_code="RuntimeError",
        error_message="生成失败", **fields,
    )


@pytest.mark.parametrize("factory", [
    _failure,
    lambda **fields: StartupFailedMessage(
        phase=StartupPhase.WORK_LOCK, reason="工作锁失败", **fields),
])
def test_failure_wire_keeps_complete_diagnostic_and_legacy_defaults(factory):
    graph = {"type": "builtins.OSError", "message": "完整原因\x00末尾",
             "cause": {"$ref": "#"}, "notes": ["补充事实"]}
    message = factory(diagnostic=graph)

    assert decode_message(encode_message(message)) == message
    assert json.loads(encode_message(message))["diagnostic"] == graph
    legacy = decode_message(encode_message(factory()))
    assert legacy.diagnostic is None
    assert legacy.diagnostic_fallback is None
    assert legacy.diagnostic_size_bytes is None
    assert "diagnostic" not in json.loads(encode_message(factory()))


@pytest.mark.parametrize("factory", [
    _failure,
    lambda **fields: StartupFailedMessage(
        phase=StartupPhase.RUNTIME_CHECK, reason="运行库失败", **fields),
])
def test_fallback_wire_preserves_route_and_diagnostic_byte_count(factory):
    message = factory(diagnostic_fallback="stderr", diagnostic_size_bytes=70001)

    assert decode_message(encode_message(message)) == message


@pytest.mark.parametrize("fields", [
    {"diagnostic": []},
    {"diagnostic_fallback": "file", "diagnostic_size_bytes": 1},
    {"diagnostic_fallback": "stderr"},
    {"diagnostic_size_bytes": 1},
    {"diagnostic_fallback": "stderr", "diagnostic_size_bytes": True},
    {"diagnostic_fallback": "stderr", "diagnostic_size_bytes": 0},
    {"diagnostic_fallback": "stderr", "diagnostic_size_bytes": -1},
    {"diagnostic": {}, "diagnostic_fallback": "stderr", "diagnostic_size_bytes": 1},
])
def test_invalid_diagnostic_route_is_rejected(fields):
    with pytest.raises(MessageProtocolError):
        _failure(**fields)


def test_job_failure_keeps_original_exception_group_cause_and_notes(mocker, job):
    cause = OSError(5, "原始设备原因", b"/raw\xff")
    leaf = RuntimeError("完整生成原因" * 400)
    leaf.__cause__ = cause
    leaf.add_note("不能丢失的补充说明")
    error = ExceptionGroup("生成和收场均失败", [leaf, ValueError("收场原因")])
    mocker.patch.object(worker, "_read_job_metadata", autospec=True, side_effect=error)
    stderr = mocker.patch.object(worker.os, "write", autospec=True)

    result = worker.run_job(job)

    assert result.error_code == "ExceptionGroup"
    graph = result.diagnostic
    assert graph["type"] == "builtins.ExceptionGroup"
    assert graph["traceback"]
    assert graph["exceptions"][0]["message"] == str(leaf)
    assert graph["exceptions"][0]["notes"] == ["不能丢失的补充说明"]
    assert graph["exceptions"][0]["cause"]["errno"] == 5
    assert graph["exceptions"][0]["cause"]["filename"] == {"bytes_hex": b"/raw\xff".hex()}
    assert graph["exceptions"][1]["message"] == "收场原因"
    assert decode_message(encode_message(result)).diagnostic == graph
    stderr.assert_not_called()


@pytest.mark.parametrize("message_size", [MAX_MESSAGE_BYTES - 1024, MAX_MESSAGE_BYTES + 1])
def test_oversize_job_diagnostic_uses_complete_stderr_and_bounded_result(mocker, job, message_size):
    error = RuntimeError("x" * message_size + "完整尾部")
    error.add_note("尾部补充事实")
    mocker.patch.object(worker, "_read_job_metadata", autospec=True, side_effect=error)
    captured = bytearray()

    def partial_write(fd, data):
        assert fd == 2
        count = min(307, len(data))
        captured.extend(data[:count])
        return count

    mocker.patch.object(worker.os, "write", autospec=True, side_effect=partial_write)

    result = worker.run_job(job)

    assert result.error_code == "RuntimeError"
    assert len(result.error_message) <= 1024
    assert result.diagnostic is None
    assert result.diagnostic_fallback == "stderr"
    document = json.loads(captured)
    assert document["job_id"] == job.job_id
    assert document["instance_id"] == job.instance_id
    graph = document["diagnostic"]
    assert graph["message"] == str(error)
    assert graph["notes"] == ["尾部补充事实"]
    diagnostic_bytes = json.dumps(
        graph, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    assert result.diagnostic_size_bytes == len(diagnostic_bytes)
    if message_size < MAX_MESSAGE_BYTES:
        assert result.diagnostic_size_bytes < MAX_MESSAGE_BYTES
    encoded = encode_message(result)
    assert len(encoded) <= MAX_MESSAGE_BYTES
    assert decode_message(encoded) == result


def test_stderr_failure_does_not_replace_the_original_job_result(mocker, job):
    error = OSError("生成失败" * MAX_MESSAGE_BYTES)
    mocker.patch.object(worker, "_read_job_metadata", autospec=True, side_effect=error)
    stderr = mocker.patch.object(worker.os, "write", autospec=True, side_effect=BrokenPipeError)

    result = worker.run_job(job)

    assert result.error_code == "OSError"
    assert result.error_kind is ErrorKind.REPORT
    assert result.diagnostic_fallback == "stderr"
    stderr.assert_called_once()


@pytest.mark.parametrize("phase", [StartupPhase.WORK_LOCK, StartupPhase.RUNTIME_CHECK])
def test_worker_startup_failure_preserves_the_caught_exception(mocker, phase):
    error = (worker.WorkLockError("锁失败") if phase is StartupPhase.WORK_LOCK
             else StateDatabaseError("运行库失败"))
    error.__cause__ = OSError(5, "底层完整原因")
    error.add_note("启动补充说明")
    connection = create_autospec(Connection, instance=True, spec_set=True)
    lock = create_autospec(worker._WorkLockHold, instance=True, spec_set=True)
    mocker.patch.object(worker, "install_parent_guard", autospec=True, return_value=None)
    mocker.patch.object(worker, "acquire_work_lock", autospec=True,
                        side_effect=error if phase is StartupPhase.WORK_LOCK else None,
                        return_value=lock)
    mocker.patch.object(worker, "ensure_runtime_library", autospec=True,
                        side_effect=error if phase is StartupPhase.RUNTIME_CHECK else None)
    mocker.patch.object(worker.time, "monotonic", autospec=True, return_value=0)

    assert worker.worker_main(connection, 123, "/state.db.report.lock", 1) == 3

    message = decode_message(connection.send_bytes.call_args.args[0])
    assert message.phase is phase
    assert message.diagnostic["message"] == str(error)
    assert message.diagnostic["cause"]["message"] == "[Errno 5] 底层完整原因"
    assert message.diagnostic["notes"] == ["启动补充说明"]
    assert message.diagnostic["traceback"]
    connection.close.assert_called_once()


def test_parent_guard_startup_failure_keeps_complete_original_reason(mocker):
    reason = "父进程保护失败" * 200
    connection = create_autospec(Connection, instance=True, spec_set=True)
    mocker.patch.object(worker, "install_parent_guard", autospec=True, return_value=reason)

    assert worker.worker_main(connection, 123, "/state.db.report.lock", 1) == 3

    message = decode_message(connection.send_bytes.call_args.args[0])
    assert len(message.reason) <= 256
    assert message.diagnostic == {"reason": reason}


def test_oversize_startup_diagnostic_uses_complete_stderr(mocker):
    error = worker.WorkLockError("锁失败" * MAX_MESSAGE_BYTES)
    error.__cause__ = OSError("完整底层原因")
    connection = create_autospec(Connection, instance=True, spec_set=True)
    captured = bytearray()

    def write(fd, data):
        assert fd == 2
        captured.extend(data)
        return len(data)

    mocker.patch.object(worker.os, "write", autospec=True, side_effect=write)

    worker._try_send_startup_failure(connection, StartupPhase.WORK_LOCK, error)

    message = decode_message(connection.send_bytes.call_args.args[0])
    assert message.diagnostic is None
    assert message.diagnostic_fallback == "stderr"
    assert message.diagnostic_size_bytes > MAX_MESSAGE_BYTES
    document = json.loads(captured)
    assert document["phase"] == StartupPhase.WORK_LOCK.value
    assert document["diagnostic"]["message"] == str(error)
    assert document["diagnostic"]["cause"]["message"] == "完整底层原因"


def _capture_stderr(mocker):
    captured = bytearray()

    def write(fd, data):
        assert fd == 2
        captured.extend(data)
        return len(data)

    mocker.patch.object(worker.os, "write", autospec=True, side_effect=write)
    return captured


def _ready_worker(mocker, job):
    connection = create_autospec(Connection, instance=True, spec_set=True)
    connection.recv_bytes.return_value = encode_message(job)
    lock = create_autospec(worker._WorkLockHold, instance=True, spec_set=True)
    mocker.patch.object(worker, "install_parent_guard", autospec=True, return_value=None)
    mocker.patch.object(worker, "acquire_work_lock", autospec=True, return_value=lock)
    mocker.patch.object(worker, "ensure_runtime_library", autospec=True)
    mocker.patch.object(worker.time, "monotonic", autospec=True, return_value=0)
    return connection, lock


@pytest.mark.parametrize("oversized", [False, True])
def test_result_send_failure_keeps_original_diagnostic_and_communication_error(mocker, job, oversized):
    error = RuntimeError("原始报告失败" * (MAX_MESSAGE_BYTES if oversized else 1))
    error.__cause__ = OSError("原始报告底层原因")
    mocker.patch.object(worker, "_read_job_metadata", autospec=True, side_effect=error)
    connection, lock = _ready_worker(mocker, job)
    send_error = OSError("控制消息发送失败")
    send_error.__cause__ = ValueError("通信底层原因")
    connection.send_bytes.side_effect = [None, send_error]
    captured = _capture_stderr(mocker)

    with pytest.raises(OSError) as caught:
        worker.worker_main(connection, 123, "/state.db.report.lock", 1)

    assert caught.value is send_error
    documents = [json.loads(line) for line in captured.splitlines()]
    graphs = [document["diagnostic"] for document in documents if "diagnostic" in document]
    assert len(graphs) == 1
    assert graphs[0]["message"] == str(error)
    assert graphs[0]["cause"]["message"] == "原始报告底层原因"
    communication = documents[-1]["communication_error"]
    assert communication["message"] == "控制消息发送失败"
    assert communication["cause"]["message"] == "通信底层原因"
    assert communication["traceback"]
    lock.close.assert_called_once()
    connection.close.assert_called_once()


@pytest.mark.parametrize("oversized", [False, True])
def test_startup_send_failure_keeps_original_diagnostic_and_communication_error(mocker, oversized):
    error = worker.WorkLockError("原始启动失败" * (MAX_MESSAGE_BYTES if oversized else 1))
    error.__cause__ = OSError("原始启动底层原因")
    connection = create_autospec(Connection, instance=True, spec_set=True)
    connection.send_bytes.side_effect = ValueError("启动消息发送失败")
    captured = _capture_stderr(mocker)

    worker._try_send_startup_failure(connection, StartupPhase.WORK_LOCK, error)

    documents = [json.loads(line) for line in captured.splitlines()]
    graphs = [document["diagnostic"] for document in documents if "diagnostic" in document]
    assert len(graphs) == 1
    assert graphs[0]["message"] == str(error)
    assert graphs[0]["cause"]["message"] == "原始启动底层原因"
    communication = documents[-1]["communication_error"]
    assert communication["message"] == "启动消息发送失败"
    assert communication["traceback"]


def test_success_result_send_failure_keeps_actual_communication_error(mocker, job):
    connection, lock = _ready_worker(mocker, job)
    mocker.patch.object(worker, "run_job", autospec=True, return_value=ResultSuccessMessage(
        job_id=job.job_id, instance_id=job.instance_id, path="/staging/report.json",
        size_bytes=1, sha256="b" * 64))
    send_error = OSError("成功结果发送失败")
    connection.send_bytes.side_effect = [None, send_error]
    captured = _capture_stderr(mocker)

    with pytest.raises(OSError) as caught:
        worker.worker_main(connection, 123, "/state.db.report.lock", 1)

    assert caught.value is send_error
    document = json.loads(captured)
    assert document["communication_error"]["message"] == "成功结果发送失败"
    assert "diagnostic" not in document
    lock.close.assert_called_once()
    connection.close.assert_called_once()

"""监督未决任务的收场：迟到错误保留，成功和错误身份不获资格。

进程与通信替身只在内存中运行，通信接口受真实 WorkerCommunicator
约束；真实管道、线程及完整帧顺序在组件集成层另行验证。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import create_autospec

import pytest

from camctl.reporting.communication import ChannelClosed, MessageEvent, WorkerCommunicator
from camctl.reporting.maintenance import MaintenanceLimits
from camctl.reporting.messages import (
    ErrorKind,
    JobMessage,
    ReadyMessage,
    ResultFailureMessage,
    ResultSuccessMessage,
    ShutdownMessage,
)
from camctl.reporting.supervisor import GenerationOutcomeKind, WorkerSupervisor

_INSTANCE = "f" * 32


def _job() -> JobMessage:
    return JobMessage(
        job_id="current-task", report_id=1, from_wm=0, to_wm=4,
        frozen_event_id=9, instance_id=_INSTANCE, db_path="/state.db",
        staging_path="/staging/reports/current.json", entity_batch_size=32,
        staging_root="/staging", ready_root="/ready", processing_root="/processing",
        event_batch_size=256, busy_timeout_ms=9000,
    )


def _failure(kind: ErrorKind = ErrorKind.STATE) -> ResultFailureMessage:
    return ResultFailureMessage(
        job_id="current-task", instance_id=_INSTANCE, error_kind=kind,
        error_code="ConsistencyError", error_message="冻结依据不可靠",
    )


def _success() -> ResultSuccessMessage:
    return ResultSuccessMessage(
        job_id="current-task", instance_id=_INSTANCE,
        path="/staging/reports/current.json", size_bytes=64, sha256="0" * 64,
    )


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class _Process:
    """符合 ProcessHandle 的退出事实；没有线程和真实进程。"""

    def __init__(self) -> None:
        self.alive = True
        self.exitcode = None

    def is_alive(self) -> bool:
        return self.alive

    def join(self, timeout: float | None = None) -> None:
        pass

    def terminate(self) -> None:
        self.alive = False
        self.exitcode = 1

    def kill(self) -> None:
        self.alive = False
        self.exitcode = 1


class _UnusedEnd:
    def close(self) -> None:
        pass


class _Channel:
    """完整消息 FIFO；退出后 close 保留送达消息再给终端。"""

    def __init__(self, process: _Process, clock: _Clock, *,
                 mode: str, late_message) -> None:
        self.process = process
        self.clock = clock
        self.mode = mode
        self.late_message = late_message
        self.events: asyncio.Queue = asyncio.Queue()
        self.events.put_nowait(MessageEvent(ReadyMessage()))
        self.job_sent = asyncio.Event()
        self.result_wait_started = asyncio.Event()
        self.terminal: ChannelClosed | None = None
        self.interface = create_autospec(
            WorkerCommunicator, instance=True, spec_set=True)
        self.interface.closed = False
        self.interface.send.side_effect = self.send
        self.interface.receive.side_effect = self.receive
        self.interface.close.side_effect = self.close

    async def send(self, message) -> None:
        if isinstance(message, JobMessage):
            self.job_sent.set()
            if self.mode == "send_failure":
                self._deliver_late()
                raise BrokenPipeError("任务发送失败")
            if self.mode == "deadline":
                self.clock.value = 10.0
            elif self.mode == "protocol":
                self.events.put_nowait(MessageEvent(ReadyMessage()))
            elif self.mode == "immediate":
                self.events.put_nowait(MessageEvent(self.late_message))
                self.late_message = None
            elif self.mode == "closed":
                self.process.alive = False
                self.process.exitcode = 0
                self.terminal = ChannelClosed(None)
                self.events.put_nowait(self.terminal)
            return
        assert isinstance(message, ShutdownMessage)
        self._deliver_late()

    def _deliver_late(self) -> None:
        if self.late_message is not None:
            self.events.put_nowait(MessageEvent(self.late_message))
            self.late_message = None
        self.process.alive = False
        self.process.exitcode = 0

    async def receive(self):
        if self.job_sent.is_set() and self.events.empty():
            self.result_wait_started.set()
        if self.terminal is not None and self.events.empty():
            return self.terminal
        event = await self.events.get()
        if isinstance(event, ChannelClosed):
            self.terminal = event
        return event

    async def close(self) -> None:
        self.interface.closed = True
        if self.terminal is None:
            self.terminal = ChannelClosed(None)
            self.events.put_nowait(self.terminal)


def _supervisor(monkeypatch, mode: str, late_message):
    process = _Process()
    clock = _Clock()
    channel = _Channel(process, clock, mode=mode, late_message=late_message)
    monkeypatch.setattr(
        "camctl.reporting.supervisor.WorkerCommunicator",
        lambda connection: channel.interface,
    )
    total = 0.001 if mode == "wait_timeout" else 2.0
    supervisor = WorkerSupervisor(
        limits=MaintenanceLimits(
            startup_seconds=2.0, total_seconds=total, stop_grace_seconds=0.01),
        lock_path="/state.db.report.lock", clock=clock,
        spawn=lambda: (process, object(), _UnusedEnd()),
    )
    return supervisor, channel, process


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["send_failure", "deadline", "wait_timeout", "protocol"])
async def test_retirement_keeps_matching_state_failure(monkeypatch, mode):
    """任一停止入口丢弃排队 STATE 都应使本用例失败。"""
    supervisor, _channel, process = _supervisor(monkeypatch, mode, _failure())
    outcome = await supervisor.generate(_job())
    assert outcome.kind is GenerationOutcomeKind.STATE_FAILURE
    assert outcome.failure == _failure()
    assert outcome.success is None
    assert outcome.detail
    assert not process.alive
    assert not supervisor.reusable


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [_success(), _failure(ErrorKind.REPORT), ReadyMessage()])
async def test_timeout_never_accepts_ordinary_late_result(monkeypatch, message):
    supervisor, _channel, process = _supervisor(monkeypatch, "deadline", message)
    outcome = await supervisor.generate(_job())
    assert outcome.kind is GenerationOutcomeKind.TIMEOUT
    assert outcome.success is None
    assert not process.alive
    assert not supervisor.reusable


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("job_id", "other-task"), ("instance_id", "0" * 32)])
@pytest.mark.parametrize("message", [_success(), _failure(), _failure(ErrorKind.REPORT)])
async def test_wrong_result_identity_is_protocol_failure(monkeypatch, field, value, message):
    supervisor, _channel, process = _supervisor(
        monkeypatch, "immediate", replace(message, **{field: value}))
    outcome = await supervisor.generate(_job())
    assert outcome.kind is GenerationOutcomeKind.PROTOCOL
    assert outcome.success is None
    assert outcome.failure is None
    assert not process.alive


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("job_id", "other-task"), ("instance_id", "0" * 32)])
async def test_late_state_from_wrong_identity_is_not_database_failure(monkeypatch, field, value):
    supervisor, _channel, process = _supervisor(
        monkeypatch, "deadline", replace(_failure(), **{field: value}))
    outcome = await supervisor.generate(_job())
    assert outcome.kind is GenerationOutcomeKind.PROTOCOL
    assert outcome.failure is None
    assert outcome.success is None
    assert not process.alive


@pytest.mark.asyncio
async def test_explicit_stop_reports_state_and_does_not_revive_generation(monkeypatch):
    supervisor, channel, process = _supervisor(monkeypatch, "pending", _failure())
    task = asyncio.create_task(supervisor.generate(_job()))
    try:
        await channel.job_sent.wait()
        await channel.result_wait_started.wait()
        shutdown = await supervisor.stop()
        assert shutdown.state_failure == _failure()
        outcome = await task
        assert outcome.kind is GenerationOutcomeKind.STATE_FAILURE
        assert outcome.failure == _failure()
        assert outcome.success is None
        assert not process.alive
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["deadline", "send_failure", "wait_timeout", "protocol"])
async def test_late_directory_configuration_failure_remains_fatal(monkeypatch, mode):
    failure = replace(_failure(ErrorKind.REPORT), error_code="configuration_error",
                      error_message="ready 当前路径与已保存绑定不同")
    supervisor, _channel, process = _supervisor(monkeypatch, mode, failure)

    outcome = await supervisor.generate(_job())

    assert outcome.kind is GenerationOutcomeKind.REPORT_FAILURE
    assert outcome.failure == failure
    assert outcome.success is None
    assert not process.alive


@pytest.mark.asyncio
async def test_explicit_stop_preserves_directory_configuration_failure(monkeypatch):
    failure = replace(_failure(ErrorKind.REPORT), error_code="configuration_error",
                      error_message="staging 当前路径与已保存绑定不同")
    supervisor, channel, process = _supervisor(monkeypatch, "pending", failure)
    task = asyncio.create_task(supervisor.generate(_job()))
    try:
        await channel.job_sent.wait()
        await channel.result_wait_started.wait()
        shutdown = await supervisor.stop()
        assert shutdown.configuration_failure == failure
        outcome = await task
        assert outcome.kind is GenerationOutcomeKind.REPORT_FAILURE
        assert outcome.failure == failure
        assert not process.alive
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_stop_after_confirmed_success_does_not_reclassify_old_message(monkeypatch):
    supervisor, channel, _process = _supervisor(monkeypatch, "immediate", _success())
    outcome = await supervisor.generate(_job())
    assert outcome.kind is GenerationOutcomeKind.SUCCESS
    channel.late_message = _failure()
    shutdown = await supervisor.stop()
    assert shutdown.state_failure is None
    assert outcome.kind is GenerationOutcomeKind.SUCCESS
    assert outcome.success == _success()


@pytest.mark.asyncio
async def test_channel_terminal_without_result_keeps_missing_result(monkeypatch):
    supervisor, _channel, process = _supervisor(monkeypatch, "closed", None)
    outcome = await supervisor.generate(_job())
    assert outcome.kind is GenerationOutcomeKind.CHANNEL_LOST
    assert outcome.success is None
    assert outcome.failure is None
    assert not process.alive


@pytest.mark.asyncio
async def test_kill_without_observed_exit_keeps_ownership(monkeypatch):
    """信号已发出但退出未获确认时，通道和进程仍由监督者持有。"""
    supervisor, channel, process = _supervisor(monkeypatch, "pending", None)

    async def ignore_shutdown(message):
        assert isinstance(message, ShutdownMessage)

    channel.interface.send.side_effect = ignore_shutdown
    process.terminate = lambda: None
    process.kill = lambda: None

    class AdvancingClock:
        def __init__(self):
            self.value = 0.0

        def __call__(self):
            self.value += 1.0
            return self.value

    # 启动已确认后再让三个退出观察期限逐个耗尽，避免真实等待。
    assert (await supervisor.start()).ready
    supervisor._clock = AdvancingClock()
    with pytest.raises(RuntimeError):
        await supervisor.stop()
    assert process.alive
    assert not channel.interface.closed
    assert supervisor._process is process
    assert supervisor._communicator is channel.interface
    assert not supervisor.reusable
    with pytest.raises(RuntimeError):
        await supervisor.start()

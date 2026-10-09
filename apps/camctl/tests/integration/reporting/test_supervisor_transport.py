"""报告监督者组件集成测试：真实管道、通信线程与替身进程。

监督者通过真实 Pipe 和通信线程接收就绪及结果消息，验证启动、
生成、退出顺序、超时与通道结束时的收场行为。
"""

from __future__ import annotations

import asyncio
import multiprocessing

import pytest

from camctl.reporting.messages import (
    ErrorKind,
    JobMessage,
    ReadyMessage,
    ResultFailureMessage,
    ResultSuccessMessage,
    StartupFailedMessage,
    StartupPhase,
    encode_message,
)
from camctl.reporting.maintenance import MaintenanceLimits
from camctl.reporting.supervisor import (
    GenerationOutcomeKind,
    WorkerSupervisor,
)

_INSTANCE = "f" * 32


def _job(job_id: str = "task-1") -> JobMessage:
    return JobMessage(
        job_id=job_id, report_id=1, from_wm=0, to_wm=4, frozen_event_id=9,
        instance_id=_INSTANCE, db_path="/var/lib/camctl/state.db",
        staging_path="/var/lib/camctl/staging/reports/report-1.json",
        staging_root="/var/lib/camctl/staging", ready_root="/var/lib/camctl/ready",
        processing_root="/var/lib/camctl/processing",
        entity_batch_size=32, event_batch_size=256, busy_timeout_ms=9000)


def _success(job_id: str = "task-1") -> ResultSuccessMessage:
    return ResultSuccessMessage(
        job_id=job_id, instance_id=_INSTANCE,
        path="/var/lib/camctl/staging/reports/report-1.json",
        size_bytes=64, sha256="0" * 64)


class _PeerEnd:
    """测试持有的对端引用：监督者关闭代理不影响真实端点。"""

    def __init__(self, connection) -> None:
        self._connection = connection

    def send_bytes(self, data: bytes) -> None:
        self._connection.send_bytes(data)

    def send(self, message) -> None:
        self.send_bytes(encode_message(message))

    def close(self) -> None:
        self._connection.close()


class _UnusedEnd:
    def close(self) -> None:
        pass


class _FakeProcess:
    def __init__(self) -> None:
        self.alive = True
        self.exitcode = None
        self.terminated = 0
        self.killed = 0

    def is_alive(self) -> bool:
        return self.alive

    def join(self, timeout: float | None = None) -> None:
        return None

    def terminate(self) -> None:
        self.terminated += 1
        self.alive = False
        self.exitcode = 1

    def kill(self) -> None:
        self.killed += 1
        self.alive = False
        self.exitcode = 1


class _Rig:
    """一次监督测试的替身组合：假进程、真实管道与计数 spawn。"""

    def __init__(self, limits: MaintenanceLimits | None = None) -> None:
        self.limits = limits or MaintenanceLimits(
            startup_seconds=2.0, total_seconds=2.0, stop_grace_seconds=0.05)
        self.process = _FakeProcess()
        parent, child = multiprocessing.Pipe(duplex=True)
        self.peer = _PeerEnd(child)
        self.spawn_calls = 0
        self.supervisor = WorkerSupervisor(
            limits=self.limits, lock_path="/tmp/state.db.report.lock",
            spawn=self._spawn)
        self.parent_end = parent

    def _spawn(self):
        self.spawn_calls += 1
        return self.process, self.parent_end, _UnusedEnd()


@pytest.mark.asyncio
class TestSupervisorStartup:
    async def test_ready_startup_reports_ready(self) -> None:
        rig = _Rig()
        rig.peer.send(ReadyMessage())
        startup = await rig.supervisor.start()
        assert startup.ready
        assert rig.spawn_calls == 1
        # 可复用时再次启动不再 spawn。
        again = await rig.supervisor.start()
        assert again.ready and rig.spawn_calls == 1
        await rig.supervisor.stop()

    async def test_startup_failure_carries_phase_and_reason(self) -> None:
        rig = _Rig()
        rig.peer.send(StartupFailedMessage(
            phase=StartupPhase.WORK_LOCK, reason="锁被占用"))
        startup = await rig.supervisor.start()
        assert not startup.ready
        assert startup.failure is not None
        assert startup.failure.phase is StartupPhase.WORK_LOCK
        assert not rig.supervisor.reusable

    async def test_startup_timeout_retires_process(self) -> None:
        rig = _Rig(limits=MaintenanceLimits(
            startup_seconds=0.1, total_seconds=1.0, stop_grace_seconds=0.05))
        startup = await rig.supervisor.start()
        assert not startup.ready
        assert "就绪" in startup.detail
        assert rig.process.terminated == 1


@pytest.mark.asyncio
class TestSupervisorGeneration:
    async def test_matching_success_keeps_worker_reusable(self) -> None:
        rig = _Rig()
        rig.peer.send(ReadyMessage())
        await rig.supervisor.start()
        outcome = await _generate_with_peer(rig, lambda: rig.peer.send(_success()))
        assert outcome.kind is GenerationOutcomeKind.SUCCESS
        assert outcome.success == _success()
        assert rig.supervisor.reusable
        await rig.supervisor.stop()

    async def test_old_task_result_rejects_current_generation(self) -> None:
        rig = _Rig()
        rig.peer.send(ReadyMessage())
        await rig.supervisor.start()
        outcome = await _generate_with_peer(
            rig,
            lambda: (rig.peer.send(_success(job_id="stale-job")),
                     rig.peer.send(_success(job_id="task-1"))))
        assert outcome.kind is GenerationOutcomeKind.PROTOCOL
        assert outcome.job_id == "task-1"
        assert outcome.success is None
        assert not rig.supervisor.reusable
        await rig.supervisor.stop()

    async def test_failure_result_retires_worker(self) -> None:
        rig = _Rig()
        rig.peer.send(ReadyMessage())
        await rig.supervisor.start()

        def deliver() -> None:
            rig.peer.send(ResultFailureMessage(
                job_id="task-1", instance_id=_INSTANCE,
                error_kind=ErrorKind.STATE, error_code="ConsistencyError",
                error_message="冻结依据不符"))

        outcome = await _generate_with_peer(rig, deliver)
        assert outcome.kind is GenerationOutcomeKind.STATE_FAILURE
        assert not rig.supervisor.reusable
        assert not rig.process.alive

    async def test_exit_checks_delivered_result(self) -> None:
        """结果已送达而退出先处理：仍按成功收场，进程不再复用。"""
        rig = _Rig()
        rig.peer.send(ReadyMessage())
        await rig.supervisor.start()
        outcome = await _generate_with_peer(rig, _exit_after_result(rig))
        assert outcome.kind is GenerationOutcomeKind.SUCCESS
        assert outcome.success is not None
        assert "不再复用" in outcome.detail
        assert not rig.supervisor.reusable

    async def test_exit_without_result_is_channel_lost(self) -> None:
        rig = _Rig()
        rig.peer.send(ReadyMessage())
        await rig.supervisor.start()
        outcome = await _generate_with_peer(rig, _exit_without_result(rig))
        assert outcome.kind is GenerationOutcomeKind.CHANNEL_LOST
        assert outcome.success is None

    async def test_total_deadline_times_out_and_escalates(self) -> None:
        rig = _Rig(limits=MaintenanceLimits(
            startup_seconds=2.0, total_seconds=0.1, stop_grace_seconds=0.05))
        rig.peer.send(ReadyMessage())
        await rig.supervisor.start()
        outcome = await rig.supervisor.generate(_job())
        assert outcome.kind is GenerationOutcomeKind.TIMEOUT
        assert rig.process.terminated == 1


def _exit_after_result(rig: _Rig):
    def peer() -> None:
        rig.peer.send(_success())
        rig.process.alive = False
        rig.peer.close()

    return peer


def _exit_without_result(rig: _Rig):
    def peer() -> None:
        rig.process.alive = False
        rig.peer.close()

    return peer


async def _generate_with_peer(rig: _Rig, peer_action) -> object:
    """先让派发进入等待，再由对端行动；退出类行动需要先生效。"""
    task = asyncio.create_task(rig.supervisor.generate(_job()))
    # 等待派发完成发送并进入结果等待（通信线程轮询周期为 50ms）。
    for _ in range(20):
        await asyncio.sleep(0.01)
        if task.done():
            break
    peer_action()
    return await asyncio.wait_for(task, 5)

"""S5 实际任务监督与取消接手的单元测试。

期望独立来自模块协作契约的状态分类与接手规则：等待者取消不释
放实际资源，实际结果只被接手消费一次；接手异常保留诊断且不阻
断其余任务收场。
"""

from __future__ import annotations

import asyncio

import pytest

from camctl.session.supervision import (
    OwnedTask,
    SupervisionError,
    Supervisor,
    TaskRecord,
)

pytestmark = pytest.mark.asyncio


class RecordingOwner:
    """受真实接口约束的拥有者替身：等待实际结果后保存/通知再释放。"""

    def __init__(self, fail_identities: frozenset[str] = frozenset()) -> None:
        self.receipts: list[str] = []
        self.released: set[str] = set()
        self.fail_identities = fail_identities
        self.awaited_actual: list[str] = []

    async def take_over(self, task: OwnedTask) -> None:
        result = await asyncio.shield(task.pending)
        self.awaited_actual.append(task.identity)
        if task.identity in self.fail_identities:
            raise RuntimeError(f"接手 {task.identity} 失败")
        self.receipts.append(task.identity)
        self.released.add(task.identity)


def _task(identity: str, pending, *, stage: str = "db-write", business: str = "capture") -> OwnedTask:
    return OwnedTask(
        identity=identity,
        stage=stage,
        business=business,
        resources=("device:1",),
        pending=pending,
    )


class TestRegisterAndHandoff:
    async def test_unknown_token_rejected(self) -> None:
        supervisor = Supervisor()
        pending = asyncio.get_running_loop().create_future()
        with pytest.raises(SupervisionError, match="未登记"):
            supervisor.handoff(object(), _task("op-1", pending))

    async def test_duplicate_identity_rejected_while_pending(self) -> None:
        supervisor = Supervisor()
        token = supervisor.register(RecordingOwner())
        pending = asyncio.get_running_loop().create_future()
        supervisor.handoff(token, _task("op-1", pending))
        with pytest.raises(SupervisionError, match="op-1"):
            supervisor.handoff(token, _task("op-1", pending))

    async def test_drain_empty_registry_returns_immediately(self) -> None:
        supervisor = Supervisor()
        result = await supervisor.drain_required()
        assert result.settled == ()
        assert result.failed == ()


class TestCancelKeepsActualOwner:
    async def test_cancel_keeps_actual_owner(self) -> None:
        supervisor = Supervisor()
        owner = RecordingOwner()
        token = supervisor.register(owner)
        loop = asyncio.get_running_loop()
        actual = loop.create_future()

        async def waiter() -> None:
            try:
                # 实际任务以屏蔽方式等待：等待者取消不中止实际任务。
                await asyncio.shield(actual)
            except asyncio.CancelledError:
                # 先登记责任再解除原等待。
                supervisor.handoff(token, _task("op-1", actual))
                raise

        waiter_task = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        waiter_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter_task
        # 等待者已取消：实际结果尚未产生，资源不得释放。
        assert owner.released == set()
        assert owner.awaited_actual == []

        # 实际任务随后结束（保存/通知由拥有者在接手中完成）。
        loop.call_later(0.01, actual.set_result, {"kind": "COMPLETED"})
        result = await supervisor.drain_required()
        assert owner.awaited_actual == ["op-1"]
        assert owner.receipts == ["op-1"]
        assert owner.released == {"op-1"}
        assert result.settled == (
            TaskRecord(identity="op-1", stage="db-write", business="capture", resources=("device:1",)),
        )
        assert result.failed == ()

    async def test_queued_withdrawal_race_consumed_once(self) -> None:
        # 撤回竞争以“确认未执行”收场：实际结果仍只消费一次。
        supervisor = Supervisor()
        owner = RecordingOwner()
        token = supervisor.register(owner)
        pending = asyncio.get_running_loop().create_future()
        supervisor.handoff(token, _task("op-2", pending, stage="queued"))
        pending.set_result({"kind": "NOT_EXECUTED"})
        await supervisor.drain_required()
        assert owner.receipts == ["op-2"]

    async def test_commit_unknown_consumed_once_without_retry(self) -> None:
        supervisor = Supervisor()
        owner = RecordingOwner()
        token = supervisor.register(owner)
        pending = asyncio.get_running_loop().create_future()
        supervisor.handoff(token, _task("op-3", pending))
        pending.set_result({"kind": "UNKNOWN"})
        await supervisor.drain_required()
        assert owner.receipts == ["op-3"]
        # 再次收场没有剩余责任，也不会重复消费。
        again = await supervisor.drain_required()
        assert again.settled == ()
        assert owner.receipts == ["op-3"]

    async def test_terminal_target_with_inflight_call_consumed_once(self) -> None:
        # 目标业务已由其他路径终态，但调用仍在途：结果仍交给拥有者一次。
        supervisor = Supervisor()
        owner = RecordingOwner()
        token = supervisor.register(owner)
        pending = asyncio.get_running_loop().create_future()
        supervisor.handoff(token, _task("op-4", pending))
        await asyncio.sleep(0.01)
        pending.set_result({"kind": "COMPLETED", "late": True})
        result = await supervisor.drain_required()
        assert owner.receipts == ["op-4"]
        assert result.settled == (
            TaskRecord(identity="op-4", stage="db-write", business="capture", resources=("device:1",)),
        )

    async def test_takeover_failure_recorded_and_others_settled(self) -> None:
        supervisor = Supervisor()
        owner = RecordingOwner(fail_identities=frozenset({"op-bad"}))
        token = supervisor.register(owner)
        loop = asyncio.get_running_loop()
        bad = loop.create_future()
        good = loop.create_future()
        bad.set_result({"kind": "COMPLETED"})
        good.set_result({"kind": "COMPLETED"})
        supervisor.handoff(token, _task("op-bad", bad))
        supervisor.handoff(token, _task("op-good", good))
        result = await supervisor.drain_required()
        assert owner.awaited_actual == ["op-bad", "op-good"]
        # 接手异常不重复投递实际结果，也不阻断其余任务收场。
        assert owner.receipts == ["op-good"]
        assert [f.record.identity for f in result.failed] == ["op-bad"]
        assert "op-bad" in result.failed[0].error
        assert [r.identity for r in result.settled] == ["op-good"]

    async def test_drain_consumes_late_handoffs(self) -> None:
        supervisor = Supervisor()
        first_started = asyncio.Event()

        class SignalingOwner(RecordingOwner):
            async def take_over(self, task: OwnedTask) -> None:
                if task.identity == "op-5":
                    first_started.set()
                await super().take_over(task)

        owner = SignalingOwner()
        token = supervisor.register(owner)
        loop = asyncio.get_running_loop()
        first = loop.create_future()
        second = loop.create_future()
        supervisor.handoff(token, _task("op-5", first))

        async def late_handoff() -> None:
            await first_started.wait()
            supervisor.handoff(token, _task("op-6", second))
            first.set_result({"kind": "COMPLETED"})
            second.set_result({"kind": "COMPLETED"})

        late = asyncio.create_task(late_handoff())
        result = await supervisor.drain_required()
        await late
        assert sorted(owner.receipts) == ["op-5", "op-6"]
        assert sorted(r.identity for r in result.settled) == ["op-5", "op-6"]

    async def test_cancelled_drain_resumes_without_double_consumption(self) -> None:
        supervisor = Supervisor()
        owner = RecordingOwner()
        token = supervisor.register(owner)
        loop = asyncio.get_running_loop()
        pending = loop.create_future()

        async def slow_actual() -> None:
            await asyncio.sleep(0.02)
            pending.set_result({"kind": "COMPLETED"})

        supervisor.handoff(token, _task("op-7", pending))
        releaser = asyncio.create_task(slow_actual())
        first_drain = asyncio.create_task(supervisor.drain_required())
        await asyncio.sleep(0.005)
        # 拥有者仍在等待实际结果时，外部取消收场等待。
        first_drain.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_drain
        # 实际消费不受影响；重试沿同一收场等待，不重复消费。
        result = await supervisor.drain_required()
        await releaser
        assert owner.receipts == ["op-7"]
        assert [r.identity for r in result.settled] == ["op-7"]
        # 收场完成后再次收场是新的空收场。
        again = await supervisor.drain_required()
        assert again.settled == ()
        assert owner.receipts == ["op-7"]

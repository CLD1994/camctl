"""P2 队列决策核心的单元测试。

期望独立来自持久化运行规格：容量按等待条目计量，取出或确认撤回
释放；业务优先；入队截止与空位授予在同一临界区形成唯一结果。
不使用真实线程，直接驱动决策核心。
"""

from __future__ import annotations

import asyncio

import pytest

from camctl.contracts.values import OperationKey
from camctl.persistence.executor import QueueCore
from camctl.persistence.models import (
    DbExecutorError,
    DbJob,
    DbJobKind,
    DbPriority,
    WithdrawalStatus,
)

pytestmark = pytest.mark.asyncio


def _key(index: int) -> OperationKey:
    return OperationKey(f"{index:032x}")


def _job(index: int, *, priority: DbPriority = DbPriority.BUSINESS, kind: DbJobKind = DbJobKind.WRITE) -> DbJob:
    return DbJob(
        key=_key(index),
        description=f"job-{index}",
        kind=kind,
        priority=priority,
        execute=lambda connection: None,
    )


def _register(core: QueueCore, job: DbJob):
    result = asyncio.get_running_loop().create_future()
    return core.register_admission(job, result)


class TestCapacityAndPriority:
    async def test_admit_up_to_capacity_then_full(self) -> None:
        core = QueueCore(capacity=2)
        assert core.try_admit(_job(1)) is True
        assert core.try_admit(_job(2)) is True
        assert core.try_admit(_job(3)) is False
        assert core.queued_count() == 2

    async def test_invalid_capacity_rejected(self) -> None:
        for bad in (0, -1, True, "8", 1.5):
            with pytest.raises(DbExecutorError):
                QueueCore(capacity=bad)  # type: ignore[arg-type]

    async def test_business_preferred_over_snapshot(self) -> None:
        core = QueueCore(capacity=4)
        core.try_admit(_job(1, priority=DbPriority.SNAPSHOT))
        core.try_admit(_job(2))
        first = core.pop_next()
        second = core.pop_next()
        assert (first.key, second.key) == (_key(2), _key(1))

    async def test_fifo_within_priority(self) -> None:
        core = QueueCore(capacity=4)
        core.try_admit(_job(2))
        core.try_admit(_job(1))
        first = core.pop_next()
        second = core.pop_next()
        assert (first.key, second.key) == (_key(2), _key(1))

    async def test_pop_empty_returns_none(self) -> None:
        core = QueueCore(capacity=1)
        assert core.pop_next() is None


class TestAdmissionAndTimeout:
    async def test_pop_frees_slot_and_grants_earliest_waiter(self) -> None:
        core = QueueCore(capacity=1)
        core.try_admit(_job(1))
        admission = _register(core, _job(2))
        assert core.try_admit(_job(3)) is False
        popped = core.pop_next()
        assert popped is not None and popped.key == _key(1)
        granted = core.admit_next_waiting()
        assert granted is admission
        assert admission.state == admission.GRANTED
        # 空位交给等待者后队列仍满。
        assert core.queued_count() == 1
        assert core.try_admit(_job(4)) is False

    async def test_timeout_settles_and_grant_skips_it(self) -> None:
        core = QueueCore(capacity=1)
        core.try_admit(_job(1))
        admission = _register(core, _job(2))
        assert core.settle_timeout(admission) is True
        assert admission.state == admission.TIMED_OUT
        core.pop_next()
        # 已超时的等待者不再取得空位。
        assert core.admit_next_waiting() is None
        assert core.queued_count() == 0

    async def test_grant_wins_over_late_timeout(self) -> None:
        core = QueueCore(capacity=1)
        core.try_admit(_job(1))
        admission = _register(core, _job(2))
        core.pop_next()
        core.admit_next_waiting()
        # 截止通知迟到：操作已经入队，不能改判为超时。
        assert core.settle_timeout(admission) is False
        assert admission.state == admission.GRANTED
        popped = core.pop_next()
        assert popped is not None and popped.key == _key(2)

    async def test_settled_timeout_is_final(self) -> None:
        core = QueueCore(capacity=1)
        core.try_admit(_job(1))
        admission = _register(core, _job(2))
        assert core.settle_timeout(admission) is True
        # 重复结算不能翻转已经确认的未入队结果。
        assert core.settle_timeout(admission) is True


class TestWithdrawal:
    async def test_withdraw_queued_frees_slot_for_waiter(self) -> None:
        core = QueueCore(capacity=1)
        core.try_admit(_job(1))
        admission = _register(core, _job(2))
        assert core.withdraw(_key(1)) is WithdrawalStatus.WITHDRAWN
        granted = core.admit_next_waiting()
        assert granted is admission
        assert core.queued_count() == 1

    async def test_withdraw_waiting_admission_settles_it(self) -> None:
        core = QueueCore(capacity=1)
        core.try_admit(_job(1))
        admission = _register(core, _job(2))
        assert core.withdraw(_key(2)) is WithdrawalStatus.WITHDRAWN
        assert admission.state == admission.TIMED_OUT
        core.withdraw(_key(1))
        assert core.admit_next_waiting() is None

    async def test_withdraw_dispatched_reports_dispatched(self) -> None:
        core = QueueCore(capacity=2)
        core.try_admit(_job(1))
        core.pop_next()
        assert core.withdraw(_key(1)) is WithdrawalStatus.DISPATCHED
        # 完成后不再属于执行器范围。
        core.complete(_key(1))
        assert core.withdraw(_key(1)) is None

    async def test_withdraw_unknown_returns_none(self) -> None:
        core = QueueCore(capacity=1)
        assert core.withdraw(_key(404)) is None

    async def test_withdraw_all_returns_queued_and_pending(self) -> None:
        core = QueueCore(capacity=2)
        core.try_admit(_job(1))
        core.try_admit(_job(2, priority=DbPriority.SNAPSHOT))
        admission = _register(core, _job(3))
        core.pop_next()  # job1 派发，取出释放条目；授予由执行器调用 admit_next_waiting
        jobs, admissions = core.withdraw_all()
        assert [job.key for job in jobs] == [_key(2)]
        assert [item.job.key for item in admissions] == [_key(3)]
        assert core.has_queued() is False
        assert admission.state == admission.TIMED_OUT

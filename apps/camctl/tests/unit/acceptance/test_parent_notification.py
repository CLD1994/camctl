"""A5 父计划状态派生与提交通知的单元测试。

期望独立来自计划运行状态规则：全部动作终态才 COMPLETED；曾实际
开始则 RUNNING；否则 PENDING。通知只在完整提交后发出一次，回滚
不发成功通知。
"""

from __future__ import annotations

import pytest

from camctl.acceptance.rules import ActionManagement, PlanState, derive_plan_state
from camctl.acceptance.service import AckDisposition, PlanDisposition
from camctl.acceptance.notification import AcceptanceNotifier, notify_acceptance


class RecordingNotifier:
    def __init__(self) -> None:
        self.notifications: list[int] = []

    def work_available(self, plan_id: int) -> None:
        self.notifications.append(plan_id)


def _actions(*entries: tuple[int, int]) -> list[ActionManagement]:
    return [ActionManagement(status=status, execution_started=started) for status, started in entries]


class TestDerivePlanState:
    def test_future_pending_is_work(self) -> None:
        # 未执行而取消的动作不是开始事实：剩余 pending 仍 PENDING。
        state = derive_plan_state(_actions((6, 0), (1, 0)))
        assert state is PlanState.PENDING

    def test_started_fact_makes_running(self) -> None:
        state = derive_plan_state(_actions((3, 1), (1, 0)))
        assert state is PlanState.RUNNING

    def test_all_terminal_is_completed(self) -> None:
        state = derive_plan_state(_actions((3, 1), (4, 1), (6, 0)))
        assert state is PlanState.COMPLETED

    def test_cancelled_never_started_pending_with_future(self) -> None:
        state = derive_plan_state(_actions((6, 0), (1, 0), (5, 0)))
        assert state is PlanState.PENDING

    def test_empty_actions_pending(self) -> None:
        state = derive_plan_state([])
        assert state is PlanState.PENDING


class TestNotifyAcceptance:
    def _result(self, disposition: PlanDisposition):
        from camctl.acceptance.service import AcceptanceResult

        return AcceptanceResult(
            plan_disposition=disposition,
            plan_id=5,
            ack_disposition=AckDisposition.NOT_PROVIDED,
            ack_watermark=0,
        )

    def test_committed_registration_notifies_once(self) -> None:
        notifier = RecordingNotifier()
        notify_acceptance(self._result(PlanDisposition.REGISTERED), notifier)
        assert notifier.notifications == [5]

    def test_reuse_and_rejection_do_not_notify_new_work(self) -> None:
        notifier = RecordingNotifier()
        notify_acceptance(self._result(PlanDisposition.REUSED), notifier)
        notify_acceptance(self._result(PlanDisposition.REJECTED), notifier)
        assert notifier.notifications == []

    def test_notification_survives_waiter_cancel(self) -> None:
        # 通知由责任拥有者消费提交结果触发，不依赖原等待者存在：
        # 等待者在实际提交完成前被取消，通知仍恰好一次。
        import asyncio

        def scenario() -> int:
            notifier = RecordingNotifier()
            result = self._result(PlanDisposition.REGISTERED)

            async def main() -> int:
                loop = asyncio.get_running_loop()
                commit = loop.create_future()

                async def waiter() -> None:
                    await asyncio.shield(commit)

                waiter_task = asyncio.create_task(waiter())
                await asyncio.sleep(0)
                waiter_task.cancel()
                try:
                    await waiter_task
                except asyncio.CancelledError:
                    pass
                # 实际提交随后完成：接手方消费结果并通知。
                commit.set_result(result)
                notify_acceptance(commit.result(), notifier)
                return len(notifier.notifications)

            return asyncio.run(main())

        assert scenario() == 1

    def test_rollback_result_never_notifies(self) -> None:
        # 仓储回滚在服务层是状态库错误：调用方不进入通知分支。
        import asyncio

        from camctl.acceptance.input import ParsedInput
        from camctl.acceptance.service import AcceptanceContext, AcceptanceStateError, CommandMode, accept_input
        from camctl.contracts.values import new_operation_key
        from camctl.persistence.models import DbOutcome, DbOutcomeKind

        class RollingBackRepository:
            def process_input(self, command, key, owned):
                return DbOutcome(kind=DbOutcomeKind.ROLLED_BACK, error=RuntimeError("约束失败"))

        context = AcceptanceContext(
            mode=CommandMode.SUBMIT,
            catalog=None,
            repository=RollingBackRepository(),
            clock=type("C", (), {"utc_micros": staticmethod(lambda: 1)})(),
        )
        parsed = ParsedInput(path="p", document={"request_id": "1"})

        async def scenario() -> None:
            await accept_input(parsed, context, new_operation_key(), None)

        with pytest.raises(AcceptanceStateError):
            asyncio.run(scenario())

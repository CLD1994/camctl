"""按真实目标类型组织的取消收场端口实现。

拍摄目标等待执行链的停止收场（不越权代停止）；取回目标推进交付撤
回（ready 撤回、processing 不可撤回、位置未知先请求并等待核实）；
清理目标对未发出删除的成员解除限制，删除中的成员由清理执行链收
场；取消动作目标转发发起者收场；报告目标的本次责任分类完成，共
享生成不在目标范围。
"""

from __future__ import annotations

from contextlib import closing
from typing import Any, Callable

from camctl.cancellation.service import OriginSettle
from camctl.cancellation.ports import SettlementOutcome
from camctl.cancellation.service import settle_origin_cancel
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture_facts import load_start_facts
from camctl.persistence.transaction import row_facts

__all__ = ["TargetSettlement"]

#: 动作类型与终态集合。
_CAPTURE_TYPES = frozenset({1, 2, 3})
_ACTION_TERMINAL = (3, 4, 5, 6)
_OBTAIN_TYPE, _CLEANUP_TYPE, _CANCEL_TYPE, _REPORT_TYPE = 4, 5, 6, 7
#: deliveries.status：PUBLISHED。
_PUBLISHED = 5
#: cleanup_items.status：UNRESOLVED/PENDING_DELETE/DELETING/SUCCEEDED/FAILED。
_CLEANUP_UNRESOLVED, _CLEANUP_PENDING, _CLEANUP_DELETING = 1, 2, 3
_CLEANUP_SUCCEEDED, _CLEANUP_FAILED = 4, 5
#: cancel_delivery_items.status：PENDING/WITHDRAWN/NOT_RETRACTABLE/FAILED。
_DELIVERY_ITEM_PENDING, _DELIVERY_ITEM_WITHDRAWN = 1, 2
_DELIVERY_ITEM_NOT_RETRACTABLE, _DELIVERY_ITEM_FAILED = 3, 4
#: deliveries.withdrawal_state：NOT_REQUESTED。
_WITHDRAWAL_NOT_REQUESTED = 1
_RUN = enum_for("operation_runs.status")
_KIND = enum_for("operation_runs.kind")


class TargetSettlement:
    """目标拥有者收场端口的真实实现；按目标动作类型分派。"""

    def __init__(self, owned: Any, outputs: Any, cancellations: Any,
                 withdrawal_positions: Callable[[int], str],
                 occurred_at: Callable[[], int]) -> None:
        self._owned = owned
        self._outputs = outputs
        self._cancellations = cancellations
        self._withdrawal_positions = withdrawal_positions
        self._occurred_at = occurred_at

    async def settle(self, target_action_id: int) -> SettlementOutcome:
        row = self._owned.connection.execute(
            "SELECT type, status FROM actions WHERE id = ?",
            (target_action_id,)).fetchone()
        if row is None:
            raise ValueError(f"取消目标动作不存在: {target_action_id}")
        kind, status = row
        if kind in _CAPTURE_TYPES:
            return self._settle_capture(target_action_id, status)
        if kind == _OBTAIN_TYPE:
            return await self._settle_obtain(target_action_id)
        if kind == _CLEANUP_TYPE:
            return self._settle_cleanup(target_action_id)
        if kind == _CANCEL_TYPE:
            return await self._settle_cancel_action(target_action_id)
        if kind == 8:
            from camctl.persistence.repositories.motor import read_motor_facts
            facts = read_motor_facts(self._owned.connection, target_action_id)
            return SettlementOutcome(complete=facts.action["status"] in _ACTION_TERMINAL)
        if kind == _REPORT_TYPE:
            # 报告动作的同步责任分类由资格与生效承担；共享生成
            # 不属于目标范围，本次有限处理到此完成。
            return SettlementOutcome(complete=True)
        raise ValueError(f"取消目标类型不可解释: {target_action_id} {kind!r}")

    def _settle_capture(self, target_action_id: int, status: int) -> SettlementOutcome:
        """目标终态结束等待；本次必要停止的原结果决定取消是否成功。"""
        if status not in _ACTION_TERMINAL:
            return SettlementOutcome(complete=False)
        connection = self._owned.connection
        action = row_facts(connection, "actions", target_action_id)
        if not action["cancel_requested"]:
            return SettlementOutcome(complete=True)
        facts = load_start_facts(connection, action)
        with closing(connection.execute(
            "SELECT kind, action_id, activity_id, status FROM operation_runs"
            " WHERE responsibility_key = ?", (f"stop/{target_action_id}",),
        )) as cursor:
            stop = cursor.fetchone()
        if stop is not None:
            if (facts.activity is None or stop[:3] !=
                    (int(_KIND.STOP), target_action_id, facts.activity["id"])):
                raise ConsistencyError("取消收场的停止责任与原活动不符")
            if stop[3] in (int(_RUN.PENDING), int(_RUN.ACTIVE)):
                return SettlementOutcome(complete=False)
            if stop[3] in (int(_RUN.FAILED), int(_RUN.UNCONFIRMED)):
                # 后续新观察不覆盖本次有限处理已经保存的失败或未知结论。
                return SettlementOutcome(complete=True, failed=True)
            if stop[3] == int(_RUN.SUCCEEDED):
                return SettlementOutcome(complete=True)
        if facts.not_started or (facts.activity is not None and facts.activity["activity_state"] == 3):
            return SettlementOutcome(complete=True)
        raise ConsistencyError("取消拍摄终态缺少可靠无需停止或停止结束依据")

    async def _settle_obtain(self, target_action_id: int) -> SettlementOutcome:
        """取回目标：推进 ready 交付撤回，位置未知先请求并等待。"""
        from camctl.outputs.handoff import (
            AdvanceWithdrawal, WithdrawalChoice)

        pending = self._pending_withdrawal_items(target_action_id)
        if not pending:
            return SettlementOutcome(complete=True)
        connection = self._owned.connection
        waiting = False
        for delivery_id in pending:
            position = self._withdrawal_positions(delivery_id)
            state = connection.execute(
                "SELECT withdrawal_state FROM deliveries WHERE id = ?",
                (delivery_id,)).fetchone()
            if state is not None and state[0] == _WITHDRAWAL_NOT_REQUESTED:
                # 撤回责任先进入待执行，再按可靠位置观察保存结果。
                self._advance(AdvanceWithdrawal(
                    delivery_id, WithdrawalChoice.REQUESTED,
                    self._occurred_at()))
            if position == "ready":
                choice = WithdrawalChoice.WITHDRAWN
            elif position == "processing":
                choice = WithdrawalChoice.NOT_RETRACTABLE
            else:
                waiting = True
                continue
            self._advance(AdvanceWithdrawal(
                delivery_id, choice, self._occurred_at()))
        if waiting:
            return SettlementOutcome(complete=False)
        statuses = {
            row[0] for row in connection.execute(
                "SELECT cdi.status FROM cancel_delivery_items cdi"
                " JOIN deliveries d ON d.id = cdi.delivery_id"
                " WHERE d.action_id = ?", (target_action_id,))}
        if _DELIVERY_ITEM_PENDING in statuses:
            return SettlementOutcome(complete=False)
        return SettlementOutcome(
            complete=True,
            failed=_DELIVERY_ITEM_FAILED in statuses)

    def _settle_cleanup(self, target_action_id: int) -> SettlementOutcome:
        """清理目标：未发出删除的成员解除限制，删除中等待执行链。

        成员全部终态（含目标集合未固定的零成员）后把目标动作终态
        化为取消，取消动作的完成依据按终态化后的目标状态判定；目
        标已终态时直接按成员事实返回，重入幂等。
        """
        from camctl.outputs.cleanup_flow import (
            CancelCleanupItem, FinishCanceledCleanupAction)

        connection = self._owned.connection
        action_row = connection.execute(
            "SELECT status FROM actions WHERE id = ?",
            (target_action_id,)).fetchone()
        if action_row is None:
            raise ValueError(f"取消目标动作不存在: {target_action_id}")
        with closing(connection.execute(
            "SELECT id, status FROM cleanup_items WHERE action_id = ?",
            (target_action_id,),
        )) as cursor:
            items = tuple(cursor.fetchall())
        for item_id, status in items:
            if status in (_CLEANUP_UNRESOLVED, _CLEANUP_PENDING):
                outcome = self._outputs.cancel_cleanup_item(
                    CancelCleanupItem(item_id, self._occurred_at()),
                    new_operation_key(), self._owned)
                if outcome.kind is not DbOutcomeKind.COMPLETED:
                    raise ValueError(f"清理取消事务未提交: {outcome.error}")
        statuses = {status for _, status in items}
        if int(action_row[0]) not in _ACTION_TERMINAL:
            if _CLEANUP_DELETING in statuses:
                return SettlementOutcome(complete=False)
            finished = self._outputs.finish_canceled_cleanup(
                FinishCanceledCleanupAction(
                    action_id=target_action_id,
                    occurred_at=self._occurred_at()),
                new_operation_key(), self._owned)
            if finished.kind is not DbOutcomeKind.COMPLETED:
                raise ValueError(
                    f"清理取消终态化事务未提交: {finished.error}")
        return SettlementOutcome(
            complete=True, failed=_CLEANUP_FAILED in statuses)

    async def _settle_cancel_action(
            self, target_action_id: int) -> SettlementOutcome:
        outcome = await settle_origin_cancel(
            OriginSettle(origin_action_id=target_action_id), self._runtime())
        if outcome.kind is not DbOutcomeKind.COMPLETED:
            raise ValueError(f"发起者收场未提交: {outcome.error}")
        return SettlementOutcome(complete=True)

    def _advance(self, command) -> None:
        outcome = self._outputs.advance_withdrawal(
            command, new_operation_key(), self._owned)
        if outcome.kind is not DbOutcomeKind.COMPLETED:
            raise ValueError(f"撤回推进事务未提交: {outcome.error}")

    def _pending_withdrawal_items(self, target_action_id: int) -> tuple[int, ...]:
        connection = self._owned.connection
        with closing(connection.execute(
            "SELECT cdi.delivery_id FROM cancel_delivery_items cdi"
            " JOIN deliveries d ON d.id = cdi.delivery_id"
            " WHERE d.action_id = ? AND cdi.status = ?",
            (target_action_id, _DELIVERY_ITEM_PENDING),
        )) as cursor:
            return tuple(int(row[0]) for row in cursor.fetchall())

    def _runtime(self):
        from camctl.cancellation.service import CancellationRuntime

        return CancellationRuntime(
            owned=self._owned, repository=self._cancellations,
            settlement=self, occurred_at=self._occurred_at)

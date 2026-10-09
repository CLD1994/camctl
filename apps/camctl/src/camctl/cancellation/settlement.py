"""按真实目标类型组织的取消收场端口实现。

拍摄目标等待执行链的停止收场（不越权代停止）；取回目标推进交付撤
回（ready 实际撤回、processing 不可撤回、未知保存独立有限结果）；
清理目标对未发出删除的成员解除限制，删除中的成员由清理执行链收
场；取消动作目标转发发起者收场；报告目标的本次责任分类完成，共
享生成不在目标范围。
"""

from __future__ import annotations

from contextlib import closing
from typing import Any, Awaitable, Callable

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
_CLEANUP_SUCCEEDED, _CLEANUP_FAILED, _CLEANUP_CANCELED = 4, 5, 6
#: cancel_delivery_items.status：PENDING/WITHDRAWN/NOT_RETRACTABLE/FAILED。
_DELIVERY_ITEM_PENDING, _DELIVERY_ITEM_WITHDRAWN = 1, 2
_DELIVERY_ITEM_NOT_RETRACTABLE, _DELIVERY_ITEM_FAILED = 3, 4
_RUN = enum_for("operation_runs.status")
_KIND = enum_for("operation_runs.kind")


class TargetSettlement:
    """目标拥有者收场端口的真实实现；按目标动作类型分派。"""

    def __init__(self, owned: Any, outputs: Any, cancellations: Any,
                 withdrawal_positions: Callable[[int], str],
                 occurred_at: Callable[[], int], work_files: Any = None,
                 withdrawal_execute: Callable[[int], Awaitable[bool]] | None = None,
                 withdrawal_resume: Callable[[int], Awaitable[bool]] | None = None) -> None:
        self._owned = owned
        self._outputs = outputs
        self._cancellations = cancellations
        self._occurred_at = occurred_at
        self._work_files = work_files
        self._withdrawal_execute = withdrawal_execute
        self._withdrawal_resume = withdrawal_resume

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
            from camctl.reporting.policy import finish_canceled_sync_action

            finished = finish_canceled_sync_action(
                new_operation_key(), self._owned,
                action_id=target_action_id, occurred_at=self._occurred_at())
            if finished.kind is not DbOutcomeKind.COMPLETED:
                raise ValueError(f"报告取消收场事务未提交: {finished.error}")
            return SettlementOutcome(complete=True)
        raise ValueError(f"取消目标类型不可解释: {target_action_id} {kind!r}")

    def _settle_capture(self, target_action_id: int, status: int) -> SettlementOutcome:
        """分别核对必要停止及延时文件核实，采用各自原处理结论。"""
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
        failed = False
        if stop is not None:
            if (facts.activity is None or stop[:3] !=
                    (int(_KIND.STOP), target_action_id, facts.activity["id"])):
                raise ConsistencyError("取消收场的停止责任与原活动不符")
            if stop[3] in (int(_RUN.PENDING), int(_RUN.ACTIVE)):
                return SettlementOutcome(complete=False)
            if stop[3] in (int(_RUN.FAILED), int(_RUN.UNCONFIRMED)):
                # 后续新观察不覆盖本次有限处理已经保存的失败或未知结论。
                failed = True
            elif stop[3] != int(_RUN.SUCCEEDED):
                if not (facts.not_started or facts.activity["activity_state"] == 3):
                    raise ConsistencyError("取消拍摄终态缺少可靠无需停止或停止结束依据")
        elif not (facts.not_started or (facts.activity is not None and facts.activity["activity_state"] == 3)):
            raise ConsistencyError("取消拍摄终态缺少可靠无需停止或停止结束依据")
        if action["type"] == 2:
            with closing(connection.execute(
                "SELECT id,kind,action_id,activity_id,status FROM operation_runs"
                " WHERE action_id=? AND kind=? ORDER BY id",
                (target_action_id, int(_KIND.CHECK_CAPTURE_RESULTS)),
            )) as cursor:
                rows = cursor.fetchall()
            if rows:
                if (len(rows) != 1 or facts.activity is None or rows[0][1:4] !=
                        (int(_KIND.CHECK_CAPTURE_RESULTS), target_action_id, facts.activity["id"])):
                    raise ConsistencyError("录像取消的原核实责任与活动不符")
                run_id, _kind, _action, _activity, result_status = rows[0]
                if result_status in (int(_RUN.PENDING), int(_RUN.ACTIVE)):
                    return SettlementOutcome(complete=False)
                with closing(connection.execute(
                    "SELECT 1 FROM operation_attempts WHERE run_id=?"
                    " AND (status=1 OR result_json IS NULL) LIMIT 1", (run_id,),
                )) as cursor:
                    if cursor.fetchone() is not None:
                        return SettlementOutcome(complete=False)
        if action["type"] == 3 and not facts.not_started:
            if facts.activity is None:
                raise ConsistencyError("取消延时摄影缺少原活动及必要文件核实责任")
            with closing(connection.execute(
                "SELECT kind, action_id, activity_id, status FROM operation_runs"
                " WHERE responsibility_key = ?", (f"results/{facts.activity['id']}",),
            )) as cursor:
                results = cursor.fetchone()
            if (results is None or results[:3] !=
                    (int(_KIND.CHECK_CAPTURE_RESULTS), target_action_id, facts.activity["id"])):
                raise ConsistencyError("取消延时摄影的文件核实责任与原活动不符")
            if results[3] in (int(_RUN.PENDING), int(_RUN.ACTIVE)):
                return SettlementOutcome(complete=False)
            failed = failed or results[3] in (int(_RUN.FAILED), int(_RUN.UNCONFIRMED))
        return SettlementOutcome(complete=True, failed=failed)

    async def _settle_obtain(self, target_action_id: int) -> SettlementOutcome:
        """分别消费未发布副本首清与已发布交付的实际撤回责任。"""

        connection = self._owned.connection
        first = SettlementOutcome(complete=True)
        if self._withdrawal_resume is not None and not await self._withdrawal_resume(target_action_id):
            return SettlementOutcome(complete=False)
        if self._work_files is not None:
            # 实际资格仅补充本次在办文件任务；原 READ 是否可靠结束
            # 仍由仓储的持久化原尝试决定，不能以动作终态替代。
            with closing(connection.execute(
                "SELECT f.id FROM intermediate_files f JOIN deliveries d ON d.id=f.owner_delivery_id"
                " WHERE d.action_id=? AND d.status IN (1,2,3,4,6,7)", (target_action_id,),
            )) as cursor:
                file_ids = tuple(int(row[0]) for row in cursor.fetchall())
            if set(file_ids) & set(self._work_files.executor.unfinished_files()):
                return SettlementOutcome(complete=False)
            from camctl.persistence.repositories.outputs import SettleCanceledObtain
            with closing(connection.execute(
                "SELECT i.id FROM obtain_items i JOIN obtain_source_selections s ON s.id=i.selection_id"
                " JOIN action_dependencies d ON d.id=s.dependency_id"
                " WHERE d.action_id=? ORDER BY i.id", (target_action_id,),
            )) as cursor:
                item_ids = tuple(int(row[0]) for row in cursor.fetchall())
            result = self._outputs.settle_canceled_obtain(
                SettleCanceledObtain(target_action_id, self._occurred_at(), item_ids),
                new_operation_key(), self._owned)
            if result.kind is not DbOutcomeKind.COMPLETED:
                raise ConsistencyError(f"取消取回的完整收场事务未完成: {result.error}")
            if not result.value.complete:
                return SettlementOutcome(complete=False)
            first = await self._work_files.first_cleanup(
                result.value.file_ids, owned=self._owned, occurred_at=self._occurred_at())
            if not first.complete:
                return first
        pending = self._pending_withdrawal_items(target_action_id)
        waiting = False
        if pending and self._withdrawal_execute is None:
            raise TypeError("交付撤回未装配实际文件操作及原保存协作者")
        for delivery_id in pending:
            waiting = not await self._withdrawal_execute(delivery_id) or waiting
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
            failed=first.failed or _DELIVERY_ITEM_FAILED in statuses)

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
            raise ConsistencyError(f"取消目标动作不存在: {target_action_id}")
        with closing(connection.execute(
            "SELECT id, status, error_code FROM cleanup_items WHERE action_id = ?",
            (target_action_id,),
        )) as cursor:
            items = tuple(cursor.fetchall())
        for item_id, status, _error in items:
            if status in (_CLEANUP_UNRESOLVED, _CLEANUP_PENDING):
                outcome = self._outputs.cancel_cleanup_item(
                    CancelCleanupItem(item_id, self._occurred_at()),
                    new_operation_key(), self._owned)
                if outcome.kind is not DbOutcomeKind.COMPLETED:
                    raise ConsistencyError(f"清理取消事务未提交: {outcome.error}")
        statuses = {status for _, status, _error in items}
        if int(action_row[0]) not in _ACTION_TERMINAL:
            if _CLEANUP_DELETING in statuses:
                return SettlementOutcome(complete=False)
            finished = self._outputs.finish_canceled_cleanup(
                FinishCanceledCleanupAction(
                    action_id=target_action_id,
                    occurred_at=self._occurred_at()),
                new_operation_key(), self._owned)
            if finished.kind is not DbOutcomeKind.COMPLETED:
                raise ConsistencyError(
                    f"清理取消终态化事务未提交: {finished.error}")
        return SettlementOutcome(
            complete=True,
            failed=any(status == _CLEANUP_FAILED or (status == _CLEANUP_CANCELED and error is not None)
                       for _, status, error in items))

    async def _settle_cancel_action(
            self, target_action_id: int) -> SettlementOutcome:
        outcome = await settle_origin_cancel(
            OriginSettle(origin_action_id=target_action_id), self._runtime())
        if outcome.kind is not DbOutcomeKind.COMPLETED:
            raise ValueError(f"发起者收场未提交: {outcome.error}")
        return SettlementOutcome(complete=True)

    def _pending_withdrawal_items(self, target_action_id: int) -> tuple[int, ...]:
        connection = self._owned.connection
        with closing(connection.execute(
            "SELECT DISTINCT cdi.delivery_id FROM cancel_delivery_items cdi"
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

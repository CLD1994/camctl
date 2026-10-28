"""按真实目标类型组织的取消收场端口实现。

拍摄目标等待执行链的停止收场（不越权代停止）；取回目标推进交付撤
回（ready 实际撤回、processing 不可撤回、未知保存独立有限结果）；
清理目标对未发出删除的成员解除限制，删除中的成员由清理执行链收
场；取消动作目标转发发起者收场；报告目标的本次责任分类完成，共
享生成不在目标范围。
"""

from __future__ import annotations

from camctl.persistence.models import db_failure

from contextlib import closing
from typing import Any, Awaitable, Callable

from camctl.cancellation.service import OriginSettle
from camctl.cancellation.ports import SettlementOutcome
from camctl.cancellation.service import settle_origin_cancel
from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError, new_operation_key
from camctl.persistence.business import BusinessDatabase, settle_required
from camctl.persistence.models import DbOutcomeKind
from camctl.persistence.repositories.capture_facts import load_start_facts
from camctl.persistence.transaction import row_facts

__all__ = ["TargetSettlement"]

#: 动作类型与终态集合。
_CAPTURE_TYPES = frozenset({1, 2, 3})
_ACTION_TERMINAL = (3, 4, 5, 6)
_OBTAIN_TYPE, _CLEANUP_TYPE, _CANCEL_TYPE, _REPORT_TYPE = 4, 5, 6, 7
#: cleanup_items.status：UNRESOLVED/PENDING_DELETE/DELETING/SUCCEEDED/FAILED。
_CLEANUP_UNRESOLVED, _CLEANUP_PENDING, _CLEANUP_DELETING = 1, 2, 3
_CLEANUP_SUCCEEDED, _CLEANUP_FAILED, _CLEANUP_CANCELED = 4, 5, 6
#: cancel_delivery_items.status：PENDING/WITHDRAWN/NOT_RETRACTABLE/FAILED。
_DELIVERY_ITEM_PENDING, _DELIVERY_ITEM_WITHDRAWN = 1, 2
_DELIVERY_ITEM_NOT_RETRACTABLE, _DELIVERY_ITEM_FAILED = 3, 4
_ACTIVE_WITHDRAWAL_PARENT = (
    " JOIN cancel_items ci ON ci.id=cdi.cancel_item_id"
    " JOIN actions origin ON origin.id=ci.action_id"
    " WHERE ci.status=2 AND origin.status=2 AND origin.cancel_requested=0")
_RUN = enum_for("operation_runs.status")
_KIND = enum_for("operation_runs.kind")
_ATTEMPT = enum_for("operation_attempts.status")


class TargetSettlement:
    """目标拥有者收场端口的真实实现；按目标动作类型分派。"""

    def __init__(self, database: BusinessDatabase, outputs: Any,
                 cancellations: Any,
                 withdrawal_positions: Callable[[int], Awaitable[str]],
                 occurred_at: Callable[[], int], work_files: Any = None,
                 withdrawal_execute: Callable[[int], Awaitable[bool]] | None = None,
                 withdrawal_resume: Callable[[int], Awaitable[bool]] | None = None,
                 cleanup_pending: Callable[
                     [BusinessDatabase, int], Awaitable[bool]] | None = None,
                 preparation_pending: Callable[[int], bool] | None = None,
                 availability_owner: Any = None,
                 pending_origin_settlements: dict | None = None) -> None:
        self._database = database
        self._outputs = outputs
        self._cancellations = cancellations
        self._occurred_at = occurred_at
        self._work_files = work_files
        self._withdrawal_execute = withdrawal_execute
        self._withdrawal_resume = withdrawal_resume
        self._cleanup_pending = cleanup_pending
        self._preparation_pending = preparation_pending
        self._availability_owner = availability_owner
        self._pending_origin_settlements = (
            {} if pending_origin_settlements is None
            else pending_origin_settlements)

    async def settle(self, target_action_id: int) -> SettlementOutcome:
        """按动作类型推进本次取消所需的有限收场。"""
        if self._availability_owner is not None and self._availability_owner.pending(target_action_id):
            self._availability_owner.cancel_action(target_action_id)
            return SettlementOutcome(complete=False)
        row = await self._database.read(
            lambda owned: _fetchone(
                owned.connection,
                "SELECT type, status FROM actions WHERE id = ?",
                (target_action_id,)),
            description="读取取消目标类型与状态",
        )
        if row is None:
            raise ValueError(f"取消目标动作不存在: {target_action_id}")
        kind, status = row
        if kind in _CAPTURE_TYPES or kind == _OBTAIN_TYPE:
            pending = await self._database.read(
                lambda owned: _availability_attempt_pending(owned.connection, target_action_id),
                description="读取设备检查取消收场事实")
            if pending:
                return SettlementOutcome(complete=False)
        if kind in _CAPTURE_TYPES:
            return await self._settle_capture(target_action_id, status)
        if kind == _OBTAIN_TYPE:
            return await self._settle_obtain(target_action_id)
        if kind == _CLEANUP_TYPE:
            return await self._settle_cleanup(target_action_id)
        if kind == _CANCEL_TYPE:
            return await self._settle_cancel_action(target_action_id)
        if kind == 8:
            from camctl.persistence.repositories.motor import read_motor_facts

            status = await self._database.read(
                lambda owned: read_motor_facts(
                    owned.connection, target_action_id).action["status"],
                description="读取电机取消目标状态",
            )
            return SettlementOutcome(complete=status in _ACTION_TERMINAL)
        if kind == _REPORT_TYPE:
            from camctl.reporting.policy import finish_canceled_sync_action

            key = new_operation_key()
            occurred_at = self._occurred_at()

            async def finish_and_classify():
                finished = await self._database.write(
                    lambda owned: finish_canceled_sync_action(
                        key, owned, action_id=target_action_id,
                        occurred_at=occurred_at),
                    key=key,
                    description="保存取消报告动作结果",
                )
                if finished.kind is not DbOutcomeKind.COMPLETED:
                    raise ValueError(
                        f"报告取消收场事务未提交: {finished.error}")

            await settle_required(finish_and_classify())
            return SettlementOutcome(complete=True)
        raise ValueError(f"取消目标类型不可解释: {target_action_id} {kind!r}")

    async def _settle_capture(
            self, target_action_id: int, status: int) -> SettlementOutcome:
        """分别核对必要停止及延时文件核实，采用各自原处理结论。"""
        if (status not in _ACTION_TERMINAL or self._preparation_pending is not None
                and self._preparation_pending(target_action_id)):
            return SettlementOutcome(complete=False)

        def read(owned):
            connection = owned.connection
            action = row_facts(connection, "actions", target_action_id)
            facts = load_start_facts(connection, action)
            if (not facts.preparation_resolved or facts.not_started
                    and facts.activity is not None
                    and facts.activity["occupancy_state"] == 1):
                return SettlementOutcome(complete=False)
            if not action["cancel_requested"]:
                return SettlementOutcome(complete=True)
            stop = _fetchone(
                connection,
                "SELECT kind, action_id, activity_id, status FROM operation_runs"
                " WHERE responsibility_key = ?", (f"stop/{target_action_id}",))
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
                    if not (facts.not_started or
                            facts.activity["activity_state"] == 3):
                        raise ConsistencyError(
                            "取消拍摄终态缺少可靠无需停止或停止结束依据")
            elif not (facts.not_started or (facts.activity is not None and
                    facts.activity["activity_state"] == 3)):
                raise ConsistencyError(
                    "取消拍摄终态缺少可靠无需停止或停止结束依据")
            if action["type"] == 2:
                rows = _fetchall(
                    connection,
                    "SELECT id,kind,action_id,activity_id,status FROM operation_runs"
                    " WHERE action_id=? AND kind=? ORDER BY id",
                    (target_action_id, int(_KIND.CHECK_CAPTURE_RESULTS)))
                if rows:
                    if (len(rows) != 1 or facts.activity is None or rows[0][1:4] !=
                            (int(_KIND.CHECK_CAPTURE_RESULTS), target_action_id,
                             facts.activity["id"])):
                        raise ConsistencyError(
                            "录像取消的原核实责任与活动不符")
                    run_id, _kind, _action, _activity, result_status = rows[0]
                    if result_status in (int(_RUN.PENDING), int(_RUN.ACTIVE)):
                        return SettlementOutcome(complete=False)
                    unresolved = _fetchone(
                        connection,
                        "SELECT 1 FROM operation_attempts WHERE run_id=?"
                        " AND (status=1 OR result_json IS NULL) LIMIT 1", (run_id,))
                    if unresolved is not None:
                        return SettlementOutcome(complete=False)
            if action["type"] == 3 and not facts.not_started:
                if facts.activity is None:
                    raise ConsistencyError(
                        "取消延时摄影缺少原活动及必要文件核实责任")
                results = _fetchone(
                    connection,
                    "SELECT kind, action_id, activity_id, status FROM operation_runs"
                    " WHERE responsibility_key = ?",
                    (f"results/{facts.activity['id']}",))
                if (results is None or results[:3] !=
                        (int(_KIND.CHECK_CAPTURE_RESULTS), target_action_id,
                         facts.activity["id"])):
                    raise ConsistencyError(
                        "取消延时摄影的文件核实责任与原活动不符")
                if results[3] in (int(_RUN.PENDING), int(_RUN.ACTIVE)):
                    return SettlementOutcome(complete=False)
                failed = failed or results[3] in (
                    int(_RUN.FAILED), int(_RUN.UNCONFIRMED))
            return SettlementOutcome(complete=True, failed=failed)

        return await self._database.read(
            read, description="读取取消拍摄收场事实")

    async def _settle_obtain(self, target_action_id: int) -> SettlementOutcome:
        """分别消费未发布副本首清与已发布交付的实际撤回责任。"""
        database = self._database
        first = SettlementOutcome(complete=True)
        if (self._withdrawal_resume is not None
                and not await self._withdrawal_resume(target_action_id)):
            return SettlementOutcome(complete=False)
        if self._work_files is not None:
            # 资格查询和文件占用查询分处各自的拥有者。
            file_ids = await database.read(
                lambda owned: tuple(int(row[0]) for row in _fetchall(
                    owned.connection,
                    "SELECT f.id FROM intermediate_files f JOIN deliveries d"
                    " ON d.id=f.owner_delivery_id"
                    " WHERE d.action_id=? AND d.status IN (1,2,3,4,6,7)",
                    (target_action_id,))),
                description="读取取消取回的中间文件",
            )
            if set(file_ids) & set(self._work_files.executor.unfinished_files()):
                return SettlementOutcome(complete=False)
            item_ids = await database.read(
                lambda owned: tuple(int(row[0]) for row in _fetchall(
                    owned.connection,
                    "SELECT i.id FROM obtain_items i JOIN obtain_source_selections s"
                    " ON s.id=i.selection_id JOIN action_dependencies d"
                    " ON d.id=s.dependency_id WHERE d.action_id=? ORDER BY i.id",
                    (target_action_id,))),
                description="读取取消取回的项目集合",
            )
            from camctl.persistence.repositories.outputs import SettleCanceledObtain

            request = SettleCanceledObtain(
                target_action_id, self._occurred_at(), item_ids)
            key = new_operation_key()

            async def settle_and_start_cleanup():
                result = await database.write(
                    lambda owned: self._outputs.settle_canceled_obtain(
                        request, key, owned),
                    key=key,
                    description="保存取消取回收场结果",
                )
                if result.kind is not DbOutcomeKind.COMPLETED:
                    raise ConsistencyError(
                        f"取消取回的完整收场事务未完成: {result.error}")
                if not result.value.complete:
                    return result, SettlementOutcome(complete=True)
                first = await self._work_files.first_cleanup(
                    result.value.file_ids, database=database,
                    occurred_at=self._occurred_at())
                return result, first

            result, first = await settle_required(settle_and_start_cleanup())
            if not result.value.complete:
                return SettlementOutcome(complete=False)
            if not first.complete:
                return first
        pending = await self._pending_withdrawal_items(target_action_id)
        waiting = False
        if pending and self._withdrawal_execute is None:
            raise TypeError("交付撤回未装配实际文件操作及原保存协作者")
        for delivery_id in pending:
            waiting = not await self._withdrawal_execute(delivery_id) or waiting
        if waiting:
            return SettlementOutcome(complete=False)
        statuses = await database.read(
            lambda owned: frozenset(row[0] for row in _fetchall(
                owned.connection,
                "SELECT cdi.status FROM cancel_delivery_items cdi"
                " JOIN deliveries d ON d.id = cdi.delivery_id"
                + _ACTIVE_WITHDRAWAL_PARENT + " AND d.action_id = ?", (target_action_id,))),
            description="读取取消取回的交付撤回结果",
        )
        if _DELIVERY_ITEM_PENDING in statuses:
            return SettlementOutcome(complete=False)
        return SettlementOutcome(
            complete=True,
            failed=first.failed or _DELIVERY_ITEM_FAILED in statuses)

    async def _settle_cleanup(self, target_action_id: int) -> SettlementOutcome:
        """清理目标：未发出删除的成员解除限制，删除中等待执行链。

        成员全部终态（含目标集合未固定的零成员）后把目标动作终态
        化为取消，取消动作的完成依据按终态化后的目标状态判定；目
        标已终态时直接按成员事实返回，重入幂等。
        """
        from camctl.outputs.cleanup_flow import (
            CancelCleanupItem, FinishCanceledCleanupAction)
        from camctl.outputs.cleanup_discovery import cleanup_action_has_responsibility

        database = self._database

        def load_cleanup_facts(owned):
            connection = owned.connection
            action = _fetchone(
                connection, "SELECT status FROM actions WHERE id = ?",
                (target_action_id,))
            items = _fetchall(
                connection,
                "SELECT id, status, error_code FROM cleanup_items WHERE action_id = ?",
                (target_action_id,))
            return action, tuple(items)

        action_row, items = await database.read(
            load_cleanup_facts, description="读取取消清理收场事实")
        if action_row is None:
            raise ConsistencyError(f"取消目标动作不存在: {target_action_id}")
        if (self._cleanup_pending is not None
                and await self._cleanup_pending(database, target_action_id)):
            return SettlementOutcome(complete=False)
        for item_id, status, _error in items:
            if status in (_CLEANUP_UNRESOLVED, _CLEANUP_PENDING):
                request = CancelCleanupItem(item_id, self._occurred_at())
                key = new_operation_key()

                async def cancel_item():
                    outcome = await database.write(
                        lambda owned: self._outputs.cancel_cleanup_item(
                            request, key, owned),
                        key=key,
                        description="保存取消清理成员结果",
                    )
                    if outcome.kind is not DbOutcomeKind.COMPLETED:
                        raise ConsistencyError(
                            f"清理取消事务未提交: {outcome.error}")

                await settle_required(cancel_item())
        still_responsible = await database.read(
            lambda owned: cleanup_action_has_responsibility(
                owned.connection, target_action_id),
            description="核实取消清理剩余责任",
        )
        if still_responsible:
            return SettlementOutcome(complete=False)
        statuses = {status for _, status, _error in items}
        if int(action_row[0]) not in _ACTION_TERMINAL:
            if _CLEANUP_DELETING in statuses:
                return SettlementOutcome(complete=False)
            request = FinishCanceledCleanupAction(
                action_id=target_action_id,
                occurred_at=self._occurred_at())
            key = new_operation_key()

            async def finish_cleanup():
                finished = await database.write(
                    lambda owned: self._outputs.finish_canceled_cleanup(
                        request, key, owned),
                    key=key,
                    description="保存取消清理动作结果",
                )
                if finished.kind is not DbOutcomeKind.COMPLETED:
                    raise ConsistencyError(
                        f"清理取消终态化事务未提交: {finished.error}")

            await settle_required(finish_cleanup())
        return SettlementOutcome(
            complete=True,
            failed=any(status == _CLEANUP_FAILED or
                       (status == _CLEANUP_CANCELED and error is not None)
                       for _, status, error in items))

    async def _settle_cancel_action(
            self, target_action_id: int) -> SettlementOutcome:
        outcome = await settle_origin_cancel(
            OriginSettle(origin_action_id=target_action_id), self._runtime())
        if outcome.kind is not DbOutcomeKind.COMPLETED:
            raise db_failure(ValueError, f"发起者收场未提交: {outcome.error}", outcome)
        return SettlementOutcome(complete=True)

    async def _pending_withdrawal_items(
            self, target_action_id: int) -> tuple[int, ...]:
        return await self._database.read(
            lambda owned: tuple(int(row[0]) for row in _fetchall(
                owned.connection,
                "SELECT DISTINCT cdi.delivery_id FROM cancel_delivery_items cdi"
                " JOIN deliveries d ON d.id = cdi.delivery_id"
                + _ACTIVE_WITHDRAWAL_PARENT + " AND d.action_id = ? AND cdi.status = ?",
                (target_action_id, _DELIVERY_ITEM_PENDING))),
            description="读取待撤回交付",
        )

    def _runtime(self):
        from camctl.cancellation.service import CancellationRuntime

        return CancellationRuntime(
            database=self._database, repository=self._cancellations,
            settlement=self, occurred_at=self._occurred_at,
            pending_origin_settlements=self._pending_origin_settlements)


def _availability_attempt_pending(connection, action_id):
    return _fetchone(connection,
        "SELECT 1 FROM operation_runs r JOIN operation_attempts t ON t.run_id=r.id"
        " WHERE r.action_id=? AND r.kind=? AND (t.status=? OR t.result_event_id IS NULL"
        " OR t.result_json IS NULL) LIMIT 1",
        (action_id, int(_KIND.CHECK_DEVICE_AVAILABILITY), int(_ATTEMPT.RUNNING))) is not None


def _fetchone(connection, sql: str, parameters: tuple):
    with closing(connection.execute(sql, parameters)) as cursor:
        return cursor.fetchone()


def _fetchall(connection, sql: str, parameters: tuple):
    with closing(connection.execute(sql, parameters)) as cursor:
        return cursor.fetchall()

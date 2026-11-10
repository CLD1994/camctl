"""单次不透明数据库工作及其提交、回滚收场。"""

from dataclasses import dataclass
from enum import Enum
import sqlite3
from typing import Any, Callable

from camctl.logging_runtime.transaction_timing import TransactionPhase, db_phase


class TransactionError(ValueError):
    """数据库事务或写入事实违反约束。"""


@dataclass(frozen=True)
class WriteReceipt:
    """一次写事务的完成结果。"""

    kind: str
    result: Any = None
    error: BaseException | None = None
    boundary: Any = None
    change_set: Any = None
    secondary_errors: tuple[BaseException, ...] = ()


_WRITE_RECEIPT_ATTRIBUTE = "_camctl_write_receipt"


def write_receipt_for(error: BaseException) -> WriteReceipt | None:
    receipt = getattr(error, _WRITE_RECEIPT_ATTRIBUTE, None)
    return receipt if isinstance(receipt, WriteReceipt) else None


def _attach_write_receipt(error: BaseException, receipt: WriteReceipt) -> None:
    setattr(error, _WRITE_RECEIPT_ATTRIBUTE, receipt)


def _attach_control_receipt(control: BaseException, kind: str,
                            secondary_errors: tuple[BaseException, ...] = ()) -> None:
    unique_errors: list[BaseException] = []
    for error in secondary_errors:
        if error is control or any(error is saved for saved in unique_errors):
            continue
        unique_errors.append(error)
    _attach_write_receipt(control, WriteReceipt(
        kind=kind, error=control, secondary_errors=tuple(unique_errors)))


class _Phase(Enum):
    ACTIVE = "active"
    COMMITTING = "committing"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    UNKNOWN = "unknown"


class AtomicTransaction:
    """拥有一个已开始事务的提交与回滚，不解释工作内容。"""

    def __init__(self, connection):
        self.connection = connection
        self.phase = _Phase.ACTIVE
        self._exit_operation = None

    @property
    def committing(self):
        return self.phase is _Phase.COMMITTING or (
            self.phase is _Phase.UNKNOWN and self._exit_operation == "commit")

    @property
    def rollback_attempted(self):
        return self.phase is _Phase.ROLLED_BACK or (
            self.phase is _Phase.UNKNOWN and self._exit_operation == "rollback")

    def commit(self):
        if self.phase is _Phase.ACTIVE:
            self.prepare_commit()
        elif self.phase is not _Phase.COMMITTING:
            raise TransactionError("已收场的事务不能再次提交")
        try:
            with db_phase(TransactionPhase.COMMIT):
                self.connection.execute("COMMIT")
        except BaseException:
            self.phase = _Phase.UNKNOWN
            raise
        self.phase = _Phase.COMMITTED

    def prepare_commit(self):
        """标记提交收场已经开始，后续中断须保留未知结果。"""
        if self.phase is not _Phase.ACTIVE:
            raise TransactionError("只有活动事务可以开始提交")
        self._exit_operation = "commit"
        self.phase = _Phase.COMMITTING

    def rollback(self, before: Callable[[], None] | None = None):
        if self.phase is not _Phase.ACTIVE:
            raise TransactionError("只有活动事务可以回滚")
        self._exit_operation = "rollback"
        try:
            if before is not None:
                before()
            with db_phase(TransactionPhase.ROLLBACK):
                self.connection.execute("ROLLBACK")
        except BaseException:
            self.phase = _Phase.UNKNOWN
            raise
        self.phase = _Phase.ROLLED_BACK

    def rollback_error(self, error: BaseException, before: Callable[[], None] | None = None):
        try:
            self.rollback(before)
        except BaseException as rollback_error:
            if not isinstance(rollback_error, Exception):
                if isinstance(error, Exception):
                    _attach_control_receipt(rollback_error, "unknown", (error,))
                    raise rollback_error
                _attach_control_receipt(error, "unknown", (rollback_error,))
                raise error
            if not isinstance(error, Exception):
                _attach_control_receipt(error, "unknown", (rollback_error,))
                raise error
            combined = error if rollback_error is error else ExceptionGroup(
                "写事务操作及回滚均失败", [error, rollback_error])
            return WriteReceipt(kind="unknown", error=combined)
        return WriteReceipt(kind="rolled_back", error=error)

    def rollback_control(self, control: BaseException, before: Callable[[], None] | None = None):
        try:
            self.rollback(before)
        except BaseException as rollback_error:
            _attach_control_receipt(control, "unknown", (rollback_error,))
        else:
            _attach_control_receipt(control, "rolled_back")
        raise control


def execute_transaction(connection, work):
    """在原连接取得写事务并调用工作；工作只通过事务对象收场。"""
    try:
        with db_phase(TransactionPhase.BEGIN):
            connection.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as error:
        return WriteReceipt(kind="rolled_back", error=error)
    transaction = AtomicTransaction(connection)
    try:
        result = work(transaction)
        if transaction.phase in (_Phase.COMMITTING, _Phase.UNKNOWN):
            if isinstance(result, WriteReceipt) and result.kind == "unknown":
                transaction.phase = _Phase.UNKNOWN
                return result
            error = TransactionError("事务收场未确认，不能交付成功结果")
            _attach_write_receipt(error, WriteReceipt(kind="unknown", error=error))
            raise error
        if transaction.phase is _Phase.ACTIVE:
            error = TransactionError("事务工作未完成提交或回滚")
            receipt = transaction.rollback_error(error)
            _attach_write_receipt(error, receipt)
            raise error
        return result
    except BaseException as control:
        if transaction.phase in (_Phase.COMMITTED, _Phase.ROLLED_BACK):
            _attach_write_receipt(control, WriteReceipt(
                kind="completed" if transaction.phase is _Phase.COMMITTED else "rolled_back",
                error=control))
            raise
        if isinstance(control, Exception):
            if transaction.phase in (_Phase.COMMITTING, _Phase.UNKNOWN):
                if write_receipt_for(control) is None:
                    _attach_write_receipt(control, WriteReceipt(kind="unknown", error=control))
            elif transaction.phase is _Phase.ACTIVE:
                _attach_write_receipt(control, transaction.rollback_error(control))
            raise
        receipt = write_receipt_for(control)
        if receipt is not None and receipt.kind == "unknown":
            raise
        if transaction.phase in (_Phase.COMMITTING, _Phase.UNKNOWN):
            _attach_control_receipt(control, "unknown")
            raise
        if not transaction.rollback_attempted:
            transaction.rollback_control(control)
        raise

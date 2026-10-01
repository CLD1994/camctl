"""完整历史事务边界及其校验。

H、C、S 都用完整事务结束处的位置表达；初始化起点 (0, 0)
专门表示数据库初始化后的历史起点，不对应事件或事务记录。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Generic, TypeVar

from camctl.contracts.pages import Page
from camctl.contracts.values import MAX_OBJECT_ID

T = TypeVar("T")
C = TypeVar("C")


class BoundaryError(ValueError):
    """边界、事务范围或分页位置违反历史读取契约。"""


@dataclass(frozen=True)
class HistoryBoundary:
    """已经完成的完整历史事务边界。

    初始化起点为 (0, 0)；其他边界的事务标识与结束事件
    均为 1～2^63-1 的正整数。
    """

    txn_id: int
    last_event_id: int

    def __post_init__(self) -> None:
        for name in ("txn_id", "last_event_id"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise BoundaryError(f"{name} 必须是整数: {value!r}")
        if self.txn_id == 0 and self.last_event_id == 0:
            return
        if self.txn_id < 1 or self.last_event_id < 1:
            raise BoundaryError(
                f"边界要么是初始化起点 (0, 0)，要么两项均为正整数: {(self.txn_id, self.last_event_id)}"
            )
        if self.txn_id > MAX_OBJECT_ID or self.last_event_id > MAX_OBJECT_ID:
            raise BoundaryError(f"边界分量超出 2^63-1: {(self.txn_id, self.last_event_id)}")


#: 完整有效数据库初始化后的历史起点。
INITIAL_BOUNDARY = HistoryBoundary(txn_id=0, last_event_id=0)


@dataclass(frozen=True)
class TransactionRange:
    """一组共同提交的历史事件的范围。"""

    txn_id: int
    first_event_id: int
    last_event_id: int

    def __post_init__(self) -> None:
        for name in ("txn_id", "first_event_id", "last_event_id"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise BoundaryError(f"{name} 必须是正整数: {value!r}")
            if value > MAX_OBJECT_ID:
                raise BoundaryError(f"{name} 超出 2^63-1: {value}")
        if self.first_event_id > self.last_event_id:
            raise BoundaryError(
                f"首事件不能晚于末事件: {(self.first_event_id, self.last_event_id)}"
            )


def validate_boundary(boundary: HistoryBoundary, transaction: TransactionRange) -> None:
    """校验边界确实是该事务的完整结束位置。

    事务中间位置可用作事件分页位置，但不是完整状态查询或
    报告冻结边界；初始化起点不对应任何事务记录。
    """
    if boundary.txn_id == 0:
        raise BoundaryError("初始化起点不对应历史事务，不能与事务范围校验")
    if boundary.txn_id != transaction.txn_id:
        raise BoundaryError(
            f"边界事务 {boundary.txn_id} 与目标事务 {transaction.txn_id} 不一致"
        )
    if boundary.last_event_id != transaction.last_event_id:
        if transaction.first_event_id <= boundary.last_event_id < transaction.last_event_id:
            raise BoundaryError(
                f"事件 {boundary.last_event_id} 是事务 {transaction.txn_id} 的中间位置，不是完整边界"
            )
        raise BoundaryError(
            f"事件 {boundary.last_event_id} 不是事务 {transaction.txn_id} 的结束位置"
        )


class ReadOrder(Enum):
    """分页读取沿事件位置的排序方向。"""

    ASCENDING = "ascending"
    DESCENDING = "descending"


@dataclass(frozen=True)
class ReadScope(Generic[C]):
    """一次分页读取的固定范围与排序。

    具体游标结构由所属查询接口定义；``cursor_position``
    从游标提取可比较的事件位置，供推进方向与范围校验使用。
    """

    order: ReadOrder
    previous_position: int | None
    lower_position: int
    upper_position: int | None
    batch_limit: int
    cursor_position: Callable[[C], int]

    def __post_init__(self) -> None:
        if not isinstance(self.order, ReadOrder):
            raise BoundaryError(f"排序必须是 ReadOrder 成员: {self.order!r}")
        if not isinstance(self.batch_limit, int) or isinstance(self.batch_limit, bool) or self.batch_limit < 1:
            raise BoundaryError(f"批量上限必须是正整数: {self.batch_limit!r}")


def validate_page(page: Page[T, C], scope: ReadScope[C]) -> None:
    """按固定范围校验批次的继续位置与批量上限。

    继续位置必须沿排序方向严格推进并保持在固定范围内；
    结束批次不携带继续位置，跳过位置校验但同样受批量上限约束。
    """
    if len(page.items) > scope.batch_limit:
        raise BoundaryError(
            f"批次数量 {len(page.items)} 超过上限 {scope.batch_limit}"
        )
    cursor = page.next_cursor
    if cursor is None:
        return
    try:
        position = scope.cursor_position(cursor)
    except Exception as error:
        raise BoundaryError("游标结构不属于所属查询范围") from error
    if not isinstance(position, int) or isinstance(position, bool):
        raise BoundaryError(f"游标位置必须是整数: {position!r}")
    if scope.order is ReadOrder.ASCENDING:
        if scope.previous_position is not None and position <= scope.previous_position:
            raise BoundaryError(
                f"升序游标必须严格推进: {position} 未超过 {scope.previous_position}"
            )
    elif scope.order is ReadOrder.DESCENDING:
        if scope.previous_position is not None and position >= scope.previous_position:
            raise BoundaryError(
                f"降序游标必须严格减小: {position} 未小于 {scope.previous_position}"
            )
    else:
        raise BoundaryError(f"排序必须是 ReadOrder 成员: {scope.order!r}")
    if position < scope.lower_position:
        raise BoundaryError(f"游标位置低于固定范围下界: {position}")
    if scope.upper_position is not None and position > scope.upper_position:
        raise BoundaryError(f"游标位置超出固定范围上界: {position}")

"""统一候选排序：计划时间、类型、受理顺序及稳定身份。

排序只使用持久化的业务事实：所属动作的 scheduled_at、所属计划
的受理顺序（plans.id）、原 actions 数组位置及文件条目登记顺序。
请求受理顺序、数据库查询顺序、内存加载顺序及协程唤醒顺序不能
颠倒契约顺序。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, TypeVar

__all__ = [
    "ActionCandidate",
    "FileCandidate",
    "ProductCandidate",
    "ProductKind",
    "action_key",
    "file_key",
    "order_actions",
    "order_files",
    "order_products",
    "product_key",
]


@dataclass(frozen=True)
class ActionCandidate:
    """一个以动作身份参与的候选（如待启动录像）。"""

    action_id: int
    scheduled_at: int
    plan_id: int
    input_index: int


class ProductKind(Enum):
    """同一产物的资格候选类型；同时间取回优先于清理。"""

    OBTAIN = "obtain"
    DELETE = "delete"


@dataclass(frozen=True)
class ProductCandidate:
    """对同一已登记产物的取回或清理候选。"""

    action_id: int
    scheduled_at: int
    plan_id: int
    input_index: int
    kind: ProductKind


@dataclass(frozen=True)
class FileCandidate:
    """相机读取的文件候选：发起动作加文件条目登记顺序。"""

    action: ActionCandidate
    entry_index: int


def action_key(candidate: ActionCandidate) -> tuple[int, int, int]:
    """启动候选比较键：计划时间、受理计划、原数组位置。"""
    return (candidate.scheduled_at, candidate.plan_id, candidate.input_index)


def product_key(candidate: ProductCandidate) -> tuple[int, int, int, int]:
    """逐产物资格比较键：计划时间从早到晚，同时间取回优先。"""
    kind_rank = 0 if candidate.kind is ProductKind.OBTAIN else 1
    return (
        candidate.scheduled_at,
        kind_rank,
        candidate.plan_id,
        candidate.input_index,
    )


def file_key(candidate: FileCandidate) -> tuple[int, int, int, int]:
    """读取候选比较键：发起动作键加文件条目登记顺序。"""
    return (*action_key(candidate.action), candidate.entry_index)


T = TypeVar("T")


def _ordered(candidates: Iterable[T], key) -> list[T]:
    return sorted(candidates, key=key)


def order_actions(candidates: Iterable[ActionCandidate]) -> list[ActionCandidate]:
    """按契约顺序排列启动候选；结果与输入顺序无关。"""
    return _ordered(candidates, action_key)


def order_products(candidates: Iterable[ProductCandidate]) -> list[ProductCandidate]:
    """按契约顺序排列逐产物资格候选；同时间取回优先。"""
    return _ordered(candidates, product_key)


def order_files(candidates: Iterable[FileCandidate]) -> list[FileCandidate]:
    """按契约顺序排列读取候选；同一动作内按登记顺序。"""
    return _ordered(candidates, file_key)

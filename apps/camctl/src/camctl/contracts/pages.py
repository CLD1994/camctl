"""有界批次与继续位置的分页结果契约。

``Page`` 只保存本批数据和继续位置；结束属性由继续位置推导，
不接受构造参数或赋值。空批次配合有效继续位置仍可继续，
调用方不能以本批数据是否为空判断扫描结束。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")
C = TypeVar("C")


class PageError(ValueError):
    """批次构造违反分页结果契约。"""


@dataclass(frozen=True)
class Page(Generic[T, C]):
    """一次读取交付的有界批次及继续位置。

    ``next_cursor`` 为 ``None`` 表示读取方已可靠确认扫描范围结束；
    有效继续位置表示调用方还须继续检查，不保证下一批有有效结果。
    """

    items: tuple[T, ...]
    next_cursor: "C | None"

    def __post_init__(self) -> None:
        if self.items is None or isinstance(self.items, (str, bytes)):
            raise PageError("items 必须是数据序列")
        if not isinstance(self.items, tuple):
            object.__setattr__(self, "items", tuple(self.items))

    @property
    def exhausted(self) -> bool:
        """本批之后扫描是否已结束；等于继续位置为空。"""
        return self.next_cursor is None

"""写事务内可靠完成的正整数等值查询范围，不保存业务行副本。"""

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from camctl.contracts.values import ObjectId


@dataclass(frozen=True)
class BaselineRangeRead:
    """同一写事务读取的基准历史范围摘要，不缓存完整目录。

    追加只需首尾及上一批身份；固定读取完整连续范围并填入计数。
    complete 区分这两种读取，不能用局部摘要验证总计数。
    """

    first_event_id: int | None
    last_event_id: int | None
    chunk_count: int
    entry_count: int | None
    last_identity: tuple[str, str, str] | None
    complete: bool


@dataclass(frozen=True)
class ReadCoverage:
    """以 (表, 查询列) 对应的正整数值集合声明完整读取范围。

    生产者须在同一写事务中完成查询并提供全部匹配行，才可声明
    该范围。空范围不证明任何查询已执行；可靠空结果仍须登记所
    查询的值。主键、外键和正整数登记状态使用同一规则；带额外
    筛选条件的查询不能声明整个等值范围。内核推进当前行后，范围
    继续适用于这些当前行。
    """

    ranges: Mapping[tuple[str, str], frozenset[int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized = {
            key: frozenset(ObjectId(identity) for identity in identities)
            for key, identities in self.ranges.items()
        }
        object.__setattr__(self, "ranges", MappingProxyType(normalized))

    def covers(self, table: str, column: str, identity: int) -> bool:
        """该身份的等值查询是否完整；不从当前行或其他范围推断。"""
        return ObjectId(identity) in self.ranges.get((table, column), ())

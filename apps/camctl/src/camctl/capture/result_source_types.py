"""原结果读取所依据的来源类型。"""

from dataclasses import dataclass
from enum import StrEnum


# 沿用既有序列化路径；原仓储模块引用这两个相同的类型对象。
class ResultSourceKind(StrEnum):
    __module__ = 'camctl.persistence.repositories.result_sources'

    NATURAL = 'natural'
    STOP = 'stop'
    DIRECT = 'direct'
    UNCONFIRMED = 'unconfirmed'
    UNANCHORED = 'unanchored'


@dataclass(frozen=True)
class ResultSource:
    __module__ = 'camctl.persistence.repositories.result_sources'

    kind: ResultSourceKind
    intent_event_id: int
    context: dict | None = None
    natural_lower_bound: int | None = None
    requires_file_wait: bool = False

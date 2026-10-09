"""数据库执行的结果、任务与撤回分类。

执行完成状态与业务结果是两个维度：完成状态表达数据库事实是否
可靠保存，业务结果（包括可靠拒绝）由任务自身的返回值表达。
"""

from __future__ import annotations

import enum
from sqlite3 import Error as DatabaseAccessError
from dataclasses import dataclass
from typing import Any, Callable, Generic, TypeVar

from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.values import OperationKey
from camctl.persistence.runtime import DirectoryBindingError, OwnedConnection

# 数据访问及部署绑定错误作为持久化端口的结果类型公开。业务层
# 可以识别这些错误，不需要依赖 SQLite 或连接创建模块。

T = TypeVar("T")


class DbOutcomeKind(enum.Enum):
    """一次数据库操作的完成状态分区。"""

    COMPLETED = "completed"
    NOT_EXECUTED = "not_executed"
    ROLLED_BACK = "rolled_back"
    UNKNOWN = "unknown"


class DbReadError(Exception):
    """数据库读取失败；不以空批次或默认值代替。"""


class DbEnqueueTimeoutError(Exception):
    """入队等待超过时限；本次操作以后也不会执行。"""


class DbExecutorError(ValueError):
    """执行器接口使用违反契约（未知操作、错误种类或已关闭）。"""


@dataclass(frozen=True)
class DbOutcome(Generic[T]):
    """一次写操作的结果。

    value、error、boundary 及 stage 按完成状态分区适用：COMPLETED
    携带成功值与完整提交边界；NOT_EXECUTED 的 error 为入队超时等
    明确原因或为空；ROLLED_BACK 与 UNKNOWN 携带实际错误。
    """

    kind: DbOutcomeKind
    value: T | None = None
    error: BaseException | None = None
    boundary: HistoryBoundary | None = None
    stage: str | None = None


@dataclass(frozen=True)
class ReadReceipt(Generic[T]):
    """一次读操作的结果；读取错误与数据分别表达。"""

    value: T | None = None
    error: DbReadError | None = None
    boundary: HistoryBoundary | None = None


class DbJobKind(enum.Enum):
    READ = "read"
    WRITE = "write"


class DbPriority(enum.Enum):
    """业务操作优先于快照维护；同优先级按到达顺序执行。"""

    BUSINESS = "business"
    SNAPSHOT = "snapshot"


@dataclass(frozen=True)
class DbJob(Generic[T]):
    """一项完整的数据库操作。

    execute 在数据库线程上以所属连接执行：写任务返回 DbOutcome，
    读任务返回数据值。key 在入队前确定，用于撤回与提交核实。
    """

    key: OperationKey
    description: str
    kind: DbJobKind
    priority: DbPriority
    execute: Callable[[OwnedConnection], Any]


class WithdrawalStatus(enum.Enum):
    WITHDRAWN = "withdrawn"
    DISPATCHED = "dispatched"


@dataclass(frozen=True)
class WithdrawalResult:
    """撤回请求的唯一判定：已确认未执行，或已在执行中。"""

    key: OperationKey
    status: WithdrawalStatus

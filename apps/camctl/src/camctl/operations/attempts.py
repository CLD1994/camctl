"""普通尝试的意图组织与结束事务输入。

意图、身份、次数及采用配置可靠提交后才派发；结束结果与适用等待
或流程结束在完整结果事务中共同保存。业务重试及流程结束的取舍由
所属纯规则决定，本模块只承载类型化输入与派发资格判定。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum

from camctl.contracts.values import MAX_OBJECT_ID, UtcMicros
from camctl.devices.evidence import OPERATIONS
from camctl.operations.models import (
    AttemptStatus,
    AttemptTicket,
    ErrorValue,
    ValidatedOutcome,
)
from camctl.persistence.models import DbOutcome, DbOutcomeKind

__all__ = [
    "AttemptConfig",
    "AttemptConfigError",
    "AttemptFinish",
    "AttemptIntent",
    "AttemptTarget",
    "BeginAttemptResult",
    "BeginDisposition",
    "FinishAttemptResult",
    "FinishDisposition",
    "OperationKind",
    "QueryPurpose",
    "RefusalKind",
    "RunFinish",
    "RunOutcome",
    "RunStatus",
    "dispatch_decision",
    "query_responsibility_key",
    "responsibility_key",
    "seconds_from_json",
]


class OperationKind(Enum):
    """普通操作流程的种类；成员名与整数登记一致，值是责任键前缀。"""

    START = "start"
    STOP = "stop"
    READ_FILE = "read"
    DELETE_FILE = "delete"
    CHECK_FILE_EXISTS = "exists"
    QUERY_ACTIVITY = "query"
    CHECK_CAPTURE_RESULTS = "results"
    STOP_RESIDUAL = "followup"


class QueryPurpose(Enum):
    """状态查询的固定用途；值是责任键中的用途段。"""

    BEFORE_EXECUTION = "preflight"
    START_CONFIRMATION = "start"
    ACTIVITY_OBSERVATION = "activity"
    STOP_CONFIRMATION = "stop"
    RESIDUAL_STOP_CONFIRMATION = "residual"


class RunStatus(Enum):
    """操作流程状态；成员名与整数登记一致。"""

    PENDING = "pending"
    ACTIVE = "active"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    UNCONFIRMED = "unconfirmed"
    EXPIRED = "expired"


#: 以活动为目标的种类；其余种类按目标列互斥规则使用文件或查询目标。
_ACTIVITY_KINDS = frozenset(
    {OperationKind.START, OperationKind.STOP, OperationKind.CHECK_CAPTURE_RESULTS}
)


class AttemptConfigError(ValueError):
    """意图配置或目标组合违反登记的结构规则。"""


def _positive_int(value: object, name: str, *, minimum: int = 1) -> int:
    if (isinstance(value, bool) or not isinstance(value, int)
            or not minimum <= value <= MAX_OBJECT_ID):
        raise AttemptConfigError(f"{name} 必须是 {minimum}～{MAX_OBJECT_ID} 的整数: {value!r}")
    return value


def _utc_micros(value: object) -> UtcMicros:
    try:
        timestamp = UtcMicros(value)
    except ValueError as error:
        raise AttemptConfigError(f"occurred_at 必须是整数 UTC 微秒: {value!r}") from error
    if not -MAX_OBJECT_ID - 1 <= timestamp <= MAX_OBJECT_ID:
        raise AttemptConfigError(f"occurred_at 超出 SQLite 整数范围: {value!r}")
    return timestamp


def _seconds(value: object, name: str, *, allow_zero: bool) -> Decimal:
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, int) and not isinstance(value, bool):
        number = Decimal(value)
    elif isinstance(value, str):
        try:
            number = Decimal(value)
        except InvalidOperation as error:
            raise AttemptConfigError(f"{name} 必须是数值秒: {value!r}") from error
    else:
        raise AttemptConfigError(f"{name} 必须是数值秒: {value!r}")
    if not number.is_finite():
        raise AttemptConfigError(f"{name} 必须是有限数值: {value!r}")
    if number < 0 or (number == 0 and not allow_zero):
        raise AttemptConfigError(f"{name} 必须是{'非负' if allow_zero else '正'}秒数: {value!r}")
    return number


@dataclass(frozen=True)
class AttemptConfig:
    """本次尝试实际采用的预算与期限配置。

    timeout_s 为空表示主机本地操作没有应用层调用时限；
    retry_interval_s 为空表示不适用定时重试。kind 6/7 的流程要求
    两者齐全，由登记的结构约束与仓储共同校验。
    """

    max_attempts: int
    timeout_s: Decimal | None = None
    retry_interval_s: Decimal | None = None

    def __post_init__(self) -> None:
        _positive_int(self.max_attempts, "max_attempts")
        if self.timeout_s is not None:
            object.__setattr__(
                self, "timeout_s", _seconds(self.timeout_s, "timeout_s", allow_zero=False)
            )
        if self.retry_interval_s is not None:
            object.__setattr__(
                self,
                "retry_interval_s",
                _seconds(
                    self.retry_interval_s, "retry_interval_s", allow_zero=True
                ),
            )


@dataclass(frozen=True)
class AttemptTarget:
    """操作责任的目标引用；按种类恰好填写一列或全部为空。"""

    activity_id: int | None = None
    copy_id: int | None = None
    cleanup_item_id: int | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("activity_id", self.activity_id),
            ("copy_id", self.copy_id),
            ("cleanup_item_id", self.cleanup_item_id),
        ):
            if value is not None:
                _positive_int(value, name)


_KIND_TARGET_COLUMNS = {
    OperationKind.START: frozenset({"activity_id"}),
    OperationKind.STOP: frozenset({"activity_id"}),
    OperationKind.READ_FILE: frozenset({"copy_id"}),
    OperationKind.DELETE_FILE: frozenset({"cleanup_item_id"}),
    OperationKind.CHECK_FILE_EXISTS: frozenset({"cleanup_item_id"}),
    OperationKind.QUERY_ACTIVITY: None,  # 按用途分两类，见 _QUERY_PURPOSE_COLUMNS。
    OperationKind.CHECK_CAPTURE_RESULTS: frozenset({"activity_id"}),
    OperationKind.STOP_RESIDUAL: frozenset({"activity_id"}),
}

_QUERY_PURPOSE_COLUMNS = {
    QueryPurpose.BEFORE_EXECUTION: frozenset({"query_purpose"}),
    QueryPurpose.START_CONFIRMATION: frozenset({"query_purpose", "activity_id"}),
    QueryPurpose.ACTIVITY_OBSERVATION: frozenset({"query_purpose", "activity_id"}),
    QueryPurpose.STOP_CONFIRMATION: frozenset({"query_purpose", "activity_id"}),
    QueryPurpose.RESIDUAL_STOP_CONFIRMATION: frozenset({"query_purpose", "activity_id"}),
}


@dataclass(frozen=True)
class AttemptIntent:
    """一次普通设备调用前的意图及本次采用配置。"""

    operation: str
    action_id: int
    kind: OperationKind
    target: AttemptTarget
    query_purpose: QueryPurpose | None
    config: AttemptConfig
    occurred_at: int
    copy_round: int | None = None

    def __post_init__(self) -> None:
        _positive_int(self.action_id, "action_id")
        if not isinstance(self.operation, str) or self.operation not in OPERATIONS:
            raise AttemptConfigError(f"operation 必须是已登记的操作类别: {self.operation!r}")
        if not isinstance(self.kind, OperationKind):
            raise AttemptConfigError(f"kind 必须是 OperationKind: {self.kind!r}")
        if not isinstance(self.target, AttemptTarget):
            raise AttemptConfigError(f"target 必须是 AttemptTarget: {self.target!r}")
        if not isinstance(self.config, AttemptConfig):
            raise AttemptConfigError(f"config 必须是 AttemptConfig: {self.config!r}")
        _utc_micros(self.occurred_at)
        if self.kind is OperationKind.QUERY_ACTIVITY:
            if not isinstance(self.query_purpose, QueryPurpose):
                raise AttemptConfigError("查询意图必须填写 query_purpose")
            required = _QUERY_PURPOSE_COLUMNS[self.query_purpose]
        else:
            if self.query_purpose is not None:
                raise AttemptConfigError("只有查询意图填写 query_purpose")
            required = _KIND_TARGET_COLUMNS[self.kind]
        values = {
            "activity_id": self.target.activity_id,
            "copy_id": self.target.copy_id,
            "cleanup_item_id": self.target.cleanup_item_id,
            "query_purpose": self.query_purpose,
        }
        filled = {name for name, value in values.items() if value is not None}
        if filled != set(required):
            raise AttemptConfigError(
                f"{self.kind.value} 要求恰好填写 {sorted(required)}，"
                f"实际填写 {sorted(filled)}"
            )
        if self.kind is OperationKind.READ_FILE:
            _positive_int(self.copy_round, "copy_round")
        elif self.copy_round is not None:
            raise AttemptConfigError("只有读取尝试保存 copy_round")


def query_responsibility_key(
    action_id: int, purpose: QueryPurpose, activity_id: int | None
) -> str:
    """从查询固定身份生成规范责任键，不构造调用意图或事实时刻。"""
    _positive_int(action_id, "action_id")
    if not isinstance(purpose, QueryPurpose):
        raise AttemptConfigError("查询责任必须填写 QueryPurpose")
    if purpose is QueryPurpose.BEFORE_EXECUTION:
        if activity_id is not None:
            raise AttemptConfigError("执行前检查不得指向具体活动")
        return f"query/preflight/{action_id}"
    _positive_int(activity_id, "activity_id")
    return f"query/{purpose.value}/{action_id}/{activity_id}"


def responsibility_key(intent: AttemptIntent) -> str:
    """按登记格式推导操作责任键；ID 使用无前导零十进制表示。"""
    kind = intent.kind
    if kind is OperationKind.QUERY_ACTIVITY:
        assert intent.query_purpose is not None
        return query_responsibility_key(intent.action_id, intent.query_purpose, intent.target.activity_id)
    if kind in _ACTIVITY_KINDS:
        assert intent.target.activity_id is not None
        if kind is OperationKind.CHECK_CAPTURE_RESULTS:
            return f"results/{intent.target.activity_id}"
        return f"{kind.value}/{intent.action_id}"
    if kind is OperationKind.STOP_RESIDUAL:
        assert intent.target.activity_id is not None
        return f"followup/{intent.action_id}/{intent.target.activity_id}"
    if kind is OperationKind.READ_FILE:
        assert intent.target.copy_id is not None
        return f"read/{intent.target.copy_id}"
    assert intent.target.cleanup_item_id is not None
    return f"{kind.value}/{intent.target.cleanup_item_id}"


def ticket_target_id(intent: AttemptIntent) -> str | None:
    """观察身份核对使用的规范十进制目标；执行前检查为空。"""
    if intent.kind is OperationKind.QUERY_ACTIVITY:
        if intent.query_purpose is QueryPurpose.BEFORE_EXECUTION:
            return None
        return str(intent.target.activity_id)
    if intent.target.activity_id is not None:
        return str(intent.target.activity_id)
    if intent.target.copy_id is not None:
        return str(intent.target.copy_id)
    assert intent.target.cleanup_item_id is not None
    return str(intent.target.cleanup_item_id)


class BeginDisposition(Enum):
    """意图事务的可靠结果分区。"""

    GRANTED = "granted"
    REJECTED = "rejected"


@dataclass(frozen=True)
class BeginAttemptResult:
    """意图事务的返回：授予时携带票据，拒绝时携带可靠原因。"""

    disposition: BeginDisposition
    ticket: AttemptTicket | None = None
    reason: str | None = None


class FinishDisposition(Enum):
    """结束事务的可靠结果分区。"""

    SAVED = "saved"
    ALREADY_ENDED = "already_ended"


class RunOutcome(Enum):
    """流程最终结果的类型化名称。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    UNCONFIRMED = "unconfirmed"
    EXPIRED = "expired"


@dataclass(frozen=True)
class RunFinish:
    """与尝试结果共同提交的流程结束决定。

    FAILED 与 UNCONFIRMED 必须携带流程错误，其余最终结果不携带；
    该组合规则由操作事务守卫在写事务内统一保证。
    """

    status: RunOutcome
    error: ErrorValue | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, RunOutcome):
            raise AttemptConfigError(f"status 必须是 RunOutcome: {self.status!r}")


@dataclass(frozen=True)
class AttemptFinish:
    """一次结束事务的完整输入：票据、已校验结果及流程处置。

    retry_wait 与 run_finish 互斥：需要重试的流程保持执行中并建立
    重试间隔责任；两者都为空表示本次事务只保存尝试结果。
    occurred_at 是本次结束事实的事件时刻（微秒）。
    """

    ticket: AttemptTicket
    outcome: ValidatedOutcome
    occurred_at: int
    retry_wait: bool = False
    run_finish: RunFinish | None = None

    def __post_init__(self) -> None:
        _utc_micros(self.occurred_at)
        if self.retry_wait and self.run_finish is not None:
            raise AttemptConfigError("重试等待与流程结束不能同时提交")


@dataclass(frozen=True)
class FinishAttemptResult:
    """结束事务的返回：保存后的实际事实或原有终态。"""

    disposition: FinishDisposition
    attempt_status: AttemptStatus | None = None
    run_status: RunStatus | None = None


class RefusalKind(Enum):
    """意图未授予时驱动不得派发的可靠分区。"""

    NOT_EXECUTED = "not_executed"
    ROLLED_BACK = "rolled_back"
    UNKNOWN = "unknown"
    REJECTED = "rejected"


@dataclass(frozen=True)
class DispatchDecision:
    """派发资格判定：只有可靠授予的意图才携带票据。"""

    ticket: AttemptTicket | None
    refusal: RefusalKind | None

    @property
    def dispatched(self) -> bool:
        return self.ticket is not None


def dispatch_decision(outcome: DbOutcome[BeginAttemptResult]) -> DispatchDecision:
    """意图可靠提交后才允许派发；拒绝、回滚或结果未知均不派发。"""
    if outcome.kind is DbOutcomeKind.COMPLETED:
        result = outcome.value
        if result is not None and result.disposition is BeginDisposition.GRANTED:
            return DispatchDecision(ticket=result.ticket, refusal=None)
        return DispatchDecision(ticket=None, refusal=RefusalKind.REJECTED)
    refusal = {
        DbOutcomeKind.NOT_EXECUTED: RefusalKind.NOT_EXECUTED,
        DbOutcomeKind.ROLLED_BACK: RefusalKind.ROLLED_BACK,
        DbOutcomeKind.UNKNOWN: RefusalKind.UNKNOWN,
    }[outcome.kind]
    return DispatchDecision(ticket=None, refusal=refusal)


def seconds_from_json(raw: object) -> Decimal | None:
    """从数据库 JSON 数字文本恢复精确秒数；空值为空。"""
    if raw is None:
        return None
    if isinstance(raw, Decimal):
        return raw
    try:
        return Decimal(str(raw))
    except InvalidOperation as error:
        raise AttemptConfigError(f"保存的秒数不是数值: {raw!r}") from error

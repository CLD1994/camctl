"""电机位置精确适配、发送事实、事务请求与当前流程许可。"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from camctl.contracts.enums import enum_for
from camctl.contracts.values import ObjectId, OperationKey, UtcMicros
from camctl.scheduling.rules import ExpirationReason

from camctl.contracts.json_values import is_json_integer


def position_value(value) -> int:
    """把合法 JSON 数学整数无损转换为协议的有符号 32 位位置。"""
    if not is_json_integer(value) or not -(1 << 31) <= value < (1 << 31):
        raise ValueError("position 必须是有符号 32 位数学整数")
    return int(value)


SendOutcome = enum_for("motor_notifications.outcome")


@dataclass(frozen=True)
class SendFacts:
    """已保存的发送事实；未知结果没有可推断的字节数和错误号。"""

    notification_id: int
    action_id: int
    intent_at: int | None
    intent_operation_key: str | None
    outcome: SendOutcome
    written_bytes: int | None
    errno: int | None
    finished_at: int | None

    def __post_init__(self):
        ObjectId(self.notification_id)
        ObjectId(self.action_id)
        if not isinstance(self.outcome, SendOutcome):
            raise ValueError("发送状态必须属于专属枚举")
        if (self.intent_at is None) != (self.intent_operation_key is None):
            raise ValueError("发送意图时刻与操作身份必须共同存在")
        if self.intent_at is not None:
            UtcMicros(self.intent_at)
            if (
                not isinstance(self.intent_operation_key, str)
                or not self.intent_operation_key
            ):
                raise ValueError("发送意图缺少操作身份")
        elif self.outcome is not SendOutcome.NOT_SENT:
            raise ValueError("该发送状态要求先前意图")
        if self.outcome is SendOutcome.PENDING:
            if any(
                value is not None
                for value in (self.finished_at, self.written_bytes, self.errno)
            ):
                raise ValueError("未决意图不能携带发送结果")
        else:
            if self.finished_at is None:
                raise ValueError("已保存结论缺少事实时刻")
            UtcMicros(self.finished_at)
        if self.outcome in (SendOutcome.WRITTEN, SendOutcome.FAILED):
            if (
                not isinstance(self.written_bytes, int)
                or isinstance(self.written_bytes, bool)
                or self.written_bytes < 0
            ):
                raise ValueError("写入结果须有实际非负字节数")
            if self.outcome is SendOutcome.WRITTEN and (
                self.written_bytes == 0 or self.errno is not None
            ):
                raise ValueError("完整写入字节数须为正数且没有系统错误")
            if self.errno is not None and (
                not isinstance(self.errno, int)
                or isinstance(self.errno, bool)
                or self.errno <= 0
            ):
                raise ValueError("系统错误号须为实际正整数")
        elif self.written_bytes is not None or self.errno is not None:
            raise ValueError("未发送或未知结论不能补造写入事实")


@dataclass(frozen=True)
class MotorActionFacts:
    action: Mapping[str, Any]
    notification: SendFacts | None


@dataclass(frozen=True)
class SendPermit:
    """只交给首次意图事务成功提交的本地执行流程，不在恢复时重建。"""

    action_id: int
    notification_id: int
    operation_key: OperationKey


class PrepareOutcome(Enum):
    GRANTED = "granted"
    ALREADY = "already"
    REJECTED = "rejected"


@dataclass(frozen=True)
class PrepareSendRequest:
    action_id: int
    trusted_wall_now: int
    occurred_at: int


@dataclass(frozen=True)
class PrepareSendResult:
    outcome: PrepareOutcome
    permit: SendPermit | None = None
    reason: str | None = None


class MotorFinalKind(Enum):
    WRITTEN = "written"
    FAILED = "failed"
    UNCONFIRMED = "unconfirmed"
    EXPIRED = "expired"
    CHANNEL_UNAVAILABLE = "channel_unavailable"


@dataclass(frozen=True)
class FinishSendRequest:
    action_id: int
    occurred_at: int
    kind: MotorFinalKind
    permit: SendPermit | None = None
    written_bytes: int | None = None
    errno: int | None = None
    reason: str | None = None
    expiration_reason: ExpirationReason | None = None


@dataclass(frozen=True)
class FinishSendResult:
    status: int
    reused: bool = False

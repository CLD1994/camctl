"""相机读取机会的授予与释放类型。

机会归属由 ``file_copies.slot_device_id`` 表达：设备来源拷贝才占机会，
主机源拷贝不申请。授予在写事务内核对当前持有者、资格及候选排序，
无类型优先级地使用统一文件顺序；释放核对原责任和实际读取及重试
结束，不能凭迟到通知清空新归属。等待不消耗读取次数。
"""

from dataclasses import dataclass
from enum import Enum

from camctl.contracts.values import MAX_OBJECT_ID, ObjectId, UtcMicros

__all__ = [
    "SlotDecision",
    "SlotOutcome",
    "SlotRequest",
]


class SlotOutcome(Enum):
    """一次机会事务的结果分区。"""

    #: 已保存机会归属（slot 从空到源设备）。
    GRANTED = "granted"
    #: 本拷贝已持有机会，只读复用原责任，不重新竞争。
    HELD = "held"
    #: 已保存归属释放（slot 从源设备到空）。
    RELEASED = "released"
    #: 归属已为空，幂等只读返回，不写入。
    ALREADY_RELEASED = "already_released"
    #: 只读等待；reason 区分持有者、候选排序、重试间隔或时间。
    WAIT = "wait"
    #: 读取责任已结束或取消生效，不授予新的普通读取机会。
    FINISHED = "finished"


@dataclass(frozen=True)
class SlotDecision:
    """机会事务的确定结果；等待时 reason 说明当前阻挡事实。"""

    outcome: SlotOutcome
    reason: str | None = None


@dataclass(frozen=True)
class SlotRequest:
    """一次机会变化的申请；copy_id 定位已建档拷贝。"""

    copy_id: int
    occurred_at: int

    def __post_init__(self) -> None:
        ObjectId(self.copy_id)
        timestamp = UtcMicros(self.occurred_at)
        if not -MAX_OBJECT_ID - 1 <= timestamp <= MAX_OBJECT_ID:
            raise ValueError(f"事件时间超出 SQLite 整数微秒范围: {self.occurred_at!r}")

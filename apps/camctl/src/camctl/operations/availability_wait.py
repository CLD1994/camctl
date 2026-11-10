"""可用性检查等待终结的固定申请及格式解析。"""

from dataclasses import dataclass

from camctl.contracts.values import ConsistencyError, ObjectId, UtcMicros


@dataclass(frozen=True)
class WaitingFinishRequest:
    check_id: int
    trusted_wall_now: int
    occurred_at: int
    max_attempts: int | None
    binding_details: dict | None = None
    current_identity: dict | None = None

    def __post_init__(self):
        ObjectId(self.check_id)
        UtcMicros(self.trusted_wall_now)
        UtcMicros(self.occurred_at)
        if self.max_attempts is not None:
            ObjectId(self.max_attempts)
        elif self.binding_details is None:
            raise ValueError("普通检查等待终结必须保存实际采用的次数上限")
        if self.binding_details is None and self.current_identity is not None:
            raise ValueError("当前机身身份必须属于绑定失败申请")


def parse_waiting_finish_request(raw):
    base = {"check_id", "trusted_wall_now", "occurred_at", "max_attempts"}
    if not isinstance(raw, dict) or set(raw) not in (base, base | {"binding_details", "current_identity"}):
        raise ConsistencyError("检查等待终结缺少完整申请")
    try:
        return WaitingFinishRequest(**raw)
    except (TypeError, ValueError) as error:
        raise ConsistencyError("检查等待终结申请无效") from error

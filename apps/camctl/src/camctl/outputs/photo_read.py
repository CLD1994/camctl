"""按需照片来源的一次下载长度及目标重置决定。"""

from dataclasses import dataclass

from camctl.contracts.json_values import is_json_integer
from camctl.contracts.values import MAX_OBJECT_ID, ObjectId, UtcMicros, parse_object_id
from camctl.operations.models import AttemptTicket


@dataclass(frozen=True)
class PhotoReadStart:
    """已打开的本次照片读取所观察到的完整长度。"""

    copy_id: int
    ticket: AttemptTicket
    size_bytes: int
    occurred_at: int

    def __post_init__(self):
        ObjectId(self.copy_id)
        UtcMicros(self.occurred_at)
        if (not isinstance(self.ticket, AttemptTicket) or self.ticket.operation != "read"
                or self.ticket.target_id != str(self.copy_id)
                or self.ticket.responsibility_key != f"read/{self.copy_id}"):
            raise ValueError("照片读取长度必须对应原 READ 票据")
        ObjectId(self.ticket.run_id)
        ObjectId(self.ticket.attempt_id)
        if type(self.size_bytes) is not int or not 0 < self.size_bytes <= MAX_OBJECT_ID:
            raise ValueError("照片本次下载长度必须是正整数")

    def evidence(self) -> dict:
        return {"ticket": {"run_id": self.ticket.run_id, "attempt_no": self.ticket.attempt_id,
                           "operation": self.ticket.operation, "target_id": self.ticket.target_id,
                           "responsibility_key": self.ticket.responsibility_key},
                "size_bytes": self.size_bytes}


def parse_photo_read_start(value, occurred_at: int) -> PhotoReadStart:
    if not isinstance(value, dict) or set(value) != {"ticket", "size_bytes"}:
        raise ValueError("照片读取长度证据成员不完整")
    ticket = value["ticket"]
    if not isinstance(ticket, dict) or set(ticket) != {
            "run_id", "attempt_no", "operation", "target_id", "responsibility_key"}:
        raise ValueError("照片读取票据成员不完整")
    if not all(is_json_integer(item) for item in (ticket["run_id"], ticket["attempt_no"], value["size_bytes"])):
        raise ValueError("照片读取票据或长度不是整数")
    copy_id = parse_object_id(ticket["target_id"])
    return PhotoReadStart(copy_id, AttemptTicket(int(ticket["attempt_no"]), ticket["operation"],
        ticket["target_id"], ticket["responsibility_key"], int(ticket["run_id"])),
        int(value["size_bytes"]), occurred_at)


@dataclass(frozen=True)
class PhotoReadDecision:
    source_size: int
    committed_bytes: int
    reset_pending: bool


def decide_photo_read_start(*, previous_size: int | None, committed_bytes: int,
                            reset_pending: bool, current_size: int) -> PhotoReadDecision:
    """新下载是新的字节快照；旧进度不能拼入本次下载。"""
    if previous_size is not None and (type(previous_size) is not int or previous_size <= 0):
        raise ValueError("已保存的照片读取长度无效")
    if (type(committed_bytes) is not int or committed_bytes < 0
            or (previous_size is None and committed_bytes != 0)
            or (previous_size is not None and committed_bytes > previous_size)):
        raise ValueError("照片可靠进度与原读取长度不符")
    if type(reset_pending) is not bool or type(current_size) is not int or not 0 < current_size <= MAX_OBJECT_ID:
        raise ValueError("照片读取长度或重置状态无效")
    return PhotoReadDecision(current_size, 0, reset_pending or previous_size is not None)

"""照片单角色缓存的有界原申请格式。"""
from dataclasses import dataclass
import re
from typing import Mapping

from camctl.contracts.json_values import is_json_integer
from camctl.contracts.values import MAX_OBJECT_ID, ObjectId, UtcMicros


@dataclass(frozen=True)
class PhotoCacheComplete:
    action_id: int
    file_id: int
    device_file_id: int
    result_page_event_id: int
    selected: str
    size_bytes: int
    sha256: str
    occurred_at: int
    archive_error: Mapping | None

    def __post_init__(self):
        for name in ("action_id", "file_id", "device_file_id", "result_page_event_id"):
            ObjectId(getattr(self, name))
        if self.selected not in ("sync", "camera"):
            raise ValueError("照片缓存来源类别无效")
        if type(self.size_bytes) is not int or not 0 < self.size_bytes <= MAX_OBJECT_ID:
            raise ValueError("照片缓存大小必须是正整数")
        if not isinstance(self.sha256, str) or re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None:
            raise ValueError("照片缓存摘要无效")
        UtcMicros(self.occurred_at)
        if self.archive_error is not None and not isinstance(self.archive_error, Mapping):
            raise ValueError("照片缓存错误必须是原异常诊断或 null")


def parse_photo_cache_request(value):
    if not isinstance(value, Mapping) or set(value) != {
            "action_id", "file_id", "device_file_id", "result_page_event_id",
            "selected", "size_bytes", "sha256", "occurred_at", "archive_error"}:
        raise ValueError("照片缓存申请成员不完整")
    values = dict(value)
    for name in ("action_id", "file_id", "device_file_id", "result_page_event_id", "size_bytes", "occurred_at"):
        if not is_json_integer(values[name]):
            raise ValueError(f"照片缓存申请 {name} 必须是整数")
        values[name] = int(values[name])
    return PhotoCacheComplete(**values)

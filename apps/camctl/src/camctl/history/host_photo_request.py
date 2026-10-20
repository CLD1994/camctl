"""单张主机拍摄的纯请求格式及事件正文契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, get_type_hints

from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import is_json_integer
from camctl.contracts.values import MAX_OBJECT_ID, ObjectId, UtcMicros
from camctl.host_files.models import FilePurpose
from camctl.host_files.paths import (
    HOST_PHOTO_PURPOSES,
    validate_relative_file_path,
)

__all__ = [
    "HostPhotoRequestError",
    "HostPhotoComplete",
    "PrepareHostPhoto",
    "parse_host_photo_request",
    "validate_host_photo_event_structure",
]

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PREPARE_MEMBERS = frozenset({"action_id", "occurred_at"})
_COMPLETE_MEMBERS = frozenset({
    "action_id",
    "original_file_id",
    "original_size_bytes",
    "original_sha256",
    "preview_file_id",
    "preview_size_bytes",
    "preview_sha256",
    "occurred_at",
})
_PURPOSE = enum_for("intermediate_files.purpose")
_HOST_PURPOSE_BY_CODE = {
    int(_PURPOSE[purpose.name]): purpose for purpose in HOST_PHOTO_PURPOSES
}


class HostPhotoRequestError(ValueError):
    """主机拍摄请求或其事件正文不满足格式契约。"""


def _exact_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HostPhotoRequestError(f"{name} 必须是精确整数")
    return value


def _json_integer(value: Any, name: str) -> int:
    """将精确 JSON 数学整数适配为内部 int，同时排除布尔和非整数。"""
    if not is_json_integer(value):
        raise HostPhotoRequestError(f"{name} 必须是精确整数")
    return int(value)


def _object_id(value: Any, name: str) -> None:
    try:
        ObjectId(_exact_int(value, name))
    except ValueError as error:
        raise HostPhotoRequestError(f"{name} 必须是 SQLite 范围内的正身份") from error


def _utc_micros(value: Any) -> None:
    try:
        UtcMicros(_exact_int(value, "occurred_at"))
    except ValueError as error:
        raise HostPhotoRequestError("occurred_at 必须是 UTC 微秒整数") from error


def _positive_size(value: Any, name: str) -> None:
    size = _exact_int(value, name)
    if not 0 < size <= MAX_OBJECT_ID:
        raise HostPhotoRequestError(f"{name} 必须是 SQLite 范围内的正字节数")


def _sha256(value: Any, name: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise HostPhotoRequestError(f"{name} 必须是 64 位小写 SHA-256")


@dataclass(frozen=True)
class PrepareHostPhoto:
    action_id: int
    occurred_at: int

    def __post_init__(self) -> None:
        _object_id(self.action_id, "action_id")
        _utc_micros(self.occurred_at)


@dataclass(frozen=True)
class HostPhotoComplete:
    action_id: int
    original_file_id: int
    original_size_bytes: int
    original_sha256: str
    preview_file_id: int
    preview_size_bytes: int
    preview_sha256: str
    occurred_at: int

    def __post_init__(self) -> None:
        for name in ("action_id", "original_file_id", "preview_file_id"):
            _object_id(getattr(self, name), name)
        if self.original_file_id == self.preview_file_id:
            raise HostPhotoRequestError("原图与预览必须使用不同文件身份")
        _positive_size(self.original_size_bytes, "original_size_bytes")
        _positive_size(self.preview_size_bytes, "preview_size_bytes")
        _sha256(self.original_sha256, "original_sha256")
        _sha256(self.preview_sha256, "preview_sha256")
        _utc_micros(self.occurred_at)


def parse_host_photo_request(value: Any, *, reason: int) -> PrepareHostPhoto | HostPhotoComplete:
    """按准备或完整字节申请的唯一成员集解析请求。"""
    if type(reason) is not int:
        raise HostPhotoRequestError("主机拍摄申请分支必须是整数")
    expected = _PREPARE_MEMBERS if reason == 1 else _COMPLETE_MEMBERS if reason == 2 else None
    if expected is None:
        raise HostPhotoRequestError(f"不支持主机拍摄申请分支: {reason!r}")
    if not isinstance(value, Mapping):
        raise HostPhotoRequestError("host_photo_request 必须是对象")
    actual = set(value)
    if actual != expected:
        missing, extra = sorted(expected - actual), sorted(actual - expected)
        raise HostPhotoRequestError(
            f"host_photo_request 成员不符: 缺少 {missing}，多出 {extra}"
        )
    try:
        request_type = PrepareHostPhoto if reason == 1 else HostPhotoComplete
        type_hints = get_type_hints(request_type)
        request_values = {
            name: _json_integer(item, name) if type_hints[name] is int else item
            for name, item in value.items()
        }
        return request_type(**request_values)
    except (TypeError, ValueError) as error:
        if isinstance(error, HostPhotoRequestError):
            raise
        raise HostPhotoRequestError(f"host_photo_request 格式非法: {error}") from error


def validate_host_photo_event_structure(event: Any, *, event_name: str, branch_name: str) -> None:
    """核对事件正文自身可证明的主机拍摄申请、责任身份与完整字节。"""
    if event_name != "INTERMEDIATE_FILE_CHANGED":
        return
    raw = event.evidence.get("host_photo_request")
    capture_creates = [
        row for row in event.rows
        if row.table == "intermediate_files"
        and not row.before.exists
        and row.after.values.get("purpose") in _HOST_PURPOSE_BY_CODE
    ]
    if branch_name == "CREATE":
        if not capture_creates and raw is None:
            return
        if raw is None:
            raise HostPhotoRequestError("主机拍摄 CREATE 缺少原准备申请")
        request = parse_host_photo_request(raw, reason=event.reason)
        if not isinstance(request, PrepareHostPhoto):
            raise HostPhotoRequestError("主机拍摄 CREATE 必须使用准备申请")
        if request.occurred_at != event.occurred_at:
            raise HostPhotoRequestError("准备申请时刻与事件头不一致")
        if len(event.rows) != 2 or len(capture_creates) != 2:
            raise HostPhotoRequestError("准备申请必须共同创建恰好两份主机拍摄责任")
        roles: list[FilePurpose] = []
        for row in capture_creates:
            values = row.after.values
            purpose_code = _json_integer(values.get("purpose"), "purpose")
            purpose = _HOST_PURPOSE_BY_CODE.get(purpose_code)
            if purpose is None or row.table != "intermediate_files":
                raise HostPhotoRequestError("准备申请包含未知主机拍摄角色")
            roles.append(purpose)
            owner_action_id = _json_integer(values.get("owner_action_id"), "owner_action_id")
            if (owner_action_id != request.action_id
                    or values.get("owner_delivery_id") is not None):
                raise HostPhotoRequestError("主机拍摄责任动作与准备申请不一致")
            retention_state = _json_integer(values.get("retention_state"), "retention_state")
            cleanup_state = _json_integer(values.get("cleanup_state"), "cleanup_state")
            try:
                validate_relative_file_path(purpose, row.row_id, values.get("relative_path"))
            except ValueError as error:
                raise HostPhotoRequestError("主机拍摄责任路径与角色或身份不一致") from error
            if (retention_state != 1
                    or cleanup_state != 1
                    or values.get("size_bytes") is not None or values.get("sha256") is not None):
                raise HostPhotoRequestError("准备责任必须是未提升、未清理且尚无完整字节的文件")
        if set(roles) != set(HOST_PHOTO_PURPOSES) or len(roles) != len(set(roles)):
            raise HostPhotoRequestError("准备申请必须各有一份原图和预览责任")
        return

    if branch_name != "HOST_PHOTO_COMPLETE":
        if branch_name == "LIFECYCLE" and raw is not None:
            raise HostPhotoRequestError("通用 LIFECYCLE 不携带主机拍摄申请")
        return
    if raw is None:
        raise HostPhotoRequestError("主机拍摄完整字节分支缺少申请")
    request = parse_host_photo_request(raw, reason=2)
    if not isinstance(request, HostPhotoComplete):
        raise HostPhotoRequestError("主机拍摄完整字节事件必须使用完整字节申请")
    if request.occurred_at != event.occurred_at:
        raise HostPhotoRequestError("完整字节申请时刻与事件头不一致")
    expected = {
        request.original_file_id: (request.original_size_bytes, request.original_sha256),
        request.preview_file_id: (request.preview_size_bytes, request.preview_sha256),
    }
    if len(event.rows) != 2 or {row.row_id for row in event.rows} != set(expected):
        raise HostPhotoRequestError("完整字节申请必须更新其两份不同文件责任")
    for row in event.rows:
        if row.table != "intermediate_files" or not row.before.exists or not row.after.exists:
            raise HostPhotoRequestError("完整字节申请只更新既有主机文件责任")
        size, digest = expected[row.row_id]
        if (set(row.before.values) != {"size_bytes", "sha256"}
                or row.before.values != {"size_bytes": None, "sha256": None}
                or set(row.after.values) != {"size_bytes", "sha256"}
                or _json_integer(row.after.values.get("size_bytes"), "size_bytes") != size
                or not isinstance(row.after.values.get("sha256"), str)
                or row.after.values.get("sha256") != digest):
            raise HostPhotoRequestError("完整字节行与申请的大小或 SHA-256 不一致")

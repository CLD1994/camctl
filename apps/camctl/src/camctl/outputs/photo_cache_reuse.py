"""按原照片、角色及等待责任判定固定缓存是否可复用。"""

from dataclasses import dataclass
from typing import Mapping

from camctl.contracts.enums import enum_for
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.values import ConsistencyError


@dataclass(frozen=True)
class BoundPhotoCache:
    source_action_id: int
    source_device_file_id: int
    intermediate_file_id: int
    result_page_event_id: int | None
    published_boundary: HistoryBoundary
    size_bytes: int
    sha256: str | None
    relative_path: str
    purpose: str
    start_result_event_id: int | None = None


def photo_cache_source_matches(action: Mapping | None, *, driver_id: str) -> bool:
    """仅设备正式源照片具有按角色固定的本地缓存。"""
    return (action is not None and action["type"] == int(enum_for("actions.type").CAMERA_TAKE_PHOTO)
            and action["driver_id"] == driver_id
            and action["execution_spec_json"].get("output_storage") == "device")


def photo_cache_role(output: Mapping):
    """按正式产物角色取得对应的缓存用途和设备观察角色。"""
    kind, purpose = enum_for("outputs.kind"), enum_for("intermediate_files.purpose")
    return {int(kind.ORIGINAL): (purpose.CAPTURE_ORIGINAL, "original"),
            int(kind.PREVIEW): (purpose.CAPTURE_PREVIEW, "thumbnail")}.get(output["kind"])


def photo_cache_kind(purpose_value):
    """根据缓存用途取得对应的正式产物角色。"""
    purposes, kinds = enum_for("intermediate_files.purpose"), enum_for("outputs.kind")
    return {int(purposes.CAPTURE_ORIGINAL): int(kinds.ORIGINAL),
            int(purposes.CAPTURE_PREVIEW): int(kinds.PREVIEW)}.get(purpose_value)


def photo_cache_usable(*, file: Mapping, deferred: bool, allow_released: bool, historical: bool) -> bool:
    """固定等待者只能使用仍保留的角色缓存或明确允许的历史缓存。"""
    retention, cleanup = enum_for("intermediate_files.retention_state"), enum_for("intermediate_files.cleanup_state")
    retained = (file["retention_state"] == int(retention.REQUIRED)
                and file["cleanup_state"] == int(cleanup.NOT_NEEDED))
    released = (allow_released and file["retention_state"] == int(retention.RELEASABLE)
                and (historical or file["cleanup_state"] != int(cleanup.COMPLETED)))
    return (bool(retained or released)
            and (allow_released or file["last_error_json"] is None)
            and file["size_bytes"] is not None and (deferred or file["sha256"] is not None))


def photo_cache_waiter_finished(status: int) -> bool:
    """终态取回动作不再占有照片角色缓存。"""
    states = enum_for("actions.status")
    return status in (int(states.SUCCEEDED), int(states.FAILED), int(states.CANCELED), int(states.EXPIRED))


def photo_cache_item_needs_cache(items) -> bool:
    """已物化的零条或一条固定选择成员是否仍需缓存。"""
    if len(items) > 1:
        raise ConsistencyError("同一来源选择重复包含照片角色产物")
    item_status, delivery = enum_for("obtain_items.status"), enum_for("deliveries.status")
    if not items or items[0][0] in (int(item_status.FAILED), int(item_status.CANCELED)):
        return False
    return items[0][1] not in (int(delivery.PUBLISHED), int(delivery.FAILED),
                               int(delivery.CANCELED), int(delivery.WITHDRAWN))


def validate_photo_cache_binding_copy(copy: Mapping) -> None:
    """绑定失败必须关联本地照片缓存的完整读取和真实摘要不符。"""
    if (copy.get("source_intermediate_file_id") is None or copy.get("source_device_file_id") is not None
            or copy.get("delivery_id") is None or copy.get("read_end_event_id") is None
            or copy.get("verification_state") != int(enum_for("file_copies.verification_state").MISMATCHED)
            or copy.get("committed_bytes") != copy.get("source_size")
            or not isinstance(copy.get("source_sha256"), str) or not isinstance(copy.get("target_sha256"), str)
            or copy["source_sha256"] == copy["target_sha256"]):
        raise ConsistencyError("照片缓存绑定失败缺少完整读取与实际摘要不符")

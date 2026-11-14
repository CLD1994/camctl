"""照片角色缓存复用资格只由固定角色和明确文件事实决定。"""

import pytest

from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.outputs.photo_cache_reuse import (
    photo_cache_item_needs_cache, photo_cache_kind, photo_cache_role,
    photo_cache_source_matches, photo_cache_usable, photo_cache_waiter_finished,
)


def _file(**changes):
    retention = enum_for("intermediate_files.retention_state")
    cleanup = enum_for("intermediate_files.cleanup_state")
    return dict(retention_state=int(retention.REQUIRED), cleanup_state=int(cleanup.NOT_NEEDED),
                last_error_json=None, size_bytes=9, sha256="a" * 64) | changes


def test_role_and_source_identity_are_required():
    action = {"type": int(enum_for("actions.type").CAMERA_TAKE_PHOTO), "driver_id": "camera",
              "execution_spec_json": {"output_storage": "device"}}
    assert photo_cache_source_matches(action, driver_id="camera")
    assert not photo_cache_source_matches(action, driver_id="other")
    assert not photo_cache_source_matches(action | {"execution_spec_json": {"output_storage": "host"}}, driver_id="camera")
    kind, purpose = enum_for("outputs.kind"), enum_for("intermediate_files.purpose")
    assert photo_cache_role({"kind": int(kind.ORIGINAL)}) == (purpose.CAPTURE_ORIGINAL, "original")
    assert photo_cache_kind(int(purpose.CAPTURE_PREVIEW)) == int(kind.PREVIEW)
    assert photo_cache_role({"kind": int(kind.REPAIRED)}) is None


def test_released_cache_requires_explicit_historical_qualification():
    retention, cleanup = enum_for("intermediate_files.retention_state"), enum_for("intermediate_files.cleanup_state")
    file = _file(retention_state=int(retention.RELEASABLE), cleanup_state=int(cleanup.COMPLETED))
    assert not photo_cache_usable(file=file, deferred=False, allow_released=False, historical=False)
    assert not photo_cache_usable(file=file, deferred=False, allow_released=True, historical=False)
    assert photo_cache_usable(file=file, deferred=False, allow_released=True, historical=True)
    assert not photo_cache_usable(file=_file(sha256=None), deferred=False, allow_released=False, historical=False)
    assert photo_cache_usable(file=_file(sha256=None), deferred=True, allow_released=False, historical=False)
    assert not photo_cache_usable(file=_file(last_error_json={}), deferred=False, allow_released=False, historical=False)


def test_fixed_waiter_requires_cache_until_original_delivery_terminal():
    action, item, delivery = enum_for("actions.status"), enum_for("obtain_items.status"), enum_for("deliveries.status")
    assert photo_cache_waiter_finished(int(action.CANCELED))
    assert not photo_cache_waiter_finished(int(action.RUNNING))
    assert photo_cache_item_needs_cache([(int(item.SELECTED), None)])
    assert not photo_cache_item_needs_cache([(int(item.FAILED), None)])
    assert not photo_cache_item_needs_cache([(int(item.DELIVERY_CREATED), int(delivery.PUBLISHED))])
    assert not photo_cache_item_needs_cache([])
    with pytest.raises(ConsistencyError, match="重复包含"):
        photo_cache_item_needs_cache([(1, None), (1, None)])

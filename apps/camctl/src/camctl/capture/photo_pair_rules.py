"""依据已读取的来源事实核对单张照片与产物配对。"""

from __future__ import annotations

import re
from typing import Any, Mapping

from camctl.acceptance.definitions import read_action_spec
from camctl.capture.photo_format import saved_photo_format
from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import ConsistencyError, MAX_OBJECT_ID
from camctl.host_files.models import FilePurpose
from camctl.host_files.paths import HOST_PHOTO_PURPOSES, relative_file_path, validate_relative_file_path
from camctl.host_files.photo_formats import photo_file_extension, photo_media_type
from camctl.outputs.catalog import OutputDraft, OutputKind

_PURPOSE = enum_for("intermediate_files.purpose")
_RETENTION = enum_for("intermediate_files.retention_state")
_FILE_CLEANUP = enum_for("intermediate_files.cleanup_state")
_FILE_COMPLETION = enum_for("device_files.completion_state")
_FILE_CHECKSUM = enum_for("device_files.checksum_support")
_OWNERSHIP_METHOD = enum_for("device_files.ownership_evidence_json.method")
_COMPLETION_BASIS = enum_for("device_files.completion_evidence_json.basis")

_OUTPUT_KIND = enum_for("outputs.kind")
_KIND_CODES = {kind: int(_OUTPUT_KIND[kind.name]) for kind in OutputKind}


#: device_files.role 与产物种类的对应；修复产物承载于中间文件。
_FILE_ROLE = enum_for("device_files.role")
_FILE_ROLE_FOR_KIND = {_OUTPUT_KIND.ORIGINAL: _FILE_ROLE.ORIGINAL, _OUTPUT_KIND.PREVIEW: _FILE_ROLE.PREVIEW}
_HOST_PURPOSE_FOR_KIND = {
    _OUTPUT_KIND.ORIGINAL: _PURPOSE.CAPTURE_ORIGINAL,
    _OUTPUT_KIND.PREVIEW: _PURPOSE.CAPTURE_PREVIEW,
    _OUTPUT_KIND.REPAIRED: _PURPOSE.REPAIR_OUTPUT,
}
_HOST_PHOTO_KIND_FOR_PURPOSE = {
    int(purpose): kind
    for kind, purpose in _HOST_PURPOSE_FOR_KIND.items()
    if FilePurpose[purpose.name] in HOST_PHOTO_PURPOSES
}
_HOST_PHOTO_PURPOSE_CODES = frozenset(_HOST_PHOTO_KIND_FOR_PURPOSE)
_HOST_PHOTO_SHA256 = re.compile(r"[0-9a-f]{64}\Z")

class _HostPhotoPairError(ValueError):
    """主机单张拍摄的原图、预览责任或产物配对不完整。"""


def _host_photo_source_pair(
    rows: Mapping[int, Mapping[str, Any]], action_id: int, *,
    host_spec: bool = False, terminal: bool = False, device_temporary: bool = False,
    action: Mapping[str, Any],
) -> dict[int, Mapping[str, Any]] | None:
    """按已保存规格和来源责任读取唯一原图/预览对。"""
    photo_rows = [
        facts for facts in rows.values()
        if facts.get("owner_action_id") == action_id
        and facts.get("purpose") in _HOST_PHOTO_PURPOSE_CODES
    ]
    if not photo_rows:
        if host_spec:
            raise _HostPhotoPairError("主机单张成功缺少原图和预览文件责任")
        return None
    if len(photo_rows) != len(HOST_PHOTO_PURPOSES):
        raise _HostPhotoPairError("成功单张拍摄必须恰有原图和预览两份文件责任")
    by_kind: dict[int, Mapping[str, Any]] = {}
    for facts in photo_rows:
        purpose_code = facts.get("purpose")
        kind = _HOST_PHOTO_KIND_FOR_PURPOSE.get(purpose_code)
        if kind is None or kind in by_kind:
            raise _HostPhotoPairError("主机拍摄角色必须各有且只有一份文件责任")
        purpose = _PURPOSE(purpose_code)
        if (facts.get("owner_action_id") != action_id
                or facts.get("owner_delivery_id") is not None):
            raise _HostPhotoPairError("主机拍摄文件责任与成功动作不一致")
        try:
            model = FilePurpose[purpose.name]
            validate_relative_file_path(model, facts["id"], facts["relative_path"])
            expected_path = relative_file_path(
                model, facts["id"], photo_file_extension(model, saved_photo_format(action)))
        except (KeyError, TypeError, ValueError) as error:
            raise _HostPhotoPairError("主机拍摄固定角色路径与文件身份不一致") from error
        if facts.get("relative_path") != expected_path:
            raise _HostPhotoPairError("主机拍摄固定角色路径与文件身份不一致")
        if terminal and device_temporary:
            valid_lifecycle = facts.get("retention_state") in (
                int(_RETENTION.REQUIRED), int(_RETENTION.RELEASABLE))
        elif terminal:
            valid_lifecycle = (
                facts.get("retention_state") == int(_RETENTION.PROMOTED)
                and facts.get("cleanup_state") in (
                    int(_FILE_CLEANUP.NOT_NEEDED), int(_FILE_CLEANUP.COMPLETED))
            )
        else:
            valid_lifecycle = (
                facts.get("retention_state") == int(_RETENTION.REQUIRED)
                and facts.get("cleanup_state") == int(_FILE_CLEANUP.NOT_NEEDED)
            )
        size = facts.get("size_bytes")
        digest = facts.get("sha256")
        if (not valid_lifecycle or type(size) is not int
                or not 0 < size <= MAX_OBJECT_ID
                or not isinstance(digest, str) or _HOST_PHOTO_SHA256.fullmatch(digest) is None):
            raise _HostPhotoPairError("主机拍摄两份责任必须保留完整可靠大小与 SHA-256")
        by_kind[kind] = facts
    if set(by_kind) != {_KIND_CODES[OutputKind.ORIGINAL], _KIND_CODES[OutputKind.PREVIEW]}:
        raise _HostPhotoPairError("主机拍摄责任角色没有恰好配成原图和预览")
    return by_kind


def _host_photo_execution_spec(action: Mapping[str, Any]) -> bool:
    """校验保存的单张执行规格，并指出其是否声明主机输出。"""
    spec = read_action_spec(action)
    if spec is None:
        raise ConsistencyError("单张动作缺少可解释的已保存执行规格")
    return spec.get("output_storage") == "host"


def _device_photo_source_pair(device_rows, temporary_rows, action_id, *, action, terminal=False):
    """设备单张按冻结的来源语义核对原图与预览配对。"""
    files = [row for row in device_rows.values() if row.get("source_action_id") == action_id]
    from camctl.devices.drivers.hasselblad.files import (
        CameraFileIdentity, DeferredCameraFileIdentity, DownloadVariant,
    )
    from camctl.devices.bindings import DeviceBinding

    deferred = read_action_spec(action).get("photo_source_policy") == "deferred"
    capture_format = saved_photo_format(action)
    by_kind = {}
    identities = []
    for kind in (_KIND_CODES[OutputKind.ORIGINAL], _KIND_CODES[OutputKind.PREVIEW]):
        matches = [row for row in files if row.get("role") == _FILE_ROLE_FOR_KIND[kind]]
        if len(matches) != 1:
            raise _HostPhotoPairError("相机单张必须恰有原图和预览两份设备源")
        source = matches[0]
        ownership = source.get("ownership_evidence_json")
        completion = source.get("completion_evidence_json")
        ownership = parse_exact_json(ownership) if isinstance(ownership, str) else ownership
        completion = parse_exact_json(completion) if isinstance(completion, str) else completion
        if (source.get("observer_action_id") != action_id
                or source.get("completion_state") != int(_FILE_COMPLETION.COMPLETE)
                or (not deferred and source.get("checksum_support") != int(_FILE_CHECKSUM.SUPPORTED))
                or not isinstance(ownership, Mapping)
                or ownership.get("method") != int(_OWNERSHIP_METHOD.DRIVER_TASK_ASSOCIATION)
                or not isinstance(completion, Mapping)
                or completion.get("basis") != int(_COMPLETION_BASIS.DEVICE_GUARANTEE)):
            raise _HostPhotoPairError("相机源缺少可靠归属、完成或固定摘要")
        purpose = FilePurpose[_HOST_PURPOSE_FOR_KIND[kind].name]
        locator = source.get("locator_json")
        try:
            locator = parse_exact_json(locator) if isinstance(locator, str) else locator
            identity_type = DeferredCameraFileIdentity if deferred else CameraFileIdentity
            identity = identity_type.from_source(source["identity_key"], locator)
            expected_variant = (DownloadVariant.ORIGINAL if purpose is FilePurpose.CAPTURE_ORIGINAL
                                else DownloadVariant.THUMBNAIL)
            if (identity.binding != DeviceBinding(action["device_id"], action["driver_id"])
                    or identity.variant is not expected_variant
                    or not identity.camera_name.casefold().endswith(
                        "." + photo_file_extension(FilePurpose.CAPTURE_ORIGINAL, capture_format))):
                raise ValueError("设备源身份或媒体类型与原照片责任不一致")
            if deferred:
                if (source.get("size_bytes") is not None or source.get("sha256") is not None
                        or source.get("media_type") is not None
                        or source.get("checksum_support") not in (
                            int(_FILE_CHECKSUM.UNDETERMINED), int(_FILE_CHECKSUM.UNSUPPORTED))):
                    raise ValueError("按需照片来源不能冒充已读取的大小、摘要或媒体类型")
            elif (identity.sha256 != source["sha256"] or identity.size_bytes != source["size_bytes"]
                    or source.get("media_type") != photo_media_type(purpose, capture_format)):
                raise ValueError("设备源固定内容与原照片责任不一致")
        except (KeyError, TypeError, ValueError) as error:
            raise _HostPhotoPairError("相机源定位与原绑定、用途及固定内容不一致") from error
        identities.append(identity)
        by_kind[kind] = source
    if len({(identity.serial, identity.product_id, identity.camera_path, identity.camera_name)
            for identity in identities}) != 1:
        raise _HostPhotoPairError("相机原图与预览必须指向同一机内照片")
    if deferred and len({identity.activity_id for identity in identities}) != 1:
        raise _HostPhotoPairError("相机原图与预览必须属于同一拍摄活动")
    if len(files) != 2:
        raise _HostPhotoPairError("相机单张必须恰有两个设备来源角色")
    original = by_kind[_KIND_CODES[OutputKind.ORIGINAL]]
    preview = by_kind[_KIND_CODES[OutputKind.PREVIEW]]
    if (original.get("original_device_file_id") is not None
            or original.get("pairing_evidence_json") is not None
            or preview.get("original_device_file_id") != original["id"]
            or preview.get("pairing_evidence_json") is None):
        raise _HostPhotoPairError("相机预览没有关联本动作的原图设备源")
    return by_kind


def _device_photo_drafts(drafts, sources):
    if len(drafts) != len(sources) or {draft.kind for draft in drafts} != {
            OutputKind.ORIGINAL, OutputKind.PREVIEW}:
        raise _HostPhotoPairError("相机单张必须同时登记原图和预览")
    for draft in drafts:
        source = sources[_KIND_CODES[draft.kind]]
        if (draft.file.device_file_id != source["id"]
                or draft.file.intermediate_file_id is not None
                or draft.file_complete is not True or draft.sha256 != source["sha256"]):
            raise _HostPhotoPairError("相机产物草稿与可靠设备源不一致")
        if draft.kind is OutputKind.PREVIEW and (
                draft.original_batch_file_id != sources[_KIND_CODES[OutputKind.ORIGINAL]]["id"]
                or draft.original_output_id is not None
                or draft.original_batch_intermediate_file_id is not None):
            raise _HostPhotoPairError("相机预览必须关联同批原图设备源")


def _host_photo_drafts(
    drafts: tuple[OutputDraft, ...], sources: Mapping[int, Mapping[str, Any]], *, action
) -> dict[int, OutputDraft]:
    """要求成功申请的两份草稿与原责任身份、完整摘要和预览关联相同。"""
    if len(drafts) != 2:
        raise _HostPhotoPairError("成功单张拍摄必须登记原图与预览两份产物")
    by_kind = {draft.kind: draft for draft in drafts}
    if len(by_kind) != 2 or set(by_kind) != {OutputKind.ORIGINAL, OutputKind.PREVIEW}:
        raise _HostPhotoPairError("成功单张拍摄草稿必须恰有一份原图和一份预览")
    original = by_kind[OutputKind.ORIGINAL]
    preview = by_kind[OutputKind.PREVIEW]
    for draft in (original, preview):
        source = sources[_KIND_CODES[draft.kind]]
        purpose = FilePurpose[_HOST_PURPOSE_FOR_KIND[_KIND_CODES[draft.kind]].name]
        if (draft.file.device_file_id is not None
                or draft.file.intermediate_file_id != source["id"]
                or draft.file_complete is not True
                or draft.sha256 != source["sha256"]
                or not isinstance(draft.original_name, str) or not draft.original_name
                or draft.media_type != photo_media_type(purpose, saved_photo_format(action))):
            raise _HostPhotoPairError("主机拍摄草稿的身份、字节摘要或元信息与来源不一致")
    original_id = sources[_KIND_CODES[OutputKind.ORIGINAL]]["id"]
    if (preview.original_batch_intermediate_file_id != original_id
            or preview.original_output_id is not None
            or preview.original_batch_file_id is not None):
        raise _HostPhotoPairError("主机预览必须明确关联同一动作的原图责任")
    return {_KIND_CODES[kind]: draft for kind, draft in by_kind.items()}


def _host_photo_registered_pair(
    outputs: Mapping[int, Mapping[str, Any]],
    origins: Mapping[int, Mapping[str, Any]],
    action_id: int,
    sources: Mapping[int, Mapping[str, Any]],
    *, device: bool = False, action,
) -> None:
    """检查保存或提案中的两份正式产物和唯一原图关联。"""
    action_outputs = {
        identity: values for identity, values in outputs.items()
        if values.get("source_action_id") == action_id
    }
    if len(action_outputs) != 2:
        raise _HostPhotoPairError("成功单张拍摄的正式产物必须恰有原图和预览")
    by_kind = {values.get("kind"): (identity, values)
               for identity, values in action_outputs.items()}
    if (len(by_kind) != 2
            or set(by_kind) != {_KIND_CODES[OutputKind.ORIGINAL], _KIND_CODES[OutputKind.PREVIEW]}):
        raise _HostPhotoPairError("成功单张拍摄的正式产物角色必须恰好配对")
    original_id, original = by_kind[_KIND_CODES[OutputKind.ORIGINAL]]
    preview_id, preview = by_kind[_KIND_CODES[OutputKind.PREVIEW]]
    for kind, values in (
        (_KIND_CODES[OutputKind.ORIGINAL], original),
        (_KIND_CODES[OutputKind.PREVIEW], preview),
    ):
        source = sources[kind]
        source_column = "device_file_id" if device else "intermediate_file_id"
        other_column = "intermediate_file_id" if device else "device_file_id"
        media_type = (None if device and read_action_spec(action).get("photo_source_policy") == "deferred"
                      else photo_media_type(FilePurpose[_HOST_PURPOSE_FOR_KIND[kind].name],
                                            saved_photo_format(action)))
        if (values.get(source_column) != source["id"]
                or values.get(other_column) is not None
                or not isinstance(values.get("original_name"), str)
                or not values["original_name"]
                or values.get("media_type") != media_type):
            raise _HostPhotoPairError("正式产物角色、文件身份或可读元信息与主机责任不一致")
    original_links = [values for values in origins.values()
                      if values.get("output_id") == original_id]
    preview_links = [values for values in origins.values()
                     if values.get("output_id") == preview_id]
    if original_links or len(preview_links) != 1 or preview_links[0].get("original_output_id") != original_id:
        raise _HostPhotoPairError("正式预览必须唯一关联同动作正式原图")



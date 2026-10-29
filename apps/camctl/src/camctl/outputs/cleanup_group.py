"""识别共享机内照片的正式原图和预览。"""

from camctl.contracts.json_values import parse_exact_json
from camctl.contracts.values import ConsistencyError
from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID
from camctl.devices.drivers.hasselblad.files import (
    DownloadVariant, camera_file_identity_from_source,
)


def cleanup_group(reads, output_id):
    """按已登记配对和固定设备身份返回共同清理成员。"""
    output = reads.optional("outputs", output_id)
    if output is None or output.get("device_file_id") is None:
        return (output_id,)
    source = reads.required("device_files", output["device_file_id"])
    try:
        key = parse_exact_json(source["identity_key"])
        if not isinstance(key, list) or len(key) != 3:
            raise ValueError("设备文件身份结构无效")
        if key[1] != DRIVER_ID:
            return (output_id,)
        locator = source["locator_json"]
        identity = camera_file_identity_from_source(
            source["identity_key"], parse_exact_json(locator) if isinstance(locator, str) else locator)
        family = reads.family(output_id)
        if output_id not in (family.original.output_id,
                             None if family.preview is None else family.preview.output_id):
            return (output_id,)
        if family.preview is None:
            return (output_id,)
        pair = (family.original.output_id, family.preview.output_id)
        identities = []
        for member in pair:
            row = reads.required("outputs", member)
            if row.get("device_file_id") is None:
                raise ValueError("机内照片配对必须同为设备源")
            file = reads.required("device_files", row["device_file_id"])
            raw = file["locator_json"]
            parsed = camera_file_identity_from_source(
                file["identity_key"], parse_exact_json(raw) if isinstance(raw, str) else raw)
            if row["source_action_id"] != output["source_action_id"]:
                raise ValueError("机内照片配对的来源动作不同")
            identities.append(parsed)
        original, preview = identities

        def physical(value):
            return (value.binding, value.serial, value.product_id,
                    value.camera_path, value.camera_name)
        if (physical(original) != physical(preview)
                or physical(identity) != physical(original)
                or (hasattr(original, "activity_id") and hasattr(preview, "activity_id")
                    and original.activity_id != preview.activity_id)
                or original.variant is not DownloadVariant.ORIGINAL
                or preview.variant is not DownloadVariant.THUMBNAIL):
            raise ValueError("机内照片配对的固定身份不一致")
        return pair
    except (KeyError, TypeError, ValueError) as error:
        raise ConsistencyError("清理目标的机内照片配对无法可靠解释") from error

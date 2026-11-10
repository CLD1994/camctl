"""正式产物关联已取得的设备文件或中间文件时的资格判断。"""

from camctl.capture.photo_pair_rules import _FILE_ROLE_FOR_KIND, _OUTPUT_KIND
from camctl.contracts.enums import enum_for
from camctl.devices.capture_preparation import CAPTURE_ACTION_TYPES

CAPTURE_TYPES = frozenset(CAPTURE_ACTION_TYPES)
_PURPOSE = enum_for("intermediate_files.purpose")
_RETENTION = enum_for("intermediate_files.retention_state")


def capture_binding_error(action):
    if action.get("type") not in CAPTURE_TYPES:
        return "产物来源和设备文件观察者必须是拍摄动作"
    device, driver = action.get("device_id"), action.get("driver_id")
    if not isinstance(device, str) or not device or not isinstance(driver, str) or not driver:
        return "拍摄动作缺少已保存的设备或驱动绑定"
    return None


def output_carrier_error(values):
    if not {"device_file_id", "intermediate_file_id"} <= values.keys():
        return "产物承载文件类型尚未完整读取"
    if (values.get("device_file_id") is None) == (values.get("intermediate_file_id") is None):
        return "产物必须恰好由一种文件身份承载"
    return None


def device_source_ownership_error(facts, source_id):
    if facts.get("source_action_id") != source_id or facts.get("ownership_evidence_json") is None:
        return "产物承载文件缺少该来源动作的可靠归属"
    return None


def device_output_error(facts, kind, device_file_id):
    expected_role = _FILE_ROLE_FOR_KIND.get(kind)
    if expected_role is None or facts.get("role") != expected_role:
        return f"产物种类与文件角色不符: kind={kind} role={facts.get('role')}"
    if facts.get("completion_state") != 3:
        return f"产物承载文件未完成: {device_file_id}"
    if kind == _OUTPUT_KIND.ORIGINAL and (
        "original_device_file_id" not in facts or "pairing_evidence_json" not in facts
        or facts["original_device_file_id"] is not None or facts["pairing_evidence_json"] is not None
    ):
        return "原片文件必须明确没有预览配对"
    return None


def intermediate_output_ownership_error(facts, source_id):
    if (facts.get("owner_action_id") != source_id or "owner_delivery_id" not in facts
            or facts["owner_delivery_id"] is not None):
        return "产物中间文件不属于来源动作的文件责任"
    return None


def repair_output_error(facts):
    if facts.get("purpose") != int(_PURPOSE.REPAIR_OUTPUT):
        return f"修复成品承载文件不是修复输出用途: {facts.get('purpose')!r}"
    if facts.get("retention_state") != int(_RETENTION.REQUIRED):
        return f"修复成品承载文件必须尚未释放、提升或交接: {facts.get('retention_state')!r}"
    if facts.get("size_bytes") is None or facts.get("sha256") is None:
        return "修复成品承载文件缺少完整字节事实"
    return None


def origin_links_error(identity, kind, links):
    if kind == _OUTPUT_KIND.ORIGINAL:
        if links:
            return "原片不能携带派生关联"
        return None
    if len(links) != 1:
        return "派生产物必须共同登记唯一原片关联"
    if links[0]["original_output_id"] == identity:
        return "派生产物不能引用自身"
    return None


def derivative_origin_error(original, output, kind):
    if (original.get("kind") != _OUTPUT_KIND.ORIGINAL
            or original.get("source_action_id") != output["source_action_id"]
            or (kind == _OUTPUT_KIND.REPAIRED and original.get("device_file_id") is None)):
        return "派生关联必须指向同源原片；修复保持设备原片资格"
    return None


def preview_source_pair_error(original, output, file):
    if output["device_file_id"] is not None:
        if (file.get("original_device_file_id") != original["device_file_id"]
                or file.get("pairing_evidence_json") is None):
            return "预览的产物关联与设备文件配对不一致"
    elif original["intermediate_file_id"] is None:
        return "主机预览必须关联同源主机原图"
    return None


def existing_derivative_error(previous, output, seen_kinds):
    previous_kind = previous.get("kind")
    if (previous_kind not in {_OUTPUT_KIND.PREVIEW, _OUTPUT_KIND.REPAIRED}
            or previous_kind in seen_kinds
            or previous.get("source_action_id") != output["source_action_id"]):
        return "已有原片派生集合的种类或来源矛盾"
    return None


def duplicate_derivative_error(kind, seen_kinds):
    if kind in seen_kinds:
        return "同一原片至多登记一份预览和一份修复成品"
    return None

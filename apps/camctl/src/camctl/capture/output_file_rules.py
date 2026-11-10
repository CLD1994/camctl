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

"""根据已物化的设备文件事实判断归属、完成与摘要资格。"""

from collections.abc import Mapping

from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import is_json_integer

_ROLE = enum_for("device_files.role")
_OWNERSHIP = enum_for("device_files.ownership_evidence_json.method")
_PAIRING = enum_for("device_files.pairing_evidence_json.method")
_COMPLETION = enum_for("device_files.completion_state")
_BASIS = enum_for("device_files.completion_evidence_json.basis")
_CHECKSUM = enum_for("device_files.checksum_support")


def ownership_claim_error(file_id, before, after):
    """检查首次归属申请；原片的真实当前事实由调用方另行读取。"""
    if before.get("source_action_id") is not None or before.get("ownership_evidence_json") is not None:
        return "文件来源只能从未知一次确认"
    if before.get("role") != int(_ROLE.UNDETERMINED):
        return "已确认用途的文件不能再次确认归属"
    source = after.get("source_action_id")
    if not is_json_integer(source) or source < 1:
        return f"确认来源必须是合法动作身份: {source!r}"
    role = after.get("role")
    if role not in (int(_ROLE.ORIGINAL), int(_ROLE.PREVIEW)):
        return f"归属确认的用途必须是原片或预览: {role!r}"
    evidence = after.get("ownership_evidence_json")
    if isinstance(evidence, dict) and evidence.get("method") == int(_OWNERSHIP.RESULT_PAGE_DIFFERENCE):
        return "原页差集归属只能使用独立原页分支"
    if not isinstance(evidence, dict) or evidence.get("method") not in (int(member) for member in _OWNERSHIP):
        return "归属证据缺少登记的方法编号"
    if not isinstance(evidence.get("observation"), dict):
        return "归属证据缺少驱动观察依据"
    if (evidence.get("method") == int(_OWNERSHIP.BASELINE_DIFFERENCE)
            and not is_json_integer(evidence.get("activity_id"))):
        return "固定基准差集证据必须填写活动身份"
    paired, pairing = after.get("original_device_file_id"), after.get("pairing_evidence_json")
    if role == int(_ROLE.PREVIEW):
        if not is_json_integer(paired) or paired < 1 or paired == file_id:
            return f"预览配对必须是其他文件身份: {paired!r}"
        if (not isinstance(pairing, dict) or pairing.get("method") != int(_PAIRING.DRIVER_PAIRING)
                or not isinstance(pairing.get("observation"), dict)):
            return "预览配对缺少驱动配对证据"
    elif paired is not None or pairing is not None:
        return "原片不携带配对"
    return None


def ownership_pair_error(original, source_action_id):
    if original.get("role") != int(_ROLE.ORIGINAL) or original.get("source_action_id") != source_action_id:
        return "配对两端必须属于同一来源任务且原片用途为原片"
    return None


def completion_facts_error(size, evidence):
    """按实际大小和设备依据检查完成事实；原观察来源另行核实。"""
    observation = evidence.get("observation") if isinstance(evidence, dict) else None
    deferred_photo = size is None and isinstance(observation, Mapping) and "photo_start" in observation
    if not deferred_photo and (not is_json_integer(size) or size < 0):
        return f"完成状态必须携带非负完整大小: {size!r}"
    if (not isinstance(evidence, dict) or not is_json_integer(evidence.get("basis"))
            or evidence.get("basis") not in (int(member) for member in _BASIS)):
        return "完成状态缺少登记依据"
    if deferred_photo and evidence.get("basis") != int(_BASIS.DEVICE_GUARANTEE):
        return "未知大小的照片必须依据原设备完成保证"
    if not isinstance(evidence.get("observation"), dict):
        return "完成状态缺少驱动观察依据"
    return None


def completion_wait_reference_error(evidence):
    if (evidence.get("basis") == int(_BASIS.TIME_AND_OUTPUTS)
            and (not is_json_integer(evidence.get("activity_id"))
                 or not is_json_integer(evidence.get("wait_completed_event_id")))):
        return "等待与产物契约依据必须引用任务及等待完成事件"
    return None


def incomplete_facts_error(state, observed):
    if observed.get("size_bytes") is not None:
        return "只有完成状态携带完整大小"
    if state == int(_COMPLETION.UNCONFIRMED) and not isinstance(observed.get("last_error_json"), dict):
        return "未确认状态必须携带实际失败证据"
    return None


def checksum_decision_error(facts, support):
    if (facts.get("checksum_support") != int(_CHECKSUM.UNDETERMINED)
            or support not in (int(_CHECKSUM.SUPPORTED), int(_CHECKSUM.UNSUPPORTED))):
        return "摘要能力只能从未判定一次决定"
    return None


def checksum_value_error(facts, support, sha256):
    if sha256 is not None:
        if support != int(_CHECKSUM.SUPPORTED) or facts.get("completion_state") != int(_COMPLETION.COMPLETE):
            return "摘要只能在已完成的受支持文件上保存"
        if (not isinstance(sha256, str) or len(sha256) != 64
                or any(char not in "0123456789abcdef" for char in sha256)):
            return f"摘要必须是 64 位小写十六进制: {sha256!r}"
    return None

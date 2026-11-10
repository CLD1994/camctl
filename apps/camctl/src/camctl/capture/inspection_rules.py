"""分段检查对原片、原读取与媒体结果的资格判断。"""

from collections.abc import Mapping

from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError


def inspection_member_is_eligible(action_id, set_id, file_id, set_row, member, file_row):
    """同一动作的已固定、已归属且完整的原片才可作为检查输入。"""
    set_state = enum_for("segment_sets.state")
    role = enum_for("segment_members.role")
    ownership = enum_for("segment_members.ownership")
    completion = enum_for("segment_members.completion")
    file_role = enum_for("device_files.role")
    file_completion = enum_for("device_files.completion_state")
    presence = enum_for("device_files.presence_state")
    return not (
        set_row["action_id"] != action_id
        or set_row["state"] not in (int(set_state.MEMBERS_FIXED), int(set_state.ASSESSED))
        or member["set_id"] != set_id or member["file_id"] != file_id
        or member["role"] != int(role.ORIGINAL)
        or member["ownership"] != int(ownership.OWNED)
        or member["completion"] != int(completion.COMPLETE)
        or file_row["source_action_id"] != action_id
        or file_row["role"] != int(file_role.ORIGINAL)
        or file_row["completion_state"] != int(file_completion.COMPLETE)
        or file_row["presence_state"] != int(presence.PRESENT)
        or file_row["size_bytes"] is None
    )


def inspection_read_is_complete(inspection, copy, run, attempt):
    """核对检查副本及对应原读取已经成功，调用方另核对未决尝试。"""
    verified = enum_for("file_copies.verification_state")
    return not (
        copy["source_device_file_id"] != inspection["source_device_file_id"]
        or run["action_id"] != inspection["action_id"]
        or copy["source_size"] is None or copy["committed_bytes"] != copy["source_size"]
        or copy["target_sha256"] is None
        or copy["verification_state"] not in (int(verified.MATCHED), int(verified.SOURCE_CHECKSUM_UNAVAILABLE))
        or run["status"] != int(enum_for("operation_runs.status").SUCCEEDED)
        or not run["attempts_used"] or run["retry_wait_required"]
        or attempt["status"] != int(enum_for("operation_attempts.status").SUCCEEDED)
        or attempt["result_json"] is None or attempt["result_event_id"] is None
    )


def inspection_result_requires_read(state, document):
    """完成或工具已经执行的检查结果必须有可靠原读取。"""
    tool = document.get("tool")
    failure = document.get("failure")
    return (state == int(enum_for("segment_inspections.state").COMPLETED)
            or isinstance(tool, Mapping) and tool.get("ran") is True
            or isinstance(failure, Mapping) and failure.get("code") == "probe_failed")


def binding_read_error(document):
    """绑定失败只能来自原检查读取，保留原错误对象。"""
    failure = document.get("failure")
    error = failure.get("read_error") if isinstance(failure, Mapping) else None
    if not isinstance(error, Mapping) or error.get("code") != "device_binding_unavailable":
        return None
    if failure.get("code") != "read_failed":
        raise ConsistencyError("绑定读取失败必须归属于原检查读取")
    return error


def binding_result_blocked(action):
    """已取消或终态的动作不能继续新增绑定失败结果。"""
    return bool(action["cancel_requested"] or action["status"] != 2)

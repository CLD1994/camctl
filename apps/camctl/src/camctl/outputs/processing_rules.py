"""按录像处理的已保存阶段判断来源完成及原片输入需求。"""

from camctl.contracts.enums import enum_for
from camctl.contracts.json_values import is_json_integer
from camctl.contracts.values import ConsistencyError

_CHECK_STATE = enum_for("recording_processing.check_state")
_CHECK_DECISION = enum_for("recording_processing.check_decision")
_REPAIR_STATE = enum_for("recording_processing.repair_state")
_DISCARD_STATE = enum_for("recording_processing.discard_state")
_UNFINISHED_PROCESSING_CHECK = frozenset({int(_CHECK_STATE.RUNNING)})
_UNFINISHED_PROCESSING_REPAIR = frozenset(
    int(member) for member in _REPAIR_STATE if member.name in {"PENDING", "RUNNING"})
_UNFINISHED_PROCESSING_DISCARD = frozenset(
    int(member) for member in _DISCARD_STATE if member.name in {"PENDING", "RUNNING"})


def processing_completed(row) -> bool:
    if row is None:
        return True
    try:
        states = tuple(row[column] for column in ("check_state", "repair_state", "discard_state"))
        if not all(is_json_integer(value) for value in states):
            raise ValueError("处理状态必须是登记的整数编号")
        check_state, repair_state, discard_state = (
            enum(value) for enum, value in zip((_CHECK_STATE, _REPAIR_STATE, _DISCARD_STATE), states))
    except (KeyError, TypeError, ValueError) as error:
        raise ConsistencyError("来源处理状态缺失或无效") from error
    return (
        check_state not in _UNFINISHED_PROCESSING_CHECK
        and repair_state not in _UNFINISHED_PROCESSING_REPAIR
        and discard_state not in _UNFINISHED_PROCESSING_DISCARD
    )


def processing_requires_existing_input(processing) -> bool:
    """工具阶段是否证明原输入已经建立；False 不授予首次输入资格。"""
    try:
        check, repair = processing["check_state"], processing["repair_state"]
        if not is_json_integer(check) or not is_json_integer(repair):
            raise ValueError("处理阶段必须是登记的整数编号")
        check, repair = _CHECK_STATE(check), _REPAIR_STATE(repair)
    except (KeyError, TypeError, ValueError) as error:
        raise ConsistencyError("原录像的检查或修复阶段缺失或无法解释") from error
    return (check in (_CHECK_STATE.RUNNING, _CHECK_STATE.COMPLETED)
            or repair in (_REPAIR_STATE.RUNNING, _REPAIR_STATE.SUCCEEDED))


def internal_input_requirement(processing) -> str | None:
    """三类准备记录均无时的当前内部输入需求判定。

    返回 None 表示存在输入需求（独立检查或修复需求）；否则返回只
    读不授予的原因。工具阶段已证明输入存在、或检查需求仍在而修
    复已终局结束（2026-10-03 决策按不可解释状态处理）时抛一致性
    错误。检查已失败或未确认时不保留待执行修复的输入需求。
    """
    try:
        decision = _CHECK_DECISION(processing["check_decision"])
        check = _CHECK_STATE(processing["check_state"])
        repair = _REPAIR_STATE(processing["repair_state"])
        discard = _DISCARD_STATE(processing["discard_state"])
    except (KeyError, TypeError, ValueError) as error:
        raise ConsistencyError("原录像的处理决定或阶段缺失或无法解释") from error
    if processing_requires_existing_input(processing):
        raise ConsistencyError("原片工具处理已经开始，但原输入准备责任缺失")
    if discard in (_DISCARD_STATE.PENDING, _DISCARD_STATE.RUNNING):
        return "discard_active"
    if decision is _CHECK_DECISION.REQUIRED and check is _CHECK_STATE.NOT_PERFORMED:
        if repair in (_REPAIR_STATE.FAILED, _REPAIR_STATE.CANCELED):
            raise ConsistencyError("检查需求仍在而修复已终局结束，处理阶段组合不可解释")
        return None
    if repair is _REPAIR_STATE.PENDING:
        if check in (_CHECK_STATE.FAILED, _CHECK_STATE.UNCONFIRMED):
            return "input_need_ended"
        return None
    if (check in (_CHECK_STATE.FAILED, _CHECK_STATE.UNCONFIRMED)
            or repair in (_REPAIR_STATE.FAILED, _REPAIR_STATE.CANCELED)):
        return "input_need_ended"
    if decision is _CHECK_DECISION.UNDETERMINED or repair is _REPAIR_STATE.UNDETERMINED:
        return "input_undecided"
    return "input_not_needed"


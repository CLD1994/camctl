"""已保存录像处理状态决定来源完成和内部输入需求。"""

import pytest

from camctl.contracts.enums import enum_for
from camctl.contracts.values import ConsistencyError
from camctl.outputs.processing_rules import internal_input_requirement, processing_completed, processing_requires_existing_input
from camctl.outputs.sources import first_selection_qualification_error

CHECK = enum_for("recording_processing.check_state")
DECISION = enum_for("recording_processing.check_decision")
REPAIR = enum_for("recording_processing.repair_state")
DISCARD = enum_for("recording_processing.discard_state")


def _processing(**overrides):
    values = dict(check_state=int(CHECK.NOT_PERFORMED), check_decision=int(DECISION.REQUIRED),
                  repair_state=int(REPAIR.NOT_NEEDED), discard_state=int(DISCARD.NOT_NEEDED))
    values.update(overrides)
    return values


@pytest.mark.parametrize("field,value", [("check_state", CHECK.RUNNING), ("repair_state", REPAIR.PENDING),
    ("repair_state", REPAIR.RUNNING), ("discard_state", DISCARD.PENDING), ("discard_state", DISCARD.RUNNING)])
def test_unfinished_processing_blocks_source_selection(field, value):
    assert not processing_completed(_processing(**{field: int(value)}))
    assert processing_completed(_processing())
    assert processing_completed(None)


@pytest.mark.parametrize("changes,expected", [
    ({}, None),
    ({"discard_state": int(DISCARD.PENDING)}, "discard_active"),
    ({"check_decision": int(DECISION.NOT_NEEDED), "repair_state": int(REPAIR.PENDING)}, None),
    ({"check_decision": int(DECISION.NOT_NEEDED), "check_state": int(CHECK.FAILED), "repair_state": int(REPAIR.PENDING)}, "input_need_ended"),
    ({"check_decision": int(DECISION.UNDETERMINED), "repair_state": int(REPAIR.UNDETERMINED)}, "input_undecided"),
    ({"check_decision": int(DECISION.NOT_NEEDED)}, "input_not_needed"),
])
def test_processing_input_need_distinguishes_phases(changes, expected):
    assert internal_input_requirement(_processing(**changes)) == expected


def test_processing_rejects_invalid_or_already_started_facts():
    with pytest.raises(ConsistencyError, match="处理状态缺失或无效"):
        processing_completed(_processing(check_state=True))
    assert processing_requires_existing_input(_processing(check_state=int(CHECK.RUNNING)))
    with pytest.raises(ConsistencyError, match="工具处理已经开始"):
        internal_input_requirement(_processing(check_state=int(CHECK.RUNNING)))
    with pytest.raises(ConsistencyError, match="检查需求仍在"):
        internal_input_requirement(_processing(repair_state=int(REPAIR.FAILED)))


def _selection():
    kind, status = enum_for("actions.type"), enum_for("actions.status")
    owner = dict(type=int(kind.OBTAIN_ACTION_OUTPUTS), source_resolution_state=int(enum_for("actions.source_resolution_state").FIXED),
                 resolved_source_plan_id=7, status=int(status.RUNNING), cancel_requested=0, execution_started=1)
    source = dict(type=int(kind.CAMERA_RECORD), status=int(status.SUCCEEDED))
    return owner, source


def test_first_selection_checks_fixed_source_then_owner_then_source_terminal():
    owner, source = _selection()
    assert first_selection_qualification_error(owner=owner, source=source, source_plan_id=7) is None
    assert first_selection_qualification_error(owner=owner | {"resolved_source_plan_id": 8, "cancel_requested": 1}, source=source, source_plan_id=7) == "首次选择要求已固定的取回来源及同计划拍摄成员"
    assert first_selection_qualification_error(owner=owner | {"cancel_requested": True}, source=source, source_plan_id=7) == "选择固定要求取回动作执行中且未取消"
    assert first_selection_qualification_error(owner=owner, source=source | {"status": int(enum_for("actions.status").RUNNING)}, source_plan_id=7) == "选择固定要求来源动作已终态"

"""受理创建规则只消费已保存的动作和计划事实。"""

import pytest

from camctl.acceptance import definitions, storage_rules
from camctl.contracts.enums import enum_for
from camctl.contracts.workflow_errors import action_error_id


def _failed_action(**changes):
    return {
        "type": enum_for("actions.type").REPORT_STATUS,
        "name": "report",
        "input_fields_json": {"params": {}},
        "device_id": None,
        "scheduled_at": None,
        "group_name": None,
        "max_delay_ms": None,
        "status": 4,
        "execution_started": 0,
        "execution_spec_json": None,
        "effective_params_json": None,
        "driver_id": None,
        "error_code": action_error_id("action_validation_failed"),
        "error_details_json": {"issues": [{"field": "params", "reason": "required"}]},
        **changes,
    }


def test_registered_admission_failure_is_accepted_without_changing_facts():
    facts = _failed_action()
    before = dict(facts)
    assert storage_rules.validate_admitted_action(facts) is None
    assert facts == before


def test_admitted_action_with_execution_definition_is_accepted(monkeypatch):
    monkeypatch.setattr(definitions, "reconstruct_action_input", lambda row: {"params": {"scope": "full"}})
    facts = _failed_action(
        status=1,
        error_code=None,
        error_details_json=None,
        execution_spec_json={},
        input_fields_json={"params": {"scope": "full"}},
    )
    assert storage_rules.validate_admitted_action(facts) is None


def test_uninterpretable_execution_definition_is_rejected_first():
    with pytest.raises(ValueError, match="持久化动作执行定义不可解释"):
        storage_rules.validate_admitted_action(_failed_action(type=-1))


def test_registered_admission_error_requires_its_details():
    with pytest.raises(ValueError, match="详情不符合登记结构"):
        storage_rules.validate_admitted_action(_failed_action(error_details_json={}))


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"execution_started": 1}, "受理失败动作必须 execution_started=0"),
        ({"error_code": None}, "受理失败动作必须 execution_started=0"),
        ({"execution_spec_json": {}}, "受理失败动作不携带执行定义"),
        ({"error_code": action_error_id("device_start_failed")}, "首次受理失败必须使用受理阶段错误"),
        ({"status": 1, "execution_spec_json": None}, "可受理动作必须携带执行定义"),
    ],
)
def test_action_rules_reject_contradictory_facts_after_definition_check(monkeypatch, changes, message):
    # 定义读取器本身也会拒绝其中部分矛盾；此处单独检查后续规则。
    monkeypatch.setattr(storage_rules, "read_action_spec", lambda values: None)
    with pytest.raises(ValueError, match=message):
        storage_rules.validate_admitted_action(_failed_action(**changes))


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [((), 1), ((4,), 3), ((4, 4), 3), ((4, 1), 1), ((2,), 1)],
)
def test_initial_plan_status_only_completes_when_all_existing_actions_failed(statuses, expected):
    assert storage_rules.validate_initial_plan_status(expected, statuses) is None
    with pytest.raises(ValueError, match=f"计划状态与动作聚合不符: 期望 {expected}"):
        storage_rules.validate_initial_plan_status(1 if expected == 3 else 3, statuses)


def test_initial_plan_rule_rejects_an_unmaterialized_iterator():
    def statuses():
        raise AssertionError("不应迭代未物化的状态")
        yield 4

    with pytest.raises(TypeError, match="已物化的元组"):
        storage_rules.validate_initial_plan_status(3, statuses())


def test_source_rule_checks_owner_and_members_without_reading():
    rules = storage_rules
    assert rules.validate_admitted_source_owners((3, 3), 3, 3, True) == 3
    rules.validate_admitted_source_owner_type(4, frozenset({4, 5}))
    rules.validate_admitted_source_member(11, 11)
    with pytest.raises(rules.AcceptanceStorageRuleError, match="只能保存一个"):
        rules.validate_admitted_source_owners((3, 4), 3, 3, True)
    with pytest.raises(rules.AcceptanceStorageRuleError, match="没有完整覆盖"):
        rules.validate_admitted_source_member(11, 12)
    with pytest.raises(rules.AcceptanceStorageRuleError, match="受理零成员无需空事件"):
        rules.validate_admitted_source_owners((), 3, 3, True)
    with pytest.raises(rules.AcceptanceStorageRuleError, match="来源计划"):
        rules.validate_fixed_source({"source_resolution_state": 2, "resolved_source_plan_id": None})


def test_diagnostic_and_ack_rules_use_actual_saved_facts():
    rules = storage_rules
    rules.validate_diagnostic_key(True)
    rules.validate_diagnostic_errors([{"error": "bad"}])
    with pytest.raises(rules.AcceptanceStorageRuleError, match="非空错误列表"):
        rules.validate_diagnostic_errors([])
    before = {"acknowledged_wm": 10}
    after = {"acknowledged_wm": 20, "acknowledged_report_id": 5}
    rules.validate_acknowledged_watermark(before, after, {"to_wm": 20})
    with pytest.raises(rules.AcceptanceStorageRuleError, match="严格提高"):
        rules.validate_acknowledged_watermark({"acknowledged_wm": 20}, after, {"to_wm": 20})
    sync = {"action_id": 2, "from_wm": 10, "started_boundary_event_id": 8}
    report = {"from_wm": 10, "to_wm": 20, "frozen_event_id": 8}
    result = {"ack_report_id": 5, "ended_event_id": 15}
    rules.validate_acknowledged_sync(3, 15, result, sync, report)
    with pytest.raises(rules.AcceptanceStorageRuleError, match="未满足同步起点"):
        rules.validate_acknowledged_sync(3, 15, result, sync, {**report, "frozen_event_id": 7})
    with pytest.raises(rules.AcceptanceStorageRuleError, match="必须是本事件"):
        rules.validate_acknowledged_sync(3, 16, result, sync, report)

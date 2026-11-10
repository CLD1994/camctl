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

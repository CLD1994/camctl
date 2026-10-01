"""A2 公共 Schema、静态目录与分层受理规则的组件集成测试。

期望独立来自输入契约与数据类型规格：公共结构错误整份拒绝，单动
作自身错误按动作失败；整数识别、倍数与范围按精确数值判断。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from camctl.acceptance.rules import (
    ActionValidation,
    BodyDecision,
    validate_capture_params,
    validate_new_body,
)
from camctl.acceptance.schema import RuleError

CAMERA_DEFINITION = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "type": {"const": "single_shot"},
        "shots": {"type": "integer", "minimum": 1, "maximum": 10},
        "interval_s": {"type": "number", "multipleOf": Decimal("0.1")},
    },
    "required": ["type"],
    "additionalProperties": False,
}


class StubCatalog:
    """受静态目录端口约束的替身：单相机设备与定义。"""

    def __init__(self) -> None:
        self.devices = {"cam-1", "cam-2"}

    def action_types(self):
        return frozenset(
            {"camera_take_photo", "camera_record", "camera_timelapse"}
        )

    def device_exists(self, device_id: str) -> bool:
        return device_id in self.devices

    def driver_id(self, device_id):
        return "camctl-adb" if device_id in self.devices else None

    def device_supports(self, device_id, action_type):
        return self.device_exists(device_id) and action_type.startswith("camera_")

    def parameter_definition(self, device_id: str, action_type: str, parameter_type: str):
        if device_id not in self.devices or action_type not in self.action_types():
            return None
        from camctl.acceptance.ports import ParameterDefinition

        return ParameterDefinition(
            schema=CAMERA_DEFINITION,
            defaults={"shots": 1, "interval_s": Decimal("0.5")},
        )


def _body(actions: list, *, request_id: str = "42") -> dict:
    return {
        "request_id": request_id,
        "created_at": "2026-01-15 08:00:00",
        "name": "plan",
        "actions": actions,
    }


def _camera_action(name: str = "shoot", *, device: str = "cam-1", **overrides):
    action = {
        "name": name,
        "type": "camera_take_photo",
        "device_id": device,
        "scheduled_at": "2026-01-15 09:00:00",
        "params": {"type": "single_shot"},
        "policy": {"max_delay_ms": 1000},
    }
    action.update(overrides)
    return action


class TestPreciseNumbers:
    def test_integral_decimals_satisfy_integer(self) -> None:
        validation = validate_capture_params(
            {"type": "single_shot", "shots": Decimal("1.0")},
            _definition(),
        )
        assert validation.ok, validation.failure

    def test_exponent_form_integer_satisfies(self) -> None:
        validation = validate_capture_params(
            {"type": "single_shot", "shots": Decimal("1e0")},
            _definition(),
        )
        assert validation.ok, validation.failure

    def test_near_integer_long_decimal_rejected(self) -> None:
        validation = validate_capture_params(
            {"type": "single_shot", "shots": Decimal("1.0000000000000001")},
            _definition(),
        )
        assert not validation.ok

    def test_multiple_of_uses_exact_fraction(self) -> None:
        ok = validate_capture_params(
            {"type": "single_shot", "interval_s": Decimal("0.3")},
            _definition(),
        )
        assert ok.ok, ok.failure
        bad = validate_capture_params(
            {"type": "single_shot", "interval_s": Decimal("0.35")},
            _definition(),
        )
        assert not bad.ok

    def test_bool_and_string_numbers_rejected(self) -> None:
        for raw in ({"type": "single_shot", "shots": True}, {"type": "single_shot", "shots": "3"}):
            assert not validate_capture_params(raw, _definition()).ok

    def test_defaults_applied_and_original_preserved(self) -> None:
        raw = {"type": "single_shot"}
        validation = validate_capture_params(raw, _definition())
        assert validation.ok
        assert validation.effective_params == {
            "type": "single_shot",
            "shots": 1,
            "interval_s": Decimal("0.5"),
        }
        assert raw == {"type": "single_shot"}


def _definition():
    from camctl.acceptance.ports import ParameterDefinition

    return ParameterDefinition(
        schema=CAMERA_DEFINITION,
        defaults={"shots": 1, "interval_s": Decimal("0.5")},
    )


class TestRuleErrors:
    def test_invalid_schema_is_rule_error(self) -> None:
        from camctl.acceptance.ports import ParameterDefinition

        broken = ParameterDefinition(
            schema={"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "no-such-type"},
            defaults={},
        )
        with pytest.raises(RuleError):
            validate_capture_params({"x": 1}, broken)

    def test_missing_required_definition_for_supported_action(self) -> None:
        catalog = StubCatalog()
        catalog.devices = {"cam-1"}
        # 目录声明支持但缺少定义属规则错误，不是用户参数错误。
        class NoDefinition(StubCatalog):
            def parameter_definition(self, device_id, action_type, parameter_type):
                raise RuleError("已声明参数定义读取失败")

        with pytest.raises(RuleError):
            validate_new_body(
                _body([_camera_action()]), NoDefinition()
            )


class TestBodyDecisions:
    def test_valid_camera_plan_accepted(self) -> None:
        decision = validate_new_body(_body([_camera_action("a"), _camera_action("b")]), StubCatalog())
        assert not decision.is_whole_rejection
        assert [item.name for item in decision.actions if item.ok] == ["a", "b"]

    def test_duplicate_names_whole_rejection(self) -> None:
        decision = validate_new_body(
            _body([_camera_action("dup"), _camera_action("dup")]), StubCatalog()
        )
        assert decision.is_whole_rejection

    def test_unknown_action_type_whole_rejection(self) -> None:
        action = _camera_action()
        action["type"] = "teleport"
        decision = validate_new_body(_body([action]), StubCatalog())
        assert decision.is_whole_rejection

    def test_mixed_errors_stay_whole_rejection(self) -> None:
        # 未知动作、名称重复与本动作参数错误混合：整份拒绝优先。
        bad_params = _camera_action("bad")
        bad_params["params"] = {"type": "single_shot", "shots": 99}
        decision = validate_new_body(
            _body([_camera_action("dup"), _camera_action("dup"), bad_params]),
            StubCatalog(),
        )
        assert decision.is_whole_rejection

    def test_order_independence(self) -> None:
        bad = _camera_action("bad")
        bad["params"] = {"type": "single_shot", "shots": 99}
        good = _camera_action("good")
        first = validate_new_body(_body([bad, good]), StubCatalog())
        second = validate_new_body(_body([good, bad]), StubCatalog())
        assert {item.name for item in first.actions} == {item.name for item in second.actions}
        assert {item.name for item in first.actions if not item.ok} == {"bad"}

    def test_unknown_device_is_action_failure(self) -> None:
        decision = validate_new_body(
            _body([_camera_action("shoot", device="cam-x")]), StubCatalog()
        )
        assert not decision.is_whole_rejection
        assert [item.name for item in decision.actions if not item.ok] == ["shoot"]

    def test_semantic_date_is_action_failure(self) -> None:
        action = _camera_action()
        action["scheduled_at"] = "2026-02-30 09:00:00"
        decision = validate_new_body(_body([action]), StubCatalog())
        assert not decision.is_whole_rejection
        assert not decision.actions[0].ok

    def test_invalid_created_at_is_whole_rejection(self) -> None:
        body = _body([_camera_action()])
        body["created_at"] = "2026-13-01 08:00:00"
        decision = validate_new_body(body, StubCatalog())
        assert decision.is_whole_rejection

    def test_invalid_public_structure_is_whole_rejection(self) -> None:
        body = _body([_camera_action()])
        body["extra_field"] = 1
        decision = validate_new_body(body, StubCatalog())
        assert decision.is_whole_rejection

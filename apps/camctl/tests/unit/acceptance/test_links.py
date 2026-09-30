"""A3 完整本计划关联与执行定义的单元测试。

期望独立来自受理与自动预览契约：先建全计划名称索引再解析引用；
自动预览冲突使全部冲突取回失败；跨计划来源不在受理时查询。
"""

from __future__ import annotations

import pytest

from camctl.acceptance.links import PlanIdentities, prepare_plan
from camctl.acceptance.rules import validate_new_body

from .helpers import StubCatalog, auto_preview, camera_action, manual_obtain


def _prepare(actions: list[dict]):
    body = {
        "request_id": "42",
        "created_at": "2026-01-15 08:00:00",
        "name": "plan",
        "actions": actions,
    }
    decision = validate_new_body(body, StubCatalog())
    assert not decision.is_whole_rejection, decision.rejection_reasons
    names = [action["name"] for action in actions]
    identities = PlanIdentities(
        plan_id=7,
        action_ids={name: 100 + index for index, name in enumerate(names)},
    )
    return prepare_plan(decision, identities)


class TestAutoPreviewConflicts:
    def test_all_duplicate_previews_fail(self) -> None:
        prepared = _prepare(
            [
                camera_action("shoot-a"),
                camera_action("shoot-b"),
                auto_preview("p-a", "shoot-a"),
                auto_preview("p-a2", "shoot-a"),
                auto_preview("p-b", "shoot-b"),
                manual_obtain("m-1", "shoot-a"),
            ]
        )
        failed = {a.name: a for a in prepared.actions if not a.ok}
        assert sorted(failed) == ["p-a", "p-a2"]
        assert failed["p-a"].failure_code == "duplicate_auto_preview"
        assert "shoot-a" in failed["p-a"].failure_reason
        # 拍摄、另一来源的唯一自动取回与手动取回不受冲突影响。
        assert [a.name for a in prepared.actions if a.ok] == [
            "shoot-a",
            "shoot-b",
            "p-b",
            "m-1",
        ]

    def test_manual_obtain_and_camera_unaffected_by_conflict(self) -> None:
        prepared = _prepare(
            [
                camera_action("shoot"),
                auto_preview("dup-1", "shoot"),
                auto_preview("dup-2", "shoot"),
                manual_obtain("manual", "shoot"),
            ]
        )
        assert [a.name for a in prepared.actions if a.ok] == ["shoot", "manual"]


class TestInPlanReferences:
    def test_forward_reference_resolves(self) -> None:
        # 取回出现在被引用拍摄之前：全计划索引先建立，引用合法。
        prepared = _prepare(
            [
                manual_obtain("early", "late-shot"),
                camera_action("late-shot"),
            ]
        )
        by_name = {a.name: a for a in prepared.actions}
        assert by_name["early"].ok
        assert by_name["early"].in_plan_dependencies == ("late-shot",)

    def test_missing_source_name_fails_the_obtain(self) -> None:
        prepared = _prepare([manual_obtain("orphan", "no-such-shot")])
        action = prepared.actions[0]
        assert not action.ok
        assert "no-such-shot" in action.failure_reason

    def test_cross_plan_sources_not_queried_at_acceptance(self) -> None:
        # 按实例 ID 与按组引用属跨计划来源：不在受理时查询或失败。
        by_id = manual_obtain("by-id", None)
        by_id["params"] = {
            "source": {"action_instance_id": "500"},
            "output_ids": ["900"],
            "purpose": "manual",
        }
        by_group = manual_obtain("by-group", None)
        by_group["params"] = {"source": {"group": "tripod"}, "purpose": "manual"}
        prepared = _prepare([by_id, by_group])
        assert all(action.ok for action in prepared.actions)
        assert all(not action.in_plan_dependencies for action in prepared.actions)


class TestGroups:
    def test_failed_camera_in_group_still_source(self) -> None:
        # 组内拍摄自身失败仍按类型作为组来源成员。
        broken = camera_action("broken-shot", group="tripod")
        broken["params"] = {"type": "single_shot", "shots": 99}
        by_group = manual_obtain("group-obtain", None)
        by_group["params"] = {"source": {"group": "tripod"}, "purpose": "manual"}
        prepared = _prepare(
            [
                camera_action("good-shot", group="tripod"),
                broken,
                by_group,
            ]
        )
        by_name = {a.name: a for a in prepared.actions}
        assert by_name["group-obtain"].ok
        assert by_name["broken-shot"].ok is False
        assert by_name["broken-shot"].group_name == "tripod"

    def test_obtain_with_group_field_is_whole_rejection(self) -> None:
        # 公共 Schema 禁止取回填写 group：整份拒绝，不存在成员关系。
        from camctl.acceptance.rules import validate_new_body
        from .helpers import StubCatalog

        grouped_obtain = manual_obtain("grouped-obtain", "shoot")
        grouped_obtain["group"] = "tripod"
        decision = validate_new_body(
            {
                "request_id": "42",
                "created_at": "2026-01-15 08:00:00",
                "name": "plan",
                "actions": [camera_action("shoot", group="tripod"), grouped_obtain],
            },
            StubCatalog(),
        )
        assert decision.is_whole_rejection


class TestExecutionDefinitions:
    def test_effective_params_and_identity_preserved(self) -> None:
        prepared = _prepare([camera_action("shoot")])
        action = prepared.actions[0]
        assert action.ok
        assert action.action_id == 100
        assert action.effective_params["shots"] == 1
        assert action.raw_fields["params"] == {"type": "single_shot"}
        assert action.scheduled_at_micros is not None

    def test_failed_action_keeps_original_fields(self) -> None:
        broken = camera_action("broken")
        broken["params"] = {"type": "single_shot", "shots": 99}
        prepared = _prepare([broken])
        action = prepared.actions[0]
        assert not action.ok
        assert action.raw_fields["params"] == {"type": "single_shot", "shots": 99}
        assert action.effective_params is None

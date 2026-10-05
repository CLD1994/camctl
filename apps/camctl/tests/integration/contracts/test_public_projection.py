"""K4 公开投影与真实协议登记的集成测试。

期望独立来自报告字段依赖登记：公开字段从已保存事实计算，不查
询设备或当前库；内部变化不产生公开变化；缺事实为规则错误。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from camctl.contracts.public_projection import (
    OMIT,
    ProjectionInput,
    PublicProjectionError,
    project_public,
    public_changed,
)


def _plan_facts(plan_id: int = 1, **columns) -> ProjectionInput:
    row = {
        "id": plan_id,
        "request_id": 42,
        "name": "morning",
        "created_at": 1_736_899_200_000_000,
        "status": 1,
    }
    row.update(columns)
    return ProjectionInput(
        entity="plan",
        root_id=plan_id,
        tables={"plans": {plan_id: row}},
        selected_entities={},
    )


class TestProjectPublicPlan:
    def test_plan_fields_encoded_per_contract(self) -> None:
        fragment = project_public(_plan_facts())
        # 对象 ID 规范十进制字符串；时间秒级 UTC。
        assert fragment["plan_instance_id"] == "1"
        assert fragment["request_id"] == "42"
        assert fragment["created_at"] == "2025-01-15 00:00:00"
        assert fragment["name"] == "morning"
        assert fragment["status"] == "pending"

    def test_status_enum_maps_public_values(self) -> None:
        assert project_public(_plan_facts(status=2))["status"] == "running"
        assert project_public(_plan_facts(status=3))["status"] == "completed"

    def test_actions_entities_omitted_when_selection_empty(self) -> None:
        fragment = project_public(_plan_facts())
        assert "actions" not in fragment  # empty: omit

    def test_actions_selected_ids_encoded_ordered(self) -> None:
        def action_row(action_id: int) -> dict:
            return {
                "id": action_id, "plan_id": 1, "input_index": action_id, "name": f"a{action_id}",
                "type": 4, "device_id": None, "scheduled_at": None, "group_name": None,
                "status": 1, "execution_started": 0, "cancel_requested": 0,
                "error_code": None, "error_details_json": None, "input_fields_json": {},
                "effective_params_json": None, "driver_id": None, "max_delay_ms": None,
            }

        facts = ProjectionInput(
            entity="plan",
            root_id=1,
            tables={
                "plans": {1: {"id": 1, "request_id": 1, "name": "p", "created_at": 0, "status": 1}},
                "actions": {1: action_row(1), 2: action_row(2)},
            },
            selected_entities={"action": {2: {}, 1: {}}},
        )
        fragment = project_public(facts)
        assert [a["action_instance_id"] for a in fragment["actions"]] == ["2", "1"]

    def test_nested_selection_scopes_children_to_their_parent(self) -> None:
        """入选树沿实体层级逐层限定：动作只携带自己的产物。"""
        def action_row(action_id: int) -> dict:
            return {
                "id": action_id, "plan_id": 1, "input_index": action_id, "name": f"a{action_id}",
                "type": 1, "device_id": "cam-1", "scheduled_at": 1_736_899_200_000_000,
                "group_name": None, "status": 1, "execution_started": 0, "cancel_requested": 0,
                "error_code": None, "error_details_json": None, "input_fields_json": {},
                "effective_params_json": {}, "driver_id": "camctl-adb", "max_delay_ms": 1000,
            }

        def output_row(output_id: int, action_id: int) -> dict:
            return {
                "id": output_id, "source_action_id": action_id, "kind": 1,
                "original_name": f"shot-{output_id}.jpg", "media_type": "image/jpeg",
                "device_file_id": None, "intermediate_file_id": None,
                "original_output_id": None, "original_batch_file_id": None,
                "availability": 1, "media_json": {}, "error_code": None, "error_json": None,
                "preview_of_output_id": None, "cleanup_status": 1, "cleanup_error_json": None,
            }

        facts = ProjectionInput(
            entity="plan",
            root_id=1,
            tables={
                "plans": {1: {"id": 1, "request_id": 1, "name": "p", "created_at": 0, "status": 1}},
                "actions": {1: action_row(1), 2: action_row(2)},
                "outputs": {9: output_row(9, 1), 12: output_row(12, 2)},
            },
            selected_entities={"action": {2: {}, 1: {"output": {9: {}}}}},
        )
        fragment = project_public(facts)
        by_id = {a["action_instance_id"]: a for a in fragment["actions"]}
        assert [o["output_id"] for o in by_id["1"]["outputs"]] == ["9"]
        assert "outputs" not in by_id["2"]  # 入选作用域不跨父对象


    def test_unknown_enum_member_is_rule_error(self) -> None:
        with pytest.raises(PublicProjectionError):
            project_public(_plan_facts(status=99))

    def test_missing_root_fact_is_rule_error(self) -> None:
        with pytest.raises(PublicProjectionError):
            project_public(
                ProjectionInput(entity="plan", root_id=7, tables={"plans": {}}, selected_entities={})
            )


class TestProjectPublicAction:
    def _action_facts(self, action_id: int = 5, **columns) -> ProjectionInput:
        row = {
            "id": action_id,
            "plan_id": 1,
            "input_index": 0,
            "name": "shoot",
            "type": 1,
            "device_id": "cam-1",
            "scheduled_at": 1_736_899_200_000_000,
            "group_name": None,
            "status": 1,
            "execution_started": 0,
            "cancel_requested": 0,
            "error_code": None,
            "error_details_json": None,
            "input_fields_json": {},
            "effective_params_json": {"quality": Decimal("1.5")},
            "driver_id": "camctl-adb",
            "max_delay_ms": 1000,
        }
        row.update(columns)
        return ProjectionInput(
            entity="action",
            root_id=action_id,
            tables={"actions": {action_id: row}},
            selected_entities={},
        )

    def test_action_type_enum(self) -> None:
        fragment = project_public(self._action_facts())
        assert fragment["type"] == "camera_take_photo"

    def test_error_field_requires_registered_code(self) -> None:
        details = {"issues":[{"field":"actions[0].params","reason":"required"}]}
        fragment = project_public(self._action_facts(status=4, error_code=1, error_details_json=details))
        assert fragment["error"]["code"] == "action_validation_failed"
        assert fragment["error"]["details"] == details

    def test_no_error_field_without_code(self) -> None:
        fragment = project_public(self._action_facts())
        assert "error" not in fragment

    def test_json_column_decoded_structured(self) -> None:
        fragment = project_public(self._action_facts())
        assert fragment["effective_params"] == {"quality": Decimal("1.5")}


class TestNestedProjectionRelations:
    """嵌套投影沿自身声明的关联建立子上下文，跨表列按关联行取值。"""

    def _output_facts(self, output_row, tables_extra=None):
        tables = {
            "outputs": {9: output_row},
            **(tables_extra or {}),
        }
        return ProjectionInput(entity="output", root_id=9, tables=tables, selected_entities={})

    def _output_row(self, **columns):
        row = {
            "id": 9, "source_action_id": 1, "kind": 1, "original_name": "s.jpg",
            "media_type": "image/jpeg", "device_file_id": None, "intermediate_file_id": None,
            "original_output_id": None, "original_batch_file_id": None, "availability": 1,
            "media_json": {}, "error_code": None, "error_json": None,
            "preview_of_output_id": None, "cleanup_status": 1, "cleanup_error_json": None,
        }
        row.update(columns)
        return row

    def test_checksum_reads_referenced_device_file(self):
        digest = "a" * 64
        facts = self._output_facts(
            self._output_row(device_file_id=4),
            {"device_files": {4: {"id": 4, "size_bytes": 12, "sha256": digest}}},
        )
        fragment = project_public(facts)
        assert fragment["checksum"] == {"status": "available", "sha256": digest}
        assert fragment["size"] == 12

    def test_checksum_reads_referenced_host_file(self):
        digest = "b" * 64
        facts = self._output_facts(
            self._output_row(intermediate_file_id=7),
            {"intermediate_files": {7: {"id": 7, "size_bytes": 30, "sha256": digest}}},
        )
        fragment = project_public(facts)
        assert fragment["checksum"] == {"status": "available", "sha256": digest}
        assert fragment["size"] == 30

    def test_missing_referenced_file_facts_is_rule_error(self):
        facts = self._output_facts(self._output_row(device_file_id=4))
        with pytest.raises(PublicProjectionError):
            project_public(facts)

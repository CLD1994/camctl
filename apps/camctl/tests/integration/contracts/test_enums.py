"""真实包内整数登记、编号及公共 Schema 的组合验证。"""

from __future__ import annotations

import pytest

from camctl.contracts.enums import (
    assert_public_contracts,
    decode_member,
    encode_member,
    enum_for,
    public_text,
)

class TestRegistryEnums:
    def test_member_values_follow_registry(self) -> None:
        # 独立预期：来源为编号一览文档中的固定成员与编号。
        action_status = enum_for("actions.status")
        assert [(member.name, int(member)) for member in action_status] == [
            ("PENDING", 1),
            ("RUNNING", 2),
            ("SUCCEEDED", 3),
            ("FAILED", 4),
            ("EXPIRED", 5),
            ("CANCELED", 6),
        ]
        plan_status = enum_for("plans.status")
        assert [(member.name, int(member)) for member in plan_status] == [
            ("PENDING", 1),
            ("RUNNING", 2),
            ("COMPLETED", 3),
        ]
        action_type = enum_for("actions.type")
        assert int(action_type["CAMERA_TAKE_PHOTO"]) == 1
        assert int(action_type["REPORT_STATUS"]) == 7

    def test_unknown_code_is_rejected(self) -> None:
        action_status = enum_for("actions.status")
        with pytest.raises(ValueError):
            action_status(99)

    def test_cross_enum_mixing_is_rejected(self) -> None:
        action_status = enum_for("actions.status")
        with pytest.raises(ValueError):
            encode_member("plans.status", action_status.RUNNING)
        with pytest.raises(ValueError):
            decode_member("plans.status", action_status.RUNNING)

    def test_decode_and_round_trip(self) -> None:
        member = decode_member("actions.status", 2)
        assert member.name == "RUNNING"
        assert encode_member("actions.status", member) == 2

    def test_unknown_column_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            enum_for("plans.nonexistent")

    def test_public_text_encoding(self) -> None:
        assert public_text(enum_for("plans.status").COMPLETED) == "completed"
        assert public_text(enum_for("actions.type").CAMERA_RECORD) == "camera_record"
        with pytest.raises(ValueError):
            public_text("not-an-enum-member")  # type: ignore[arg-type]

    def test_public_contracts_agree_with_schema(self) -> None:
        # 生成的映射与权威资源一致：内部成员的小写名称集合
        # 必须与公共 Schema 登记的文本枚举完全相同。
        assert_public_contracts()

    def test_registry_itself_is_well_formed(self) -> None:
        # 独立核对登记的成员编号互不冲突且为正整数。
        from camctl.contracts.enums import load_registry

        registry = load_registry()
        combined = {**registry["enums"], **registry["json_enums"]}
        assert "actions.status" in combined
        for column, definition in combined.items():
            codes = list(definition["members"].values())
            assert all(isinstance(code, int) and not isinstance(code, bool) and code > 0 for code in codes)
            assert len(codes) == len(set(codes)), column

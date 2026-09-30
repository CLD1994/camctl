"""R4 确定性分批编码的单元测试。

同 H 的同一事实集合重编码字节一致；字段顺序与省略由投影决定；
数值精确（Decimal 直出不经 float）；整份文档通过公共 Schema。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from camctl.reporting.encoding import encode_report, ReportDocument


def _facts() -> dict:
    return {
        "plans": {
            1: {
                "id": 1,
                "request_id": 42,
                "name": "morning",
                "created_at": 1_736_899_200_000_000,
                "status": 2,
            }
        },
        "actions": {
            5: {
                "id": 5, "plan_id": 1, "input_index": 0, "name": "shoot",
                "type": 1, "device_id": "cam-1",
                "scheduled_at": 1_736_899_200_000_000, "group_name": None,
                "status": 1, "execution_started": 1, "cancel_requested": 0,
                "error_code": None, "error_details_json": None,
                "input_fields_json": {
                    "policy": {"max_delay_ms": 1000},
                    "params": {"type": "single_shot", "quality": Decimal("1.50")},
                },
                "effective_params_json": {"type": "single_shot", "quality": Decimal("1.50")},
                "driver_id": "camctl-adb", "max_delay_ms": 1000,
            }
        },
    }


def _document() -> ReportDocument:
    return ReportDocument(
        report_id="7",
        from_wm=0,
        to_wm=5,
        plans=(("plan", 1, ("action", (5,))),),
        diagnostics=(),
    )


class TestDeterministicEncoding:
    def test_same_facts_same_bytes(self) -> None:
        first = encode_report(_document(), _facts())
        second = encode_report(_document(), _facts())
        assert first == second
        assert first.endswith(b"\n")

    def test_json_parses_and_passes_schema(self) -> None:
        import json

        from camctl.contracts.schemas import validate_document

        payload = json.loads(encode_report(_document(), _facts()))
        validate_document("protocol/status-report.schema.json", payload)
        assert payload["report_id"] == "7"
        assert payload["from_wm"] == 0
        assert payload["to_wm"] == 5
        plan = payload["plans"][0]
        assert plan["plan_instance_id"] == "1"
        assert plan["status"] == "running"
        action = plan["actions"][0]
        assert action["action_instance_id"] == "5"
        assert action["type"] == "camera_take_photo"

    def test_decimal_precision_preserved(self) -> None:
        import json

        payload = json.loads(encode_report(_document(), _facts()))
        action = payload["plans"][0]["actions"][0]
        assert action["effective_params"]["quality"] == Decimal("1.50")
        assert action["policy"] == {"max_delay_ms": 1000}
        assert b"1.50" in encode_report(_document(), _facts())

    def test_batching_keeps_identical_bytes(self) -> None:
        # 逐批输出与一次性输出同字节（分批只是生产组织，不改内容）。
        whole = encode_report(_document(), _facts())
        parts = list(iter_report_batches(_document(), _facts(), batch_size=1))
        assert b"".join(parts) == whole


def iter_report_batches(document, facts, *, batch_size: int):
    """测试帮助：按实体分批产出与整体一致的字节片段。"""
    from camctl.reporting.encoding import iter_encoded_batches

    yield from iter_encoded_batches(document, facts, batch_size=batch_size)

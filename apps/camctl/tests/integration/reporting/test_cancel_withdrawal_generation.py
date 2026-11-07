"""报告生成器与真实投影、Schema、编码及文件交付的组合。

历史读取端口提供固定 H 的内存事实；不验证 SQLite 历史恢复。
"""

import json
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from camctl.contracts.enums import enum_for
from camctl.contracts.history_values import HistoryBoundary
from camctl.contracts.pages import Page
from camctl.contracts.schemas import validate_document
from camctl.history.report_action import ReportHistoryHandle
from camctl.history.report_selection import ReportEntitySelection
from camctl.history.snapshots import SnapshotRow
from camctl.persistence.repositories.history import HistoryRepository
from camctl.reporting.generation import GenerationSpec, generate_report_file


@pytest.mark.parametrize("target_type,withdrawals", [
    ("OBTAIN_ACTION_OUTPUTS", []),
    ("OBTAIN_ACTION_OUTPUTS", [
        {"delivery_id": "31", "status": "withdrawn"},
        {"delivery_id": "32", "status": "not_retractable"},
    ]),
    ("CAMERA_TIMELAPSE", None),
    ("CAMERA_TAKE_PHOTO", None),
])
def test_generated_cancel_report_projects_only_obtain_withdrawals(
    tmp_path, target_type, withdrawals,
):
    boundary = HistoryBoundary(1, 1)
    repo = create_autospec(HistoryRepository, instance=True)
    repo.frozen_registration.return_value = SimpleNamespace(
        from_wm=0, to_wm=1, boundary=boundary)
    selection = create_autospec(ReportEntitySelection, instance=True)
    selection.instance_id = "instance"
    selection.__enter__.return_value = selection
    selection.read_page.side_effect = lambda entity, **kwargs: Page(
        {"plan": (1,), "action": (2,)}.get(entity, ()), None)
    repo.open_report_selection.return_value = selection
    plan = {"id": 1, "request_id": 10, "name": "取消计划",
            "created_at": 0, "status": 2}
    target = {"id": 3, "type": int(enum_for("actions.type")[target_type])}
    repo.restore_primary_at_h.side_effect = lambda entity, identity, *args, **kwargs: (
        plan if entity == "plan" else target)
    repo.related_primary_rows_at_h.return_value = ()
    repo.status_history.return_value = [
        {"status": 1, "changed_at": 0, "clock_status": 1},
        {"status": 2, "changed_at": 1, "clock_status": 1},
    ]
    handle = create_autospec(ReportHistoryHandle, instance=True)
    handle.action_id = 2
    handle.__enter__.return_value = handle
    handle.primary.return_value = {
        "id": 2, "name": "取消取回", "type": int(enum_for("actions.type").CANCEL_TASK),
        "status": 2, "execution_started": 1, "device_id": None,
        "scheduled_at": None, "group_name": None, "effective_params_json": None,
        "input_fields_json": '{"params":{"target":{"action_instance_id":"3"}}}',
        "error_code": None,
    }
    handle.active_result_ref.return_value = None
    item = SnapshotRow("cancel_items", 4, {
        "id": 4, "action_id": 2, "target_action_id": 3, "status": 2,
        "cancellation_effect": 2, "error_code": None,
    })
    members = tuple(SnapshotRow("cancel_delivery_items", index, {
        "id": index, "cancel_item_id": 4, "delivery_id": int(value["delivery_id"]),
        "status": int(enum_for("cancel_delivery_items.status")[value["status"].upper()]),
        "error_code": None,
    }) for index, value in enumerate(withdrawals or (), start=5))
    handle.related_rows.side_effect = lambda table, *args: iter({
        "cancel_items": (item,), "cancel_delivery_items": members,
    }.get(table, ()))
    repo.open_report_action.return_value = handle
    spec = GenerationSpec(tmp_path / "unused.db", 1, 0, 1, 1, tmp_path / "report.json")

    generated = generate_report_file(spec, repository=repo, buffer_size=7)

    document = json.loads(generated.path.read_bytes())
    validate_document("protocol/status-report.schema.json", document)
    [actual] = document["plans"][0]["actions"][0]["result"]["items"]
    expected = {"action_instance_id": "3", "status": "running",
                "cancellation_effect": "applied"}
    if withdrawals is not None:
        expected["withdrawals"] = withdrawals
    assert actual == expected

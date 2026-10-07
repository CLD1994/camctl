"""I5 清理与取消组合跨组件验收：删除效果未知保留原请求结果。

真实 camctl CLI 子进程经部署装配桥接入受契约约束的设备替身：删除
调用持续失败且查询确认文件仍在时，清理按删除预算耗尽失败，拍摄
动作的成功事实、产物登记与已发布报告不被改写；随后另一个清理请
求对同一产物精确删除成功，旧请求的失败事实保持不变。客户端经其
服务的真实导入路径保存两批报告并递交最终 ACK。
"""

from __future__ import annotations

import glob
import json
import sqlite3
from pathlib import Path

from camctl_fixtures import (
    Deployment,
    future_schedule,
    photo_file,
    photo_plan,
    stub_driver_spec,
    video_file,
)

#: 设备侧照片内容：替身按身份提供读取、摘要与存在性事实。
_PHOTO_CONTENT = ("photo-payload-" * 320)[:4096]


def _query(state_db: Path, sql: str, params=()) -> list[tuple]:
    """只读连接查询部署状态库（busy 覆盖与子进程的短暂竞争）。"""
    connection = sqlite3.connect(
        f"file:{state_db.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def _ready_report(deployment: Deployment) -> dict:
    """ready 根目录中唯一报告的解析正文。"""
    names = sorted(glob.glob("status-report-*.json",
                             root_dir=str(deployment.ready)))
    assert len(names) == 1, names
    content = (deployment.ready / names[0]).read_text("utf-8")
    return json.loads(content)


def _action_of(report: dict, name: str) -> dict:
    """按名称在报告全部计划中定位唯一动作。"""
    found = [
        action
        for plan in report["plans"]
        for action in plan["actions"]
        if action["name"] == name
    ]
    assert len(found) == 1, found
    return found[0]


def test_cleanup_unknown_preserves_original_request_result(
        tmp_path: Path) -> None:
    """删除效果未知耗尽预算后清理失败；拍摄结果与产物保持，随后
    另一精确清理成功也不改写旧请求的失败事实。"""
    deployment = Deployment(tmp_path)
    initialized = deployment.camctl(
        "init", "--config", str(deployment.config_path))
    assert initialized.exit_code == 0, initialized.stderr

    # 客户端真实导出：拍摄与范围清理同计划，清理按名称引用来源。
    body = {
        "request_id": "0",
        "created_at": "2026-01-15 08:00:00",
        "name": "photo-and-cleanup",
        "actions": [
            {
                "name": "shoot",
                "type": "camera_take_photo",
                "device_id": "cam-1",
                "scheduled_at": future_schedule(1),
                "params": {"type": "single_shot"},
                "policy": {"max_delay_ms": 5000},
            },
            {
                "name": "purge",
                "type": "delete_action_outputs",
                "scheduled_at": future_schedule(2),
                "params": {"source": {"action_name": "shoot"}},
            },
        ],
    }
    deployment.install_client_capabilities(
        stub_driver_spec(
            {"1": [photo_file("shot-1")]},
            device_files={"shot-1": _PHOTO_CONTENT}))
    plan_path, receipt = deployment.export_plan_with_client(body)
    # 删除调用持续失败且文件确实还在：删除效果未知，预算耗尽。
    failing = stub_driver_spec(
        {"1": [photo_file("shot-1")]},
        device_files={"shot-1": _PHOTO_CONTENT},
        delete_error="设备删除失败")
    submitted = deployment.camctl(
        "submit", str(plan_path), "--config", str(deployment.config_path),
        driver=failing)
    assert submitted.exit_code == 0, submitted.stderr
    first_run = deployment.camctl(
        "run", "--config", str(deployment.config_path), driver=failing)
    assert first_run.exit_code == 0, first_run.stderr

    state_db = deployment.state_db
    # 拍摄成功；删除效果未知耗尽预算后成员失败并保留限制，产物不
    # 再声明为可用，但文件仍在：后续仍可精确清理。
    assert _query(
        state_db, "SELECT status FROM actions WHERE name = 'shoot'"
        ) == [(3,)]
    assert _query(
        state_db, "SELECT availability FROM outputs") == [(2,)]
    # 清理动作失败；成员按删除预算耗尽失败（明细错误编号 2）。
    assert _query(
        state_db, "SELECT status FROM actions WHERE name = 'purge'"
        ) == [(4,)]
    assert _query(
        state_db, "SELECT status, error_code FROM cleanup_items"
        ) == [(5, 2)]

    # 首批报告：拍摄成功与清理失败并存，产物身份与可用性如实呈现。
    first_report = _ready_report(deployment)
    first_id = first_report["report_id"]
    shoot = _action_of(first_report, "shoot")
    purge = _action_of(first_report, "purge")
    assert shoot["status"] == "succeeded"
    assert purge["status"] == "failed"
    assert purge["error"]["code"] == "cleanup_items_failed"
    assert [output["availability"] for output in shoot["outputs"]] == [
        "restricted"]
    output_id = shoot["outputs"][0]["output_id"]
    saved_first = deployment.import_reports_with_client(deployment.ready)
    assert saved_first["saved_report_ids"] == [first_id]

    # 另一个请求后来成功：对同一产物精确删除（客户端从报告取得身份）。
    second_body = {
        "request_id": "0",
        "created_at": "2026-01-15 08:00:00",
        "name": "precise-cleanup",
        "actions": [
            {
                "name": "purge2",
                "type": "delete_action_outputs",
                "scheduled_at": future_schedule(1),
                "params": {"output_ids": [output_id]},
            },
        ],
    }
    second_path, second_receipt = deployment.export_plan_with_client(
        second_body)
    succeeding = stub_driver_spec(
        {"1": [photo_file("shot-1")]},
        device_files={"shot-1": _PHOTO_CONTENT}, delete_error=None)
    second_submitted = deployment.camctl(
        "submit", str(second_path), "--config", str(deployment.config_path),
        driver=succeeding)
    assert second_submitted.exit_code == 0, second_submitted.stderr
    second_run = deployment.camctl(
        "run", "--config", str(deployment.config_path), driver=succeeding)
    assert second_run.exit_code == 0, second_run.stderr

    # 精确清理成功：产物按可靠删除事实转为已清理并保留身份；第一
    # 个请求的失败成员保持原失败事实。
    assert _query(
        state_db, "SELECT status FROM actions WHERE name = 'purge2'"
        ) == [(3,)]
    assert _query(
        state_db, "SELECT status FROM cleanup_items ORDER BY id"
        ) == [(5,), (4,)]
    assert _query(
        state_db, "SELECT availability FROM outputs") == [(3,)]
    # 旧请求的确定结果保持：拍摄成功与首次清理失败都不被改写。
    assert _query(
        state_db, "SELECT status FROM actions ORDER BY id"
        ) == [(3,), (4,), (3,)]

    # 第二批报告是增量窗口：只含新变化的实体（拍摄产物的可用性
    # 更新与精确清理）；旧请求的失败事实已在首批报告定格，不在增
    # 量窗口内改写或重述。
    second_report = _ready_report(deployment)
    second_id = second_report["report_id"]
    assert _action_of(second_report, "shoot")["status"] == "succeeded"
    assert [output["availability"] for output in
            _action_of(second_report, "shoot")["outputs"]] == ["cleaned"]
    assert _action_of(second_report, "purge2")["status"] == "succeeded"
    assert not [
        action for plan in second_report["plans"]
        for action in plan["actions"] if action["name"] == "purge"]
    saved_second = deployment.import_reports_with_client(deployment.ready)
    assert saved_second["saved_report_ids"] == [second_id]

    ack = deployment.write_plan(
        {"request_id": str(receipt["request_id"]),
         "last_report_id": second_id})
    absorbed = deployment.camctl(
        "run", str(ack), "--config", str(deployment.config_path),
        driver=succeeding)
    assert absorbed.exit_code == 0, absorbed.stderr
    assert _query(
        state_db,
        "SELECT acknowledged_report_id, acknowledged_wm FROM runtime_state"
    ) == [(int(second_id), second_report["to_wm"])]

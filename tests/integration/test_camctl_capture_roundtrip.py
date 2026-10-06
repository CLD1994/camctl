"""I5 采集链跨组件组合验收（第一段：照片与延时）。

真实 camctl CLI 子进程经部署装配桥接入受契约约束的设备替身完成
受理、推进、产物登记与报告发布；客户端经其服务的真实导入路径核
验并保存报告，随后递交 ACK 由统一受理接口吸收。录像媒体链与 C
领取模块不在本文件范围（按集成计划 I5 后续链路接入）。
"""

from __future__ import annotations

import glob
import json
import sqlite3
import time
from pathlib import Path

from camctl_fixtures import (
    Deployment,
    future_schedule,
    photo_file,
    photo_plan,
    stub_driver_spec,
    timelapse_plan,
    video_file,
)


def _query(state_db: Path, sql: str, params=()) -> list[tuple]:
    """只读连接查询部署状态库（busy 覆盖与子进程的短暂竞争）。"""
    connection = sqlite3.connect(
        f"file:{state_db.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def _await_row(state_db: Path, sql: str, params=(), *,
               timeout_s: float = 30.0) -> list[tuple]:
    """轮询查询直到出现行；同步点，不使用随机 sleep。"""
    deadline = time.monotonic() + timeout_s
    while True:
        rows = _query(state_db, sql, params)
        if rows:
            return rows
        assert time.monotonic() < deadline, f"等待超时: {sql}"


def _ready_reports(deployment: Deployment) -> list[tuple[str, str]]:
    """ready 根目录中的 (文件名, 字节) 列表；ready 只保留最新报告。"""
    found = []
    for name in sorted(glob.glob("status-report-*.json",
                                 root_dir=str(deployment.ready))):
        content = (deployment.ready / name).read_text("utf-8")
        found.append((name, content))
    return found


def test_photo_keeps_completed_outputs(tmp_path: Path) -> None:
    """照片链跨组件闭环：受理、推进、产物、报告、导入、ACK、保留。

    完成的正式产物在取回与清理发生前保持事实不变；报告经客户端
    真实导入保存后，递交的 ACK 被受理接口吸收并推进累计确认。
    """
    deployment = Deployment(tmp_path)
    initialized = deployment.camctl(
        "init", "--config", str(deployment.config_path))
    assert initialized.exit_code == 0, initialized.stderr

    plan = deployment.write_plan(photo_plan("41", future_schedule(1)))
    spec = stub_driver_spec({"1": [photo_file("shot-1")]})
    submitted = deployment.camctl(
        "submit", str(plan), "--config", str(deployment.config_path),
        driver=spec)
    assert submitted.exit_code == 0, submitted.stderr
    assert submitted.message() == {
        "kind": "succeeded", "body": {"needs_run": True}}

    run = deployment.camctl(
        "run", "--config", str(deployment.config_path), driver=spec)
    assert run.exit_code == 0, run.stderr
    assert run.message() == {"kind": "succeeded"}

    state_db = deployment.state_db
    assert _query(state_db, "SELECT status FROM plans") == [(3,)]
    assert _query(state_db, "SELECT status FROM actions") == [(3,)]
    # 正式产物登记：photo 类产物归属来源动作，文件事实完整。
    assert _query(
        state_db,
        "SELECT o.kind FROM outputs o JOIN device_files f"
        " ON f.id = o.device_file_id WHERE o.source_action_id = 1"
        " AND f.completion_state = 3") == [(1,)]
    # 成功链收场活动：占用释放，无遗留执行中事实。
    assert _query(
        state_db,
        "SELECT activity_state, occupancy_state FROM device_activities"
    ) == [(3, 2)]

    # 报告发布到 ready 根目录；staging 不留报告临时文件。
    reports = _ready_reports(deployment)
    assert len(reports) == 1, reports
    report_name, report_bytes = reports[0]
    report = json.loads(report_bytes)
    report_id = report["report_id"]
    action = report["plans"][0]["actions"][0]
    assert action["name"] == "shoot"
    assert action["status"] == "succeeded"
    assert not list((deployment.staging / "reports").glob("*"))

    # 客户端经真实导入路径核验保存；累计确认指向已保存报告。
    saved = deployment.import_reports_with_client(deployment.ready)
    assert saved["saved_report_ids"] == [report_id]
    assert saved["ack_id"] == report_id

    # 递交 ACK（顶层 last_report_id）；受理接口吸收并推进累计确认。
    ack = deployment.write_plan(
        {"request_id": "41", "last_report_id": report_id})
    absorbed = deployment.camctl(
        "run", str(ack), "--config", str(deployment.config_path),
        driver=spec)
    assert absorbed.exit_code == 0, absorbed.stderr
    assert _query(
        state_db,
        "SELECT acknowledged_report_id, acknowledged_wm FROM runtime_state"
    ) == [(int(report_id), report["to_wm"])]

    # 完成的产物事实保留：无取回与清理发生时不被后续会话消耗。
    rerun = deployment.camctl("run", "--config", str(deployment.config_path),
                              driver=spec)
    assert rerun.exit_code == 0, rerun.stderr
    assert _query(
        state_db,
        "SELECT o.kind, f.completion_state FROM outputs o"
        " JOIN device_files f ON f.id = o.device_file_id"
    ) == [(1, 3)]


def test_timelapse_recovers_remaining_wait(tmp_path: Path) -> None:
    """延时链跨会话恢复：中断后从已保存等待安排继续到终态。

    第一个 run 会话启动拍摄后中断（等待安排已保存）；第二个会话
    恢复剩余等待，动作按时间与产物完成判定成功并登记产物。
    """
    deployment = Deployment(tmp_path)
    initialized = deployment.camctl(
        "init", "--config", str(deployment.config_path))
    assert initialized.exit_code == 0, initialized.stderr

    plan = deployment.write_plan(timelapse_plan("42", future_schedule(1)))
    spec = stub_driver_spec({"1": [video_file("seq-1")]})
    submitted = deployment.camctl(
        "submit", str(plan), "--config", str(deployment.config_path),
        driver=spec)
    assert submitted.exit_code == 0, submitted.stderr

    state_db = deployment.state_db
    spec = stub_driver_spec({"1": [video_file("seq-1")]})
    first = deployment.start_camctl("run", "--config", str(deployment.config_path),
                                    driver=spec)
    try:
        # 等待安排已保存（CAPTURE_WAIT_CHANGED 的首次安排）。
        _await_row(
            state_db,
            "SELECT 1 FROM history_events WHERE event_type = 15"
            " AND json_extract(body_json, '$.reason') = 1")
    finally:
        first.terminate()
        first.wait(timeout=30)

    second = deployment.camctl(
        "run", "--config", str(deployment.config_path), driver=spec)
    assert second.exit_code == 0, second.stderr
    assert second.message() == {"kind": "succeeded"}

    # 恢复的等待按完成分支保存完成事实；动作与计划成功终态。
    assert _query(
        state_db,
        "SELECT 1 FROM history_events WHERE event_type = 15"
        " AND json_extract(body_json, '$.reason') = 3")
    assert _query(state_db, "SELECT status FROM actions") == [(3,)]
    assert _query(state_db, "SELECT status FROM plans") == [(3,)]
    assert _query(state_db, "SELECT COUNT(*) FROM outputs") == [(1,)]

"""I5 取回交付链跨组件组合验收：录像交付存活迟到取消。

真实 camctl CLI 子进程经部署装配桥接入受契约约束的设备替身完成
录像、取回拷贝与交付发布；交付文件被领取进 processing 后，迟到
的取消请求按不可撤回事实收场，交付结果与原请求终态保持不变。客
户端经其服务的真实导入路径保存最终报告并递交 ACK。C 领取模块与
录像媒体工具不在本文件范围（按集成计划 I5 后续链路接入）。
"""

from __future__ import annotations

import glob
import json
import shutil
import sqlite3
import time
from pathlib import Path

from camctl_fixtures import (
    Deployment,
    future_schedule,
    stub_driver_spec,
    video_file,
)

#: 设备侧视频内容；长度与 video_file 条目的 size_bytes 一致，
#: 取回拷贝按登记长度读取并做源端摘要比较。
_CLIP_CONTENT = ("clip-payload-" * 700)[:8192]


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


def _ready_report(deployment: Deployment) -> dict:
    """ready 根目录中唯一报告的解析正文。"""
    names = sorted(glob.glob("status-report-*.json",
                             root_dir=str(deployment.ready)))
    assert len(names) == 1, names
    content = (deployment.ready / names[0]).read_text("utf-8")
    return json.loads(content)


def _delivered_files(directory: Path) -> list[Path]:
    """目录中的交付文件；报告文件不属于交付。"""
    return sorted(
        path for path in directory.iterdir() if path.is_file()
        and not path.name.startswith("status-report-"))


def test_recording_delivery_survives_late_cancel(tmp_path: Path) -> None:
    """录像交付存活迟到取消：拷贝交付到 ready、领取进 processing、
    取消按不可撤回收场后原请求终态与交付字节保持。"""
    deployment = Deployment(tmp_path)
    initialized = deployment.camctl(
        "init", "--config", str(deployment.config_path))
    assert initialized.exit_code == 0, initialized.stderr

    spec = stub_driver_spec(
        {"1": [video_file("clip-1")]},
        device_files={"clip-1": _CLIP_CONTENT})
    deployment.install_client_capabilities(spec)

    # 客户端真实导出：录像与取回同计划，取回按名称引用本计划来源。
    body = {
        "request_id": "0",
        "created_at": "2026-01-15 08:00:00",
        "name": "record-and-obtain",
        "actions": [
            {
                "name": "拍录",
                "type": "camera_record",
                "device_id": "cam-1",
                "scheduled_at": future_schedule(1),
                "params": {"type": "video"},
                "policy": {"max_delay_ms": 5000},
            },
            {
                "name": "取回",
                "type": "obtain_action_outputs",
                "scheduled_at": future_schedule(2),
                "params": {
                    "source": {"action_name": "拍录"},
                    "purpose": "manual",
                },
            },
        ],
    }
    plan_path, receipt = deployment.export_plan_with_client(body)
    original_request = str(receipt["request_id"])
    submitted = deployment.camctl(
        "submit", str(plan_path), "--config", str(deployment.config_path),
        driver=spec)
    assert submitted.exit_code == 0, submitted.stderr
    assert submitted.message() == {"kind": "succeeded", "body": {"needs_run": True}}

    # 一个 run 会话完成录像、产物核实、取回拷贝与交付发布。
    run = deployment.camctl(
        "run", "--config", str(deployment.config_path), driver=spec)
    assert run.exit_code == 0, run.stderr

    state_db = deployment.state_db
    assert _query(state_db, "SELECT status FROM plans") == [(3,)]
    assert _query(
        state_db, "SELECT status FROM actions ORDER BY id") == [(3,), (3,)]
    delivered = _delivered_files(deployment.ready)
    assert len(delivered) == 1, delivered
    assert delivered[0].read_bytes() == _CLIP_CONTENT.encode("utf-8")

    # C 领取未接入时的等价领取事实：主程序把交付文件领进 processing。
    claimed = deployment.processing / delivered[0].name
    shutil.move(str(delivered[0]), str(claimed))

    # 迟到取消按请求身份引用原计划；此时目标已全部成功终态。
    cancel_body = {
        "request_id": "0",
        "created_at": "2026-01-15 08:00:00",
        "name": "late-cancel",
        "actions": [
            {
                "name": "取消",
                "type": "cancel_task",
                "scheduled_at": future_schedule(1),
                "params": {"target": {"request_id": original_request}},
            },
        ],
    }
    cancel_path, _ = deployment.export_plan_with_client(cancel_body)
    cancel_submitted = deployment.camctl(
        "submit", str(cancel_path), "--config", str(deployment.config_path),
        driver=spec)
    assert cancel_submitted.exit_code == 0, cancel_submitted.stderr
    cancel_run = deployment.camctl(
        "run", "--config", str(deployment.config_path), driver=spec)
    assert cancel_run.exit_code == 0, cancel_run.stderr

    # 取消动作成功收场：processing 不可撤回是独立事实，不删除交付。
    assert _query(
        state_db, "SELECT status FROM actions WHERE name = '取消'") == [(3,)]
    assert _query(
        state_db, "SELECT status FROM plans") == [(3,), (3,)]
    assert _query(
        state_db, "SELECT withdrawal_state FROM deliveries") == [(4,)]
    # 原请求终态与交付结果保持：动作不被改写，文件字节不变。
    assert _query(
        state_db,
        "SELECT status FROM actions WHERE name IN ('拍录', '取回')"
        " ORDER BY id") == [(3,), (3,)]
    assert claimed.read_bytes() == _CLIP_CONTENT.encode("utf-8")
    assert not _delivered_files(deployment.ready)

    # 客户端导入最终报告：取消动作成功，随后递交 ACK 被受理吸收。
    report = _ready_report(deployment)
    report_id = report["report_id"]
    cancel_actions = [
        action
        for plan in report["plans"]
        for action in plan["actions"]
        if action["name"] == "取消"
    ]
    assert [action["status"] for action in cancel_actions] == ["succeeded"]
    saved = deployment.import_reports_with_client(deployment.ready)
    assert saved["saved_report_ids"] == [report_id]
    assert saved["ack_id"] == report_id

    ack = deployment.write_plan(
        {"request_id": original_request, "last_report_id": report_id})
    absorbed = deployment.camctl(
        "run", str(ack), "--config", str(deployment.config_path),
        driver=spec)
    assert absorbed.exit_code == 0, absorbed.stderr
    assert _query(
        state_db,
        "SELECT acknowledged_report_id, acknowledged_wm FROM runtime_state"
    ) == [(int(report_id), report["to_wm"])]

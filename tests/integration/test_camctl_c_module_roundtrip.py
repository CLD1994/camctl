"""路线图 375 真实 C 模块递交链全场景复验。

在报告同步主链（test_camctl_report_roundtrip）已验证的真实客户端
导出、C 主程序递交与领取、camctl 执行及客户端导入基础上，把拍摄、
取回、取消与清理场景全部改为经同一 C 模块递交链执行：计划由客户
端真实导出，host-demo 的 submit 内部调用经部署装配桥接入设备替身
的 camctl submit/run，产物与报告由 claim 领取进 processing，客户
端导入后下一份导出自动携带 ACK，经同一递交入口吸收。

覆盖：录像、照片、延时摄影、普通取回与自动取回、清理、取消及同
步 ACK。报告失败恢复场景已由 report_roundtrip 承载，不在本文件
重复。
"""

from __future__ import annotations

import json
from pathlib import Path

from _wsl_host_demo import (
    HostDemo,
    query_state,
    wait_for,
    write_stub_launcher,
)
from camctl_fixtures import (
    Deployment,
    future_schedule,
    photo_file,
    preview_file,
    stub_driver_spec,
    video_file,
)

#: 设备侧内容；截断前长度覆盖条目声明的 size_bytes，拷贝按登记
#: 长度读取并做源端摘要比较。
_PHOTO_CONTENT = ("c-module-photo-" * 300)[:4096]
_CLIP_CONTENT = ("c-module-clip-" * 620)[:8192]
_SEQ_CONTENT = ("c-module-seq-" * 660)[:8192]
_PREVIEW_CONTENT = ("c-module-preview-" * 130)[:2048]
_CANCEL_CLIP_CONTENT = ("c-module-cancel-" * 520)[:8192]


def _delivered_files(directory: Path) -> list[Path]:
    """目录中的交付文件；报告文件不属于交付。"""
    return sorted(
        path for path in directory.iterdir() if path.is_file()
        and not path.name.startswith("status-report-"))


def _report_file(directory: Path) -> Path:
    """目录中唯一的状态报告文件。"""
    found = sorted(path for path in directory.iterdir()
                   if path.name.startswith("status-report-"))
    assert len(found) == 1, found
    return found[0]


def _report_with_statuses(directory: Path, expected: dict[str, str]):
    """目录中动作状态满足期望的报告；用于等待会话的最终报告。

    会话的中间报告在取回执行中即可发布，最终报告按生命周期替换
    旧文件；按报告正文断言动作状态，避免领取到执行中的中间报告。
    轮询期间报告可能正被替换或写入，读不到的文件按尚未就绪跳过。
    """
    for path in sorted(directory.glob("status-report-*.json")):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, PermissionError):
            continue
        statuses = {
            action["name"]: action["status"]
            for plan in report["plans"] for action in plan["actions"]}
        if all(statuses.get(name) == status
               for name, status in expected.items()):
            return report
    return None


def _init_with_capabilities(deployment: Deployment, spec: dict) -> None:
    initialized = deployment.camctl(
        "init", "--config", str(deployment.config_path))
    assert initialized.exit_code == 0, initialized.stderr
    deployment.install_client_capabilities(spec)


def test_capture_obtain_cleanup_roundtrip_via_c_module(
        tmp_path: Path, host_demo: str) -> None:
    """三种拍摄、普通取回与清理经 C 模块递交链：客户端导出计划、
    主程序递交并驱动执行、产物与报告领取进 processing、客户端导入
    并在下一份导出的 ACK 经同一递交入口吸收。"""
    deployment = Deployment(tmp_path)
    spec = stub_driver_spec(
        {"1": [photo_file("cm-shot")],
         "2": [video_file("cm-clip")],
         "3": [video_file("cm-seq")]},
        device_files={"cm-shot": _PHOTO_CONTENT,
                      "cm-clip": _CLIP_CONTENT,
                      "cm-seq": _SEQ_CONTENT})
    _init_with_capabilities(deployment, spec)
    state_db = deployment.state_db

    shoot_at = future_schedule(1)
    body = {
        "request_id": "0",
        "created_at": "2026-01-15 08:00:00",
        "name": "c-module-capture",
        "actions": [
            {
                "name": "拍照",
                "type": "camera_take_photo",
                "device_id": "cam-1",
                "scheduled_at": shoot_at,
                "params": {"type": "single_shot"},
                "policy": {"max_delay_ms": 5000},
            },
            {
                "name": "拍录",
                "type": "camera_record",
                "device_id": "cam-1",
                "scheduled_at": shoot_at,
                "params": {"type": "video"},
                "policy": {"max_delay_ms": 5000},
            },
            {
                "name": "延时",
                "type": "camera_timelapse",
                "device_id": "cam-1",
                "scheduled_at": shoot_at,
                "params": {"type": "timelapse"},
                "policy": {"max_delay_ms": 5000},
            },
            {
                "name": "取照片",
                "type": "obtain_action_outputs",
                "scheduled_at": shoot_at,
                "params": {
                    "source": {"action_name": "拍照"},
                    "purpose": "manual",
                },
            },
            {
                "name": "取延时",
                "type": "obtain_action_outputs",
                "scheduled_at": shoot_at,
                "params": {
                    "source": {"action_name": "延时"},
                    "purpose": "manual",
                },
            },
            {
                "name": "清理",
                "type": "delete_action_outputs",
                # 排期在两种取回完成后：清理依赖来源终态即可执行，
                # 与正在拷贝的取回同窗口会先删除设备内容。
                "scheduled_at": future_schedule(10),
                "params": {"source": {"action_name": "拍照"}},
            },
        ],
    }
    plan_path, _ = deployment.export_plan_with_client(body)

    demo = HostDemo(deployment, host_demo,
                    launcher=write_stub_launcher(deployment))
    try:
        demo.start()
        demo.submit(plan_path)
        # 主程序驱动的 run 会话完成三种拍摄、两次取回与一次清理。
        wait_for(lambda: query_state(
            state_db, "SELECT status FROM actions ORDER BY id"
        ) == [(3,)] * 6 or None,
            timeout_s=180, message="六动作未全部成功")
        assert query_state(state_db, "SELECT status FROM plans") == [(3,)]
        # 三种拍摄各自登记原片产物；被取回来源建立交付（部分取回）。
        assert query_state(
            state_db, "SELECT source_action_id, kind FROM outputs"
            " ORDER BY source_action_id") == [(1, 1), (2, 1), (3, 1)]
        assert query_state(
            state_db,
            "SELECT d.action_id FROM deliveries d ORDER BY d.id") == [(4,), (5,)]
        # 清理成功：照片来源产物按可靠删除转为已清理，录像与延时保
        # 持可用；清理成员完成。
        assert query_state(
            state_db, "SELECT availability FROM outputs"
            " ORDER BY source_action_id") == [(3,), (1,), (1,)]
        assert query_state(
            state_db, "SELECT status FROM cleanup_items") == [(4,)]

        # ready 同存两份交付与一份报告；领取后原字节进 processing。
        delivered = _delivered_files(deployment.ready)
        assert len(delivered) == 2, delivered
        expected = {_PHOTO_CONTENT.encode("utf-8"),
                    _SEQ_CONTENT.encode("utf-8")}
        assert {path.read_bytes() for path in delivered} == expected
        report = wait_for(lambda: _report_with_statuses(
            deployment.ready, {"清理": "succeeded", "取延时": "succeeded"}
        ) or None,
            timeout_s=120, message="会话最终报告未发布")
        report_path = _report_file(deployment.ready)
        report_bytes = report_path.read_bytes()
        demo.claim()
        wait_for(lambda: (
            len(_delivered_files(deployment.processing)) == 2
            and _report_file(deployment.processing).exists()) or None,
            timeout_s=30, message="领取未移动交付与报告")
        assert not list(deployment.ready.iterdir())
        claimed = _delivered_files(deployment.processing)
        assert {path.read_bytes() for path in claimed} == expected
        assert (deployment.processing / report_path.name
                ).read_bytes() == report_bytes
        statuses = {
            action["name"]: action["status"]
            for plan in report["plans"] for action in plan["actions"]}
        assert statuses == {
            "拍照": "succeeded", "拍录": "succeeded", "延时": "succeeded",
            "取照片": "succeeded", "取延时": "succeeded",
            "清理": "succeeded"}

        # 客户端导入 processing 内报告；下一份导出自动携带 ACK，经
        # 主程序递交后被受理接口吸收。
        saved = deployment.import_reports_with_client(deployment.processing)
        assert saved["saved_report_ids"] == [report["report_id"]]
        sync_body = {
            "name": "c-module-ack",
            "actions": [
                {
                    "name": "sync",
                    "type": "report_status",
                    "scheduled_at": future_schedule(1),
                    "params": {"scope": "full"},
                }
            ],
        }
        sync_path, sync_receipt = deployment.export_plan_with_client(sync_body)
        assert sync_receipt["has_ack"] is True
        assert sync_receipt["last_report_id"] == report["report_id"]
        demo.submit(sync_path)
        wait_for(lambda: query_state(
            state_db,
            "SELECT acknowledged_report_id, acknowledged_wm FROM runtime_state"
        ) == [(int(report["report_id"]), report["to_wm"])] or None,
            timeout_s=180, message="ACK 未被吸收")
    finally:
        demo.stop()


def test_auto_preview_roundtrip_via_c_module(
        tmp_path: Path, host_demo: str) -> None:
    """自动取回经 C 模块递交链：驱动按配对关联列举预览、预览登记为
    正式产物，客户端展开的自动取回选择预览产物交付并领取导入。"""
    deployment = Deployment(tmp_path)
    spec = stub_driver_spec(
        {"1": [photo_file("ap-shot"),
               preview_file("ap-preview", "ap-shot")]},
        device_files={"ap-shot": _PHOTO_CONTENT,
                      "ap-preview": _PREVIEW_CONTENT},
        photo_preview_supported=True)
    _init_with_capabilities(deployment, spec)
    state_db = deployment.state_db

    shoot_at = future_schedule(1)
    body = {
        "request_id": "0",
        "created_at": "2026-01-15 08:00:00",
        "name": "c-module-auto-preview",
        "actions": [
            {
                "name": "shoot",
                "type": "camera_take_photo",
                "device_id": "cam-1",
                "scheduled_at": shoot_at,
                "params": {"type": "single_shot"},
                "policy": {"max_delay_ms": 5000},
            },
            {
                "name": "auto-preview",
                "type": "obtain_action_outputs",
                "scheduled_at": shoot_at,
                "params": {
                    "source": {"action_name": "shoot"},
                    "filter": "preview",
                    "purpose": "auto_preview",
                },
            },
        ],
    }
    plan_path, _ = deployment.export_plan_with_client(body)

    demo = HostDemo(deployment, host_demo,
                    launcher=write_stub_launcher(deployment))
    try:
        demo.start()
        demo.submit(plan_path)
        wait_for(lambda: query_state(
            state_db, "SELECT status FROM actions ORDER BY id"
        ) == [(3,), (3,)] or None,
            timeout_s=180, message="拍摄与自动取回未全部成功")
        assert query_state(state_db, "SELECT status FROM plans") == [(3,)]
        # 原片与预览分别登记；自动取回选择预览产物建立交付。
        assert query_state(
            state_db, "SELECT kind FROM outputs ORDER BY id") == [(1,), (3,)]
        assert query_state(
            state_db,
            "SELECT o.kind FROM obtain_items i"
            " JOIN outputs o ON o.id = i.output_id") == [(3,)]

        report = wait_for(lambda: _report_with_statuses(
            deployment.ready,
            {"shoot": "succeeded", "auto-preview": "succeeded"}) or None,
            timeout_s=120, message="会话最终报告未发布")
        demo.claim()
        wait_for(lambda: (
            len(_delivered_files(deployment.processing)) == 1
            and _report_file(deployment.processing).exists()) or None,
            timeout_s=30, message="领取未移动预览交付与报告")
        assert not list(deployment.ready.iterdir())
        claimed = _delivered_files(deployment.processing)
        assert [path.read_bytes() for path in claimed] == [
            _PREVIEW_CONTENT.encode("utf-8")]
        actions = {
            action["name"]: action
            for plan in report["plans"] for action in plan["actions"]}
        assert {name: action["status"]
                for name, action in actions.items()} == {
            "shoot": "succeeded", "auto-preview": "succeeded"}
        assert actions["auto-preview"]["automation"] == {
            "purpose": "auto_preview",
            "source_action_instance_id":
                actions["shoot"]["action_instance_id"],
        }
        saved = deployment.import_reports_with_client(deployment.processing)
        assert saved["saved_report_ids"] == [report["report_id"]]
    finally:
        demo.stop()


def test_late_cancel_roundtrip_via_c_module(
        tmp_path: Path, host_demo: str) -> None:
    """迟到取消经 C 模块递交链：录像与取回完成后交付已领取，取消计
    划按不可撤回收场；携带 ACK 的取消计划一次递交完成取消与累计
    确认吸收，交付字节与原请求终态保持。"""
    deployment = Deployment(tmp_path)
    spec = stub_driver_spec(
        {"1": [video_file("lc-clip")]},
        device_files={"lc-clip": _CANCEL_CLIP_CONTENT})
    _init_with_capabilities(deployment, spec)
    state_db = deployment.state_db

    body = {
        "request_id": "0",
        "created_at": "2026-01-15 08:00:00",
        "name": "c-module-record-obtain",
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

    demo = HostDemo(deployment, host_demo,
                    launcher=write_stub_launcher(deployment))
    try:
        demo.start()
        demo.submit(plan_path)
        wait_for(lambda: query_state(
            state_db, "SELECT status FROM actions ORDER BY id"
        ) == [(3,), (3,)] or None,
            timeout_s=180, message="录像与取回未全部成功")
        report = wait_for(lambda: _report_with_statuses(
            deployment.ready,
            {"拍录": "succeeded", "取回": "succeeded"}) or None,
            timeout_s=120, message="会话最终报告未发布")

        demo.claim()
        wait_for(lambda: (
            len(_delivered_files(deployment.processing)) == 1
            and _report_file(deployment.processing).exists()) or None,
            timeout_s=30, message="领取未移动交付与报告")
        claimed = _delivered_files(deployment.processing)
        assert [path.read_bytes() for path in claimed] == [
            _CANCEL_CLIP_CONTENT.encode("utf-8")]
        saved = deployment.import_reports_with_client(deployment.processing)
        assert saved["ack_id"] == report["report_id"]

        # 迟到取消按请求身份引用原计划；导出自动携带已保存报告的
        # ACK，一次递交同时完成取消执行与累计确认吸收。
        cancel_body = {
            "request_id": "0",
            "created_at": "2026-01-15 08:00:00",
            "name": "c-module-late-cancel",
            "actions": [
                {
                    "name": "取消",
                    "type": "cancel_task",
                    "scheduled_at": future_schedule(1),
                    "params": {"target": {"request_id": original_request}},
                },
            ],
        }
        cancel_path, cancel_receipt = deployment.export_plan_with_client(
            cancel_body)
        assert cancel_receipt["has_ack"] is True
        demo.submit(cancel_path)
        wait_for(lambda: query_state(
            state_db, "SELECT status FROM actions WHERE name = '取消'"
        ) == [(3,)] or None,
            timeout_s=180, message="取消动作未收场")
        # 取消按不可撤回收场：交付不删除、原请求终态不改写。
        assert query_state(
            state_db, "SELECT status FROM plans ORDER BY id") == [(3,), (3,)]
        assert query_state(
            state_db, "SELECT status FROM actions ORDER BY id"
        ) == [(3,), (3,), (3,)]
        assert query_state(
            state_db, "SELECT withdrawal_state FROM deliveries") == [(4,)]
        assert [path.read_bytes()
                for path in _delivered_files(deployment.processing)] == [
            _CANCEL_CLIP_CONTENT.encode("utf-8")]
        wait_for(lambda: query_state(
            state_db,
            "SELECT acknowledged_report_id, acknowledged_wm FROM runtime_state"
        ) == [(int(report["report_id"]), report["to_wm"])] or None,
            timeout_s=180, message="取消计划携带的 ACK 未被吸收")
    finally:
        demo.stop()

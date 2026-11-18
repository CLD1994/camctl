"""路线图 375 真实 C 模块递交链全场景复验。

在报告同步主链（test_camctl_report_roundtrip）已验证的真实客户端
导出、C 主程序递交与领取、camctl 执行及客户端导入基础上，把拍摄、
取回、取消与清理场景全部改为经同一 C 模块递交链执行：计划由客户
端真实导出，host-demo 初始化或 submit 经部署装配桥接入设备替身
的 camctl run/submit，产物与报告由 claim 领取进 processing，客户
端导入后下一份导出自动携带 ACK，经同一递交入口吸收。

覆盖：录像、照片、延时摄影、普通取回与自动取回、清理、取消及同
步 ACK。报告失败恢复场景已由 report_roundtrip 承载，不在本文件
重复。
"""

from __future__ import annotations

import json
import io
import os
import re
import time
from decimal import Decimal
from pathlib import Path

from _wsl_host_demo import (
    HostDemo,
    query_state,
    wait_for,
    write_stub_launcher,
)
from camctl_fixtures import (
    Deployment,
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
            with _open_ready_report(path) as reader:
                report = json.loads(reader.read())
        except (FileNotFoundError, PermissionError):
            continue
        statuses = {
            action["name"]: action["status"]
            for plan in report["plans"] for action in plan["actions"]}
        if all(statuses.get(name) == status
               for name, status in expected.items()):
            return report
    return None


def _open_ready_report(path: Path):
    """打开测试观察器读取的 READY 报告；调用方负责关闭。"""
    if os.name != "nt":
        return path.open(encoding="utf-8")
    # Windows 测试解释器限定 CPython 3.11；复用其 Win32 句柄接口。
    import _winapi
    import msvcrt
    def opener(file_name, _flags):
        # Win32 FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE。
        share_mode = 1 | 2 | 4
        # FILE_ATTRIBUTE_NORMAL 不设置关闭时删除等额外行为。
        handle = _winapi.CreateFile(str(file_name), _winapi.GENERIC_READ, share_mode, 0,
                                    _winapi.OPEN_EXISTING, 128, 0)
        try:
            return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            _winapi.CloseHandle(handle)
            raise
    # opener 返回 FD 后，标准库拥有其后包装成功与失败的全部关闭责任。
    return io.open(path, encoding="utf-8", opener=opener)


def _init_with_capabilities(deployment: Deployment, spec: dict) -> None:
    initialized = deployment.camctl(
        "init", "--config", str(deployment.config_path))
    assert initialized.exit_code == 0, initialized.stderr
    initialized = deployment.install_client_capabilities(spec)
    assert initialized["initialized"] is True
    state = initialized["state"]
    assert state["startup"]["state"] == "ready"
    assert state["drafts"] == state["requests"] == state["imports"] == state["reports"] == []
    assert state["coverage"] == 0
    assert not any(path.is_file() for path in (deployment.root / "client-store/uploads").rglob("*"))


def test_capture_obtain_cleanup_roundtrip_via_c_module(
        tmp_path: Path, host_demo: str, reuse_client, monkeypatch) -> None:
    """三种拍摄、普通取回与清理经 C 模块递交链：客户端导出计划、
    主程序递交并驱动执行、产物与报告领取进 processing、客户端导入
    并在下一份导出的 ACK 经同一递交入口吸收。"""
    monkeypatch.setenv("CAMCTL_TEST_TRACE_CALLS", "1")
    monkeypatch.setenv("WSLENV", ":".join(filter(None, (
        os.environ.get("WSLENV"), "CAMCTL_TEST_TRACE_CALLS"))))
    deployment = Deployment(tmp_path, controlled_clock=True)
    spec = stub_driver_spec(
        {"1": [photo_file("cm-shot")],
         "2": [video_file("cm-clip")],
         "3": [video_file("cm-seq")]},
        device_files={"cm-shot": _PHOTO_CONTENT,
                      "cm-clip": _CLIP_CONTENT,
                      "cm-seq": _SEQ_CONTENT})
    reuse_client(deployment)
    _init_with_capabilities(deployment, spec)
    state_db = deployment.state_db

    shoot_at = deployment.schedule(1)
    cleanup_at = deployment.schedule(10)
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
                "scheduled_at": cleanup_at,
                "params": {"source": {"action_name": "拍照"}},
            },
        ],
    }
    plan_path, receipt = deployment.export_plan_with_client(body)

    demo = HostDemo(deployment, host_demo,
                    launcher=write_stub_launcher(deployment))
    failure = demo.watch_failures(
        request_id=receipt["request_id"],
        expected_statuses={action["name"]: "SUCCEEDED" for action in body["actions"]},
    )
    try:
        demo.start(initial_plan=plan_path)
        deadline = time.monotonic() + 180
        deployment.freeze_at(shoot_at)
        wait_for(lambda: query_state(state_db,
            "SELECT r.id FROM operation_runs r JOIN actions a ON a.id=r.action_id"
            " WHERE a.name='拍录' AND r.responsibility_key='start/' || a.id"
            " AND r.status IN (3,4)") or None,
            timeout_s=max(0, deadline - time.monotonic()),
            message="录像 START 责任未收场", failure=failure)
        # 真实录像 START 后推进其一秒目标；后续延时仍在原五秒窗口内。
        deployment.freeze_at(deployment.schedule(1))
        wait_for(lambda: query_state(state_db,
            "SELECT d.expected_check_at FROM device_activities d"
            " JOIN actions a ON a.id=d.action_id"
            " WHERE a.name='延时' AND d.sent_at IS NOT NULL"
            " AND d.expected_check_at=d.sent_at+3000000"
            " AND d.wait_completed_event_id IS NULL") or None,
            timeout_s=max(0, deadline - time.monotonic()),
            message="延时 START 及三秒等待安排未保存", failure=failure)
        # 保留完整延时目标；两份交付实际发布后再到达原清理时刻。
        deployment.freeze_at(deployment.schedule(3))
        wait_for(lambda: query_state(
            state_db, "SELECT status FROM actions WHERE name IN ('取照片','取延时') ORDER BY id"
        ) == [(3,), (3,)] and len(_delivered_files(deployment.ready)) == 2 or None,
            timeout_s=max(0, deadline - time.monotonic()),
            message="两份取回交付未发布", failure=failure)
        frozen = deployment.freeze_at(cleanup_at)
        running = deployment.resume_clock()
        assert running["utc_anchor_us"] == frozen["utc_us"]
        assert running["monotonic_anchor_ns"] == frozen["monotonic_ns"]
        # 主程序驱动的 run 会话完成三种拍摄、两次取回与一次清理。
        wait_for(lambda: query_state(
            state_db, "SELECT status FROM actions ORDER BY id"
        ) == [(3,)] * 6 or None,
            timeout_s=max(0, deadline - time.monotonic()), message="六动作未全部成功",
            failure=failure)
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
            timeout_s=120, message="会话最终报告未发布", failure=failure)
        report_path = _report_file(deployment.ready)
        report_bytes = report_path.read_bytes()
        demo.claim()
        assert not list(deployment.ready.iterdir())
        claimed = _delivered_files(deployment.processing)
        assert len(claimed) == 2, claimed
        assert {path.read_bytes() for path in claimed} == expected
        claimed_report = _report_file(deployment.processing)
        assert claimed_report.name == report_path.name
        assert claimed_report.read_bytes() == report_bytes
        statuses = {
            action["name"]: action["status"]
            for plan in report["plans"] for action in plan["actions"]}
        assert statuses == {
            "拍照": "succeeded", "拍录": "succeeded", "延时": "succeeded",
            "取照片": "succeeded", "取延时": "succeeded",
            "清理": "succeeded"}

        # 客户端导入报告，再下载同一原请求并携带当前 ACK。
        saved = deployment.import_reports_with_client(deployment.processing)
        assert saved["saved_report_ids"] == [report["report_id"]]
        ack_path = deployment.root / "capture-with-ack.json"
        ack_receipt = deployment.run_client_driver(
            "download-request", str(deployment.root / "client-store"),
            str(receipt["request_id"]), str(ack_path),
            output=deployment.root / "client-download.json")
        assert ack_receipt == {
            "request_id": receipt["request_id"], "has_ack": True,
            "last_report_id": report["report_id"]}
        original = json.loads(plan_path.read_text(encoding="utf-8"),
                              parse_float=Decimal)
        redownloaded = json.loads(ack_path.read_text(encoding="utf-8"),
                                  parse_float=Decimal)
        assert original["request_id"] == receipt["request_id"]
        assert redownloaded.pop("last_report_id") == report["report_id"]
        original.pop("last_report_id", None)
        assert redownloaded == original
        wait_for(lambda: any(
            " command=run " in line and " result=success " in line
            and " needs_run=0 " in line for line in demo.module_log().splitlines()),
            timeout_s=180, message="原执行会话未完成回收", failure=failure)
        counts = query_state(state_db,
            "SELECT (SELECT count(*) FROM plans),(SELECT count(*) FROM deliveries)")
        assert counts == [(1, 2)]
        attempts = query_state(state_db,
            "SELECT r.id, r.kind, p.id, p.status, p.result_event_id"
            " FROM operation_runs r JOIN operation_attempts p ON p.run_id=r.id"
            " ORDER BY r.id, p.id")
        assert attempts
        read_calls = (deployment.gates / "stub-calls.log").read_text(encoding="utf-8")
        assert read_calls
        log_start = len(demo.module_log())
        demo.submit(ack_path)
        submitted = re.search(
            r"^call=\d+ command=submit pid=\d+ result=success "
            r"[^\n]* needs_run=([01]) ",
            demo.module_log()[log_start:], re.MULTILINE)
        assert submitted, demo.module_log()[log_start:]
        if submitted.group(1) == "1":
            wait_for(lambda: any(
                " command=run " in line and " result=success " in line
                and " needs_run=0 " in line
                for line in demo.module_log()[log_start:].splitlines()),
                timeout_s=180, message="重送后的执行会话未完成回收", failure=failure)
        wait_for(lambda: query_state(
            state_db,
            "SELECT acknowledged_report_id, acknowledged_wm FROM runtime_state"
        ) == [(int(report["report_id"]), report["to_wm"])] or None,
            timeout_s=180, message="ACK 未被吸收", failure=failure)
        assert query_state(state_db,
            "SELECT (SELECT count(*) FROM plans),(SELECT count(*) FROM deliveries)") == counts
        assert query_state(state_db,
            "SELECT r.id, r.kind, p.id, p.status, p.result_event_id"
            " FROM operation_runs r JOIN operation_attempts p ON p.run_id=r.id"
            " ORDER BY r.id, p.id") == attempts
        assert (deployment.gates / "stub-calls.log").read_text(encoding="utf-8") == read_calls
    finally:
        demo.stop()


def test_auto_preview_roundtrip_via_c_module(
        tmp_path: Path, host_demo: str, reuse_client) -> None:
    """自动取回经 C 模块递交链：驱动按配对关联列举预览、预览登记为
    正式产物，客户端展开的自动取回选择预览产物交付并领取导入。"""
    deployment = Deployment(tmp_path, controlled_clock=True)
    spec = stub_driver_spec(
        {"1": [photo_file("ap-shot"),
               preview_file("ap-preview", "ap-shot")]},
        device_files={"ap-shot": _PHOTO_CONTENT,
                      "ap-preview": _PREVIEW_CONTENT},
        photo_preview_supported=True)
    reuse_client(deployment)
    _init_with_capabilities(deployment, spec)
    state_db = deployment.state_db

    shoot_at = deployment.schedule(1)
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
    plan_path, receipt = deployment.export_plan_with_client(body)

    demo = HostDemo(deployment, host_demo,
                    launcher=write_stub_launcher(deployment))
    failure = demo.watch_failures(
        request_id=receipt["request_id"],
        expected_statuses={action["name"]: "SUCCEEDED" for action in body["actions"]},
    )
    try:
        demo.start()
        demo.submit(plan_path)
        deadline = time.monotonic() + 180
        frozen = deployment.freeze_at(shoot_at)
        wait_for(lambda: query_state(state_db,
            "SELECT p.result_event_id FROM operation_attempts p"
            " JOIN operation_runs r ON r.id=p.run_id"
            " JOIN actions a ON a.id=r.action_id"
            " WHERE a.name='shoot' AND p.result_event_id IS NOT NULL"
            " AND EXISTS(SELECT 1 FROM json_each(p.result_json, '$.observations')"
            " WHERE json_extract(value, '$.type')='photo_taken')") or None,
            timeout_s=max(0, deadline - time.monotonic()),
            message="照片 START 结果未保存", failure=failure)
        running = deployment.resume_clock()
        assert running["utc_anchor_us"] == frozen["utc_us"]
        assert running["monotonic_anchor_ns"] == frozen["monotonic_ns"]
        wait_for(lambda: query_state(
            state_db, "SELECT status FROM actions ORDER BY id"
        ) == [(3,), (3,)] or None,
            timeout_s=max(0, deadline - time.monotonic()), message="拍摄与自动取回未全部成功",
            failure=failure)
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
            timeout_s=120, message="会话最终报告未发布", failure=failure)
        demo.claim()
        wait_for(lambda: (
            len(_delivered_files(deployment.processing)) == 1
            and _report_file(deployment.processing).exists()) or None,
            timeout_s=30, message="领取未移动预览交付与报告", failure=failure)
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
        tmp_path: Path, host_demo: str, reuse_client) -> None:
    """迟到取消经 C 模块递交链：录像与取回完成后交付已领取，取消计
    划按不可撤回收场；携带 ACK 的取消计划一次递交完成取消与累计
    确认吸收，交付字节与原请求终态保持。"""
    deployment = Deployment(tmp_path, controlled_clock=True)
    spec = stub_driver_spec(
        {"1": [video_file("lc-clip")]},
        device_files={"lc-clip": _CANCEL_CLIP_CONTENT})
    reuse_client(deployment)
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
                "scheduled_at": deployment.schedule(1),
                "params": {"type": "video"},
                "policy": {"max_delay_ms": 5000},
            },
            {
                "name": "取回",
                "type": "obtain_action_outputs",
                "scheduled_at": deployment.schedule(2),
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
    failure = demo.watch_failures(
        request_id=receipt["request_id"],
        expected_statuses={action["name"]: "SUCCEEDED" for action in body["actions"]},
    )
    try:
        demo.start(initial_plan=plan_path)
        deadline = time.monotonic() + 180
        deployment.freeze_at(body["actions"][0]["scheduled_at"])
        wait_for(lambda: query_state(state_db,
            "SELECT r.id FROM operation_runs r JOIN actions a ON a.id=r.action_id"
            " WHERE a.name='拍录' AND r.responsibility_key='start/' || a.id"
            " AND r.status IN (3,4)") or None,
            timeout_s=max(0, deadline - time.monotonic()),
            message="录像 START 责任未收场", failure=failure)
        deployment.freeze_at(body["actions"][1]["scheduled_at"])
        deployment.resume_clock()
        wait_for(lambda: query_state(
            state_db, "SELECT status FROM actions ORDER BY id"
        ) == [(3,), (3,)] or None,
            timeout_s=max(0, deadline - time.monotonic()),
            message="录像与取回未全部成功",
            failure=failure)
        report = wait_for(lambda: _report_with_statuses(
            deployment.ready,
            {"拍录": "succeeded", "取回": "succeeded"}) or None,
            timeout_s=120, message="会话最终报告未发布", failure=failure)

        demo.claim()
        wait_for(lambda: (
            len(_delivered_files(deployment.processing)) == 1
            and _report_file(deployment.processing).exists()) or None,
            timeout_s=30, message="领取未移动交付与报告", failure=failure)
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
                    "scheduled_at": deployment.schedule(1),
                    "params": {"target": {"request_id": original_request}},
                },
            ],
        }
        cancel_path, cancel_receipt = deployment.export_plan_with_client(
            cancel_body)
        assert cancel_receipt["has_ack"] is True
        failure = demo.watch_failures(request_id=cancel_receipt["request_id"], expected_statuses={"取消": "SUCCEEDED"})
        demo.submit(cancel_path)
        wait_for(lambda: query_state(
            state_db, "SELECT status FROM actions WHERE name = '取消'"
        ) == [(3,)] or None,
            timeout_s=180, message="取消动作未收场", failure=failure)
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
            timeout_s=180, message="取消计划携带的 ACK 未被吸收", failure=failure)
    finally:
        demo.stop()

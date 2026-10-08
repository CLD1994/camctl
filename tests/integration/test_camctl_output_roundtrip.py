"""I5 取回交付链跨组件组合验收：录像交付存活迟到取消。

真实 camctl CLI 子进程经部署装配桥接入受契约约束的设备替身完成
录像、取回拷贝与交付发布；交付文件被领取进 processing 后，迟到
的取消请求按不可撤回事实收场，交付结果与原请求终态保持不变。客
户端经其服务的真实导入路径保存最终报告并递交 ACK。C 领取模块与
录像媒体工具不在本文件范围（按集成计划 I5 后续链路接入）。

本文件同时覆盖自动预览取回链（I5 checkbox③ 覆盖面）：驱动声明
拍摄参数类型支持预览并按配对关联列举预览文件；客户端展开的自动
取回按预览筛选选择真实登记的预览产物并交付。同一来源的重复自动
关联在受理时登记失败；拍摄执行前的计划级取消联动收场自动取回。
三种拍摄与部分取回组合（X10 剩余验收）由本文件末尾用例覆盖：照
片、录像与延时同计划执行，部分来源的产物被取回交付，未取回产物
保持登记事实。
"""

from __future__ import annotations

import glob
import json
import shutil
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from camctl_fixtures import (
    Deployment,
    future_schedule,
    photo_file,
    preview_file,
    stub_driver_spec,
    video_file,
)

#: 设备侧视频内容；长度与 video_file 条目的 size_bytes 一致，
#: 取回拷贝按登记长度读取并做源端摘要比较。
_CLIP_CONTENT = ("clip-payload-" * 700)[:8192]
#: 设备侧照片原片内容；长度与 photo_file 条目的 size_bytes 一致。
_PHOTO_CONTENT = ("photo-payload-" * 320)[:4096]
#: 设备侧预览内容；长度与 preview_file 条目的 size_bytes 一致。
_PREVIEW_CONTENT = ("preview-payload-" * 160)[:2048]
#: 组合用例的三份设备侧内容（照片、录像、延时序列）。截断前长度
#: 覆盖条目声明的 size_bytes（4096/8192），拷贝按登记长度读取并做
#: 源端摘要比较。
_COMBO_PHOTO_CONTENT = ("combo-photo-" * 400)[:4096]
_COMBO_CLIP_CONTENT = ("combo-clip-" * 745)[:8192]
_COMBO_SEQ_CONTENT = ("combo-seq-" * 820)[:8192]


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


def _wait_until(moment: str) -> None:
    """等待部署墙钟越过给定的计划时刻（秒精度加一秒余量）。"""
    target = datetime.strptime(moment, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc) + timedelta(seconds=1)
    while datetime.now(timezone.utc) < target:
        time.sleep(0.1)


def test_auto_preview_obtains_registered_preview(tmp_path: Path) -> None:
    """自动预览链：驱动按配对关联列举预览文件，预览登记为正式产
    物并保留配对证据，客户端展开的自动取回选择预览产物交付。"""
    deployment = Deployment(tmp_path)
    initialized = deployment.camctl(
        "init", "--config", str(deployment.config_path))
    assert initialized.exit_code == 0, initialized.stderr

    spec = stub_driver_spec(
        {"1": [photo_file("shot-1"),
               preview_file("shot-1-preview", "shot-1")]},
        device_files={"shot-1": _PHOTO_CONTENT,
                      "shot-1-preview": _PREVIEW_CONTENT},
        photo_preview_supported=True)
    deployment.install_client_capabilities(spec)

    # 客户端真实导出：拍摄与自动取回同计划，取回三字段明确填写，
    # scheduled_at 与来源拍摄表达同一时刻。
    shoot_at = future_schedule(1)
    body = {
        "request_id": "0",
        "created_at": "2026-01-15 08:00:00",
        "name": "photo-auto-preview",
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
    submitted = deployment.camctl(
        "submit", str(plan_path), "--config", str(deployment.config_path),
        driver=spec)
    assert submitted.exit_code == 0, submitted.stderr

    # 一个 run 会话完成拍摄、预览列举登记、取回拷贝与交付发布。
    run = deployment.camctl(
        "run", "--config", str(deployment.config_path), driver=spec)
    assert run.exit_code == 0, run.stderr

    state_db = deployment.state_db
    assert _query(state_db, "SELECT status FROM plans") == [(3,)]
    assert _query(
        state_db, "SELECT status FROM actions ORDER BY id") == [(3,), (3,)]
    # 产物目录：原片与预览分别登记；预览设备文件保留原片文件引用
    # 与驱动配对证据，原片文件不携带配对。
    assert _query(
        state_db, "SELECT kind FROM outputs ORDER BY id") == [(1,), (3,)]
    assert _query(
        state_db, "SELECT role FROM device_files ORDER BY id") == [(2,), (3,)]
    assert _query(
        state_db,
        "SELECT original_device_file_id IS NOT NULL,"
        " pairing_evidence_json IS NOT NULL FROM device_files"
        " WHERE role = 3") == [(1, 1)]
    # 自动取回选择预览产物，交付字节是预览内容。
    assert _query(
        state_db,
        "SELECT o.kind FROM obtain_items i"
        " JOIN outputs o ON o.id = i.output_id") == [(3,)]
    delivered = _delivered_files(deployment.ready)
    assert len(delivered) == 1, delivered
    assert delivered[0].read_bytes() == _PREVIEW_CONTENT.encode("utf-8")

    # 报告表达两个动作成功，自动取回保留自动用途展示关系。
    report = _ready_report(deployment)
    actions = {
        action["name"]: action
        for plan in report["plans"] for action in plan["actions"]}
    assert {name: action["status"]
            for name, action in actions.items()} == {
        "shoot": "succeeded", "auto-preview": "succeeded"}
    assert actions["auto-preview"]["automation"] == {
        "purpose": "auto_preview",
        "source_action_instance_id": actions["shoot"]["action_instance_id"],
    }


def test_duplicate_auto_preview_fails_at_admission(tmp_path: Path) -> None:
    """同一来源的两个自动预览取回：受理时全部登记为失败并保留冲突
    范围；拍摄按自身条件正常执行，冲突动作没有交付。"""
    deployment = Deployment(tmp_path)
    initialized = deployment.camctl(
        "init", "--config", str(deployment.config_path))
    assert initialized.exit_code == 0, initialized.stderr

    spec = stub_driver_spec(
        {"1": [photo_file("shot-1")]},
        device_files={"shot-1": _PHOTO_CONTENT},
        photo_preview_supported=True)
    deployment.install_client_capabilities(spec)

    shoot_at = future_schedule(1)
    body = {
        "request_id": "0",
        "created_at": "2026-01-15 08:00:00",
        "name": "photo-duplicate-auto",
        "actions": [
            {
                "name": "shoot",
                "type": "camera_take_photo",
                "device_id": "cam-1",
                "scheduled_at": shoot_at,
                "params": {"type": "single_shot"},
                "policy": {"max_delay_ms": 5000},
            },
            *_repeat_auto_preview(
                "auto-a", "auto-b", source="shoot", scheduled_at=shoot_at),
        ],
    }
    plan_path, _ = deployment.export_plan_with_client(body)
    submitted = deployment.camctl(
        "submit", str(plan_path), "--config", str(deployment.config_path),
        driver=spec)
    assert submitted.exit_code == 0, submitted.stderr

    # 受理事务直接登记冲突失败：拍摄待执行，两个取回已终态失败。
    state_db = deployment.state_db
    assert _query(
        state_db, "SELECT status FROM actions ORDER BY id"
    ) == [(1,), (4,), (4,)]

    run = deployment.camctl(
        "run", "--config", str(deployment.config_path), driver=spec)
    assert run.exit_code == 0, run.stderr
    assert _query(
        state_db, "SELECT status FROM actions ORDER BY id"
    ) == [(3,), (4,), (4,)]
    # 冲突动作没有执行尝试或交付；拍摄保留原片产物。
    assert _query(state_db, "SELECT COUNT(*) FROM deliveries") == [(0,)]

    report = _ready_report(deployment)
    actions = {
        action["name"]: action
        for plan in report["plans"] for action in plan["actions"]}
    assert actions["shoot"]["status"] == "succeeded"
    for name in ("auto-a", "auto-b"):
        error = actions[name]["error"]
        assert actions[name]["status"] == "failed"
        assert error["code"] == "duplicate_auto_preview"
        assert error["stage"] == "admission"
        assert error["details"]["source_action_name"] == "shoot"
        assert sorted(error["details"]["obtain_action_names"]) == [
            "auto-a", "auto-b"]


def _repeat_auto_preview(*names: str, source: str,
                         scheduled_at: str) -> list[dict]:
    """构造多个引用同一来源拍摄的自动预览取回动作。"""
    return [
        {
            "name": name,
            "type": "obtain_action_outputs",
            "scheduled_at": scheduled_at,
            "params": {
                "source": {"action_name": source},
                "filter": "preview",
                "purpose": "auto_preview",
            },
        }
        for name in names
    ]


def test_plan_cancel_before_start_settles_auto_preview(
        tmp_path: Path) -> None:
    """拍摄执行前的计划级取消：计划实例入口直接针对计划内动作，
    待执行拍摄与关联的自动预览取回都按取消收场，没有产物或交付。"""
    deployment = Deployment(tmp_path)
    initialized = deployment.camctl(
        "init", "--config", str(deployment.config_path))
    assert initialized.exit_code == 0, initialized.stderr

    shoot_at = future_schedule(30)
    cancel_at = future_schedule(1)
    spec = stub_driver_spec(
        {"1": [photo_file("shot-1"),
               preview_file("shot-1-preview", "shot-1")]},
        device_files={"shot-1": _PHOTO_CONTENT,
                      "shot-1-preview": _PREVIEW_CONTENT},
        photo_preview_supported=True)
    deployment.install_client_capabilities(spec)

    body = {
        "request_id": "0",
        "created_at": "2026-01-15 08:00:00",
        "name": "photo-auto-preview",
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
    submitted = deployment.camctl(
        "submit", str(plan_path), "--config", str(deployment.config_path),
        driver=spec)
    assert submitted.exit_code == 0, submitted.stderr

    state_db = deployment.state_db
    plan_instance = _query(
        state_db, "SELECT id FROM plans WHERE name = 'photo-auto-preview'"
    )[0][0]

    # 取消时刻先于拍摄时刻：等待墙钟越过取消时刻再提交取消计划。
    _wait_until(cancel_at)
    cancel_body = {
        "request_id": "0",
        "created_at": "2026-01-15 08:00:00",
        "name": "cancel-before-start",
        "actions": [
            {
                "name": "cancel",
                "type": "cancel_task",
                "scheduled_at": cancel_at,
                "params": {"target": {"plan_instance_id": str(plan_instance)}},
            },
        ],
    }
    cancel_path, _ = deployment.export_plan_with_client(cancel_body)
    cancel_submitted = deployment.camctl(
        "submit", str(cancel_path), "--config", str(deployment.config_path),
        driver=spec)
    assert cancel_submitted.exit_code == 0, cancel_submitted.stderr

    # 一个 run 会话执行取消：待执行动作按取消收场，无需等到拍摄
    # 时刻。
    run = deployment.camctl(
        "run", "--config", str(deployment.config_path), driver=spec)
    assert run.exit_code == 0, run.stderr

    assert _query(state_db, "SELECT status FROM plans") == [(3,), (3,)]
    assert _query(
        state_db, "SELECT status FROM actions ORDER BY id"
    ) == [(6,), (6,), (3,)]
    # 没有拍摄执行与取回交付：产物和交付都不存在。
    assert _query(state_db, "SELECT COUNT(*) FROM outputs") == [(0,)]
    assert _query(state_db, "SELECT COUNT(*) FROM deliveries") == [(0,)]

    report = _ready_report(deployment)
    statuses = {
        action["name"]: action["status"]
        for plan in report["plans"] for action in plan["actions"]}
    assert statuses == {
        "shoot": "canceled", "auto-preview": "canceled",
        "cancel": "succeeded"}


def test_three_capture_kinds_partial_obtain(tmp_path: Path) -> None:
    """三种拍摄与部分取回组合：照片、录像与延时同计划顺序执行，
    照片与延时产物被取回交付，录像产物保持登记不建交付。"""
    deployment = Deployment(tmp_path)
    initialized = deployment.camctl(
        "init", "--config", str(deployment.config_path))
    assert initialized.exit_code == 0, initialized.stderr

    # 动作主键按提交顺序：拍照 1、拍录 2、延时 3；列举按主键回询。
    spec = stub_driver_spec(
        {"1": [photo_file("combo-shot")],
         "2": [video_file("combo-clip")],
         "3": [video_file("combo-seq")]},
        device_files={"combo-shot": _COMBO_PHOTO_CONTENT,
                      "combo-clip": _COMBO_CLIP_CONTENT,
                      "combo-seq": _COMBO_SEQ_CONTENT})
    deployment.install_client_capabilities(spec)

    shoot_at = future_schedule(1)
    body = {
        "request_id": "0",
        "created_at": "2026-01-15 08:00:00",
        "name": "three-kinds-partial",
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
        ],
    }
    plan_path, _ = deployment.export_plan_with_client(body)
    submitted = deployment.camctl(
        "submit", str(plan_path), "--config", str(deployment.config_path),
        driver=spec)
    assert submitted.exit_code == 0, submitted.stderr

    # 一个 run 会话顺序完成三种拍摄与两次取回（一次一份拷贝）。
    run = deployment.camctl(
        "run", "--config", str(deployment.config_path), driver=spec)
    assert run.exit_code == 0, run.stderr

    state_db = deployment.state_db
    assert _query(state_db, "SELECT status FROM plans") == [(3,)]
    assert _query(
        state_db, "SELECT status FROM actions ORDER BY id"
    ) == [(3,), (3,), (3,), (3,), (3,)]
    # 三种拍摄各自登记原片产物；只有被取回来源的两份建立交付。
    assert _query(
        state_db, "SELECT source_action_id, kind FROM outputs"
        " ORDER BY source_action_id") == [(1, 1), (2, 1), (3, 1)]
    assert _query(
        state_db,
        "SELECT d.action_id FROM deliveries d ORDER BY d.id") == [(4,), (5,)]
    # ready 交付两份：字节与各自来源内容一致（部分取回事实）。
    delivered = _delivered_files(deployment.ready)
    assert len(delivered) == 2, delivered
    assert {path.read_bytes() for path in delivered} == {
        _COMBO_PHOTO_CONTENT.encode("utf-8"),
        _COMBO_SEQ_CONTENT.encode("utf-8")}

    # 报告表达五个动作全部成功；两个取回动作各自交付自己的来源。
    report = _ready_report(deployment)
    statuses = {
        action["name"]: action["status"]
        for plan in report["plans"] for action in plan["actions"]}
    assert statuses == {
        "拍照": "succeeded", "拍录": "succeeded", "延时": "succeeded",
        "取照片": "succeeded", "取延时": "succeeded"}


#: 跨计划引用用例的设备侧内容；截断前长度覆盖 4096 声明。
_CROSS_FIRST_CONTENT = ("cross-plan-first-" * 250)[:4096]
_CROSS_SECOND_CONTENT = ("cross-plan-second-" * 250)[:4096]


def test_cross_plan_reference_and_multi_request(tmp_path: Path) -> None:
    """跨计划引用与多请求组合：三个请求的计划在同一部署先后执行，
    第二个请求的取回按稳定动作实例 ID 引用第一个请求的拍摄产物并
    交付；第三个请求引用不存在的实例在执行资格取得后按解析失败
    收场，不产生交付，已确定的跨计划交付不受影响。"""
    deployment = Deployment(tmp_path)
    initialized = deployment.camctl(
        "init", "--config", str(deployment.config_path))
    assert initialized.exit_code == 0, initialized.stderr

    # 动作主键按提交顺序：首拍 1、次拍 2、取回 3、坏引用取回 4；
    # 列举按主键回询，取回引用不需要设备侧条目。
    spec = stub_driver_spec(
        {"1": [photo_file("cross-first")],
         "2": [photo_file("cross-second")]},
        device_files={"cross-first": _CROSS_FIRST_CONTENT,
                      "cross-second": _CROSS_SECOND_CONTENT})
    deployment.install_client_capabilities(spec)
    state_db = deployment.state_db

    # 第一个请求：一次拍摄取得稳定动作实例与产物身份。
    first_body = {
        "request_id": "0",
        "created_at": "2026-01-15 08:00:00",
        "name": "cross-plan-first",
        "actions": [
            {
                "name": "首拍",
                "type": "camera_take_photo",
                "device_id": "cam-1",
                "scheduled_at": future_schedule(1),
                "params": {"type": "single_shot"},
                "policy": {"max_delay_ms": 5000},
            },
        ],
    }
    first_path, _ = deployment.export_plan_with_client(first_body)
    first_submit = deployment.camctl(
        "submit", str(first_path), "--config", str(deployment.config_path),
        driver=spec)
    assert first_submit.exit_code == 0, first_submit.stderr
    first_run = deployment.camctl(
        "run", "--config", str(deployment.config_path), driver=spec)
    assert first_run.exit_code == 0, first_run.stderr
    first_action = _query(
        state_db, "SELECT id FROM actions WHERE name = '首拍'")[0][0]

    # 第二个请求：自己再拍一张，取回按动作实例 ID 引用第一个请求
    # 的产物（跨计划引用；实例引用在执行资格取得后一次固定）。
    second_body = {
        "request_id": "0",
        "created_at": "2026-01-15 08:00:00",
        "name": "cross-plan-second",
        "actions": [
            {
                "name": "次拍",
                "type": "camera_take_photo",
                "device_id": "cam-1",
                "scheduled_at": future_schedule(1),
                "params": {"type": "single_shot"},
                "policy": {"max_delay_ms": 5000},
            },
            {
                "name": "取回首拍",
                "type": "obtain_action_outputs",
                "scheduled_at": future_schedule(1),
                "params": {
                    "source": {"action_instance_id": str(first_action)},
                    "purpose": "manual",
                },
            },
        ],
    }
    second_path, _ = deployment.export_plan_with_client(second_body)
    second_submit = deployment.camctl(
        "submit", str(second_path), "--config", str(deployment.config_path),
        driver=spec)
    assert second_submit.exit_code == 0, second_submit.stderr
    second_run = deployment.camctl(
        "run", "--config", str(deployment.config_path), driver=spec)
    assert second_run.exit_code == 0, second_run.stderr

    # 三个动作全部成功；取回解析固定到第一个请求的真实来源计划。
    assert _query(
        state_db, "SELECT status FROM actions ORDER BY id") == [(3,), (3,), (3,)]
    assert _query(
        state_db,
        "SELECT source_resolution_state, resolved_source_plan_id"
        " FROM actions WHERE name = '取回首拍'"
    ) == [(2, _query(
        state_db, "SELECT plan_id FROM actions WHERE id = ?", (first_action,))[0][0])]
    # 交付恰一份且字节是首拍内容：跨计划引用精确指向来源动作，
    # 同计划的次拍产物保持登记、不被误取。
    assert _query(
        state_db, "SELECT action_id FROM deliveries") == [(3,)]
    assert _query(
        state_db, "SELECT source_action_id FROM outputs ORDER BY id"
    ) == [(1,), (2,)]
    delivered = _delivered_files(deployment.ready)
    assert [path.read_bytes() for path in delivered] == [
        _CROSS_FIRST_CONTENT.encode("utf-8")]

    # 第三个请求：引用不存在的动作实例。受理不解析实例引用（提交
    # 成功），执行资格取得后按解析失败收场：动作失败携带公共原因，
    # 不产生交付，此前确定的跨计划交付保持不变。
    third_body = {
        "request_id": "0",
        "created_at": "2026-01-15 08:00:00",
        "name": "cross-plan-broken",
        "actions": [
            {
                "name": "坏引用取回",
                "type": "obtain_action_outputs",
                "scheduled_at": future_schedule(1),
                "params": {
                    "source": {"action_instance_id": "999999"},
                    "purpose": "manual",
                },
            },
        ],
    }
    third_path, _ = deployment.export_plan_with_client(third_body)
    third_submit = deployment.camctl(
        "submit", str(third_path), "--config", str(deployment.config_path),
        driver=spec)
    assert third_submit.exit_code == 0, third_submit.stderr
    third_run = deployment.camctl(
        "run", "--config", str(deployment.config_path), driver=spec)
    assert third_run.exit_code == 0, third_run.stderr
    rows = _query(
        state_db,
        "SELECT status, error_code, source_resolution_state,"
        " resolved_source_plan_id FROM actions WHERE name = '坏引用取回'")
    assert rows == [(4, 20, 3, None)], rows
    assert json.loads(_query(
        state_db,
        "SELECT error_details_json FROM actions WHERE name = '坏引用取回'"
    )[0][0])["reason"] == "action_not_found"
    assert _query(
        state_db, "SELECT status FROM plans ORDER BY id") == [(3,), (3,), (3,)]
    assert _query(
        state_db, "SELECT action_id FROM deliveries") == [(3,)]

    # 报告表达三请求组合的最终事实；客户端导入并递交 ACK。
    report = _ready_report(deployment)
    statuses = {
        action["name"]: action["status"]
        for plan in report["plans"] for action in plan["actions"]}
    assert statuses == {
        "首拍": "succeeded", "次拍": "succeeded", "取回首拍": "succeeded",
        "坏引用取回": "failed"}
    report_id = report["report_id"]
    saved = deployment.import_reports_with_client(deployment.ready)
    assert saved["saved_report_ids"] == [report_id]
    ack = deployment.write_plan(
        {"request_id": "0", "last_report_id": report_id})
    absorbed = deployment.camctl(
        "run", str(ack), "--config", str(deployment.config_path),
        driver=spec)
    assert absorbed.exit_code == 0, absorbed.stderr
    assert _query(
        state_db,
        "SELECT acknowledged_report_id, acknowledged_wm FROM runtime_state"
    ) == [(int(report_id), report["to_wm"])]

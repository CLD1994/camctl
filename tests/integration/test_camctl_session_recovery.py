"""I5 会话恢复与边界输入跨组件验收。

真实 camctl CLI 子进程经部署装配桥接入受契约约束的设备替身。本
文件覆盖会话与输入边界的组合：run 进程执行中被终止后由下一个会话
从状态库恢复（WAL 事实不丢）、运行中与并发 submit 的受理互不干扰、
同请求身份重送不重复受理、未来动作不阻塞会话退出、启动窗口已过的
动作按过期收场、报告文件交接失败不阻止设备动作且责任保留、设备定
义改变与迟到文件不改写已确定结果。
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
    stub_driver_spec,
    terminate_process_tree,
    timelapse_plan,
    video_file,
)

#: 设备侧视频内容；长度与 video_file 条目的 size_bytes 一致。
_CLIP_CONTENT = ("clip-payload-" * 700)[:8192]


def _query(state_db: Path, sql: str, params=()) -> list[tuple]:
    """查询部署状态库（busy 覆盖与子进程的短暂竞争）。

    读写模式打开：进程被强杀后遗留的 WAL 共享内存需要可写连接执
    行恢复，只读连接在 Windows 上会报磁盘 I/O 错误。树杀后操作系
    统清理被杀进程的文件句柄存在短暂延迟，期间恢复性打开可能报
    磁盘 I/O 错误，按短退避重试。
    """
    deadline = time.monotonic() + 10.0
    while True:
        try:
            connection = sqlite3.connect(state_db, timeout=30)
            try:
                return connection.execute(sql, params).fetchall()
            finally:
                connection.close()
        except sqlite3.OperationalError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _await_row(state_db: Path, sql: str, params=(), *,
               timeout_s: float = 30.0) -> list[tuple]:
    """轮询查询直到出现行；同步点，不使用随机 sleep。"""
    deadline = time.monotonic() + timeout_s
    while True:
        rows = _query(state_db, sql, params)
        if rows:
            return rows
        assert time.monotonic() < deadline, f"等待超时: {sql}"


def _await_status(state_db: Path, name: str, status: int) -> None:
    """等待指定名称动作到达给定状态后返回。"""
    _await_row(
        state_db,
        "SELECT id FROM actions WHERE name = ? AND status = ?",
        (name, status))


def _ready_report(deployment: Deployment) -> dict:
    """ready 根目录中唯一报告的解析正文。"""
    names = sorted(glob.glob("status-report-*.json",
                             root_dir=str(deployment.ready)))
    assert len(names) == 1, names
    content = (deployment.ready / names[0]).read_text("utf-8")
    return json.loads(content)


def _past_schedule(seconds: int) -> str:
    """早于当前部署墙钟的计划时刻（UTC 表达）。"""
    moment = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def test_process_restart_resumes_interrupted_work(tmp_path: Path) -> None:
    """run 进程执行中被终止：已保存事实不丢，下一个会话恢复剩余等
    待并完成迟到提交的新计划；运行间配置重载不破坏既有事实。"""
    deployment = Deployment(tmp_path)
    initialized = deployment.camctl(
        "init", "--config", str(deployment.config_path))
    assert initialized.exit_code == 0, initialized.stderr

    # 等待时长 5 秒：结果列举被门阻塞，制造会话必须跨进程恢复的
    # 执行中事实。启动调用与等待安排先行完成，中断落在结果等待
    # 阶段；恢复会话按各自动作主键回询结果列举（剧本 files 的键），
    # 两份计划分别核实自己的文件。
    spec = stub_driver_spec(
        {"1": [video_file("seq-1")], "2": [photo_file("shot-a")]},
        timelapse_duration_s=5.0,
        gates={"result": "result.gate"},
        device_files={"seq-1": _CLIP_CONTENT,
                      "shot-a": "photo-payload-a"})
    deployment.install_client_capabilities(spec)

    plan_path, _ = deployment.export_plan_with_client(
        timelapse_plan("0", future_schedule(1)))
    submitted = deployment.camctl(
        "submit", str(plan_path), "--config", str(deployment.config_path),
        driver=spec)
    assert submitted.exit_code == 0, submitted.stderr

    # 第一个 run 会话推进到启动责任收场（等待阶段）后被进程级终
    # 止（真实重启，不是会话正常退出）；终止不落在启动调用在途的
    # 窗口内，避免把未保存的调用结果留给恢复语义之外的状态。
    first = deployment.start_camctl(
        "run", "--config", str(deployment.config_path), driver=spec)
    state_db = deployment.state_db
    _await_row(
        state_db,
        "SELECT 1 FROM operation_runs r"
        " JOIN actions a ON a.id = r.action_id"
        " WHERE a.name = 'timelapse'"
        " AND r.responsibility_key = 'start/' || a.id AND r.status IN (3, 4)")
    # 会话存活期间提交第二份计划：关闭阶段到达的输入不丢失。
    photo_path, _ = deployment.export_plan_with_client({
        "request_id": "1",
        "created_at": "2026-01-15 08:00:00",
        "name": "late-arrival",
        "actions": [{
            "name": "shoot",
            "type": "camera_take_photo",
            "device_id": "cam-1",
            "scheduled_at": future_schedule(6),
            "params": {"type": "single_shot"},
            "policy": {"max_delay_ms": 5000},
        }],
    })
    late = deployment.camctl(
        "submit", str(photo_path), "--config", str(deployment.config_path),
        driver=spec)
    assert late.exit_code == 0, late.stderr

    terminate_process_tree(first)
    # 中断时刻的事实：动作执行中、活动与等待安排已保存，进程死亡
    # 不产生部分写入。
    assert _query(
        state_db, "SELECT status FROM actions WHERE name = 'timelapse'"
    ) == [(2,)]

    # 运行间配置重载：结果核实重试间隔从 0 改为 2 秒，只影响后续
    # 轮次的等待节奏，不改写已保存事实。
    config = deployment.config_path.read_text(encoding="utf-8")
    deployment.config_path.write_text(
        config.replace(
            "[devices.cam-1.result_check]\nretry_interval_s = \"0\"",
            "[devices.cam-1.result_check]\nretry_interval_s = \"2\""),
        encoding="utf-8")
    (deployment.gates / "result.gate").write_text("", encoding="utf-8")

    recovered = deployment.camctl(
        "run", "--config", str(deployment.config_path), driver=spec)
    assert recovered.exit_code == 0, recovered.stderr

    # 恢复会话完成两份计划：中断的延时任务从剩余等待恢复并成功，
    # 关闭阶段到达的照片计划被执行。
    assert _query(state_db, "SELECT status FROM plans") == [(3,), (3,)]
    assert _query(
        state_db, "SELECT status FROM actions WHERE name = 'timelapse'"
    ) == [(3,)]
    assert _query(
        state_db, "SELECT status FROM actions WHERE name = 'shoot'"
    ) == [(3,)]
    # 两动作按各自文件类别核实同一列举集合，各登记一份原片产物。
    assert _query(
        state_db,
        "SELECT a.name FROM outputs o"
        " JOIN actions a ON a.id = o.source_action_id ORDER BY a.name"
    ) == [("shoot",), ("timelapse",)]
    report = _ready_report(deployment)
    statuses = {
        action["name"]: action["status"]
        for plan in report["plans"] for action in plan["actions"]}
    assert statuses == {"timelapse": "succeeded", "shoot": "succeeded"}


def test_concurrent_submits_and_duplicate_identity(tmp_path: Path) -> None:
    """并发 submit 交错执行互不干扰；同请求身份重送不重复受理。"""
    deployment = Deployment(tmp_path)
    initialized = deployment.camctl(
        "init", "--config", str(deployment.config_path))
    assert initialized.exit_code == 0, initialized.stderr

    spec = stub_driver_spec(
        {"1": [photo_file("shot-1")], "2": [photo_file("shot-2")]},
        device_files={"shot-1": "content-one",
                      "shot-2": "content-two"})
    deployment.install_client_capabilities(spec)

    def body(request_id: str, action: str) -> dict:
        return {
            "request_id": request_id,
            "created_at": "2026-01-15 08:00:00",
            "name": f"plan-{request_id}",
            "actions": [{
                "name": action,
                "type": "camera_take_photo",
                "device_id": "cam-1",
                "scheduled_at": future_schedule(1),
                "params": {"type": "single_shot"},
                "policy": {"max_delay_ms": 5000},
            }],
        }

    first_path, _ = deployment.export_plan_with_client(body("0", "shoot-a"))
    second_path, _ = deployment.export_plan_with_client(body("1", "shoot-b"))

    # 三个提交进程交错执行：两份不同计划并发受理，同一正文（同请求
    # 身份）重送表达提交结果未知的请求重送。
    processes = [
        deployment.start_camctl(
            "submit", str(path), "--config", str(deployment.config_path),
            driver=spec)
        for path in (first_path, second_path, second_path)]
    results = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=120)
        results.append((process.returncode, stdout, stderr))
    for code, _, stderr in results:
        assert code == 0, stderr

    state_db = deployment.state_db
    # 三个提交只建立两份计划：同身份重送复用首次受理。
    assert _query(state_db, "SELECT COUNT(*) FROM plans") == [(2,)]
    assert _query(state_db, "SELECT COUNT(*) FROM actions") == [(2,)]

    run = deployment.camctl(
        "run", "--config", str(deployment.config_path), driver=spec)
    assert run.exit_code == 0, run.stderr
    assert _query(state_db, "SELECT status FROM plans") == [(3,), (3,)]
    assert _query(
        state_db, "SELECT status FROM actions ORDER BY id") == [(3,), (3,)]
    # 同身份请求不重复执行：两份产物恰好对应两份计划。
    assert _query(state_db, "SELECT COUNT(*) FROM outputs") == [(2,)]


def test_future_action_waits_and_past_window_expires(
        tmp_path: Path) -> None:
    """未来动作不阻塞会话退出；启动窗口已过的动作按过期收场。"""
    deployment = Deployment(tmp_path)
    initialized = deployment.camctl(
        "init", "--config", str(deployment.config_path))
    assert initialized.exit_code == 0, initialized.stderr

    spec = stub_driver_spec({"1": [photo_file("shot-1")]})
    deployment.install_client_capabilities(spec)

    future_path, _ = deployment.export_plan_with_client({
        "request_id": "0",
        "created_at": "2026-01-15 08:00:00",
        "name": "far-future",
        "actions": [{
            "name": "shoot-future",
            "type": "camera_take_photo",
            "device_id": "cam-1",
            "scheduled_at": future_schedule(5),
            "params": {"type": "single_shot"},
            "policy": {"max_delay_ms": 5000},
        }],
    })
    submitted = deployment.camctl(
        "submit", str(future_path), "--config", str(deployment.config_path),
        driver=spec)
    assert submitted.exit_code == 0, submitted.stderr

    state_db = deployment.state_db
    # 会话把未到时动作计入未完成责任并驻留：到时前动作保持待执行，
    # 不被提前执行或判过期。
    session = deployment.start_camctl(
        "run", "--config", str(deployment.config_path), driver=spec)
    _await_status(state_db, "shoot-future", 1)
    assert _query(
        state_db, "SELECT status FROM actions WHERE name = 'shoot-future'"
    ) == [(1,)]
    stdout, stderr = session.communicate(timeout=120)
    assert session.returncode == 0, stderr or stdout
    # 到时后由驻留会话正常执行。
    assert _query(
        state_db, "SELECT status FROM actions WHERE name = 'shoot-future'"
    ) == [(3,)]

    # 启动窗口已过且从未派发：按过期收场，不执行设备调用。
    past_path, _ = deployment.export_plan_with_client({
        "request_id": "1",
        "created_at": "2026-01-15 08:00:00",
        "name": "missed-window",
        "actions": [{
            "name": "shoot-past",
            "type": "camera_take_photo",
            "device_id": "cam-1",
            "scheduled_at": _past_schedule(120),
            "params": {"type": "single_shot"},
            "policy": {"max_delay_ms": 5000},
        }],
    })
    expired = deployment.camctl(
        "submit", str(past_path), "--config", str(deployment.config_path),
        driver=spec)
    assert expired.exit_code == 0, expired.stderr
    settling = deployment.camctl(
        "run", "--config", str(deployment.config_path), driver=spec)
    assert settling.exit_code == 0, settling.stderr

    assert _query(
        state_db, "SELECT status FROM actions WHERE name = 'shoot-past'"
    ) == [(5,)]
    assert _query(
        state_db, "SELECT status FROM plans WHERE name = 'missed-window'"
    ) == [(3,)]
    # 过期动作不登记产物；到时执行的未来动作恰好登记一份原片。
    assert _query(
        state_db,
        "SELECT a.name FROM outputs o"
        " JOIN actions a ON a.id = o.source_action_id"
        " ORDER BY a.name") == [("shoot-future",)]

    report = _ready_report(deployment)
    actions = {
        action["name"]: action["status"]
        for plan in report["plans"] for action in plan["actions"]}
    assert actions["shoot-past"] == "expired"
    assert actions["shoot-future"] == "succeeded"


def test_report_failure_preserves_device_work_and_redelivers(
        tmp_path: Path) -> None:
    """报告文件交接失败不阻止设备动作：动作与计划按事实终态，报告
    责任保留并由后续会话补交付。"""
    deployment = Deployment(tmp_path)
    initialized = deployment.camctl(
        "init", "--config", str(deployment.config_path))
    assert initialized.exit_code == 0, initialized.stderr

    spec = stub_driver_spec(
        {"1": [photo_file("shot-1")]},
        device_files={"shot-1": "content-one"})
    deployment.install_client_capabilities(spec)

    # ready 位置被普通文件占据：报告无法写入部署绑定目录。
    shutil.rmtree(deployment.ready)
    deployment.ready.write_text("", encoding="utf-8")

    plan_path, _ = deployment.export_plan_with_client({
        "request_id": "0",
        "created_at": "2026-01-15 08:00:00",
        "name": "photo-despite-report-failure",
        "actions": [{
            "name": "shoot",
            "type": "camera_take_photo",
            "device_id": "cam-1",
            "scheduled_at": future_schedule(1),
            "params": {"type": "single_shot"},
            "policy": {"max_delay_ms": 5000},
        }],
    })
    submitted = deployment.camctl(
        "submit", str(plan_path), "--config", str(deployment.config_path),
        driver=spec)
    assert submitted.exit_code == 0, submitted.stderr

    failing = deployment.camctl(
        "run", "--config", str(deployment.config_path), driver=spec)
    state_db = deployment.state_db
    # 设备动作与计划不被报告交接失败拖住：按事实成功终态。
    _await_status(state_db, "shoot", 3)
    assert _query(state_db, "SELECT status FROM plans") == [(3,)]
    assert _query(state_db, "SELECT kind FROM outputs") == [(1,)]
    assert not glob.glob("status-report-*.json", root_dir=str(deployment.ready))

    # 恢复部署绑定目录后，报告责任由新的会话补交付。
    deployment.ready.unlink()
    deployment.ready.mkdir()
    redelivering = deployment.camctl(
        "run", "--config", str(deployment.config_path), driver=spec)
    assert redelivering.exit_code == 0, redelivering.stderr

    report = _ready_report(deployment)
    statuses = {
        action["name"]: action["status"]
        for plan in report["plans"] for action in plan["actions"]}
    assert statuses == {"shoot": "succeeded"}
    saved = deployment.import_reports_with_client(deployment.ready)
    assert saved["saved_report_ids"] == [report["report_id"]]


def test_late_result_preserves_terminal_decision(tmp_path: Path) -> None:
    """设备定义改变与迟到文件不改写已确定结果：集合核实失败的动作
    保持终态与无产物事实。"""
    deployment = Deployment(tmp_path)
    initialized = deployment.camctl(
        "init", "--config", str(deployment.config_path))
    assert initialized.exit_code == 0, initialized.stderr

    # 首个驱动剧本列举空集合：延时任务等待完成后可靠确认没有视频
    # 产物，按 no_outputs 失败收场且不登记产物。
    empty_spec = stub_driver_spec({"1": []}, timelapse_duration_s=3.0)
    deployment.install_client_capabilities(empty_spec)

    plan_path, _ = deployment.export_plan_with_client(
        timelapse_plan("0", future_schedule(1)))
    submitted = deployment.camctl(
        "submit", str(plan_path), "--config", str(deployment.config_path),
        driver=empty_spec)
    assert submitted.exit_code == 0, submitted.stderr

    first = deployment.camctl(
        "run", "--config", str(deployment.config_path), driver=empty_spec)
    assert first.exit_code == 0, first.stderr
    state_db = deployment.state_db
    assert _query(
        state_db, "SELECT status FROM actions WHERE name = 'timelapse'"
    ) == [(4,)]
    assert _query(state_db, "SELECT COUNT(*) FROM outputs") == [(0,)]

    # 设备绑定改变：新剧本在收场后报告视频文件（迟到结果）。
    late_spec = stub_driver_spec(
        {"1": [video_file("late-1")]},
        timelapse_duration_s=3.0,
        device_files={"late-1": _CLIP_CONTENT})
    second = deployment.camctl(
        "run", "--config", str(deployment.config_path), driver=late_spec)
    assert second.exit_code == 0, second.stderr

    # 迟到结果不改写确定终态：动作保持失败，产物不登记。
    assert _query(
        state_db, "SELECT status FROM actions WHERE name = 'timelapse'"
    ) == [(4,)]
    assert _query(state_db, "SELECT COUNT(*) FROM outputs") == [(0,)]
    assert _query(state_db, "SELECT status FROM plans") == [(3,)]

    report = _ready_report(deployment)
    statuses = {
        action["name"]: action["status"]
        for plan in report["plans"] for action in plan["actions"]}
    assert statuses == {"timelapse": "failed"}

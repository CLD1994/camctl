"""I6 跨组件媒体链组合验收：异常录像检查、无重编码修复与修复成品取回。

真实 camctl CLI 子进程经部署装配桥接入受契约约束的设备替身；设备侧
提供真实媒体字节，检查与修复使用部署环境的真实 ffprobe/ffmpeg。两
条用例分别覆盖计时证据不足（受限会话保守收场后由正常会话拷贝检查
时长）与可信计时多录（跨会话对账停止后直接修复，不取回探测时长）
两条进入修复的路径；修复成品提升为正式产物后由默认取回交付到
ready，交付字节与修复成品一致。另以真实 Linux 进程验证受管工具后
代保持进程组归属（R-10 的软件层事实；主程序组收场归 I1/I2/B7）。

本机没有 ffprobe/ffmpeg 时两媒体用例按工具缺席跳过（与 host_files
组件测试同一环境语义）；进程组用例仅 POSIX 执行。
"""

from __future__ import annotations

import asyncio
import glob
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from camctl_fixtures import (
    Deployment,
    future_schedule,
    stub_driver_spec,
    terminate_process_tree,
    video_file,
)

_MEDIA_TOOLS = pytest.mark.skipif(
    shutil.which("ffprobe") is None or shutil.which("ffmpeg") is None,
    reason="本用例组合需要部署环境提供真实 ffprobe/ffmpeg",
)

#: 源视频时长（秒）：严格超过目标 3 秒加余量 0.5 秒的修复门槛。
_SOURCE_DURATION_S = 5
#: 录像目标时长（秒）：替身驱动的录像任务契约值。
_TARGET_DURATION_S = 3.0


def _query(state_db: Path, sql: str, params=()) -> list[tuple]:
    """只读连接查询部署状态库（busy 覆盖与子进程的短暂竞争）。"""
    connection = sqlite3.connect(
        f"file:{state_db.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def _await_row(state_db: Path, sql: str, params=(), *,
               timeout_s: float = 60.0) -> list[tuple]:
    """轮询查询直到出现行；同步点，不使用随机 sleep。"""
    deadline = time.monotonic() + timeout_s
    while True:
        rows = _query(state_db, sql, params)
        if rows:
            return rows
        assert time.monotonic() < deadline, f"等待超时: {sql}"


def _recover_wal(state_db: Path) -> None:
    """强杀会话后以读写连接触发 WAL 恢复，随后只读连接才可用。"""
    connection = sqlite3.connect(state_db, timeout=30)
    try:
        connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
    finally:
        connection.close()


def _generate_source_video(target: Path) -> None:
    """生成真实源视频：H.264 编码、时长严格超过修复门槛。"""
    completed = subprocess.run(
        ["ffmpeg", "-nostdin", "-y",
         "-f", "lavfi", "-i", f"testsrc=duration={_SOURCE_DURATION_S}:rate=15",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(target)],
        capture_output=True, text=True, timeout=120)
    assert completed.returncode == 0, completed.stderr


def _probe_media_facts(path: Path) -> tuple[Decimal, set[str]]:
    """以真实 ffprobe 读取时长与视频编码名（无重编码断言依据）。"""
    completed = subprocess.run(
        ["ffprobe", "-v", "error",
         "-show_entries", "format=duration:stream=codec_type:stream=codec_name",
         "-of", "json", str(path)],
        capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    duration = Decimal(payload["format"]["duration"])
    codecs = {
        stream["codec_name"]
        for stream in payload["streams"]
        if stream.get("codec_type") == "video"}
    assert codecs, payload
    return duration, codecs


def _ready_report(deployment: Deployment) -> dict:
    """ready 根目录中唯一报告的解析正文。"""
    names = sorted(glob.glob("status-report-*.json",
                             root_dir=str(deployment.ready)))
    assert len(names) == 1, names
    return json.loads((deployment.ready / names[0]).read_text("utf-8"))


def _delivered_files(directory: Path) -> list[Path]:
    """目录中的交付文件；报告文件不属于交付。"""
    return sorted(
        path for path in directory.iterdir() if path.is_file()
        and not path.name.startswith("status-report-"))


def _submit_record_and_obtain(deployment: Deployment, spec: dict) -> None:
    """受理录像与取回同计划：取回按名称引用来源，默认选择修复成品。"""
    body = {
        "request_id": "3001",
        "created_at": "2026-01-15 08:00:00",
        "name": "record-repair-obtain",
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
    plan_path = deployment.write_plan(body)
    submitted = deployment.camctl(
        "submit", str(plan_path), "--config", str(deployment.config_path),
        driver=spec)
    assert submitted.exit_code == 0, submitted.stderr
    assert submitted.message() == {"kind": "succeeded", "body": {"needs_run": True}}


def _start_recording_and_interrupt(deployment: Deployment,
                                   spec: dict) -> int:
    """启动 run 会话执行录像，确认启动后终止会话树。

    返回启动确认的墙钟微秒；录像保持执行中、停止未尝试，供后续会
    话按对账或保守收场接管。
    """
    process = deployment.start_camctl(
        "run", "--config", str(deployment.config_path), driver=spec)
    try:
        rows = _await_row(
            deployment.state_db,
            "SELECT started_at FROM device_activities"
            " WHERE started_at IS NOT NULL")
    finally:
        terminate_process_tree(process)
    _recover_wal(deployment.state_db)
    started_at_us = int(rows[0][0])
    assert _query(
        deployment.state_db,
        "SELECT status FROM actions WHERE type = 2") == [(2,)]
    return started_at_us


def _wait_wall_past(moment: datetime) -> None:
    """等待部署墙钟越过给定时刻（加一秒余量）。"""
    target = moment + timedelta(seconds=1)
    while datetime.now(timezone.utc) < target:
        time.sleep(0.1)


def _assert_repaired_delivery(deployment: Deployment) -> None:
    """修复成品的交付事实：默认取回只选修复成品，字节与登记一致。

    交付文件必须是真实媒体：时长裁剪到目标附近（严格短于源）、编
    码名与源一致证明无重新编码；字节摘要与提升的修复输出登记相等。
    """
    state_db = deployment.state_db
    assert _query(
        state_db, "SELECT kind FROM outputs ORDER BY id") == [(1,), (2,)]
    assert _query(
        state_db,
        "SELECT i.basis FROM obtain_items i"
        " JOIN outputs o ON o.id = i.output_id") == [(2,)]
    delivered = _delivered_files(deployment.ready)
    assert len(delivered) == 1, delivered
    duration, codecs = _probe_media_facts(delivered[0])
    assert Decimal("2.5") <= duration < Decimal("4.5"), duration
    assert codecs == {"h264"}, codecs
    registered = _query(
        state_db,
        "SELECT i.size_bytes, i.sha256 FROM outputs o"
        " JOIN intermediate_files i ON i.id = o.intermediate_file_id"
        " WHERE o.kind = 2")
    assert len(registered) == 1
    size_bytes, sha256 = registered[0]
    content = delivered[0].read_bytes()
    assert len(content) == size_bytes
    assert hashlib.sha256(content).hexdigest() == sha256


@_MEDIA_TOOLS
def test_insufficient_timing_checks_repairs_and_delivers(tmp_path: Path) -> None:
    """计时证据不足的检查链：受限会话保守停止保存等待阶段，正常会
    话拷贝原片以真实 ffprobe 确认时长、超过门槛后无重编码修复，修
    复成品登记提升并由默认取回交付。"""
    deployment = Deployment(tmp_path, recording_margin_s="0.5")
    initialized = deployment.camctl(
        "init", "--config", str(deployment.config_path))
    assert initialized.exit_code == 0, initialized.stderr

    source = tmp_path / "source.mp4"
    _generate_source_video(source)
    source_duration, source_codecs = _probe_media_facts(source)
    assert source_duration == Decimal(_SOURCE_DURATION_S)
    assert source_codecs == {"h264"}

    content = source.read_bytes()
    spec = stub_driver_spec(
        {"1": [video_file("clip-1", size_bytes=len(content))]},
        record_duration_s=_TARGET_DURATION_S,
        device_files={"clip-1": {"path": str(source)}})
    _submit_record_and_obtain(deployment, spec)

    # 第一个会话启动录像后中断：录像执行中、锚点随会话失效。
    _start_recording_and_interrupt(deployment, spec)

    # 时钟异常会话保守收场：放弃计时停止、列举原片、固定计时证据
    # 不足的检查决定并归属源文件，会话以 clock_invalid 结束。
    deployment.set_min_plausible_date("2030-01-01")
    restricted = deployment.camctl(
        "run", "--config", str(deployment.config_path), driver=spec)
    assert restricted.exit_code == 1, restricted.stderr
    assert restricted.message()["body"]["reason"] == "clock_invalid"
    state_db = deployment.state_db
    assert _query(
        state_db, "SELECT status FROM actions ORDER BY id") == [(2,), (1,)]
    assert _query(
        state_db,
        "SELECT check_decision, source_device_file_id IS NOT NULL"
        " FROM recording_processing") == [(3, 1)]
    assert _query(state_db, "SELECT check_state FROM recording_processing") \
        == [(1,)]

    # 正常会话接管：拷贝检查、真实时长超过门槛、修复并交付修复成品。
    deployment.set_min_plausible_date("2025-01-01")
    resumed = deployment.camctl(
        "run", "--config", str(deployment.config_path), driver=spec)
    assert resumed.exit_code == 0, resumed.stderr

    assert _query(state_db, "SELECT status FROM plans") == [(3,)]
    assert _query(state_db, "SELECT status FROM actions ORDER BY id") \
        == [(3,), (3,)]
    check_state, repair_state, media_json, repair_basis = _query(
        state_db,
        "SELECT check_state, repair_state, media_json, repair_basis_json"
        " FROM recording_processing")[0]
    assert check_state == 3
    assert repair_state == 5
    media = json.loads(media_json)
    assert media["duration"]["seconds"] == float(_SOURCE_DURATION_S)
    basis = json.loads(repair_basis)
    assert basis["reason"] == 2
    # 门槛秒数是实际比较门槛：目标时长（3 秒）加修复余量（0.5 秒）。
    assert Decimal(str(basis["threshold_s"])) == Decimal("3.5")
    _assert_repaired_delivery(deployment)

    report = _ready_report(deployment)
    statuses = {
        action["name"]: action["status"]
        for plan in report["plans"] for action in plan["actions"]}
    assert statuses == {"拍录": "succeeded", "取回": "succeeded"}


@_MEDIA_TOOLS
def test_excess_recording_repairs_without_probing(tmp_path: Path) -> None:
    """可信计时多录的对账链：跨会话对账停止确认录制严格超过门槛后
    固定多录决定，不取回探测时长直接修复，修复成品由默认取回交付。"""
    deployment = Deployment(tmp_path, recording_margin_s="0.5")
    initialized = deployment.camctl(
        "init", "--config", str(deployment.config_path))
    assert initialized.exit_code == 0, initialized.stderr

    source = tmp_path / "source.mp4"
    _generate_source_video(source)
    content = source.read_bytes()
    spec = stub_driver_spec(
        {"1": [video_file("clip-1", size_bytes=len(content))]},
        record_duration_s=_TARGET_DURATION_S,
        device_files={"clip-1": {"path": str(source)}})
    _submit_record_and_obtain(deployment, spec)

    # 第一个会话启动录像后中断；等待墙钟越过目标加余量再由正常会
    # 话对账停止，停止确认时的已录时长严格超过修复门槛。
    started_at_us = _start_recording_and_interrupt(deployment, spec)
    started_moment = datetime.fromtimestamp(
        started_at_us / 1_000_000, tz=timezone.utc)
    _wait_wall_past(started_moment + timedelta(
        seconds=_TARGET_DURATION_S + 2.5))

    reconciled = deployment.camctl(
        "run", "--config", str(deployment.config_path), driver=spec)
    assert reconciled.exit_code == 0, reconciled.stderr

    state_db = deployment.state_db
    assert _query(state_db, "SELECT status FROM plans") == [(3,)]
    assert _query(state_db, "SELECT status FROM actions ORDER BY id") \
        == [(3,), (3,)]
    # 多录判定不取回视频探测时长：检查未执行，决定依据是控制计时。
    check_decision, check_state, check_basis, repair_state, repair_basis \
        = _query(
            state_db,
            "SELECT check_decision, check_state, check_basis_json,"
            " repair_state, repair_basis_json FROM recording_processing")[0]
    assert check_decision == 2
    assert check_state == 1
    assert json.loads(check_basis)["reason"] == 3
    assert repair_state == 5
    basis = json.loads(repair_basis)
    assert basis["reason"] == 2
    assert Decimal(str(basis["actual_duration_s"])) > Decimal("3.5")
    # 门槛秒数与判定使用的实际比较门槛一致：目标加余量，不只余量。
    assert Decimal(str(basis["threshold_s"])) == Decimal("3.5")
    _assert_repaired_delivery(deployment)

    report = _ready_report(deployment)
    statuses = {
        action["name"]: action["status"]
        for plan in report["plans"] for action in plan["actions"]}
    assert statuses == {"拍录": "succeeded", "取回": "succeeded"}


@pytest.mark.skipif(
    os.name != "posix", reason="进程组归属以真实 POSIX 进程验证")
def test_managed_tool_children_stay_in_process_group() -> None:
    """受管工具经统一启动边界启动：后代留在调用方的进程组，不建立
    新会话也不脱离；组终止可以覆盖工具进程（R-10 软件层事实）。"""
    from camctl.operations.process import ToolSpec, execute_tool

    class _NoStop:
        async def requested(self) -> None:
            await asyncio.Event().wait()

    async def scenario():
        return await execute_tool(
            ToolSpec(
                ("sh", "-c", "cut -d' ' -f5 /proc/self/stat"),
                timeout_s=Decimal("5"), terminate_grace_s=Decimal("1")),
            stop=_NoStop())

    outcome = asyncio.run(scenario())
    assert outcome.error is None
    assert outcome.exit is not None and outcome.exit.exit_code == 0
    tool_pgid = int(outcome.output.decode("utf-8").strip())
    assert tool_pgid == os.getpgrp()

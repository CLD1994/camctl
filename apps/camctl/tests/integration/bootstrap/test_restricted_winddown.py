"""时钟异常会话保守收场的组件集成测试。

受限 run 会话（墙钟检查失败）经生产装配工厂驱动录像保守收场：对
既往会话确认启动、尚未停止的录像以本会话单调钟额外等待
min(目标时长, recovery_wait_cap_s) 后按原停止预算停止，保存等待阶
段（计时证据不足检查与源文件关联），不启动媒体链、不登记正式产
物，动作保持执行中，会话按时钟异常退出；后续正常会话用原停止结
果与源文件继续后处理。重复受限会话不重复停止；取消已生效的录像
不经计时立即停止收场；停止失败按重试间隔在预算内重试，耗尽保持
执行中等待既有失败规则。
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.acceptance.service import CommandMode
from camctl.bootstrap.capture_assembly import session_capture_assembly
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.bootstrap.flows import cancel_flow, capture_flow, winddown_flow
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.devices.drivers.registry import DriverRegistry
from camctl.host_files.media import ProbeRequest, RepairRequest
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

from ..capture.test_capture_contract import ResultsDouble, _entry
from ..capture.test_recording_media_link import _CONTENT
from .test_media_assembly import (
    _PROBE_BODY,
    _REPAIR_BODY,
    _SessionDriver,
    _registry,
    _tool,
)
from .test_recording_stop import (
    _RecordCatalog,
    _await_query,
    _cancel,
    _future_schedule,
    _record_plan,
    _scalar,
    _submit_plan,
)

pytestmark = pytest.mark.asyncio

#: 检查工具按目标时长回报的探测时长（与 _RecordCatalog 的 6 秒一致）。
_PROBE_BODY_SIX = 'print(\'{"format": {"duration": "6"}}\')\n'


class _FakeWinddownClock:
    """受控单调钟：保守等待即时推进读数并记录请求的秒数。"""

    def __init__(self) -> None:
        self.ns = time.monotonic_ns()
        self.waits: list[float] = []

    async def wait(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.ns += int(seconds * 1_000_000_000)


def _config(home: Path, *, min_plausible: str = "2025-01-01"):
    return load_config(
        {
            "paths": {
                "state_db": str(home / "state.db"),
                "log_file": str(home / "camctl.log"),
                "staging": str(home / "staging"),
                "ready": str(home / "ready"),
                "processing": str(home / "processing"),
            },
            "clock": {"min_plausible_date": min_plausible},
            "devices": {
                "cam-1": {
                    "kind": "camera",
                    "driver": "camctl-adb",
                }
            },
        },
        ConfigDefaults(),
    )


def _factory(cfg, registry: DriverRegistry, results, tools_dir: Path | None,
             monotonic_ns=None, *, media_enabled: bool = True):
    return session_capture_assembly(
        devices=cfg.devices,
        drivers=registry,
        results=results,
        staging=Path(cfg.paths.staging),
        wait_config=lambda params: None,
        monotonic_ns=monotonic_ns,
        probe_request=(
            ProbeRequest(ffprobe=_tool(tools_dir, "ffprobe", _PROBE_BODY_SIX))
            if tools_dir is not None else None),
        repair_request=(
            RepairRequest(ffmpeg=_tool(tools_dir, "ffmpeg", _REPAIR_BODY))
            if tools_dir is not None else None),
        media_enabled=media_enabled,
    )


async def _recording_in_flight(tmp_path: Path, home_name: str, *, driver=None):
    """既往正常会话已确认启动、尚未停止的执行中录像。

    第一个会话用冻结的单调钟启动录像后终止，锚点随会话失效；目
    标时长 6 秒长于测试用恢复上限 2 秒，使保守窗口取上限。
    """
    home = tmp_path / home_name
    home.mkdir()
    cfg = _config(home)
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    assert initialize_state(
        cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
    await _submit_plan(
        tmp_path, cfg, _RecordCatalog(),
        _record_plan("1", _future_schedule(2)))
    db = Path(cfg.paths.state_db)
    driver = driver if driver is not None else _SessionDriver(_CONTENT)
    clock = {"ns": time.monotonic_ns()}
    results = ResultsDouble({})
    deps = build_runtime(CommandMode.RUN, cfg, catalog=_RecordCatalog())
    task = asyncio.create_task(execute_command(
        deps, None,
        flows={
            "scheduling": capture_flow(
                _factory(cfg, _registry(driver), results, None,
                         monotonic_ns=lambda: clock["ns"],
                         media_enabled=False)),
            "cancel": cancel_flow(ready=Path(cfg.paths.ready),
                                  processing=Path(cfg.paths.processing)),
        },
        poll_interval_s=0.1,
    ))
    try:
        await _await_query(
            db,
            "SELECT started_at IS NOT NULL FROM device_activities"
            " WHERE id = 1", (1,))
    finally:
        await _cancel(task)
        close_runtime(deps)
    return cfg, db, driver, results


async def _restricted_session(
    tmp_path: Path, home_name: str, driver, results, fake: _FakeWinddownClock,
    *, wait_cap_s: Decimal = Decimal("2"),
):
    """以墙钟不可信配置执行一次受限 run 会话并返回机器结果。"""
    home = tmp_path / home_name
    cfg = _config(home, min_plausible="2030-01-01")
    deps = build_runtime(CommandMode.RUN, cfg, catalog=_RecordCatalog())
    factory = _factory(
        cfg, _registry(driver), results, None,
        monotonic_ns=lambda: fake.ns, media_enabled=False)
    try:
        return await execute_command(
            deps, None,
            flows={},
            restricted_flows={
                "cancel": cancel_flow(
                    ready=Path(cfg.paths.ready),
                    processing=Path(cfg.paths.processing),
                    unscheduled_only=True),
                "winddown": winddown_flow(
                    capture_factory=factory, wait_cap_s=wait_cap_s,
                    sleep=fake.wait),
            },
            poll_interval_s=0.1,
        )
    finally:
        close_runtime(deps)


def _mark_canceled(db: Path) -> None:
    """把执行中录像标记为取消已生效（取消链已保存的联动事实）。"""
    owned = open_existing(db, DbOpenMode.EXISTING_RW, DbConfig())
    try:
        owned.connection.execute(
            "UPDATE actions SET cancel_requested = 1 WHERE id = 1")
        owned.connection.commit()
    finally:
        owned.connection.close()


class TestRestrictedWinddownSavesProgress:
    async def test_winddown_waits_cap_stops_and_saves_progress(
            self, tmp_path: Path) -> None:
        cfg, db, driver, results = await _recording_in_flight(
            tmp_path, "winddown")
        repair_before = _scalar(
            db, "SELECT repair_state FROM recording_processing"
            " WHERE action_id = 1")[0]
        results.files_by_action[1] = (_entry("clip-1", size=len(_CONTENT)),)
        fake = _FakeWinddownClock()
        outcome = await _restricted_session(
            tmp_path, "winddown", driver, results, fake)
        # 受限会话完成规定收场后按时钟异常退出，不取得普通接纳。
        assert outcome.succeeded is False
        assert outcome.reason == "clock_invalid"
        # 窗口取 min(目标 6 秒, 上限 2 秒)：只等待上限指示的 2 秒。
        assert fake.waits == [2.0]
        assert ("stop", "stop_recording") in driver.calls
        # 动作保持执行中表达“已停止，等待正常会话处理”。
        assert _scalar(db, "SELECT status FROM actions WHERE id = 1") == (2,)
        processing = _scalar(
            db,
            "SELECT check_decision,"
            " json_extract(check_basis_json, '$.reason'),"
            " json_extract(check_basis_json, '$.target_duration_ms'),"
            " repair_state, source_device_file_id"
            " FROM recording_processing WHERE action_id = 1")
        assert processing == (3, 2, 6000, repair_before, 1)
        # 原片登记归属与写完事实，但不成为正式产物。
        assert _scalar(
            db,
            "SELECT completion_state, role, source_action_id"
            " FROM device_files WHERE id = 1") == (3, 2, 1)
        assert _scalar(
            db, "SELECT COUNT(*) FROM outputs WHERE source_action_id = 1"
            ) == (0,)
        # 受限会话不启动视频拷贝：暂存目录没有录像输入副本。
        assert not list(
            (Path(cfg.paths.staging) / "recording-inputs").iterdir())


class TestNormalSessionResumesProgress:
    async def test_normal_session_continues_post_processing(
            self, tmp_path: Path) -> None:
        cfg, db, driver, results = await _recording_in_flight(
            tmp_path, "resume")
        results.files_by_action[1] = (_entry("clip-1", size=len(_CONTENT)),)
        fake = _FakeWinddownClock()
        outcome = await _restricted_session(
            tmp_path, "resume", driver, results, fake)
        assert outcome.reason == "clock_invalid"
        # 后续正常会话：用原停止结果与源文件继续后处理，媒体链推
        # 进检查（时长与目标一致），登记原片为正式产物并成功终局。
        tools_dir = tmp_path / "resume-tools"
        tools_dir.mkdir()
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_RecordCatalog())
        task = asyncio.create_task(execute_command(
            deps, None,
            flows={
                "scheduling": capture_flow(
                    _factory(cfg, _registry(driver), results, tools_dir)),
                "cancel": cancel_flow(ready=Path(cfg.paths.ready),
                                      processing=Path(cfg.paths.processing)),
            },
            poll_interval_s=0.1,
        ))
        try:
            await _await_query(db, "SELECT status FROM actions WHERE id = 1",
                               (3,))
        finally:
            await _cancel(task)
            close_runtime(deps)
        assert [kind for kind, _ in driver.calls].count("read") >= 1
        assert _scalar(
            db, "SELECT COUNT(*) FROM outputs WHERE source_action_id = 1"
            " AND kind = 1") == (1,)


class TestRepeatedRestrictedSession:
    async def test_second_restricted_session_keeps_progress_without_restop(
            self, tmp_path: Path) -> None:
        cfg, db, driver, results = await _recording_in_flight(
            tmp_path, "repeat")
        results.files_by_action[1] = (_entry("clip-1", size=len(_CONTENT)),)
        fake = _FakeWinddownClock()
        await _restricted_session(tmp_path, "repeat", driver, results, fake)
        stop_calls = [call for call in driver.calls if call[0] == "stop"]
        again = _FakeWinddownClock()
        outcome = await _restricted_session(
            tmp_path, "repeat", driver, results, again)
        assert outcome.reason == "clock_invalid"
        # 停止已确认：不重复等待、不重复停止，保存的进度保持。
        assert again.waits == []
        assert [call for call in driver.calls if call[0] == "stop"] == stop_calls
        assert _scalar(db, "SELECT status FROM actions WHERE id = 1") == (2,)
        assert _scalar(
            db, "SELECT check_decision FROM recording_processing"
            " WHERE action_id = 1") == (3,)
        assert _scalar(db, "SELECT COUNT(*) FROM device_files") == (1,)


class TestCanceledRecordingStopsImmediately:
    async def test_canceled_recording_finishes_without_cap_wait(
            self, tmp_path: Path) -> None:
        cfg, db, driver, results = await _recording_in_flight(
            tmp_path, "canceled")
        _mark_canceled(db)
        fake = _FakeWinddownClock()
        outcome = await _restricted_session(
            tmp_path, "canceled", driver, results, fake)
        assert outcome.reason == "clock_invalid"
        # 取消已生效：不经计时立即停止，放弃内容，取消终态。
        assert fake.waits == []
        assert ("stop", "stop_recording") in driver.calls
        assert _scalar(db, "SELECT status FROM actions WHERE id = 1") == (6,)
        assert _scalar(
            db, "SELECT check_decision FROM recording_processing"
            " WHERE action_id = 1") == (1,)
        assert _scalar(
            db, "SELECT COUNT(*) FROM outputs WHERE source_action_id = 1"
            ) == (0,)


class TestStopRetryWithinBudget:
    async def test_failed_stops_retry_then_confirm_and_save_progress(
            self, tmp_path: Path) -> None:
        driver = _SessionDriver(_CONTENT, stop_failures=2)
        cfg, db, driver, results = await _recording_in_flight(
            tmp_path, "retry", driver=driver)
        results.files_by_action[1] = (_entry("clip-1", size=len(_CONTENT)),)
        fake = _FakeWinddownClock()
        outcome = await _restricted_session(
            tmp_path, "retry", driver, results, fake)
        assert outcome.reason == "clock_invalid"
        # 前两次停止失败按重试间隔重试，第三次确认后保存等待阶段。
        assert fake.waits == [2.0, 3.0, 3.0]
        stop_calls = [call for call in driver.calls if call[0] == "stop"]
        assert len(stop_calls) == 3
        assert _scalar(db, "SELECT status FROM actions WHERE id = 1") == (2,)
        assert _scalar(
            db, "SELECT check_decision FROM recording_processing"
            " WHERE action_id = 1") == (3,)
        assert _scalar(
            db, "SELECT attempts_used FROM operation_runs"
            " WHERE responsibility_key = 'stop/1'") == (3,)


class TestStopExhaustedKeepsRecording:
    async def test_exhausted_budget_keeps_recording_running(
            self, tmp_path: Path) -> None:
        driver = _SessionDriver(_CONTENT, stop_failures=10)
        cfg, db, driver, results = await _recording_in_flight(
            tmp_path, "exhausted", driver=driver)
        results.files_by_action[1] = (_entry("clip-1", size=len(_CONTENT)),)
        fake = _FakeWinddownClock()
        outcome = await _restricted_session(
            tmp_path, "exhausted", driver, results, fake)
        assert outcome.reason == "clock_invalid"
        # 停止预算耗尽：不再无界重试，动作保持执行中等待既有失败
        # 与残留收场规则，不建立等待阶段。
        assert fake.waits == [2.0, 3.0, 3.0]
        stop_calls = [call for call in driver.calls if call[0] == "stop"]
        assert len(stop_calls) == 3
        assert _scalar(db, "SELECT status FROM actions WHERE id = 1") == (2,)
        assert _scalar(
            db, "SELECT check_decision FROM recording_processing"
            " WHERE action_id = 1") == (1,)
        assert _scalar(
            db, "SELECT COUNT(*) FROM device_files") == (0,)

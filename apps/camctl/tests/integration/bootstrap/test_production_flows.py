"""生产装配收口的组件集成测试。

run 会话不注入流程时使用生产装配（报告、取消与拍摄推进），拍摄推
进的驱动端口从进程驱动登记点解析，结果列举经生产适配把驱动的
result 端口观察转成候选产物文件；受限会话（墙钟检查失败）的生产
装配消费取消、一次报告与录像保守收场。测试以受契约约束的替身驱
动经登记点接入，验证装配结构与业务链路的真实协作，不宣称真实设
备通过。
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from camctl.acceptance.service import CommandMode
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.devices.drivers.registry import DriverEntry, DriverStatus
from camctl.devices.drivers.runtime import (
    current_registry,
    register_drivers,
    reset_drivers,
)
from camctl.devices.evidence import (
    DeviceObservation,
    EvidenceContract,
    EvidenceRegistry,
)
from camctl.devices.ports import ControlRequest, DeviceCallResult, DriverDeclaration
from camctl.persistence.initialization import InitOutcome, initialize_state

from .test_recording_stop import (
    _RecordCatalog,
    _await_query,
    _cancel,
    _future_schedule,
    _record_plan,
    _scalar,
    _submit_plan,
)
from .test_timelapse_finish import _TimelapseCatalog, _timelapse_plan

#: 生产装配共用的驱动证据契约（启动、停止、结果列举与轮次收场）。
_PRODUCTION_EVIDENCE = EvidenceRegistry((
    EvidenceContract(type="operation_returned", version=1, operation="control",
                     fields=frozenset()),
    EvidenceContract(type="start_confirmed", version=1, operation="control",
                     fields=frozenset({"activity_id"}), identity_field="activity_id"),
    EvidenceContract(type="timelapse_sent", version=1, operation="control",
                     fields=frozenset({"activity_id"}), identity_field="activity_id"),
    EvidenceContract(type="stop_returned", version=1, operation="stop",
                     fields=frozenset()),
    EvidenceContract(type="stop_confirmed", version=1, operation="stop",
                     fields=frozenset({"activity_id"}), identity_field="activity_id"),
    EvidenceContract(type="result_files_listed", version=1, operation="result",
                     fields=frozenset({"activity_id", "entries"}),
                     identity_field="activity_id"),
    EvidenceContract(type="results_returned", version=1, operation="result",
                     fields=frozenset()),
))

_CONTROL_OBSERVATIONS = {
    "take_photo": "photo_taken",
    "start_recording": "start_confirmed",
    "start_timelapse": "timelapse_sent",
}


class _ProductionDriver:
    """经登记项接入的契约替身：启动确认、停止确认与 result 端口列举。

    单动作场景（动作身份 1）：control/stop 观察身份与操作目标一致，
    list_results 按请求的活动身份返回编排的列举条目。
    """

    def __init__(self, files_by_action: dict[int, list[dict[str, Any]]]) -> None:
        self._files = files_by_action
        self.calls: list[tuple[str, str]] = []

    async def control(self, request: ControlRequest) -> DeviceCallResult:
        self.calls.append(("control", request.operation))
        return DeviceCallResult(
            observations=(DeviceObservation(
                type=_CONTROL_OBSERVATIONS[request.operation], version=1,
                data={"activity_id": "1"}),),
            error=None)

    async def stop(self, request: ControlRequest) -> DeviceCallResult:
        self.calls.append(("stop", request.operation))
        return DeviceCallResult(
            observations=(DeviceObservation(
                type="stop_confirmed", version=1,
                data={"activity_id": "1"}),),
            error=None)

    async def list_results(
        self, request: ControlRequest, batch: int,
    ) -> DeviceCallResult:
        identity = request.params["activity_id"]
        self.calls.append(("result", identity))
        entries = self._files.get(int(identity), [])
        return DeviceCallResult(
            observations=(DeviceObservation(
                type="result_files_listed", version=1,
                data={"activity_id": identity, "entries": entries}),),
            error=None)


def _entry(identity: str, *, kind: str = "video", size: int = 4096) -> dict:
    return {
        "identity": identity,
        "locator": {"path": f"/DCIM/{identity}"},
        "size_bytes": size,
        "complete": True,
        "kind": kind,
        "original_name": f"{identity}.mp4",
        "media_type": "video/mp4",
    }


def _entry_for_driver(driver_id: str, driver: _ProductionDriver) -> DriverEntry:
    return DriverEntry(
        driver_id=driver_id,
        driver=driver,
        declaration=DriverDeclaration(
            control_supported=True,
            stop_supported=True,
            query_supported=False,
            result_supported=True,
            read_supported=False,
            digest_supported=False,
            delete_supported=False,
        ),
        evidence=_PRODUCTION_EVIDENCE,
        status=DriverStatus.SOFTWARE_CONTRACT_VERIFIED,
    )


def _config(home: Path, *, min_plausible: str = "2025-01-01",
            wait_cap_s: str = "60") -> Any:
    return load_config(
        {
            "paths": {
                "state_db": str(home / "state.db"),
                "log_file": str(home / "camctl.log"),
                "staging": str(home / "staging"),
                "ready": str(home / "ready"),
                "processing": str(home / "processing"),
            },
            "clock": {
                "min_plausible_date": min_plausible,
                "recovery_wait_cap_s": wait_cap_s,
            },
            "devices": {
                "cam-1": {
                    "kind": "camera",
                    "driver": "production-double",
                    "result_check": {"retry_interval_s": "0"},
                    "recording": {"stop_retry_interval_s": "0"},
                }
            },
        },
        ConfigDefaults(),
    )


@pytest.fixture(autouse=True)
def _isolated_registry():
    reset_drivers()
    yield
    reset_drivers()


pytestmark = pytest.mark.asyncio


class TestDriverRuntimeRegistry:
    async def test_registered_entry_is_resolved_and_duplicates_rejected(
            self) -> None:
        entry = _entry_for_driver("production-double", _ProductionDriver({}))
        assert current_registry().entry("production-double") is None
        register_drivers(entry)
        assert current_registry().entry("production-double") is entry
        other = _entry_for_driver("production-double", _ProductionDriver({}))
        with pytest.raises(ValueError, match="重复登记"):
            register_drivers(other)
        reset_drivers()
        assert current_registry().entry("production-double") is None


class TestRunSessionProductionScheduling:
    async def test_timelapse_reaches_success_through_default_flows(
            self, tmp_path: Path) -> None:
        """延时任务经生产默认装配推进到成功终态。

        拍摄推进、结果列举适配与等待配置全部来自生产装配：驱动端
        口经登记项解析，列举观察经 result_files_listed 契约解释为产
        物文件，目标时长与余量读首次固定的执行定义。
        """
        home = tmp_path / "timelapse"
        home.mkdir()
        cfg = _config(home)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        driver = _ProductionDriver({1: [_entry("seq-1")]})
        register_drivers(_entry_for_driver("production-double", driver))
        await _submit_plan(
            tmp_path, cfg, _TimelapseCatalog(),
            _timelapse_plan("1", _future_schedule(1)))
        db = Path(cfg.paths.state_db)
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_TimelapseCatalog())
        task = asyncio.create_task(execute_command(deps, None))
        try:
            # 发送经生产装配确认；结果列举来自驱动 result 端口。
            await _await_query(
                db, "SELECT status FROM actions WHERE id = 1", (3,),
                timeout_s=25)
            assert ("control", "start_timelapse") in driver.calls
            assert ("result", "1") in driver.calls
            # 完整集合满足：按时间与产物完成判定成功终态并登记产物。
            assert _scalar(
                db, "SELECT COUNT(*) FROM outputs WHERE source_action_id = 1"
                ) == (1,)
            assert _scalar(
                db, "SELECT occupancy_state FROM device_activities"
                " WHERE id = 1") == (2,)
            assert _scalar(
                db, "SELECT status FROM plans WHERE id = 1") == (3,)
            # 正常会话完成全部责任后自行退出。
            outcome = await asyncio.wait_for(task, timeout=25)
            assert outcome.succeeded is True, outcome.details
        finally:
            if not task.done():
                await _cancel(task)
        close_runtime(deps)


class TestRestrictedSessionProductionWinddown:
    async def test_clock_invalid_session_stops_recording_via_default_flows(
            self, tmp_path: Path) -> None:
        """受限会话经生产装配保守收场已启动的录像。

        第一个正常会话用生产装配确认启动后中断；墙钟不可信的后续
        会话消费生产受限装配（取消、一次报告与保守收场）：按上限
        等待后停止，保存等待阶段，动作保持执行中，不登记正式产物。
        """
        home = tmp_path / "winddown"
        home.mkdir()
        cfg = _config(home, wait_cap_s="1")
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        driver = _ProductionDriver({})
        register_drivers(_entry_for_driver("production-double", driver))
        await _submit_plan(
            tmp_path, cfg, _RecordCatalog(),
            _record_plan("1", _future_schedule(1)))
        db = Path(cfg.paths.state_db)
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_RecordCatalog())
        task = asyncio.create_task(execute_command(deps, None))
        try:
            await _await_query(
                db,
                "SELECT started_at IS NOT NULL FROM device_activities"
                " WHERE id = 1", (1,))
        finally:
            await _cancel(task)
        close_runtime(deps)

        # 墙钟不可信的受限会话：生产受限装配消费保守收场。录像原片
        # 已在设备上（驱动列举可见），等待阶段据此登记归属与完成。
        driver._files[1] = [_entry("clip-1", size=4096)]
        restricted = _config(home, min_plausible="2030-01-01", wait_cap_s="1")
        restricted_deps = build_runtime(
            CommandMode.RUN, restricted, catalog=_RecordCatalog())
        try:
            outcome = await asyncio.wait_for(
                asyncio.ensure_future(
                    execute_command(restricted_deps, None)), timeout=30)
        finally:
            close_runtime(restricted_deps)
        assert outcome.succeeded is False
        assert outcome.reason == "clock_invalid"
        # 保守窗口取上限 1 秒后按原预算停止，录像确认停止。
        assert ("stop", "stop_recording") in driver.calls
        assert ("result", "1") in driver.calls
        assert _scalar(
            db, "SELECT status, attempts_used FROM operation_runs"
            " WHERE responsibility_key = 'stop/1'") == (3, 1)
        # 等待阶段已保存：计时证据不足的检查决定 REQUIRED 并关联原片。
        assert _scalar(
            db, "SELECT check_decision, source_device_file_id"
            " FROM recording_processing WHERE action_id = 1") == (3, 1)
        # 动作保持执行中，交由后续正常会话继续后处理。
        assert _scalar(db, "SELECT status FROM actions WHERE id = 1") == (2,)
        assert _scalar(
            db, "SELECT COUNT(*) FROM outputs WHERE source_action_id = 1"
            ) == (0,)

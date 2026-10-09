"""媒体链会话级装配的组件集成测试。

生产装配工厂经驱动登记项解析设备端口并组装推进运行时：控制与停
止端口、读取会话与源端摘要都按登记项静态声明取得，媒体链随读取
声明构造（未声明读取的处理行保持等待），异常多录余量取自设备本
地配置；结果列举端口由部署注入。跨会话恢复的异常多录经真实会话
推进对账、停止、原片拷贝、摘要比较与修复登记；驱动未登记时本轮
不推进。
"""

from __future__ import annotations

from camctl.capture.result_inputs import RESULT_FILES_CONTRACT

import asyncio
import hashlib
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.acceptance.service import CommandMode
from camctl.bootstrap.capture_assembly import session_capture_assembly
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.bootstrap.flows import cancel_flow, capture_flow
from camctl.bootstrap.lifecycle import build_runtime, close_runtime, execute_command
from camctl.devices.drivers.registry import (
    DriverEntry,
    DriverRegistry,
    DriverStatus,
)
from camctl.devices.evidence import (
    DeviceObservation,
    EvidenceContract,
    EvidenceRegistry,
)
from camctl.devices.ports import DeviceCallResult, DriverDeclaration
from camctl.devices.read_session import ReadSession, SourceFile
from camctl.host_files.media import ProbeRequest, RepairRequest
from camctl.persistence.initialization import InitOutcome, initialize_state

from ..capture.test_capture_contract import ResultsDouble, _entry
from ..capture.test_recording_media_link import _CONTENT
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

#: 录像与取消链五契约加源端摘要契约的登记证据。
_EVIDENCE = EvidenceRegistry(
    (
        EvidenceContract(type="operation_returned", version=1, operation="control",
                         fields=frozenset()),
        EvidenceContract(type="start_confirmed", version=1, operation="control",
                         fields=frozenset({"activity_id"}), identity_field="activity_id"),
        EvidenceContract(type="stop_returned", version=1, operation="stop",
                         fields=frozenset()),
        EvidenceContract(type="stop_confirmed", version=1, operation="stop",
                         fields=frozenset({"activity_id"}), identity_field="activity_id"),
        EvidenceContract(type="results_returned", version=1, operation="result",
                         fields=frozenset()),
        RESULT_FILES_CONTRACT,
        EvidenceContract(type="file_digest", version=1, operation="digest",
                         fields=frozenset({"file_id", "sha256"}),
                         identity_field="file_id"),
    )
)

_PROBE_BODY = (
    'print(\'{"streams": [{"codec_type": "video"}],'
    ' "format": {"duration": "1"}}\')\n'
)
_REPAIR_BODY = (
    "import shutil, sys\n"
    "args = sys.argv[1:]\n"
    "source = args[args.index('-i') + 1]\n"
    "shutil.copyfile(source, args[-1])\n"
)


class _ShortRecordCatalog(_RecordCatalog):
    """目标时长一秒的受理目录替身：缩短跨会话对账等待。"""

    duration_s = Decimal("1")


class _MemoryStream:
    """内存字节流：与受管读取通道同形的同步读取、唤醒与关闭。"""

    def __init__(self, content: bytes) -> None:
        self._content = content
        self._position = 0

    def read(self, limit: int) -> bytes:
        chunk = self._content[self._position:self._position + limit]
        self._position += len(chunk)
        return chunk

    def cancel(self) -> None:
        return None

    def close(self) -> None:
        return None


class _SessionDriver:
    """登记项驱动替身：启动/停止确认、连续读取与源端摘要。

    stop_failures 指定停止调用先失败的次数，用于停止预算内的
    重试场景。
    """

    def __init__(self, content: bytes, *, stop_failures: int = 0) -> None:
        self._content = content
        self._sha256 = hashlib.sha256(content).hexdigest()
        self._stop_failures = stop_failures
        self.calls: list[tuple[str, str]] = []

    async def control(self, request) -> DeviceCallResult:
        self.calls.append(("control", request.operation))
        return DeviceCallResult(
            observations=(
                DeviceObservation(
                    type="start_confirmed", version=1,
                    data={"activity_id": "1"}),
            ),
            error=None,
        )

    async def stop(self, request) -> DeviceCallResult:
        self.calls.append(("stop", request.operation))
        if self._stop_failures > 0:
            self._stop_failures -= 1
            return DeviceCallResult(
                observations=(), error={"code": "device_error"})
        return DeviceCallResult(
            observations=(
                DeviceObservation(
                    type="stop_confirmed", version=1,
                    data={"activity_id": "1"}),
            ),
            error=None,
        )

    async def open_read(self, source: SourceFile, offset: int, ticket, *, idle_timeout_s):
        self.calls.append(("read", source.file_id))
        return ReadSession(
            source, offset, _MemoryStream(self._content[offset:]),
            idle_timeout_s)

    async def digest(self, request) -> DeviceCallResult:
        file_id = request.params["file_id"]
        self.calls.append(("digest", file_id))
        return DeviceCallResult(
            observations=(
                DeviceObservation(
                    type="file_digest", version=1,
                    data={"file_id": file_id, "sha256": self._sha256}),
            ),
            error=None,
        )


def _tool(directory: Path, name: str, body: str) -> str:
    """写入可执行的检查/修复工具替身脚本并返回调用路径。"""
    helper = directory / f"{name}.py"
    helper.write_text(body, encoding="utf-8")
    if sys.platform == "win32":
        launcher = directory / f"{name}.cmd"
        launcher.write_text(
            f'@{sys.executable} "{helper}" %*\n', encoding="utf-8")
        return str(launcher)
    launcher = directory / f"{name}.sh"
    launcher.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{helper}" "$@"\n', encoding="utf-8")
    launcher.chmod(0o755)
    return str(launcher)


def _config(home: Path, *, repair_margin_s: str = "2"):
    return load_config(
        {
            "paths": {
                "state_db": str(home / "state.db"),
                "log_file": str(home / "camctl.log"),
                "staging": str(home / "staging"),
                "ready": str(home / "ready"),
                "processing": str(home / "processing"),
            },
            "devices": {
                "cam-1": {
                    "kind": "camera",
                    "driver": "camctl-adb",
                    "recording": {"repair_margin_s": repair_margin_s},
                }
            },
        },
        ConfigDefaults(),
    )


def _registry(driver, *, read_supported: bool = True) -> DriverRegistry:
    return DriverRegistry((
        DriverEntry(
            driver_id="camctl-adb",
            driver=driver,
            declaration=DriverDeclaration(
                control_supported=True,
                stop_supported=True,
                query_supported=False,
                result_supported=False,
                read_supported=read_supported,
                digest_supported=read_supported,
                delete_supported=False,
            ),
            evidence=_EVIDENCE,
            status=DriverStatus.SOFTWARE_CONTRACT_VERIFIED,
        ),
    ))


def _factory(cfg, registry, results, tools_dir: Path, clock):
    return session_capture_assembly(
        devices=cfg.devices,
        drivers=registry,
        results=results,
        staging=Path(cfg.paths.staging),
        wait_config=lambda params: None,
        monotonic_ns=lambda: clock["ns"],
        probe_request=ProbeRequest(
            ffprobe=_tool(tools_dir, "ffprobe", _PROBE_BODY)),
        repair_request=RepairRequest(
            ffmpeg=_tool(tools_dir, "ffmpeg", _REPAIR_BODY)),
    )


def _run_session(deps, cfg, factory):
    return asyncio.create_task(execute_command(
        deps, None,
        flows={
            "scheduling": capture_flow(factory),
            "cancel": cancel_flow(ready=Path(cfg.paths.ready),
                                  processing=Path(cfg.paths.processing)),
        },
        poll_interval_s=0.1,
    ))


async def _wait_wall_past(db: Path, started_at_us: int, span_s: float) -> None:
    """等待真实墙钟越过已保存启动墙钟加指定秒数。"""
    deadline = started_at_us + int(span_s * 1_000_000)
    while time.time() * 1_000_000 < deadline:
        await asyncio.sleep(0.05)


class TestExcessRepairThroughSessionAssembly:
    async def test_cross_session_excess_repairs_and_finishes(
            self, tmp_path: Path) -> None:
        home = tmp_path / "excess"
        home.mkdir()
        cfg = _config(home)
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        await _submit_plan(
            tmp_path, cfg, _ShortRecordCatalog(),
            _record_plan("1", _future_schedule(2)))
        db = Path(cfg.paths.state_db)
        driver = _SessionDriver(_CONTENT)
        clock = {"ns": time.monotonic_ns()}
        results = ResultsDouble({})
        # 第一个会话：到期启动并确认，单调钟不推进所以不到停止时
        # 机；锚点随会话失效。
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_ShortRecordCatalog())
        task = _run_session(deps, cfg, _factory(
            cfg, _registry(driver), results, tools_dir, clock))
        try:
            await _await_query(
                db,
                "SELECT started_at IS NOT NULL FROM device_activities"
                " WHERE id = 1", (1,))
            assert driver.calls == [("control", "start_recording")]
        finally:
            await _cancel(task)
            close_runtime(deps)
        # 墙钟越过启动加目标加余量（1 + 2 秒）：第二个会话对账满
        # 足后停止，控制耗时超过门槛触发异常多录修复。
        started_at = _scalar(
            db, "SELECT started_at FROM device_activities WHERE id = 1")[0]
        await _wait_wall_past(db, int(started_at), 3.2)
        results.files_by_action[1] = (_entry("clip-1", size=len(_CONTENT)),)
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_ShortRecordCatalog())
        task = _run_session(deps, cfg, _factory(
            cfg, _registry(driver), results, tools_dir, clock))
        try:
            await _await_query(db, "SELECT status FROM actions WHERE id = 1", (3,))
        finally:
            await _cancel(task)
            close_runtime(deps)
        operations = [kind for kind, _ in driver.calls]
        assert ("stop", "stop_recording") in driver.calls
        assert "read" in operations
        assert "digest" in operations
        # 门槛秒数保存实际比较门槛：目标时长（1 秒）加设备配置的
        # 修复余量（2 秒）。
        processing = _scalar(
            db,
            "SELECT check_decision,"
            " json_extract(check_basis_json, '$.reason'),"
            " repair_state,"
            " json_extract(repair_basis_json, '$.reason'),"
            " json_extract(repair_basis_json, '$.threshold_s')"
            " FROM recording_processing WHERE action_id = 1")
        assert processing == (2, 3, 5, 2, 3)
        # 源端摘要能力按登记声明固定为已支持并经驱动端口比较。
        assert _scalar(
            db, "SELECT checksum_support FROM device_files"
            " WHERE id = 1") == (2,)
        kinds = _scalar(
            db, "SELECT COUNT(*) FROM outputs"
            " WHERE source_action_id = 1 AND kind IN (1, 2)")
        assert kinds == (2,)


class TestUndeclaredReadKeepsWaiting:
    async def test_no_media_port_when_read_not_declared(
            self, tmp_path: Path) -> None:
        home = tmp_path / "waiting"
        home.mkdir()
        cfg = _config(home)
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        await _submit_plan(
            tmp_path, cfg, _ShortRecordCatalog(),
            _record_plan("1", _future_schedule(2)))
        db = Path(cfg.paths.state_db)
        driver = _SessionDriver(_CONTENT)
        clock = {"ns": time.monotonic_ns()}
        results = ResultsDouble({})
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_ShortRecordCatalog())
        task = _run_session(deps, cfg, _factory(
            cfg, _registry(driver, read_supported=False),
            results, tools_dir, clock))
        try:
            await _await_query(
                db,
                "SELECT started_at IS NOT NULL FROM device_activities"
                " WHERE id = 1", (1,))
        finally:
            await _cancel(task)
            close_runtime(deps)
        started_at = _scalar(
            db, "SELECT started_at FROM device_activities WHERE id = 1")[0]
        await _wait_wall_past(db, int(started_at), 3.2)
        results.files_by_action[1] = (_entry("clip-1", size=len(_CONTENT)),)
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_ShortRecordCatalog())
        task = _run_session(deps, cfg, _factory(
            cfg, _registry(driver, read_supported=False),
            results, tools_dir, clock))
        try:
            # 停止与异常多录决定照常建立；读取未声明所以媒体端口
            # 未装配，修复责任保持待执行，动作不终局。
            await _await_query(
                db,
                "SELECT repair_state,"
                " json_extract(repair_basis_json, '$.threshold_s')"
                " FROM recording_processing WHERE action_id = 1", (3, 3))
        finally:
            await _cancel(task)
            close_runtime(deps)
        assert _scalar(db, "SELECT status FROM actions WHERE id = 1") == (2,)
        operations = [kind for kind, _ in driver.calls]
        assert "read" not in operations
        assert "digest" not in operations
        assert _scalar(
            db, "SELECT COUNT(*) FROM outputs WHERE source_action_id = 1"
            ) == (0,)


class TestUnregisteredDriverKeepsAction:
    async def test_missing_registry_entry_does_not_dispatch(
            self, tmp_path: Path) -> None:
        home = tmp_path / "unregistered"
        home.mkdir()
        cfg = _config(home)
        tools_dir = tmp_path / "tools"
        tools_dir.mkdir()
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        await _submit_plan(
            tmp_path, cfg, _ShortRecordCatalog(),
            _record_plan("1", _future_schedule(2)))
        db = Path(cfg.paths.state_db)
        driver = _SessionDriver(_CONTENT)
        clock = {"ns": time.monotonic_ns()}
        results = ResultsDouble({})
        # 登记为空：动作开始事务照常推进，但没有运行时可派发。
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_ShortRecordCatalog())
        task = _run_session(deps, cfg, _factory(
            cfg, DriverRegistry(), results, tools_dir, clock))
        try:
            await _await_query(
                db, "SELECT status FROM actions WHERE id = 1", (2,))
            await asyncio.sleep(0.5)
        finally:
            await _cancel(task)
            close_runtime(deps)
        assert driver.calls == []
        # 开始事务已登记活动，但没有运行时派发：活动保持未启动。
        assert _scalar(
            db, "SELECT started_at IS NULL FROM device_activities"
            " WHERE action_id = 1") == (1,)


class TestRetryIntervalInjection:
    """设备级重试间隔随装配进入运行时预算与媒体链。"""

    @staticmethod
    def _open(cfg):
        from camctl.persistence.runtime import DbConfig, DbOpenMode, open_existing

        return open_existing(
            Path(cfg.paths.state_db), DbOpenMode.EXISTING_RW, DbConfig())

    async def test_device_intervals_reach_runtime_configs(
            self, tmp_path: Path) -> None:
        home = tmp_path / "intervals"
        home.mkdir()
        cfg = load_config(
            {
                "paths": {
                    "state_db": str(home / "state.db"),
                    "log_file": str(home / "camctl.log"),
                    "staging": str(home / "staging"),
                    "ready": str(home / "ready"),
                    "processing": str(home / "processing"),
                },
                "devices": {
                    "cam-1": {
                        "kind": "camera",
                        "driver": "camctl-adb",
                        "recording": {
                            "start_retry_interval_s": "4",
                            "stop_retry_interval_s": "0.5",
                        },
                        "result_check": {"retry_interval_s": "2"},
                        "copy": {"retry_interval_s": "1"},
                    }
                },
            },
            ConfigDefaults(),
        )
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        factory = session_capture_assembly(
            devices=cfg.devices,
            drivers=_registry(_SessionDriver(_CONTENT)),
            results=ResultsDouble({}),
            staging=Path(cfg.paths.staging),
            wait_config=lambda params: None,
            monotonic_ns=time.monotonic_ns,
        )
        owned = self._open(cfg)
        try:
            runtime = factory(owned, "cam-1")
            assert runtime is not None
            assert runtime.stop_config.retry_interval_s == Decimal("0.5")
            assert runtime.check_config.retry_interval_s == Decimal("2")
            assert runtime.media is not None
            assert runtime.media.retry_interval_s == Decimal("1")
        finally:
            owned.connection.close()

    async def test_spec_defaults_apply_without_device_overrides(
            self, tmp_path: Path) -> None:
        home = tmp_path / "defaults"
        home.mkdir()
        cfg = _config(home)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        factory = session_capture_assembly(
            devices=cfg.devices,
            drivers=_registry(_SessionDriver(_CONTENT)),
            results=ResultsDouble({}),
            staging=Path(cfg.paths.staging),
            wait_config=lambda params: None,
            monotonic_ns=time.monotonic_ns,
        )
        owned = self._open(cfg)
        try:
            runtime = factory(owned, "cam-1")
            assert runtime is not None
            # 未覆盖时采用规格默认：停止与核实间隔 3 秒。
            assert runtime.stop_config.retry_interval_s == Decimal("3")
            assert runtime.check_config.retry_interval_s == Decimal("3")
            assert runtime.media is not None
            assert runtime.media.retry_interval_s == Decimal("3")
        finally:
            owned.connection.close()

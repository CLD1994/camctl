"""取回动作执行链接入 run 会话的组件集成测试。

真实 run 会话按轮推进拍摄与取回流程：照片完成后跨计划取回执行
来源解析、选择固定、读取资格建档，读取在设备空闲轮次经真实机会
事务与尝试预算推进到发布；显式实例不存在按来源解析失败保存动
作终态；读取持续失败耗尽预算后交付终局失败；设备被到时录像占用
时读取让路，录像终态后继续推进。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import sqlite3
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest

from camctl.acceptance.service import CommandMode
from camctl.bootstrap.capture_assembly import session_capture_assembly
from camctl.bootstrap.config import ConfigDefaults, load_config
from camctl.bootstrap.flows import cancel_flow, capture_flow
from camctl.bootstrap.lifecycle import (
    build_runtime, close_runtime, execute_command)
from camctl.bootstrap.obtain_assembly import (
    obtain_flow, session_obtain_assembly)
from camctl.capture.handlers import ObservedFile
from camctl.capture.results import FileKind
from camctl.devices.drivers.registry import (
    DriverEntry, DriverRegistry, DriverStatus)
from camctl.devices.evidence import (
    DeviceObservation, EvidenceContract, EvidenceRegistry)
from camctl.devices.ports import DeviceCallResult, DriverDeclaration
from camctl.devices.read_session import ReadSession, SourceFile
from camctl.host_files.media import ProbeRequest, RepairRequest
from camctl.persistence.initialization import InitOutcome, initialize_state
from camctl.capture.timelapse import CaptureWaitConfig

from ..capture.test_capture_contract import ResultsDouble
from .test_run_dispatch import _parsed

pytestmark = pytest.mark.asyncio

_PHOTO_CONTENT = b"photo-bytes-for-delivery"
_RECORD_CONTENT = b"record-bytes-for-delivery"

_PROBE_BODY = (
    'print(\'{"streams": [{"codec_type": "video"}],'
    ' "format": {"duration": "1"}}\')\n'
)
_REPAIR_BODY = (
    "import shutil, sys\n"
    "shutil.copyfile(sys.argv[-2], sys.argv[-1])\n"
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
        f'#!/bin/sh\nexec "{sys.executable}" "{helper}" "$@"\n',
        encoding="utf-8")
    launcher.chmod(0o755)
    return str(launcher)

_CAMERA_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {"type": {"const": "single_shot"}},
    "required": ["type"],
    "additionalProperties": False,
}
_VIDEO_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {"type": {"const": "video"}},
    "required": ["type"],
    "additionalProperties": False,
}

_EVIDENCE = EvidenceRegistry(
    (
        EvidenceContract(type="operation_returned", version=1,
                         operation="control", fields=frozenset()),
        EvidenceContract(type="photo_taken", version=1, operation="control",
                         fields=frozenset({"activity_id"}),
                         identity_field="activity_id"),
        EvidenceContract(type="start_confirmed", version=1,
                         operation="control",
                         fields=frozenset({"activity_id"}),
                         identity_field="activity_id"),
        EvidenceContract(type="stop_returned", version=1, operation="stop",
                         fields=frozenset()),
        EvidenceContract(type="stop_confirmed", version=1, operation="stop",
                         fields=frozenset({"activity_id"}),
                         identity_field="activity_id"),
        EvidenceContract(type="read_returned", version=1, operation="read",
                         fields=frozenset()),
        EvidenceContract(type="results_returned", version=1,
                         operation="result", fields=frozenset()),
        EvidenceContract(type="file_digest", version=1, operation="digest",
                         fields=frozenset({"file_id", "sha256"}),
                         identity_field="file_id"),
    )
)


class _Catalog:
    """受理目录替身：支持 cam-1 的照片、录像与取回动作。"""

    def action_types(self):
        return frozenset(
            {"camera_take_photo", "camera_record", "obtain_action_outputs"})

    def device_exists(self, device_id):
        return device_id == "cam-1"

    def driver_id(self, device_id):
        return "camctl-adb" if device_id == "cam-1" else None

    def device_supports(self, device_id, action_type):
        return self.device_exists(device_id) and action_type in (
            "camera_take_photo", "camera_record")

    #: 录像目标时长一秒：缩短跨会话对账等待。
    duration_s = Decimal("1")

    def parameter_definition(self, device_id, action_type, parameter_type):
        from camctl.acceptance.ports import ParameterDefinition
        from camctl.devices.tasks import CaptureTask

        if not self.device_supports(device_id, action_type):
            return None
        if action_type == "camera_take_photo":
            if parameter_type != "single_shot":
                return None
            return ParameterDefinition(schema=_CAMERA_SCHEMA, defaults={})

        if parameter_type != "video":
            return None

        def task(params):
            return CaptureTask(
                "camera_record",
                target_duration_s=self.duration_s,
                stop_supported=True,
            )

        return ParameterDefinition(
            schema=_VIDEO_SCHEMA, defaults={}, task_factory=task)


class _MemoryStream:
    """内存字节流：与受管读取通道同形的同步读取与收场。"""

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


class _ObtainDriver:
    """契约替身：拍摄控制、停止确认、设备读取与源端摘要。

    read_failures 指定读取调用先失败的次数；正常读取按源内容连
    续交付。photo_target 与 record_target 是观察身份的活动身份：
    派发顺序固定先照片后录像，活动身份由数据库按此顺序分配。
    """

    def __init__(self, *, read_failures: int = 0, photo_target: str = "1",
                 record_target: str = "2") -> None:
        self.read_calls: list[str] = []
        self.control_calls: list[str] = []
        self._read_failures = read_failures
        self._photo_target = photo_target
        self._record_target = record_target

    async def control(self, request) -> DeviceCallResult:
        self.control_calls.append(request.operation)
        observation = {
            "take_photo": ("photo_taken", self._photo_target),
            "start_recording": ("start_confirmed", self._record_target),
        }[request.operation]
        return DeviceCallResult(
            observations=(
                DeviceObservation(
                    type=observation[0], version=1,
                    data={"activity_id": observation[1]}),
            ),
            error=None,
        )

    async def stop(self, request) -> DeviceCallResult:
        self.control_calls.append(request.operation)
        return DeviceCallResult(
            observations=(
                DeviceObservation(
                    type="stop_confirmed", version=1,
                    data={"activity_id": self._record_target}),
            ),
            error=None,
        )

    async def open_read(self, source: SourceFile, offset: int, ticket):
        self.read_calls.append(source.file_id)
        if self._read_failures > 0:
            self._read_failures -= 1
            raise RuntimeError("设备读取通道失败")
        return ReadSession(
            source, offset,
            _MemoryStream(_content_of(source, offset)), Decimal("10"))

    async def digest(self, request) -> DeviceCallResult:
        file_id = request.params["file_id"]
        identity = json.loads(request.params["identity_key"])[-1]
        content = _CONTENT_BY_IDENTITY[identity]
        return DeviceCallResult(
            observations=(
                DeviceObservation(
                    type="file_digest", version=1,
                    data={"file_id": file_id,
                          "sha256": hashlib.sha256(content).hexdigest()}),
            ),
            error=None,
        )


_CONTENT_BY_IDENTITY: dict[str, bytes] = {}


def _content_of(source: SourceFile, offset: int) -> bytes:
    """按设备文件身份键取源内容；身份键是设备、驱动与文件身份。"""
    identity = json.loads(source.file_id)[-1]
    return _CONTENT_BY_IDENTITY[identity][offset:]


def _tools(tmp_path: Path) -> Path:
    """写入检查/修复工具替身并返回目录；录像媒体链按需调用。"""
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir(exist_ok=True)
    return tools_dir


def _photo_entry(identity: str, name: str) -> ObservedFile:
    return ObservedFile(
        identity=identity,
        locator={"path": f"/DCIM/{identity}"},
        evidence={"listing": identity},
        complete=True,
        size_bytes=len(_PHOTO_CONTENT),
        kind=FileKind.PHOTO,
        original_name=name,
        media_type="image/jpeg",
    )


def _record_entry(identity: str, name: str, content: bytes) -> ObservedFile:
    return ObservedFile(
        identity=identity,
        locator={"path": f"/DCIM/{identity}"},
        evidence={"listing": identity},
        complete=True,
        size_bytes=len(content),
        kind=FileKind.VIDEO,
        original_name=name,
        media_type="video/mp4",
    )


def _config(home: Path, *, retry_interval_s: str = "0") -> object:
    return load_config(
        {
            "paths": {
                "state_db": str(home / "state.db"),
                "log_file": str(home / "camctl.log"),
                "staging": str(home / "staging"),
                "ready": str(home / "ready"),
                "processing": str(home / "processing"),
            },
            "copy": {"segment_size": "4 KiB"},
            "devices": {
                "cam-1": {
                    "kind": "camera",
                    "driver": "camctl-adb",
                    "recording": {"repair_margin_s": "1"},
                    "copy": {"retry_interval_s": retry_interval_s},
                }
            },
        },
        ConfigDefaults(),
    )


def _registry(driver: _ObtainDriver, *,
              capture_read_parallel: bool = False) -> DriverRegistry:
    return DriverRegistry((
        DriverEntry(
            driver_id="camctl-adb",
            driver=driver,
            declaration=DriverDeclaration(
                control_supported=True,
                stop_supported=True,
                query_supported=False,
                result_supported=False,
                read_supported=True,
                digest_supported=True,
                delete_supported=False,
                capture_read_parallel_supported=capture_read_parallel,
            ),
            evidence=_EVIDENCE,
            status=DriverStatus.SOFTWARE_CONTRACT_VERIFIED,
        ),
    ))


def _capture_factory(cfg, registry, results, clock, tools_dir):
    return session_capture_assembly(
        devices=cfg.devices,
        drivers=registry,
        results=results,
        staging=Path(cfg.paths.staging),
        wait_config=lambda params: CaptureWaitConfig(
            target_duration_ms=1_000, driver_margin_ms=0),
        monotonic_ns=lambda: clock["ns"],
        probe_request=ProbeRequest(
            ffprobe=_tool(tools_dir, "ffprobe", _PROBE_BODY)),
        repair_request=RepairRequest(
            ffmpeg=_tool(tools_dir, "ffmpeg", _REPAIR_BODY)),
    )


def _obtain_factory(cfg, drivers):
    return session_obtain_assembly(
        devices=cfg.devices,
        drivers=drivers,
        staging=Path(cfg.paths.staging),
        ready=Path(cfg.paths.ready),
        processing=Path(cfg.paths.processing),
        segment_size=cfg.copy.segment_size_bytes,
    )


def _run_session(deps, cfg, driver, results, clock, tools_dir, *,
                 capture_read_parallel: bool = False):
    return asyncio.create_task(execute_command(
        deps, None,
        flows={
            "scheduling": capture_flow(
                _capture_factory(
                    cfg, _registry(
                        driver, capture_read_parallel=capture_read_parallel),
                    results, clock, tools_dir)),
            "obtain": obtain_flow(_obtain_factory(
                cfg, _registry(
                    driver, capture_read_parallel=capture_read_parallel))),
            "cancel": cancel_flow(ready=Path(cfg.paths.ready),
                                  processing=Path(cfg.paths.processing)),
        },
        poll_interval_s=0.1,
    ))


def _past_schedule(seconds: int = 1) -> str:
    moment = time.strftime(
        "%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - seconds))
    return moment


def _future_schedule(seconds: int) -> str:
    moment = time.strftime(
        "%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + seconds))
    return moment


def _photo_plan(request_id: str, *, scheduled_at: str | None = None) -> dict:
    return {
        "request_id": request_id,
        "created_at": "2026-01-15 08:00:00",
        "name": f"photo-plan-{request_id}",
        "actions": [
            {
                "name": "shoot",
                "type": "camera_take_photo",
                "device_id": "cam-1",
                "scheduled_at": scheduled_at or _past_schedule(),
                "params": {"type": "single_shot"},
                "policy": {"max_delay_ms": 5000},
            }
        ],
    }


def _obtain_plan(request_id: str, source: dict) -> dict:
    return {
        "request_id": request_id,
        "created_at": "2026-01-15 08:00:00",
        "name": f"obtain-plan-{request_id}",
        "actions": [
            {
                "name": "grab",
                "type": "obtain_action_outputs",
                "scheduled_at": _past_schedule(),
                "params": {"source": source, "purpose": "manual"},
            }
        ],
    }


def _record_plan(request_id: str, scheduled_at: str) -> dict:
    return {
        "request_id": request_id,
        "created_at": "2026-01-15 08:00:00",
        "name": f"record-plan-{request_id}",
        "actions": [
            {
                "name": "record",
                "type": "camera_record",
                "device_id": "cam-1",
                "scheduled_at": scheduled_at,
                "params": {"type": "video"},
                "policy": {"max_delay_ms": 5000},
            }
        ],
    }


async def _submit(tmp_path: Path, cfg, body: dict) -> None:
    deps = build_runtime(CommandMode.SUBMIT, cfg, catalog=_Catalog())
    try:
        outcome = await execute_command(
            deps, await _parsed(tmp_path, body))
        assert outcome.succeeded is True, outcome.details
    finally:
        close_runtime(deps)


def _scalar(db_path: Path, sql: str, *params):
    with sqlite3.connect(db_path) as connection:
        return connection.execute(sql, params).fetchone()


async def _await_query(db_path: Path, sql: str, expected, timeout_s: float = 15.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        row = _scalar(db_path, sql)
        if row is not None and row == expected:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"等待查询结果超时: {sql} 期望 {expected}，"
        f"当前 {_scalar(db_path, sql)}")


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


class TestObtainExecutionLink:
    async def test_obtain_delivery_reaches_ready(self, tmp_path: Path) -> None:
        """照片完成后跨计划取回发布交付：动作成功、文件进入 ready。"""
        cfg = _config(tmp_path)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        await _submit(tmp_path, cfg, _photo_plan("1"))
        photo_row = _scalar(
            Path(cfg.paths.state_db),
            "SELECT id FROM actions WHERE name='shoot'")
        await _submit(tmp_path, cfg, _obtain_plan(
            "2", {"action_instance_id": str(photo_row[0])}))
        _CONTENT_BY_IDENTITY.clear()
        _CONTENT_BY_IDENTITY["shot-1"] = _PHOTO_CONTENT
        driver = _ObtainDriver()
        results = ResultsDouble({1: (_photo_entry("shot-1", "IMG_0001.jpg"),)})
        clock = {"ns": time.monotonic_ns()}
        tools_dir = _tools(tmp_path)
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_Catalog())
        task = _run_session(deps, cfg, driver, results, clock, tools_dir)
        try:
            db = Path(cfg.paths.state_db)
            await _await_query(
                db, "SELECT status FROM actions WHERE name='grab'", (3,))
            delivery = _scalar(
                db, "SELECT id, status, display_name FROM deliveries")
            assert delivery[1] == 5, delivery
            assert delivery[2] == "IMG_0001-photo-plan-1-shoot.jpg", delivery
            ready_file = Path(cfg.paths.ready) / f"{delivery[0]}.jpg"
            assert ready_file.read_bytes() == _PHOTO_CONTENT
            assert [json.loads(call)[-1] for call in driver.read_calls] == ["shot-1"]
        finally:
            await _cancel(task)
        close_runtime(deps)

    async def test_obtain_unknown_instance_fails_action(
            self, tmp_path: Path) -> None:
        """显式实例不存在：来源解析失败保存动作终态，不创建交付。"""
        cfg = _config(tmp_path)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        await _submit(tmp_path, cfg, _photo_plan("1"))
        await _submit(tmp_path, cfg, _obtain_plan(
            "2", {"action_instance_id": "999"}))
        driver = _ObtainDriver()
        _CONTENT_BY_IDENTITY["shot-1"] = _PHOTO_CONTENT
        results = ResultsDouble({1: (_photo_entry("shot-1", "IMG_0001.jpg"),)})
        clock = {"ns": time.monotonic_ns()}
        tools_dir = _tools(tmp_path)
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_Catalog())
        task = _run_session(deps, cfg, driver, results, clock, tools_dir)
        try:
            db = Path(cfg.paths.state_db)
            await _await_query(
                db, "SELECT status FROM actions WHERE name='grab'", (4,))
            error = _scalar(
                db,
                "SELECT json_extract(error_details_json, '$.reason')"
                " FROM actions WHERE name='grab'")
            assert error[0] == "action_not_found", error
            assert _scalar(db, "SELECT COUNT(*) FROM deliveries") == (0,)
        finally:
            await _cancel(task)
        close_runtime(deps)

    async def test_obtain_read_exhaustion_fails_delivery(
            self, tmp_path: Path) -> None:
        """读取持续失败耗尽预算：交付终局失败，动作按逐项失败收场。"""
        cfg = _config(tmp_path)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        await _submit(tmp_path, cfg, _photo_plan("1"))
        photo_row = _scalar(
            Path(cfg.paths.state_db),
            "SELECT id FROM actions WHERE name='shoot'")
        await _submit(tmp_path, cfg, _obtain_plan(
            "2", {"action_instance_id": str(photo_row[0])}))
        _CONTENT_BY_IDENTITY["shot-1"] = _PHOTO_CONTENT
        driver = _ObtainDriver(read_failures=99)
        results = ResultsDouble({1: (_photo_entry("shot-1", "IMG_0001.jpg"),)})
        clock = {"ns": time.monotonic_ns()}
        tools_dir = _tools(tmp_path)
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_Catalog())
        task = _run_session(deps, cfg, driver, results, clock, tools_dir)
        try:
            db = Path(cfg.paths.state_db)
            await _await_query(
                db, "SELECT status FROM actions WHERE name='grab'", (4,))
            delivery = _scalar(
                db, "SELECT status, json_extract(error_json, '$.code'),"
                    " json_extract(error_json, '$.details.attempts_used')"
                    " FROM deliveries")
            assert delivery == (6, "read_attempts_exhausted", 3), delivery
            # 机会已交还：拷贝行不再持有设备读取归属。
            slot = _scalar(
                db, "SELECT slot_device_id FROM file_copies")
            assert slot == (None,), slot
        finally:
            await _cancel(task)
        close_runtime(deps)

    async def test_obtain_read_yields_to_recording(self, tmp_path: Path) -> None:
        """到时录像占用设备时读取让路；录像终态后读取继续推进。"""
        cfg = _config(tmp_path)
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        await _submit(tmp_path, cfg, _photo_plan("1"))
        photo_row = _scalar(
            Path(cfg.paths.state_db),
            "SELECT id FROM actions WHERE name='shoot'")
        await _submit(tmp_path, cfg, _obtain_plan(
            "2", {"action_instance_id": str(photo_row[0])}))
        tools_dir = _tools(tmp_path)
        await _submit(
            tmp_path, cfg, _record_plan("3", _past_schedule()))
        _CONTENT_BY_IDENTITY.clear()
        _CONTENT_BY_IDENTITY["shot-1"] = _PHOTO_CONTENT
        _CONTENT_BY_IDENTITY["clip-1"] = _RECORD_CONTENT
        driver = _ObtainDriver()
        results = ResultsDouble(
            {1: (_photo_entry("shot-1", "IMG_0001.jpg"),)})
        clock = {"ns": time.monotonic_ns()}
        db = Path(cfg.paths.state_db)
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_Catalog())
        task = _run_session(deps, cfg, driver, results, clock, tools_dir)
        try:
            # 录像启动进入执行中：读取建档完成但设备被拍摄占用。
            await _await_query(
                db,
                "SELECT started_at IS NOT NULL FROM device_activities"
                " WHERE action_id ="
                " (SELECT id FROM actions WHERE name='record')", (1,))
            await _await_query(
                db, "SELECT status FROM deliveries", (1,))
            await asyncio.sleep(0.4)
            # 让路：录像执行期间没有打开过读取会话。
            assert driver.read_calls == []
            started_at = _scalar(
                db, "SELECT started_at FROM device_activities WHERE id=1")[0]
        finally:
            await _cancel(task)
            close_runtime(deps)
        # 墙钟越过启动加目标加余量后，第二会话对账停止并完成录像；
        # 设备空闲后取回读取继续推进到发布。
        deadline = started_at + int(3.2 * 1_000_000)
        while time.time() * 1_000_000 < deadline:
            await asyncio.sleep(0.05)
        results.files_by_action[3] = (
            _record_entry("clip-1", "VID_0001.mp4", _RECORD_CONTENT),)
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_Catalog())
        clock = {"ns": time.monotonic_ns()}
        task = _run_session(deps, cfg, driver, results, clock, tools_dir)
        try:
            await _await_query(
                db, "SELECT status FROM actions WHERE name='record'", (3,))
            await _await_query(
                db, "SELECT status FROM actions WHERE name='grab'", (3,))
            delivery = _scalar(
                db, "SELECT id, status FROM deliveries")
            assert delivery[1] == 5, delivery
            ready_file = Path(cfg.paths.ready) / f"{delivery[0]}.jpg"
            assert ready_file.read_bytes() == _PHOTO_CONTENT
            # 录像原片的媒体链拷贝与取回读取共用读取端口；让路后的
            # 取回读取按录像终态后的推进顺序最后发生。
            reads = [json.loads(call)[-1] for call in driver.read_calls]
            assert reads[-1] == "shot-1" and "clip-1" in reads, reads
        finally:
            await _cancel(task)
        close_runtime(deps)

    async def test_obtain_read_parallel_with_recording(self,
                                                       tmp_path: Path) -> None:
        """驱动声明拍摄与读取并行时，执行中的录像不阻塞取回读取。

        照片先完成，取回读取首次失败进入重试等待；等待期间录像到
        时启动并持续执行，重试在录像仍执行中时并行授予并推进到发
        布，动作成功终态。一次一份拷贝互斥保持（本用例只有取回一
        份拷贝）。
        """
        cfg = _config(tmp_path, retry_interval_s="1.5")
        assert initialize_state(
            cfg, Path(cfg.paths.state_db)).outcome is InitOutcome.CREATED
        await _submit(tmp_path, cfg, _photo_plan("1"))
        photo_row = _scalar(
            Path(cfg.paths.state_db),
            "SELECT id FROM actions WHERE name='shoot'")
        await _submit(tmp_path, cfg, _obtain_plan(
            "2", {"action_instance_id": str(photo_row[0])}))
        tools_dir = _tools(tmp_path)
        # 录像在读取重试等待期间到时启动：重试落进执行窗口。
        await _submit(
            tmp_path, cfg, _record_plan("3", _future_schedule(1)))
        _CONTENT_BY_IDENTITY.clear()
        _CONTENT_BY_IDENTITY["shot-1"] = _PHOTO_CONTENT
        _CONTENT_BY_IDENTITY["clip-1"] = _RECORD_CONTENT
        driver = _ObtainDriver(read_failures=1)
        results = ResultsDouble(
            {1: (_photo_entry("shot-1", "IMG_0001.jpg"),)})
        clock = {"ns": time.monotonic_ns()}
        db = Path(cfg.paths.state_db)
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_Catalog())
        task = _run_session(
            deps, cfg, driver, results, clock, tools_dir,
            capture_read_parallel=True)
        try:
            # 照片完成后首次读取失败，重试等待期间录像到时启动。
            await _await_query(
                db,
                "SELECT started_at IS NOT NULL FROM device_activities"
                " WHERE action_id ="
                " (SELECT id FROM actions WHERE name='record')", (1,))
            assert len(driver.read_calls) == 1, driver.read_calls
            # 声明并行：重试不为执行中的录像让路，在录像终态前完成
            # 拷贝、校验与发布。
            await _await_query(
                db, "SELECT status FROM actions WHERE name='grab'", (3,))
            delivery = _scalar(db, "SELECT id, status FROM deliveries")
            assert delivery[1] == 5, delivery
            ready_file = Path(cfg.paths.ready) / f"{delivery[0]}.jpg"
            assert ready_file.read_bytes() == _PHOTO_CONTENT
            assert len(driver.read_calls) == 2, driver.read_calls
            # 录像仍执行中：读取完成不改变拍摄执行状态。
            assert _scalar(
                db, "SELECT status FROM actions WHERE name='record'") == (2,)
            started_at = _scalar(
                db, "SELECT started_at FROM device_activities WHERE id=2")[0]
        finally:
            await _cancel(task)
            close_runtime(deps)
        # 墙钟越过启动加目标加余量后，第二会话对账停止并完成录像。
        deadline = started_at + int(3.2 * 1_000_000)
        while time.time() * 1_000_000 < deadline:
            await asyncio.sleep(0.05)
        results.files_by_action[3] = (
            _record_entry("clip-1", "VID_0001.mp4", _RECORD_CONTENT),)
        deps = build_runtime(CommandMode.RUN, cfg, catalog=_Catalog())
        clock = {"ns": time.monotonic_ns()}
        task = _run_session(
            deps, cfg, driver, results, clock, tools_dir,
            capture_read_parallel=True)
        try:
            await _await_query(
                db, "SELECT status FROM actions WHERE name='record'", (3,))
        finally:
            await _cancel(task)
        close_runtime(deps)

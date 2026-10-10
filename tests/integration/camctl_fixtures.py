"""跨组件集成测试共用夹具。

真实 camctl CLI 以子进程运行；部署装配桥（_camctl_stub_entry）在
子进程启动阶段登记受契约约束的设备替身，替身的驱动定义与运行端
口同源登记，其余装配全部来自生产入口。客户端消费经 tsx 驱动调
用客户端服务的真实导入路径。设备替身的行为由剧本文件描述，同步
点用门文件表达，不使用随机 sleep。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_REPO = _ROOT.parent.parent

#: 无部署装配时的直接 CLI 入口（受理等不依赖驱动登记的命令）。
_DIRECT_ENTRY = "from camctl.cli import main; raise SystemExit(main())"

#: 部署装配桥：先登记替身驱动再进入生产 CLI。
_BRIDGE = _ROOT / "_camctl_stub_entry.py"

_CLIENT_DIR = _REPO / "apps" / "client"


def terminate_process_tree(process) -> None:
    """终止会话子进程及其全部后代进程。

    run 会话派生报告 worker 等孙进程；只终止直接子进程会让孤儿继
    续持有状态库文件（Windows 上表现为后续连接报磁盘 I/O 错误）。
    POSIX 上 CLI 以独立会话启动，按进程组终止整个会话。
    """
    if os.name == "posix":
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        process.wait(timeout=30)
        return
    subprocess.run(
        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
        capture_output=True)
    process.wait(timeout=30)


@dataclass(frozen=True)
class CliResult:
    """一次 CLI 子进程的结果：退出码与两路输出。"""

    exit_code: int
    stdout: str
    stderr: str

    def message(self) -> dict:
        """解析 stdout 的单行结果消息（成功与业务错误共用形态）。"""
        lines = [line for line in self.stdout.splitlines() if line]
        assert len(lines) == 1, f"stdout 应只有一行结果: {self.stdout!r}"
        return json.loads(lines[0])


class Deployment:
    """一个初始化前的部署目录：配置、计划文件与替身剧本。"""

    def __init__(self, root: Path, *, devices: bool = True,
                 recording_margin_s: str | None = None) -> None:
        self.root = root
        self.home = root / "deployment"
        self.staging = self.home / "staging"
        self.ready = self.home / "ready"
        self.processing = self.home / "processing"
        self.gates = root / "gates"
        for directory in (self.home, self.staging, self.ready,
                          self.processing, self.gates):
            directory.mkdir(parents=True)
        self.state_db = self.home / "state.db"
        self.config_path = self.home / "config.toml"
        self.config_path.write_text(
            "\n".join(
                [
                    "[paths]",
                    f'state_db = "{_toml_path(self.state_db)}"',
                    f'log_file = "{_toml_path(self.home / "camctl.log")}"',
                    f'staging = "{_toml_path(self.staging)}"',
                    f'ready = "{_toml_path(self.ready)}"',
                    f'processing = "{_toml_path(self.processing)}"',
                    "",
                    "[clock]",
                    'min_plausible_date = "2025-01-01"',
                    "",
                    *(
                        [
                            "[devices.cam-1]",
                            'kind = "camera"',
                            'driver = "test-stub"',
                            "",
                            *(
                                [
                                    "[devices.cam-1.recording]",
                                    f'repair_margin_s = "{recording_margin_s}"',
                                    "",
                                ]
                                if recording_margin_s is not None
                                else []
                            ),
                            "[devices.cam-1.result_check]",
                            'retry_interval_s = "0"',
                            "",
                            "[devices.cam-1.cleanup]",
                            'delete_retry_interval_s = "0"',
                            'query_retry_interval_s = "0"',
                            "",
                        ]
                        if devices
                        else []
                    ),
                ]
            ),
            encoding="utf-8",
        )
        self.driver_spec = root / "driver-spec.json"

    def set_min_plausible_date(self, date_text: str) -> None:
        """改写墙钟可信下界；未来日期使下次会话进入受限收场。"""
        text = self.config_path.read_text(encoding="utf-8")
        updated = re.sub(
            r'min_plausible_date = "[^"]*"',
            f'min_plausible_date = "{date_text}"',
            text, count=1)
        assert updated != text, "配置中未找到 min_plausible_date"
        self.config_path.write_text(updated, encoding="utf-8")

    def _write_driver_spec(self, driver: dict) -> None:
        spec = dict(driver)
        spec["gate_dir"] = str(self.gates)
        self.driver_spec.write_text(
            json.dumps(spec, ensure_ascii=False), encoding="utf-8")

    def camctl(self, *args: str, driver: dict | None = None,
               timeout_s: float = 180.0) -> CliResult:
        """运行真实 camctl CLI；driver 提供时经部署装配桥接入替身。"""
        environment = os.environ.copy()
        environment["CAMCTL_TEST_STATE_DB"] = str(self.state_db)
        if driver is not None:
            self._write_driver_spec(driver)
            environment["CAMCTL_TEST_DRIVER_SPEC"] = str(self.driver_spec)
            command = [sys.executable, str(_BRIDGE), *args]
        else:
            command = [sys.executable, "-c", _DIRECT_ENTRY, *args]
        completed = subprocess.run(
            command, capture_output=True, text=True, env=environment,
            timeout=timeout_s)
        return CliResult(completed.returncode, completed.stdout, completed.stderr)

    def start_camctl(self, *args: str, driver: dict | None = None):
        """启动不等待退出的 CLI 子进程（会话中断与并发场景）。

        POSIX 上以独立会话启动，测试按进程组终止整个会话树；工具
        与 worker 后代保持在会话组内，不脱离。
        """
        environment = os.environ.copy()
        environment["CAMCTL_TEST_STATE_DB"] = str(self.state_db)
        if driver is not None:
            self._write_driver_spec(driver)
            environment["CAMCTL_TEST_DRIVER_SPEC"] = str(self.driver_spec)
            command = [sys.executable, str(_BRIDGE), *args]
        else:
            command = [sys.executable, "-c", _DIRECT_ENTRY, *args]
        kwargs = {"start_new_session": True} if os.name == "posix" else {}
        return subprocess.Popen(
            command,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=environment, **kwargs)

    def write_plan(self, body: dict) -> Path:
        target = self.root / f"plan-{body['request_id']}.json"
        target.write_text(
            json.dumps(body, ensure_ascii=False), encoding="utf-8")
        return target

    def run_client_driver(self, *args: str, output: Path) -> dict:
        """运行客户端真实服务驱动，返回导出、导入或估算凭据。"""
        completed = subprocess.run(
            ["node", "--import", "tsx", str(_ROOT / "client_driver.ts"), *args,
             str(output)],
            capture_output=True, text=True, cwd=str(_CLIENT_DIR), timeout=120)
        assert completed.returncode == 0, (
            f"客户端驱动失败: {completed.stderr}\n{completed.stdout}")
        return json.loads(output.read_text(encoding="utf-8"))

    def import_reports_with_client(self, reports_dir: Path) -> dict:
        """用客户端服务的真实导入路径消费报告，返回保存凭据。

        驱动脚本扫描目录中的 status-report 文件，经客户端 Application
        校验、合并并保存，输出已保存报告与累计确认位置。
        """
        return self.run_client_driver(
            "import", str(reports_dir), str(self.root / "client-store"),
            output=self.root / "client-import.json")

    def install_client_capabilities(self, driver: dict) -> None:
        """把 camctl describe 的能力说明导入客户端存储。

        真实链路中客户端从部署侧取得设备能力说明；测试以 describe
        命令与替身驱动的登记定义同源产生同一说明。
        """
        described = self.camctl(
            "describe", "--config", str(self.config_path), driver=driver)
        assert described.exit_code == 0, described.stderr
        store = self.root / "client-store"
        store.mkdir(exist_ok=True)
        (store / "device-capabilities.json").write_text(
            described.stdout, encoding="utf-8")

    def export_plan_with_client(self, plan_body: dict) -> tuple[Path, dict]:
        """用客户端服务的真实导出路径产生计划文件，返回路径与凭据。

        计划正文经能力校验后由客户端分配随机整数请求身份；客户端
        已保存报告时下载正文自动携带 last_report_id（ACK）。
        """
        body_path = self.root / "plan-body.json"
        body_path.write_text(
            json.dumps(plan_body, ensure_ascii=False), encoding="utf-8")
        sequence = len(list(self.root.glob("client-plan-*"))) + 1
        plan_path = self.root / f"client-plan-{sequence}.json"
        receipt = self.run_client_driver(
            "export", str(self.root / "client-store"), str(body_path),
            str(plan_path), output=self.root / "client-export.json")
        return plan_path, receipt


def _toml_path(path: Path) -> str:
    """TOML 基本字符串路径：反斜杠转义后可移植。"""
    return str(path).replace("\\", "\\\\")


def future_schedule(seconds: int) -> str:
    """计划时间按 UTC 表达；部署时钟与受理解析都使用 UTC。"""
    moment = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def photo_plan(request_id: str, scheduled_at: str) -> dict:
    return {
        "request_id": request_id,
        "created_at": "2026-01-15 08:00:00",
        "name": f"plan-{request_id}",
        "actions": [
            {
                "name": "shoot",
                "type": "camera_take_photo",
                "device_id": "cam-1",
                "scheduled_at": scheduled_at,
                "params": {"type": "single_shot"},
                "policy": {"max_delay_ms": 5000},
            }
        ],
    }


def timelapse_plan(request_id: str, scheduled_at: str) -> dict:
    return {
        "request_id": request_id,
        "created_at": "2026-01-15 08:00:00",
        "name": f"plan-{request_id}",
        "actions": [
            {
                "name": "timelapse",
                "type": "camera_timelapse",
                "device_id": "cam-1",
                "scheduled_at": scheduled_at,
                "params": {"type": "timelapse"},
                "policy": {"max_delay_ms": 5000},
            }
        ],
    }


def stub_driver_spec(files: dict[str, list[dict]], *,
                     timelapse_duration_s: float = 3.0,
                     record_duration_s: float = 1.0,
                     gates: dict[str, str] | None = None,
                     device_files: dict[str, str] | None = None,
                     delete_error: str | None = None,
                     photo_preview_supported: bool = False) -> dict:
    """设备替身剧本：驱动能力、按活动身份的结果文件与同步门。

    files 的键是活动身份（单动作部署从 1 开始）；gates 把端口名映
    到剧本目录中的门文件名，该文件出现前替身不响应对应调用。
    device_files 按设备侧文件身份提供读取内容（取回链的真实字节
    与摘要来源）。delete_error 提供时删除调用持续返回该错误（效
    果未知）；删除成功时移除设备内容并按契约回填文件缺席观察，
    查询按设备内容实时报告存在性。photo_preview_supported 声明
    单张拍摄参数类型是否支持预览（驱动能力声明面）。
    """
    return {
        "driver_id": "test-stub",
        "timelapse_duration_s": timelapse_duration_s,
        "record_duration_s": record_duration_s,
        "files": files,
        "gates": gates or {},
        "device_files": device_files or {},
        "delete_error": delete_error,
        "photo_preview_supported": photo_preview_supported,
    }


def photo_file(identity: str) -> dict:
    """一张完整照片的结果列举条目（photo kind）。"""
    return {
        "identity": identity,
        "locator": {"path": f"/DCIM/{identity}"},
        "size_bytes": 4096,
        "complete": True,
        "kind": "photo",
        "original_name": f"{identity}.jpg",
        "media_type": "image/jpeg",
    }


def video_file(identity: str, *, size_bytes: int = 8192) -> dict:
    """一段完整视频的结果列举条目（video kind）。

    size_bytes 须与设备侧实际内容长度一致：取回拷贝按登记长度读取
    并与源端摘要比较。
    """
    return {
        "identity": identity,
        "locator": {"path": f"/DCIM/{identity}"},
        "size_bytes": size_bytes,
        "complete": True,
        "kind": "video",
        "original_name": f"{identity}.mp4",
        "media_type": "video/mp4",
    }


def preview_file(identity: str, paired_identity: str, *,
                 size_bytes: int = 2048) -> dict:
    """一张完整预览图片的结果列举条目。

    驱动以 paired_identity 声明与本批原片条目的配对关联（预览文
    件规格：驱动提供对应原文件的明确关联及写入完成依据）。
    """
    return {
        "identity": identity,
        "locator": {"path": f"/DCIM/{identity}"},
        "size_bytes": size_bytes,
        "complete": True,
        "kind": "photo",
        "original_name": f"{identity}.jpg",
        "media_type": "image/jpeg",
        "paired_identity": paired_identity,
    }


#: 替身驱动用到的观察契约（与 D2/D4 契约测试同形态）。
_STUB_EVIDENCE_CONTRACTS = (
    ("operation_returned", 1, "control", frozenset(), None),
    ("photo_taken", 1, "control", frozenset({"activity_id"}), "activity_id"),
    ("start_confirmed", 1, "control", frozenset({"activity_id"}), "activity_id"),
    ("timelapse_sent", 1, "control", frozenset({"activity_id"}), "activity_id"),
    ("stop_returned", 1, "stop", frozenset(), None),
    ("stop_confirmed", 1, "stop", frozenset({"activity_id"}), "activity_id"),
    ("results_returned", 1, "result", frozenset(), None),
    ("file_digest", 1, "digest",
     frozenset({"file_id", "sha256"}), "file_id"),
    ("read_returned", 1, "read", frozenset(), None),
    ("delete_returned", 1, "delete", frozenset(), None),
    ("file_absent", 1, "delete",
     frozenset({"cleanup_item_id"}), "cleanup_item_id"),
    ("file_presence", 1, "query",
     frozenset({"cleanup_item_id", "present"}), "cleanup_item_id"),
)

_CONTROL_OBSERVATIONS = {
    "take_photo": "photo_taken",
    "start_recording": "start_confirmed",
    "start_timelapse": "timelapse_sent",
}

_GATE_TIMEOUT_S = 120.0


class _ScriptedStubDriver:
    """受契约约束的设备替身：启动确认、停止与结果列举。

    行为来自剧本：结果列举按生产适配回询的动作主键返回条目（剧
    本 files 的键即动作主键字符串）；剧本 gates 映射的端口在对应
    门文件出现前不响应（显式同步点，非随机 sleep）。确认观察携带
    的活动身份与生产核对的操作目标（设备活动主键）对齐：控制调
    用先于响应提交授予事务，替身从部署状态库读当前占用持有者的
    活动行取得该主键，库不可用时回退剧本 activity_identity。
    """

    def __init__(self, spec: dict, gate_dir: Path) -> None:
        self._spec = spec
        self._gate_dir = gate_dir
        self.calls: list[tuple[str, str]] = []

    def _trace(self, port: str, detail: str) -> None:
        """跨组件诊断开关：把替身端口调用追加写入剧本目录日志。"""
        if not os.environ.get("CAMCTL_TEST_TRACE_CALLS"):
            return
        with open(self._gate_dir / "stub-calls.log", "a",
                  encoding="utf-8") as log:
            log.write(f"{port} {detail}\n")

    def _fail_trace(self, port: str, detail: str, error: BaseException) -> None:
        self._trace(port, f"{detail} FAILED {type(error).__name__}: {error}")

    def _await_gate(self, port: str) -> None:
        gate = self._spec.get("gates", {}).get(port)
        if not gate:
            return
        target = self._gate_dir / gate
        deadline = time.monotonic() + _GATE_TIMEOUT_S
        while not target.exists():
            assert time.monotonic() < deadline, f"门超时: {target}"
            time.sleep(0.02)

    async def control(self, request) -> object:
        from camctl.devices.evidence import DeviceObservation
        from camctl.devices.ports import DeviceCallResult

        self._await_gate("control")
        # 确认观察身份须与生产核对的操作目标（设备活动主键）一致。
        # 控制请求不携带该身份（第一版接口缝隙），替身从部署状态库
        # 读当前占用持有者的活动行对齐；查不到时回退剧本身份。
        identity = self._held_activity_identity(request) or str(
            self._spec.get("activity_identity", "1"))
        self.calls.append(("control", request.operation))
        return DeviceCallResult(
            observations=(DeviceObservation(
                type=_CONTROL_OBSERVATIONS[request.operation], version=1,
                data={"activity_id": identity}),),
            error=None)

    def _held_activity_identity(self, request) -> str | None:
        """读部署状态库中该设备已派发待响应的活动主键。

        生产授予事务先于控制调用提交：设备占用互斥保证同一设备至
        多一行处于已派发待响应（dispatch_state=2），即本次调用的
        操作目标。库不可读或无匹配行时返回 None，由调用方回退。
        """
        return self._activity_identity(request, "da.dispatch_state = 2")

    def _running_activity_identity(self, request) -> str | None:
        """读部署状态库中该设备执行中的活动主键（停止调用目标）。

        停止请求不携带任务身份（第一版接口缝隙）：生产按活动的执行
        中事实发起停止对账，替身查库读该设备占用中且进行中的活动行
        对齐。库不可读或无匹配行时返回 None，由调用方回退。
        """
        return self._activity_identity(
            request, "da.occupancy_state = 1 AND da.activity_state = 2")

    def _activity_identity(self, request, condition: str) -> str | None:
        state_db = os.environ.get("CAMCTL_TEST_STATE_DB")
        if not state_db:
            return None
        uri = (f"file:{Path(state_db).as_posix()}?mode=ro")
        try:
            with closing(sqlite3.connect(uri, uri=True, timeout=5)) as probe:
                row = probe.execute(
                    "SELECT da.id FROM device_activities da"
                    " JOIN actions a ON a.id = da.action_id"
                    f" WHERE a.device_id = ? AND {condition}"
                    " ORDER BY da.id DESC LIMIT 1",
                    (request.binding.device_id,)).fetchone()
        except sqlite3.Error:
            return None
        return None if row is None else str(int(row[0]))

    async def stop(self, request) -> object:
        from camctl.devices.evidence import DeviceObservation
        from camctl.devices.ports import DeviceCallResult

        self._await_gate("stop")
        # 停止确认观察身份须与生产核对的操作目标（执行中活动主键）
        # 一致：查库对齐；查不到时回退请求参数或剧本身份。
        identity = self._running_activity_identity(request) or str(
            request.params.get("activity_id",
                               self._spec.get("activity_identity", "1")))
        self.calls.append(("stop", request.operation))
        return DeviceCallResult(
            observations=(DeviceObservation(
                type="stop_confirmed", version=1,
                data={"activity_id": identity}),),
            error=None)

    async def list_results(self, request, batch: int) -> object:
        from camctl.devices.evidence import DeviceObservation
        from camctl.devices.ports import DeviceCallResult
        from camctl.operations.models import (
            CallOutcome, EffectState, EvidenceValue, Settlement, SettlementBasis,
        )

        self._await_gate("result")
        identity = str(request.params["activity_id"])
        self.calls.append(("result", identity))
        entries = self._spec.get("files", {}).get(identity, [])
        return DeviceCallResult.from_outcome(CallOutcome(
            effect=EffectState.CONFIRMED,
            settlement=Settlement(
                SettlementBasis.OBSERVED, EvidenceValue("results_returned", 1, {})),
            observations=(DeviceObservation(
                type="result_files_listed", version=2,
                data={"activity_id": identity, "entries": entries,
                      "cursor": request.params.get("cursor"), "next_cursor": None,
                      "set_finalized": self._spec.get("set_finalized", True),
                      "completion_evidence": None}),),
        ))

    def _device_content(self, identity: str) -> bytes:
        """按设备侧文件身份取剧本内容；取回链的真实字节来源。

        值为文本时按 UTF-8 编码；真实二进制媒体以 {"path": …} 提供
        文件位置，读取实际字节（剧本经 JSON 传递，字节不能内联）。
        """
        entry = self._spec.get("device_files", {}).get(identity)
        assert entry is not None, f"剧本缺少设备文件内容: {identity!r}"
        if isinstance(entry, dict):
            return Path(entry["path"]).read_bytes()
        return (entry.encode("utf-8") if isinstance(entry, str)
                else entry)

    async def open_read(self, source, offset: int, ticket, *, idle_timeout_s):
        from decimal import Decimal

        from camctl.devices.read_session import ReadSession

        identity = json.loads(source.file_id)[-1]
        self.calls.append(("read", identity))
        try:
            content = self._device_content(identity)[offset:]
            session = ReadSession(
                source, offset, _MemoryStream(content), idle_timeout_s)
        except BaseException as error:
            self._fail_trace("read", identity, error)
            raise
        self._trace("read", identity)
        return session

    async def digest(self, request) -> object:
        from camctl.devices.evidence import DeviceObservation
        from camctl.devices.ports import DeviceCallResult

        identity = json.loads(request.params["identity_key"])[-1]
        self.calls.append(("digest", identity))
        return DeviceCallResult(
            observations=(DeviceObservation(
                type="file_digest", version=1,
                data={"file_id": request.params["file_id"],
                      "sha256": hashlib.sha256(
                          self._device_content(identity)).hexdigest()}),),
            error=None)

    async def delete(self, request) -> object:
        from camctl.devices.evidence import DeviceObservation
        from camctl.devices.ports import DeviceCallResult

        item_id = request.params["cleanup_item_id"]
        self.calls.append(("delete", item_id))
        error = self._spec.get("delete_error")
        if error is not None:
            # 删除调用失败且无观察：删除效果未知，由查询核实。
            return DeviceCallResult(observations=(), error=error)
        identity = json.loads(request.params["identity_key"])[-1]
        self._spec["device_files"].pop(identity, None)
        return DeviceCallResult(
            observations=(
                DeviceObservation(
                    type="delete_returned", version=1, data={}),
                DeviceObservation(
                    type="file_absent", version=1,
                    data={"cleanup_item_id": item_id}),
            ),
            error=None)

    async def query_state(self, request) -> object:
        from camctl.devices.evidence import DeviceObservation
        from camctl.devices.ports import DeviceCallResult

        item_id = request.params["cleanup_item_id"]
        self.calls.append(("query", item_id))
        identity = json.loads(request.params["identity_key"])[-1]
        present = identity in self._spec["device_files"]
        return DeviceCallResult(
            observations=(DeviceObservation(
                type="file_presence", version=1,
                data={"cleanup_item_id": item_id,
                      "present": present}),),
            error=None)


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


def _stub_definition(spec: dict):
    """替身的驱动定义：照片单张与设备自结束的延时任务。"""
    from decimal import Decimal

    from camctl.devices.catalog import ActionCapability, DriverDefinition
    from camctl.contracts.json_values import parse_exact_json
    from camctl.devices.tasks import (
        CaptureTask,
        CompletionMode,
        EndControl,
        StartReturn,
    )

    def photo_task(params):
        return CaptureTask("camera_take_photo")

    def record_task(params):
        return CaptureTask(
            "camera_record",
            target_duration_s=params.get(
                "duration_s", Decimal(str(spec.get("record_duration_s", 1.0)))),
            stop_supported=True,
        )

    def timelapse_task(params):
        return CaptureTask(
            "camera_timelapse",
            target_duration_s=params.get(
                "capture_duration_s", Decimal(str(spec.get("timelapse_duration_s", 3.0)))),
            duration_based=True,
            wait_after_send=True,
            end_control=EndControl.DEVICE,
            start_return_meaning=StartReturn.SENT,
            completion_mode=CompletionMode.TIME_AND_OUTPUTS,
            stop_supported=False,
            result_wait_margin_s=Decimal("0"),
        )

    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"type": {"const": "…"}},
        "required": ["type"],
        "additionalProperties": False,
    }
    photo_schema = json.loads(json.dumps(schema))
    photo_schema["properties"]["type"]["const"] = "single_shot"
    record_schema = json.loads(json.dumps(schema))
    record_schema["properties"]["type"]["const"] = "video"
    timelapse_schema = json.loads(json.dumps(schema))
    timelapse_schema["properties"]["type"]["const"] = "timelapse"
    # 估算文本独立精确解析，避免剧本的普通 JSON 解析先舍入数字。
    estimate_texts = spec.get("video_size_estimate_json", {})
    estimates = {kind: parse_exact_json(text) for kind, text in estimate_texts.items()}
    schemas = spec.get("parameter_schema_json", {})
    if "camera_record" in schemas:
        record_schema = parse_exact_json(schemas["camera_record"])
    if "camera_timelapse" in schemas:
        timelapse_schema = parse_exact_json(schemas["camera_timelapse"])
    return DriverDefinition(
        driver_id=spec["driver_id"],
        actions={
            "camera_take_photo": (ActionCapability(
                action_type="camera_take_photo", parameter_type="single_shot",
                name="单张拍摄", description="跨组件替身的单张拍摄",
                preview_supported=bool(
                    spec.get("photo_preview_supported", False)),
                schema=photo_schema, defaults={},
                task_factory=photo_task),),
            "camera_record": (ActionCapability(
                action_type="camera_record", parameter_type="video",
                name="录像", description="跨组件替身的停止控制录像",
                preview_supported=False, schema=record_schema, defaults={},
                task_factory=record_task,
                video_size_estimate=estimates.get("camera_record")),),
            "camera_timelapse": (ActionCapability(
                action_type="camera_timelapse", parameter_type="timelapse",
                name="延时摄影", description="跨组件替身的定时结束延时任务",
                preview_supported=False, schema=timelapse_schema, defaults={},
                task_factory=timelapse_task,
                video_size_estimate=estimates.get("camera_timelapse")),),
        },
    )


def install_stub_driver(spec: dict) -> None:
    """部署装配：驱动定义与运行端口同源登记，随后由生产 CLI 接管。"""
    from camctl.devices.definitions_runtime import register_driver_definitions
    from camctl.devices.drivers.registry import DriverEntry, DriverStatus
    from camctl.devices.drivers.runtime import register_drivers
    from camctl.devices.evidence import EvidenceContract, EvidenceRegistry
    from camctl.devices.ports import DriverDeclaration
    from camctl.capture.result_inputs import RESULT_FILES_CONTRACT, RESULT_PAGE_CONTRACT

    evidence = EvidenceRegistry((RESULT_FILES_CONTRACT, RESULT_PAGE_CONTRACT) + tuple(
        EvidenceContract(
            type=kind, version=version, operation=operation, fields=fields,
            identity_field=identity)
        for kind, version, operation, fields, identity
        in _STUB_EVIDENCE_CONTRACTS
    ))
    register_driver_definitions(_stub_definition(spec))
    register_drivers(DriverEntry(
        driver_id=spec["driver_id"],
        driver=_ScriptedStubDriver(spec, Path(spec.get("gate_dir", "."))),
        declaration=DriverDeclaration(
            control_supported=True,
            stop_supported=True,
            query_supported=True,
            result_supported=True,
            read_supported=True,
            digest_supported=True,
            delete_supported=True,
        ),
        evidence=evidence,
        status=DriverStatus.SOFTWARE_CONTRACT_VERIFIED,
    ))

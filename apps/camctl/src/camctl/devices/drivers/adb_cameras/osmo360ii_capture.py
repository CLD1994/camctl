"""OSMO 360 II 普通录像和原生有限延时的共同正式合同。"""

from dataclasses import replace
from decimal import Decimal
import shlex

from camctl.devices.adb_transport import DeviceCommand
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry, RESULT_PAGE_CONTRACT
from camctl.devices.tasks import (CancelCompletionPolicy, CaptureTask, CompletionMode, EndControl, StartReturn)
from .action6_record import _digest_timeout
from .commands import CameraModel, OSMO_RECORD_FORMAT, OSMO_SESSION_SETUP
from .contracts import CameraCall, CameraContract
from .filesystem import ShellFileTools
from .file_commands import FILE_COMMAND_EVIDENCE, file_command_factory
from .osmo360ii_responses import OSMO360II_OBSERVATIONS, Osmo360IIDjiInterpreter, Osmo360IISimulationInterpreter
from .osmo360ii_results import list_osmo360ii_results


def _task(params):
    timelapse = params.get("type") == "osmo360ii_timelapse"
    task = CaptureTask("camera_timelapse" if timelapse else "camera_record",
        target_duration_s=params["duration_s"], stop_supported=True,
        start_return_meaning=StartReturn.SENT,
        startup_margin_ms=0,
        file_completion_wait_s=Decimal("30"), ownership_mode=2,
        output_scope={"directories": ["/mnt/media_rw/emulated/DCIM"]},
        product_rules=({"kind": "video", "format_id": "osv", "min_count": 1,
            "exact_count": None if timelapse else 1, "require_pairing": False, "min_size_bytes": 1},))
    if timelapse:
        task = replace(task, duration_based=True, wait_after_send=True, end_control=EndControl.DEVICE,
            start_return_meaning=StartReturn.SENT, completion_mode=CompletionMode.TIME_AND_OUTPUTS,
            result_wait_margin_s=Decimal("30"), cancel_completion_policy=CancelCompletionPolicy.FINITE_DEVICE_END)
    return task


class _Osmo360IIContract(CameraContract):
    def capabilities(self):
        return tuple(replace(cap, preview_supported=True,
            description=cap.description.replace("参数合法不表示已有完整命令映射；", "")
                + "OSV 为原始视频，LRF 为可选配对预览。"
                + "相机在本会话首次拍摄准备时跳过时间设置并选择仅充电；固定设置按需执行，码率按每个动作动态设置。"
                + ("完成判断采用正常设备运行与停止确认后固定 30 秒文件等待假设。"
                "普通取回优先交付已登记的修复 MP4，原 OSV 可用明确产物 ID 取回；"
                "修复保留双路视频及音频内容，不声明厂商原生编辑兼容。"
                if cap.action_type == "camera_record" else
                "完成判断采用正常设备执行有限目标后自行结束的运行假设，"
                "目标后等待固定 30 秒结果余量再核实原片；设备采集目标保持原值。"))
            for cap in super().capabilities())


def osmo360ii_contract():
    returned = {operation: EvidenceContract(operation + "_returned", 1, operation, frozenset())
                for operation in ("control", "stop")}
    foreground = {operation: EvidenceContract(operation + "_foreground_recovery", 1, operation,
                    frozenset({"terminate_grace_s"})) for operation in ("control", "stop", "result", "read")}
    tools = ShellFileTools()
    evidence = EvidenceRegistry((*returned.values(), *foreground.values(), *OSMO360II_OBSERVATIONS,
        *FILE_COMMAND_EVIDENCE,
        EvidenceContract("adb_foreground_recovery", 1, "delete", frozenset()),
        EvidenceContract("dispatch_prevented", 1, "control", frozenset()), RESULT_PAGE_CONTRACT,
        EvidenceContract("result_returned", 1, "result", frozenset({"file_completion",
            "file_completion_source_page_event_id", "calls", "call_count", "optional_preview_observations"})),
        EvidenceContract("file_digest", 1, "digest", frozenset({"file_id", "sha256"}), identity_field="file_id"),
        EvidenceContract("read_returned", 1, "read", frozenset())))

    def factory(kind):
        def build(request, serial, argv, batch):
            if argv is None:
                raise ValueError("OSMO 控制要求原明确命令")
            command = tuple(shlex.split(shlex.split(argv[5])[2]))
            interpreter = (Osmo360IISimulationInterpreter(request.ticket.target_id)
                if command[0] == "simulate_device" and kind is CameraCall.SETTING
                else Osmo360IIDjiInterpreter(kind, request.operation, request.ticket.target_id, command,
                    start_return_meaning=request.start_return_meaning))
            operation = request.ticket.operation
            return DeviceCommand(operation, argv, request.binding, request.timeout_s, Decimal("1"), evidence,
                returned[operation], foreground[operation], interpreter, kind.value)
        return build

    # 两个入口固定任务种类；工厂不会依赖可省略的 type 来混淆任务。
    return _Osmo360IIContract(CameraModel.OSMO360II,
        task_factories={name: (lambda params, name=name: _task({**params, "type": name}))
            for name in ("osmo360ii_record", "osmo360ii_timelapse")},
        commands={**{kind: factory(kind) for kind in (CameraCall.SETTING, CameraCall.START, CameraCall.STOP)},
            **{kind: file_command_factory(kind, tools=tools, evidence=evidence)
               for kind in (CameraCall.FILE_QUERY, CameraCall.DELETE)}},
        file_tools=tools, digest_timeout_s=_digest_timeout, evidence=evidence,
        session_setup_commands=OSMO_SESSION_SETUP,
        session_once_commands=frozenset((*OSMO_SESSION_SETUP, OSMO_RECORD_FORMAT)),
        result_reader=list_osmo360ii_results,
        recovery_operations=frozenset({"control", "stop", "result", "read", "query", "delete"}))

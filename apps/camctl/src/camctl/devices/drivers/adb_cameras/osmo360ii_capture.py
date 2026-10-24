"""OSMO 360 II 普通录像和原生有限延时的共同正式合同。"""

from dataclasses import replace
from decimal import Decimal
import shlex

from camctl.devices.adb_transport import DeviceCommand
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry, RESULT_PAGE_CONTRACT
from camctl.devices.tasks import (CancelCompletionPolicy, CaptureResultPolicy, CaptureTask,
                                  CompletionMode, EndControl, StartReturn, VideoResultMode,
                                  video_result_mode_for_declaration)
from .action6_record import _digest_timeout
from .commands import CameraModel, OSMO_RECORD_FORMAT, OSMO_SESSION_SETUP
from .contracts import CameraCall, CameraContract
from .filesystem import ShellFileTools
from .file_commands import FILE_COMMAND_EVIDENCE, file_command_factory
from .osmo360ii_responses import OSMO360II_OBSERVATIONS, Osmo360IIDjiInterpreter, Osmo360IISimulationInterpreter
from .osmo360ii_results import list_osmo360ii_results


FILE_COMPLETION_WAIT_S = Decimal("3")


def _task(params, *, result_mode=VideoResultMode.SINGLE_FILE):
    result_mode = VideoResultMode(result_mode)
    timelapse = params.get("type") == "osmo360ii_timelapse"
    task = CaptureTask("camera_timelapse" if timelapse else "camera_record",
        target_duration_s=params["duration_s"], stop_supported=True,
        start_return_meaning=StartReturn.SENT,
        startup_margin_ms=0,
        file_completion_wait_s=FILE_COMPLETION_WAIT_S, ownership_mode=2,
        output_scope={"directories": ["/mnt/media_rw/emulated/DCIM"]},
        product_rules=({"kind": "video", "format_id": "osv", "min_count": 1,
            "exact_count": (1 if not timelapse and result_mode is VideoResultMode.SINGLE_FILE else None),
            "require_pairing": False, "min_size_bytes": 1},),
        video_result_mode=result_mode,
        result_policy=((CaptureResultPolicy.OSMO_EMPTY_TAIL_CLASSIFICATION_V1 if timelapse
            else CaptureResultPolicy.OSMO360II_RECORD_SEGMENTS_V1)
            if result_mode is VideoResultMode.SEGMENTED else None))
    if timelapse:
        task = replace(task, duration_based=True, wait_after_send=True, end_control=EndControl.DEVICE,
            start_return_meaning=StartReturn.SENT, completion_mode=CompletionMode.TIME_AND_OUTPUTS,
            result_wait_margin_s=Decimal("30"), cancel_completion_policy=CancelCompletionPolicy.FINITE_DEVICE_END)
    return task


def _bind_osmo_task(task_type):
    def bind(declaration):
        result_mode = video_result_mode_for_declaration(declaration)
        return lambda params: _task({**params, "type": task_type}, result_mode=result_mode)
    return bind


def _deployment_description_factory(action_type):
    def describe(declaration):
        result_mode = video_result_mode_for_declaration(declaration)
        if action_type == "camera_record":
            if result_mode is VideoResultMode.SINGLE_FILE:
                return "当前部署使用单文件视频结果模式，普通录像沿单原片流程处理。"
            return ("当前部署使用分段视频结果模式，普通录像按实际分段保留所有合格 OSV，"
                    "逐段登记可选配对 LRF。")
        if result_mode is VideoResultMode.SINGLE_FILE:
            return "当前部署使用单文件视频结果模式，原生延时保留有限任务的原有产物规则。"
        return "当前部署使用分段视频结果模式，原生延时使用 OSMO 空尾分类策略。"
    return describe


_OSMO_DEPLOYMENT_TASK_FACTORIES = {
    "osmo360ii_record": _bind_osmo_task("osmo360ii_record"),
    "osmo360ii_timelapse": _bind_osmo_task("osmo360ii_timelapse"),
}
_OSMO_DEPLOYMENT_DESCRIPTION_FACTORIES = {
    action_type: _deployment_description_factory(action_type)
    for action_type in ("camera_record", "camera_timelapse")
}


class _Osmo360IIContract(CameraContract):
    def capabilities(self):
        return tuple(
            replace(
                cap,
                preview_supported=True,
                deployment_task_factory=(_OSMO_DEPLOYMENT_TASK_FACTORIES.get(cap.parameter_type)
                    if callable(cap.task_factory) else None),
                deployment_description_factory=(_OSMO_DEPLOYMENT_DESCRIPTION_FACTORIES.get(cap.action_type)
                    if callable(cap.task_factory) else None),
                description=cap.description.replace(
                    "参数合法不表示已有完整命令映射；", ""
                )
                + "OSV 为原始视频，LRF 为可选配对预览。"
                + "相机在本会话首次拍摄准备时跳过时间设置、关闭自动休眠并选择仅充电；"
                "本地设置命令被接受不表示已读回相机实际状态。固定设置按需执行，码率按每个动作动态设置。"
                + (
                    f"完成判断采用正常设备运行与停止确认后固定 {FILE_COMPLETION_WAIT_S} 秒文件等待假设。"
                    "普通取回按已保存范围交付整段原文件，明确产物 ID 可取回范围外原片；"
                    "运行时不测量或裁剪媒体时长，浏览器播放能力由实际格式和解码支持决定。"
                    if cap.action_type == "camera_record"
                    else "完成判断采用正常设备执行有限目标后自行结束的运行假设，"
                    "目标后等待固定 30 秒结果余量再核实原片；设备采集目标保持原值。"
                ),
            )
            for cap in super().capabilities()
        )


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
        task_factories={name: (lambda params, name=name,
            result_mode=VideoResultMode.SINGLE_FILE: _task(
                {**params, "type": name}, result_mode=result_mode))
            for name in ("osmo360ii_record", "osmo360ii_timelapse")},
        commands={**{kind: factory(kind) for kind in (CameraCall.SETTING, CameraCall.START, CameraCall.STOP)},
            **{kind: file_command_factory(kind, tools=tools, evidence=evidence)
               for kind in (CameraCall.FILE_QUERY, CameraCall.DELETE)}},
        file_tools=tools, digest_timeout_s=_digest_timeout, evidence=evidence,
        os_reboot_recovery_supported=True,
        session_setup_commands=OSMO_SESSION_SETUP,
        session_once_commands=frozenset((*OSMO_SESSION_SETUP, OSMO_RECORD_FORMAT)),
        result_reader=list_osmo360ii_results,
        recovery_operations=frozenset({"control", "stop", "result", "read", "query", "delete"}))

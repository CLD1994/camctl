"""Action6 录像与原生有限延时任务契约；完成依据包含明确运行假设。"""

from dataclasses import replace
from decimal import Decimal
import shlex

from camctl.devices.adb_transport import DeviceCommand
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry, RESULT_PAGE_CONTRACT
from camctl.devices.tasks import (CancelCompletionPolicy, CaptureResultPolicy, CaptureTask, CompletionMode,
                                  EndControl, StartReturn, VideoResultMode,
                                  video_result_mode_for_declaration)
from .action6_formats import product_rules_for_output
from .commands import ACTION_APERTURE, ACTION_BITRATE, ACTION6_SESSION_SETUP, CameraModel
from .contracts import CAPTURE_PREPARATION_COMPLETED, CameraCall, CameraContract
from .filesystem import ShellFileTools
from .file_commands import FILE_COMMAND_EVIDENCE, file_command_factory
from .action6_results import list_action6_results
from .responses import (ACTION6_RECORD_OBSERVATIONS, ACTION6_TIMELAPSE_OBSERVATIONS, Action6DjiInterpreter,
                        Action6SimulationSettingInterpreter)


FILE_COMPLETION_WAIT_S = Decimal("5")
STARTUP_MARGIN_MS = 1000
ACTION6_TIMELAPSE_RESULT_MARGIN_S = Decimal("30")
ACTION6_TIMELAPSE_FILE_WAIT_S = Decimal("3")
ACTION6_TIMELAPSE_TASK_METADATA = CaptureTask(
    "camera_timelapse", stop_supported=True, duration_based=True, wait_after_send=True,
    end_control=EndControl.DEVICE, start_return_meaning=StartReturn.SENT,
    completion_mode=CompletionMode.TIME_AND_OUTPUTS,
    result_wait_margin_s=ACTION6_TIMELAPSE_RESULT_MARGIN_S,
    file_completion_wait_s=ACTION6_TIMELAPSE_FILE_WAIT_S, ownership_mode=2,
    output_scope={"directories": ["/mnt/media_rw/emulated/DCIM"]},
    cancel_completion_policy=CancelCompletionPolicy.FINITE_DEVICE_END,
    startup_margin_ms=0)


def _task(params, *, result_mode=VideoResultMode.SINGLE_FILE):
    result_mode = VideoResultMode(result_mode)
    return CaptureTask("camera_record", target_duration_s=params["duration_s"], stop_supported=True,
        start_return_meaning=StartReturn.SENT, startup_margin_ms=STARTUP_MARGIN_MS,
        file_completion_wait_s=FILE_COMPLETION_WAIT_S, ownership_mode=2,
        output_scope={"directories": ["/mnt/media_rw/emulated/DCIM"]},
        product_rules=({"kind": "video", "format_id": "mp4", "min_count": 1,
                        "exact_count": (1 if result_mode is VideoResultMode.SINGLE_FILE else None),
                        "require_pairing": False, "min_size_bytes": 1},), video_result_mode=result_mode,
        result_policy=(CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1
            if result_mode is VideoResultMode.SEGMENTED else None))


def _timelapse_task(params, *, result_mode=VideoResultMode.SINGLE_FILE):
    result_mode = VideoResultMode(result_mode)
    if "outputs" not in params:
        raise ValueError("Action6 原生延时任务必须明确提供 outputs")
    return replace(ACTION6_TIMELAPSE_TASK_METADATA,
        target_duration_s=params["duration_s"],
        product_rules=product_rules_for_output(params["outputs"]),
        video_result_mode=result_mode,
        result_policy=(CaptureResultPolicy.ACTION6_VIDEO_SEGMENTS_V1
            if result_mode is VideoResultMode.SEGMENTED else None))


def _deployment_task_factory(task_factory):
    def bind(declaration):
        result_mode = video_result_mode_for_declaration(declaration)
        return lambda params: task_factory(params, result_mode=result_mode)
    return bind


def _deployment_description_factory(action_type):
    def describe(declaration):
        result_mode = video_result_mode_for_declaration(declaration)
        if action_type == "camera_record":
            if result_mode is VideoResultMode.SINGLE_FILE:
                return "当前部署使用单文件视频结果模式，普通录像要求恰好一份 MP4 原片。"
            return "当前部署使用分段视频结果模式，普通录像至少要求一份非空 MP4 原片。"
        if result_mode is VideoResultMode.SINGLE_FILE:
            return "当前部署使用单文件视频结果模式；原生延时视频和所选独立照片仍按各自原有最低产物规则处理。"
        return "当前部署使用分段视频结果模式；原生延时视频分段与所选独立照片仍分别按各自规则处理。"
    return describe


_ACTION6_DEPLOYMENT_TASK_FACTORIES = {
    "action6_record": _deployment_task_factory(_task),
    "action6_timelapse": _deployment_task_factory(_timelapse_task),
}
_ACTION6_DEPLOYMENT_DESCRIPTION_FACTORIES = {
    action_type: _deployment_description_factory(action_type)
    for action_type in ("camera_record", "camera_timelapse")
}


def _digest_timeout(size):
    if type(size) is not int or size < 0:
        raise ValueError("源摘要要求原可靠的非负文件长度")
    return max(Decimal("60"), Decimal(size) / Decimal(4 * 1024 * 1024) + Decimal("30"))


class _Action6Contract(CameraContract):
    def capabilities(self):
        return tuple(replace(cap, preview_supported=True,
            deployment_task_factory=(_ACTION6_DEPLOYMENT_TASK_FACTORIES.get(cap.parameter_type)
                if callable(cap.task_factory) else None),
            deployment_description_factory=(_ACTION6_DEPLOYMENT_DESCRIPTION_FACTORIES.get(cap.action_type)
                if callable(cap.task_factory) else None),
            description=cap.description + "MP4 为视频原片，唯一同目录、同 stem 的 LRF 为可选视频预览；"
                "JPG、DNG 为独立照片原片，不生成照片预览。"
                "本会话首次拍摄准备时跳过时间设置、关闭自动休眠，并关闭两路屏幕实时画面；"
                "本地设置命令被接受不表示已读回相机实际状态。"
                + ("完成判断采用正常设备执行有限目标后自行结束的运行假设；"
                   f"目标后结果余量为 {ACTION6_TIMELAPSE_RESULT_MARGIN_S} 秒，"
                   f"提前停止后的文件等待为 {ACTION6_TIMELAPSE_FILE_WAIT_S} 秒，均非实测保证。"
                   if cap.action_type == "camera_timelapse" else
                   "完成判断采用正常设备运行、停止确认后固定 5 秒文件等待的运行假设。"))
            for cap in super().capabilities())


def action6_record_contract():
    returned = {operation: EvidenceContract(operation + "_returned", 1, operation, frozenset())
                for operation in ("control", "stop")}
    foreground = {operation: EvidenceContract(operation + "_foreground_recovery", 1, operation,
                    frozenset({"terminate_grace_s"})) for operation in ("control", "stop", "result", "read")}
    tools = ShellFileTools()
    calls = frozenset({"calls"})
    evidence = EvidenceRegistry((*returned.values(), *foreground.values(), *ACTION6_RECORD_OBSERVATIONS,
        *ACTION6_TIMELAPSE_OBSERVATIONS, *FILE_COMMAND_EVIDENCE, CAPTURE_PREPARATION_COMPLETED,
        EvidenceContract("adb_foreground_recovery", 1, "delete", frozenset()),
        EvidenceContract("dispatch_prevented", 1, "control", frozenset()),
        EvidenceContract("control_returned", 2, "control", calls),
        EvidenceContract("control_foreground_recovery", 2, "control",
                         frozenset({"terminate_grace_s", "calls"})),
        EvidenceContract("tool_not_started", 1, "control", frozenset()),
        EvidenceContract("tool_not_started", 2, "control", calls), RESULT_PAGE_CONTRACT,
        EvidenceContract("result_returned", 1, "result", frozenset({
            "file_completion", "file_completion_source_page_event_id", "calls"})),
        EvidenceContract("result_returned", 2, "result", frozenset({
            "file_completion", "file_completion_source_page_event_id", "calls", "call_count",
            "optional_preview_observations"})),
        EvidenceContract("file_digest", 1, "digest", frozenset({"file_id", "sha256"}), identity_field="file_id"),
        EvidenceContract("read_returned", 1, "read", frozenset())))

    def factory(kind):
        def build(request, serial, argv, batch):
            if argv is None:
                raise ValueError("录像控制要求原明确命令")
            script = shlex.split(argv[5])[2]
            command = tuple(shlex.split(script))
            interpreter = (Action6SimulationSettingInterpreter(request.ticket.target_id)
                if (kind is CameraCall.SETTING
                    and request.operation in {"start_recording", "start_timelapse"}
                    and (command in ACTION6_SESSION_SETUP
                         or (request.operation == "start_recording" and command in ACTION_BITRATE.values())))
                else Action6DjiInterpreter(kind, request.operation, request.ticket.target_id,
                    start_return_meaning=request.start_return_meaning))
            operation = request.ticket.operation
            return DeviceCommand(operation, argv, request.binding, request.timeout_s, Decimal("1"), evidence,
                                 returned[operation], foreground[operation], interpreter, kind.value)
        return build

    return _Action6Contract(CameraModel.ACTION6,
        task_factories={"action6_record": _task, "action6_timelapse": _timelapse_task},
        task_metadata={"action6_timelapse": ACTION6_TIMELAPSE_TASK_METADATA},
        commands={**{kind: factory(kind) for kind in (CameraCall.SETTING, CameraCall.START, CameraCall.STOP)},
            **{kind: file_command_factory(kind, tools=tools, evidence=evidence)
               for kind in (CameraCall.FILE_QUERY, CameraCall.DELETE)}},
        file_tools=tools, digest_timeout_s=_digest_timeout, evidence=evidence,
        os_reboot_recovery_supported=True,
        session_setup_commands=ACTION6_SESSION_SETUP,
        session_once_commands=frozenset(ACTION6_SESSION_SETUP),
        non_blocking_setting_commands=frozenset(
            ("start_recording", command) for command in ACTION_APERTURE.values()),
        setting_call_history_operations=frozenset({"start_recording"}),
        result_reader=list_action6_results, recovery_operations=frozenset({"control", "stop", "result", "read", "query", "delete"}))

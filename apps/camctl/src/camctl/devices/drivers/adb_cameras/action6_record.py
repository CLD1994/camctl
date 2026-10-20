"""Action6 录像与原生有限延时任务契约；完成依据包含明确运行假设。"""

from dataclasses import replace
from decimal import Decimal
import shlex

from camctl.devices.adb_transport import DeviceCommand
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry, RESULT_PAGE_CONTRACT
from camctl.devices.tasks import (CancelCompletionPolicy, CaptureTask, CompletionMode,
                                  EndControl, StartReturn)
from .action6_formats import product_rules_for_output
from .commands import ACTION_APERTURE, ACTION_BITRATE, CameraModel
from .contracts import CameraCall, CameraContract
from .filesystem import ShellFileTools
from .file_commands import FILE_COMMAND_EVIDENCE, file_command_factory
from .action6_results import list_action6_results
from .responses import (ACTION6_RECORD_OBSERVATIONS, ACTION6_TIMELAPSE_OBSERVATIONS, Action6DjiInterpreter,
                        Action6SimulationSettingInterpreter)


FILE_COMPLETION_WAIT_S = Decimal("5")
STARTUP_MARGIN_MS = 1000
ACTION6_TIMELAPSE_RESULT_MARGIN_S = Decimal("30")
ACTION6_TIMELAPSE_FILE_WAIT_S = Decimal("30")
ACTION6_TIMELAPSE_TASK_METADATA = CaptureTask(
    "camera_timelapse", stop_supported=True, duration_based=True, wait_after_send=True,
    end_control=EndControl.DEVICE, start_return_meaning=StartReturn.SENT,
    completion_mode=CompletionMode.TIME_AND_OUTPUTS,
    result_wait_margin_s=ACTION6_TIMELAPSE_RESULT_MARGIN_S,
    file_completion_wait_s=ACTION6_TIMELAPSE_FILE_WAIT_S, ownership_mode=2,
    output_scope={"directories": ["/mnt/media_rw/emulated/DCIM"]},
    cancel_completion_policy=CancelCompletionPolicy.FINITE_DEVICE_END,
    startup_margin_ms=0)


def _task(params):
    return CaptureTask("camera_record", target_duration_s=params["duration_s"], stop_supported=True,
        start_return_meaning=StartReturn.SENT, startup_margin_ms=STARTUP_MARGIN_MS,
        file_completion_wait_s=FILE_COMPLETION_WAIT_S, ownership_mode=2,
        output_scope={"directories": ["/mnt/media_rw/emulated/DCIM"]},
        product_rules=({"kind": "video", "format_id": "mp4", "min_count": 1,
                        "exact_count": 1, "require_pairing": False},))


def _timelapse_task(params):
    if "outputs" not in params:
        raise ValueError("Action6 原生延时任务必须明确提供 outputs")
    return replace(ACTION6_TIMELAPSE_TASK_METADATA,
        target_duration_s=params["duration_s"],
        product_rules=product_rules_for_output(params["outputs"]))


def _digest_timeout(size):
    if type(size) is not int or size < 0:
        raise ValueError("源摘要要求原可靠的非负文件长度")
    return max(Decimal("60"), Decimal(size) / Decimal(4 * 1024 * 1024) + Decimal("30"))


class _Action6Contract(CameraContract):
    def capabilities(self):
        return tuple(replace(cap, preview_supported=True,
            description=cap.description + "MP4 为视频原片，唯一同目录、同 stem 的 LRF 为可选视频预览；"
                "JPG、DNG 为独立照片原片，不生成照片预览。"
                + ("完成判断采用正常设备执行有限目标后自行结束的运行假设；"
                   "目标后结果余量和提前停止后的文件等待各为 30 秒，均非实测保证。"
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
        *ACTION6_TIMELAPSE_OBSERVATIONS, *FILE_COMMAND_EVIDENCE,
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
                if (kind is CameraCall.SETTING and request.operation == "start_recording"
                    and command in ACTION_BITRATE.values())
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
        non_blocking_setting_commands=frozenset(
            ("start_recording", command) for command in ACTION_APERTURE.values()),
        setting_call_history_operations=frozenset({"start_recording"}),
        result_reader=list_action6_results, recovery_operations=frozenset({"control", "stop", "result", "read", "query", "delete"}))

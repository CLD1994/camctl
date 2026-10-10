"""Action6 普通录像契约；完成依据包含明确的正常设备运行假设。"""

from decimal import Decimal
import shlex

from camctl.devices.adb_transport import DeviceCommand
from camctl.devices.evidence import EvidenceContract, EvidenceRegistry, RESULT_PAGE_CONTRACT
from camctl.devices.tasks import CaptureTask
from .commands import CameraModel
from .contracts import CameraCall, CameraContract
from .filesystem import ShellFileTools
from .record_results import list_record_results
from .responses import ACTION6_RECORD_OBSERVATIONS, Action6DjiInterpreter, Action6UnconfirmedSettingInterpreter


FILE_COMPLETION_WAIT_S = Decimal("5")


def _task(params):
    return CaptureTask("camera_record", target_duration_s=params["duration_s"], stop_supported=True,
        file_completion_wait_s=FILE_COMPLETION_WAIT_S, ownership_mode=2,
        output_scope={"directories": ["/mnt/media_rw/emulated/DCIM"]},
        product_rules=({"kind": "video", "format_id": "mp4", "min_count": 1,
                        "exact_count": 1, "require_pairing": False},))


def _digest_timeout(size):
    if type(size) is not int or size < 0:
        raise ValueError("源摘要要求原可靠的非负文件长度")
    return max(Decimal("60"), Decimal(size) / Decimal(4 * 1024 * 1024) + Decimal("30"))


def action6_record_contract():
    returned = {operation: EvidenceContract(operation + "_returned", 1, operation, frozenset())
                for operation in ("control", "stop")}
    foreground = {operation: EvidenceContract(operation + "_foreground_recovery", 1, operation,
                    frozenset({"terminate_grace_s"})) for operation in ("control", "stop", "result", "read")}
    evidence = EvidenceRegistry((*returned.values(), *foreground.values(), *ACTION6_RECORD_OBSERVATIONS,
        EvidenceContract("dispatch_prevented", 1, "control", frozenset()), RESULT_PAGE_CONTRACT,
        EvidenceContract("result_returned", 1, "result", frozenset({
            "file_completion", "file_completion_source_page_event_id", "calls"})),
        EvidenceContract("file_digest", 1, "digest", frozenset({"file_id", "sha256"}), identity_field="file_id"),
        EvidenceContract("read_returned", 1, "read", frozenset())))

    def factory(kind):
        def build(request, serial, argv, batch):
            if argv is None:
                raise ValueError("录像控制要求原明确命令")
            script = shlex.split(argv[5])[2]
            executable = shlex.split(script)[0]
            interpreter = (Action6UnconfirmedSettingInterpreter() if executable == "simulate_device" else
                           Action6DjiInterpreter(kind, request.operation, request.ticket.target_id))
            operation = request.ticket.operation
            return DeviceCommand(operation, argv, request.binding, request.timeout_s, Decimal("1"), evidence,
                                 returned[operation], foreground[operation], interpreter, kind.value)
        return build

    return CameraContract(CameraModel.ACTION6, task_factories={"action6_record": _task},
        commands={kind: factory(kind) for kind in (CameraCall.SETTING, CameraCall.START, CameraCall.STOP)},
        file_tools=ShellFileTools(), digest_timeout_s=_digest_timeout, evidence=evidence,
        result_reader=list_record_results, recovery_operations=frozenset({"control", "stop", "result", "read"}))

"""软件测试显式注入的相机契约；不代表真实固件的响应。"""
from dataclasses import replace
from decimal import Decimal

from camctl.capture.result_inputs import RESULT_PAGE_CONTRACT
from camctl.devices.adb_transport import DeviceCommand, InterpretedFacts
from camctl.devices.drivers.adb_cameras.commands import CameraModel
from camctl.devices.drivers.adb_cameras.contracts import CameraCall, CameraContract
from camctl.devices.drivers.adb_cameras.definitions import candidate_capabilities, output_scope_for
from camctl.devices.drivers.adb_cameras.filesystem import ShellFileTools
from camctl.devices.drivers.adb_cameras.transport import shell_argv
from camctl.devices.evidence import DeviceObservation, EvidenceContract, EvidenceRegistry
from camctl.devices.tasks import CaptureTask, CompletionMode, EndControl, StartReturn
from camctl.operations.models import EffectState, ErrorValue


def software_contract(model):
    returned = {operation: EvidenceContract(f"{operation}_returned", 1, operation, frozenset())
                for operation in ("control", "stop", "result", "delete")}
    assumptions = {operation: EvidenceContract(f"{operation}_foreground_recovery", 1, operation,
                                               frozenset({"terminate_grace_s"})) for operation in returned}
    observations = (
        EvidenceContract("dispatch_prevented", 1, "control", frozenset()),
        *(EvidenceContract(name, 1, operation, frozenset({"activity_id"}), identity_field="activity_id")
          for name, operation in (("setting_applied", "control"), ("start_confirmed", "control"),
                                  ("timelapse_sent", "control"), ("stop_confirmed", "stop"))),
        EvidenceContract("file_digest", 1, "digest", frozenset({"file_id", "sha256"}), identity_field="file_id"),
        EvidenceContract("file_absent", 1, "delete", frozenset({"cleanup_item_id"}), identity_field="cleanup_item_id"),
        RESULT_PAGE_CONTRACT,
    )
    evidence = EvidenceRegistry((*returned.values(), *assumptions.values(), *observations))
    class Parser:
        def __init__(self, kind, request):
            self.kind, self.request = kind, request
        def interpret(self, raw):
            if (raw.error is not None or raw.output_failure is not None
                    or raw.exit is None or raw.exit.exit_code != 0):
                return InterpretedFacts((), ErrorValue("software_call_failed", "device"), EffectState.UNKNOWN)
            if self.kind is CameraCall.RESULT:
                observation = DeviceObservation("result_files_listed", 2, {
                    "activity_id": self.request.ticket.target_id, "entries": [],
                    "cursor": self.request.params.get("cursor"), "next_cursor": None,
                    "set_finalized": True, "completion_evidence": None})
            elif self.kind is CameraCall.DELETE:
                observation = DeviceObservation("file_absent", 1, {"cleanup_item_id": self.request.ticket.target_id})
            else:
                name = ("setting_applied" if self.kind is CameraCall.SETTING else "stop_confirmed"
                        if self.kind is CameraCall.STOP else "timelapse_sent"
                        if self.request.operation == "start_timelapse" else "start_confirmed")
                observation = DeviceObservation(name, 1, {"activity_id": self.request.ticket.target_id})
            return InterpretedFacts((observation,), None, EffectState.CONFIRMED)
    def command_factory(kind):
        def build(request, serial, argv, batch):
            operation = request.ticket.operation
            return DeviceCommand(operation, argv or shell_argv(serial, "true"), request.binding,
                request.timeout_s, Decimal("1"), evidence, returned[operation], assumptions[operation],
                Parser(kind, request), kind.value)
        return build
    def task(params):
        action_type = "camera_record" if params["type"].endswith("_record") else "camera_timelapse"
        rules = [{"kind": "video", "format_id": "mp4", "min_count": 1,
                  "exact_count": None, "require_pairing": False}]
        if params.get("outputs") in ("video_raw", "video_jpeg"):
            rules.append({"kind": "photo", "format_id": "dng" if params["outputs"] == "video_raw" else "jpeg",
                          "min_count": 1, "exact_count": None, "require_pairing": False})
        fixed = CaptureTask(action_type, target_duration_s=params["duration_s"], stop_supported=True,
            ownership_mode=2, output_scope=output_scope_for(model), product_rules=tuple(rules))
        if action_type == "camera_timelapse":
            return replace(fixed, stop_supported=model is CameraModel.OSMO360II,
                duration_based=True, wait_after_send=True, end_control=EndControl.DEVICE,
                start_return_meaning=StartReturn.SENT, completion_mode=CompletionMode.TIME_AND_OUTPUTS,
                result_wait_margin_s=0)
        return fixed
    return CameraContract(model, task_factories={cap.parameter_type: task for cap in candidate_capabilities(model)},
        commands={kind: command_factory(kind) for kind in (CameraCall.SETTING, CameraCall.START,
                  CameraCall.STOP, CameraCall.RESULT, CameraCall.DELETE)},
        file_tools=ShellFileTools(), digest_timeout_s=lambda _size: Decimal("300"), evidence=evidence,
        capture_read_parallel_supported=True)

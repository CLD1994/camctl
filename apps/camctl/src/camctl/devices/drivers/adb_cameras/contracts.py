"""具体相机的任务和响应契约；候选命令不自动提供可用能力。"""

from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Awaitable, Callable, Mapping, Any

from camctl.devices.adb_transport import DeviceCommand
from camctl.devices.capture_preparation import (
    CAPTURE_PREPARATION_COMPLETED, DEFAULT_PREPARE_LEAD_S,
)
from camctl.devices.drivers.registry import DriverStatus
from camctl.devices.evidence import EvidenceContract, EvidenceError, EvidenceRegistry
from camctl.devices.ports import ControlRequest, DriverDeclaration, DeviceCallResult
from camctl.devices.tasks import (CaptureStorage, CaptureTask, CaptureTaskFactory,
                                  CompletionMode, EndControl, StartReturn)
from camctl.devices.task_metadata import cancellation_description
from .commands import CameraModel, Command
from .definitions import candidate_capabilities
from .filesystem import ShellFileTools


class CameraCall(StrEnum):
    SETTING = "setting"
    START = "start"
    STOP = "stop"
    RESULT = "result"
    QUERY = "query"
    FILE_QUERY = "file_query"
    DELETE = "delete"


CommandFactory = Callable[[ControlRequest, str, tuple[str, ...] | None, int | None], DeviceCommand]


@dataclass(frozen=True)
class CameraContract:
    driver_id: CameraModel
    task_factories: Mapping[str, CaptureTaskFactory] = field(default_factory=dict)
    task_metadata: Mapping[str, CaptureTask] = field(default_factory=dict)
    commands: Mapping[CameraCall, CommandFactory] = field(default_factory=dict)
    file_tools: ShellFileTools | None = None
    digest_timeout_s: Callable[[int], Decimal] | None = None
    evidence: EvidenceRegistry = field(default_factory=lambda: EvidenceRegistry(()))
    status: DriverStatus = DriverStatus.DEVICE_VERIFICATION_PENDING
    capture_read_parallel_supported: bool = False
    os_reboot_recovery_supported: bool = False
    recovery_operations: frozenset[str] = frozenset()
    result_reader: Callable[[Any, ControlRequest, int], Awaitable[DeviceCallResult]] | None = None
    session_setup_commands: tuple[Command, ...] = ()
    session_once_commands: frozenset[Command] = frozenset()
    non_blocking_setting_commands: frozenset[tuple[str, Command]] = frozenset()
    setting_call_history_operations: frozenset[str] = frozenset()
    default_prepare_lead_s: Decimal = DEFAULT_PREPARE_LEAD_S

    @staticmethod
    def source_storage_resolver(_declaration: Mapping[str, object]) -> CaptureStorage:
        """ADB 相机当前把拍摄正式产物固定保留在设备文件中。"""
        return CaptureStorage.DEVICE

    def __post_init__(self):
        if (not isinstance(self.default_prepare_lead_s, Decimal)
                or not self.default_prepare_lead_s.is_finite()
                or self.default_prepare_lead_s < 0):
            raise ValueError("相机默认准备提前量必须是有限非负秒数")
        if not set(self.session_setup_commands) <= self.session_once_commands:
            raise ValueError("会话首次准备命令必须同时声明为会话内固定设置")
        if (not isinstance(self.non_blocking_setting_commands, frozenset)
                or any(not isinstance(operation, str) or not operation
                       or not isinstance(command, tuple) or not command
                       or any(not isinstance(part, str) or not part for part in command)
                       for operation, command in self.non_blocking_setting_commands)):
            raise ValueError("非阻断设置策略必须按操作和完整命令声明")
        if (not isinstance(self.setting_call_history_operations, frozenset)
                or any(not isinstance(operation, str) or not operation
                       for operation in self.setting_call_history_operations)):
            raise ValueError("设置调用历史必须按操作声明")
        object.__setattr__(self, "task_factories", MappingProxyType(dict(self.task_factories)))
        object.__setattr__(self, "task_metadata", MappingProxyType(dict(self.task_metadata)))
        object.__setattr__(self, "commands", MappingProxyType(dict(self.commands)))

    def _has_evidence(self, type_, version, operation):
        try:
            return self.evidence.contract(type_, version).operation == operation
        except EvidenceError:
            return False

    def _has_evidence_shape(self, type_, version, operation, fields, identity_field):
        try:
            contract = self.evidence.contract(type_, version)
        except EvidenceError:
            return False
        return (contract.operation == operation and contract.fields == fields
                and contract.identity_field == identity_field)

    @property
    def declaration(self):
        files = self.file_tools is not None
        query_recovery = ("query" in self.recovery_operations
            and self._has_evidence_shape("query_returned", 1, "query", frozenset(), None)
            and self._has_evidence_shape("query_foreground_recovery", 1, "query",
                                         frozenset({"terminate_grace_s"}), None))
        file_query_supported = (files and callable(self.commands.get(CameraCall.FILE_QUERY))
            and query_recovery and self._has_evidence_shape(
                "file_presence", 1, "query",
                frozenset({"cleanup_item_id", "present"}), "cleanup_item_id"))
        delete_observation_supported = (
            self._has_evidence_shape("file_absent", 1, "delete",
                frozenset({"cleanup_item_id"}), "cleanup_item_id")
            or self._has_evidence_shape("file_delete_presence", 1, "delete",
                frozenset({"cleanup_item_id", "present"}), "cleanup_item_id"))
        delete_supported = (files and callable(self.commands.get(CameraCall.DELETE))
            and file_query_supported and "delete" in self.recovery_operations
            and self._has_evidence_shape("delete_returned", 1, "delete", frozenset(), None)
            and self._has_evidence_shape("delete_foreground_recovery", 1, "delete",
                                         frozenset({"terminate_grace_s"}), None)
            and delete_observation_supported)
        return DriverDeclaration(
            control_supported=(all(callable(self.commands.get(kind)) for kind in (CameraCall.SETTING, CameraCall.START))
                               and self._has_evidence("dispatch_prevented", 1, "control")),
            capture_preparation_supported=(callable(self.commands.get(CameraCall.SETTING))
                and self._has_evidence("dispatch_prevented", 1, "control")
                and self._has_evidence_shape(CAPTURE_PREPARATION_COMPLETED.type,
                    CAPTURE_PREPARATION_COMPLETED.version, "control",
                    CAPTURE_PREPARATION_COMPLETED.fields, "activity_id")),
            stop_supported=callable(self.commands.get(CameraCall.STOP)),
            query_supported=callable(self.commands.get(CameraCall.QUERY)),
            file_query_supported=file_query_supported,
            result_supported=((callable(self.commands.get(CameraCall.RESULT)) or callable(self.result_reader))
                              and self._has_evidence("result_files_listed", 2, "result")),
            read_supported=files, digest_supported=(files and callable(self.digest_timeout_s)
                                                    and self._has_evidence("file_digest", 1, "digest")),
            delete_supported=delete_supported,
            directory_supported=files,
            os_reboot_recovery_supported=self.os_reboot_recovery_supported,
            capture_read_parallel_supported=self.capture_read_parallel_supported,
            adb_foreground_recovery_operations=self.recovery_operations)

    def capabilities(self):
        declaration = self.declaration
        ready = (declaration.control_supported and declaration.result_supported
                 and declaration.directory_supported)
        capabilities = []
        for capability in candidate_capabilities(self.driver_id):
            factory = (self.task_factories.get(capability.parameter_type)
                       if ready and (capability.action_type != "camera_record" or declaration.stop_supported)
                       else None)
            metadata = self.task_metadata.get(capability.parameter_type) if callable(factory) else None
            task = (metadata if isinstance(metadata, CaptureTask) else
                    factory({"type": capability.parameter_type, "duration_s": 1})
                    if callable(factory) else None)
            task_description = cancellation_description(task) if isinstance(task, CaptureTask) else ""
            if metadata is not None and capability.action_type == "camera_timelapse":
                task_description = task_description.replace("录像启动后", "延时任务启动后")
            timing_description = ""
            if (capability.action_type == "camera_record" and isinstance(task, CaptureTask)
                    and task.start_return_meaning is StartReturn.SENT
                    and task.startup_margin_ms is not None):
                timing_description = ("开始命令正常响应只确认相机已接受命令；主机"
                    + (f"额外等待 {Decimal(task.startup_margin_ms) / 1000} 秒启动余量，再计满原目标时长，"
                       if task.startup_margin_ms else "从可靠命令确认时点起计满原目标时长，")
                    + "不据此记录实际首帧时刻。")
            if (metadata is not None and capability.action_type == "camera_timelapse"
                    and task.end_control is EndControl.DEVICE
                    and task.start_return_meaning is StartReturn.SENT
                    and task.completion_mode is CompletionMode.TIME_AND_OUTPUTS):
                timing_description = (
                    "设备按有限采集目标自行结束；成功启动响应只表示命令已发送。"
                    f"按目标时长等待后，再等待 {task.result_wait_margin_s} 秒核实产物；"
                    f"提前停止确认后等待 {task.file_completion_wait_s} 秒核实文件。")
            description = capability.description
            if callable(factory):
                if capability.action_type == "camera_timelapse":
                    description = (description
                        .replace("参数合法不表示已有完整命令映射；", "")
                        .replace("正常结束及文件写完依据待设备核实。", ""))
                elif capability.action_type == "camera_record":
                    description = description.replace("调用响应待设备核实。", "")
            capabilities.append(replace(capability, task_factory=factory,
                name=("普通录像参数" if capability.action_type == "camera_record" else "原生延时摄影参数")
                    if callable(factory) else capability.name,
                description=description + timing_description
                    + (f"{task_description}" if task_description else "")))
        return tuple(capabilities)

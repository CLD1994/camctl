"""具体相机的任务和响应契约；候选命令不自动提供可用能力。"""

from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Awaitable, Callable, Mapping, Any

from camctl.devices.adb_transport import DeviceCommand
from camctl.devices.drivers.registry import DriverStatus
from camctl.devices.evidence import EvidenceError, EvidenceRegistry
from camctl.devices.ports import ControlRequest, DriverDeclaration, DeviceCallResult
from camctl.devices.tasks import CaptureTaskFactory
from .commands import CameraModel
from .definitions import candidate_capabilities
from .filesystem import ShellFileTools


class CameraCall(StrEnum):
    SETTING = "setting"
    START = "start"
    STOP = "stop"
    RESULT = "result"
    QUERY = "query"
    DELETE = "delete"


CommandFactory = Callable[[ControlRequest, str, tuple[str, ...] | None, int | None], DeviceCommand]


@dataclass(frozen=True)
class CameraContract:
    driver_id: CameraModel
    task_factories: Mapping[str, CaptureTaskFactory] = field(default_factory=dict)
    commands: Mapping[CameraCall, CommandFactory] = field(default_factory=dict)
    file_tools: ShellFileTools | None = None
    digest_timeout_s: Callable[[int], Decimal] | None = None
    evidence: EvidenceRegistry = field(default_factory=lambda: EvidenceRegistry(()))
    status: DriverStatus = DriverStatus.DEVICE_VERIFICATION_PENDING
    capture_read_parallel_supported: bool = False
    recovery_operations: frozenset[str] = frozenset()
    result_reader: Callable[[Any, ControlRequest, int], Awaitable[DeviceCallResult]] | None = None

    def __post_init__(self):
        object.__setattr__(self, "task_factories", MappingProxyType(dict(self.task_factories)))
        object.__setattr__(self, "commands", MappingProxyType(dict(self.commands)))

    def _has_evidence(self, type_, version, operation):
        try:
            return self.evidence.contract(type_, version).operation == operation
        except EvidenceError:
            return False

    @property
    def declaration(self):
        files = self.file_tools is not None
        return DriverDeclaration(
            control_supported=(all(callable(self.commands.get(kind)) for kind in (CameraCall.SETTING, CameraCall.START))
                               and self._has_evidence("dispatch_prevented", 1, "control")),
            stop_supported=callable(self.commands.get(CameraCall.STOP)),
            query_supported=callable(self.commands.get(CameraCall.QUERY)),
            result_supported=((callable(self.commands.get(CameraCall.RESULT)) or callable(self.result_reader))
                              and self._has_evidence("result_files_listed", 2, "result")),
            read_supported=files, digest_supported=(files and callable(self.digest_timeout_s)
                                                    and self._has_evidence("file_digest", 1, "digest")),
            delete_supported=files and callable(self.commands.get(CameraCall.DELETE)),
            directory_supported=files,
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
            capabilities.append(replace(capability, task_factory=factory,
                name=("普通录像参数" if capability.action_type == "camera_record" else "原生延时摄影参数")
                    if callable(factory) else capability.name,
                description=((capability.description.replace("调用响应待设备核实。", "")
                              if capability.action_type == "camera_record" else
                              capability.description.replace(
                                  "仅接受资料给出的完整组合；正常结束及文件写完依据待设备核实。",
                                  "间隔、持续时间、产物和曝光须符合 Schema 列出的完整组合。"))
                             if callable(factory) else capability.description)))
        return tuple(capabilities)

"""按原设备绑定组合设置、实际控制及独立文件访问。"""

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
import shlex
from types import SimpleNamespace

from camctl.devices.adb_transport import invoke
from camctl.devices.bindings import DeviceBinding, DeviceConfigurationError, BindingStatus, check_binding
from camctl.devices.drivers.registry import CapabilityNotDeclaredError
from camctl.devices.evidence import DeviceObservation, validate_observation
from camctl.devices.file_identity import FileIdentity
from camctl.devices.ports import DeviceCallResult
from camctl.operations.models import AttemptStatus, CallOutcome, EffectState, ErrorValue, EvidenceValue, Settlement, SettlementBasis
from camctl.operations.validation import validate_outcome
from .commands import settings_for, start_for, stop_for
from .contracts import CameraCall
from .filesystem import AdbFilesystem
from .transport import shell_argv


class _NeverStop:
    async def requested(self):
        await asyncio.Future()


class AdbCameraDriver:
    def __init__(self, contract, devices, transport, *, terminate_grace_s, monotonic_ns):
        self.contract = contract
        self.declaration = contract.declaration
        self.devices = devices
        self.transport = transport
        self.terminate_grace_s = terminate_grace_s
        self.monotonic_ns = monotonic_ns

    def _serial(self, binding):
        result = check_binding(binding, SimpleNamespace(devices=self.devices))
        if binding.driver_id != self.contract.driver_id or result.status is not BindingStatus.MATCHED:
            raise DeviceConfigurationError("原相机绑定在本次配置中不可用")
        declaration = self.devices[binding.device_id]
        adb = declaration.get("adb")
        serial = adb.get("serial") if isinstance(adb, Mapping) else None
        try:
            shell_argv(serial, "true")
        except ValueError as error:
            raise DeviceConfigurationError(str(error)) from error
        return serial

    def _request(self, request, operation):
        if request.ticket is None or request.ticket.operation != operation:
            raise ValueError("相机调用必须携带匹配的原尝试")
        if (not isinstance(request.timeout_s, Decimal) or not request.timeout_s.is_finite()
                or request.timeout_s <= 0):
            raise ValueError("相机调用必须携带本次有限正期限")
        return self._serial(request.binding)

    async def _invoke(self, request, kind, serial, argv=None, batch=None, *, timeout_s=None):
        factory = self.contract.commands.get(kind)
        if not callable(factory):
            raise CapabilityNotDeclaredError(f"驱动未声明 {kind.value} 响应契约")
        command = factory(request, serial, argv, batch)
        if (command.binding != request.binding or command.operation != request.ticket.operation
                or command.argv[:4] != ("adb", "-s", serial, "exec-out")
                or (argv is not None and command.argv != argv)
                or command.evidence is not self.contract.evidence):
            raise ValueError("命令映射改变原绑定、操作、命令或证据登记")
        command = replace(command, timeout_s=request.timeout_s if timeout_s is None else timeout_s,
                          terminate_grace_s=self.terminate_grace_s)
        return await invoke(command, request.ticket, self.transport)

    def _prevented(self, request, reason, previous):
        error = ErrorValue(reason, "dispatch")
        if previous is None:
            outcome = CallOutcome(status=AttemptStatus.FAILED, error=error, effect=EffectState.NO_EFFECT,
                settlement=Settlement(SettlementBasis.NOT_DISPATCHED, EvidenceValue("dispatch_prevented", 1, {})))
        else:
            outcome = replace(previous, status=AttemptStatus.FAILED, error=error, effect=EffectState.NO_EFFECT)
        validate_outcome(request.ticket, outcome, self.contract.evidence)
        return DeviceCallResult.from_outcome(outcome)

    async def control(self, request):
        serial = self._request(request, "control")
        if not self.declaration.control_supported:
            raise CapabilityNotDeclaredError("驱动未声明完整控制契约")
        if not callable(request.dispatch_check):
            raise ValueError("多步相机启动需要框架的派发资格检查")
        action_type = {"start_recording": "camera_record", "start_timelapse": "camera_timelapse"}.get(request.operation)
        if action_type is None:
            raise ValueError("没有该相机启动操作")
        began_ns = self.monotonic_ns()
        settings = settings_for(self.contract.driver_id, action_type, request.params)
        commands = (*((CameraCall.SETTING, command) for command in settings),
                    (CameraCall.START, start_for(self.contract.driver_id, action_type)))
        previous = None
        for kind, command in commands:
            argv = shell_argv(serial, shlex.join(command))
            reason = request.dispatch_check()
            if reason is not None:
                return self._prevented(request, reason, previous)
            remaining = request.timeout_s - Decimal(self.monotonic_ns() - began_ns) / Decimal(1_000_000_000)
            if remaining <= 0:
                return self._prevented(request, "start_preparation_timeout", previous)
            outcome = await self._invoke(request, kind, serial, argv, timeout_s=remaining)
            if kind is CameraCall.START:
                return DeviceCallResult.from_outcome(outcome)
            previous = outcome
            if (outcome.status is not AttemptStatus.SUCCEEDED or outcome.error is not None
                    or outcome.effect is not EffectState.CONFIRMED):
                failed = replace(outcome, status=AttemptStatus.FAILED, effect=EffectState.NO_EFFECT,
                    error=outcome.error or ErrorValue("camera_settings_unconfirmed", "device_settings"))
                validate_outcome(request.ticket, failed, self.contract.evidence)
                return DeviceCallResult.from_outcome(failed)
        raise AssertionError("相机启动组合没有实际启动步骤")

    async def stop(self, request):
        serial = self._request(request, "stop")
        action_type = {"stop_recording": "camera_record", "stop_timelapse": "camera_timelapse"}.get(request.operation)
        if action_type is None:
            raise ValueError("没有该相机停止操作")
        argv = shell_argv(serial, shlex.join(stop_for(self.contract.driver_id, action_type)))
        return DeviceCallResult.from_outcome(await self._invoke(request, CameraCall.STOP, serial, argv))

    async def list_results(self, request, batch):
        serial = self._request(request, "result")
        if type(batch) is not int or not 1 <= batch <= 128:
            raise ValueError("相机结果批量必须在 1 到 128 之间")
        return DeviceCallResult.from_outcome(await self._invoke(request, CameraCall.RESULT, serial, batch=batch))

    async def query_state(self, request):
        serial = self._request(request, "query")
        return DeviceCallResult.from_outcome(await self._invoke(request, CameraCall.QUERY, serial))

    def _filesystem(self, binding):
        serial = self._serial(binding)
        if self.contract.file_tools is None:
            raise CapabilityNotDeclaredError("相机文件工具契约尚未提供")
        return AdbFilesystem(binding, serial, self.transport, tools=self.contract.file_tools,
                             terminate_grace_s=self.terminate_grace_s)

    async def read_directory(self, request, *, stop):
        return await self._filesystem(request.binding).read_directory(request, stop=stop)

    async def open_read(self, source, offset, ticket, *, idle_timeout_s):
        identity = FileIdentity.from_json(dict(source.locator))
        return await self._filesystem(identity.binding).open_read(source, offset, ticket, idle_timeout_s=idle_timeout_s)

    async def digest(self, request):
        if not self.declaration.digest_supported:
            raise CapabilityNotDeclaredError("相机整片摘要契约尚未提供")
        identity = FileIdentity.from_json(dict(request.params["locator"]))
        if identity.binding != request.binding:
            raise ValueError("源摘要定位改变原文件绑定")
        filesystem = self._filesystem(request.binding)
        # 整片摘要期限由驱动按可靠源长度声明，不使用元数据查询期限。
        timeout = self.contract.digest_timeout_s(request.params["size_bytes"])
        if not isinstance(timeout, Decimal) or not timeout.is_finite() or timeout <= 0:
            raise ValueError("驱动的整片摘要调用期限必须是有限正秒数")
        result = await filesystem.source_digest(identity, timeout_s=timeout, stop=_NeverStop())
        if result.error is not None:
            return DeviceCallResult((), {"code": result.error.code, "stage": result.error.stage,
                                       "details": dict(result.error.details)})
        observation = DeviceObservation("file_digest", 1, {"file_id": request.params["file_id"], "sha256": result.value})
        validate_observation(observation, self.contract.evidence.contract(observation.type, observation.version),
                             expected_identity=request.params["file_id"])
        return DeviceCallResult((observation,), None)

    async def delete(self, request):
        serial = self._request(request, "delete")
        identity = FileIdentity.from_json(dict(request.params["locator"]))
        if identity.binding != request.binding:
            raise ValueError("删除定位改变原文件绑定")
        filesystem = self._filesystem(request.binding)
        argv = shell_argv(serial, filesystem.tools.delete_script(identity.path))
        return DeviceCallResult.from_outcome(await self._invoke(request, CameraCall.DELETE, serial, argv))

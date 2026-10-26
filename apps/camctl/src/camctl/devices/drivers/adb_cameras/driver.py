"""按原设备绑定组合设置、实际控制及独立文件访问。"""

import asyncio
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import shlex
from types import SimpleNamespace

from camctl.devices.adb_transport import invoke, ManagedTransport
from camctl.devices.bindings import DeviceConfigurationError, BindingStatus, check_binding
from camctl.devices.capture_preparation import PreparedCaptureCache
from camctl.devices.drivers.registry import CapabilityNotDeclaredError
from camctl.devices.evidence import DeviceObservation, validate_observation
from camctl.devices.file_identity import FileIdentity
from camctl.devices.ports import DeviceCallResult
from camctl.logging_runtime.performance import measure_performance
from camctl.logging_runtime.diagnostics import record_error
from camctl.operations.models import (AttemptStatus, CallOutcome, EffectState, ErrorValue,
    EvidenceValue, Settlement, SettlementBasis)
from camctl.operations.process import RawToolOutcome, ToolStartError
from camctl.operations.result_format import error_document, result_document
from camctl.operations.validation import validate_outcome
from camctl.operations.owned_calls import current_call_scope
from camctl.operations.diagnostic_controls import is_capture_control_error
from .commands import settings_from_effective_params, start_for, stop_for
from .contracts import CAPTURE_PREPARATION_COMPLETED, CameraCall
from .filesystem import AdbFilesystem
from .transport import shell_argv
from .software_recovery import SoftwareRecoveryDriver


class _NeverStop:
    async def requested(self):
        await asyncio.Future()


class _RawOutcomeCapture:
    """记录同一次受管传输实际返回的原始结果，不创建第二次调用。"""

    def __init__(self, transport: ManagedTransport, captured: list[RawToolOutcome]):
        self.transport = transport
        self.captured = captured

    async def run(self, spec, stop):
        raw = await self.transport.run(spec, stop)
        self.captured.append(raw)
        return raw


class AdbCameraDriver(SoftwareRecoveryDriver):
    def __init__(self, contract, devices, transport, *, terminate_grace_s, monotonic_ns):
        self.contract = contract
        self.declaration = contract.declaration
        self.devices = devices
        self.transport = transport
        self.terminate_grace_s = terminate_grace_s
        self.monotonic_ns = monotonic_ns
        self._configured_settings = set()
        self._recovery_devices = set()
        self._prepared_captures = PreparedCaptureCache()

    @property
    def default_prepare_lead_s(self):
        return self.contract.default_prepare_lead_s

    def begin_session(self):
        """新执行会话只清空内存记录，相机设置仍在拍摄准备时按需执行。"""
        self._configured_settings.clear()
        self._recovery_devices.clear()
        self._prepared_captures.clear()

    def _clear_prepared_capture(self, binding):
        self._prepared_captures.clear(binding)

    def _prepared_capture_matches(self, request, serial):
        return self._prepared_captures.matches(request, serial)

    def prepared_capture_matches(self, request):
        """只检查本会话内存与本地绑定，不查询或设置设备。"""
        try:
            serial = self._serial(request.binding)
        except (DeviceConfigurationError, RuntimeError):
            return False
        return self._prepared_capture_matches(request, serial)

    def _serial(self, binding):
        self._check_recovery_access(binding)
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

    async def _invoke(self, request, kind, serial, argv=None, batch=None, *, timeout_s=None,
                      raw_capture: list[RawToolOutcome] | None = None):
        factory = self.contract.commands.get(kind)
        if not callable(factory):
            raise CapabilityNotDeclaredError(f"驱动未声明 {kind.value} 响应契约")
        command = factory(request, serial, argv, batch)
        if (command.binding != request.binding or command.operation != request.ticket.operation
                or command.argv[:5] != ("adb", "-s", serial, "shell", "-T")
                or (argv is not None and command.argv != argv)
                or command.evidence is not self.contract.evidence):
            raise ValueError("命令映射改变原绑定、操作、命令或证据登记")
        command = replace(command, timeout_s=request.timeout_s if timeout_s is None else timeout_s,
                          terminate_grace_s=self.terminate_grace_s)
        transport = self.transport if raw_capture is None else _RawOutcomeCapture(self.transport, raw_capture)
        return await invoke(command, request.ticket, transport)

    def _tool_start_failed(self, request, failure):
        outcome = CallOutcome(status=AttemptStatus.FAILED,
            error=ErrorValue("tool_start_failed", "transport", {"message": str(failure)}),
            effect=EffectState.NO_EFFECT,
            settlement=Settlement(SettlementBasis.NOT_DISPATCHED,
                EvidenceValue("tool_not_started", 1, {})))
        validate_outcome(request.ticket, outcome, self.contract.evidence)
        return outcome

    def _call_record(self, command, outcome, raw):
        return {
            "command": list(command),
            "status": outcome.status.value,
            "effect": outcome.effect.value,
            "error": error_document(outcome.error),
            "result_document": result_document(outcome),
            "stdout_hex": None if raw is None or raw.output is None else raw.output.hex(),
            "stderr_hex": None if raw is None or raw.stderr is None else raw.stderr.hex(),
        }

    def _with_attached_calls(self, request, outcome, calls):
        if not calls:
            validate_outcome(request.ticket, outcome, self.contract.evidence)
            return DeviceCallResult.from_outcome(outcome)
        settlement = outcome.settlement
        if settlement is None:
            raise ValueError("已捕获光圈调用时，外层结果必须保留收场依据")
        evidence = settlement.evidence
        if evidence.version == 1 and evidence.type in {
                "control_returned", "control_foreground_recovery", "tool_not_started"}:
            type_, version = evidence.type, 2
            data = dict(evidence.data)
            data["calls"] = calls
        else:
            raise ValueError("Action6 设置调用历史无法附加到未登记的外层收场依据")
        outcome = replace(outcome, settlement=Settlement(settlement.basis, EvidenceValue(type_, version, data)))
        validate_outcome(request.ticket, outcome, self.contract.evidence)
        failed = [call for call in calls if not is_capture_control_error(call["error"])
                  and (call["error"] is not None or call["status"] == AttemptStatus.FAILED.value)]
        if (outcome.error is None or is_capture_control_error(outcome.error)) and failed:
            record_error({"setting_calls": failed}, stage="device_settings",
                context={"binding": request.binding, "ticket": request.ticket},
                details={"outcome": outcome})
        return DeviceCallResult.from_outcome(outcome)

    def _prevented(self, request, reason, previous, attached_calls=()):
        error = ErrorValue(reason, "dispatch")
        if previous is None:
            outcome = CallOutcome(status=AttemptStatus.FAILED, error=error, effect=EffectState.NO_EFFECT,
                settlement=Settlement(SettlementBasis.NOT_DISPATCHED, EvidenceValue("dispatch_prevented", 1, {})))
        else:
            outcome = replace(previous, status=AttemptStatus.FAILED, error=error, effect=EffectState.NO_EFFECT,
                observations=() if attached_calls else previous.observations,
                call_info=None if attached_calls else previous.call_info)
        if attached_calls:
            return self._with_attached_calls(request, outcome, attached_calls)
        validate_outcome(request.ticket, outcome, self.contract.evidence)
        return DeviceCallResult.from_outcome(outcome)

    async def control(self, request):
        return await self._capture_control(request, prepare_only=False)

    async def prepare_capture(self, request):
        """执行原设置组合；成功只保存内存凭据，不派发 START。"""
        return await self._capture_control(request, prepare_only=True)

    async def _capture_control(self, request, *, prepare_only):
        serial = self._request(request, "control")
        if prepare_only and not self.declaration.capture_preparation_supported:
            raise CapabilityNotDeclaredError("驱动未声明独立拍摄准备契约")
        if not prepare_only and not self.declaration.control_supported:
            raise CapabilityNotDeclaredError("驱动未声明完整控制契约")
        if not callable(request.dispatch_check):
            raise ValueError("多步相机启动需要框架的派发资格检查")
        action_type = {"start_recording": "camera_record", "start_timelapse": "camera_timelapse"}.get(request.operation)
        if action_type is None:
            raise ValueError("没有该相机启动操作")
        prepared = not prepare_only and request.prepared_by is not None
        if prepared and not self._prepared_capture_matches(request, serial):
            raise ValueError("实际启动缺少与原设备、活动、参数及准备尝试匹配的会话凭据")
        if prepare_only and request.prepared_by is not None:
            raise ValueError("独立设置准备不能携带已有准备凭据")
        began_ns = self.monotonic_ns()
        params = deepcopy(dict(request.params)) if prepare_only else None
        if prepared:
            commands = ((CameraCall.START, start_for(self.contract.driver_id, action_type)),)
        else:
            settings = settings_from_effective_params(self.contract.driver_id, action_type, request.params)
            self._clear_prepared_capture(request.binding)
            commands = tuple((CameraCall.SETTING, command)
                for command in (*self.contract.session_setup_commands, *settings))
            if not prepare_only:
                commands += ((CameraCall.START, start_for(self.contract.driver_id, action_type)),)
        previous = None
        preparation_basis = None
        attached_calls = []
        for kind, command in commands:
            session_key = (request.binding, serial, command)
            once = kind is CameraCall.SETTING and command in self.contract.session_once_commands
            if once and session_key in self._configured_settings:
                continue
            argv = shell_argv(serial, shlex.join(command))
            reason = await request.dispatch_check()
            if reason is not None:
                return self._prevented(request, reason, previous, attached_calls)
            scope = current_call_scope()
            if scope is not None and scope.interrupted is not None:
                return self._prevented(request, "transport_cancelled", previous, attached_calls)
            remaining = request.timeout_s - Decimal(self.monotonic_ns() - began_ns) / Decimal(1_000_000_000)
            if remaining <= 0:
                return self._prevented(request, "start_preparation_timeout", previous, attached_calls)
            is_non_blocking_aperture = (kind is CameraCall.SETTING
                and (request.operation, command) in self.contract.non_blocking_setting_commands)
            record_setting_call = (kind is CameraCall.SETTING
                and request.operation in self.contract.setting_call_history_operations)
            raw_capture = [] if record_setting_call else None
            try:
                with measure_performance(
                        "camera_start_command" if kind is CameraCall.START else "camera_setting",
                        device_id=request.binding.device_id,
                        activity_id=request.ticket.target_id,
                        run_id=request.ticket.run_id,
                        attempt_id=request.ticket.attempt_id,
                        command=list(command), timeout_s=str(remaining)):
                    outcome = await self._invoke(request, kind, serial, argv, timeout_s=remaining,
                                                 raw_capture=raw_capture)
            except ToolStartError as error:
                if (request.operation not in self.contract.setting_call_history_operations
                        or (not record_setting_call and kind is not CameraCall.START)):
                    raise
                outcome = self._tool_start_failed(request, error)
            if outcome.error is not None:
                outcome = replace(outcome, error=ErrorValue(
                    outcome.error.code, outcome.error.stage,
                    {**outcome.error.details, "command": list(command),
                     "timeout_s": str(remaining)}))
            if record_setting_call:
                raw = raw_capture[0] if raw_capture else None
                attached_calls.append(self._call_record(command, outcome, raw))
            if kind is CameraCall.START:
                return self._with_attached_calls(request, outcome, attached_calls)
            previous = outcome
            if is_non_blocking_aperture:
                scope = current_call_scope()
                if scope is not None and scope.interrupted is not None:
                    interrupted = replace(previous, status=AttemptStatus.FAILED,
                        effect=EffectState.NO_EFFECT,
                        error=ErrorValue("transport_cancelled", "transport"),
                        observations=() if attached_calls else previous.observations,
                        call_info=None if attached_calls else previous.call_info)
                    return self._with_attached_calls(request, interrupted, attached_calls)
                continue
            if (outcome.status is not AttemptStatus.SUCCEEDED or outcome.error is not None
                    or outcome.effect is not EffectState.CONFIRMED):
                failed = replace(outcome, status=AttemptStatus.FAILED, effect=EffectState.NO_EFFECT,
                    error=outcome.error or ErrorValue("camera_settings_unconfirmed", "device_settings"))
                return self._with_attached_calls(request, failed, attached_calls)
            if once:
                self._configured_settings.add(session_key)
            if prepare_only:
                preparation_basis = outcome
            scope = current_call_scope()
            if scope is not None and scope.interrupted is not None:
                # 当前设置实际已返回，但原等待取消后不派发下一设置或 START。
                failed = replace(outcome, status=AttemptStatus.FAILED, effect=EffectState.NO_EFFECT,
                    error=ErrorValue("transport_cancelled", "transport"),
                    observations=() if attached_calls else outcome.observations,
                    call_info=None if attached_calls else outcome.call_info)
                return self._with_attached_calls(request, failed, attached_calls)
        if not prepare_only or previous is None or preparation_basis is None:
            raise AssertionError("相机调用组合缺少实际设置或启动步骤")
        reason = await request.dispatch_check()
        if reason is not None:
            return self._prevented(request, reason, previous, attached_calls)
        scope = current_call_scope()
        if scope is not None and scope.interrupted is not None:
            return self._prevented(request, "transport_cancelled", previous, attached_calls)
        if request.timeout_s - Decimal(self.monotonic_ns() - began_ns) / Decimal(1_000_000_000) <= 0:
            return self._prevented(request, "start_preparation_timeout", previous, attached_calls)
        # 组合完成由全部必需设置的可靠结果承载。非阻断调用的原失败
        # 保留在附属历史中，不把它的未派发依据改写为成功调用。
        completed = replace(preparation_basis, observations=(DeviceObservation(
            CAPTURE_PREPARATION_COMPLETED.type, CAPTURE_PREPARATION_COMPLETED.version,
            {"activity_id": request.ticket.target_id}),))
        result = self._with_attached_calls(request, completed, attached_calls)
        self._prepared_captures.remember(replace(request, params=params), serial)
        return result

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
        if callable(self.contract.result_reader):
            result = await self.contract.result_reader(self, request, batch)
            if result.outcome is None:
                raise ValueError("结果读取缺少完整实际调用结果")
            validate_outcome(request.ticket, result.outcome, self.contract.evidence)
            return result
        return DeviceCallResult.from_outcome(await self._invoke(request, CameraCall.RESULT, serial, batch=batch))

    async def query_state(self, request):
        serial = self._request(request, "query")
        return DeviceCallResult.from_outcome(await self._invoke(request, CameraCall.QUERY, serial))

    async def query_file(self, request):
        """按原文件身份执行 FILE_QUERY，底层 QUERY 票据与状态查询一致。"""
        serial = self._request(request, "query")
        identity = FileIdentity.from_source(
            request.params["identity_key"], request.params["locator"])
        if identity.binding != request.binding:
            raise ValueError("文件查询定位改变原设备绑定")
        if not self.declaration.file_query_supported:
            raise CapabilityNotDeclaredError("相机未声明独立文件查询契约")
        self._filesystem(request.binding)
        return DeviceCallResult.from_outcome(
            await self._invoke(request, CameraCall.FILE_QUERY, serial))

    def _filesystem(self, binding):
        serial = self._serial(binding)
        if self.contract.file_tools is None:
            raise CapabilityNotDeclaredError("相机文件工具契约尚未提供")
        return AdbFilesystem(binding, serial, self.transport, tools=self.contract.file_tools,
                             terminate_grace_s=self.terminate_grace_s)

    async def read_directory(self, request, *, stop):
        return await self._filesystem(request.binding).read_directory(request, stop=stop)

    def directory_scan(self, request):
        return self._filesystem(request.binding).directory_scan(request)

    async def open_read(self, source, offset, ticket, *, idle_timeout_s):
        identity = FileIdentity.from_source(source.file_id, source.locator)
        return await self._filesystem(identity.binding).open_read(source, offset, ticket, idle_timeout_s=idle_timeout_s)

    async def digest(self, request):
        if not self.declaration.digest_supported:
            raise CapabilityNotDeclaredError("相机整片摘要契约尚未提供")
        identity = FileIdentity.from_source(request.params["identity_key"], request.params["locator"])
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
                                       "details": dict(result.error.details)},
                raw_outcome=result.outcome, diagnostic_error=result.failure)
        observation = DeviceObservation("file_digest", 1, {"file_id": request.params["file_id"], "sha256": result.value})
        validate_observation(observation, self.contract.evidence.contract(observation.type, observation.version),
                             expected_identity=request.params["file_id"])
        return DeviceCallResult((observation,), None, raw_outcome=result.outcome)

    async def delete(self, request):
        serial = self._request(request, "delete")
        identity = FileIdentity.from_source(request.params["identity_key"], request.params["locator"])
        if identity.binding != request.binding:
            raise ValueError("删除定位改变原文件绑定")
        self._filesystem(request.binding)
        return DeviceCallResult.from_outcome(await self._invoke(request, CameraCall.DELETE, serial))

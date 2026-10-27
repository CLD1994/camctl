"""CFV 100C 的受管单张拍摄调用。"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Callable

from camctl.contracts.values import parse_object_id
from camctl.devices.bindings import BindingStatus, DeviceConfigurationError, check_binding
from camctl.devices.capture_preparation import (
    CAPTURE_PREPARATION_COMPLETED, DEFAULT_PREPARE_LEAD_S, PreparedCaptureCache,
)
from camctl.devices.evidence import (
    DeviceObservation,
    EvidenceContract,
    EvidenceRegistry,
)
from camctl.devices.ports import ControlRequest, DeviceCallResult
from camctl.devices.start_entry import ENTERED, PREVENTED, entered_observation
from camctl.devices.read_session import ReadSession, SourceFile, ReadPreparationError, ReadPreparationSettlement
from camctl.host_files.downloads import DownloadWorkspace
from camctl.host_files.capture_sources import (
    CaptureFileError,
    CaptureFileSpec,
    CapturedArtifact,
    prepare_capture_workspace,
    verify_capture_files,
)
from camctl.logging_runtime.events import log_context, measure_stage
from camctl.operations.models import (
    AttemptStatus,
    CallInfo,
    CallOutcome,
    EffectState,
    ErrorValue,
    EvidenceValue,
    Settlement,
    SettlementBasis,
)
from camctl.operations.owned_calls import current_call_scope
from camctl.operations.error_diagnostics import DiagnosticError, exception_diagnostic
from camctl.operations.process import (
    RawToolOutcome,
    StopSignal,
    ToolCallCancelled,
    ToolSpec,
    ToolStartError,
    execute_tool,
)

from camctl.operations.tool_diagnostics import (
    ToolDiagnostics, ToolCallOutput, ToolStream, ToolStreamState,
)
from .commands import PhotoPhase, PhotoStep, PHOTO_START_CALL_LIMIT, photo_start_commands
from .configuration import CameraConfig, read_camera_config
from .definitions import DRIVER_DECLARATION, DRIVER_ID
from .parameters import PhotoSettings, parse_photo_settings
from .responses import (
    CaptureResponse,
    CliParseError,
    parse_capture,
    parse_devices,
    parse_serial,
    doctor_passed,
)
from .files import CameraFileIdentity

__all__ = ["AvailabilityCall", "AvailabilityCancelled", "AvailabilityResult", "EVIDENCE", "HasselbladDriver"]


_PHOTO_TAKEN = EvidenceContract(
    "photo_taken",
    1,
    "control",
    frozenset({"activity_id", "original", "preview", "steps"}),
    identity_field="activity_id",
)
_PHOTO_PREPARATION_STEPS = EvidenceContract(
    "photo_preparation_steps", 1, "control", frozenset({"activity_id", "steps"}),
    identity_field="activity_id", max_diagnostic_calls=PHOTO_START_CALL_LIMIT,
)
_OPERATION_RETURNED = EvidenceContract(
    "operation_returned", 1, "control", frozenset({"terminate_grace_s"}),
    max_diagnostic_calls=PHOTO_START_CALL_LIMIT
)
_DISPATCH_PREVENTED = EvidenceContract(
    "dispatch_prevented", 1, "control", frozenset(),
    max_diagnostic_calls=PHOTO_START_CALL_LIMIT
)
_AVAILABILITY = EvidenceContract(
    "device_availability", 1, "availability",
    frozenset({"check_id", "expected_serial", "expected_product_id", "observed_serial", "steps",
               "started_monotonic_ns", "finished_monotonic_ns"}),
    identity_field="check_id",
)
_AVAILABILITY_RETURNED = EvidenceContract(
    "availability_returned", 1, "availability", frozenset(), max_diagnostic_calls=3,
)
_AVAILABILITY_NOT_DISPATCHED = EvidenceContract(
    "availability_not_dispatched", 1, "availability", frozenset(), max_diagnostic_calls=3,
)
EVIDENCE = EvidenceRegistry((
    _PHOTO_TAKEN, _OPERATION_RETURNED, _DISPATCH_PREVENTED, ENTERED, PREVENTED,
    CAPTURE_PREPARATION_COMPLETED, _PHOTO_PREPARATION_STEPS,
    EvidenceContract("read_returned", 1, "read", frozenset()),
    EvidenceContract("local_read_recovery", 1, "read", frozenset()),
    EvidenceContract("local_control_recovery", 1, "control", frozenset()),
    EvidenceContract("local_availability_recovery", 1, "availability", frozenset()),
    _AVAILABILITY, _AVAILABILITY_RETURNED, _AVAILABILITY_NOT_DISPATCHED,
))

_VENDOR_ID = 0x2756
_NANOSECONDS_PER_SECOND = Decimal(1_000_000_000)
READ_PREPARATION_TIMEOUT_S = Decimal("180")


@dataclass(frozen=True)
class AvailabilityCall:
    name: str
    returned_monotonic_ns: int
    outcome: RawToolOutcome | None = None
    error: BaseException | None = None


@dataclass(frozen=True)
class AvailabilityResult:
    expected_serial: str
    expected_product_id: int
    observed_serial: str | None
    available: bool
    reason: str | None
    calls: tuple[AvailabilityCall, ...]
    settled: bool
    started_monotonic_ns: int
    finished_monotonic_ns: int

    def to_device_result(self, check_id: str) -> DeviceCallResult:
        """将已收场事实交给原结果拥有者；未知收场不能伪造成票据。"""
        parse_object_id(check_id)
        if not self.settled:
            raise ValueError("检查实际收场尚未确认")
        steps = []
        diagnostics = []
        for index, call in enumerate(self.calls, 1):
            raw = call.outcome
            error = None if raw is None else raw.error
            if error is not None or call.error is not None:
                error = {"tool_error": error, "exception": exception_diagnostic(call.error)}
            steps.append({
                "name": call.name,
                "returned_monotonic_ns": call.returned_monotonic_ns,
                "exit": None if raw is None or raw.exit is None else {
                    "exit_code": raw.exit.exit_code, "signal": raw.exit.signal},
                "error": error,
                "used_grace_s": None if raw is None or raw.used_grace_s is None else str(raw.used_grace_s),
                "signal_failures": [] if raw is None else [
                    {"stage": failure.stage.value, "message": failure.message}
                    for failure in raw.signal_failures],
                "output_failure": None if raw is None else raw.output_failure,
            })
            complete = (raw is not None and raw.output is not None and raw.stderr is not None
                        and raw.output_failure is None)

            def stream(data):
                state = ToolStreamState.UNAVAILABLE if data is None else (
                    ToolStreamState.COMPLETE if complete else ToolStreamState.UNCONFIRMED)
                return ToolStream(state, data)

            diagnostics.append(ToolCallOutput(index, call.name,
                stream(None if raw is None else raw.output), stream(None if raw is None else raw.stderr)))
        observation = DeviceObservation(_AVAILABILITY.type, _AVAILABILITY.version, {
            "check_id": check_id, "expected_serial": self.expected_serial,
            "expected_product_id": self.expected_product_id,
            "observed_serial": self.observed_serial, "steps": steps,
            "started_monotonic_ns": self.started_monotonic_ns,
            "finished_monotonic_ns": self.finished_monotonic_ns,
        })
        error = None if self.available else ErrorValue("device_error", "availability", {
            "reason": self.reason, "step": None if not self.calls else self.calls[-1].name,
        })
        dispatched = any(call.outcome is not None for call in self.calls)
        settlement_contract = _AVAILABILITY_RETURNED if dispatched else _AVAILABILITY_NOT_DISPATCHED
        outcome = CallOutcome(
            status=AttemptStatus.SUCCEEDED if self.available else AttemptStatus.FAILED,
            error=error, effect=EffectState.NO_EFFECT,
            settlement=Settlement(SettlementBasis.OBSERVED if dispatched else SettlementBasis.NOT_DISPATCHED,
                                  EvidenceValue.of(settlement_contract)),
            observations=(observation,), diagnostics=ToolDiagnostics(3, tuple(diagnostics)),
        )
        diagnostic_error = next((call.error for call in self.calls if call.error is not None), None)
        return DeviceCallResult(outcome.observations, DeviceCallResult._error_view(outcome), outcome,
                                diagnostic_error=diagnostic_error)


class AvailabilityCancelled(asyncio.CancelledError):
    """取消仍向调用者传播，同时交付本次检查已经取得的事实。"""

    def __init__(self, result: AvailabilityResult):
        super().__init__("设备可用性检查被取消")
        self.result = result


class CameraReadError(ReadPreparationError, DiagnosticError):
    """下载或身份核对失败；保留各次受管调用与实际收场细节。"""

    def __init__(self, reason, steps, *, causes=(), settlement=ReadPreparationSettlement.SETTLED):
        self.reason, self.steps = reason, tuple(dict(step) for step in steps)
        self.causes = tuple(causes)
        # OSError 的 repr 不包含 filename；异常组的 str 则不包含各子错误。
        diagnostics = tuple(f"{type(cause).__name__}: {cause}" if isinstance(cause, OSError)
                            else repr(cause) for cause in self.causes)
        super().__init__(f"相机文件读取失败: {reason}; steps={self.steps!r}; causes={diagnostics!r}",
                         settlement=settlement)
        self.diagnostic_details = {
            "reason": reason,
            "steps": [deepcopy(step) for step in self.steps],
            "causes": [exception_diagnostic(cause) for cause in self.causes],
            "settlement": settlement.value,
        }


class PhysicalIdentityMismatch(ValueError):
    """原文件的固定机身与本次合法配置不一致。"""

    reason = "physical_identity_mismatch"

    def __init__(self, binding):
        self.binding = binding
        super().__init__("相机源的原机身与本次配置不一致")


class _NeverStop:
    """hb_cli 的 stop 子命令会关机，取消只能由受管进程边界处理。"""

    async def requested(self) -> None:
        await asyncio.Future()


class _ExecuteToolTransport:
    """所有实际 hb_cli 子进程均通过统一受管入口执行。"""

    async def run(self, spec: ToolSpec, stop: StopSignal) -> RawToolOutcome:
        return await execute_tool(spec, stop=stop)


class HasselbladDriver:
    """核对绑定机身并准备参数，在实际 START 中执行对焦及单张拍摄。"""

    driver_id = DRIVER_ID
    evidence = EVIDENCE
    default_prepare_lead_s = DEFAULT_PREPARE_LEAD_S

    def __init__(
        self,
        devices: Mapping,
        roots,
        *,
        monotonic_ns: Callable[[], int],
        terminate_grace_s: Decimal = Decimal("3"),
        transport=None,
    ) -> None:
        if not isinstance(devices, Mapping):
            raise TypeError("devices 必须是映射")
        if not callable(monotonic_ns):
            raise TypeError("monotonic_ns 必须可调用")
        if (
            isinstance(terminate_grace_s, bool)
            or not isinstance(terminate_grace_s, Decimal)
            or not terminate_grace_s.is_finite()
            or terminate_grace_s <= 0
        ):
            raise ValueError("terminate_grace_s 必须是有限正 Decimal 秒数")
        self.devices = devices
        self.roots = roots
        self.monotonic_ns = monotonic_ns
        self.terminate_grace_s = terminate_grace_s
        self.transport = _ExecuteToolTransport() if transport is None else transport
        if not callable(getattr(self.transport, "run", None)):
            raise TypeError("transport 必须提供 run(ToolSpec, StopSignal)")
        self.declaration = DRIVER_DECLARATION
        self._prepared_captures = PreparedCaptureCache()

    def begin_session(self) -> None:
        """新执行会话清空准备凭据，不向相机派发命令。"""
        self._prepared_captures.clear()

    def prepared_capture_matches(self, request: ControlRequest) -> bool:
        """只检查本会话凭据及本地绑定，不读取或设置机身。"""
        try:
            config = self._binding_config(request.binding)
        except (DeviceConfigurationError, ValueError):
            return False
        return self._prepared_captures.matches(request, (config.serial, config.product_id))

    def _binding_config(self, binding):
        if binding.driver_id != self.driver_id:
            raise DeviceConfigurationError("原设备绑定的驱动与 Hasselblad 不匹配")
        result = check_binding(binding, self)
        if result.status is not BindingStatus.MATCHED:
            raise DeviceConfigurationError(f"原设备绑定在本次配置中不可用: {result.status.value}")
        return read_camera_config(self.devices[binding.device_id])

    async def check_availability(
        self, binding, *, expected_serial: str, expected_product_id: int, timeout_s: Decimal,
    ) -> AvailabilityResult:
        """一次独立检查只交付身份与受管工具事实，不释放活动。"""
        if not isinstance(expected_serial, str) or not expected_serial.strip():
            raise ValueError("检查必须携带固定的机身 serial")
        if type(expected_product_id) is not int or not 1 <= expected_product_id <= 0xffff:
            raise ValueError("检查必须携带固定的产品标识")
        if not isinstance(timeout_s, Decimal) or not timeout_s.is_finite() or timeout_s <= 0:
            raise ValueError("检查期限必须是有限正 Decimal 秒数")
        config = self._binding_config(binding)
        if (config.serial, config.product_id) != (expected_serial, expected_product_id):
            raise DeviceConfigurationError("当前设备绑定与固定机身身份不一致")

        def now():
            value = self.monotonic_ns()
            if type(value) is not int:
                raise TypeError("monotonic_ns 必须返回整数纳秒")
            return value

        began = now()
        calls: list[AvailabilityCall] = []
        serial = None

        def result(reason=None, *, settled=True):
            return AvailabilityResult(expected_serial, expected_product_id, serial,
                                      reason is None, reason, tuple(calls), settled, began, now())

        for name in ("list", "info", "doctor"):
            scope = current_call_scope()
            if scope is not None and scope.interrupted is not None:
                return result("cancelled")
            remaining = timeout_s - Decimal(now() - began) / _NANOSECONDS_PER_SECOND
            if remaining <= 0:
                return result("deadline_elapsed")
            spec = ToolSpec((config.executable, "--pid", f"0x{expected_product_id:04x}", name),
                            remaining, self.terminate_grace_s)
            interrupted = None
            try:
                with log_context(device_id=binding.device_id, driver_id=binding.driver_id,
                                 command=(name,)):
                    raw = await self.transport.run(spec, _NeverStop())
            except ToolCallCancelled as error:
                raw = error.outcome
                interrupted = error
            except asyncio.CancelledError as error:
                if isinstance(error.__cause__, ToolStartError):
                    calls.append(AvailabilityCall(name, now(), error=error.__cause__))
                    raise AvailabilityCancelled(result("tool_start_failed")) from error
                calls.append(AvailabilityCall(name, now(), error=error))
                raise AvailabilityCancelled(result("cancelled", settled=False)) from error
            except ToolStartError as error:
                calls.append(AvailabilityCall(name, now(), error=error))
                return result("tool_start_failed")
            except Exception as error:
                calls.append(AvailabilityCall(name, now(), error=error))
                return result("transport_failed", settled=False)
            if not isinstance(raw, RawToolOutcome):
                error = TypeError("transport.run 必须返回 RawToolOutcome")
                calls.append(AvailabilityCall(name, now(), error=error))
                return result("transport_failed", settled=False)
            returned = now()
            calls.append(AvailabilityCall(name, returned, raw))

            def finish(value):
                if interrupted is not None:
                    raise AvailabilityCancelled(value) from interrupted
                return value

            failure = self._local_return_failure(raw, needs_stdout=True)
            if failure is not None:
                return finish(result(failure, settled=raw.exit is not None))
            if raw.stderr is None:
                return finish(result("stderr_unavailable"))
            if Decimal(returned - began) / _NANOSECONDS_PER_SECOND >= timeout_s:
                return finish(result("deadline_elapsed"))
            try:
                if name == "list":
                    matching = tuple(camera for camera in parse_devices(raw.output)
                                     if camera.vendor_id == _VENDOR_ID
                                     and camera.product_id == expected_product_id)
                    if len(matching) != 1:
                        return finish(result("no_matching_device" if not matching else "multiple_matching_devices"))
                elif name == "info":
                    serial = parse_serial(raw.output)
                    if serial != expected_serial:
                        return finish(result("serial_mismatch"))
                elif not doctor_passed(raw.output, raw.stderr):
                    return finish(result("doctor_not_passed"))
            except CliParseError as error:
                calls[-1] = replace(calls[-1], error=error)
                return finish(result("invalid_response"))
            if interrupted is not None:
                raise AvailabilityCancelled(result() if name == "doctor" else result("cancelled")) from interrupted
        return result()

    async def open_read(self, source, offset, ticket, *, idle_timeout_s):
        """下载指定固定源，核验后从偏移读取；工具 stdout 不承载文件定位。"""
        if not isinstance(source, SourceFile):
            raise ValueError("读取必须携带固定源文件")
        identity = CameraFileIdentity.from_source(source.file_id, source.locator)
        if source.size_bytes != identity.size_bytes:
            raise ValueError("相机源长度与固定文件身份不一致")
        config = self._binding_config(identity.binding)
        if (config.serial, config.product_id) != (identity.serial, identity.product_id):
            raise PhysicalIdentityMismatch(identity.binding)
        workspace = DownloadWorkspace(self.roots.staging, ticket)
        if type(offset) is not int or not 0 <= offset <= source.size_bytes:
            raise ValueError("读取偏移不在固定长度范围内")
        if (not isinstance(idle_timeout_s, Decimal) or not idle_timeout_s.is_finite()
                or idle_timeout_s <= 0):
            raise ValueError("无数据超时必须是有限正 Decimal 秒数")
        steps = []
        settled = True
        began = self.monotonic_ns()

        async def run(name, args, *, stdout):
            nonlocal settled
            remaining = READ_PREPARATION_TIMEOUT_S - Decimal(self.monotonic_ns() - began) / _NANOSECONDS_PER_SECOND
            if remaining <= 0:
                raise CameraReadError("preparation_timeout", steps)
            settled = False
            with log_context(device_id=identity.binding.device_id,
                             driver_id=identity.binding.driver_id,
                             file_id=source.file_id, run_id=ticket.run_id,
                             attempt_id=ticket.attempt_id):
                raw = await self._run_step(config=config, name=name, args=args,
                                           timeout_s=remaining, steps=steps)
            settled = True
            if raw is None:
                raise CameraReadError("tool_start_failed", steps)
            failure = self._local_return_failure(raw, needs_stdout=stdout)
            if raw.error == "cancelled":
                raise asyncio.CancelledError("相机读取已取消，受管工具已退出") from CameraReadError(failure, steps)
            if failure:
                raise CameraReadError(failure, steps)
            return raw

        try:
            async with asyncio.timeout(float(READ_PREPARATION_TIMEOUT_S)):
                try:
                    await workspace.prepare()
                except (OSError, ValueError) as error:
                    raise CameraReadError("download_maintenance_waiting", steps, causes=(error,),
                        settlement=ReadPreparationSettlement.WAITING) from error
                raw = await run("list", ("list",), stdout=True)
                cameras = parse_devices(raw.output)
                matching = tuple(camera for camera in cameras if camera.vendor_id == _VENDOR_ID
                                 and camera.product_id == config.product_id)
                if len(matching) != 1:
                    raise CameraReadError("no_matching_device" if not matching else "multiple_matching_devices", steps)
                raw = await run("info", ("info",), stdout=True)
                if parse_serial(raw.output) != identity.serial:
                    raise CameraReadError("serial_mismatch", steps)
                await run("download", ("download", identity.variant.value, identity.camera_name,
                                       "--output", str(workspace.path)), stdout=False)
                stream = await workspace.open_verified(source.size_bytes, identity.sha256, offset)
                return ReadSession(source, offset, stream, idle_timeout_s)
        except BaseException as error:
            # transport 未交回实际退出事实时，不能删除可能仍被工具使用的目录。
            if not settled or not workspace.files_closed:
                causes = (error,) if error.__cause__ is None else (error, error.__cause__)
                raise CameraReadError("preparation_unconfirmed", steps, causes=causes,
                    settlement=ReadPreparationSettlement.UNCONFIRMED) from error
            if settled:
                try:
                    await workspace.cleanup()
                except BaseException as cleanup_error:
                    if isinstance(error, Exception) and isinstance(cleanup_error, Exception):
                        raise CameraReadError("preparation_and_cleanup_failed", steps,
                                              causes=(error, cleanup_error)) from error
                    raise BaseExceptionGroup("相机读取准备与临时下载回收均失败", [error, cleanup_error]) from None
            if isinstance(error, Exception) and not isinstance(error, CameraReadError):
                raise CameraReadError("preparation_failed", steps, causes=(error,)) from error
            raise

    def _request_config(self, request: ControlRequest) -> tuple[str, CameraConfig, PhotoSettings]:
        if request.operation != "take_photo":
            raise ValueError("Hasselblad 驱动只接受 take_photo")
        ticket = request.ticket
        if ticket is None or ticket.operation != "control":
            raise ValueError("拍摄调用必须携带已提交的 control 尝试")
        if not isinstance(ticket.target_id, str):
            raise ValueError("拍摄调用必须以活动身份为目标")
        parse_object_id(ticket.target_id)
        config = self._binding_config(request.binding)
        try:
            settings = parse_photo_settings(request.params)
        except ValueError as error:
            raise ValueError("Hasselblad 拍摄参数不符合完整照片参数类型") from error
        timeout = request.timeout_s
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, Decimal)
            or not timeout.is_finite()
            or timeout <= 0
        ):
            raise ValueError("拍摄调用必须携带有限正 Decimal 总期限")
        if not callable(request.dispatch_check):
            raise ValueError("多步拍摄需要框架的派发资格检查")
        return ticket.target_id, config, settings

    def _call_info(self, raw: RawToolOutcome | None) -> CallInfo | None:
        if raw is None or raw.exit is None:
            return None
        if raw.exit.exit_code is not None:
            return CallInfo(local_exit_code=raw.exit.exit_code)
        return CallInfo(local_signal=raw.exit.signal)

    @staticmethod
    def _step_fact(name: str, raw: RawToolOutcome) -> dict[str, object]:
        """保存真实本地调用信息，不从标准输出推断设备退出或成功。"""
        output = raw.output
        facts: dict[str, object] = {
            "name": name,
            "local_exit_code": None if raw.exit is None else raw.exit.exit_code,
            "local_signal": None if raw.exit is None else raw.exit.signal,
            "transport_error": raw.error,
            "output_failure": raw.output_failure,
            "stdout_size_bytes": None if output is None else len(output),
            "stdout_sha256": None if output is None else hashlib.sha256(output).hexdigest(),
            "signal_failures": [
                {"stage": failure.stage.value, "message": failure.message}
                for failure in raw.signal_failures
            ],
        }
        if raw.used_grace_s is not None:
            facts["used_grace_s"] = raw.used_grace_s
        return facts

    @staticmethod
    def _local_return_failure(raw: RawToolOutcome, *, needs_stdout: bool) -> str | None:
        if raw.error is not None:
            return raw.error
        if raw.output_failure is not None:
            return "output_incomplete"
        if raw.exit is None:
            return "local_exit_unavailable"
        if raw.exit.signal is not None:
            return "process_signalled"
        if raw.exit.exit_code != 0:
            return "exit_nonzero"
        if needs_stdout and raw.output is None:
            return "stdout_unavailable"
        return None

    async def _run_step(
        self,
        *,
        config: CameraConfig,
        name: str,
        args: tuple[str, ...],
        timeout_s: Decimal,
        steps: list[dict[str, object]],
    ) -> RawToolOutcome | None:
        spec = ToolSpec(
            (config.executable, "--pid", f"0x{config.product_id:04x}", *args),
            timeout_s,
            self.terminate_grace_s,
        )
        try:
            with log_context(command=args):
                raw = await self.transport.run(spec, _NeverStop())
        except ToolStartError as failure:
            steps.append({
                "name": name,
                "start_error": f"{type(failure).__name__}: {failure}",
            })
            return None
        except ToolCallCancelled as failure:
            raw = failure.outcome
        except asyncio.CancelledError as failure:
            # 真实 execute_tool 在拥有范围内交回收场后的 RawToolOutcome；
            # 裸取消没有该事实，保留未收场责任并由外层拥有者恢复。
            scope = current_call_scope()
            if scope is not None:
                scope.interrupted = failure
            raise
        # 除 ToolStartError 外，transport 异常违反“运行后交回收场结果”的
        # 端口契约；不把它伪装成结束结果，保留原 RUNNING 尝试供恢复接手。
        if not isinstance(raw, RawToolOutcome):
            raise TypeError("transport.run 必须返回 RawToolOutcome")
        steps.append(self._step_fact(name, raw))
        if raw.exit is None:
            raise RuntimeError(f"{name} transport 未提供本地退出/收场结果")
        return raw

    async def control(self, request: ControlRequest) -> DeviceCallResult:
        if request.start_entry is not None:
            request.start_entry.enter(request.ticket)
        return await self._capture_control(request, prepare_only=False)

    async def prepare_capture(self, request: ControlRequest) -> DeviceCallResult:
        """独立完成可重复设置，不进入 START、移动镜头或拍照。"""
        return await self._capture_control(request, prepare_only=True)

    async def _capture_control(self, request: ControlRequest, *, prepare_only: bool) -> DeviceCallResult:
        if prepare_only:
            request = replace(request, params=deepcopy(dict(request.params)))
        activity_id, config, settings = self._request_config(request)
        prepared = not prepare_only and request.prepared_by is not None
        if prepare_only and request.prepared_by is not None:
            raise ValueError("独立设置准备不能携带已有准备凭据")
        if prepared and not self._prepared_captures.matches(request, (config.serial, config.product_id)):
            raise ValueError("实际启动缺少与原设备、活动、参数及准备尝试匹配的会话凭据")
        if not prepared:
            self._prepared_captures.clear(request.binding)
        entries = () if prepare_only else (entered_observation(request.ticket),)
        roots = self.roots
        phase = PhotoPhase.PREPARATION if prepare_only else PhotoPhase.CAPTURE if prepared else None
        commands = photo_start_commands(settings, phase=phase)
        began_ns = self.monotonic_ns()
        if type(began_ns) is not int:
            raise TypeError("monotonic_ns 必须返回整数纳秒")
        steps: list[dict[str, object]] = []
        calls: list[ToolCallOutput] = []
        last_raw: RawToolOutcome | None = None

        def finish(status, effect, *, error=None, observations=()):
            # 原关联由票据确定；这里只保留实际取得的双流和步骤对应关系。
            basis = SettlementBasis.OBSERVED if last_raw is not None else SettlementBasis.NOT_DISPATCHED
            data = {} if last_raw is None or last_raw.used_grace_s is None else {
                "terminate_grace_s": last_raw.used_grace_s}
            outcome = CallOutcome(
                status=status, error=error, effect=effect,
                settlement=Settlement(basis, EvidenceValue(
                    "operation_returned" if last_raw is not None else "dispatch_prevented", 1, data)),
                observations=(*observations, *entries),
                call_info=self._call_info(last_raw),
                diagnostics=ToolDiagnostics(PHOTO_START_CALL_LIMIT, tuple(calls)),
            )
            # 两份名称目录随本次返回冻结，不在接手时读取驱动或重新规划。
            planned_names = tuple(command.name.value for command in commands)
            actual_names = tuple(step["name"] for step in steps)

            def check_prefix(original):
                diagnostic_calls = original.diagnostics.calls
                if (len(diagnostic_calls) > len(planned_names)
                        or len(diagnostic_calls) != len(actual_names)
                        or any(call.step_name != planned or actual != call.step_name
                               for call, planned, actual in zip(
                                   diagnostic_calls, planned_names, actual_names))):
                    raise ValueError("原工具诊断与实际命令前缀不符")

            return DeviceCallResult.from_outcome(outcome, diagnostics_check=check_prefix)

        def failed(step, reason, *, effect=EffectState.NO_EFFECT, extra=None, stage="control", code="device_error"):
            details = {"step": step, "reason": reason, "steps": [dict(fact) for fact in steps]}
            if extra is not None:
                details.update(extra)
            # 实际调用/解释失败已取得，照片是否未知不改变这次失败的确定性。
            return finish(AttemptStatus.FAILED, effect, error=ErrorValue(code, stage, details))

        async def remaining(name):
            reason = await request.dispatch_check()
            if reason is not None:
                if not isinstance(reason, str) or not reason:
                    raise ValueError("派发检查必须返回非空原因或 None")
                return failed(name, reason, stage="dispatch", code=reason)
            scope = current_call_scope()
            if scope is not None and scope.interrupted is not None:
                return failed(name, "transport_cancelled", stage="dispatch", code="transport_cancelled")
            now_ns = self.monotonic_ns()
            if type(now_ns) is not int:
                raise TypeError("monotonic_ns 必须返回整数纳秒")
            budget = request.timeout_s - Decimal(now_ns - began_ns) / _NANOSECONDS_PER_SECOND
            if budget <= 0:
                return failed(name, "deadline_elapsed", stage="dispatch", code="start_preparation_timeout")
            return budget

        for command in commands:
            name, args = command.name.value, command.argv
            budget = await remaining(name)
            if isinstance(budget, DeviceCallResult):
                return budget
            if command.name is PhotoStep.CAPTURE:
                try:
                    workspace = await prepare_capture_workspace(roots, activity_id)
                except (CaptureFileError, OSError) as error:
                    return failed("prepare_workspace", getattr(error, "stage", "workspace_prepare_failed"),
                                  extra={"workspace_error": str(error)})
                budget = await remaining(name)
                if isinstance(budget, DeviceCallResult):
                    return budget
                args = (*args, "--output", str(workspace))
            stage = ("camera_start_command" if command.name is PhotoStep.CAPTURE else
                     "camera_setting" if command.name not in (PhotoStep.LIST, PhotoStep.INFO) else None)
            with log_context(device_id=request.binding.device_id,
                             driver_id=request.binding.driver_id, activity_id=activity_id,
                             run_id=request.ticket.run_id, attempt_id=request.ticket.attempt_id):
                with measure_stage(stage, command=args, timeout_s=str(budget)) if stage is not None else nullcontext():
                    raw = await self._run_step(config=config, name=name, args=args,
                                               timeout_s=budget, steps=steps)
            complete = raw is not None and raw.output is not None and raw.stderr is not None and raw.output_failure is None
            def stream(data):
                state = ToolStreamState.UNAVAILABLE if data is None else (
                    ToolStreamState.COMPLETE if complete else ToolStreamState.UNCONFIRMED)
                return ToolStream(state, data)
            calls.append(ToolCallOutput(len(calls) + 1, name,
                                       stream(None if raw is None else raw.output),
                                       stream(None if raw is None else raw.stderr)))
            if raw is None:
                return failed(name, "tool_start_failed", extra={"start_error": steps[-1]["start_error"]})
            last_raw = raw
            failure = self._local_return_failure(raw, needs_stdout=command.name in (PhotoStep.LIST, PhotoStep.INFO, PhotoStep.CAPTURE))
            if failure is None and command.name is PhotoStep.CAPTURE and raw.stderr is None:
                failure = "stderr_unavailable"
            if failure is not None:
                return failed(name, failure, effect=EffectState.UNKNOWN if command.name is PhotoStep.CAPTURE else EffectState.NO_EFFECT)
            if command.name is PhotoStep.LIST:
                try:
                    cameras = parse_devices(raw.output)
                except CliParseError as error:
                    return failed(name, "response_unconfirmed", extra={"response_error": str(error)})
                matching = tuple(camera for camera in cameras if camera.vendor_id == _VENDOR_ID and camera.product_id == config.product_id)
                steps[-1]["devices"] = [{"vendor_id": c.vendor_id, "product_id": c.product_id, "bus": c.bus, "address": c.address} for c in cameras]
                steps[-1]["matching_count"] = len(matching)
                if len(matching) != 1:
                    return failed(name, "no_matching_device" if not matching else "multiple_matching_devices")
            elif command.name is PhotoStep.INFO:
                try:
                    serial = parse_serial(raw.output)
                except CliParseError as error:
                    return failed(name, "response_unconfirmed", extra={"response_error": str(error)})
                steps[-1]["observed_serial"] = serial
                if serial != config.serial:
                    return failed(name, "serial_mismatch")
                steps[-1]["serial_verified"] = True
            elif command.name is not PhotoStep.CAPTURE:
                steps[-1]["setting_returned"] = True
                if command.name in (PhotoStep.FOCUS, PhotoStep.AUTOFOCUS):
                    steps[-1]["lens_operation_returned"] = True

        if prepare_only:
            budget = await remaining("preparation_completed")
            if isinstance(budget, DeviceCallResult):
                return budget
            observation = DeviceObservation(
                CAPTURE_PREPARATION_COMPLETED.type, CAPTURE_PREPARATION_COMPLETED.version,
                {"activity_id": activity_id})
            preparation_steps = DeviceObservation(
                _PHOTO_PREPARATION_STEPS.type, _PHOTO_PREPARATION_STEPS.version,
                {"activity_id": activity_id, "steps": deepcopy(steps)})
            result = finish(AttemptStatus.SUCCEEDED, EffectState.CONFIRMED,
                            observations=(observation, preparation_steps))
            self._prepared_captures.remember(request, (serial, config.product_id))
            return result

        try:
            response: CaptureResponse = parse_capture(raw.output, raw.stderr, output_complete=complete,
                capture_format=settings.capture_format, output_directory=str(workspace))
        except CliParseError as error:
            return failed("capture", "capture_response_unconfirmed", effect=EffectState.UNKNOWN,
                          extra={"response_error": str(error), "response_code": error.code})
        steps[-1]["files"] = [{"host_name": item.host_name, "size_bytes": item.size, "media_type": item.media_type}
                              for item in (response.original, response.preview)]
        steps[-1]["camera_locator"] = response.camera_locator.as_json()
        steps[-1]["camera_identity"] = {"serial": config.serial, "product_id": config.product_id}
        specs = tuple(CaptureFileSpec(item.host_name, item.size, item.media_type) for item in (response.original, response.preview))
        try:
            artifacts = await verify_capture_files(roots, activity_id, specs)
        except CaptureFileError as error:
            return failed("verify_files", error.stage, effect=EffectState.UNKNOWN, extra={"file_error": str(error)})
        if (not isinstance(artifacts, tuple) or len(artifacts) != 2
                or any(not isinstance(item, CapturedArtifact) for item in artifacts)
                or tuple((item.original_name, item.size_bytes, item.media_type) for item in artifacts)
                != tuple((spec.original_name, spec.size_bytes, spec.media_type) for spec in specs)):
            return failed("verify_files", "verified_file_pair_mismatch", effect=EffectState.UNKNOWN)
        observation = DeviceObservation("photo_taken", 1, {
            "activity_id": activity_id, "original": artifacts[0].as_json(), "preview": artifacts[1].as_json(),
            "steps": [dict(fact) for fact in steps]})
        return finish(AttemptStatus.SUCCEEDED, EffectState.CONFIRMED, observations=(observation,))

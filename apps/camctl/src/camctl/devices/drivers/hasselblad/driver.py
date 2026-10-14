"""CFV 100C 的受管单张拍摄调用。"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from decimal import Decimal
from typing import Callable

from camctl.contracts.values import parse_object_id
from camctl.devices.bindings import BindingStatus, DeviceConfigurationError, check_binding
from camctl.devices.evidence import (
    DeviceObservation,
    EvidenceContract,
    EvidenceRegistry,
    validate_observation,
)
from camctl.devices.ports import ControlRequest, DeviceCallResult, DriverDeclaration
from camctl.devices.read_session import ReadSession, SourceFile, ReadPreparationError, ReadPreparationSettlement
from camctl.host_files.downloads import DownloadWorkspace
from camctl.host_files.capture_sources import (
    CaptureFileError,
    CaptureFileSpec,
    CapturedArtifact,
    prepare_capture_workspace,
    verify_capture_files,
)
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
from camctl.operations.process import (
    RawToolOutcome,
    StopSignal,
    ToolCallCancelled,
    ToolSpec,
    ToolStartError,
    execute_tool,
)
from camctl.operations.validation import validate_outcome

from .configuration import CameraConfig, read_camera_config
from .definitions import DRIVER_ID, PARAMETER_TYPE
from .responses import (
    CaptureResponse,
    CliParseError,
    parse_capture,
    parse_devices,
    parse_serial,
)
from .files import CameraFileIdentity

__all__ = ["EVIDENCE", "HasselbladDriver"]


_PHOTO_TAKEN = EvidenceContract(
    "photo_taken",
    1,
    "control",
    frozenset({"activity_id", "original", "preview", "steps"}),
    identity_field="activity_id",
)
_OPERATION_RETURNED = EvidenceContract(
    "operation_returned", 1, "control", frozenset({"terminate_grace_s"})
)
_DISPATCH_PREVENTED = EvidenceContract(
    "dispatch_prevented", 1, "control", frozenset()
)
EVIDENCE = EvidenceRegistry((
    _PHOTO_TAKEN, _OPERATION_RETURNED, _DISPATCH_PREVENTED,
    EvidenceContract("read_returned", 1, "read", frozenset()),
    EvidenceContract("local_read_recovery", 1, "read", frozenset()),
))

_VENDOR_ID = 0x2756
_RAW_MEDIA_TYPE = "image/x-hasselblad-3fr"
_PREVIEW_MEDIA_TYPE = "image/jpeg"
_NANOSECONDS_PER_SECOND = Decimal(1_000_000_000)
READ_PREPARATION_TIMEOUT_S = Decimal("180")


class CameraReadError(ReadPreparationError):
    """下载或身份核对失败；保留各次受管调用与实际收场细节。"""

    def __init__(self, reason, steps, *, causes=(), settlement=ReadPreparationSettlement.SETTLED):
        self.reason, self.steps = reason, tuple(dict(step) for step in steps)
        self.causes = tuple(causes)
        # OSError 的 repr 不包含 filename；异常组的 str 则不包含各子错误。
        diagnostics = tuple(f"{type(cause).__name__}: {cause}" if isinstance(cause, OSError)
                            else repr(cause) for cause in self.causes)
        super().__init__(f"相机文件读取失败: {reason}; steps={self.steps!r}; causes={diagnostics!r}",
                         settlement=settlement)


class _NeverStop:
    """hb_cli 的 stop 子命令会关机，取消只能由受管进程边界处理。"""

    async def requested(self) -> None:
        await asyncio.Future()


class _ExecuteToolTransport:
    """所有实际 hb_cli 子进程均通过统一受管入口执行。"""

    async def run(self, spec: ToolSpec, stop: StopSignal) -> RawToolOutcome:
        return await execute_tool(spec, stop=stop)


class HasselbladDriver:
    """验证绑定机身身份后执行一次 RAW 拍摄，不重试业务命令。"""

    driver_id = DRIVER_ID
    evidence = EVIDENCE

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
        self.declaration = DriverDeclaration(
            control_supported=True,
            stop_supported=False,
            query_supported=False,
            result_supported=False,
            read_supported=True,
            digest_supported=False,
            delete_supported=False,
            directory_supported=False,
            local_read_recovery_supported=True,
        )

    def _binding_config(self, binding):
        if binding.driver_id != self.driver_id:
            raise DeviceConfigurationError("原设备绑定的驱动与 Hasselblad 不匹配")
        result = check_binding(binding, self)
        if result.status is not BindingStatus.MATCHED:
            raise DeviceConfigurationError(f"原设备绑定在本次配置中不可用: {result.status.value}")
        return read_camera_config(self.devices[binding.device_id])

    async def open_read(self, source, offset, ticket, *, idle_timeout_s):
        """下载指定固定源，核验后从偏移读取；工具 stdout 不承载文件定位。"""
        if not isinstance(source, SourceFile):
            raise ValueError("读取必须携带固定源文件")
        identity = CameraFileIdentity.from_source(source.file_id, source.locator)
        config = self._binding_config(identity.binding)
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
                if parse_serial(raw.output) != config.serial:
                    raise CameraReadError("serial_mismatch", steps)
                await run("download", ("download", identity.variant.value, identity.original_name,
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

    def _request_config(self, request: ControlRequest) -> tuple[str, CameraConfig]:
        if request.operation != "take_photo":
            raise ValueError("Hasselblad 驱动只接受 take_photo")
        ticket = request.ticket
        if ticket is None or ticket.operation != "control":
            raise ValueError("拍摄调用必须携带已提交的 control 尝试")
        if not isinstance(ticket.target_id, str):
            raise ValueError("拍摄调用必须以活动身份为目标")
        parse_object_id(ticket.target_id)
        config = self._binding_config(request.binding)
        if set(request.params) != {"type"} or request.params.get("type") != PARAMETER_TYPE:
            raise ValueError("Hasselblad 单张参数必须恰为已登记照片类型")
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
        return ticket.target_id, config

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
            "stderr_hex": None if raw.stderr is None else raw.stderr.hex(),
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

    def _result(
        self,
        request: ControlRequest,
        *,
        status: AttemptStatus,
        effect: EffectState,
        last_raw: RawToolOutcome | None,
        error: ErrorValue | None = None,
        observation: DeviceObservation | None = None,
        settlement_basis: SettlementBasis | None = None,
    ) -> DeviceCallResult:
        if settlement_basis is None:
            settlement_basis = (
                SettlementBasis.OBSERVED
                if last_raw is not None
                else SettlementBasis.NOT_DISPATCHED
            )
        evidence_type = (
            "operation_returned"
            if settlement_basis is SettlementBasis.OBSERVED
            else "dispatch_prevented"
        )
        settlement_data: dict[str, object] = {}
        if (
            settlement_basis is SettlementBasis.OBSERVED
            and last_raw is not None
            and last_raw.used_grace_s is not None
        ):
            settlement_data["terminate_grace_s"] = last_raw.used_grace_s
        outcome = CallOutcome(
            status=status,
            error=error,
            effect=effect,
            settlement=Settlement(
                settlement_basis, EvidenceValue(evidence_type, 1, settlement_data)
            ),
            observations=() if observation is None else (observation,),
            call_info=self._call_info(last_raw),
        )
        validate_outcome(request.ticket, outcome, self.evidence)
        return DeviceCallResult.from_outcome(outcome)

    def _failed(
        self,
        request: ControlRequest,
        *,
        step: str,
        reason: str,
        steps: list[dict[str, object]],
        last_raw: RawToolOutcome | None,
        effect: EffectState,
        extra: Mapping[str, object] | None = None,
        error_code: str = "device_error",
        error_stage: str = "control",
        settlement_basis: SettlementBasis | None = None,
    ) -> DeviceCallResult:
        details: dict[str, object] = {
            "step": step,
            "reason": reason,
            "steps": [dict(fact) for fact in steps],
        }
        if extra is not None:
            details.update(extra)
        return self._result(
            request,
            status=AttemptStatus.UNKNOWN if effect is EffectState.UNKNOWN else AttemptStatus.FAILED,
            effect=effect,
            last_raw=last_raw,
            error=ErrorValue(error_code, error_stage, details),
            settlement_basis=settlement_basis,
        )

    def _prevented(
        self,
        request: ControlRequest,
        *,
        step: str,
        reason: str,
        steps: list[dict[str, object]],
        last_raw: RawToolOutcome | None,
        error_code: str,
    ) -> DeviceCallResult:
        return self._failed(
            request,
            step=step,
            reason=reason,
            steps=steps,
            last_raw=last_raw,
            effect=EffectState.NO_EFFECT,
            error_code=error_code,
            error_stage="dispatch",
        )

    def _next_dispatch(
        self,
        request: ControlRequest,
        *,
        step: str,
        began_ns: int,
        steps: list[dict[str, object]],
        last_raw: RawToolOutcome | None,
    ) -> Decimal | DeviceCallResult:
        reason = request.dispatch_check()
        if reason is not None:
            if not isinstance(reason, str) or not reason:
                raise ValueError("派发检查必须返回非空原因或 None")
            return self._prevented(
                request, step=step, reason=reason, steps=steps, last_raw=last_raw,
                error_code=reason,
            )
        scope = current_call_scope()
        if scope is not None and scope.interrupted is not None:
            return self._prevented(
                request, step=step, reason="transport_cancelled", steps=steps,
                last_raw=last_raw, error_code="transport_cancelled",
            )
        now_ns = self.monotonic_ns()
        if isinstance(now_ns, bool) or not isinstance(now_ns, int):
            raise TypeError("monotonic_ns 必须返回整数纳秒")
        remaining = request.timeout_s - Decimal(now_ns - began_ns) / _NANOSECONDS_PER_SECOND
        if remaining <= 0:
            return self._prevented(
                request, step=step, reason="deadline_elapsed", steps=steps,
                last_raw=last_raw, error_code="start_preparation_timeout",
            )
        return remaining

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
        activity_id, config = self._request_config(request)
        roots = self.roots
        began_ns = self.monotonic_ns()
        if isinstance(began_ns, bool) or not isinstance(began_ns, int):
            raise TypeError("monotonic_ns 必须返回整数纳秒")
        steps: list[dict[str, object]] = []
        last_raw: RawToolOutcome | None = None

        async def run_checked(name: str, args: tuple[str, ...]):
            nonlocal last_raw
            remaining = self._next_dispatch(
                request, step=name, began_ns=began_ns, steps=steps, last_raw=last_raw
            )
            if isinstance(remaining, DeviceCallResult):
                return remaining, None
            raw = await self._run_step(
                config=config, name=name, args=args, timeout_s=remaining, steps=steps
            )
            if raw is not None:
                last_raw = raw
            return None, raw

        result, raw = await run_checked("list", ("list",))
        if result is not None:
            return result
        if raw is None:
            return self._failed(
                request, step="list", reason="tool_start_failed", steps=steps,
                last_raw=last_raw, effect=EffectState.NO_EFFECT,
                extra={"start_error": steps[-1]["start_error"]},
                settlement_basis=(SettlementBasis.NOT_DISPATCHED if last_raw is None
                                  else SettlementBasis.OBSERVED),
            )
        failure = self._local_return_failure(raw, needs_stdout=True)
        if failure is not None:
            return self._failed(request, step="list", reason=failure, steps=steps,
                                last_raw=last_raw, effect=EffectState.NO_EFFECT)
        try:
            cameras = parse_devices(raw.output)
        except CliParseError as failure:
            return self._failed(request, step="list", reason="response_unconfirmed", steps=steps,
                                last_raw=last_raw, effect=EffectState.NO_EFFECT,
                                extra={"response_error": str(failure)})
        matching = tuple(
            camera for camera in cameras
            if camera.vendor_id == _VENDOR_ID and camera.product_id == config.product_id
        )
        steps[-1]["devices"] = [
            {"vendor_id": camera.vendor_id, "product_id": camera.product_id,
             "bus": camera.bus, "address": camera.address}
            for camera in cameras
        ]
        steps[-1]["matching_count"] = len(matching)
        if not matching:
            return self._failed(request, step="list", reason="no_matching_device", steps=steps,
                                last_raw=last_raw, effect=EffectState.NO_EFFECT)
        if len(matching) != 1:
            return self._failed(request, step="list", reason="multiple_matching_devices", steps=steps,
                                last_raw=last_raw, effect=EffectState.NO_EFFECT)

        result, raw = await run_checked("info", ("info",))
        if result is not None:
            return result
        if raw is None:
            return self._failed(
                request, step="info", reason="tool_start_failed", steps=steps,
                last_raw=last_raw, effect=EffectState.NO_EFFECT,
                extra={"start_error": steps[-1]["start_error"]},
            )
        failure = self._local_return_failure(raw, needs_stdout=True)
        if failure is not None:
            return self._failed(request, step="info", reason=failure, steps=steps,
                                last_raw=last_raw, effect=EffectState.NO_EFFECT)
        try:
            observed_serial = parse_serial(raw.output)
        except CliParseError as failure:
            return self._failed(request, step="info", reason="response_unconfirmed", steps=steps,
                                last_raw=last_raw, effect=EffectState.NO_EFFECT,
                                extra={"response_error": str(failure)})
        steps[-1]["observed_serial"] = observed_serial
        if observed_serial != config.serial:
            return self._failed(request, step="info", reason="serial_mismatch", steps=steps,
                                last_raw=last_raw, effect=EffectState.NO_EFFECT)
        steps[-1]["serial_verified"] = True

        for setting, args in (
            ("drive_mode_single", ("set", "drive-mode", "single")),
            ("capture_format_raw", ("set", "capture-format", "raw")),
        ):
            result, raw = await run_checked(setting, args)
            if result is not None:
                return result
            if raw is None:
                return self._failed(
                    request, step=setting, reason="tool_start_failed", steps=steps,
                    last_raw=last_raw, effect=EffectState.NO_EFFECT,
                    extra={"start_error": steps[-1]["start_error"]},
                )
            failure = self._local_return_failure(raw, needs_stdout=False)
            if failure is not None:
                return self._failed(request, step=setting, reason=failure, steps=steps,
                                    last_raw=last_raw, effect=EffectState.NO_EFFECT)
            steps[-1]["setting_returned"] = True

        # 本地目录创建前先核一次剩余资格，避免取消或期限耗尽后创建
        # 一个永远不会用于拍摄的工作目录；目录准备之后仍在 run_checked
        # 中再次检查，覆盖准备期间发生的状态变化。
        before_workspace = self._next_dispatch(
            request, step="capture", began_ns=began_ns, steps=steps, last_raw=last_raw
        )
        if isinstance(before_workspace, DeviceCallResult):
            return before_workspace

        try:
            workspace = await prepare_capture_workspace(roots, activity_id)
        except CaptureFileError as failure:
            return self._failed(request, step="prepare_workspace", reason=failure.stage,
                                steps=steps, last_raw=last_raw, effect=EffectState.NO_EFFECT,
                                extra={"workspace_error": str(failure)})
        except OSError as failure:
            return self._failed(request, step="prepare_workspace", reason="workspace_prepare_failed",
                                steps=steps, last_raw=last_raw, effect=EffectState.NO_EFFECT,
                                extra={"workspace_error": f"{type(failure).__name__}: {failure}"})

        result, raw = await run_checked("capture", ("capture", "--output", str(workspace)))
        if result is not None:
            return result
        if raw is None:
            return self._failed(
                request, step="capture", reason="tool_start_failed", steps=steps,
                last_raw=last_raw, effect=EffectState.NO_EFFECT,
                extra={"start_error": steps[-1]["start_error"]},
            )
        failure = self._local_return_failure(raw, needs_stdout=True)
        if failure is not None:
            return self._failed(request, step="capture", reason=failure, steps=steps,
                                last_raw=last_raw, effect=EffectState.UNKNOWN)
        try:
            response: CaptureResponse = parse_capture(
                raw.output, output_directory=str(workspace)
            )
        except CliParseError as failure:
            return self._failed(request, step="capture", reason="capture_response_unconfirmed",
                                steps=steps, last_raw=last_raw, effect=EffectState.UNKNOWN,
                                extra={"response_error": str(failure)})
        steps[-1]["files"] = [
            {"original_name": response.original.original_name,
             "size_bytes": response.original.size,
             "media_type": _RAW_MEDIA_TYPE},
            {"original_name": response.preview.original_name,
             "size_bytes": response.preview.size,
             "media_type": _PREVIEW_MEDIA_TYPE},
        ]
        specs = (
            CaptureFileSpec(response.original.original_name, response.original.size, _RAW_MEDIA_TYPE),
            CaptureFileSpec(response.preview.original_name, response.preview.size, _PREVIEW_MEDIA_TYPE),
        )
        try:
            artifacts = await verify_capture_files(roots, activity_id, specs)
        except CaptureFileError as failure:
            return self._failed(request, step="verify_files", reason=failure.stage,
                                steps=steps, last_raw=last_raw, effect=EffectState.UNKNOWN,
                                extra={"file_error": str(failure)})
        if (
            not isinstance(artifacts, tuple)
            or len(artifacts) != 2
            or any(not isinstance(artifact, CapturedArtifact) for artifact in artifacts)
            or tuple((artifact.original_name, artifact.size_bytes, artifact.media_type)
                     for artifact in artifacts)
            != tuple((spec.original_name, spec.size_bytes, spec.media_type) for spec in specs)
        ):
            return self._failed(request, step="verify_files", reason="verified_file_pair_mismatch",
                                steps=steps, last_raw=last_raw, effect=EffectState.UNKNOWN)

        photo = DeviceObservation("photo_taken", 1, {
            "activity_id": activity_id,
            "original": artifacts[0].as_json(),
            "preview": artifacts[1].as_json(),
            "steps": [dict(fact) for fact in steps],
        })
        validate_observation(photo, _PHOTO_TAKEN, expected_identity=activity_id)
        return self._result(
            request,
            status=AttemptStatus.SUCCEEDED,
            effect=EffectState.CONFIRMED,
            last_raw=last_raw,
            observation=photo,
        )

"""拍摄推进运行时的会话级装配。

按设备声明与驱动登记项组装 CaptureRuntime：控制、停止、结果列举、
读取与源端摘要端口经 port_for 按静态声明取得，结果列举缺省按驱动
result 端口构造生产适配（DriverResultListing），注入 results 时整体
替换为部署端口。媒体链（读取会话、受管检查修复工具与修复余量）
随读取声明构造，未声明读取能力的驱动不装配媒体端口，处理行保持
等待。时钟异常的受限会话以 media_enabled=False 构造：不装配媒体
链，保守收场只停止并保存等待阶段。设备未声明时装配原绑定局部
失败责任；驱动未登记按配置错误拒绝。控制能力或生产装配下结果
列举能力未声明的动作保持原责任，等待适用装配；异
常多录修复余量读取 devices.<id>.recording.repair_margin_s
（configuration.md#配置归属），默认 10 秒。
"""

from __future__ import annotations

import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping

from camctl.capture.handlers import (
    CaptureRuntime,
    ListedResult,
    ObservedFile,
    PendingCallResult, PendingFileObservation, PendingCaptureCompletion, PendingResultCheckClose,
    ResultFilesPort,
    SessionRecordingState,
)
from camctl.capture.media import MediaPolicy
from camctl.capture.media_flow import (
    DriverReadSessions,
    MediaFlow,
    load_confirmed_source,
)
from camctl.capture.results import FileKind
from camctl.capture.result_inputs import observed_file as _observed_file
from camctl.capture.recovery import RecoveryBoundary, RecoveryDiagnostic, recovery_registry
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.devices.bindings import DeviceBinding, DeviceConfigurationError, check_binding
from camctl.devices.drivers.registry import (
    CapabilityNotDeclaredError,
    DriverRegistry,
    port_for,
)
from camctl.devices.ports import ControlRequest
from camctl.devices.read_session import SourceFile
from camctl.host_files.media import ProbeRequest, RepairRequest, probe_media, repair_media
from camctl.host_files.models import BoundDirectories
from camctl.host_files.tasks import FileTaskExecutor, FileTaskId, FileTaskResult
from camctl.operations.attempts import AttemptConfig, RetryWaitGate
from camctl.operations.models import AttemptTicket
from camctl.operations.validation import validate_outcome
from camctl.outputs.copy import SourceDigest
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.scheduling import SchedulingRepository
from camctl.persistence.repositories.timelapse import TimelapseRepository
from camctl.scheduling.rules import LaunchWindow
from camctl.session.supervision import OwnedTask, Supervisor

__all__ = [
    "DriverDigestReader",
    "DriverResultListing",
    "HostMediaTools",
    "execution_wait_config",
    "session_capture_assembly",
]

#: 异常多录修复余量的默认秒数（camera-recording.md#配置归属）。
_DEFAULT_REPAIR_MARGIN_S = Decimal("10")

#: 通信重试间隔的规格默认秒数（configuration.md#通信重试间隔）。
_DEFAULT_RETRY_INTERVAL_S = Decimal("3")

#: 结果列举观察的契约类型、版本与单次批量（D5 契约测试共用同一形态）。
_RESULT_LISTED_TYPE = "result_files_listed"
_RESULT_LISTED_VERSION = 1
_RESULT_BATCH_SIZE = 100


def _device_seconds(
    declaration: Mapping[str, Any], section: str, key: str,
    default: Decimal,
) -> Decimal:
    """读取设备子表中已规范化的秒数；未配置用默认值。"""
    subtable = declaration.get(section)
    if not isinstance(subtable, Mapping):
        return default
    value = subtable.get(key, default)
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _device_attempts(
    declaration: Mapping[str, Any], section: str, default: int,
    key: str = "max_attempts",
) -> int:
    """读取设备子表中已规范化的尝试上限；未配置用默认值。"""
    subtable = declaration.get(section)
    if not isinstance(subtable, Mapping):
        return default
    value = subtable.get(key, default)
    return value if isinstance(value, int) and not isinstance(value, bool) \
        else default

#: 源端摘要观察的契约类型与版本（devices 契约测试共用同一形态）。
_DIGEST_TYPE = "file_digest"
_DIGEST_VERSION = 1


class _SessionMediaOwner:
    """媒体任务等待者取消后的接手：等待实际结束并释放文件占用。

    实际结果不在此保存；媒体链按已保存事实在下一轮幂等续跑，接
    手只保证工具执行与文件占用实际收场。
    """

    async def take_over(self, task: OwnedTask) -> None:
        await task.pending.wait()


class HostMediaTools:
    """受管媒体工具的生产适配：绑定执行器、任务身份与工具参数。"""

    def __init__(
        self,
        roots: BoundDirectories,
        executor: FileTaskExecutor,
        owner: Any,
        *,
        probe_request: ProbeRequest | None = None,
        repair_request: RepairRequest | None = None,
    ) -> None:
        self._roots = roots
        self._executor = executor
        self._owner = owner
        self._probe_request = probe_request if probe_request is not None \
            else ProbeRequest()
        self._repair_request = repair_request if repair_request is not None \
            else RepairRequest()
        self._sequence = 0

    def _next_task_id(self, prefix: str) -> FileTaskId:
        self._sequence += 1
        return FileTaskId(f"{prefix}-{self._sequence}")

    async def probe(self, input: Any) -> FileTaskResult:
        return await probe_media(
            input, self._roots, self._probe_request,
            executor=self._executor,
            task_id=self._next_task_id("media-probe"),
            owner=self._owner,
        )

    async def repair(self, input: Any, output: Any,
                     *, trim_s: Decimal) -> FileTaskResult:
        # 多录裁剪只采用无重新编码方式：流复制到目标时长，尾部允许
        # 保留余量，不为精确边界重新编码（camera-recovery.md 裁剪约
        # 束）。注入的附加参数置于裁剪参数之前，测试替身脚本可叠加。
        request = RepairRequest(
            ffmpeg=self._repair_request.ffmpeg,
            output_args=self._repair_request.output_args + (
                "-c", "copy", "-t", str(trim_s)))
        return await repair_media(
            input, output, self._roots, request,
            executor=self._executor,
            task_id=self._next_task_id("media-repair"),
            owner=self._owner,
        )


class DriverDigestReader:
    """源端摘要读取适配：按源身份调用驱动摘要端口并核对证据。

    观察身份使用设备文件行号（规范十进制）；请求同时携带身份键与
    定位信息供驱动定位实际文件。登记证据缺少摘要契约属于装配不
    一致，直接暴露；驱动观察不符契约或调用错误表达为摘要获取失
    败，不降级为不支持。
    """

    def __init__(
        self, driver: Any, binding: DeviceBinding, evidence: Any,
        source: SourceFile, file_row_id: str,
    ) -> None:
        self._driver = driver
        self._binding = binding
        self._evidence = evidence
        self._source = source
        self._file_row_id = file_row_id

    async def read_digest(self) -> SourceDigest:
        request = ControlRequest(
            operation="digest",
            binding=self._binding,
            params={
                "file_id": self._file_row_id,
                "identity_key": self._source.file_id,
                "locator": dict(self._source.locator),
                "size_bytes": self._source.size_bytes,
            },
        )
        result = await self._driver.digest(request)
        usable = [observation for observation in result.observations
                  if observation.type == _DIGEST_TYPE]
        if result.error is not None and not usable:
            return SourceDigest(
                digest=None, error=dict(result.error) if isinstance(
                    result.error, Mapping) else str(result.error))
        if len(usable) != 1:
            return SourceDigest(
                digest=None,
                error="digest_observation_invalid:"
                      f" 期望一份摘要观察，实际 {len(usable)}")
        from camctl.devices.evidence import validate_observation

        contract = self._evidence.contract(_DIGEST_TYPE, _DIGEST_VERSION)
        try:
            validate_observation(
                usable[0], contract, expected_identity=self._file_row_id)
            sha256 = usable[0].data.get("sha256")
            return SourceDigest(digest=sha256, error=None)
        except ValueError as error:
            return SourceDigest(
                digest=None, error=f"digest_observation_invalid: {error}")


class DriverResultListing:
    """结果列举端口的驱动适配：list_results 观察转候选产物文件。

    每次列举按活动身份调用驱动的 result 端口，观察经登记契约校验
    后逐条解释为 ObservedFile；条目自身结构作为归属与完成的结构化
    依据。登记证据缺少列举契约属于装配错误，直接暴露；调用错误在
    没有可靠观察时表达为异常，由核实轮次按列举失败收场；可靠观察
    与调用错误并存时观察优先。
    """

    def __init__(
        self, driver: Any, binding: DeviceBinding, evidence: Any,
    ) -> None:
        self._driver = driver
        self._binding = binding
        self._evidence = evidence

    async def list_round(
        self, ticket: AttemptTicket, *, timeout_s: Decimal,
    ) -> ListedResult:
        """沿已提交的活动票据调用一次，并保留完整实际结果。"""
        if (ticket.operation != "result" or ticket.target_id is None
                or ticket.responsibility_key != f"results/{ticket.target_id}"):
            raise ValueError("结果列举要求原 RESULTS 活动票据")
        request = ControlRequest(
            operation="result", binding=self._binding,
            params={"activity_id": ticket.target_id},
            ticket=ticket, timeout_s=timeout_s)
        result = await self._driver.list_results(request, _RESULT_BATCH_SIZE)
        if result.outcome is None:
            raise ValueError("结果列举缺少完整实际调用结果")
        validate_outcome(ticket, result.outcome, self._evidence)
        if not result.observations and result.error is not None:
            return ListedResult((), result.outcome)
        return ListedResult(
            self._files_from_result(result, ticket.target_id), result.outcome)

    async def list_files(self, action_id: int) -> tuple[ObservedFile, ...]:
        self._evidence.contract(_RESULT_LISTED_TYPE, _RESULT_LISTED_VERSION)
        request = ControlRequest(
            operation="result",
            binding=self._binding,
            params={"activity_id": str(action_id)},
        )
        result = await self._driver.list_results(request, _RESULT_BATCH_SIZE)
        return self._files_from_result(result, str(action_id))

    def _files_from_result(
        self, result: Any, activity_id: str,
    ) -> tuple[ObservedFile, ...]:
        from camctl.devices.evidence import validate_observation

        contract = self._evidence.contract(
            _RESULT_LISTED_TYPE, _RESULT_LISTED_VERSION)
        listed = [observation for observation in result.observations
                  if observation.type == _RESULT_LISTED_TYPE]
        if not listed:
            if result.error is not None:
                raise RuntimeError(
                    f"结果列举调用失败: {dict(result.error)}")
            raise RuntimeError("结果列举观察缺失: 驱动未返回列举契约观察")
        entries: list[ObservedFile] = []
        for observation in listed:
            validate_observation(
                observation, contract, expected_identity=activity_id)
            entries.extend(
                _observed_file(entry)
                for entry in observation.data.get("entries", ()))
        return tuple(entries)


def _digest_reader_factory(
    owned: Any, driver: Any, binding: DeviceBinding, evidence: Any,
) -> Callable[[int], DriverDigestReader]:
    """构造按源设备文件解析的源端摘要读取工厂。"""

    def resolve(source_device_file_id: int) -> DriverDigestReader:
        source = load_confirmed_source(owned, source_device_file_id)
        return DriverDigestReader(
            driver, binding, evidence, source, str(source_device_file_id))

    return resolve


def execution_wait_config(action: Mapping[str, Any]) -> CaptureWaitConfig:
    """从已保存的动作行取得延时等待配置。

    目标时长与驱动必要余量在受理时固定于执行定义（result_wait_
    margin_ms），读取只使用首次保存的事实。会话装配再加入本次设备
    的部署额外等待。定义缺少目标时长属于不可推进的任务形态，明确拒绝。
    """
    import json

    spec = action["execution_spec_json"]
    if isinstance(spec, str):
        spec = json.loads(spec)
    if not isinstance(spec, Mapping) or "target_duration_ms" not in spec:
        raise ValueError("延时执行定义缺少 target_duration_ms")
    return CaptureWaitConfig(
        target_duration_ms=int(spec["target_duration_ms"]),
        driver_margin_ms=int(spec.get("result_wait_margin_ms", 0)),
    )


def _repair_margin_s(declaration: Mapping[str, Any]) -> Decimal:
    """读取设备的异常多录修复余量；未配置用默认秒数。"""
    recording = declaration.get("recording")
    if not isinstance(recording, Mapping):
        return _DEFAULT_REPAIR_MARGIN_S
    raw = recording.get("repair_margin_s", _DEFAULT_REPAIR_MARGIN_S)
    if isinstance(raw, str):
        raw = Decimal(raw)
    return raw


def _window_of(action: Mapping[str, Any]) -> LaunchWindow:
    return LaunchWindow(
        scheduled_at=action["scheduled_at"],
        window_end=action["scheduled_at"] + action["max_delay_ms"] * 1000)


def session_capture_assembly(
    *,
    devices: Mapping[str, Mapping[str, Any]],
    drivers: DriverRegistry,
    results: ResultFilesPort | None = None,
    staging: Path,
    wait_config: Callable[[Mapping[str, Any]], Any],
    wall_us: Callable[[], int] | None = None,
    monotonic_ns: Callable[[], int] | None = None,
    probe_request: ProbeRequest | None = None,
    repair_request: RepairRequest | None = None,
    media_enabled: bool = True,
    segment_size: int = 1024 * 1024,
    recovery_boundary: RecoveryBoundary = RecoveryBoundary.UNCONFIRMED,
    recovery_max_event_id: Callable[[], int | None] | None = None,
    on_recovery_diagnostic: Callable[[RecoveryDiagnostic], None] | None = None,
    file_executor: FileTaskExecutor | None = None,
    pending_call_results: dict[tuple[int, int], PendingCallResult] | None = None,
    pending_capture_completions: dict[int, PendingCaptureCompletion] | None = None,
    pending_result_closes: dict[int, PendingResultCheckClose] | None = None,
    recording_anchors: dict[int, tuple[int, int]] | None = None,
    retry_wait_gate: RetryWaitGate | None = None,
    pending_media_results: dict | None = None,
    pending_file_observations: dict[tuple[int, str], PendingFileObservation] | None = None,
    continuing_read_tickets: dict | None = None,
    pending_read_results: dict | None = None,
    pending_read_business: dict | None = None,
    pending_read_ends: dict | None = None,
    pending_baselines: dict | None = None,
) -> Callable[[Any, str], CaptureRuntime | None]:
    """构造会话级拍摄推进工厂：按设备解析登记驱动端口并组装运行时。

    会话共享录像锚点表、结果列举缓存与媒体任务执行器；每个推进轮
    次按设备构造 CaptureRuntime。设备未声明时构造不取得设备端口的
    局部失败运行时，驱动未登记时抛配置错误。控制能力未声明或结果
    列举能力未声明（生产装配时）返回 None，保持已保存责任。
    results 未注入时按
    驱动 result 端口构造生产列举适配（DriverResultListing），注入时
    整体替换为部署提供的端口。wall_us 与 monotonic_ns 缺省使用真实
    系统钟，测试可注入受控读数。media_enabled=False 供时钟异常的受
    限会话构造：不装配媒体链，保守收场不启动拷贝、核验与修复。
    recovery_max_event_id 读取会话已经固定的初始边界，不查询当前
    数据库边界；恢复适用声明和证据按原动作的驱动登记取得。
    pending_call_results 由会话拥有，普通、残留和受限工厂共用；
    独立使用本工厂时，未传入集合则为其创建一份。
    pending_capture_completions 共用拍摄终态及附属读取收尾的原完整申请。
    pending_result_closes 共用有限耗尽的原完整申请，保存恢复不重新决定时刻。
    recording_anchors 与 retry_wait_gate 保留同会话原结果的单调计时
    依据；三个生产工厂从会话接收同一对象，接手保存不会重新计时。
    四个 READ 集合保留原实际结束、完整申请及续传身份；默认三个工厂
    从同一会话接收，受限工厂不因持有这些原事实装配媒体链。
    """

    anchors = {} if recording_anchors is None else recording_anchors
    listings: dict[int, tuple[tuple, tuple]] = {}
    timelapse_deadlines: dict[int, int] = {}
    pending_start_results = {} if pending_call_results is None else pending_call_results
    capture_completions = {} if pending_capture_completions is None else pending_capture_completions
    result_closes = {} if pending_result_closes is None else pending_result_closes
    file_observations = {} if pending_file_observations is None else pending_file_observations
    baselines = {} if pending_baselines is None else pending_baselines
    continuing_read_tickets = {} if continuing_read_tickets is None else continuing_read_tickets
    pending_read_results = {} if pending_read_results is None else pending_read_results
    pending_read_business = {} if pending_read_business is None else pending_read_business
    pending_read_ends = {} if pending_read_ends is None else pending_read_ends
    media_results = {} if pending_media_results is None else pending_media_results
    retry_gate = RetryWaitGate() if retry_wait_gate is None else retry_wait_gate
    roots = BoundDirectories(staging=staging)
    executor = file_executor if file_executor is not None else FileTaskExecutor(Supervisor())
    tools = HostMediaTools(
        roots, executor, _SessionMediaOwner(),
        probe_request=probe_request, repair_request=repair_request)
    wall = wall_us if wall_us is not None \
        else (lambda: int(time.time() * 1_000_000))
    monotonic = monotonic_ns if monotonic_ns is not None else time.monotonic_ns
    current_config = SimpleNamespace(devices=devices)

    def original_recovery_evidence(saved: DeviceBinding, operation: str):
        return recovery_registry(drivers.entry(saved.driver_id), operation)

    def fixed_recovery_boundary() -> int | None:
        return None if recovery_max_event_id is None else recovery_max_event_id()

    def binding_only_runtime(owned: Any) -> CaptureRuntime:
        """缺少设备声明仍装配局部失败责任，不取得设备端口。"""
        return CaptureRuntime(
            owned=owned, scheduling=SchedulingRepository(),
            operations=OperationRepository(), capture=CaptureRepository(),
            timelapse=TimelapseRepository(), driver=None, results=None,
            evidence=None, wall_us=wall, monotonic_ns=monotonic,
            window_of=_window_of, wait_config=wait_config,
            recovery_boundary=recovery_boundary,
            recovery_max_event_id=fixed_recovery_boundary(),
            recovery_evidence_for=original_recovery_evidence,
            pending_start_results=pending_start_results,
            pending_capture_completions=capture_completions,
            pending_result_closes=result_closes,
            pending_file_observations=file_observations,
            pending_baselines=baselines,
            retry_gate=retry_gate,
            pending_read_results=pending_read_results, pending_read_business=pending_read_business,
            pending_read_ends=pending_read_ends, continuing_read_tickets=continuing_read_tickets,
            on_recovery_diagnostic=on_recovery_diagnostic,
            pending_media_results=media_results, file_executor=executor,
            binding_check=lambda saved: check_binding(saved, current_config))

    def factory(owned: Any, device_id: str) -> CaptureRuntime | None:
        declaration = devices.get(device_id)
        if not isinstance(declaration, Mapping):
            return binding_only_runtime(owned)
        driver_id = declaration.get("driver")
        entry = drivers.entry(driver_id) if isinstance(driver_id, str) else None
        if entry is None:
            raise DeviceConfigurationError(
                f"设备 {device_id} 声明的驱动未登记: {driver_id!r}")
        try:
            control_port = port_for(entry, "control")
        except CapabilityNotDeclaredError:
            return None
        try:
            stop_port = port_for(entry, "stop")
        except CapabilityNotDeclaredError:
            stop_port = None
        try:
            query_port = port_for(entry, "query")
        except CapabilityNotDeclaredError:
            query_port = None
        try:
            directory_port = port_for(entry, "directory")
        except CapabilityNotDeclaredError:
            directory_port = None
        if results is not None:
            results_port: ResultFilesPort | None = results
        else:
            try:
                result_port = port_for(entry, "result")
            except CapabilityNotDeclaredError:
                return None
            results_port = DriverResultListing(
                result_port,
                DeviceBinding(device_id=device_id, driver_id=str(driver_id)),
                entry.evidence)
        media = (
            _media_flow_with(
                owned, entry, device_id, str(driver_id), tools, staging,
                wall, declaration, retry_gate, monotonic, executor)
            if media_enabled else None)
        if media is not None:
            media.segment_size = segment_size
            media.recovery_boundary = recovery_boundary
            media.recovery_max_event_id = fixed_recovery_boundary()
            media.recovery_evidence_for = original_recovery_evidence
            media.on_recovery_diagnostic = on_recovery_diagnostic
            media.continuing_read_tickets = continuing_read_tickets
            media.pending_read_results = pending_read_results
            media.pending_read_business = pending_read_business
            media.pending_read_ends = pending_read_ends
            media.pending_media_results = media_results

        def current_wait_config(action):
            extra_wait = declaration.get("capture", {}).get("extra_wait_ms", 0)
            return replace(wait_config(action), extra_wait_ms=extra_wait)

        runtime = CaptureRuntime(
            owned=owned,
            scheduling=SchedulingRepository(),
            operations=OperationRepository(),
            capture=CaptureRepository(),
            timelapse=TimelapseRepository(),
            driver=control_port,
            results=results_port,
            evidence=entry.evidence,
            wall_us=wall,
            monotonic_ns=monotonic,
            window_of=_window_of,
            wait_config=current_wait_config,
            binding_check=lambda saved: check_binding(saved, current_config),
            recovery_boundary=recovery_boundary,
            recovery_max_event_id=fixed_recovery_boundary(),
            recovery_evidence_for=original_recovery_evidence,
            pending_start_results=pending_start_results,
            pending_capture_completions=capture_completions,
            pending_result_closes=result_closes,
            pending_file_observations=file_observations,
            pending_baselines=baselines, baseline_directory=directory_port,
            pending_read_results=pending_read_results, pending_read_business=pending_read_business,
            pending_read_ends=pending_read_ends, continuing_read_tickets=continuing_read_tickets,
            on_recovery_diagnostic=on_recovery_diagnostic,
            pending_media_results=media_results, file_executor=executor,
            stopper=stop_port,
            state_query=query_port,
            media=media,
            repair_margin_s=_repair_margin_s(declaration),
            listing_cache=listings,
            timelapse_deadlines=timelapse_deadlines,
            retry_gate=retry_gate,
            start_config=AttemptConfig(
                max_attempts=_device_attempts(
                    declaration, "recording", 3, "max_start_attempts"),
                timeout_s=_device_seconds(
                    declaration, "recording", "start_timeout_s", Decimal("10")),
                retry_interval_s=_device_seconds(
                    declaration, "recording", "start_retry_interval_s",
                    _DEFAULT_RETRY_INTERVAL_S)),
            stop_config=AttemptConfig(
                max_attempts=_device_attempts(
                    declaration, "recording", 3, "max_stop_attempts"),
                timeout_s=_device_seconds(
                    declaration, "recording", "stop_timeout_s", Decimal("10")),
                retry_interval_s=_device_seconds(
                    declaration, "recording", "stop_retry_interval_s",
                    _DEFAULT_RETRY_INTERVAL_S)),
            query_config=AttemptConfig(
                max_attempts=_device_attempts(declaration, "query", 3),
                timeout_s=_device_seconds(
                    declaration, "query", "timeout_s", Decimal("10")),
                retry_interval_s=_device_seconds(
                    declaration, "query", "retry_interval_s",
                    _DEFAULT_RETRY_INTERVAL_S)),
            residual_config=AttemptConfig(
                max_attempts=_device_attempts(declaration, "residual_stop", 3),
                timeout_s=_device_seconds(
                    declaration, "residual_stop", "timeout_s", Decimal("10")),
                retry_interval_s=_device_seconds(
                    declaration, "residual_stop", "retry_interval_s",
                    _DEFAULT_RETRY_INTERVAL_S)),
            check_config=AttemptConfig(
                max_attempts=_device_attempts(declaration, "result_check", 3),
                timeout_s=_device_seconds(
                    declaration, "result_check", "call_timeout_s", Decimal("10")),
                retry_interval_s=_device_seconds(
                    declaration, "result_check", "retry_interval_s",
                    _DEFAULT_RETRY_INTERVAL_S)),
        )
        runtime.recording_state = SessionRecordingState(runtime, anchors)
        return runtime

    return factory


def _media_flow_with(
    owned: Any,
    entry: Any,
    device_id: str,
    driver_id: str,
    tools: HostMediaTools,
    staging: Path,
    occurred_at: Callable[[], int],
    declaration: Mapping[str, Any],
    retry_gate: RetryWaitGate,
    monotonic: Callable[[], int],
    file_executor: FileTaskExecutor | None = None,
) -> MediaFlow | None:
    """按登记声明与设备声明构造媒体链；未声明读取能力时不装配。

    读取次数、无数据阈值、重拷上限和重试间隔采用本次设备配置；
    实际尝试票据由媒体入口可靠保存后交给读取端口。源端摘要读取
    在声明支持时按源设备文件经驱动端口取得。
    """
    try:
        read_port = port_for(entry, "read")
    except CapabilityNotDeclaredError:
        return None
    binding = DeviceBinding(device_id=device_id, driver_id=driver_id)
    digest_supported = entry.declaration.digest_supported
    digest_for = (
        _digest_reader_factory(owned, entry.driver, binding, entry.evidence)
        if digest_supported else None)
    return MediaFlow(
        owned=owned,
        roots=BoundDirectories(staging=staging),
        sessions=DriverReadSessions(owned, read_port, ticket=None),
        tools=tools,
        policy=MediaPolicy(repair_margin_s=_repair_margin_s(declaration)),
        occurred_at=occurred_at,
        digest_supported=digest_supported,
        digest_for=digest_for,
        retry_interval_s=_device_seconds(
            declaration, "copy", "retry_interval_s",
            _DEFAULT_RETRY_INTERVAL_S),
        monotonic_ns=monotonic,
        retry_gate=retry_gate,
        max_read_attempts=_device_attempts(declaration, "copy", 3, "max_read_attempts"),
        read_idle_timeout_s=_device_seconds(declaration, "copy", "read_idle_timeout_s", Decimal("10")),
        max_recopies=_device_attempts(declaration, "copy", 1, "max_recopies"),
        evidence=entry.evidence,
        file_executor=file_executor,
    )

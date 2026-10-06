"""拍摄推进运行时的会话级装配。

按设备声明与驱动登记项组装 CaptureRuntime：控制、停止、结果列举、
读取与源端摘要端口经 port_for 按静态声明取得，结果列举缺省按驱动
result 端口构造生产适配（DriverResultListing），注入 results 时整体
替换为部署端口。媒体链（读取会话、受管检查修复工具与修复余量）
随读取声明构造，未声明读取能力的驱动不装配媒体端口，处理行保持
等待。时钟异常的受限会话以 media_enabled=False 构造：不装配媒体
链，保守收场只停止并保存等待阶段。驱动未登记、设备未声明或生产
装配下结果列举能力未声明的动作本轮不推进，等待后续装配会话；异
常多录修复余量读取 devices.<id>.recording.repair_margin_s
（configuration.md#配置归属），默认 10 秒。
"""

from __future__ import annotations

import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

from camctl.capture.handlers import (
    CaptureRuntime,
    ObservedFile,
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
from camctl.capture.timelapse import CaptureWaitConfig
from camctl.devices.bindings import DeviceBinding
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

    async def repair(self, input: Any, output: Any) -> FileTaskResult:
        return await repair_media(
            input, output, self._roots, self._repair_request,
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

    async def list_files(self, action_id: int) -> tuple[ObservedFile, ...]:
        from camctl.devices.evidence import validate_observation

        contract = self._evidence.contract(
            _RESULT_LISTED_TYPE, _RESULT_LISTED_VERSION)
        request = ControlRequest(
            operation="result",
            binding=self._binding,
            params={"activity_id": str(action_id)},
        )
        result = await self._driver.list_results(request, _RESULT_BATCH_SIZE)
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
                observation, contract, expected_identity=str(action_id))
            entries.extend(
                _observed_file(entry)
                for entry in observation.data.get("entries", ()))
        return tuple(entries)


def _observed_file(entry: Any) -> ObservedFile:
    """把一条列举条目解释为候选产物文件；结构非法明确拒绝。"""
    if not isinstance(entry, Mapping) or not isinstance(
            entry.get("identity"), str) or not entry["identity"]:
        raise ValueError(f"列举条目缺少稳定文件身份: {entry!r}")
    locator = entry.get("locator")
    if not isinstance(locator, Mapping):
        raise ValueError(f"列举条目缺少定位结构: {entry!r}")
    size = entry.get("size_bytes")
    if size is not None and (isinstance(size, bool) or not isinstance(size, int)):
        raise ValueError(f"列举条目大小不是整数或空: {entry!r}")
    complete = entry.get("complete")
    if not isinstance(complete, bool):
        raise ValueError(f"列举条目未声明完整与否: {entry!r}")
    raw_kind = entry.get("kind", "other")
    try:
        kind = FileKind(raw_kind)
    except ValueError:
        kind = FileKind.OTHER
    original = entry.get("original_name")
    media = entry.get("media_type")
    if original is not None and not isinstance(original, str):
        raise ValueError(f"列举条目原始文件名不是文本: {entry!r}")
    if media is not None and not isinstance(media, str):
        raise ValueError(f"列举条目媒体类型不是文本: {entry!r}")
    return ObservedFile(
        identity=entry["identity"],
        locator=dict(locator),
        evidence=dict(entry),
        complete=complete,
        size_bytes=size,
        kind=kind,
        original_name=original,
        media_type=media,
    )


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
    margin_ms），读取只使用首次保存的事实；部署额外等待第一版不
    配置。定义缺少目标时长属于不可推进的任务形态，明确拒绝。
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
) -> Callable[[Any, str], CaptureRuntime | None]:
    """构造会话级拍摄推进工厂：按设备解析登记驱动端口并组装运行时。

    会话共享录像锚点表、结果列举缓存与媒体任务执行器；每个推进轮
    次按设备构造 CaptureRuntime。设备未声明、驱动未登记、控制能力
    未声明或结果列举能力未声明（生产装配时）返回 None，本轮不推进
    该设备的动作，保持已保存状态等待后续会话。results 未注入时按
    驱动 result 端口构造生产列举适配（DriverResultListing），注入时
    整体替换为部署提供的端口。wall_us 与 monotonic_ns 缺省使用真实
    系统钟，测试可注入受控读数。media_enabled=False 供时钟异常的受
    限会话构造：不装配媒体链，保守收场不启动拷贝、核验与修复。
    """

    anchors: dict[int, tuple[int, int]] = {}
    listings: dict[int, tuple[tuple, tuple]] = {}
    retry_gate = RetryWaitGate()
    roots = BoundDirectories(staging=staging)
    executor = FileTaskExecutor(Supervisor())
    tools = HostMediaTools(
        roots, executor, _SessionMediaOwner(),
        probe_request=probe_request, repair_request=repair_request)
    wall = wall_us if wall_us is not None \
        else (lambda: int(time.time() * 1_000_000))
    monotonic = monotonic_ns if monotonic_ns is not None else time.monotonic_ns

    def factory(owned: Any, device_id: str) -> CaptureRuntime | None:
        declaration = devices.get(device_id)
        if not isinstance(declaration, Mapping):
            return None
        driver_id = declaration.get("driver")
        entry = drivers.entry(driver_id) if isinstance(driver_id, str) else None
        if entry is None:
            return None
        try:
            control_port = port_for(entry, "control")
        except CapabilityNotDeclaredError:
            return None
        try:
            stop_port = port_for(entry, "stop")
        except CapabilityNotDeclaredError:
            stop_port = None
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
                wall, declaration, retry_gate, monotonic)
            if media_enabled else None)
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
            wait_config=wait_config,
            stopper=stop_port,
            media=media,
            repair_margin_s=_repair_margin_s(declaration),
            listing_cache=listings,
            retry_gate=retry_gate,
            stop_config=AttemptConfig(
                max_attempts=3, timeout_s=Decimal("10"),
                retry_interval_s=_device_seconds(
                    declaration, "recording", "stop_retry_interval_s",
                    _DEFAULT_RETRY_INTERVAL_S)),
            check_config=AttemptConfig(
                max_attempts=3, timeout_s=Decimal("10"),
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
) -> MediaFlow | None:
    """按登记声明与设备声明构造媒体链；未声明读取能力时不装配。

    读取尝试票据与修复成品扩展名保持第一版默认；源端摘要读取在
    声明支持时按源设备文件经驱动端口取得。读取重试间隔取自
    devices.<id>.copy.retry_interval_s，时间门槛随装配会话共享。
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
    )

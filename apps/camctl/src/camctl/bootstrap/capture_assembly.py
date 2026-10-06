"""拍摄推进运行时的会话级装配。

按设备声明与驱动登记项组装 CaptureRuntime：控制、停止、读取与源
端摘要端口经 port_for 按静态声明取得，媒体链（读取会话、受管检
查修复工具与修复余量）随读取声明构造，未声明读取能力的驱动不装
配媒体端口，处理行保持等待。结果列举端口暂无生产实现，由部署注
入（D5 驱动适配接入后补齐），因此本装配尚未接入 run 会话的默认
流程集合。驱动未登记或设备未声明的动作本轮不推进，等待后续装配
会话；异常多录修复余量读取 devices.<id>.recording.repair_margin_s
（configuration.md#配置归属），默认 10 秒。
"""

from __future__ import annotations

import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

from camctl.capture.handlers import (
    CaptureRuntime,
    ResultFilesPort,
    SessionRecordingState,
)
from camctl.capture.media import MediaPolicy
from camctl.capture.media_flow import (
    DriverReadSessions,
    MediaFlow,
    load_confirmed_source,
)
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
from camctl.outputs.copy import SourceDigest
from camctl.persistence.repositories.capture import CaptureRepository
from camctl.persistence.repositories.operations import OperationRepository
from camctl.persistence.repositories.scheduling import SchedulingRepository
from camctl.persistence.repositories.timelapse import TimelapseRepository
from camctl.scheduling.rules import LaunchWindow
from camctl.session.supervision import OwnedTask, Supervisor

__all__ = [
    "DriverDigestReader",
    "HostMediaTools",
    "session_capture_assembly",
]

#: 异常多录修复余量的默认秒数（camera-recording.md#配置归属）。
_DEFAULT_REPAIR_MARGIN_S = Decimal("10")

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


def _digest_reader_factory(
    owned: Any, driver: Any, binding: DeviceBinding, evidence: Any,
) -> Callable[[int], DriverDigestReader]:
    """构造按源设备文件解析的源端摘要读取工厂。"""

    def resolve(source_device_file_id: int) -> DriverDigestReader:
        source = load_confirmed_source(owned, source_device_file_id)
        return DriverDigestReader(
            driver, binding, evidence, source, str(source_device_file_id))

    return resolve


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
    results: ResultFilesPort,
    staging: Path,
    wait_config: Callable[[Mapping[str, Any]], Any],
    wall_us: Callable[[], int] | None = None,
    monotonic_ns: Callable[[], int] | None = None,
    probe_request: ProbeRequest | None = None,
    repair_request: RepairRequest | None = None,
) -> Callable[[Any, str], CaptureRuntime | None]:
    """构造会话级拍摄推进工厂：按设备解析登记驱动端口并组装运行时。

    会话共享录像锚点表与媒体任务执行器；每个推进轮次按设备构造
    CaptureRuntime。设备未声明、驱动未登记或控制能力未声明时返回
    None，本轮不推进该设备的动作，保持已保存状态等待后续会话。
    wall_us 与 monotonic_ns 缺省使用真实系统钟，测试可注入受控读数。
    """

    anchors: dict[int, tuple[int, int]] = {}
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
        media = _media_flow_with(
            owned, entry, device_id, str(driver_id), tools, staging, wall,
            declaration)
        runtime = CaptureRuntime(
            owned=owned,
            scheduling=SchedulingRepository(),
            operations=OperationRepository(),
            capture=CaptureRepository(),
            timelapse=TimelapseRepository(),
            driver=control_port,
            results=results,
            evidence=entry.evidence,
            wall_us=wall,
            monotonic_ns=monotonic,
            window_of=_window_of,
            wait_config=wait_config,
            stopper=stop_port,
            media=media,
            repair_margin_s=_repair_margin_s(declaration),
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
) -> MediaFlow | None:
    """按登记声明与设备声明构造媒体链；未声明读取能力时不装配。

    读取尝试票据与修复成品扩展名保持第一版默认；源端摘要读取在
    声明支持时按源设备文件经驱动端口取得。
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
    )

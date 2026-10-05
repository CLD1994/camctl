"""独立故障标记与锁内日志副本交付。

报告首次失败触发：先可靠保存故障标记（轮次、副本身份、进度），
日志线程在同一跨进程锁内追加触发记录并复制当前日志文件，释放
锁后经本地交接接口把副本移入 ready。标记存在性与内容分别判断；
存在即抑制另一份副本。失败清理只作用于本次仍在准备位置的副本，
启动清理只处理以前流程留下的副本与标记临时文件；两者都保留正
式标记，不重试、不补投，也不依赖状态库可用。
"""

from __future__ import annotations

import asyncio
import enum
import json
import os
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Protocol, Sequence

from camctl.host_files.handoff import (
    HandoffDirectories,
    PublishResult,
    PublishStage,
    ReadyName,
    publish_staged_file,
)
from camctl.host_files.io import DirectorySyncStage
from camctl.logging_runtime.service import LogRecord

__all__ = [
    "COPY_PREFIX",
    "MARKER_NAME",
    "CopyOutcome",
    "CopyReceipt",
    "CopyRequest",
    "DeleteOutcome",
    "FailureLogService",
    "MarkerCheck",
    "MarkerDelete",
    "MarkerPresence",
    "MarkerSave",
    "MarkerStage",
    "MarkerState",
    "MarkerStore",
    "SaveOutcome",
    "SweepResult",
    "copy_file_name",
    "deliver_failure_copy",
    "is_copy_file_name",
    "is_marker_temp_name",
    "marker_temp_file_name",
    "startup_sweep",
]

#: 正式故障标记的固定文件名；存在性即故障状态。
MARKER_NAME = "failure-marker.json"

#: 日志副本文件名的专用前缀；启动清理按此识别归属。
COPY_PREFIX = "log-copy-"

_BINARY = getattr(os, "O_BINARY", 0)

#: Windows 不提供目录同步句柄；目录一致性由文件系统自身保证。
_DIRECTORY_SYNC_SUPPORTED = sys.platform != "win32"


# ---- 专用命名规则 ----------------------------------------------------


def copy_file_name(round_id: str) -> str:
    """日志副本文件名：轮次标识内联，内容固定后不再改名。"""
    if not isinstance(round_id, str) or not round_id:
        raise ValueError(f"故障轮次标识必须是非空字符串: {round_id!r}")
    if "/" in round_id or "\\" in round_id or round_id in (".", ".."):
        raise ValueError(f"故障轮次标识不能用于文件名: {round_id!r}")
    return f"{COPY_PREFIX}{round_id}.log"


def is_copy_file_name(name: str) -> bool:
    if not isinstance(name, str):
        return False
    if not (name.startswith(COPY_PREFIX) and name.endswith(".log")):
        return False
    round_id = name[len(COPY_PREFIX):-len(".log")]
    return bool(round_id) and round_id not in (".", "..") and all(
        c not in "/\\:" for c in round_id)


def marker_temp_file_name(token: str) -> str:
    """标记保存流程的专用临时文件名；与正式标记明确区分。"""
    if not isinstance(token, str) or not token:
        raise ValueError(f"临时名标记必须是非空字符串: {token!r}")
    return f"{MARKER_NAME}.{token}.tmp"


def is_marker_temp_name(name: str) -> bool:
    if not isinstance(name, str):
        return False
    if not (name.startswith(MARKER_NAME + ".") and name.endswith(".tmp")):
        return False
    middle = name[len(MARKER_NAME) + 1:-len(".tmp")]
    return bool(middle) and all(c not in "/\\:" for c in middle)


# ---- 故障标记 --------------------------------------------------------


class MarkerStage(enum.Enum):
    """最后可靠确认的日志副本处理阶段。"""

    TRIGGERED = "triggered"
    COPIED = "copied"
    PUBLISHED = "published"


_STAGE_VALUES = frozenset(member.value for member in MarkerStage)


@dataclass(frozen=True)
class MarkerState:
    """从标记内容可靠解码的故障轮次事实。"""

    round_id: str
    copy_name: str
    stage: MarkerStage


class MarkerPresence(enum.Enum):
    ABSENT = "absent"
    EXISTS = "exists"
    CHECK_ERROR = "check_error"


@dataclass(frozen=True)
class MarkerCheck:
    presence: MarkerPresence
    state: MarkerState | None = None
    error: str | None = None


class SaveOutcome(enum.Enum):
    SAVED = "saved"
    FAILED_NOT_CREATED = "failed_not_created"
    PLACED_UNSYNCED = "placed_unsynced"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class MarkerSave:
    outcome: SaveOutcome
    error: str | None = None


class DeleteOutcome(enum.Enum):
    DELETED = "deleted"
    DELETE_UNSYNCED = "delete_unsynced"
    NOT_PRESENT = "not_present"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class MarkerDelete:
    outcome: DeleteOutcome
    error: str | None = None


def _describe(kind: str, failure: BaseException) -> str:
    return f"{kind}: {type(failure).__name__}: {failure}"


# 窄注入点：真实文件操作的系统调用边界；仅测试替换。
def _exists(path: Path) -> bool:
    return path.exists()


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _write_synced(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _BINARY, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)


def _replace(source: Path, target: Path) -> None:
    os.replace(source, target)


def _fsync_directory(path: Path) -> None:
    if not _DIRECTORY_SYNC_SUPPORTED:
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _unlink(path: Path) -> None:
    os.unlink(path)


def _listdir(path: Path) -> tuple[str, ...]:
    return tuple(os.listdir(path))


class MarkerStore:
    """staging/logs 下的故障标记存取；不依赖状态库。"""

    def __init__(self, logs_dir: Path) -> None:
        self._dir = Path(logs_dir)
        self._path = self._dir / MARKER_NAME

    @property
    def directory(self) -> Path:
        return self._dir

    def check(self) -> MarkerCheck:
        """存在性与内容分别判断；检查错误不当作不存在。"""
        try:
            present = _exists(self._path)
        except OSError as failure:
            return MarkerCheck(MarkerPresence.CHECK_ERROR,
                               error=_describe("stat_failed", failure))
        if not present:
            return MarkerCheck(MarkerPresence.ABSENT)
        try:
            raw = _read_bytes(self._path)
        except OSError as failure:
            # 已确认存在：内容不可读仍按有标记抑制。
            return MarkerCheck(MarkerPresence.EXISTS,
                               error=_describe("read_failed", failure))
        return MarkerCheck(MarkerPresence.EXISTS, state=_decode_state(raw))

    def create(self, round_id: str, copy_name: str) -> MarkerSave:
        """可靠保存新故障标记：临时写入同步后原子放置。"""
        payload = _encode_state(round_id, copy_name, MarkerStage.TRIGGERED)
        return self._save_payload(payload)

    def update_stage(self, stage: MarkerStage) -> MarkerSave:
        """原子替换保存进度；身份与副本身份保持不变。"""
        check = self.check()
        if check.presence is not MarkerPresence.EXISTS or check.state is None:
            return MarkerSave(SaveOutcome.FAILED_NOT_CREATED,
                              error="正式标记缺失或身份不可读，不能更新进度")
        payload = _encode_state(
            check.state.round_id, check.state.copy_name, stage)
        return self._save_payload(payload)

    def delete(self) -> MarkerDelete:
        """报告可靠恢复后删除标记并同步目录。"""
        try:
            _unlink(self._path)
        except FileNotFoundError:
            return MarkerDelete(DeleteOutcome.NOT_PRESENT)
        except InterruptedError as failure:
            return MarkerDelete(DeleteOutcome.UNKNOWN,
                                error=_describe("unlink_unknown", failure))
        except OSError as failure:
            return MarkerDelete(DeleteOutcome.FAILED,
                                error=_describe("unlink_failed", failure))
        try:
            _fsync_directory(self._dir)
        except OSError as failure:
            return MarkerDelete(DeleteOutcome.DELETE_UNSYNCED,
                                error=_describe("dir_sync_failed", failure))
        return MarkerDelete(DeleteOutcome.DELETED)

    def _save_payload(self, payload: bytes) -> MarkerSave:
        temp = self._dir / marker_temp_file_name(secrets.token_hex(8))
        try:
            _write_synced(temp, payload)
        except OSError as failure:
            return MarkerSave(SaveOutcome.FAILED_NOT_CREATED,
                              error=_describe("temp_write_failed", failure))
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            _replace(temp, self._path)
        except OSError as failure:
            return MarkerSave(
                SaveOutcome.UNKNOWN, error=_describe("replace_failed", failure))
        try:
            _fsync_directory(self._dir)
        except OSError as failure:
            return MarkerSave(SaveOutcome.PLACED_UNSYNCED,
                              error=_describe("dir_sync_failed", failure))
        return MarkerSave(SaveOutcome.SAVED)


def _encode_state(round_id: str, copy_name: str, stage: MarkerStage) -> bytes:
    return json.dumps({
        "round_id": round_id, "copy_name": copy_name, "stage": stage.value,
    }, separators=(",", ":")).encode()


def _decode_state(raw: bytes) -> MarkerState | None:
    """按标记 schema 精确解码；任何偏差都按内容无效处理。"""
    try:
        document = json.loads(raw.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    round_id = document.get("round_id")
    copy_name = document.get("copy_name")
    stage = document.get("stage")
    if (not isinstance(round_id, str) or not round_id
            or not is_copy_file_name(copy_name if isinstance(copy_name, str) else "")
            or stage not in _STAGE_VALUES):
        return None
    return MarkerState(round_id, copy_name, MarkerStage(stage))


# ---- 副本交付编排 ----------------------------------------------------


class CopyChannel(Protocol):
    """锁内追加触发记录并复制当前日志文件的端口。"""

    def copy_with_write(self, record: LogRecord,
                        destination: Path) -> tuple: ...


class CopyPublisher(Protocol):
    """本地交接接口：把 staging 树内文件原子移入 ready。"""

    async def __call__(self, source: Path, directories: HandoffDirectories,
                       target: ReadyName) -> PublishResult: ...


@dataclass(frozen=True)
class CopyRequest:
    """一次日志副本交付的输入；触发记录在复制前绑定。"""

    trigger: LogRecord
    staging_root: Path
    ready_dir: Path
    channel: CopyChannel
    marker: MarkerStore


class CopyOutcome(enum.Enum):
    PUBLISHED = "published"
    SUPPRESSED = "suppressed"
    MARKER_UNAVAILABLE = "marker_unavailable"
    MARKER_CREATE_FAILED = "marker_create_failed"
    COPY_FAILED = "copy_failed"
    COPY_UNKNOWN = "copy_unknown"


@dataclass(frozen=True)
class CopyReceipt:
    outcome: CopyOutcome
    round_id: str | None = None
    copy_name: str | None = None
    stage: MarkerStage | None = None
    error: str | None = None


async def deliver_failure_copy(
    request: CopyRequest, *, publish: CopyPublisher = publish_staged_file,
) -> CopyReceipt:
    """执行一次日志副本交付：标记先行、锁内写入复制、交接发布。

    任何一步失败或结果未知都停止后续步骤并保留实际分类；进度保
    存失败不阻止仍在进行的交付。失败清理只删除仍在准备位置的本
    次副本，正式标记始终保留。
    """
    check = await asyncio.to_thread(request.marker.check)
    if check.presence is MarkerPresence.CHECK_ERROR:
        return CopyReceipt(CopyOutcome.MARKER_UNAVAILABLE, error=check.error)
    if check.presence is MarkerPresence.EXISTS:
        state = check.state
        return CopyReceipt(
            CopyOutcome.SUPPRESSED,
            round_id=state.round_id if state else None,
            copy_name=state.copy_name if state else None,
            stage=state.stage if state else None,
            error=check.error)

    round_id = secrets.token_hex(16)
    name = copy_file_name(round_id)
    save = await asyncio.to_thread(
        request.marker.create, round_id, name)
    if save.outcome is not SaveOutcome.SAVED:
        return CopyReceipt(CopyOutcome.MARKER_CREATE_FAILED, round_id, name,
                           None, save.error)

    logs_dir = request.marker.directory
    destination = logs_dir / name
    write_result, copy_error = await asyncio.to_thread(
        request.channel.copy_with_write, request.trigger, destination)
    if not write_result.appended or copy_error is not None:
        cleanup = await asyncio.to_thread(
            _cleanup_staging_copy, destination)
        return CopyReceipt(
            CopyOutcome.COPY_FAILED, round_id, name, None,
            "; ".join(part for part in (copy_error,
                                        write_result.append_error, cleanup)
                      if part))

    await asyncio.to_thread(request.marker.update_stage, MarkerStage.COPIED)
    try:
        file_result = await publish(
            destination, HandoffDirectories(request.staging_root,
                                            request.ready_dir),
            ReadyName(name))
    except Exception as failure:
        return CopyReceipt(CopyOutcome.COPY_UNKNOWN, round_id, name,
                           MarkerStage.COPIED,
                           _describe("publish_failed", failure))
    if file_result.stage is PublishStage.UNKNOWN:
        return CopyReceipt(CopyOutcome.COPY_UNKNOWN, round_id, name,
                           MarkerStage.COPIED, file_result.error)
    if file_result.stage is not PublishStage.MOVED:
        cleanup = await asyncio.to_thread(
            _cleanup_staging_copy, destination)
        return CopyReceipt(
            CopyOutcome.COPY_FAILED, round_id, name, MarkerStage.COPIED,
            "; ".join(part for part in (file_result.error, cleanup)
                      if part))
    if not _handoff_confirmed(file_result):
        # 已移入 ready 的文件保持不撤回；同步未确认不宣称发布完成。
        return CopyReceipt(CopyOutcome.COPY_UNKNOWN, round_id, name,
                           MarkerStage.COPIED, file_result.error)

    await asyncio.to_thread(request.marker.update_stage, MarkerStage.PUBLISHED)
    return CopyReceipt(CopyOutcome.PUBLISHED, round_id, name,
                       MarkerStage.PUBLISHED)


def _handoff_confirmed(result: PublishResult) -> bool:
    return (result.source_removed is True and result.error is None
            and result.directory in (DirectorySyncStage.SYNCED,
                                     DirectorySyncStage.UNSUPPORTED))


def _cleanup_staging_copy(path: Path) -> str | None:
    """删除仍在准备位置的本次副本；删除失败保留错误不重试。"""
    try:
        _unlink(path)
    except FileNotFoundError:
        return None
    except OSError as failure:
        return _describe("copy_cleanup_failed", failure)
    return None


# ---- 同运行暂停服务 --------------------------------------------------


class FailureLogService:
    """同一次运行内的日志副本交付服务。

    标记检查或创建错误后暂停同一故障的后续尝试；报告可靠恢复时
    删除标记并解除暂停，此后新故障重新取得首次尝试机会。
    """

    def __init__(self, marker: MarkerStore, *,
                 deliver: Callable[[CopyRequest], Awaitable[CopyReceipt]]
                 = deliver_failure_copy) -> None:
        self._marker = marker
        self._deliver = deliver
        self._suspended = False

    async def on_report_failure(self, request: CopyRequest) -> CopyReceipt:
        if self._suspended:
            return CopyReceipt(CopyOutcome.SUPPRESSED)
        receipt = await self._deliver(request)
        if receipt.outcome in (CopyOutcome.MARKER_UNAVAILABLE,
                               CopyOutcome.MARKER_CREATE_FAILED):
            self._suspended = True
        return receipt

    async def on_report_recovered(self) -> MarkerDelete:
        """报告本地处理可靠恢复：结束本轮故障并解除暂停。"""
        self._suspended = False
        return await asyncio.to_thread(self._marker.delete)


# ---- run 启动清理 ----------------------------------------------------

@dataclass(frozen=True)
class SweepResult:
    error: str | None = None
    failures: tuple[tuple[str, str], ...] = ()


def startup_sweep(logs_dir: Path, *,
                  active_names: Sequence[str] = ()) -> SweepResult:
    """取得会话锁后的一次性检查：清理以前的日志副本与标记临时文件。

    保留正式标记、本次运行正在处理的副本及无法确认归属的文件；
    目录检查失败保留错误，单个删除失败不阻止其他文件。
    """
    active = frozenset(active_names)
    try:
        names = _listdir(Path(logs_dir))
    except OSError as failure:
        return SweepResult(error=_describe("listdir_failed", failure))
    failures = []
    for name in names:
        if name in active or name == MARKER_NAME:
            continue
        if not (is_copy_file_name(name) or is_marker_temp_name(name)):
            continue
        try:
            _unlink(Path(logs_dir) / name)
        except FileNotFoundError:
            continue
        except OSError as failure:
            failures.append((name, _describe("unlink_failed", failure)))
    return SweepResult(failures=tuple(failures))

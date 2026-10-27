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
import stat
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Protocol, Sequence

from camctl.host_files.handoff import (
    HandoffDirectories,
    PublishResult,
    PublishStage,
    ReadyName,
    publish_staged_file_sync,
    publish_staged_file,
)
from camctl.host_files.io import DirectorySyncStage
from camctl.logging_runtime.models import LogFileStage
from camctl.logging_runtime.service import LogRecord

if TYPE_CHECKING:
    from camctl.logging_runtime.clh_adapter import WriteResult

__all__ = [
    "COPY_PREFIX",
    "MARKER_NAME",
    "CopyOutcome",
    "CopyLocation",
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
    "LogFileStage",
    "RunFileOwnership",
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


class RunFileOwnership:
    """以固定空间标识本次 run 已登记的副本与标记临时名。"""

    _SEQUENCE_MAX = 0xFFFFFFFF

    def __init__(self) -> None:
        self._namespace = secrets.token_hex(12)
        self._sequence = 0
        self._lock = threading.Lock()

    def new_token(self) -> str:
        """在路径使用前分配唯一、可由此 owner 识别的 token。"""
        with self._lock:
            if self._sequence == self._SEQUENCE_MAX:
                raise RuntimeError("单次 run 的日志路径序号已耗尽")
            self._sequence += 1
            return f"{self._namespace}{self._sequence:08x}"

    def owns_name(self, name: str) -> bool:
        """常量空间判断专用副本名或临时名是否由本次分配。"""
        if is_copy_file_name(name):
            token = name[len(COPY_PREFIX):-len(".log")]
        elif is_marker_temp_name(name):
            token = name[len(MARKER_NAME) + 1:-len(".tmp")]
        else:
            return False
        if (len(token) != 32 or token != token.lower()
                or not token.startswith(self._namespace)):
            return False
        try:
            sequence = int(token[24:], 16)
        except ValueError:
            return False
        with self._lock:
            return 0 < sequence <= self._sequence


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
    marker_check: MarkerCheck | None = None
    temp_name: str | None = None
    temp_cleanup: MarkerDelete | None = None


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


def _exception_description(
    failure: BaseException,
    *,
    seen: frozenset[int] = frozenset(),
) -> str:
    if id(failure) in seen:
        return f"{type(failure).__name__}: <异常链循环>"
    seen = seen | {id(failure)}
    details = [f"{type(failure).__name__}: {failure}"]
    for note in getattr(failure, "__notes__", ()):
        details.append(f"note={note}")
    if isinstance(failure, BaseExceptionGroup):
        for index, member in enumerate(failure.exceptions):
            details.append(
                f"group[{index}]={_exception_description(member, seen=seen)}")
    cause = failure.__cause__
    if cause is None and not failure.__suppress_context__:
        cause = failure.__context__
    if cause is not None:
        details.append(f"cause={_exception_description(cause, seen=seen)}")
    return "; ".join(details)


def _describe(kind: str, failure: BaseException) -> str:
    return f"{kind}: {_exception_description(failure)}"


_TEMP_CLOSE_UNKNOWN_ATTR = "_camctl_marker_temp_close_unknown"
_DIRECTORY_SYNC_CONFIRMED_ATTR = "_camctl_directory_sync_confirmed"


def _add_exception_note(failure: BaseException, note: str) -> None:
    failure.add_note(note)


def _mark_temp_close_unknown(
    failure: BaseException, close_error: BaseException,
) -> None:
    setattr(failure, _TEMP_CLOSE_UNKNOWN_ATTR, True)
    _add_exception_note(
        failure,
        "marker_temp_close_unknown: "
        f"{_exception_description(close_error)}",
    )


def _temp_close_unknown(failure: BaseException) -> bool:
    return bool(getattr(failure, _TEMP_CLOSE_UNKNOWN_ATTR, False))


def _write_result_from_exception(failure: BaseException):
    """只读取原异常上由 FileChannel 写入的 typed 结果事实。"""
    from camctl.logging_runtime.clh_adapter import WriteResult

    result = getattr(failure, "write_result", None)
    if isinstance(result, WriteResult):
        return result
    if isinstance(failure, BaseExceptionGroup):
        for member in failure.exceptions:
            result = _write_result_from_exception(member)
            if result is not None:
                return result
    return None


def _copy_handles_confirmed(result: WriteResult) -> bool:
    return (result.copy_handles_closed is True
            and result.copy_source_close_error is None
            and result.copy_target_close_error is None)


def _copy_exception_cleanup_is_owned(
    request: CopyRequest, name: str, destination: Path,
    result: WriteResult | None,
) -> bool:
    """只为本次预先分配、尚在 marker 目录的已关闭副本授权清理。"""
    if result is None or not _copy_handles_confirmed(result):
        return False
    try:
        return (
            destination == Path(request.marker.directory) / name
            and request.marker.ownership.owns_name(name)
        )
    except BaseException:
        # 查询归属本身不能证明时保守保留文件，并保留主异常。
        return False


def _copy_close_diagnostics(result: WriteResult) -> tuple[str, ...]:
    facts = []
    if result.copy_handles_closed is not True:
        facts.append(
            "副本源与目标句柄关闭事实未明确确认"
            if result.copy_handles_closed is None else
            "副本源或目标句柄关闭失败／结果未知"
        )
    for side, error in (
            ("源", result.copy_source_close_error),
            ("目标", result.copy_target_close_error)):
        if error is not None:
            facts.append(f"副本{side}句柄关闭: {error}")
    return tuple(facts)


# 窄注入点：真实文件操作的系统调用边界；仅测试替换。
def _exists(path: Path) -> bool:
    return path.exists()


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _write_synced(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _BINARY, 0o600)
    primary: BaseException | None = None
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written == 0:
                raise OSError("标记临时文件写入无进展")
            view = view[written:]
        os.fsync(fd)
    except BaseException as failure:
        primary = failure

    close_error: BaseException | None = None
    try:
        os.close(fd)
    except BaseException as failure:
        close_error = failure

    if close_error is not None:
        if primary is None:
            _mark_temp_close_unknown(close_error, close_error)
            raise close_error
        if isinstance(primary, OSError) and not isinstance(close_error, Exception):
            _mark_temp_close_unknown(close_error, close_error)
            _add_exception_note(
                close_error,
                "marker_temp_prior_failure: "
                f"{_exception_description(primary)}; temp_name={path.name}; "
                "outcome=failed_not_created; temp_cleanup=not_attempted",
            )
            raise close_error from None
        _mark_temp_close_unknown(primary, close_error)
        if (isinstance(primary, OSError)
                and isinstance(close_error, Exception)
                and not isinstance(close_error, OSError)):
            group = ExceptionGroup(
                "标记临时文件写入与关闭均失败",
                [primary, close_error],
            )
            _mark_temp_close_unknown(group, close_error)
            raise group from None
    if primary is not None:
        raise primary


def _replace(source: Path, target: Path) -> None:
    os.replace(source, target)


def _fsync_directory(path: Path) -> OSError | None:
    """同步目录并独立报告已成功同步后的 close OSError。"""
    if not _DIRECTORY_SYNC_SUPPORTED:
        return None
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    sync_error: BaseException | None = None
    try:
        os.fsync(fd)
    except BaseException as failure:
        sync_error = failure

    close_error: BaseException | None = None
    try:
        os.close(fd)
    except BaseException as failure:
        close_error = failure

    if sync_error is not None:
        if close_error is not None:
            if (isinstance(sync_error, OSError)
                    and not isinstance(close_error, Exception)):
                _add_exception_note(
                    close_error,
                    "directory_sync_prior_failure: "
                    f"{_exception_description(sync_error)}; directory={path}; "
                    "directory sync not confirmed; directory close unknown",
                )
                raise close_error from None
            _add_exception_note(
                sync_error,
                "directory_close_unknown: "
                f"{_exception_description(close_error)}",
            )
            if (isinstance(sync_error, OSError)
                    and isinstance(close_error, Exception)
                    and not isinstance(close_error, OSError)):
                raise ExceptionGroup(
                    "目录同步与关闭均失败", [sync_error, close_error]
                ) from None
        raise sync_error
    if close_error is None:
        return None
    setattr(close_error, _DIRECTORY_SYNC_CONFIRMED_ATTR, True)
    _add_exception_note(
        close_error,
        "directory_fsync_confirmed: directory data sync completed before close",
    )
    if isinstance(close_error, OSError):
        return close_error
    raise close_error


def _unlink(path: Path) -> None:
    os.unlink(path)


def _cleanup_marker_temp(
    path: Path, *, save_outcome: SaveOutcome,
) -> MarkerDelete:
    """仅结果化本次路径的标准 unlink 结果；程序异常继续传播。"""
    try:
        _unlink(path)
    except FileNotFoundError:
        return MarkerDelete(DeleteOutcome.NOT_PRESENT)
    except InterruptedError as failure:
        return MarkerDelete(
            DeleteOutcome.UNKNOWN,
            error=_describe("temp_cleanup_unknown", failure),
        )
    except OSError as failure:
        return MarkerDelete(
            DeleteOutcome.FAILED,
            error=_describe("temp_cleanup_failed", failure),
        )
    except BaseException as failure:
        _add_exception_note(
            failure,
            "marker_save_facts: "
            f"outcome={save_outcome.value}; temp_name={path.name}; "
            "temp_cleanup=unknown",
        )
        raise
    return MarkerDelete(DeleteOutcome.DELETED)


def _listdir(path: Path) -> tuple[str, ...]:
    return tuple(os.listdir(path))


def _lstat(path: Path):
    return Path(path).lstat()


class MarkerStore:
    """staging/logs 下的故障标记存取；不依赖状态库。"""

    def __init__(self, logs_dir: Path, *,
                 ownership: RunFileOwnership | None = None) -> None:
        self._dir = Path(logs_dir)
        self._path = self._dir / MARKER_NAME
        self._lock = threading.RLock()
        self._ownership = ownership or RunFileOwnership()

    @property
    def directory(self) -> Path:
        return self._dir

    @property
    def ownership(self) -> RunFileOwnership:
        return self._ownership

    def check(self) -> MarkerCheck:
        """存在性与内容分别判断；检查错误不当作不存在。"""
        with self._lock:
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
            state = _decode_state(raw)
            if state is None:
                # 只记录无效类别，不复制或转储标记原始字节。
                return MarkerCheck(
                    MarkerPresence.EXISTS,
                    error="marker_content_invalid: 标记内容不符合已知结构",
                )
            return MarkerCheck(MarkerPresence.EXISTS, state=state)

    def create(self, round_id: str, copy_name: str) -> MarkerSave:
        """可靠保存新故障标记：临时写入同步后原子放置。"""
        with self._lock:
            payload = _encode_state(round_id, copy_name, MarkerStage.TRIGGERED)
            return self._save_payload(payload)

    def update_stage(
        self,
        stage: MarkerStage,
        *,
        expected_round_id: str | None = None,
        expected_copy_name: str | None = None,
    ) -> MarkerSave:
        """原子替换保存进度；身份与副本身份保持不变。"""
        with self._lock:
            check = self.check()
            if check.presence is not MarkerPresence.EXISTS or check.state is None:
                return MarkerSave(SaveOutcome.FAILED_NOT_CREATED,
                                  error="正式标记缺失或身份不可读，不能更新进度",
                                  marker_check=check)
            if (expected_round_id is not None
                    and check.state.round_id != expected_round_id):
                return MarkerSave(
                    SaveOutcome.FAILED_NOT_CREATED,
                    error=("marker_round_mismatch: 当前正式标记属于另一轮次，"
                           "旧进度未保存"),
                )
            if (expected_copy_name is not None
                    and check.state.copy_name != expected_copy_name):
                return MarkerSave(
                    SaveOutcome.FAILED_NOT_CREATED,
                    error=("marker_copy_mismatch: 当前正式标记属于另一副本，"
                           "旧进度未保存"),
                )
            payload = _encode_state(
                check.state.round_id, check.state.copy_name, stage)
            return self._save_payload(payload)

    def delete(self) -> MarkerDelete:
        """报告可靠恢复后删除标记并同步目录。"""
        with self._lock:
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
                close_error = _fsync_directory(self._dir)
            except OSError as failure:
                return MarkerDelete(DeleteOutcome.DELETE_UNSYNCED,
                                    error=_describe("dir_sync_failed", failure))
            except BaseException as failure:
                if getattr(failure, _DIRECTORY_SYNC_CONFIRMED_ATTR, False):
                    _add_exception_note(
                        failure,
                        "marker_delete_facts: outcome=deleted; "
                        "marker absent; directory sync confirmed; "
                        "directory close unknown",
                    )
                else:
                    _add_exception_note(
                        failure,
                        "marker_delete_facts: outcome=delete_unsynced; "
                        "unlink confirmed; marker absent; "
                        "directory sync not confirmed",
                    )
                raise
            if close_error is not None:
                return MarkerDelete(
                    DeleteOutcome.DELETED,
                    error=_describe("dir_close_failed_after_sync", close_error),
                )
            return MarkerDelete(DeleteOutcome.DELETED)

    def _save_payload(self, payload: bytes) -> MarkerSave:
        temp = self._dir / marker_temp_file_name(self._ownership.new_token())
        try:
            _write_synced(temp, payload)
        except BaseException as failure:
            return self._finish_failed_save(
                temp, SaveOutcome.FAILED_NOT_CREATED,
                "temp_write_failed", failure,
            )
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except BaseException as failure:
            return self._finish_failed_save(
                temp, SaveOutcome.UNKNOWN, "replace_failed", failure,
            )
        try:
            _replace(temp, self._path)
        except BaseException as failure:
            return self._finish_failed_save(
                temp, SaveOutcome.UNKNOWN, "replace_failed", failure,
            )
        temp_cleanup = MarkerDelete(DeleteOutcome.NOT_PRESENT)
        try:
            close_error = _fsync_directory(self._dir)
        except OSError as failure:
            return MarkerSave(
                SaveOutcome.PLACED_UNSYNCED,
                error=_describe("dir_sync_failed", failure),
                temp_name=temp.name,
                temp_cleanup=temp_cleanup,
            )
        except BaseException as failure:
            if getattr(failure, _DIRECTORY_SYNC_CONFIRMED_ATTR, False):
                _add_exception_note(
                    failure,
                    "marker_save_facts: outcome=saved; formal marker placed; "
                    f"temp_name={temp.name}; temp_cleanup=not_present",
                )
            else:
                _add_exception_note(
                    failure,
                    "marker_save_facts: outcome=placed_unsynced; "
                    "formal marker placed; directory sync not confirmed; "
                    f"temp_name={temp.name}; temp_cleanup=not_present",
                )
            raise
        error = (_describe("dir_close_failed_after_sync", close_error)
                 if close_error is not None else None)
        return MarkerSave(
            SaveOutcome.SAVED,
            error=error,
            temp_name=temp.name,
            temp_cleanup=temp_cleanup,
        )

    def _finish_failed_save(
        self,
        temp: Path,
        outcome: SaveOutcome,
        kind: str,
        failure: BaseException,
    ) -> MarkerSave:
        temp_name = temp.name
        if _temp_close_unknown(failure):
            _add_exception_note(
                failure,
                "marker_save_facts: "
                f"outcome={outcome.value}; temp_name={temp_name}; "
                "temp_cleanup=not_attempted",
            )
            if isinstance(failure, OSError):
                return MarkerSave(
                    outcome,
                    error=_describe(kind, failure),
                    temp_name=temp_name,
                    temp_cleanup=None,
                )
            raise failure

        try:
            cleanup = _cleanup_marker_temp(temp, save_outcome=outcome)
        except BaseException as cleanup_failure:
            facts = (
                f"marker_save_facts: outcome={outcome.value}; "
                f"temp_name={temp_name}; temp_cleanup=unknown"
            )
            if isinstance(failure, OSError):
                if isinstance(cleanup_failure, Exception):
                    group = ExceptionGroup(
                        "标记保存与临时文件清理均失败",
                        [failure, cleanup_failure],
                    )
                    _add_exception_note(group, facts)
                    raise group from None
                _add_exception_note(
                    cleanup_failure,
                    f"{facts}; cleanup_exception="
                    f"{_exception_description(cleanup_failure)}; "
                    f"primary_save_error={_exception_description(failure)}",
                )
                raise cleanup_failure from None
            _add_exception_note(
                failure,
                f"{facts}; cleanup_exception="
                f"{_exception_description(cleanup_failure)}",
            )
            raise failure

        if isinstance(failure, OSError):
            return MarkerSave(
                outcome,
                error=_describe(kind, failure),
                temp_name=temp_name,
                temp_cleanup=cleanup,
            )

        _add_exception_note(
            failure,
            "marker_save_facts: "
            f"outcome={outcome.value}; temp_name={temp_name}; "
            f"temp_cleanup={cleanup.outcome.value}; "
            f"temp_cleanup_error={cleanup.error or 'none'}",
        )
        raise failure


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


class CopyLocation(enum.Enum):
    """本次副本最后可证明的位置，不推断文件是否存在。"""

    NOT_STARTED = "not_started"
    STAGING = "staging"
    PUBLISHING = "publishing"
    READY = "ready"
    UNKNOWN = "unknown"
    ABSENT = "absent"


@dataclass(frozen=True)
class CopyReceipt:
    outcome: CopyOutcome
    round_id: str | None = None
    copy_name: str | None = None
    stage: MarkerStage | None = None
    error: str | None = None
    marker_check: MarkerCheck | None = None
    marker_create: MarkerSave | None = None
    marker_progress: tuple[tuple[MarkerStage, MarkerSave], ...] = ()
    write_result: WriteResult | None = None
    copy_error: str | None = None
    publish_error: str | None = None
    cleanup_error: str | None = None
    file_stage: LogFileStage | None = None
    publish_result: PublishResult | None = None
    copy_location: CopyLocation | None = None
    cleanup_attempted: bool = False


@dataclass
class _CopyDeliveryFacts:
    """一次交付的中央可变事实，最终只生成不可变 CopyReceipt。"""

    round_id: str | None = None
    copy_name: str | None = None
    file_stage: LogFileStage | None = None
    marker_check: MarkerCheck | None = None
    marker_create: MarkerSave | None = None
    marker_progress: list[tuple[MarkerStage, MarkerSave]] = field(
        default_factory=list)
    write_result: WriteResult | None = None
    copy_error: str | None = None
    publish_result: PublishResult | None = None
    publish_error: str | None = None
    copy_location: CopyLocation = CopyLocation.NOT_STARTED
    cleanup_attempted: bool = False
    cleanup_error: str | None = None

    def receipt(self, outcome: CopyOutcome, *,
                stage: MarkerStage | None = None,
                error: str | None = None) -> CopyReceipt:
        return CopyReceipt(
            outcome=outcome,
            round_id=self.round_id,
            copy_name=self.copy_name,
            stage=stage,
            error=error,
            marker_check=self.marker_check,
            marker_create=self.marker_create,
            marker_progress=tuple(self.marker_progress),
            write_result=self.write_result,
            copy_error=self.copy_error,
            publish_error=self.publish_error,
            cleanup_error=self.cleanup_error,
            file_stage=self.file_stage,
            publish_result=self.publish_result,
            copy_location=self.copy_location,
            cleanup_attempted=self.cleanup_attempted,
        )


def _copy_receipt_from_exception(failure: BaseException) -> CopyReceipt | None:
    """只接受原异常或异常组上明确交付的不可变回执。"""
    receipt = getattr(failure, "copy_receipt", None)
    if isinstance(receipt, CopyReceipt):
        return receipt
    if isinstance(failure, BaseExceptionGroup):
        for member in failure.exceptions:
            receipt = _copy_receipt_from_exception(member)
            if receipt is not None:
                return receipt
    return None


def _attach_copy_receipt(
    failure: BaseException, receipt: CopyReceipt,
) -> None:
    """把统一阶段事实附在实际传播的同一个异常对象上。"""
    try:
        setattr(failure, "copy_receipt", receipt)
    except (AttributeError, TypeError):
        _add_exception_note(
            failure,
            "camctl_copy_receipt_unavailable: 副本阶段事实已形成但无法附加",
        )


class _LoggingOperationStopped(Exception):
    def __init__(self, stage: LogFileStage) -> None:
        super().__init__(stage.value)
        self.stage = stage


async def _blocking_call(
    stage: LogFileStage,
    operation: Callable[[], Any],
    *,
    operation_runner: Callable[..., Awaitable[Any]] | None,
    stop_requested: Callable[[], bool] | None,
    required: bool = False,
) -> Any:
    if not required and stop_requested is not None and stop_requested():
        raise _LoggingOperationStopped(stage)
    if operation_runner is not None:
        return await operation_runner(stage, operation, required=required)
    return await asyncio.to_thread(operation)


async def _cleanup_staging_once(
    facts: _CopyDeliveryFacts,
    request: CopyRequest,
    destination: Path,
    *,
    operation_runner: Callable[..., Awaitable[Any]] | None,
    stop_requested: Callable[[], bool] | None,
    receipt_outcome: CopyOutcome,
    receipt_stage: MarkerStage | None = None,
    receipt_error: str | None = None,
    primary: BaseException | None = None,
) -> None:
    """只为关闭明确且仍归本次准备位置的副本尝试一次清理。"""
    if (facts.cleanup_attempted
            or facts.copy_location is not CopyLocation.STAGING
            or facts.copy_name is None
            or not _copy_exception_cleanup_is_owned(
                request, facts.copy_name, destination, facts.write_result)):
        return

    # 在提交之前登记，任何后续出口都不会重复清理。
    facts.cleanup_attempted = True
    cleanup_failure: BaseException | None = None
    try:
        cleanup_error = await _blocking_call(
            LogFileStage.COPY_CLEANUP,
            lambda: _cleanup_staging_copy(destination),
            operation_runner=operation_runner,
            stop_requested=stop_requested,
            required=True,
        )
    except asyncio.CancelledError as failure:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            # 新取消仍须传播；默认 service owner 会让已开始清理继续收场。
            raise
        cleanup_failure = failure
    except BaseException as failure:
        cleanup_failure = failure

    if cleanup_failure is not None:
        facts.cleanup_error = _describe(
            "copy_cleanup_failed", cleanup_failure)
        if primary is None:
            # 结果已分类但没有仍传播的原异常时，异常出口由清理阶段负责。
            facts.file_stage = LogFileStage.COPY_CLEANUP
        receipt = facts.receipt(
            receipt_outcome, stage=receipt_stage, error=receipt_error)

        if primary is None:
            _attach_copy_receipt(cleanup_failure, receipt)
            raise cleanup_failure
        if isinstance(primary, OSError):
            if isinstance(cleanup_failure, Exception):
                failure_group = ExceptionGroup(
                    "日志副本操作与必要清理均失败",
                    [primary, cleanup_failure],
                )
                _attach_copy_receipt(failure_group, receipt)
                raise failure_group
            _add_exception_note(
                cleanup_failure,
                "copy_primary_failure: "
                f"{_exception_description(primary)}",
            )
            _attach_copy_receipt(cleanup_failure, receipt)
            raise cleanup_failure from primary

        _add_exception_note(
            primary,
            "copy_cleanup_failed: attempted=true; location=staging; "
            f"error={facts.cleanup_error}",
        )
        _attach_copy_receipt(primary, receipt)
        raise primary

    facts.cleanup_error = cleanup_error
    if cleanup_error is None:
        # 成功 unlink 与可靠 FileNotFoundError 都证明准备位置已无本次文件。
        facts.copy_location = CopyLocation.ABSENT
    elif primary is not None:
        _add_exception_note(
            primary,
            "copy_cleanup_failed: "
            f"attempted=true; location=staging; error={cleanup_error}",
        )


async def deliver_failure_copy(
    request: CopyRequest, *, publish: CopyPublisher = publish_staged_file,
    operation_runner: Callable[..., Awaitable[Any]] | None = None,
    stop_requested: Callable[[], bool] | None = None,
    on_write_result: Callable[[WriteResult], None] | None = None,
) -> CopyReceipt:
    """执行一次日志副本交付：标记先行、锁内写入复制、交接发布。

    任何一步失败或结果未知都停止后续步骤并保留实际分类；进度保
    存失败不阻止仍在进行的交付。失败清理只删除仍在准备位置的本
    次副本，正式标记始终保留。
    """
    facts = _CopyDeliveryFacts()

    def exception_receipt(
        failure: BaseException,
        *,
        outcome: CopyOutcome = CopyOutcome.COPY_UNKNOWN,
        stage: MarkerStage | None = None,
        error: str | None = None,
    ) -> CopyReceipt:
        receipt = facts.receipt(
            outcome, stage=stage,
            error=error or _describe("copy_operation_failed", failure),
        )
        _attach_copy_receipt(failure, receipt)
        return receipt

    facts.file_stage = LogFileStage.MARKER_CHECK
    try:
        check = await _blocking_call(
            LogFileStage.MARKER_CHECK, request.marker.check,
            operation_runner=operation_runner,
            stop_requested=stop_requested)
    except _LoggingOperationStopped as stopped:
        return facts.receipt(
            CopyOutcome.COPY_UNKNOWN,
            error=f"日志交付在 {stopped.stage.value} 开始前停止",
        )
    except BaseException as failure:
        exception_receipt(failure)
        raise
    facts.marker_check = check
    if check.presence is MarkerPresence.CHECK_ERROR:
        return facts.receipt(
            CopyOutcome.MARKER_UNAVAILABLE, error=check.error)
    if check.presence is MarkerPresence.EXISTS:
        state = check.state
        facts.round_id = state.round_id if state else None
        facts.copy_name = state.copy_name if state else None
        return facts.receipt(
            CopyOutcome.SUPPRESSED,
            stage=state.stage if state else None,
            error=check.error,
        )

    facts.round_id = request.marker.ownership.new_token()
    facts.copy_name = copy_file_name(facts.round_id)
    round_id = facts.round_id
    name = facts.copy_name
    facts.file_stage = LogFileStage.MARKER_CREATE
    try:
        save = await _blocking_call(
            LogFileStage.MARKER_CREATE,
            lambda: request.marker.create(round_id, name),
            operation_runner=operation_runner,
            stop_requested=stop_requested)
    except _LoggingOperationStopped as stopped:
        return facts.receipt(
            CopyOutcome.COPY_UNKNOWN,
            error=f"日志交付在 {stopped.stage.value} 开始前停止",
        )
    except BaseException as failure:
        exception_receipt(failure)
        raise
    facts.marker_create = save
    if save.outcome is not SaveOutcome.SAVED:
        return facts.receipt(CopyOutcome.MARKER_CREATE_FAILED,
                             error=save.error)

    destination = request.marker.directory / name
    facts.file_stage = LogFileStage.COPY_WRITE
    write_result_notified = False

    def notify_write_result(
        result: WriteResult, primary: BaseException | None = None,
    ) -> None:
        nonlocal write_result_notified
        if write_result_notified:
            return
        write_result_notified = True
        if result.appended is True:
            from camctl.logging_runtime.diagnostics import acknowledge_diagnostic_record

            acknowledge_diagnostic_record(request.trigger, result.completion)
        if on_write_result is None:
            return
        try:
            on_write_result(result)
        except BaseException as callback_error:
            if primary is None:
                raise
            _add_exception_note(
                primary,
                "write_result_callback_failed: "
                f"{_exception_description(callback_error)}",
            )

    try:
        write_result, copy_error = await _blocking_call(
            LogFileStage.COPY_WRITE,
            lambda: request.channel.copy_with_write(
                request.trigger, destination),
            operation_runner=operation_runner,
            stop_requested=stop_requested)
        facts.write_result = write_result
        facts.copy_error = copy_error
        facts.copy_location = CopyLocation.STAGING
        # 必须在下一次 await 之前同步更新进程内的原日志禁用事实。
        notify_write_result(write_result)
    except _LoggingOperationStopped as stopped:
        return facts.receipt(
            CopyOutcome.COPY_UNKNOWN,
            error=f"日志交付在 {stopped.stage.value} 开始前停止",
        )
    except Exception as failure:
        if facts.write_result is None:
            facts.write_result = _write_result_from_exception(failure)
            facts.copy_location = CopyLocation.STAGING
        if facts.write_result is not None:
            notify_write_result(facts.write_result, failure)
        facts.copy_error = facts.copy_error or _describe(
            "copy_result_unknown", failure)
        cleanup_receipt_error = "; ".join(part for part in (
            facts.copy_error,
            *(_copy_close_diagnostics(facts.write_result)
              if facts.write_result is not None else ()),
        ) if part)
        await _cleanup_staging_once(
            facts, request, destination,
            operation_runner=operation_runner,
            stop_requested=stop_requested,
            receipt_outcome=CopyOutcome.COPY_UNKNOWN,
            receipt_error=cleanup_receipt_error,
            primary=failure,
        )
        details = [facts.copy_error]
        if facts.write_result is not None:
            details.extend(_copy_close_diagnostics(facts.write_result))
        return facts.receipt(CopyOutcome.COPY_UNKNOWN,
                             error="; ".join(details))
    except BaseException as failure:
        if facts.write_result is None:
            facts.write_result = _write_result_from_exception(failure)
            facts.copy_location = CopyLocation.STAGING
        if facts.write_result is not None:
            notify_write_result(facts.write_result, failure)
        facts.copy_error = facts.copy_error or _describe(
            "copy_result_unknown", failure)
        await _cleanup_staging_once(
            facts, request, destination,
            operation_runner=operation_runner,
            stop_requested=stop_requested,
            receipt_outcome=CopyOutcome.COPY_UNKNOWN,
            receipt_error=facts.copy_error,
            primary=failure,
        )
        exception_receipt(failure, error=facts.copy_error)
        raise

    close_diagnostics = _copy_close_diagnostics(facts.write_result)
    if not _copy_handles_confirmed(facts.write_result):
        error = "; ".join(part for part in (
            facts.copy_error, facts.write_result.append_error, facts.write_result.sync_error,
            *close_diagnostics) if part)
        return facts.receipt(CopyOutcome.COPY_UNKNOWN, error=error)
    if (not facts.write_result.completion.delivered_for(request.trigger.level)
            or facts.copy_error is not None):
        error = "; ".join(part for part in (
            facts.copy_error, facts.write_result.append_error, facts.write_result.sync_error,
            *close_diagnostics) if part)
        await _cleanup_staging_once(
            facts, request, destination,
            operation_runner=operation_runner,
            stop_requested=stop_requested,
            receipt_outcome=CopyOutcome.COPY_FAILED,
            receipt_error=error,
        )
        error = "; ".join(part for part in (
            facts.copy_error, facts.write_result.append_error, facts.write_result.sync_error,
            facts.cleanup_error) if part)
        return facts.receipt(CopyOutcome.COPY_FAILED, error=error)

    facts.file_stage = LogFileStage.MARKER_PROGRESS_COPIED
    try:
        copied_save = await _blocking_call(
            LogFileStage.MARKER_PROGRESS_COPIED,
            lambda: request.marker.update_stage(
                MarkerStage.COPIED, expected_round_id=round_id,
                expected_copy_name=name),
            operation_runner=operation_runner,
            stop_requested=stop_requested)
    except _LoggingOperationStopped as stopped:
        stop_error = f"日志交付在 {stopped.stage.value} 开始前停止"
        await _cleanup_staging_once(
            facts, request, destination,
            operation_runner=operation_runner,
            stop_requested=stop_requested,
            receipt_outcome=CopyOutcome.COPY_UNKNOWN,
            receipt_stage=MarkerStage.COPIED,
            receipt_error=stop_error,
        )
        return facts.receipt(
            CopyOutcome.COPY_UNKNOWN, stage=MarkerStage.COPIED,
            error=(f"{stop_error}; "
                   f"cleanup={facts.cleanup_error or 'none'}"),
        )
    except BaseException as failure:
        exception_error = _describe("copy_operation_failed", failure)
        await _cleanup_staging_once(
            facts, request, destination,
            operation_runner=operation_runner,
            stop_requested=stop_requested,
            receipt_outcome=CopyOutcome.COPY_UNKNOWN,
            receipt_stage=MarkerStage.COPIED,
            receipt_error=exception_error,
            primary=failure,
        )
        exception_receipt(
            failure, stage=MarkerStage.COPIED, error=exception_error)
        raise
    facts.marker_progress.append((MarkerStage.COPIED, copied_save))

    facts.file_stage = LogFileStage.COPY_PUBLISH
    try:
        directories = HandoffDirectories(request.staging_root,
                                          request.ready_dir)
        target = ReadyName(name)
        if operation_runner is not None and publish is publish_staged_file:
            file_result = await _blocking_call(
                LogFileStage.COPY_PUBLISH,
                lambda: publish_staged_file_sync(
                    destination, directories, target),
                operation_runner=operation_runner,
                stop_requested=stop_requested,
            )
        else:
            if stop_requested is not None and stop_requested():
                raise _LoggingOperationStopped(LogFileStage.COPY_PUBLISH)
            file_result = await publish(destination, directories, target)
    except _LoggingOperationStopped as stopped:
        stop_error = f"日志交付在 {stopped.stage.value} 开始前停止"
        await _cleanup_staging_once(
            facts, request, destination,
            operation_runner=operation_runner,
            stop_requested=stop_requested,
            receipt_outcome=CopyOutcome.COPY_UNKNOWN,
            receipt_stage=MarkerStage.COPIED,
            receipt_error=stop_error,
        )
        return facts.receipt(
            CopyOutcome.COPY_UNKNOWN, stage=MarkerStage.COPIED,
            error=(f"{stop_error}; "
                   f"cleanup={facts.cleanup_error or 'none'}"),
        )
    except Exception as failure:
        facts.copy_location = CopyLocation.UNKNOWN
        facts.publish_error = _describe("publish_failed", failure)
        return facts.receipt(
            CopyOutcome.COPY_UNKNOWN, stage=MarkerStage.COPIED,
            error=facts.publish_error,
        )
    except BaseException as failure:
        facts.copy_location = CopyLocation.UNKNOWN
        facts.publish_error = _describe("publish_failed", failure)
        exception_receipt(failure, stage=MarkerStage.COPIED,
                          error=facts.publish_error)
        raise

    facts.publish_result = file_result
    if file_result.stage is PublishStage.MOVED:
        facts.copy_location = CopyLocation.READY
    elif file_result.stage is PublishStage.NOT_MOVED:
        facts.copy_location = CopyLocation.STAGING
    else:
        facts.copy_location = CopyLocation.UNKNOWN
    if file_result.stage is PublishStage.UNKNOWN:
        facts.publish_error = file_result.error
        return facts.receipt(
            CopyOutcome.COPY_UNKNOWN, stage=MarkerStage.COPIED,
            error=file_result.error,
        )
    if file_result.stage is not PublishStage.MOVED:
        facts.publish_error = file_result.error
        await _cleanup_staging_once(
            facts, request, destination,
            operation_runner=operation_runner,
            stop_requested=stop_requested,
            receipt_outcome=CopyOutcome.COPY_FAILED,
            receipt_stage=MarkerStage.COPIED,
            receipt_error=file_result.error,
        )
        error = "; ".join(part for part in (
            file_result.error, facts.cleanup_error) if part)
        return facts.receipt(CopyOutcome.COPY_FAILED,
                             stage=MarkerStage.COPIED, error=error)
    if not _handoff_confirmed(file_result):
        # 已移入 ready 的文件保持不撤回；同步未确认不宣称发布完成。
        facts.publish_error = file_result.error
        return facts.receipt(
            CopyOutcome.COPY_UNKNOWN, stage=MarkerStage.COPIED,
            error=file_result.error or "ready 位置已移动，但交接同步未确认",
        )

    facts.file_stage = LogFileStage.MARKER_PROGRESS_PUBLISHED
    try:
        published_save = await _blocking_call(
            LogFileStage.MARKER_PROGRESS_PUBLISHED,
            lambda: request.marker.update_stage(
                MarkerStage.PUBLISHED, expected_round_id=round_id,
                expected_copy_name=name),
            operation_runner=operation_runner,
            stop_requested=stop_requested)
    except _LoggingOperationStopped as stopped:
        return facts.receipt(
            CopyOutcome.COPY_UNKNOWN, stage=MarkerStage.COPIED,
            error=(f"日志交付在 {stopped.stage.value} 开始前停止；"
                   "副本已进入 ready"),
        )
    except BaseException as failure:
        exception_receipt(failure, stage=MarkerStage.COPIED)
        raise
    facts.marker_progress.append((MarkerStage.PUBLISHED, published_save))
    return facts.receipt(
        CopyOutcome.PUBLISHED, stage=MarkerStage.PUBLISHED,
    )


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
                 = deliver_failure_copy,
                 on_write_result: Callable[[WriteResult], None] | None = None
                 ) -> None:
        self._marker = marker
        self._deliver = deliver
        self._on_write_result = on_write_result
        self._suspended = False
        self._lock = asyncio.Lock()
        self._owner: Any = None

    def bind_owner(self, owner: Any) -> None:
        """接入本 run 的日志步骤拥有者；不改变服务协调锁身份。"""
        self._owner = owner

    @property
    def ownership(self) -> RunFileOwnership:
        return self._marker.ownership

    @property
    def logs_dir(self) -> Path:
        return self._marker.directory

    async def on_report_failure(self, request: CopyRequest) -> CopyReceipt:
        return await self.on_report_failure_with_factory(lambda: request)

    async def on_report_failure_with_factory(
            self, request_factory: Callable[[], CopyRequest], *,
            diagnose: Callable[[CopyReceipt | None, BaseException | None],
                               Awaitable[None]] | None = None
            ) -> CopyReceipt:
        """由服务协调请求构造及副本调用，异常也暂停本故障轮。"""
        async with self._lock:
            if self._suspended:
                return CopyReceipt(CopyOutcome.SUPPRESSED)
            try:
                request = request_factory()
                if self._deliver is deliver_failure_copy:
                    receipt = await deliver_failure_copy(
                        request,
                        operation_runner=(None if self._owner is None else
                                          self._owner.owned_call),
                        stop_requested=(None if self._owner is None else
                                        self._owner.stop_requested),
                        on_write_result=self._on_write_result,
                    )
                else:
                    receipt = await self._deliver(request)
            except BaseException as failure:
                self._suspended = True
                await _diagnose_service_operation(diagnose, None, failure)
                raise
            if receipt.outcome in (CopyOutcome.MARKER_UNAVAILABLE,
                                   CopyOutcome.MARKER_CREATE_FAILED):
                self._suspended = True
            if (receipt.write_result is not None
                    and receipt.write_result.disables_file_logging):
                self._suspended = True
            await _diagnose_service_operation(diagnose, receipt, None)
            return receipt

    async def on_report_recovered(
            self, *,
            diagnose: Callable[[MarkerDelete | None, BaseException | None],
                               Awaitable[None]] | None = None
            ) -> MarkerDelete:
        """报告本地处理可靠恢复：结束本轮故障并解除暂停。"""
        async with self._lock:
            deletion_completed = False
            try:
                if self._owner is None:
                    deletion = await asyncio.to_thread(self._marker.delete)
                else:
                    deletion = await _blocking_call(
                        LogFileStage.MARKER_RECOVERY_DELETE,
                        self._marker.delete,
                        operation_runner=self._owner.owned_call,
                        stop_requested=self._owner.stop_requested,
                    )
                deletion_completed = True
            except _LoggingOperationStopped as stopped:
                deletion = MarkerDelete(
                    DeleteOutcome.FAILED,
                    error=("恢复标记删除在 "
                           f"{stopped.stage.value} 开始前停止"),
                )
            except BaseException as failure:
                await _diagnose_service_operation(diagnose, None, failure)
                raise
            if deletion_completed:
                self._suspended = False
            await _diagnose_service_operation(diagnose, deletion, None)
            return deletion


async def _diagnose_service_operation(
        callback: Callable[[Any, BaseException | None], Awaitable[None]] | None,
        result: Any, failure: BaseException | None) -> None:
    if callback is None:
        return
    try:
        await callback(result, failure)
    except Exception:
        # 诊断出口故障是附属事实，不改写报告／恢复主结果。
        return


# ---- run 启动清理 ----------------------------------------------------

@dataclass(frozen=True)
class SweepResult:
    error: str | None = None
    failures: tuple[tuple[str, str], ...] = ()


def startup_sweep(logs_dir: Path, *,
                  active_names: Sequence[str] = (),
                  ownership: RunFileOwnership | None = None,
                  owner_settled: bool = True,
                  ordinary_log_path: Path | None = None,
                  stop_requested: Callable[[], bool] | None = None,
                  unlink_candidate: Callable[[Path], None] | None = None
                  ) -> SweepResult:
    """取得会话锁后的一次性检查：清理以前的日志副本与标记临时文件。

    保留正式标记、本次运行正在处理的副本及无法确认归属的文件；
    目录检查失败保留错误，单个删除失败不阻止其他文件。
    """
    resolved_logs_dir: Path | None = None
    protected_ordinary_paths: set[Path] = set()
    if ordinary_log_path is not None:
        configured_path = Path(ordinary_log_path)
        try:
            resolved_logs_dir = Path(logs_dir).resolve()
            protected_ordinary_paths.add(
                configured_path.parent.resolve() / configured_path.name)
            protected_ordinary_paths.add(configured_path.resolve())
        except (OSError, RuntimeError, ValueError) as failure:
            return SweepResult(error=_describe(
                "ordinary_log_path_resolve_failed", failure))

    try:
        names = _listdir(Path(logs_dir))
    except FileNotFoundError:
        return SweepResult()
    except OSError as failure:
        return SweepResult(error=_describe("listdir_failed", failure))
    failures = []
    for name in names:
        if stop_requested is not None and stop_requested():
            break
        if (name in active_names or (ownership is not None
                               and ownership.owns_name(name))
                or name == MARKER_NAME):
            continue
        if not (is_copy_file_name(name) or is_marker_temp_name(name)):
            continue
        if (resolved_logs_dir is not None
                and resolved_logs_dir / name in protected_ordinary_paths):
            continue
        path = Path(logs_dir) / name
        try:
            info = _lstat(path)
        except FileNotFoundError:
            continue
        except OSError as failure:
            failures.append((name, _describe("lstat_failed", failure)))
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        if not owner_settled:
            continue
        # 名字可在枚举后、lstat期间登记；删除前同时复查调用方与当前owner。
        if (name in active_names or (ownership is not None
                                     and ownership.owns_name(name))):
            continue
        if stop_requested is not None and stop_requested():
            break
        try:
            if unlink_candidate is None:
                _unlink(path)
            else:
                unlink_candidate(path)
        except _LoggingOperationStopped:
            break
        except FileNotFoundError:
            continue
        except OSError as failure:
            failures.append((name, _describe("unlink_failed", failure)))
    return SweepResult(failures=tuple(failures))

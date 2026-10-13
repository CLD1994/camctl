"""Hasselblad 单张拍摄的主机工作文件验证与正式归档。"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import re
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, TypeVar

from camctl.contracts.values import parse_object_id
from camctl.host_files.models import BoundDirectories, FilePurpose, FileRef
from camctl.host_files.paths import resolve_file
from camctl.host_files.segments import DEFAULT_CHUNK_SIZE_BYTES

__all__ = [
    "CaptureFileError",
    "CaptureFileSpec",
    "CapturedArtifact",
    "archive_capture_file",
    "prepare_capture_workspace",
    "verify_capture_files",
    "workspace_path",
]

_MEDIA_TYPES = frozenset(("image/x-hasselblad-3fr", "image/jpeg"))
_ARTIFACT_FIELDS = frozenset(("original_name", "size_bytes", "sha256", "media_type"))
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_BINARY = getattr(os, "O_BINARY", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_Result = TypeVar("_Result")


class CaptureFileError(ValueError):
    """采集文件的输入、观察、同步或归档阶段失败。"""

    def __init__(
        self,
        stage: str,
        path: Path | None = None,
        *,
        detail: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        self.stage = stage
        self.path = path
        self.detail = detail
        self.cause = cause
        self.additional_errors: tuple[BaseException, ...] = ()
        parts = [stage]
        if path is not None:
            parts.append(str(path))
        if detail:
            parts.append(detail)
        if cause is not None:
            parts.append(f"{type(cause).__name__}: {cause}")
        super().__init__(": ".join(parts))


def _validate_name(name: object) -> None:
    if (
        not isinstance(name, str)
        or not name
        or name in (".", "..")
        or any(character in name for character in ("/", "\\", "\x00", "\r", "\n"))
    ):
        raise CaptureFileError(
            "spec_validation", detail=f"文件名必须是单个直接文件名: {name!r}"
        )


def _validate_media_type(media_type: object) -> None:
    if not isinstance(media_type, str) or media_type not in _MEDIA_TYPES:
        raise CaptureFileError(
            "spec_validation", detail=f"不支持的采集媒体类型: {media_type!r}"
        )


@dataclass(frozen=True)
class CaptureFileSpec:
    """工具成功响应中一份文件的直接名称、长度及媒体类型。"""

    original_name: str
    size_bytes: int
    media_type: str

    def __post_init__(self) -> None:
        _validate_name(self.original_name)
        if type(self.size_bytes) is not int or self.size_bytes <= 0:
            raise CaptureFileError(
                "spec_validation",
                detail=f"文件长度必须是正整数: {self.size_bytes!r}",
            )
        _validate_media_type(self.media_type)


@dataclass(frozen=True)
class CapturedArtifact:
    """经核验的拍摄文件事实；不携带工作目录或其他自由路径。"""

    original_name: str
    size_bytes: int
    sha256: str
    media_type: str

    def __post_init__(self) -> None:
        CaptureFileSpec(self.original_name, self.size_bytes, self.media_type)
        if not isinstance(self.sha256, str) or _SHA256.fullmatch(self.sha256) is None:
            raise CaptureFileError(
                "artifact_validation", detail=f"SHA-256 格式无效: {self.sha256!r}"
            )

    def as_json(self) -> dict[str, object]:
        return {
            "original_name": self.original_name,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "media_type": self.media_type,
        }

    @classmethod
    def from_json(cls, value: object) -> "CapturedArtifact":
        if not isinstance(value, Mapping) or set(value) != _ARTIFACT_FIELDS:
            raise CaptureFileError(
                "artifact_validation",
                detail="采集文件事实必须恰含 original_name、size_bytes、sha256、media_type",
            )
        try:
            return cls(
                original_name=value["original_name"],  # type: ignore[arg-type]
                size_bytes=value["size_bytes"],  # type: ignore[arg-type]
                sha256=value["sha256"],  # type: ignore[arg-type]
                media_type=value["media_type"],  # type: ignore[arg-type]
            )
        except (KeyError, TypeError) as failure:
            raise CaptureFileError(
                "artifact_validation", detail="采集文件事实字段类型无效", cause=failure
            ) from failure


@dataclass(frozen=True)
class _FileFacts:
    size_bytes: int
    sha256: str
    device: int
    inode: int


def workspace_path(roots: BoundDirectories, activity_id: str) -> Path:
    """按已保存的活动身份计算独占采集工作目录，不访问文件系统。"""
    try:
        identity = parse_object_id(activity_id)
    except ValueError as failure:
        raise CaptureFileError(
            "activity_id", detail=f"活动身份无效: {activity_id!r}", cause=failure
        ) from failure
    return roots.staging / "capture-work" / str(identity)


async def prepare_capture_workspace(
    roots: BoundDirectories, activity_id: str
) -> Path:
    """创建本次活动独占目录；已存在的目录或对象保留原样并报错。"""
    path = workspace_path(roots, activity_id)
    await _run_file_io(_prepare_capture_workspace, roots, path)
    return path


async def verify_capture_files(
    roots: BoundDirectories,
    activity_id: str,
    specs: tuple[CaptureFileSpec, ...],
) -> tuple[CapturedArtifact, ...]:
    """核对工作目录中的普通文件、完整长度及媒体签名，并同步后返回摘要。"""
    path = workspace_path(roots, activity_id)
    if type(specs) is not tuple or not specs:
        raise CaptureFileError("spec_validation", path, detail="文件规格必须是非空 tuple")
    names: set[str] = set()
    for spec in specs:
        if not isinstance(spec, CaptureFileSpec):
            raise CaptureFileError(
                "spec_validation", path, detail=f"文件规格类型无效: {spec!r}"
            )
        if spec.original_name in names:
            raise CaptureFileError(
                "spec_validation", path,
                detail=f"同一工作目录中的文件名重复: {spec.original_name!r}",
            )
        names.add(spec.original_name)
    return await _run_file_io(_verify_capture_files, roots, path, specs)


async def archive_capture_file(
    roots: BoundDirectories,
    activity_id: str,
    artifact: CapturedArtifact,
    target: FileRef,
) -> None:
    """按登记身份归档文件；匹配的既有目标可恢复，未知目标绝不覆盖。"""
    workspace = workspace_path(roots, activity_id)
    if not isinstance(artifact, CapturedArtifact):
        raise CaptureFileError(
            "artifact_validation", detail=f"采集文件事实类型无效: {artifact!r}"
        )
    expected_purpose = (
        FilePurpose.CAPTURE_ORIGINAL
        if artifact.media_type == "image/x-hasselblad-3fr"
        else FilePurpose.CAPTURE_PREVIEW
    )
    if target.purpose is not expected_purpose:
        raise CaptureFileError(
            "target_role",
            detail=(
                f"媒体类型 {artifact.media_type!r} 与目标用途 "
                f"{target.purpose!r} 不匹配"
            ),
        )
    try:
        target_path = resolve_file(target, roots).path
    except ValueError as failure:
        raise CaptureFileError(
            "target_path", detail=f"正式目标引用无效: {target!r}", cause=failure
        ) from failure
    await _run_file_io(
        _archive_capture_file,
        roots,
        workspace,
        artifact,
        target_path,
    )


async def _run_file_io(
    function: Callable[..., _Result], *args: Any
) -> _Result:
    """取消等待者时继续观察已提交线程操作的实际结果。"""
    worker = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as cancellation:
        while True:
            try:
                await asyncio.shield(worker)
                break
            except asyncio.CancelledError:
                if not worker.done():
                    continue
                try:
                    worker.result()
                except BaseException as failure:
                    cancellation.add_note(
                        f"实际文件操作收场失败: {type(failure).__name__}: {failure}"
                    )
                    raise cancellation from failure
                break
            except BaseException as failure:
                cancellation.add_note(
                    f"实际文件操作收场失败: {type(failure).__name__}: {failure}"
                )
                raise cancellation from failure
        raise cancellation


def _prepare_capture_workspace(roots: BoundDirectories, path: Path) -> None:
    parent_path = path.parent
    with _open_directory(roots.staging, "staging_open") as staging_fd:
        try:
            os.mkdir("capture-work", 0o700, dir_fd=staging_fd)
        except FileExistsError:
            pass
        except OSError as failure:
            raise CaptureFileError(
                "workspace_parent_create", parent_path, cause=failure
            ) from failure

        with _open_directory_at(
            staging_fd, "capture-work", parent_path, "workspace_parent_open"
        ) as parent_fd:
            _sync_directory_fd(staging_fd, roots.staging, "staging_sync")
            try:
                os.mkdir(path.name, 0o700, dir_fd=parent_fd)
            except FileExistsError as failure:
                raise CaptureFileError(
                    "workspace_create",
                    path,
                    detail="活动工作目录或同名对象已经存在",
                    cause=failure,
                ) from failure
            except OSError as failure:
                raise CaptureFileError(
                    "workspace_create", path, cause=failure
                ) from failure
            _sync_directory_fd(parent_fd, parent_path, "workspace_parent_sync")


def _verify_capture_files(
    roots: BoundDirectories,
    path: Path,
    specs: tuple[CaptureFileSpec, ...],
) -> tuple[CapturedArtifact, ...]:
    artifacts: list[CapturedArtifact] = []
    with _workspace_directory(roots, path) as workspace_fd:
        for spec in specs:
            facts = _read_file_facts_at(
                workspace_fd,
                spec.original_name,
                path / spec.original_name,
                spec.media_type,
                expected_size=spec.size_bytes,
                expected_sha256=None,
                file_kind="source",
            )
            artifacts.append(
                CapturedArtifact(
                    original_name=spec.original_name,
                    size_bytes=facts.size_bytes,
                    sha256=facts.sha256,
                    media_type=spec.media_type,
                )
            )
        _sync_directory_fd(workspace_fd, path, "workspace_sync")
    return tuple(artifacts)


def _archive_capture_file(
    roots: BoundDirectories,
    workspace: Path,
    artifact: CapturedArtifact,
    target_path: Path,
) -> None:
    with _workspace_directory(roots, workspace) as workspace_fd:
        with _open_directory(
            target_path.parent, "target_directory_open"
        ) as target_directory_fd:
            source_stat = _stat_entry(
                workspace_fd,
                artifact.original_name,
                workspace / artifact.original_name,
                "source_observe",
            )
            target_stat = _stat_entry(
                target_directory_fd,
                target_path.name,
                target_path,
                "target_observe",
            )
            if source_stat is None and target_stat is None:
                raise CaptureFileError(
                    "source_and_target_missing",
                    workspace / artifact.original_name,
                    detail=f"正式目标也不存在: {target_path}",
                )

            source_facts: _FileFacts | None = None
            if source_stat is not None:
                source_facts = _read_file_facts_at(
                    workspace_fd,
                    artifact.original_name,
                    workspace / artifact.original_name,
                    artifact.media_type,
                    expected_size=artifact.size_bytes,
                    expected_sha256=artifact.sha256,
                    file_kind="source",
                )

            if target_stat is not None:
                _read_file_facts_at(
                    target_directory_fd,
                    target_path.name,
                    target_path,
                    artifact.media_type,
                    expected_size=artifact.size_bytes,
                    expected_sha256=artifact.sha256,
                    file_kind="target",
                )
                if source_facts is not None:
                    _unlink_source(
                        workspace_fd,
                        artifact.original_name,
                        workspace / artifact.original_name,
                        source_facts,
                    )
                    _sync_directory_fd(workspace_fd, workspace, "workspace_sync")
                _sync_directory_fd(
                    target_directory_fd, target_path.parent, "target_directory_sync"
                )
                return

            assert source_facts is not None
            try:
                os.link(
                    artifact.original_name,
                    target_path.name,
                    src_dir_fd=workspace_fd,
                    dst_dir_fd=target_directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                # 另一个已持有该责任的恢复者可能刚刚创建了目标；重新
                # 核对目标内容后才允许复用，不覆盖任何已有目录项。
                _read_file_facts_at(
                    target_directory_fd,
                    target_path.name,
                    target_path,
                    artifact.media_type,
                    expected_size=artifact.size_bytes,
                    expected_sha256=artifact.sha256,
                    file_kind="target",
                )
            except OSError as failure:
                raise CaptureFileError(
                    "archive_link", target_path, cause=failure
                ) from failure

            _sync_directory_fd(
                target_directory_fd, target_path.parent, "target_directory_sync"
            )
            _unlink_source(
                workspace_fd,
                artifact.original_name,
                workspace / artifact.original_name,
                source_facts,
            )
            _sync_directory_fd(workspace_fd, workspace, "workspace_sync")


def _unlink_source(
    workspace_fd: int,
    name: str,
    path: Path,
    expected: _FileFacts,
) -> None:
    observed = _stat_entry(workspace_fd, name, path, "source_observe")
    if observed is None:
        return
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_dev != expected.device
        or observed.st_ino != expected.inode
    ):
        raise CaptureFileError(
            "source_changed", path, detail="归档期间源目录项已更换"
        )
    try:
        os.unlink(name, dir_fd=workspace_fd)
    except OSError as failure:
        raise CaptureFileError("source_unlink", path, cause=failure) from failure


def _read_file_facts_at(
    directory_fd: int,
    name: str,
    path: Path,
    media_type: str,
    *,
    expected_size: int,
    expected_sha256: str | None,
    file_kind: str,
) -> _FileFacts:
    fd, opened_stat = _open_regular_file_at(
        directory_fd, name, path, f"{file_kind}_open", f"{file_kind}_type"
    )
    primary: BaseException | None = None
    try:
        if opened_stat.st_size != expected_size:
            raise _file_error(
                file_kind,
                "size",
                path,
                f"实际长度 {opened_stat.st_size}，期望 {expected_size}",
            )

        digest = hashlib.sha256()
        bytes_read = 0
        header = bytearray()
        trailer = b""
        while True:
            try:
                block = os.read(fd, DEFAULT_CHUNK_SIZE_BYTES)
            except OSError as failure:
                raise CaptureFileError(
                    f"{file_kind}_read", path, cause=failure
                ) from failure
            if not block:
                break
            digest.update(block)
            bytes_read += len(block)
            if len(header) < 4:
                header.extend(block[: 4 - len(header)])
            trailer = (trailer + block)[-2:]

        if bytes_read != expected_size:
            raise _file_error(
                file_kind,
                "size",
                path,
                f"读取长度 {bytes_read}，期望 {expected_size}",
            )
        _validate_media_signature(media_type, bytes(header), trailer, bytes_read, path, file_kind)
        sha256 = digest.hexdigest()
        if expected_sha256 is not None and sha256 != expected_sha256:
            raise _file_error(
                file_kind,
                "digest",
                path,
                f"SHA-256 不匹配: 实际 {sha256}，期望 {expected_sha256}",
            )

        try:
            after = os.fstat(fd)
        except OSError as failure:
            raise CaptureFileError(
                f"{file_kind}_observe", path, cause=failure
            ) from failure
        if not _same_file_state(opened_stat, after):
            raise CaptureFileError(
                f"{file_kind}_changed", path, detail="文件在摘要读取期间发生变化"
            )
        named_after = _stat_entry(directory_fd, name, path, f"{file_kind}_observe")
        if named_after is None or not _same_identity(opened_stat, named_after):
            raise CaptureFileError(
                f"{file_kind}_changed", path, detail="文件读取期间路径指向发生变化"
            )
        try:
            os.fsync(fd)
        except OSError as failure:
            raise CaptureFileError(
                f"{file_kind}_sync", path, cause=failure
            ) from failure
        return _FileFacts(
            size_bytes=bytes_read,
            sha256=sha256,
            device=opened_stat.st_dev,
            inode=opened_stat.st_ino,
        )
    except BaseException as failure:
        primary = failure
        raise
    finally:
        _close_fd(fd, f"{file_kind}_close", path, primary)


def _open_regular_file_at(
    directory_fd: int,
    name: str,
    path: Path,
    open_stage: str,
    type_stage: str,
) -> tuple[int, os.stat_result]:
    before = _stat_entry(directory_fd, name, path, open_stage)
    if before is None:
        raise CaptureFileError(open_stage, path, detail="文件不存在")
    if not stat.S_ISREG(before.st_mode):
        raise CaptureFileError(
            type_stage, path, detail="目标不是普通文件，拒绝读取"
        )
    flags = os.O_RDONLY | _BINARY | _NONBLOCK | _NOFOLLOW
    try:
        fd = os.open(name, flags, dir_fd=directory_fd)
    except OSError as failure:
        stage = type_stage if failure.errno == errno.ELOOP else open_stage
        raise CaptureFileError(stage, path, cause=failure) from failure
    try:
        opened = os.fstat(fd)
    except OSError as failure:
        error = CaptureFileError(open_stage, path, cause=failure)
        _close_fd(fd, f"{open_stage}_close", path, error)
        raise error from failure
    if not stat.S_ISREG(opened.st_mode):
        error = CaptureFileError(
            type_stage, path, detail="打开后的对象不是普通文件，拒绝读取"
        )
        _close_fd(fd, f"{open_stage}_close", path, error)
        raise error
    if not _same_identity(before, opened):
        error = CaptureFileError(
            f"{open_stage.removesuffix('_open')}_changed",
            path,
            detail="文件在检查与打开之间被替换",
        )
        _close_fd(fd, f"{open_stage}_close", path, error)
        raise error
    return fd, opened


def _validate_media_signature(
    media_type: str,
    header: bytes,
    trailer: bytes,
    size: int,
    path: Path,
    file_kind: str,
) -> None:
    if media_type == "image/x-hasselblad-3fr":
        valid = header in (b"II*\x00", b"MM\x00*")
        description = "文件头不是已观察的 TIFF 字节序与 magic"
    else:
        valid = size >= 4 and header.startswith(b"\xff\xd8") and trailer.endswith(b"\xff\xd9")
        description = "JPEG 文件缺少 SOI 文件头或 EOI 文件尾"
    if not valid:
        raise _file_error(file_kind, "media", path, description)


def _file_error(file_kind: str, category: str, path: Path, detail: str) -> CaptureFileError:
    stage = "target_conflict" if file_kind == "target" else f"source_{category}"
    return CaptureFileError(stage, path, detail=detail)


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _same_file_state(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        _same_identity(left, right)
        and left.st_mode == right.st_mode
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_ctime_ns == right.st_ctime_ns
    )


def _stat_entry(
    directory_fd: int, name: str, path: Path, stage: str
) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as failure:
        raise CaptureFileError(stage, path, cause=failure) from failure


def _open_directory(path: Path, stage: str):
    flags = os.O_RDONLY | _BINARY | _DIRECTORY | _NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as failure:
        raise CaptureFileError(stage, path, cause=failure) from failure
    try:
        observed = os.fstat(fd)
    except OSError as failure:
        error = CaptureFileError(stage, path, cause=failure)
        _close_fd(fd, f"{stage}_close", path, error)
        raise error from failure
    if not stat.S_ISDIR(observed.st_mode):
        error = CaptureFileError(stage, path, detail="对象不是普通目录")
        _close_fd(fd, f"{stage}_close", path, error)
        raise error
    return _fd_context(fd, path, stage)


def _open_directory_at(parent_fd: int, name: str, path: Path, stage: str):
    flags = os.O_RDONLY | _BINARY | _DIRECTORY | _NOFOLLOW
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as failure:
        raise CaptureFileError(stage, path, cause=failure) from failure
    try:
        observed = os.fstat(fd)
    except OSError as failure:
        error = CaptureFileError(stage, path, cause=failure)
        _close_fd(fd, f"{stage}_close", path, error)
        raise error from failure
    if not stat.S_ISDIR(observed.st_mode):
        error = CaptureFileError(stage, path, detail="对象不是普通目录")
        _close_fd(fd, f"{stage}_close", path, error)
        raise error
    return _fd_context(fd, path, stage)


@contextmanager
def _fd_context(fd: int, path: Path, stage: str) -> Iterator[int]:
    primary: BaseException | None = None
    try:
        yield fd
    except BaseException as failure:
        primary = failure
        raise
    finally:
        _close_fd(fd, f"{stage}_close", path, primary)


@contextmanager
def _workspace_directory(
    roots: BoundDirectories, path: Path
) -> Iterator[int]:
    with _open_directory(roots.staging, "staging_open") as staging_fd:
        with _open_directory_at(
            staging_fd,
            "capture-work",
            path.parent,
            "workspace_parent_open",
        ) as parent_fd:
            with _open_directory_at(
                parent_fd,
                path.name,
                path,
                "workspace_open",
            ) as workspace_fd:
                yield workspace_fd


def _sync_directory_fd(fd: int, path: Path, stage: str) -> None:
    try:
        os.fsync(fd)
    except OSError as failure:
        raise CaptureFileError(stage, path, cause=failure) from failure


def _close_fd(
    fd: int,
    stage: str,
    path: Path,
    primary: BaseException | None = None,
) -> None:
    try:
        os.close(fd)
    except OSError as failure:
        if isinstance(primary, CaptureFileError):
            primary.additional_errors += (failure,)
            primary.add_note(f"{stage} also failed: {failure!r}")
            return
        if primary is not None:
            primary.add_note(f"{stage} also failed: {failure!r}")
            return
        raise CaptureFileError(stage, path, cause=failure) from failure

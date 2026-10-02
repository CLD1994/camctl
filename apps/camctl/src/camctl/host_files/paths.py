"""用途目录、归属及对象类型检查。

本地路径只从已保存身份、正式用途和相对路径取得；纯路径规则不
访问真实文件，实际观察在线程池中执行并返回实际错误及阶段。
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path, PurePosixPath

from camctl.contracts.values import ObjectId
from camctl.host_files.models import (
    BoundDirectories,
    FileObservation,
    FileObservationKind,
    FilePurpose,
    FileRef,
    HostPath,
)

__all__ = [
    "PURPOSE_DIRECTORIES", "inspect_file", "resolve_file", "object_file_name",
    "relative_file_path", "validate_file_extension", "validate_relative_file_path",
]

#: 用途与固定直接子目录的对应（docs/camctl/database/file-fields.md）。
PURPOSE_DIRECTORIES = {
    FilePurpose.DELIVERY_COPY: "deliveries",
    FilePurpose.RECORDING_INPUT: "recording-inputs",
    FilePurpose.PROCESSING_TEMP: "processing-temp",
    FilePurpose.REPAIR_OUTPUT: "derived",
}

_EXTENSION = re.compile(r"[A-Za-z0-9]+")
_ID_AND_EXTENSION = re.compile(rf"\A([1-9][0-9]*)(?:\.({_EXTENSION.pattern}))?\Z")
_MAX_NAME_BYTES = 255


class PathRuleError(ValueError):
    """文件引用不满足定位规则；不是文件缺失或检查错误。"""


def validate_file_extension(extension: str | None, *, required: bool = False) -> None:
    """校验生产者提供的扩展名；完整名称长度在分配身份后检查。"""
    if extension is None and not required:
        return
    if not isinstance(extension, str) or _EXTENSION.fullmatch(extension) is None:
        raise PathRuleError(f"扩展名必须是非空 ASCII 字母或数字: {extension!r}")


def object_file_name(file_id: int, extension: str | None) -> str:
    """按已分配身份生成唯一文件名，并校验完整名称长度。"""
    try:
        identity = ObjectId(file_id)
    except ValueError as error:
        raise PathRuleError(f"文件身份无效: {file_id!r}") from error
    validate_file_extension(extension)
    name = str(identity) if extension is None else f"{identity}.{extension}"
    if len(name) > _MAX_NAME_BYTES:
        raise PathRuleError(f"文件名超过 {_MAX_NAME_BYTES} ASCII 字节: {name!r}")
    return name


def relative_file_path(purpose: FilePurpose, file_id: int, extension: str | None) -> str:
    """在正式用途目录中生成登记路径，不接收自由路径文本。"""
    if not isinstance(purpose, FilePurpose):
        raise PathRuleError(f"文件用途类型无效: {purpose!r}")
    return f"{PURPOSE_DIRECTORIES[purpose]}/{object_file_name(file_id, extension)}"


def validate_relative_file_path(purpose: FilePurpose, file_id: int, relative_path: str) -> None:
    """已保存路径须与正式用途及身份一致；不访问文件系统。"""
    if not isinstance(purpose, FilePurpose) or not isinstance(relative_path, str):
        raise PathRuleError("保存路径必须使用正式用途及字符串相对路径")
    relative = PurePosixPath(relative_path)
    parts = relative.parts
    if len(parts) != 2 or any(part in ("", ".", "..") for part in parts):
        raise PathRuleError(f"相对路径必须恰为 用途目录/文件名: {relative_path!r}")
    if "\\" in relative_path or "\x00" in relative_path:
        raise PathRuleError(f"相对路径包含非法字符: {relative_path!r}")
    if relative_path != "/".join(parts) or relative_path.endswith("/"):
        raise PathRuleError(f"相对路径写法不规范: {relative_path!r}")
    directory, name = parts
    expected_directory = PURPOSE_DIRECTORIES[purpose]
    if directory != expected_directory:
        raise PathRuleError(
            f"用途目录与 {purpose.value} 不符: {directory!r} 应为 {expected_directory!r}"
        )
    match = _ID_AND_EXTENSION.match(name)
    if match is None:
        raise PathRuleError(f"文件名必须是 无前导零ID[.扩展名]: {name!r}")
    if name != object_file_name(file_id, match.group(2)):
        raise PathRuleError(
            f"文件名 ID 与文件身份不一致: {name!r} 应为 {file_id}"
        )


def resolve_file(ref: FileRef, roots: BoundDirectories) -> HostPath:
    """按已绑定根目录和通过纯规则核验的保存路径定位文件。"""
    if ref.root != roots.staging:
        raise PathRuleError(
            f"文件引用根目录与绑定不一致: {ref.root!s} != {roots.staging!s}"
        )
    validate_relative_file_path(ref.purpose, ref.file_id, ref.relative_path)
    return HostPath(
        path=roots.staging / ref.relative_path, purpose=ref.purpose, file_id=ref.file_id
    )


async def inspect_file(ref: FileRef, roots: BoundDirectories) -> FileObservation:
    """观察目标对象的实际状态。

    实际文件访问在线程池执行：缺失、目录对象与检查错误分别表
    达，权限或访问失败不是缺失。
    """
    host = resolve_file(ref, roots)
    return await asyncio.to_thread(_observe, host.path)


def _observe(path: Path) -> FileObservation:
    try:
        stat = os.stat(path)
    except FileNotFoundError:
        return FileObservation(kind=FileObservationKind.MISSING, path=path)
    except OSError as error:
        return FileObservation(kind=FileObservationKind.ERROR, path=path, error=error)
    if not os.path.isfile(path):
        return FileObservation(
            kind=FileObservationKind.TYPE_MISMATCH, path=path, is_file=False
        )
    return FileObservation(kind=FileObservationKind.VALID_OBJECT, path=path, is_file=True)

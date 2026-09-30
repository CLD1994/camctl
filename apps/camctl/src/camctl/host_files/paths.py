"""用途目录、归属及对象类型检查。

本地路径只从已保存身份、正式用途和相对路径取得；纯路径规则不
访问真实文件，实际观察在线程池中执行并返回实际错误及阶段。
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path, PurePosixPath

from camctl.host_files.models import (
    BoundDirectories,
    FileObservation,
    FileObservationKind,
    FilePurpose,
    FileRef,
    HostPath,
)

__all__ = ["PURPOSE_DIRECTORIES", "inspect_file", "resolve_file"]

#: 用途与固定直接子目录的对应（docs/camctl/database/file-fields.md）。
PURPOSE_DIRECTORIES = {
    FilePurpose.DELIVERY_COPY: "deliveries",
    FilePurpose.RECORDING_INPUT: "recording-inputs",
    FilePurpose.PROCESSING_TEMP: "processing-temp",
    FilePurpose.REPAIR_OUTPUT: "derived",
}

_ID_AND_EXTENSION = re.compile(r"\A([1-9][0-9]*)(?:\.([A-Za-z0-9]+))?\Z")
_MAX_NAME_BYTES = 255


class PathRuleError(ValueError):
    """文件引用不满足定位规则；不是文件缺失或检查错误。"""


def resolve_file(ref: FileRef, roots: BoundDirectories) -> HostPath:
    """按正式用途和保存路径定位文件。

    纯路径规则：相对路径恰两段、首段与用途目录一致、文件名 ID
    与文件身份一致且扩展名合法；引用根目录必须与绑定一致。
    """
    if ref.root != roots.staging:
        raise PathRuleError(
            f"文件引用根目录与绑定不一致: {ref.root!s} != {roots.staging!s}"
        )
    relative = PurePosixPath(ref.relative_path)
    parts = relative.parts
    if len(parts) != 2 or any(part in ("", ".", "..") for part in parts):
        raise PathRuleError(f"相对路径必须恰为 用途目录/文件名: {ref.relative_path!r}")
    if "\\" in ref.relative_path or "\x00" in ref.relative_path:
        raise PathRuleError(f"相对路径包含非法字符: {ref.relative_path!r}")
    if ref.relative_path != "/".join(parts) or ref.relative_path.endswith("/"):
        raise PathRuleError(f"相对路径写法不规范: {ref.relative_path!r}")
    directory, name = parts
    expected_directory = PURPOSE_DIRECTORIES[ref.purpose]
    if directory != expected_directory:
        raise PathRuleError(
            f"用途目录与 {ref.purpose.value} 不符: {directory!r} 应为 {expected_directory!r}"
        )
    match = _ID_AND_EXTENSION.match(name)
    if match is None:
        raise PathRuleError(f"文件名必须是 无前导零ID[.扩展名]: {name!r}")
    if int(match.group(1)) != ref.file_id:
        raise PathRuleError(
            f"文件名 ID 与文件身份不一致: {name!r} 应为 {ref.file_id}"
        )
    if len(name.encode("ascii", errors="strict")) > _MAX_NAME_BYTES:
        raise PathRuleError(f"文件名超过 {_MAX_NAME_BYTES} ASCII 字节: {name!r}")
    return HostPath(
        path=roots.staging / directory / name, purpose=ref.purpose, file_id=ref.file_id
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

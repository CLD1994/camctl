"""主机文件的实际结果类型。"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "BoundDirectories",
    "FileObservation",
    "FileObservationKind",
    "FilePurpose",
    "FileRef",
    "HostPath",
]


class FilePurpose(enum.Enum):
    """中间文件的正式用途；与固定用途目录一一对应。"""

    DELIVERY_COPY = "delivery_copy"
    RECORDING_INPUT = "recording_input"
    PROCESSING_TEMP = "processing_temp"
    REPAIR_OUTPUT = "repair_output"


class FileObservationKind(enum.Enum):
    """一次文件观察的实际分类；缺失、类型不符与检查错误分别表达。"""

    VALID_OBJECT = "valid_object"
    MISSING = "missing"
    TYPE_MISMATCH = "type_mismatch"
    ERROR = "error"


@dataclass(frozen=True)
class FileRef:
    """已保存文件的引用：身份、用途、相对路径与保存时的根目录。

    只携带保存身份，不携带设备定位或用户名称；定位由正式用途和
    相对路径完成。
    """

    file_id: int
    purpose: FilePurpose
    relative_path: str
    root: Path


@dataclass(frozen=True)
class BoundDirectories:
    """已验证的目录绑定；文件引用必须落在对应根目录内。"""

    staging: Path


@dataclass(frozen=True)
class HostPath:
    """本地定位值：用途目录内的实际路径。"""

    path: Path
    purpose: FilePurpose
    file_id: int


@dataclass(frozen=True)
class FileObservation:
    """一次实际检查的结果；错误保留原始异常供诊断。"""

    kind: FileObservationKind
    path: Path
    is_file: bool | None = None
    error: Any = None

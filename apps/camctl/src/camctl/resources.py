"""包内权威资源的定位与读取。

资源名集合从构建资源目录生成，访问不接受任意路径；
读取只通过 importlib.resources，不依赖源码仓库布局。
"""

from __future__ import annotations

from importlib import resources as importlib_resources
from typing import NewType

_PACKAGE = "camctl"
_RESOURCE_DIR = "_resources"

#: 受约束的资源名：值为构建资源目录内的相对路径，以 "/" 分隔。
ResourceName = NewType("ResourceName", str)


class ResourceError(RuntimeError):
    """资源名不在构建资源目录登记范围内，或资源读取失败。"""


def _walk_names(traversable, prefix: str = "") -> list[str]:
    names: list[str] = []
    for item in traversable.iterdir():
        if item.is_dir():
            names.extend(_walk_names(item, f"{prefix}{item.name}/"))
        else:
            names.append(f"{prefix}{item.name}")
    return names


def available_resources() -> frozenset[str]:
    """列出包内全部登记资源名；资源目录缺失时返回空集合。"""
    root = importlib_resources.files(_PACKAGE).joinpath(_RESOURCE_DIR)
    if not root.is_dir():
        return frozenset()
    return frozenset(_walk_names(root))


def resource_bytes(name: str) -> bytes:
    """按登记名读取资源内容。

    只接受 ``available_resources`` 中的名称；任意路径、越界片段
    或未登记名称都抛出 :class:`ResourceError`，不尝试其他定位方式。
    """
    if not isinstance(name, str) or not name:
        raise ResourceError(f"非法资源名: {name!r}")
    if "\\" in name or name.startswith("/"):
        raise ResourceError(f"非法资源名: {name!r}")
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ResourceError(f"非法资源名: {name!r}")
    if name not in available_resources():
        raise ResourceError(f"未登记的资源名: {name!r}")
    target = importlib_resources.files(_PACKAGE).joinpath(_RESOURCE_DIR, *parts)
    try:
        return target.read_bytes()
    except OSError as error:
        raise ResourceError(f"资源读取失败: {name!r}") from error

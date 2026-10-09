"""受原设备绑定与完整范围约束的目录分页端口值。"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from camctl.contracts.pages import Page
from camctl.devices.bindings import DeviceBinding
from camctl.devices.file_identity import FileIdentity
from camctl.operations.models import ErrorValue
from camctl.operations.process import RawToolOutcome, StopSignal


@dataclass(frozen=True)
class DirectoryCursor:
    binding: DeviceBinding
    directories: tuple[str, ...]
    directory_index: int
    after_path: str | None

    def __post_init__(self):
        roots = _roots(self.binding, self.directories)
        object.__setattr__(self, "directories", roots)
        index = self.directory_index
        if type(index) is not int or not 0 <= index < len(roots):
            raise ValueError("目录游标必须指向原范围中的目录")
        if self.after_path is not None:
            identity = FileIdentity(self.binding, self.after_path)
            if not identity.path.startswith(roots[index] + "/"):
                raise ValueError("目录游标不属于原目录")


@dataclass(frozen=True)
class DirectoryRequest:
    binding: DeviceBinding
    directories: tuple[str, ...]
    cursor: DirectoryCursor | None
    batch: int
    timeout_s: Decimal

    def __post_init__(self):
        roots = _roots(self.binding, self.directories)
        object.__setattr__(self, "directories", roots)
        if (type(self.batch) is not int or not 1 <= self.batch <= 128
                or not isinstance(self.timeout_s, Decimal)
                or not self.timeout_s.is_finite() or self.timeout_s <= 0):
            raise ValueError("目录批次必须为 1—128，期限必须为有限正秒数")
        if self.cursor is not None and (not isinstance(self.cursor, DirectoryCursor)
                or self.cursor.binding != self.binding or self.cursor.directories != roots):
            raise ValueError("目录游标不属于原绑定及完整范围")


@dataclass(frozen=True)
class DirectoryRead:
    outcome: RawToolOutcome | None
    error: ErrorValue | None
    page: Page[FileIdentity, DirectoryCursor] | None

    def __post_init__(self):
        if (self.error is None) == (self.page is None):
            raise ValueError("目录返回必须明确为可靠页或读取错误")


class DirectoryReader(Protocol):
    """返回本次实际目录页；取消等待须先完成实际调用收场。"""
    async def read_directory(self, request: DirectoryRequest, *, stop: StopSignal) -> DirectoryRead:
        ...


def _roots(binding, directories):
    if not isinstance(binding, DeviceBinding):
        raise ValueError("目录请求必须保留原设备绑定")
    roots = tuple(directories)
    if not roots:
        raise ValueError("完整目录范围不能为空")
    for root in roots:
        FileIdentity(binding, root)
    roots = tuple(sorted(roots, key=lambda root: root + "/"))
    if len(set(roots)) != len(roots) or any(
            a.startswith(b + "/") or b.startswith(a + "/")
            for i, a in enumerate(roots) for b in roots[i + 1:]):
        raise ValueError("完整目录范围不能重复或相互包含")
    return roots



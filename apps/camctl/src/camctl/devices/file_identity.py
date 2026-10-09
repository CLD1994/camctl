"""绑定范围内按完整路径定位的稳定文件身份。"""
from dataclasses import dataclass
from pathlib import PurePosixPath

from camctl.devices.bindings import DeviceBinding


@dataclass(frozen=True)
class FileIdentity:
    """路径不复用的设备文件；排序键与定位结构使用同一原绑定。"""

    binding: DeviceBinding
    path: str

    def __post_init__(self):
        if not isinstance(self.binding, DeviceBinding):
            raise ValueError("文件身份必须携带原设备绑定")
        if (not isinstance(self.path, str) or not self.path.startswith("/")
                or self.path.startswith("//") or self.path == "/" or "\0" in self.path
                or any(part in (".", "..") for part in self.path.split("/"))
                or str(PurePosixPath(self.path)) != self.path):
            raise ValueError("文件身份必须使用规范的完整设备路径")
        self.path.encode("utf-8")

    @property
    def sort_key(self) -> tuple[str, str, str]:
        return self.binding.device_id, self.binding.driver_id, self.path

    def as_json(self) -> dict:
        return {"device_id": self.binding.device_id, "driver_id": self.binding.driver_id, "path": self.path}

    @classmethod
    def from_json(cls, value) -> "FileIdentity":
        if not isinstance(value, dict) or set(value) != {"device_id", "driver_id", "path"}:
            raise ValueError("路径文件身份结构无效")
        return cls(DeviceBinding(value["device_id"], value["driver_id"]), value["path"])

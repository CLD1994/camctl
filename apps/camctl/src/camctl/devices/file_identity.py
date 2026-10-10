"""绑定范围内按完整路径定位的稳定文件身份。"""
from dataclasses import dataclass
from pathlib import PurePosixPath
from collections.abc import Mapping

from camctl.devices.bindings import DeviceBinding
from camctl.contracts.json_values import parse_exact_json


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

    @classmethod
    def from_source(cls, identity_key, locator) -> "FileIdentity":
        """从可靠源身份取得原绑定，定位只保存与身份一致的路径。"""
        value = parse_exact_json(identity_key)
        if (not isinstance(value, list) or len(value) != 3
                or any(not isinstance(part, str) or not part for part in value)):
            raise ValueError("路径源身份必须包含原设备、驱动和路径")
        identity = cls(DeviceBinding(value[0], value[1]), value[2])
        if not isinstance(locator, Mapping) or dict(locator) != {"path": identity.path}:
            raise ValueError("源定位与原稳定路径身份不一致")
        return identity

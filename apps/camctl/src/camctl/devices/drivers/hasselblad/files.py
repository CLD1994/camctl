"""拍摄时固定的相机文件身份；不访问文件系统或业务数据库。"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
import json
import re

from camctl.contracts.json_values import parse_exact_json
from camctl.devices.bindings import DeviceBinding
from .definitions import DRIVER_ID


class DownloadVariant(Enum):
    ORIGINAL = "original"
    THUMBNAIL = "thumbnail"


@dataclass(frozen=True)
class CameraFileIdentity:
    """原绑定、原图名称、读取种类及拍摄时核验的内容摘要。"""

    binding: DeviceBinding
    original_name: str
    variant: DownloadVariant
    sha256: str

    def __post_init__(self):
        if not isinstance(self.binding, DeviceBinding) or self.binding.driver_id != DRIVER_ID:
            raise ValueError("相机文件必须属于 Hasselblad 原绑定")
        name = self.original_name
        if (not isinstance(name, str) or not name or name in (".", "..")
                or any(c in name for c in ("/", "\\", "\0", "\r", "\n"))):
            raise ValueError("相机原图名称必须是单个直接文件名")
        if not isinstance(self.variant, DownloadVariant):
            raise ValueError("相机文件读取种类无效")
        if not isinstance(self.sha256, str) or re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None:
            raise ValueError("相机文件必须携带拍摄时的 SHA-256")

    @property
    def file_identity(self) -> str:
        return json.dumps(["hb-download-v1", self.original_name, self.variant.value, self.sha256],
                          ensure_ascii=False, separators=(",", ":"))

    def locator(self) -> dict:
        return {"original_name": self.original_name, "variant": self.variant.value, "sha256": self.sha256}

    @classmethod
    def from_source(cls, identity_key, locator):
        value = parse_exact_json(identity_key)
        if not isinstance(value, list) or len(value) != 3:
            raise ValueError("相机源身份必须包含原设备、驱动和固定文件身份")
        opaque = parse_exact_json(value[2])
        if not isinstance(opaque, list) or len(opaque) != 4 or opaque[0] != "hb-download-v1":
            raise ValueError("相机固定文件身份版本或结构无效")
        identity = cls(DeviceBinding(value[0], value[1]), opaque[1], DownloadVariant(opaque[2]), opaque[3])
        if not isinstance(locator, Mapping) or dict(locator) != identity.locator():
            raise ValueError("相机源定位与原固定身份不一致")
        return identity

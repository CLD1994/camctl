"""拍摄时固定的机内文件定位与身份；不访问文件系统或业务数据库。"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
import json
from pathlib import PurePosixPath
import re
import unicodedata

from camctl.contracts.json_values import json_equal, parse_exact_json
from camctl.devices.bindings import DeviceBinding
from .definitions import DRIVER_ID
from .identity import CameraIdentity


class DownloadVariant(StrEnum):
    ORIGINAL = "original"
    THUMBNAIL = "thumbnail"


class CameraLocatorStatus(StrEnum):
    PATH = "path"
    EXPLICIT_MISSING = "explicit_missing"


def _camera_name_from_path(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise ValueError("机内路径必须是绝对 POSIX 路径")
    if any(unicodedata.category(character) in {"Cc", "Zl", "Zp"} for character in value):
        raise ValueError("机内路径不能包含控制字符")
    parts = value.split("/")[1:]
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ValueError("机内路径包含空段、. 或 ..")
    path = PurePosixPath(value)
    if str(path) != value or not path.name:
        raise ValueError("机内路径必须是规范的绝对 POSIX 文件路径")
    return path.name


@dataclass(frozen=True, slots=True)
class CameraFileLocator:
    """`camera file:` 的互斥结果：合法完整路径或工具明确报告缺名。"""

    status: CameraLocatorStatus
    camera_path: str | None = None
    camera_name: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, CameraLocatorStatus):
            raise ValueError("机内文件定位状态无效")
        if self.status is CameraLocatorStatus.PATH:
            if self.camera_path is None or self.camera_name is None:
                raise ValueError("合法机内定位必须包含完整路径和文件名")
            name = _camera_name_from_path(self.camera_path)
            if self.camera_name != name:
                raise ValueError("机内文件名必须与完整路径末段一致")
        elif self.camera_path is not None or self.camera_name is not None:
            raise ValueError("明确缺名状态不能携带机内路径或文件名")

    def as_json(self) -> dict[str, str | None]:
        return {
            "status": self.status.value,
            "camera_path": self.camera_path,
            "camera_name": self.camera_name,
        }

    @classmethod
    def from_json(cls, value: object) -> "CameraFileLocator":
        if not isinstance(value, Mapping) or set(value) != {"status", "camera_path", "camera_name"}:
            raise ValueError("机内文件定位成员无效")
        status = value["status"]
        if not isinstance(status, str):
            raise ValueError("机内文件定位状态必须是字符串")
        try:
            parsed_status = CameraLocatorStatus(status)
        except ValueError as error:
            raise ValueError("机内文件定位状态不受支持") from error
        camera_path = value["camera_path"]
        camera_name = value["camera_name"]
        if camera_path is not None and not isinstance(camera_path, str):
            raise ValueError("机内完整路径必须是字符串或 null")
        if camera_name is not None and not isinstance(camera_name, str):
            raise ValueError("机内文件名必须是字符串或 null")
        return cls(parsed_status, camera_path, camera_name)


@dataclass(frozen=True, slots=True)
class CameraFileIdentity:
    """原设备绑定、完整机内路径、下载文件名及拍摄时核验的内容摘要。"""

    binding: DeviceBinding
    camera_path: str
    camera_name: str
    variant: DownloadVariant
    sha256: str
    serial: str
    product_id: int
    size_bytes: int

    def __post_init__(self) -> None:
        if not isinstance(self.binding, DeviceBinding) or self.binding.driver_id != DRIVER_ID:
            raise ValueError("相机文件必须属于 Hasselblad 原绑定")
        if (
            not isinstance(self.camera_name, str)
            or self.camera_name != _camera_name_from_path(self.camera_path)
        ):
            raise ValueError("相机下载文件名必须与完整机内路径末段一致")
        if not isinstance(self.variant, DownloadVariant):
            raise ValueError("相机文件读取种类无效")
        if not isinstance(self.sha256, str) or re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None:
            raise ValueError("相机文件必须携带拍摄时的 SHA-256")
        CameraIdentity(self.serial, self.product_id)
        if type(self.size_bytes) is not int or self.size_bytes <= 0:
            raise ValueError("相机文件必须携带拍摄时的完整长度")

    @property
    def file_identity(self) -> str:
        return json.dumps(
            ["hb-download-v3", self.serial, self.product_id, self.camera_path,
             self.camera_name, self.variant.value, self.size_bytes, self.sha256],
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def locator(self) -> dict[str, str | int]:
        return {
            "serial": self.serial,
            "product_id": self.product_id,
            "size_bytes": self.size_bytes,
            "camera_path": self.camera_path,
            "camera_name": self.camera_name,
            "variant": self.variant.value,
            "sha256": self.sha256,
        }

    @classmethod
    def from_source(cls, identity_key: str, locator: object) -> "CameraFileIdentity":
        value = parse_exact_json(identity_key)
        if not isinstance(value, list) or len(value) != 3:
            raise ValueError("相机源身份必须包含原设备、驱动和固定文件身份")
        opaque = parse_exact_json(value[2])
        if not isinstance(opaque, list) or len(opaque) != 8 or opaque[0] != "hb-download-v3":
            raise ValueError("相机固定文件身份版本或结构无效")
        identity = cls(
            DeviceBinding(value[0], value[1]),
            opaque[3],
            opaque[4],
            DownloadVariant(opaque[5]),
            opaque[7],
            opaque[1],
            opaque[2],
            opaque[6],
        )
        expected = identity.locator()
        if not isinstance(locator, Mapping) or not json_equal(dict(locator), expected):
            raise ValueError("相机源定位与原固定身份不一致")
        return identity

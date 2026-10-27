"""Hasselblad 预期机身身份与规范资源键。"""

from dataclasses import dataclass
import json
from unicodedata import category

from camctl.devices.drivers.hasselblad.definitions import DRIVER_ID


@dataclass(frozen=True)
class CameraIdentity:
    serial: str
    product_id: int

    def __post_init__(self) -> None:
        if (not isinstance(self.serial, str) or not self.serial
                or any(category(character) == "Cc" for character in self.serial)):
            raise ValueError("Hasselblad serial 必须是非空且不含控制字符的字符串")
        if type(self.product_id) is not int or not 1 <= self.product_id <= 65535:
            raise ValueError("Hasselblad product_id 必须在 1～65535 之间")

    @property
    def resource_key(self) -> str:
        """资源键只由驱动和精确 serial 决定，产品标识用于选择范围核对。"""
        return json.dumps([DRIVER_ID, self.serial], ensure_ascii=True, separators=(",", ":"))

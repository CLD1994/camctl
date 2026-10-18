"""无设备 IO 的合成驱动声明。"""

from collections.abc import Mapping

from camctl.devices.ports import DriverDeclaration
from camctl.devices.tasks import CaptureStorage


def synthetic_driver_declaration(*, delete_supported: bool = False) -> DriverDeclaration:
    """构造参数显式、其余能力均不支持的测试驱动声明。"""
    return DriverDeclaration(
        control_supported=False,
        stop_supported=False,
        query_supported=False,
        result_supported=False,
        read_supported=False,
        digest_supported=False,
        delete_supported=delete_supported,
    )


def synthetic_source_storage_resolver(
    declaration: Mapping[str, object],
) -> CaptureStorage:
    """按合成设备配置选择正式源，缺省模拟既有设备文件来源。"""
    raw = declaration.get("synthetic_output_storage", CaptureStorage.DEVICE.value)
    return CaptureStorage(raw)

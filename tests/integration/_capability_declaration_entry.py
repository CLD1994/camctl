"""复制到外部安装目录的测试装配；使用真实静态及运行登记。"""

from __future__ import annotations

import sys
from pathlib import Path

from camctl.bootstrap.application import ConfigAdapter
from camctl.devices.catalog import DriverDefinition
from camctl.devices.definitions_runtime import current_driver_definitions, register_driver_definitions
from camctl.devices.drivers.builtin import register_builtin_drivers
from camctl.devices.drivers.registry import DriverEntry, DriverStatus
from camctl.devices.drivers.runtime import current_registry, register_drivers
from camctl.devices.evidence import EvidenceRegistry
from camctl.devices.ports import DriverDeclaration
from camctl.devices.tasks import CaptureStorage
from camctl_fixtures import install_stub_driver


def resolve_storage(declaration):
    return CaptureStorage(declaration["fixture_storage"])


declaration = DriverDeclaration(
    control_supported=False, stop_supported=False, query_supported=False,
    result_supported=False, read_supported=False, digest_supported=False,
    delete_supported=False,
)
register_driver_definitions(DriverDefinition(
    "vendor.future_empty.v9", {}, declaration, resolve_storage,
))
register_drivers(DriverEntry(
    "vendor.future_empty.v9", object(), declaration, EvidenceRegistry(()),
    DriverStatus.SOFTWARE_CONTRACT_VERIFIED,
))
install_stub_driver({"driver_id": "vendor.future_capture.v9"})

# 只禁止设备端口调用；声明、配置解析、Catalog 与 CLI 均保留生产路径。
config = ConfigAdapter(home=Path.home()).load(Path(sys.argv[sys.argv.index("--config") + 1]))
register_builtin_drivers(config)


def forbidden_call(*args, **kwargs):
    Path(__file__).with_name("driver-calls").write_text("设备端口被调用", encoding="utf8")
    raise AssertionError("describe 不得调用拍摄或删除端口")


for driver_id in current_driver_definitions().drivers:
    entry = current_registry().entry(driver_id)
    if entry is not None:
        for operation in ("control", "delete"):
            if callable(getattr(entry.driver, operation, None)):
                setattr(entry.driver, operation, forbidden_call)

from camctl.cli import main  # noqa: E402

raise SystemExit(main(sys.argv[1:]))

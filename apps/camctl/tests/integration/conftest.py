"""组件集成测试从权威来源准备真实包资源。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parents[2]


def pytest_configure(config) -> None:
    sys.path.insert(0, str(PROJECT_DIR / "scripts"))
    import sync_resources

    sync_resources.sync_package_resources(PROJECT_DIR)


@pytest.fixture
def isolated_driver_registries():
    """隔离静态定义和运行驱动登记，允许用例中途重新装配。"""
    from camctl.devices.definitions_runtime import reset_driver_definitions
    from camctl.devices.drivers.runtime import reset_drivers

    def reset():
        reset_drivers()
        reset_driver_definitions()

    reset()
    try:
        yield reset
    finally:
        reset()

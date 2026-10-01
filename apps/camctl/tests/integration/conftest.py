"""组件集成测试从权威来源准备真实包资源。"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[2]


def pytest_configure(config) -> None:
    sys.path.insert(0, str(PROJECT_DIR / "scripts"))
    import sync_resources

    sync_resources.sync_package_resources(PROJECT_DIR)

"""camctl 组件测试的公共装配。

包资源（Schema、规格 SQL、内部登记）在会话开始前从仓库权威来源
同步生成；这是测试环境准备，等价于安装完整组件。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]


def pytest_sessionstart(session) -> None:
    sys.path.insert(0, str(PROJECT_DIR / "scripts"))
    import sync_resources

    sync_resources.sync_package_resources(PROJECT_DIR)

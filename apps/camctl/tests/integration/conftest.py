"""组件集成测试的公共装配。

集成测试允许构建、安装与真实文件系统访问；本装配在会话开始前
把权威资源同步到包目录，保证进程内导入同样能定位资源。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[2]


def pytest_sessionstart(session) -> None:
    sys.path.insert(0, str(PROJECT_DIR / "scripts"))
    import sync_resources

    sync_resources.sync_package_resources(PROJECT_DIR)

"""根跨组件集成测试共享夹具：Linux 或 WSL 构建的真实 host-demo。

session 级共享一次构建：集成测试按目录前台独占顺序执行，多文件
复用当前工作区的构建产物不引入并发。
"""

from __future__ import annotations

import pytest

from _wsl_host_demo import build_host_demo


@pytest.fixture(scope="session")
def host_demo() -> str:
    return build_host_demo()

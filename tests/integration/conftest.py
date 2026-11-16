"""根跨组件集成测试共享夹具：真实host-demo与显式客户端会话。

session 级共享一次构建：集成测试按目录前台独占顺序执行，多文件
复用当前工作区的构建产物不引入并发。客户端会话由调用者显式绑定，
每个测试独立拥有并在收场时关闭。
"""

from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest

from _wsl_host_demo import build_host_demo


@pytest.fixture(scope="session")
def host_demo(request) -> str:
    return build_host_demo(request.addfinalizer)


@pytest.fixture
def reuse_client(request, monkeypatch):
    """复用本世界的客户端；调用者选择绑定时点，避免外部写入冲突。"""
    def bind(deployment) -> None:
        if sys.platform != "linux":
            return
        from _client_session import ClientSession

        integration = Path(__file__).resolve().parent
        session = ClientSession(
            ["node", "--import", "tsx", str(integration / "client_driver.ts")],
            cwd=integration.parents[1] / "apps/client", env=os.environ.copy(),
            root=deployment.root, timeout=120,
        )
        request.addfinalizer(session.close)
        sequence = 0

        def call(operation: str, *args: str, output: Path) -> dict:
            nonlocal sequence
            sequence += 1
            receipt = output.with_name(f"{output.stem}-{sequence}{output.suffix}")
            return session.call(operation, *args, output=receipt)

        monkeypatch.setattr(deployment, "run_client_driver", call)

    return bind

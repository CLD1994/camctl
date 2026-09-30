"""hatch 自定义构建钩子：构建前从仓库权威来源刷新包资源。

权威来源只存在于仓库根；从 sdist 构建时仓库根不可用，
此时使用 sdist 内已经生成的资源副本。
"""

from __future__ import annotations

import sys
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

_PROJECT_DIR = Path(__file__).resolve().parent


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data) -> None:
        sys.path.insert(0, str(_PROJECT_DIR / "scripts"))
        try:
            import sync_resources
        finally:
            sys.path.remove(str(_PROJECT_DIR / "scripts"))

        repo_root = sync_resources.repo_root_of(_PROJECT_DIR)
        if repo_root.is_dir() and (repo_root / "protocol").is_dir():
            sync_resources.sync_package_resources(_PROJECT_DIR)
        elif not sync_resources.resource_root(_PROJECT_DIR).is_dir():
            raise RuntimeError(
                "构建环境既没有仓库权威来源，也没有已生成的包资源；"
                "无法构造完整的 camctl 安装物"
            )

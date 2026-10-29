"""组件直接运行 pytest 时加载共享预算插件。"""
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))


def pytest_addoption(parser, pluginmanager):
    # 共享模块可能已被测试辅助导入；由 pytest 核对是否注册并统一去重。
    import camctl_memory_plugin

    if not pluginmanager.has_plugin("camctl_memory_plugin"):
        pluginmanager.register(camctl_memory_plugin, "camctl_memory_plugin")

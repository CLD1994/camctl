"""根测试统一加载测试侧内存预算配置，不约束 pytest 自身。"""
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
pytest_plugins = ("camctl_memory_plugin",)

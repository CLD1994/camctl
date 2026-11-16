"""根测试统一加载测试侧预算配置和类别选择。"""
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
pytest_plugins = ("camctl_memory_plugin", "test_kind_plugin")

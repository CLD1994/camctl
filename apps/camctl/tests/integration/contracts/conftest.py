"""结构规则共享同一份真实源码语法树，检查过程只读取它。"""

from __future__ import annotations

import ast
from pathlib import Path
from types import MappingProxyType

import pytest


@pytest.fixture(scope="session")
def source_trees():
    root = Path(__file__).resolve().parents[3] / "src" / "camctl"
    trees = {}
    for path in sorted(root.rglob("*.py")):
        parts = list(path.relative_to(root.parent).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        trees[".".join(parts)] = ast.parse(
            path.read_text(encoding="utf-8"), filename=str(path)
        )
    return MappingProxyType(trees)

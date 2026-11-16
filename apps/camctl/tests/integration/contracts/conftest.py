"""每份真实源码解析一次，结构规则共享检查事实。"""

from __future__ import annotations

import ast
from pathlib import Path
from types import MappingProxyType

import pytest


@pytest.fixture(scope="session")
def source_inspection():
    from support.dependency_layers import _imported_modules
    from support.external_boundaries import (
        _bare_excepts, _file_entries, _network_dependencies, _subprocess_creations,
    )

    root = Path(__file__).resolve().parents[3] / "src" / "camctl"
    findings = {}
    for path in sorted(root.rglob("*.py")):
        parts = list(path.relative_to(root.parent).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        module = ".".join(parts)
        tree = ast.parse(
            path.read_text(encoding="utf-8"), filename=str(path)
        )
        nodes = tuple(ast.walk(tree))
        findings[module] = {
            "imports": _imported_modules(module, nodes),
            "subprocess": _subprocess_creations(module, nodes),
            "network": _network_dependencies(module, nodes),
            "files": _file_entries(module, nodes),
            "bare_excepts": _bare_excepts(module, nodes),
        }
        # 后续规则只需要发现的事实，不保留整份源码树。
        del nodes, tree
    return MappingProxyType(findings)

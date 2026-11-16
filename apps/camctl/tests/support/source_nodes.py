"""为源码结构检查提供可重复遍历的节点。"""

from __future__ import annotations

import ast
from collections.abc import Sequence


def collect_source_nodes(source: str | ast.AST | Sequence[ast.AST]) -> Sequence[ast.AST]:
    if isinstance(source, str):
        source = ast.parse(source)
    if isinstance(source, ast.AST):
        return tuple(ast.walk(source))
    return source

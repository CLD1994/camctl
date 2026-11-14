"""检查数据库基础设施的传递依赖。"""

import ast
from collections import deque
from dataclasses import dataclass
from importlib.util import resolve_name


FORBIDDEN = (
    "camctl.acceptance", "camctl.bootstrap", "camctl.cli", "camctl.capture",
    "camctl.cancellation", "camctl.outputs", "camctl.reporting", "camctl.scheduling",
    "camctl.session", "camctl.motor", "camctl.persistence.repositories",
    "camctl.persistence.initialization", "camctl.persistence.directory_switch",
    "camctl.devices.catalog",
)


@dataclass(frozen=True)
class Source:
    text: str
    package: bool = False


def _graph(sources: dict[str, Source]):
    """解析模块、包初始化和常见动态加载；函数内导入也属于依赖。"""
    graph = {name: set() for name in sources}
    dynamic = {name: [] for name in sources}
    for name, source in sources.items():
        tree = ast.parse(source.text, filename=name)
        package = name if source.package else name.rpartition(".")[0]
        aliases = {"__import__": "builtins.__import__"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for item in node.names:
                    if item.name in {"importlib", "builtins"}:
                        aliases[item.asname or item.name] = item.name
                    if item.name in sources:
                        graph[name].add(item.name)
            elif isinstance(node, ast.ImportFrom):
                base = resolve_name("." * node.level + (node.module or ""), package) if node.level else (node.module or "")
                if base in sources:
                    graph[name].add(base)
                for item in node.names:
                    target = f"{base}.{item.name}"
                    if target in sources:
                        graph[name].add(target)
                    if target in {"importlib.import_module", "builtins.__import__"}:
                        aliases[item.asname or item.name] = target

        def expression(node):
            if isinstance(node, ast.Name):
                return aliases.get(node.id, node.id)
            if isinstance(node, ast.Attribute):
                return f"{expression(node.value)}.{node.attr}"
            return ""

        # 常见简单别名转发，包括多层赋值；不解释任意运行时代码。
        for _ in range(len(tree.body) + 1):
            changed = False
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    value = expression(node.value)
                    if value in {"importlib", "builtins", "importlib.import_module", "builtins.__import__"}:
                        for target in node.targets:
                            if isinstance(target, ast.Name) and aliases.get(target.id) != value:
                                aliases[target.id] = value
                                changed = True
            if not changed:
                break
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and expression(node.func) in {"importlib.import_module", "builtins.__import__"}:
                target = node.args[0].value if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str) else "<无法静态确定>"
                dynamic[name].append((node.lineno, target))

        # 导入模块时 Python 也会执行各级包的初始化代码。
        parts = name.split(".")
        for index in range(1, len(parts)):
            parent = ".".join(parts[:index])
            if parent in sources and sources[parent].package:
                graph[name].add(parent)
    return graph, dynamic


def _forbidden(name: str, root: str) -> bool:
    if name == "camctl.persistence.business":
        return root != name
    return any(name == prefix or name.startswith(prefix + ".") for prefix in FORBIDDEN)


def _violations(sources: dict[str, Source], roots: tuple[str, ...]) -> list[str]:
    graph, dynamic = _graph(sources)
    violations = []
    for root in roots:
        assert root in graph, f"缺少数据库基础设施根模块: {root}"
        pending = deque([(root,)])
        seen = {root}
        while pending:
            path = pending.popleft()
            current = path[-1]
            for line, target in dynamic[current]:
                violations.append(f"{' -> '.join(path)} -> 动态导入 {target} (第 {line} 行)")
            for target in sorted(graph[current]):
                if target in seen:
                    continue
                seen.add(target)
                route = (*path, target)
                if _forbidden(target, root):
                    violations.append(" -> ".join(route))
                else:
                    pending.append(route)
    return violations

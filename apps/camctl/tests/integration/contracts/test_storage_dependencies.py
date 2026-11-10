"""检查数据库基础设施的真实传递依赖，不执行项目源码。"""

import ast
from collections import deque
from dataclasses import dataclass
from importlib.util import resolve_name
from pathlib import Path

import pytest


ROOTS = (
    "camctl.persistence.atomic",
    "camctl.persistence.transaction_data",
    "camctl.persistence.runtime",
    "camctl.persistence.executor",
    "camctl.persistence.business",
)
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


def _sources(root: Path) -> dict[str, Source]:
    result = {}
    for path in sorted(root.rglob("*.py")):
        parts = path.relative_to(root).with_suffix("").parts
        package = parts[-1] == "__init__"
        name = ".".join(("camctl", *(parts[:-1] if package else parts)))
        result[name] = Source(path.read_text(encoding="utf-8"), package)
    return result


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


def test_database_infrastructure_has_no_business_dependency_closure():
    root = Path(__file__).resolve().parents[3] / "src" / "camctl"
    violations = _violations(_sources(root), ROOTS)
    assert not violations, "数据库基础设施可达业务模块或动态导入:\n" + "\n".join(violations)


@pytest.mark.parametrize("middle", [
    "import camctl.capture.rules as rules",
    "from camctl.capture.rules import rule",
    "def later():\n    from camctl.capture import rules",
])
def test_rejects_direct_and_function_local_business_imports(middle):
    sources = {"camctl.persistence.atomic": Source(middle), "camctl.capture.rules": Source("")}
    assert any("camctl.persistence.atomic -> camctl.capture.rules" in item for item in _violations(sources, ("camctl.persistence.atomic",)))


@pytest.mark.parametrize(("via_package", "expected"), [
    (True, "camctl.neutral -> camctl.neutral.helper -> camctl.capture.rules"),
    (False, "camctl.neutral.compat -> camctl.capture.rules"),
])
def test_rejects_transitive_package_initialization_and_compatibility_forwarding(via_package, expected):
    sources = {
        "camctl.persistence.atomic": Source("import camctl.neutral.compat"),
        "camctl.neutral": Source("from . import helper" if via_package else "", package=True),
        "camctl.neutral.helper": Source("from camctl.capture import rules"),
        "camctl.neutral.compat": Source("" if via_package else "from camctl.capture.rules import rule"),
        "camctl.capture.rules": Source(""),
    }
    violations = _violations(sources, ("camctl.persistence.atomic",))
    assert any(expected in item for item in violations)


@pytest.mark.parametrize(("statement", "target"), [
    ("import importlib as loader\nloader.import_module('camctl.capture.rules')", "camctl.capture.rules"),
    ("from importlib import import_module as load\nload(variable)", "<无法静态确定>"),
    ("import builtins as b\nload = b.__import__\nload('camctl.capture.rules')", "camctl.capture.rules"),
    ("__import__(variable)", "<无法静态确定>"),
])
def test_rejects_dynamic_imports_even_when_target_cannot_be_resolved(statement, target):
    violations = _violations({"camctl.persistence.atomic": Source(statement)}, ("camctl.persistence.atomic",))
    assert len(violations) == 1 and f"动态导入 {target}" in violations[0]


def test_allows_opaque_callback_reverse_dependency_and_fixed_resources():
    sources = {
        "camctl.persistence.atomic": Source("from importlib import resources\nimport json\nfrom camctl.neutral import ordinary_symbol\ndef run(callback):\n    fixed = resources.files('camctl').joinpath('schema.json').read_text()\n    json.loads(fixed)\n    return callback()"),
        "camctl.neutral": Source(""),
        "camctl.capture.rules": Source("from camctl.persistence import atomic"),
    }
    assert _violations(sources, ("camctl.persistence.atomic",)) == []


@pytest.mark.parametrize("imports_assembly", [False, True])
def test_generic_logging_service_is_traversed_for_business_assembly(imports_assembly):
    sources = {
        "camctl.persistence.atomic": Source("from camctl.logging_runtime import service"),
        "camctl.logging_runtime.service": Source(
            "from camctl.bootstrap import lifecycle" if imports_assembly else "import queue"
        ),
        "camctl.bootstrap.lifecycle": Source(""),
    }
    violations = _violations(sources, ("camctl.persistence.atomic",))
    if imports_assembly:
        assert violations == [
            "camctl.persistence.atomic -> camctl.logging_runtime.service -> camctl.bootstrap.lifecycle"
        ]
    else:
        assert violations == []

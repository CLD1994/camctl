"""从生产纯设置函数生成可独立交付的诊断编码；不修改源节点。"""
from __future__ import annotations

import argparse
import ast
import builtins
from pathlib import Path
import symtable
import sys

REPO = Path(__file__).resolve().parents[3]
SOURCE = REPO / "apps/camctl/src/camctl/devices/drivers/adb_cameras/commands.py"
REGISTRY = REPO / "apps/camctl/src/camctl/devices/drivers/registry.py"
TARGET = REPO / "docs/hardware/action6_timelapse_candidate.py"
IMPORTS = {"Mapping": "from collections.abc import Mapping",
           "dataclass": "from dataclasses import dataclass",
           "Decimal": "from decimal import Decimal",
           "StrEnum": "from enum import StrEnum"}
EXCEPTION = "CapabilityNotDeclaredError"
HEADER = ('"""自动生成的独立诊断编码；请运行 render_action6_diagnostic_candidate.py 更新。\n'
          '模板、字面量及编码函数由生产 commands.py 同源派生，不手工修改。\n"""\n'
          'from __future__ import annotations\n')


def global_references(source):
    """只解释当前模块自由名称；注解与生成模块一样延后求值。"""
    table = symtable.symtable("from __future__ import annotations\n" + source, str(SOURCE), "exec")
    names = set()

    def visit(scope):
        names.update(symbol.get_name() for symbol in scope.get_symbols()
                     if symbol.is_global() and symbol.is_referenced())
        for child in scope.get_children():
            visit(child)
    visit(table)
    return names


def generated_source():
    source = SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    definitions = {}
    imports = {}
    bindings = set()

    def bind(name):
        if name in bindings:
            raise ValueError(f"生产编码模块绑定重复：{name}")
        bindings.add(name)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            name = node.name
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            name = node.target.id
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                name = alias.asname or alias.name
                bind(name)
                if node.level != 0 or node.module is None:
                    raise ValueError(f"生产编码模块不支持相对 ImportFrom：{name}")
                if node.module == "__future__" and (alias.name != "annotations" or alias.asname is not None):
                    raise ValueError(f"生产编码模块不支持该 future ImportFrom：{name}")
                imports[name] = (node.level, node.module, alias.name, alias.asname)
            continue
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        else:
            raise ValueError(f"生产编码模块出现未支持的顶层形状：{type(node).__name__}")
        bind(name)
        definitions[name] = node
    selected = set()
    needed_imports = set()
    pending = ["_encode_settings"]
    while pending:
        name = pending.pop()
        if name in selected or name in needed_imports:
            continue
        if name in vars(builtins):
            if name in bindings:
                raise ValueError(f"闭包依赖的内建名称被模块重新绑定：{name}")
            continue
        if name in IMPORTS:
            allowed = ast.parse(IMPORTS[name]).body[0]
            identity = (allowed.level, allowed.module, allowed.names[0].name, allowed.names[0].asname)
            if imports.get(name) != identity:
                raise ValueError(f"标准库依赖与允许形状不符：{name}")
            needed_imports.add(name)
            continue
        if name == EXCEPTION:
            if imports.get(name) != (0, "camctl.devices.drivers.registry", EXCEPTION, None):
                raise ValueError(f"映射缺口异常的生产来源已变化：{EXCEPTION}")
            selected.add(name)
            continue
        if name not in definitions:
            raise ValueError(f"独立诊断编码存在未解释的运行依赖：{name}")
        selected.add(name)
        node = definitions[name]
        for nested in ast.walk(node):
            if isinstance(nested, (ast.Import, ast.ImportFrom)):
                raise ValueError(f"{name} 的纯设置闭包包含未支持的 {type(nested).__name__}")
        start = min([node.lineno, *(decorator.lineno for decorator in getattr(node, "decorator_list", []))])
        pending.extend(global_references("\n".join(source.splitlines()[start - 1:node.end_lineno])))
    registry = REGISTRY.read_text(encoding="utf-8")
    exceptions = [node for node in ast.parse(registry).body if isinstance(node, ast.ClassDef) and node.name == EXCEPTION]
    if len(exceptions) != 1:
        raise ValueError("映射缺口异常类缺失或重复")
    error = exceptions[0]
    if (len(error.bases) != 1 or not isinstance(error.bases[0], ast.Name) or error.bases[0].id != "Exception"
            or error.keywords or error.decorator_list or len(error.body) != 1
            or not isinstance(error.body[0], ast.Expr) or not isinstance(error.body[0].value, ast.Constant)
            or not isinstance(error.body[0].value.value, str)):
        raise ValueError("映射缺口异常不再是已核对的空 Exception 类，请重新审查独立边界")
    blocks = [HEADER, *(IMPORTS[name] for name in sorted(needed_imports)),
              ast.get_source_segment(registry, error)]
    for name, node in definitions.items():
        if name in selected:
            # get_source_segment 不包含函数／类的装饰器，按原始行补回完整定义。
            start = min([node.lineno, *(decorator.lineno for decorator in getattr(node, "decorator_list", []))])
            blocks.append("\n".join(source.splitlines()[start - 1:node.end_lineno]))
    result = "\n\n".join(blocks) + "\n"
    compile(result, str(TARGET), "exec")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--write", action="store_true")
    modes.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        expected = generated_source()
        if TARGET.is_file() and TARGET.read_text(encoding="utf-8") == expected:
            return 0
        if args.write:
            TARGET.write_text(expected, encoding="utf-8")
            print(f"已更新 {TARGET.relative_to(REPO)}")
            return 0
        raise ValueError(f"{TARGET.relative_to(REPO)} 与生产编码不一致；请运行本脚本 --write")
    except (OSError, SyntaxError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

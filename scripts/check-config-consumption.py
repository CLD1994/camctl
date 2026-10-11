"""执行入口配置消费的导入图与 AST 检查。

验证生产源码中拍摄执行入口对预算、期限、间隔与等待配置的实际消
费路径：运行时的尝试配置必须经统一的按设备配置读取接口构造（或
显式注入参数转发），设备调用意图必须引用运行时持有的配置字段，
不得在入口处内联数字或绕开统一接口。检查基于语法树与真实调用点，
不依赖文本搜索。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "apps" / "camctl" / "src" / "camctl"

UNIFIED_READERS = {"_device_attempts", "_device_seconds"}
RUNTIME_CONFIG_FIELDS = {"check_config", "stop_config", "start_config",
                         "query_config", "delete_config", "residual_config"}
ATTEMPT_CONFIG_KEYS = {"max_attempts", "timeout_s", "retry_interval_s"}
WAIT_CONFIG_CALLS = {"CaptureWaitConfig", "execution_wait_config"}


def _function_name(node: ast.AST) -> str | None:
    """取调用目标的简单名称；属性访问返回属性名。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_forwarded_parameter(node: ast.AST) -> bool:
    """值是名称引用（形参或局部变量），由装配调用方决定。"""
    return isinstance(node, ast.Name)


def _attempt_config_violations(call: ast.Call, path: Path) -> list[str]:
    """AttemptConfig 构造点：各预算关键字必须经统一读取或参数转发。"""
    issues = []
    for keyword in call.keywords:
        if keyword.arg not in ATTEMPT_CONFIG_KEYS:
            continue
        value = keyword.value
        if isinstance(value, ast.Call):
            if _function_name(value.func) in UNIFIED_READERS:
                continue
            issues.append(f"{path}: AttemptConfig 的 {keyword.arg} 调用了非统一读取接口")
        elif _is_forwarded_parameter(value):
            continue
        elif isinstance(value, ast.Constant):
            issues.append(
                f"{path}: AttemptConfig 的 {keyword.arg} 使用字面量 {value.value!r}")
        else:
            issues.append(f"{path}: AttemptConfig 的 {keyword.arg} 来源无法核对")
    return issues


def _runtime_construction_violations(call: ast.Call, path: Path) -> list[str]:
    """CaptureRuntime 构造点：配置字段必须经统一接口或注入转发。"""
    issues = []
    for keyword in call.keywords:
        if keyword.arg not in RUNTIME_CONFIG_FIELDS:
            continue
        value = keyword.value
        if isinstance(value, ast.Call):
            name = _function_name(value.func)
            if name == "AttemptConfig":
                issues.extend(_attempt_config_violations(value, path))
            elif _is_forwarded_parameter(value.func) or name in UNIFIED_READERS:
                continue
            else:
                issues.append(
                    f"{path}: CaptureRuntime 的 {keyword.arg} 经未知构造 {name}")
        elif _is_forwarded_parameter(value):
            continue
        else:
            issues.append(f"{path}: CaptureRuntime 的 {keyword.arg} 来源无法核对")
    return issues


def _config_expression_violations(value: ast.AST, path: Path) -> list[str]:
    """意图配置构造：预算值必须转发装配输入；仅本地单次预算允许 1。"""
    issues = []
    if not isinstance(value, ast.Call):
        return issues
    for keyword in value.keywords:
        if keyword.arg is None or keyword.arg not in ATTEMPT_CONFIG_KEYS:
            continue
        item = keyword.value
        if isinstance(item, (ast.Attribute, ast.Name)):
            continue
        if isinstance(item, ast.Constant):
            if keyword.arg == "max_attempts" and item.value == 1:
                # 主机源交付拷贝的本地单次预算协议：一次、无时限、无
                # 定时重试，由授予拷贝时保存的配置约束。
                continue
            if item.value is None:
                continue
            issues.append(
                f"{path}: 意图配置的 {keyword.arg} 使用字面量 {item.value!r}")
        else:
            issues.append(f"{path}: 意图配置的 {keyword.arg} 来源无法核对")
    return issues


def _intent_construction_violations(call: ast.Call, path: Path) -> list[str]:
    """设备调用意图：config 必须引用运行时配置字段或转发装配输入。"""
    issues = []
    for keyword in call.keywords:
        if keyword.arg != "config":
            continue
        value = keyword.value
        if value is None:
            continue
        if (isinstance(value, ast.Attribute)
                and isinstance(value.value, ast.Name)
                and value.value.id in ("runtime", "self")
                and value.attr in RUNTIME_CONFIG_FIELDS):
            continue
        if isinstance(value, ast.Call):
            name = _function_name(value.func)
            if name in UNIFIED_READERS or name in ("AttemptConfig", "OperationConfig"):
                if name in UNIFIED_READERS:
                    continue
                issues.extend(_config_expression_violations(value, path))
                continue
            if isinstance(value.func, (ast.Name, ast.Attribute)):
                continue
        if _is_forwarded_parameter(value):
            continue
        issues.append(f"{path}: AttemptIntent 的 config 未引用运行时配置字段")
    return issues


def _wait_config_violations(call: ast.Call, path: Path) -> list[str]:
    """等待配置：extra_wait_ms 必须经统一读取或注入转发。"""
    issues = []
    for keyword in call.keywords:
        if keyword.arg != "extra_wait_ms":
            continue
        value = keyword.value
        if isinstance(value, ast.Call):
            if _function_name(value.func) in UNIFIED_READERS:
                continue
            issues.append(f"{path}: extra_wait_ms 调用了非统一读取接口")
        elif _is_forwarded_parameter(value) or isinstance(
                value, ast.IfExp) or isinstance(value, ast.BoolOp):
            continue
        elif isinstance(value, ast.Constant):
            issues.append(f"{path}: extra_wait_ms 使用字面量 {value.value!r}")
        else:
            issues.append(f"{path}: extra_wait_ms 来源无法核对")
    return issues


def check() -> list[str]:
    issues: list[str] = []
    for path in sorted(SOURCE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _function_name(node.func)
            if name == "CaptureRuntime":
                issues.extend(_runtime_construction_violations(node, path))
            elif name == "AttemptIntent":
                issues.extend(_intent_construction_violations(node, path))
            elif name in WAIT_CONFIG_CALLS:
                issues.extend(_wait_config_violations(node, path))
    return issues


def main() -> int:
    issues = check()
    if issues:
        print("执行入口配置消费检查未通过：")
        for issue in issues:
            print(f"  {issue}")
        return 1
    print(
        "执行入口配置消费检查通过：运行时尝试配置经统一按设备接口构造，"
        "调用意图引用运行时配置字段，额外等待经统一读取。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""仓储源码事务与连接使用检查。"""

import ast

_ALLOWED_TRANSACTION_OWNERS = {"HistoryRepository", "SqliteSnapshotStore"}

# 电机事实读取，以及电机、主机拍摄和分段的原键核实，只建立一致读视图。
# 许可限定到函数，不允许仓储的其他入口自开事务，也不允许BEGIN IMMEDIATE。
_READ_TRANSACTION_FUNCTIONS = {
    "module:read_motor_facts",
    "module:_load_fixed_file",
    "module:verify_segment_finish",
    "module:read_segment_members",
    "module:read_segment_finish_candidates",
    "module:read_segment_finish_scope",
    "module:segment_output_candidate",
    "module:read_result_file_input_from_reader",
    "module:result_read_snapshot",
    "module:read_pairing_fact",
    "module:verify_ownership_from_result_page",
    "module:verify_result_file_pairing",
    "module:_read_transaction",
    "MotorRepository.verify_operation",
    "CancellationRepository.verify_motor_operation",
    "HostCaptureRepository.verify_operation",
}


def audit_repositories(sources: dict[str, str]) -> None:
    direct_connections: list[str] = []
    connection_commits: list[str] = []
    transaction_owners: set[str] = set()

    for name, source in sources.items():
        tree = ast.parse(source, filename=name)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)):
                continue
            if (node.func.attr == "commit"
                    and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "connection"):
                connection_commits.append(f"{name}:{node.lineno}")
            if (node.func.attr == "connect"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "sqlite3"):
                direct_connections.append(f"{name}:{node.lineno}")
            if (node.func.attr == "execute" and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value in (
                        "BEGIN", "BEGIN IMMEDIATE", "COMMIT", "ROLLBACK")):
                owner = _statement_owner(tree, node)
                if owner in _READ_TRANSACTION_FUNCTIONS:
                    assert node.args[0].value != "BEGIN IMMEDIATE", (
                        f"只读核实入口自开写事务: {name}:{node.lineno}"
                    )
                else:
                    transaction_owners.add(owner.split(".")[0])

    assert direct_connections == [], f"仓储直连数据库: {direct_connections}"
    assert connection_commits == [], f"仓储直接提交连接: {connection_commits}"
    assert transaction_owners <= _ALLOWED_TRANSACTION_OWNERS, (
        "仓储目录出现白名单外的事务控制: "
        f"{sorted(transaction_owners - _ALLOWED_TRANSACTION_OWNERS)}"
    )


def _statement_owner(tree: ast.Module, node: ast.AST) -> str:
    """定位事务控制语句的最外层所属函数，保留类名与方法名。"""
    for container in ast.walk(tree):
        if not isinstance(container, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node in ast.walk(container):
            name = _owning_class(tree, container)
            return f"{name}.{container.name}" if name else f"module:{container.name}"
    return "module"


def _owning_class(tree: ast.Module, func: ast.AST) -> str | None:
    for scope in ast.walk(tree):
        if isinstance(scope, ast.ClassDef) and func in ast.walk(scope):
            return scope.name
    return None

"""检查源码的外部调用边界。"""

from __future__ import annotations

import ast

#: 普通业务工具进程由受管入口创建；维护引擎在被更新环境外独立运行。
SUBPROCESS_OWNERS = ("camctl.operations.process", "camctl.update_engine")
#: 子进程创建的调用形态：属性接收者为标准库名时的方法名。
SUBPROCESS_CALLS = {
    ("subprocess", "Popen"),
    ("subprocess", "run"),
    ("subprocess", "call"),
    ("subprocess", "check_call"),
    ("subprocess", "check_output"),
    ("subprocess", "getoutput"),
    ("subprocess", "getstatusoutput"),
    ("os", "system"),
    ("os", "popen"),
    ("asyncio", "create_subprocess_exec"),
    ("asyncio", "create_subprocess_shell"),
}
#: 网络客户端相关的标准库与第三方根名。urllib.parse 只解析本地
#: URI，按完整模块名另行识别；其余相关模块的导入均拒绝。
NETWORK_ROOTS = (
    "socket", "ssl", "select", "urllib", "http", "ftplib", "smtplib",
    "poplib", "imaplib", "telnetlib", "xmlrpc", "requests", "httpx",
    "aiohttp", "websockets", "zmq",
)
#: 建立网络连接的调用形态（接收者限定名 + 方法名）。
NETWORK_CALLS = {
    ("socket", "socket"),
    ("socket", "create_connection"),
    ("asyncio", "open_connection"),
    ("asyncio", "start_server"),
    ("asyncio", "create_server"),
}
#: os 模块的文件系统操作（含低级 open）：目录、重命名、删除、
#: 同步、列举与状态查询都属于直接文件系统入口。
OS_FILE_OPS = {
    "replace", "rename", "unlink", "remove", "mkdir", "makedirs", "rmdir",
    "listdir", "scandir", "stat", "lstat", "fsync", "open", "truncate",
    "walk", "fwalk", "link", "symlink", "readlink", "chmod", "utime",
}
#: Path 与 Traversable 的特征方法名；str.replace 等同名无关调用
#: 不在集合内。
PATH_FILE_METHODS = {
    "write_text", "write_bytes", "read_bytes", "read_text", "unlink",
    "mkdir", "rmdir", "rename", "iterdir", "samefile", "touch", "symlink_to",
    "stat",
}
#: 文件系统直接调用允许出现的模块及责任。新模块需要直接读写
#: 文件时在此登记；间接经 host_files 或仓储连接的模块不在此列。
FILE_ENTRY_PREFIXES = (
    "camctl.host_files",        # 文件交接与读取：目录、移动、同步与读取
    "camctl.logging_runtime",   # 日志文件写入、副本复制与标记文件
    "camctl.persistence",       # 建库目录准备、init 目录切换、回放重建
    "camctl.reporting",         # 报告生成、发布、生命周期替换与撤下
    "camctl.outputs",           # 取回中间文件清理与交付文件读取
    "camctl.session.locks",     # 会话锁文件（POSIX 与 Windows 低级打开）
    "camctl.resources",         # 包内权威资源（importlib.resources）
    "camctl.acceptance.input",  # 受理输入的真实文件读取适配器
    "camctl.bootstrap",         # 装配入口：构建目录准备与临时移除
    "camctl.deployment",        # 独立部署检查、数据库副本与迁移入口
    "camctl.update_engine",     # 独立维护引擎：备份、安装、恢复与发布
)


def _receiver_call(node: ast.Call) -> tuple[str, str] | None:
    """返回（属性接收者限定名, 方法名）；非限定调用返回 None。"""
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id, func.attr
    return None


def _bare_excepts(module: str, source: str | ast.AST) -> list[str]:
    tree = ast.parse(source) if isinstance(source, str) else source
    handlers = [
        f"{module}:{handler.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Try)
        for handler in node.handlers
        if handler.type is None
    ]
    return handlers


def _subprocess_creations(module: str, source: str | ast.AST) -> list[str]:
    tree = ast.parse(source) if isinstance(source, str) else source
    entries: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call = _receiver_call(node)
        if call is not None and call in SUBPROCESS_CALLS:
            recv, name = call
            entries.append(f"{module}:{node.lineno} ({recv}.{name})")
    imports = [
        f"{module}:{node.lineno} (import subprocess)"
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        and any(alias.name.split(".")[0] == "subprocess" for alias in node.names)
    ]
    return entries + imports


def _network_dependencies(module: str, source: str | ast.AST) -> list[str]:
    tree = ast.parse(source) if isinstance(source, str) else source
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name != "urllib.parse" and alias.name.split(".")[0] in NETWORK_ROOTS:
                    found.append(f"{module}:{node.lineno} (import {alias.name})")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if node.module != "urllib.parse" and root in NETWORK_ROOTS:
                found.append(f"{module}:{node.lineno} (from {node.module} import)")
        elif isinstance(node, ast.Call):
            call = _receiver_call(node)
            if call is not None and call in NETWORK_CALLS:
                recv, name = call
                found.append(f"{module}:{node.lineno} ({recv}.{name})")
    return found


def _file_entries(module: str, source: str | ast.AST) -> list[str]:
    tree = ast.parse(source) if isinstance(source, str) else source
    entries: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call = _receiver_call(node)
        if call is not None:
            recv, name = call
            if recv == "os" and name in OS_FILE_OPS:
                entries.append(f"{module}:{node.lineno} (os.{name})")
            elif name in PATH_FILE_METHODS:
                entries.append(f"{module}:{node.lineno} (.{name})")
        elif isinstance(node.func, ast.Name) and node.func.id == "open":
            entries.append(f"{module}:{node.lineno} (open)")
    return entries


def _starts_with(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(
        name == prefix or name.startswith(prefix + ".")
        for prefix in prefixes
    )

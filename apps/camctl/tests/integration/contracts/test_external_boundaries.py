"""源码外部调用边界的集成检查。

固化路线图 377 行源码审计的结构结论：子进程创建只有受管工具
进程一处入口；生产代码不导入网络客户端也不建立网络连接；文件
系统直接调用只出现在按责任登记的模块；异常分支全部携带显式类
型。模块依赖方向由 ``test_dependencies.py`` 的导入图规则覆盖，
本模块只检查调用边界。每条规则对合成违规样本必须能够报出。
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "src" / "camctl"

#: 受管工具进程模块：全仓库唯一允许创建子进程的地方；其他模块
#: （含 adb 传输适配）都通过它的运行接口使用子进程。
SUBPROCESS_OWNER = "camctl.operations.process"
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
#: 网络客户端相关的标准库与第三方根名：CLI 部署不使用网络，
#: 导入即违规（urllib/http 全族包含纯解析用途，一并禁止）。
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
)


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(SRC_ROOT.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _receiver_call(node: ast.Call) -> tuple[str, str] | None:
    """返回（属性接收者限定名, 方法名）；非限定调用返回 None。"""
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id, func.attr
    return None


def _bare_excepts(module: str, source: str) -> list[str]:
    tree = ast.parse(source)
    handlers = [
        f"{module}:{handler.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Try)
        for handler in node.handlers
        if handler.type is None
    ]
    return handlers


def _subprocess_creations(module: str, source: str) -> list[str]:
    tree = ast.parse(source)
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


def _network_dependencies(module: str, source: str) -> list[str]:
    tree = ast.parse(source)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in NETWORK_ROOTS:
                    found.append(f"{module}:{node.lineno} (import {alias.name})")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in NETWORK_ROOTS:
                found.append(f"{module}:{node.lineno} (from {node.module} import)")
        elif isinstance(node, ast.Call):
            call = _receiver_call(node)
            if call is not None and call in NETWORK_CALLS:
                recv, name = call
                found.append(f"{module}:{node.lineno} ({recv}.{name})")
    return found


def _file_entries(module: str, source: str) -> list[str]:
    tree = ast.parse(source)
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


def _scan_real_sources() -> dict[str, str]:
    return {
        _module_name(path): path.read_text(encoding="utf-8")
        for path in sorted(SRC_ROOT.rglob("*.py"))
    }


def _starts_with(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(
        name == prefix or name.startswith(prefix + ".")
        for prefix in prefixes
    )


class TestRealSourceBoundaries:
    def test_scan_covers_real_modules(self) -> None:
        """检查对象不是空包：真实源码被完整扫描。"""
        sources = _scan_real_sources()
        assert len(sources) >= 100
        for anchor in (
            "camctl.operations.process",
            "camctl.session.locks",
            "camctl.resources",
            "camctl.acceptance.input",
            "camctl.reporting.publication",
            "camctl.host_files.io",
        ):
            assert anchor in sources, f"扫描缺少锚点模块: {anchor}"

    def test_subprocess_creation_only_in_managed_process(self) -> None:
        """子进程创建与 subprocess 导入只出现在受管工具进程模块。"""
        violations = [
            entry
            for module, source in _scan_real_sources().items()
            if module != SUBPROCESS_OWNER
            for entry in _subprocess_creations(module, source)
        ]
        assert violations == []

    def test_no_network_client_dependency(self) -> None:
        """生产代码不导入网络客户端，也不建立网络连接。"""
        violations = [
            entry
            for module, source in _scan_real_sources().items()
            for entry in _network_dependencies(module, source)
        ]
        assert violations == []

    def test_file_calls_only_in_registered_modules(self) -> None:
        """文件系统直接调用只出现在登记责任的模块。"""
        violations = [
            entry
            for module, source in _scan_real_sources().items()
            if not _starts_with(module, FILE_ENTRY_PREFIXES)
            for entry in _file_entries(module, source)
        ]
        assert violations == []

    def test_no_bare_except(self) -> None:
        """异常分支全部携带显式类型，没有裸 except。"""
        violations = [
            entry
            for module, source in _scan_real_sources().items()
            for entry in _bare_excepts(module, source)
        ]
        assert violations == []


class TestCheckerDetectsViolations:
    """用故意违规的最小样本确认每条规则都能够失败。"""

    def test_checker_reports_each_synthetic_violation(self) -> None:
        assert _bare_excepts(
            "camctl.fake", "try:\n    pass\nexcept:\n    pass\n"
        ) == ["camctl.fake:3"]
        assert _subprocess_creations(
            "camctl.capture.fake", "import subprocess\n"
        ) == ["camctl.capture.fake:1 (import subprocess)"]
        assert _subprocess_creations(
            "camctl.reporting.fake",
            "asyncio.create_subprocess_exec('adb')\n",
        ) == ["camctl.reporting.fake:1 (asyncio.create_subprocess_exec)"]
        assert _network_dependencies(
            "camctl.devices.fake", "from urllib.request import urlopen\n"
        ) == ["camctl.devices.fake:1 (from urllib.request import)"]
        assert _network_dependencies(
            "camctl.session.fake", "socket.socket()\n"
        ) == ["camctl.session.fake:1 (socket.socket)"]
        assert _file_entries(
            "camctl.cancellation.fake", "os.unlink(path)\n"
        ) == ["camctl.cancellation.fake:1 (os.unlink)"]
        assert _file_entries(
            "camctl.scheduling.fake", "open(path, 'rb')\n"
        ) == ["camctl.scheduling.fake:1 (open)"]
        assert _file_entries(
            "camctl.history.fake", "target.write_text(data)\n"
        ) == ["camctl.history.fake:1 (.write_text)"]

    def test_checker_allows_registered_entries(self) -> None:
        """登记模块内的合法入口与 str.replace 等同名无关调用不算违规。"""
        assert _file_entries(
            "camctl.session.locks", "os.open(path, os.O_RDWR)\n"
        ) == ["camctl.session.locks:1 (os.open)"]
        assert _starts_with("camctl.session.locks", FILE_ENTRY_PREFIXES)
        assert _file_entries(
            "camctl.contracts.fake", "text.replace('a', 'b')\n"
        ) == []
        assert _subprocess_creations(
            "camctl.cli.fake", "asyncio.run(main())\n"
        ) == []
        assert _file_entries(
            "camctl.cli.fake", "iter(items)\n"
        ) == []
